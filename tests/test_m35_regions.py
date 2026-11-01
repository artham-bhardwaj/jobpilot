"""Milestone 35: India / remote / Dubai coverage, region filters in the dashboard, and the removal of auto-apply and
background resume generation. Synthetic data; HTTP is canned (nothing reaches a real job board)."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from jobpilot.models import Job, JobSource, utcnow
from jobpilot.services import jobs as job_svc
from jobpilot.services import regional_sources, regions, settings_store
from jobpilot.sources.base import JobData
from tests.helpers import make_http


# ---- the classifier --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("location,remote_type,title,expected", [
    ("Bengaluru, India", "unknown", "SDE", ["india"]),
    ("Gurugram", "hybrid", "SDE", ["india"]),
    ("IN - Bengaluru, India", "onsite", "SDE", ["india"]),
    ("Remote - India", "unknown", "SDE", ["india", "remote"]),
    ("Bengaluru, India; Remote - Remote", "remote", "SDE", ["india", "remote"]),
    ("Remote", "remote", "SDE", ["remote"]),
    ("Remote — Worldwide", "remote", "SDE", ["remote"]),
    ("Remote (EMEA)", "remote", "SDE", ["remote"]),
    ("APAC", "remote", "SDE", ["remote"]),
    ("Remote - US", "remote", "SDE", ["other"]),
    ("Remote, U.S.", "remote", "SDE", ["other"]),
    ("Remote, Germany", "remote", "SDE", ["other"]),
    ("London", "remote", "SDE", ["other"]),
    ("Dubai, United Arab Emirates", "onsite", "SDE", ["dubai"]),
    ("Abu Dhabi, ARE", None, "SDE", ["dubai"]),
    ("Remote — United Arab Emirates", "remote", "SDE", ["remote", "dubai"]),
    ("Karachi, Pakistan", None, "SDE", ["other"]),
    (None, "unknown", "Software Engineer (India)", ["india"]),
    (None, "unknown", "Software Engineer", ["other"]),
])
def test_regions_of_a_location(location, remote_type, title, expected):
    assert regions.classify(location, remote_type, title) == expected


def _add(db, title, location, remote_type=None, score=80.0, **kw):
    job, _ = job_svc.upsert(db, None, JobData(title=title, company_name=kw.pop("company", "Examplecorp"),
                                              location=location, remote_type=remote_type,
                                              description_text="Java, Spring Boot, SQL (synthetic)",
                                              source_url=f"https://example.com/{title.replace(' ', '-')}"), "manual")
    job.relevance_score = score
    job.category = kw.pop("category", "java_backend")  # one of your role categories, as matching would set
    db.commit()
    return job


def test_ingest_tags_jobs_and_search_filters_by_region(db):
    india = _add(db, "Backend Engineer A", "Pune, India")
    both = _add(db, "Backend Engineer B", "Remote - India", "remote")
    dubai = _add(db, "Backend Engineer C", "Dubai, United Arab Emirates")
    us = _add(db, "Backend Engineer D", "Remote - US", "remote")
    assert (india.regions, both.regions, dubai.regions, us.regions) == (",india,", ",india,remote,", ",dubai,", ",other,")
    ids = lambda region: {j.id for j in job_svc.search(db, region=region)[0]}  # noqa: E731
    assert ids("india") == {india.id, both.id} and ids("remote") == {both.id} and ids("dubai") == {dubai.id}
    assert ids("other") == {us.id} and len(ids(None)) == 4 and len(ids("nonsense")) == 4
    assert regions.counts(db) == {"all": 4, "india": 2, "remote": 1, "dubai": 1, "other": 1}
    us.location = "Bengaluru, India"
    assert regions.backfill(db, only_missing=False) == 1 and us.regions == ",india,remote,", "remote, based in India"


def test_remote_job_for_another_country_gets_a_location_warning(db):
    from jobpilot.services import matching
    from tests.helpers import seed_profile

    seed_profile(db)
    us = _add(db, "Java Backend Engineer", "Remote - US", "remote")
    anywhere = _add(db, "Java Backend Engineer II", "Remote", "remote")
    matching.rematch_all(db)
    check = lambda j: next(c for c in j.match_details["eligibility"]["checks"] if c["name"] == "location")  # noqa: E731
    assert check(us)["status"] == "warn" and "not India or the UAE" in check(us)["explanation"]
    assert check(anywhere)["status"] == "pass"


# ---- dashboard -------------------------------------------------------------------------------------------------

def test_jobs_page_has_region_tabs_badges_and_resume_button(client, db):
    _add(db, "Backend Engineer A", "Pune, India")
    d = _add(db, "Backend Engineer C", "Dubai, United Arab Emirates")
    page = client.get("/jobs").text
    for label in ("All my regions", "India", "Remote", "Dubai / UAE"):
        assert label in page
    assert 'class="badge r-india"' in page and 'class="badge r-dubai"' in page
    assert f'href="/jobs/{d.id}#apply"' in page, "a resume / record button on every row (the apply panel)"
    only = client.get("/jobs?region=dubai").text
    assert "Backend Engineer C" in only and "Backend Engineer A" not in only
    assert 'name="region" value="dubai"' in only, "other filters keep the region"
    detail = client.get(f"/jobs/{d.id}").text
    assert 'class="badge r-dubai"' in detail and "Resume for this job" in detail


def test_dashboard_shows_jobs_by_region(client, db):
    _add(db, "Backend Engineer A", "Pune, India", score=91)
    _add(db, "Backend Engineer B", "Remote", "remote", score=77)
    page = client.get("/").text
    assert "SDE-1 jobs by region" in page and 'href="/jobs?region=remote"' in page
    assert "Backend Engineer A" in page and "Backend Engineer B" in page


# ---- sources: region filters and the new boards -----------------------------------------------------------------

def test_board_sources_keep_india_remote_and_dubai_roles(settings):
    from jobpilot.sources.ats import GreenhouseAdapter

    base = f"{GreenhouseAdapter.BASE}/examplecorp/jobs"
    names = ["Bengaluru, India", "Remote - India", "Remote - US", "Dubai, United Arab Emirates", "London", "Remote"]
    listing = {"jobs": [{"id": i, "title": "Software Engineer", "location": {"name": n}} for i, n in enumerate(names)]}
    detail = lambda i: {"id": i, "title": "Software Engineer", "location": {"name": names[i]},  # noqa: E731
                        "content": "&lt;p&gt;Java&lt;/p&gt;", "absolute_url": f"https://example.com/{i}"}
    routes = {base: (200, json.dumps(listing)), **{f"{base}/{i}": (200, json.dumps(detail(i))) for i in range(6)}}
    cfg = {"board_token": "examplecorp", "title_keywords": ["software engineer"], "location_keywords": ["india"],
           "regions": ["india", "remote", "dubai"]}
    res = GreenhouseAdapter().fetch(make_http(settings, routes), cfg, None)
    assert sorted(j.location for j in res.items) == sorted(["Bengaluru, India", "Remote - India", "Remote",
                                                           "Dubai, United Arab Emirates"])
    india_only = GreenhouseAdapter().fetch(make_http(settings, routes), {**cfg, "regions": []}, None)
    assert sorted(j.location for j in india_only.items) == ["Bengaluru, India", "Remote - India"]
    with pytest.raises(ValueError, match="regions"):
        GreenhouseAdapter().validate_config({"board_token": "x", "regions": ["mars"]})


def test_himalayas_reads_one_page_per_query_and_drops_other_countries(settings):
    from jobpilot.sources.remote import HimalayasAdapter

    def item(title, where, guid):
        return {"title": title, "companyName": "Remotecorp", "locationRestrictions": where, "guid": guid,
                "applicationLink": guid + "?utm_source=api", "description": "<p>Java and Kafka</p>",
                "pubDate": 1792760145, "employmentType": "Full Time", "minSalary": None, "maxSalary": None}
    body = {"jobs": [item("Backend Engineer", ["India"], "https://himalayas.app/c/a"),
                     item("Software Engineer", [], "https://himalayas.app/c/b"),
                     item("Software Engineer US", ["United States"], "https://himalayas.app/c/c"),
                     item("Senior Software Engineer", ["India"], "https://himalayas.app/c/d"),
                     item("Account Manager", [], "https://himalayas.app/c/e")]}
    http = make_http(settings, {HimalayasAdapter.URL: (200, json.dumps(body))})
    cfg = {"queries": ["software engineer", "backend"], "countries": ["India", "United Arab Emirates"],
           "title_keywords": ["software engineer", "backend"], "exclude_keywords": ["senior"]}
    res = HimalayasAdapter().fetch(http, cfg, None)
    by_loc = {j.location: j for j in res.items}
    assert set(by_loc) == {"Remote — India", "Remote — Worldwide"}, "US-only and senior roles dropped"
    j = by_loc["Remote — India"]
    assert j.remote_type == "remote" and j.company_name == "Remotecorp" and j.description_text == "Java and Kafka"
    assert j.apply_url.endswith("utm_source=api") and j.external_id == "https://himalayas.app/c/a"
    assert http.session.calls.count(HimalayasAdapter.URL) == 4, "2 queries × 2 countries, never a second page"
    assert not res.full_snapshot


def test_jobicy_keeps_regions_and_reports_refusals(settings):
    from jobpilot.sources.remote import JobicyAdapter

    def item(i, title, geo):
        return {"id": i, "url": f"https://jobicy.com/jobs/{i}", "jobTitle": title, "companyName": "Remotecorp",
                "jobGeo": geo, "jobType": ["Full-Time"], "jobDescription": "<p>Python</p>", "pubDate": "2026-10-20 10:00:00"}
    body = {"jobs": [item(1, "Software Engineer", "Anywhere"), item(2, "Software Engineer", "Australia"),
                     item(3, "Backend Developer", "APAC,  EMEA"), item(4, "Software Engineer", "Singapore,  UK,  USA")]}
    http = make_http(settings, {JobicyAdapter.URL: (200, json.dumps(body))})
    res = JobicyAdapter().fetch(http, {"geos": ["anywhere"], "title_keywords": ["software engineer", "backend"]}, None)
    assert sorted(j.location for j in res.items) == ["Remote — APAC, EMEA", "Remote — Anywhere"]
    assert all(j.apply_url.startswith("https://jobicy.com/jobs/") for j in res.items), "credited: Jobicy's own page"
    refused = make_http(settings, {JobicyAdapter.URL: (200, json.dumps({"success": False, "error": "bad geo"}))})
    assert "refused" in JobicyAdapter().fetch(refused, {"geos": ["apac"], "title_keywords": ["engineer"]}, None).errors[0]
    with pytest.raises(ValueError, match="geos"):
        JobicyAdapter().validate_config({"geos": ["india"], "title_keywords": ["engineer"]})


def test_regional_sources_are_added_once_and_never_overwrite_yours(db, client):
    mine = job_svc.save_source(db, job_svc.SourceIn(
        name="Bybit (Greenhouse)", kind="greenhouse", config={"board_token": "bybit", "title_keywords": ["golang"]}))
    india = job_svc.save_source(db, job_svc.SourceIn(
        name="Rubrik (Greenhouse)", kind="greenhouse",
        config={"board_token": "rubrik", "location_keywords": ["india"], "title_keywords": ["software engineer"]}))
    db.commit()
    plan = regional_sources.plan(db)
    assert "Bybit (Greenhouse)" not in plan["add"] and "Himalayas (remote, open to India/UAE)" in plan["add"]
    assert plan["extend"] == ["Rubrik (Greenhouse)"]
    assert "Add regional sources" in client.get("/sources").text
    r = client.post("/sources/regional")
    assert r.status_code == 303 and "Added" in r.headers["location"]
    db.expire_all()
    assert db.get(JobSource, mine.id).config == {"board_token": "bybit", "title_keywords": ["golang"]}, "yours kept"
    cfg = db.get(JobSource, india.id).config
    assert cfg["regions"] == ["india", "remote", "dubai"] and cfg["location_keywords"] == ["india"]
    kinds = {s.kind for s in db.scalars(select(JobSource))}
    assert {"himalayas", "jobicy", "greenhouse", "amazon_jobs", "microsoft_careers"} <= kinds
    assert regional_sources.plan(db) == {"add": [], "extend": []}
    assert "Add regional sources" not in client.get("/sources").text
    uae = db.scalar(select(JobSource).where(JobSource.name == "Amazon UAE (amazon.jobs)"))
    assert uae.config["country"] == "ARE"


# ---- auto-apply and background resumes are gone ------------------------------------------------------------------

def test_auto_apply_is_removed(client, db):
    import importlib.util

    from jobpilot.services import scheduler

    assert importlib.util.find_spec("jobpilot.services.autopilot") is None
    assert "autopilot" not in scheduler.TASKS and "tailor_resumes" not in scheduler.TASKS
    assert "autopilot" not in settings_store.DEFAULTS
    assert client.post("/autopilot/pause").status_code in (404, 405)
    page = client.get("/system").text
    assert "Autonomous applications" not in page and "never applies to jobs for you" in page
    r = client.post("/outreach-control/pause")
    assert r.status_code == 303
    from jobpilot.services import outreach_auto

    db.expire_all()
    assert outreach_auto.is_paused(db)


def test_old_saved_settings_of_removed_features_are_ignored(db):
    from jobpilot.models import AppSetting

    sched = settings_store.get(db, "scheduler")
    db.add(AppSetting(key="scheduler", value={**sched, "tasks": {**sched["tasks"],
                                                                 "autopilot": {"enabled": True, "every_hours": 1},
                                                                 "tailor_resumes": {"enabled": True, "every_hours": 6}}}))
    db.add(AppSetting(key="notifications", value={"kinds": {"submission_batch": True, "tailored_resume": True,
                                                            "high_match": False}}))
    db.add(AppSetting(key="tailored_resumes", value={"enabled": True, "max_per_run": 5}))
    db.commit()
    tasks = settings_store.get(db, "scheduler")["tasks"]
    assert "autopilot" not in tasks and "tailor_resumes" not in tasks
    kinds = settings_store.get(db, "notifications")["kinds"]
    assert "submission_batch" not in kinds and kinds["high_match"] is False
    assert "max_per_run" not in settings_store.get(db, "tailored_resumes")
    for key in ("scheduler", "notifications", "tailored_resumes"):  # what the Settings page saves back validates
        settings_store.validate(key, settings_store.get(db, key))


def test_telegram_is_quieter_by_default():
    kinds = settings_store.DEFAULTS["notifications"]["kinds"]
    assert kinds["high_match"] and kinds["daily_summary"] and kinds["task_failure"]
    assert not kinds["apply_check"] and not kinds["relevant_digest"] and not kinds["questions"]


def test_job_first_seen_today_is_counted_as_new(db):
    j = _add(db, "Backend Engineer A", "Pune, India")
    j.first_seen_at = utcnow()
    db.commit()
    (india,) = [r for r in regions.overview(db) if r["key"] == "india"]
    assert india["count"] == 1 and india["new"] == 1 and india["top"][0].id == j.id
    assert isinstance(db.get(Job, j.id).regions, str)
