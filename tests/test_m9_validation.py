"""Milestone 9: operational-validation regressions (offline, synthetic data only)."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys

import pytest
import requests
from sqlalchemy import func, select, text

from jobpilot.models import Job
from jobpilot.services import jobs as job_svc
from jobpilot.sources.base import JobData
from tests.helpers import make_http


def _cli(tmp_path, *args):
    env = {"JOBPILOT_DATA_DIR": str(tmp_path / "d"), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    return subprocess.run([sys.executable, "-m", "jobpilot", *args], capture_output=True, text=True, env=env,
                          timeout=180, cwd=tmp_path)


def _schema5_db(tmp_path):
    from alembic import command

    from jobpilot.config import Settings
    from jobpilot.db import alembic_config, make_engine

    settings = Settings(_env_file=None, data_dir=tmp_path / "d")
    settings.ensure_dirs()
    eng = make_engine(settings)
    cfg = alembic_config(settings)
    with eng.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0005")
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO jobs (title, fingerprint, remote_type, status, user_status, first_seen_at, "
                          "last_seen_at, created_at, updated_at) VALUES ('Legacy Role', 'fp', 'unknown', 'active', "
                          "'new', '2026-01-01', '2026-01-01', '2026-01-01', '2026-01-01')"))
    eng.dispose()
    return settings


def test_db_current_reports_pending_migration_without_applying_it(tmp_path):
    settings = _schema5_db(tmp_path)
    r = _cli(tmp_path, "db", "current")
    assert r.returncode == 0, r.stderr[-500:]
    assert json.loads(r.stdout) == {"current": "0005", "head": "0006", "pending": True}
    con = sqlite3.connect(settings.db_file)
    assert con.execute("select version_num from alembic_version").fetchone()[0] == "0005"
    con.close()
    assert not list(settings.backup_dir.glob("*.zip"))

    r = _cli(tmp_path, "db", "upgrade")
    assert r.returncode == 0 and json.loads(r.stdout) == {"current": "0006", "head": "0006", "pending": False}
    assert len(list(settings.backup_dir.glob("*pre-migrate-0005*.zip"))) == 1
    con = sqlite3.connect(settings.db_file)
    assert con.execute("select title from jobs").fetchall() == [("Legacy Role",)]
    con.close()


# ---- Source failures never corrupt stored jobs (full-row comparison) ------------------------

GH = "https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs"


def _board(ids, title="Software Engineer", body="Requirements: Java, SQL") -> str:
    return json.dumps({"jobs": [{"id": i, "title": f"{title} {i}", "absolute_url": f"https://boards.greenhouse.io/examplecorp/jobs/{i}",
                                 "location": {"name": "Pune"}, "content": body} for i in ids]})


def _snapshot(db):
    db.commit()
    return {t: db.execute(text(f"SELECT * FROM {t} ORDER BY id")).fetchall()
            for t in ("jobs", "job_sightings", "companies")}


def _seeded(db, settings):
    src = job_svc.save_source(db, job_svc.SourceIn(name="Examplecorp", kind="greenhouse", company_name="Examplecorp",
                                                   config={"board_token": "examplecorp"}))
    job_svc.collect_all(db, settings, make_http(settings, {GH: (200, _board([1, 2, 3]))}))
    return src


@pytest.mark.parametrize("response", [
    (429, "slow down", {"Retry-After": "0"}), (503, "down"), requests.Timeout("t"), requests.ConnectionError("x"),
    (404, "gone"), (200, "<html>not json</html>"), (200, '{"jobs": null}'), (200, '{"jobs": ["not-an-object"]}'),
    (200, '{"jobs": [{"id": 1, "title": "X", "location": "not-an-object"}]}'), (200, '{"jobs": []}'),
])
def test_failed_or_anomalous_runs_leave_job_rows_identical(db, settings, monkeypatch, response):
    monkeypatch.setattr("jobpilot.sources.http.time.sleep", lambda s: None)
    _seeded(db, settings)
    before = _snapshot(db)
    for _ in range(3):
        res = job_svc.collect_all(db, settings, make_http(settings, {GH: response}))
        assert res["sources"]["Examplecorp"]["status"] in ("failed", "unavailable", "anomaly"), res
    assert _snapshot(db) == before


def test_ingest_error_midway_rolls_back_updates_to_existing_jobs(db, settings, monkeypatch):
    _seeded(db, settings)
    before = _snapshot(db)
    real_upsert = job_svc.upsert
    calls = []

    def fails_on_second(session, source, data, method):
        calls.append(data.external_id)
        if len(calls) == 2:
            raise RuntimeError("synthetic bug")
        return real_upsert(session, source, data, method)

    monkeypatch.setattr(job_svc, "upsert", fails_on_second)
    res = job_svc.collect_all(db, settings, make_http(settings, {GH: (200, _board([1, 2, 3], title="Renamed"))}))
    assert res["sources"]["Examplecorp"]["status"] == "failed" and len(calls) == 2
    assert _snapshot(db) == before, "the first job's update must be rolled back with the failed run"


def test_dry_run_on_existing_jobs_changes_nothing(db, settings):
    _seeded(db, settings)
    before = _snapshot(db)
    res = job_svc.collect_all(db, settings, make_http(settings, {GH: (200, _board([1, 4], title="Changed"))}), dry_run=True)
    s = res["sources"]["Examplecorp"]
    assert res["dry_run"] and s["new"] == 1 and s["updated"] == 1 and s["missing"] == 2
    assert _snapshot(db) == before


LEVER = "https://api.lever.co/v0/postings/biglabs"


def _lever_pages(total: int, per_page: int = 100) -> list:
    posts = [{"id": f"post-{i:06d}", "text": f"Engineer {i}", "hostedUrl": f"https://jobs.lever.co/biglabs/post-{i:06d}",
              "categories": {"location": "Pune"}, "descriptionPlain": "Requirements: Java"} for i in range(total)]
    return [(200, json.dumps(posts[s:s + per_page])) for s in range(0, total + 1, per_page)]


def test_lever_listing_beyond_page_cap_is_partial_and_expires_nothing(db, settings):
    """A board larger than the page cap must not be treated as a full listing (the rest would be expired)."""
    src = job_svc.save_source(db, job_svc.SourceIn(name="Big Labs", kind="lever", config={"site": "biglabs"}))
    old, _ = job_svc.upsert(db, src, JobData(title="Engineer 2400", external_id="post-002400",
                                     source_url="https://jobs.lever.co/biglabs/post-002400",
                                     apply_url="https://jobs.lever.co/biglabs/post-002400"), "api")
    for _ in range(3):
        res = job_svc.collect_all(db, settings, make_http(settings, {LEVER: _lever_pages(2500)}))
    s = res["sources"]["Big Labs"]
    assert s["status"] == "partial" and "incomplete" in s["errors"][0], s
    assert s["fetched"] == 2200
    db.refresh(old)
    assert old.status == "active" and old.miss_count == 0
    assert db.scalar(select(func.count(Job.id)).where(Job.status == "expired")) == 0


def test_lever_listing_under_cap_is_a_full_snapshot(db, settings):
    job_svc.save_source(db, job_svc.SourceIn(name="Big Labs", kind="lever", config={"site": "biglabs"}))
    res = job_svc.collect_all(db, settings, make_http(settings, {LEVER: _lever_pages(250)}))
    s = res["sources"]["Big Labs"]
    assert s["status"] == "success" and s["fetched"] == 250 and s["new"] == 250


# ---- Matching: unknown information never helps; incidental mentions are never mandatory ------

FULL_DESC = ("Requirements:\n- 1+ years of experience with Java\n- Spring Boot\n- SQL\n"
             "Salary: INR 12 LPA - 18 LPA. Full-time.")
CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _manual(db, title="Java Backend Engineer", desc=FULL_DESC, **kw):
    kw.setdefault("location", "Bengaluru")
    kw.setdefault("remote_type", "hybrid")
    job, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title=title, company_name="Examplecorp", description_text=desc, **kw))
    return job


@pytest.mark.parametrize("strip", ["location", "remote_type", "experience", "salary", "everything"])
def test_removing_information_never_raises_relevance_or_confidence(db, strip):
    from tests.helpers import seed_profile

    seed_profile(db)
    full = _manual(db)
    desc = FULL_DESC
    kw = {}
    if strip in ("location", "everything"):
        kw["location"] = None
    if strip in ("remote_type", "everything"):
        kw["remote_type"] = None
    if strip in ("experience", "everything"):
        desc = desc.replace("1+ years of experience with ", "")
    if strip in ("salary", "everything"):
        desc = desc.replace("Salary: INR 12 LPA - 18 LPA. ", "")
    less = _manual(db, title="Java Backend Engineer II", desc=desc, **kw)
    assert less.relevance_score <= full.relevance_score, (strip, less.relevance_score, full.relevance_score)
    assert CONFIDENCE_RANK[less.confidence] <= CONFIDENCE_RANK[full.confidence]
    assert not less.hard_conflict


def test_skills_mentioned_outside_requirements_are_not_mandatory():
    from jobpilot.services import job_parser

    p = job_parser.parse("Backend Engineer", (
        "About us: our platform team runs Kubernetes and Kafka at scale.\n"
        "Requirements:\n- Java\n- SQL\n"
        "Nice to have:\n- Docker\n"
        "Benefits: free AWS certification courses."))
    assert sorted(p.mandatory_skills) == ["java", "sql"]
    assert "kubernetes" not in p.mandatory_skills and "aws" not in p.mandatory_skills
    assert "docker" not in p.mandatory_skills


# ---- Email: what is sent is exactly what you approved last ---------------------------------

import base64  # noqa: E402

from tests.test_stage5 import GoogleStub, gmail_settings  # noqa: E402,F401  (fixture re-export)


class _NumberedDraftsStub(GoogleStub):
    """Like GoogleStub, but each created Gmail draft gets its own id (d-1, d-2, …)."""

    def request(self, method, url, **kw):
        r = super().request(method, url, **kw)
        if url.endswith("/drafts") and method == "POST":
            n = sum(1 for m, u, _ in self.calls if m == "POST" and u.endswith("/drafts"))
            r._content = json.dumps({"id": f"d-{n}", "message": {"id": f"m-{n}", "threadId": "t-1"}}).encode()
        return r


def _approved_draft(db, gmail_settings, body):
    from jobpilot.integrations import gmail
    from jobpilot.services import contacts as cs
    from jobpilot.services import drafts as ds
    from tests.helpers import seed_profile

    seed_profile(db)
    gmail.exchange_code(gmail_settings, "code", "http://127.0.0.1:1/", "v" * 64, http=GoogleStub())
    c, _ = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", email="jane@example.com", company_name="Examplecorp"))
    d = ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", contact_id=c.id))
    ds.edit_draft(db, d, ds.DraftEdit(to_address="jane@example.com", subject="Hello", body=body))
    ds.approve(db, d)
    return d


def test_editing_after_gmail_draft_never_sends_the_old_content(db, gmail_settings):
    from jobpilot.integrations import gmail
    from jobpilot.services import drafts as ds

    d = _approved_draft(db, gmail_settings, "Original text (synthetic).")
    stub = _NumberedDraftsStub()
    client = gmail.GmailClient(gmail_settings, http=stub)
    gmail.create_gmail_draft(db, gmail_settings, d, client)
    assert d.gmail_draft_id == "d-1"

    ds.edit_draft(db, d, ds.DraftEdit(to_address="jane@example.com", subject="Hello", body="Corrected text (synthetic)."))
    assert d.status == "draft" and d.gmail_draft_id is None, "edit must revoke approval and drop the stale Gmail draft"
    ds.approve(db, d)
    gmail.send_via_gmail(db, gmail_settings, d, client, "SEND")

    sent_id = next(kw["json"]["id"] for m, u, kw in stub.calls if u.endswith("/drafts/send"))
    assert sent_id == "d-2", "the old Gmail draft (d-1, original text) must not be sent"
    raws = [kw["json"]["message"]["raw"] for m, u, kw in stub.calls if m == "POST" and u.endswith("/drafts")]
    latest = base64.urlsafe_b64decode(raws[-1] + "==").decode(errors="replace")
    assert "Corrected text" in latest and "Original text" not in latest
    assert len(ds.revisions(db, d)) >= 2, "earlier versions are preserved as revisions"


def test_saving_identical_content_keeps_the_gmail_draft(db, gmail_settings):
    from jobpilot.integrations import gmail
    from jobpilot.services import drafts as ds

    d = _approved_draft(db, gmail_settings, "Same text (synthetic).")
    gmail.create_gmail_draft(db, gmail_settings, d, gmail.GmailClient(gmail_settings, http=_NumberedDraftsStub()))
    ds.edit_draft(db, d, ds.DraftEdit(to_address="jane@example.com", subject="Hello", body="Same text (synthetic)."))
    assert d.gmail_draft_id == "d-1", "unchanged content: the Gmail copy is still accurate"
    assert d.status == "draft", "any save still asks for re-approval (conservative, unchanged behaviour)"


def test_do_not_contact_added_after_approval_blocks_sending(db, gmail_settings):
    from jobpilot.integrations import gmail
    from jobpilot.services import contacts as cs

    d = _approved_draft(db, gmail_settings, "Hello (synthetic).")
    stub = _NumberedDraftsStub()
    cs.add_dnc(db, cs.DncIn(kind="email", value="jane@example.com"))
    db.refresh(d)
    with pytest.raises(gmail.GmailError):
        gmail.send_via_gmail(db, gmail_settings, d, gmail.GmailClient(gmail_settings, http=stub), "SEND")
    assert d.status == "approved" and d.sent_at is None
    assert not [u for m, u, kw in stub.calls if "/drafts" in u], "nothing reached Gmail"


def test_web_edit_warns_that_the_gmail_copy_is_outdated(client, db, gmail_settings):
    from jobpilot.integrations import gmail

    d = _approved_draft(db, gmail_settings, "Original text (synthetic).")
    gmail.create_gmail_draft(db, gmail_settings, d, gmail.GmailClient(gmail_settings, http=_NumberedDraftsStub()))
    db.commit()
    r = client.post(f"/drafts/{d.id}", data={"to_address": "jane@example.com", "subject": "Hello", "body": "New (synthetic)."},
                    follow_redirects=False)
    assert r.status_code == 303 and "outdated" in r.headers["location"]
    db.expire_all()
    assert db.get(type(d), d.id).gmail_draft_id is None
