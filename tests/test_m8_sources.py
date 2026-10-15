"""Milestone 8 — Stage C: source engine (offline; fake HTTP; synthetic fixtures)."""

from __future__ import annotations

import json

import pytest
import requests
from sqlalchemy import select

from jobpilot.models import Job, JobSighting, JobSource, SourceRun
from jobpilot.services import jobs as job_svc
from jobpilot.sources.base import JobData
from tests.helpers import fixture_text, make_http

GH = "https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs"


def _gh_source(db):
    return job_svc.save_source(db, job_svc.SourceIn(name="Examplecorp", kind="greenhouse", company_name="Examplecorp",
                                                    config={"board_token": "examplecorp"}))


def _board(n_jobs: int, start: int = 1) -> str:
    jobs = [{"id": i, "title": f"Software Engineer {i}", "absolute_url": f"https://boards.greenhouse.io/examplecorp/jobs/{i}",
             "location": {"name": "Pune"}, "content": "Requirements: Java, SQL"} for i in range(start, start + n_jobs)]
    return json.dumps({"jobs": jobs})


# ---- Canonical URLs and dedup -------------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("https://Jobs.Lever.co/acme/abc-123/apply?utm_source=linkedin&lever-source=x",
     "https://jobs.lever.co/acme/abc-123"),
    ("https://boards.greenhouse.io/acme/jobs/1?gh_src=abc#app", "https://boards.greenhouse.io/acme/jobs/1"),
    ("https://careers.example.com:443/jobs/9/?b=2&a=1&utm_campaign=x", "https://careers.example.com/jobs/9?a=1&b=2"),
])
def test_canonical_url_equivalence(a, b):
    assert job_svc.canonical_url(a) == job_svc.canonical_url(b)


def test_canonical_url_keeps_identifying_params_and_rejects_unsafe():
    assert job_svc.canonical_url("https://x.example.com/j?id=1") != job_svc.canonical_url("https://x.example.com/j?id=2")
    assert job_svc.canonical_url("javascript:alert(1)") is None


def test_cross_source_duplicate_by_canonical_apply_url(db):
    a = job_svc.get_or_create_source(db, "Board A", "manual")
    b = job_svc.get_or_create_source(db, "Board B", "manual")
    j1, o1 = job_svc.upsert(db, a, JobData(title="Backend Engineer", company_name="Examplecorp", location="Pune",
                                           apply_url="https://jobs.lever.co/examplecorp/abc-123/apply"), "manual")
    # Same posting, different title formatting and a tracking parameter: matched by canonical URL.
    j2, o2 = job_svc.upsert(db, b, JobData(title="Backend Engineer - Platform", company_name="Examplecorp Inc",
                                           location="Pune, India",
                                           apply_url="https://jobs.lever.co/examplecorp/abc-123?utm_source=feed"), "manual")
    assert o1 == "new" and o2 == "duplicate" and j1.id == j2.id
    assert {s.source_id for s in j1.sightings} == {a.id, b.id}
    assert j1.title == "Backend Engineer" and j1.company_name == "Examplecorp", "a duplicate never overwrites the original"


def test_provenance_and_raw_description_preserved(db, settings):
    _gh_source(db)
    job_svc.collect_all(db, settings, make_http(settings, {GH: (200, fixture_text("greenhouse_jobs.json"))}))
    job = db.scalar(select(Job).where(Job.external_id == "1001"))
    assert "<h3>Requirements</h3>" in job.description_raw, "original HTML kept"
    assert "<h3>" not in job.description_text
    assert job.first_seen_at and job.last_checked_at and job.last_check_status == "listed by source"
    assert job.sightings[0].url == "https://boards.greenhouse.io/examplecorp/jobs/1001"
    assert job.apply_url_canonical == "https://boards.greenhouse.io/examplecorp/jobs/1001"


# ---- Expiry safety -------------------------------------------------------------------------

def test_expiry_requires_consecutive_misses_and_resets(db, settings):
    _gh_source(db)
    job_svc.collect_all(db, settings, make_http(settings, {GH: (200, _board(3))}))
    job_svc.collect_all(db, settings, make_http(settings, {GH: (200, _board(2))}))  # job 3 missing once
    j3 = db.scalar(select(Job).where(Job.external_id == "3"))
    assert j3.status == "active" and j3.miss_count == 1
    job_svc.collect_all(db, settings, make_http(settings, {GH: (200, _board(3))}))  # it's back
    assert j3.miss_count == 0 and j3.status == "active"
    for _ in range(2):
        job_svc.collect_all(db, settings, make_http(settings, {GH: (200, _board(2))}))
    assert j3.status == "expired"


def test_empty_snapshot_is_an_anomaly_not_mass_expiry(db, settings):
    src = _gh_source(db)
    job_svc.collect_all(db, settings, make_http(settings, {GH: (200, _board(3))}))
    for _ in range(3):
        res = job_svc.collect_all(db, settings, make_http(settings, {GH: (200, '{"jobs": []}')}))
    assert res["sources"]["Examplecorp"]["status"] == "anomaly"
    assert all(j.status == "active" and j.miss_count == 0 for j in db.scalars(select(Job)))
    assert src.last_status == "anomaly"


def test_mass_disappearance_is_an_anomaly(db, settings):
    _gh_source(db)
    job_svc.collect_all(db, settings, make_http(settings, {GH: (200, _board(20))}))
    for _ in range(3):
        res = job_svc.collect_all(db, settings, make_http(settings, {GH: (200, _board(3))}))
    assert res["sources"]["Examplecorp"]["status"] == "anomaly"
    assert db.scalar(select(Job).where(Job.external_id == "15")).status == "active"


@pytest.mark.parametrize("response,expected_status", [
    ((429, "slow down", {"Retry-After": "0"}), "unavailable"),
    ((503, "maintenance"), "unavailable"),
    (requests.Timeout("timed out"), "unavailable"),
    (requests.ConnectionError("offline"), "unavailable"),
    ((404, "no such board"), "failed"),
    ((200, "<html>not json</html>"), "failed"),
    ((200, '{"unexpected": true}'), "failed"),
])
def test_unavailable_or_malformed_sources_never_expire_jobs(db, settings, monkeypatch, response, expected_status):
    monkeypatch.setattr("jobpilot.sources.http.time.sleep", lambda s: None)
    src = _gh_source(db)
    job_svc.collect_all(db, settings, make_http(settings, {GH: (200, _board(3))}))
    for _ in range(3):
        res = job_svc.collect_all(db, settings, make_http(settings, {GH: response}))
    assert res["sources"]["Examplecorp"]["status"] == expected_status, res
    assert all(j.status == "active" and j.miss_count == 0 for j in db.scalars(select(Job)))
    run = db.scalar(select(SourceRun).where(SourceRun.source_id == src.id).order_by(SourceRun.id.desc()))
    assert run.status == expected_status and run.error
    if isinstance(response, tuple) and response[0] == 429:
        assert run.rate_limited >= 1


def test_jsonld_gone_page_expires_after_consecutive_404s(db, settings):
    url = "https://careers.example.net/jobs/sa-77"
    src = job_svc.save_source(db, job_svc.SourceIn(name="Pages", kind="jsonld", config={"urls": [url]}))
    job_svc.collect_all(db, settings, make_http(settings, {url: (200, fixture_text("career_page.html"))}))
    job = db.scalar(select(Job))
    job_svc.collect_all(db, settings, make_http(settings, {url: (404, "gone")}))
    assert job.status == "active" and job.miss_count == 1
    job_svc.collect_all(db, settings, make_http(settings, {url: (404, "gone")}))
    assert job.status == "expired"
    job_svc.collect_all(db, settings, make_http(settings, {url: (503, "down")}))
    assert src.last_status == "unavailable", "a temporarily failing page is not a broken source"


# ---- Dry run, history, health ---------------------------------------------------------------

def test_dry_run_saves_nothing_but_records_the_run(db, settings):
    src = _gh_source(db)
    res = job_svc.collect_all(db, settings, make_http(settings, {GH: (200, fixture_text("greenhouse_jobs.json"))}),
                              dry_run=True)
    db.commit()
    assert res["dry_run"] and res["sources"]["Examplecorp"]["new"] == 2 and "DRY RUN" in res["message"]
    assert db.scalar(select(Job)) is None and db.scalar(select(JobSighting)) is None
    run = db.scalar(select(SourceRun))
    assert run.dry_run and run.new == 2 and run.status == "success"
    assert src.last_status is None and src.last_run_at is None, "dry runs don't change source state"


def test_source_health_summary(db, settings):
    src = _gh_source(db)
    job_svc.collect_all(db, settings, make_http(settings, {GH: (200, _board(3))}))
    job_svc.collect_all(db, settings, make_http(settings, {GH: (503, "x")}))
    row = next(r for r in job_svc.source_health(db) if r["source"].id == src.id)
    assert row["jobs_total"] == 3 and row["jobs_active"] == 3
    assert row["last_run"].status == "unavailable" and row["errors_recent"] == 1
    assert src.last_success_at is not None and src.last_success_at < row["last_run"].started_at
    assert row["next_run"] is None, "scheduling is off by default"


def test_check_alive_refuses_automatic_use(db, settings):
    job, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title="Role", source_url="https://careers.example.com/jobs/9"))
    http = make_http(settings, {"https://careers.example.com/jobs/9": (200, "x")})
    with pytest.raises(PermissionError):
        job_svc.check_alive(db, http, job, initiated_by_user=False)
    assert http.session.calls == []
    http2 = make_http(settings, {"https://careers.example.com/jobs/9": (429, "x", {"Retry-After": "0"})})
    msg = job_svc.check_alive(db, http2, job, initiated_by_user=True)
    assert "temporarily unavailable" in msg and job.status == "active"


def test_ingest_bug_rolls_back_only_that_source(db, settings, monkeypatch):
    _gh_source(db)
    job_svc.save_source(db, job_svc.SourceIn(name="Lever co", kind="lever", config={"site": "fictionallabs"}))
    real_upsert = job_svc.upsert

    def flaky(session, source, data, method):
        if source.kind == "greenhouse":
            raise RuntimeError("synthetic bug")
        return real_upsert(session, source, data, method)

    monkeypatch.setattr(job_svc, "upsert", flaky)
    http = make_http(settings, {GH: (200, _board(2)),
                                "https://api.lever.co/v0/postings/fictionallabs": (200, fixture_text("lever_postings.json"))})
    res = job_svc.collect_all(db, settings, http)
    assert res["sources"]["Examplecorp"]["status"] == "failed"
    assert res["sources"]["Lever co"]["status"] == "success"
    assert [j.title for j in db.scalars(select(Job))] == ["Python Backend Developer"]


def test_sources_pages_and_dry_run_via_web(client, db, monkeypatch):
    src = _gh_source(db)
    db.commit()
    from tests.helpers import make_http as mk

    monkeypatch.setattr(job_svc, "PoliteHttp", lambda s: mk(s, {GH: (200, _board(2))}))
    r = client.post("/sources/run", data={"source_id": str(src.id), "dry_run": "1"})
    assert r.status_code == 303
    db.expire_all()
    assert db.scalar(select(Job)) is None and db.scalar(select(SourceRun)).dry_run
    page = client.get("/sources").text
    assert "Last success" in page and "dry run" in page.lower()
    detail = client.get(f"/sources/{src.id}").text
    assert "Run history" in detail
    assert client.get("/sources/999999").status_code == 404
