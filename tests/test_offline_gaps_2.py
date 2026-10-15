"""Stage 7: remaining unexercised paths found by the coverage review. Offline; synthetic data."""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from jobpilot.models import Contact, EmailDraft, Job, JobSource, ResumeVersion, TaskRun
from jobpilot.services import job_parser
from tests.helpers import fixture_text, make_http, make_pdf_bytes, seed_profile
from tests.test_offline_gaps import GoogleStub, _token


@pytest.mark.parametrize("text,expected", [
    ("Salary: $120k - $150k per year", (120000, 150000, "USD")),
    ("CTC ₹12 - 18 LPA", (1200000, 1800000, "INR")),
    ("Pay range 12 to 18 LPA", (1200000, 1800000, "INR")),
    ("€55,000 – €70,000", (55000, 70000, "EUR")),
    ("Competitive salary", (None, None, None)),
])
def test_salary_parsing(text, expected):
    assert job_parser.parse_salary(text) == expected


def test_contact_dnc_toggle_and_draft_cancel_via_web(client, db):
    from jobpilot.services import contacts as cs
    from jobpilot.services import drafts as ds

    seed_profile(db)
    c, _ = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", email="jane@example.com"))
    d = ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", contact_id=c.id))
    db.commit()
    assert "msg=" in client.post(f"/drafts/{d.id}/cancel").headers["location"]
    db.expire_all()
    assert db.get(EmailDraft, d.id).status == "cancelled"
    client.post(f"/contacts/{c.id}/dnc", data={"flag": "1", "reason": "asked not to be contacted"})
    db.expire_all()
    assert db.get(Contact, c.id).do_not_contact and cs.is_blocked(db, "jane@example.com")
    r = client.post("/drafts", data={"template_key": "recruiter_outreach", "contact_id": c.id})
    assert "err=" in r.headers["location"], "drafting to a do-not-contact person is refused"
    client.post(f"/contacts/{c.id}/dnc", data={"flag": "0"})
    db.expire_all()
    assert not db.get(Contact, c.id).do_not_contact and not cs.is_blocked(db, "jane@example.com")


def test_archive_resume_version_and_settings_reset_via_web(client, db, settings):
    from jobpilot.models import Resume
    from jobpilot.services import resumes as rs
    from jobpilot.services import settings_store

    rs.create_standard_slots(db)
    r = db.scalar(select(Resume).where(Resume.category == "java_backend"))
    v = rs.import_version(db, settings, r, "j.pdf", make_pdf_bytes(["Test Candidate"]))
    db.commit()
    assert "msg=" in client.post(f"/resumes/versions/{v.id}/archive").headers["location"]
    assert "err=" in client.post(f"/resumes/versions/{v.id}/frobnicate").headers["location"]
    db.expire_all()
    assert db.get(ResumeVersion, v.id).status == "archived"
    client.post("/settings/matching", data={"value": json.dumps({"relevance_threshold": 70, "review_band_low": 30,
                                                                 "max_missing_mandatory_ratio": 0.4})})
    client.post("/settings/matching", data={"reset": "1"})
    db.expire_all()
    assert settings_store.get(db, "matching")["relevance_threshold"] == 55


def test_run_sources_from_web_in_background(client, db, monkeypatch):
    from jobpilot.services import jobs as job_svc

    job_svc.save_source(db, job_svc.SourceIn(name="Examplecorp", kind="greenhouse", company_name="Examplecorp",
                                             config={"board_token": "examplecorp"}))
    db.commit()
    routes = {"https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs": (200, fixture_text("greenhouse_jobs.json"))}
    monkeypatch.setattr(job_svc, "PoliteHttp", lambda s: make_http(s, routes))
    r = client.post("/sources/run", data={})
    assert r.status_code == 303 and "background" in r.headers["location"]
    db.expire_all()  # TestClient runs background tasks before returning
    run = db.scalar(select(TaskRun).where(TaskRun.task_name == "collect_jobs"))
    assert run.status == "success" and run.trigger == "manual"
    assert db.scalar(select(JobSource)).last_status == "success" and len(list(db.scalars(select(Job)))) == 2


def test_unexpected_error_shows_safe_500_page_and_is_logged(app, settings, monkeypatch):
    from jobpilot.services import overview

    monkeypatch.setattr(overview, "stats", lambda db: (_ for _ in ()).throw(RuntimeError("synthetic bug")))
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/")
    assert r.status_code == 500 and "unexpected error" in r.text and "synthetic bug" not in r.text
    assert "Traceback" not in r.text
    log = (settings.log_dir / "jobpilot.log").read_text()
    assert "unhandled error" in log and "synthetic bug" in log


def test_gmail_web_actions(client, settings, monkeypatch):
    from jobpilot.integrations import gmail

    r = client.post("/gmail/sync")
    assert "err=" in r.headers["location"], "not configured -> clear error"
    settings.gmail_enabled = True
    settings.gmail_secrets_file.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}))
    _token(settings)
    monkeypatch.setattr(gmail.requests, "Session", lambda: GoogleStub({}))
    r = client.post("/gmail/sync")
    assert "msg=" in r.headers["location"] and "0+repl" in r.headers["location"].replace("%20", "+")
    assert client.get("/gmail").status_code == 200
    monkeypatch.setattr(gmail.requests, "Session", lambda: GoogleStub({"/revoke": [(200, {})]}))
    assert "msg=" in client.post("/gmail/disconnect").headers["location"]
    assert not settings.gmail_token_file.exists()
    assert "err=" in client.post("/gmail/bogus").headers["location"]


def test_gmail_loopback_authorization_flow(settings, monkeypatch):
    """Simulates the browser: Google 'redirects' to the local 127.0.0.1 listener with code+state."""
    from jobpilot.integrations import gmail

    settings.gmail_enabled = True
    settings.ensure_dirs()
    settings.gmail_secrets_file.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}))
    stub = GoogleStub({"/token": [(200, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600,
                                         "scope": "gmail.compose gmail.metadata"})],
                       "/profile": [(200, {"emailAddress": "test.candidate@example.com"})]})
    monkeypatch.setattr(gmail.requests, "Session", lambda: stub)

    def fake_browser(url, tamper=False):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"][0].startswith("http://127.0.0.1:")
        state = "WRONG" if tamper else q["state"][0]
        target = f"{q['redirect_uri'][0]}?code=auth-code&state={state}"
        threading.Thread(target=lambda: (time.sleep(0.2), urllib.request.urlopen(target, timeout=5)), daemon=True).start()
        return True

    monkeypatch.setattr(gmail.webbrowser, "open", fake_browser)
    tok = gmail.authorize(settings, timeout=10)
    assert tok["email"] == "test.candidate@example.com"
    saved = json.loads(settings.gmail_token_file.read_text())
    assert saved["refresh_token"] == "rt" and saved["email"] == "test.candidate@example.com"
    assert oct(settings.gmail_token_file.stat().st_mode)[-3:] == "600"
    exchange = next(kw for m, u, kw in stub.calls if u.endswith("/token"))
    assert exchange["data"]["code"] == "auth-code" and len(exchange["data"]["code_verifier"]) >= 43

    monkeypatch.setattr(gmail.webbrowser, "open", lambda url: fake_browser(url, tamper=True))
    with pytest.raises(gmail.GmailError, match="State mismatch"):
        gmail.authorize(settings, timeout=10)


def test_gmail_authorize_refuses_when_disabled(settings):
    from jobpilot.integrations import gmail

    with pytest.raises(gmail.GmailError, match="ENABLED"):
        gmail.authorize(settings, timeout=1, open_browser=False)
