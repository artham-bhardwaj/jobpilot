"""Relevance cleanup (reversible hiding) and the Jobs search syntax."""

from __future__ import annotations

from sqlalchemy import select

from jobpilot.models import Application, Job
from jobpilot.services import cleanup, search_query, settings_store
from jobpilot.services import jobs as job_svc
from jobpilot.sources.base import JobData


def _add(db, title, location="Pune, India", category="java_backend", score=80.0, desc="Java, SQL (synthetic).",
         company="Examplecorp"):
    job, _ = job_svc.upsert(db, None, JobData(title=title, company_name=company, location=location,
                                              description_text=desc,
                                              source_url=f"https://example.com/{title.replace(' ', '-')}"), "manual")
    job.category = category
    job.relevance_score = score
    db.commit()
    return job


# ---- cleanup -------------------------------------------------------------------------------------------------

def test_cleanup_hides_irrelevant_jobs_reversibly_and_spares_your_choices(db):
    keep = _add(db, "Backend Engineer A")
    senior = _add(db, "Senior Backend Engineer")
    far = _add(db, "Backend Engineer Far", location="Berlin, Germany")
    norole = _add(db, "Backend Engineer NoRole", category=None)
    low = _add(db, "Backend Engineer Low", score=20.0)
    saved = _add(db, "Senior Saved Engineer")
    job_svc.set_user_status(db, saved, "shortlisted")
    applied = _add(db, "Senior Applied Engineer")
    db.add(Application(job_id=applied.id, status="applied"))
    mine = _add(db, "Backend Engineer Mine")
    job_svc.set_user_status(db, mine, "dismissed")
    db.commit()

    p = cleanup.plan(db)
    assert {j.id for j, _ in p["hide"]} == {senior.id, far.id, norole.id, low.id}
    assert p["by_reason"] == {"level": 1, "region": 1, "role": 1, "score": 1, "conflict": 0}
    assert keep.user_status == "new", "plan changes nothing"

    assert cleanup.run(db)["hidden"] == 4
    db.commit()
    assert senior.user_status == "dismissed" and senior.hidden_reason == "cleanup:level"
    assert far.hidden_reason == "cleanup:region" and cleanup.label(far.hidden_reason) == "outside India / Dubai / remote"
    assert saved.user_status == "shortlisted" and applied.user_status == "new" and mine.hidden_reason is None
    assert cleanup.run(db)["hidden"] == 0, "idempotent"

    # Your own status change replaces the cleanup's mark.
    job_svc.set_user_status(db, low, "new")
    assert low.hidden_reason is None
    assert cleanup.undo(db) == 3
    db.commit()
    assert senior.user_status == "new" and senior.hidden_reason is None
    assert mine.user_status == "dismissed", "your own dismissal is never undone"


def test_cleanup_runs_after_collection_and_hidden_jobs_stay_hidden(db, settings):
    senior = _add(db, "Senior Backend Engineer")
    summary = job_svc.collect_all(db, settings, source_ids=[-1])  # no sources: only the post-collection steps
    assert summary["hidden"] == 1 and senior.user_status == "dismissed"
    # Seen again by a source: still hidden (a deleted job would come back as new).
    job, outcome = job_svc.upsert(db, None, JobData(title=senior.title, company_name="Examplecorp",
                                                    location="Pune, India", description_text="Java, SQL (synthetic).",
                                                    source_url="https://example.com/Senior-Backend-Engineer"), "manual")
    assert job.id == senior.id and job.user_status == "dismissed"

    settings_store.put(db, "job_cleanup", {"auto": False, "min_score": 40})
    _add(db, "Lead Engineer")
    assert "hidden" not in job_svc.collect_all(db, settings, source_ids=[-1])


def test_cleanup_buttons_and_hidden_view(client, db):
    _add(db, "Backend Engineer A")
    senior = _add(db, "Senior Backend Engineer")
    page = client.get("/jobs").text
    assert "Clean up 1 irrelevant" in page
    r = client.post("/jobs/cleanup")
    assert r.status_code == 303 and "Hid+1" in r.headers["location"] or "Hid%201" in r.headers["location"]
    page = client.get("/jobs").text
    assert "Hidden by cleanup (1)" in page and "Clean up" not in page
    hidden = client.get("/jobs?hidden=cleanup").text
    assert "Senior Backend Engineer" in hidden and "hidden: above SDE-1" in hidden and "Backend Engineer A" not in hidden
    assert "Restore all 1" in hidden
    client.post(f"/jobs/{senior.id}/status", data={"status": "new", "next": "/jobs?hidden=cleanup"})
    db.expire_all()
    assert db.get(Job, senior.id).hidden_reason is None
    assert client.post("/jobs/cleanup/undo").status_code == 303


# ---- search --------------------------------------------------------------------------------------------------

def test_parse_terms_phrases_fields_negation_and_synonyms():
    q = search_query.parse('java "low level design" -senior company:amazon in:pune k8s c++:x')
    shown = [t.display() for t in q.terms]
    assert shown == ["java", '"low level design"', "-senior", "company:amazon", "location:pune", "k8s", "\"c++ x\""]
    assert "kubernetes" in q.terms[5].variants
    assert q.without(2) == 'java "low level design" company:amazon location:pune k8s \"c++ x\"'


def test_search_whole_words_fields_exclusion_synonyms_and_ranking(db):
    title_hit = _add(db, "Kafka Backend Engineer", score=50.0, desc="Build services.")
    desc_hit = _add(db, "Backend Engineer B", score=95.0, desc="You will work with Kafka streams and Java.")
    js = _add(db, "Frontend Engineer", desc="JavaScript and React.", company="Amazon")
    k8s = _add(db, "Platform Engineer", desc="We run everything on Kubernetes.")
    senior_txt = _add(db, "Backend Engineer C", desc="Kafka. Mentor a senior team.")

    def ids(q, **kw):
        return [j.id for j in job_svc.search(db, q=q, **kw)[0]]

    assert ids("kafka")[:1] == [title_hit.id], "title hits rank above a higher-scored description hit"
    assert set(ids("kafka")) == {title_hit.id, desc_hit.id, senior_txt.id}
    assert ids("kafka -senior") == [title_hit.id, desc_hit.id]
    assert js.id not in ids("java"), "whole words: java ≠ javascript"
    assert ids("js") == [js.id]
    assert ids("k8s") == [k8s.id], "skill synonyms"
    assert ids("company:amazon") == [js.id] and ids("title:kafka") == [title_hit.id]
    assert ids('"kafka streams"') == [desc_hit.id]
    jobs, total = job_svc.search(db, q="kafka", limit=1, offset=1)
    assert total == 3 and len(jobs) == 1
    assert "<mark>Kafka</mark>" in str(desc_hit.search_info.get("snippet", "")) or desc_hit.search_info["where"]


def test_jobs_page_search_ui(client, db):
    _add(db, "Kafka Backend Engineer")
    _add(db, "Senior Kafka Engineer")
    page = client.get("/jobs?q=kafka+-senior").text
    assert "<mark>Kafka</mark> Backend Engineer" in page and "Senior Kafka Engineer" not in page
    assert 'class="pill neg"' in page and "Search tips" in page
    none = client.get("/jobs?q=senior+kafka").text
    assert "No jobs match" in none and "at any level, role or region" in none


def test_new_setting_validates():
    settings_store.validate("job_cleanup", settings_store.DEFAULTS["job_cleanup"])
    assert select  # imported for parity with other tests
