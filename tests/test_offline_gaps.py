"""Stage 7: offline tests for previously unexercised paths.

All data is synthetic and all HTTP is faked. Passing these tests shows the code handles the
*recorded/synthetic* shapes; it does NOT prove the live services behave the same way.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
from datetime import timedelta

import pytest
import requests
from sqlalchemy import select

from jobpilot.models import Job, JobSource, ReviewItem, TaskRun, utcnow
from jobpilot.services import jobs as job_svc
from tests.helpers import FIXTURES, fixture_text, make_http, make_pdf_bytes, seed_profile


# ---- Job imports and sources -------------------------------------------------------------

def test_json_job_import(db):
    stats = job_svc.import_file(db, "jobs.json", (FIXTURES / "jobs_import.json").read_bytes())
    assert stats["new"] == 2
    assert any("missing title" in e for e in stats["errors"])
    py = db.scalar(select(Job).where(Job.title == "Python Developer"))
    assert py.external_id == "501" and py.salary_min == 900000 and py.salary_max == 1400000 and py.remote_type == "remote"
    intern = db.scalar(select(Job).where(Job.title == "ML Intern"))
    assert intern.source_url is None, "javascript: URL must be dropped"
    assert intern.salary_min is None and intern.remote_type == "unknown"
    with pytest.raises(ValueError):
        job_svc.import_file(db, "jobs.json", b'"just a string"')
    with pytest.raises(ValueError):
        job_svc.import_file(db, "jobs.xml", b"<jobs/>")


def test_jsonld_multi_url_source_with_partial_and_failing_pages(db, settings):
    urls = ["https://careers.example.net/jobs/sa-77", "https://careers.example.net/jobs/partial",
            "https://careers.example.net/jobs/gone", "http://10.0.0.8/internal-job"]
    src = job_svc.save_source(db, job_svc.SourceIn(name="Sample Analytics pages", kind="jsonld",
                                                   company_name="Sample Analytics", config={"urls": urls}))
    http = make_http(settings, {urls[0]: (200, fixture_text("career_page.html")),
                                urls[1]: (200, fixture_text("career_partial.html"))})
    summary = job_svc.collect_all(db, settings, http, source_ids=[src.id])
    stats = summary["sources"]["Sample Analytics pages"]
    assert stats["new"] == 2
    errs = " ".join(stats["errors"])
    assert "partial" in errs or "No structured JobPosting" in errs
    assert "404" in errs and "private" in errs
    assert src.last_status == "partial"
    partial = db.scalar(select(Job).where(Job.title == "Platform Engineer"))
    assert partial.raw.get("partial") is True
    assert "extracted without structured data" in " ".join(partial.match_details["review_reasons"])
    assert http.session.calls.count("http://10.0.0.8/internal-job") == 0
    with pytest.raises(ValueError):
        job_svc.save_source(db, job_svc.SourceIn(name="bad", kind="jsonld", config={"urls": ["ftp://x"]}))
    with pytest.raises(ValueError):
        job_svc.save_source(db, job_svc.SourceIn(name="bad", kind="jsonld", config={"urls": []}))


def test_lever_single_posting_url(db, settings):
    pid = "bbbbbbbb-1111-2222-3333-555555555555"
    api = f"https://api.lever.co/v0/postings/fictionallabs/{pid}"
    http = make_http(settings, {api: (200, fixture_text("lever_posting_single.json"))})
    job, outcome, warnings = job_svc.add_from_url(db, http, f"https://jobs.lever.co/fictionallabs/{pid}", "Fictional Labs")
    assert outcome == "new" and not warnings
    assert job.title == "Java Developer" and job.remote_type == "onsite" and job.department == "Payments"
    assert job.apply_url.endswith("/apply") and job.sightings[0].method == "api"
    assert http.session.calls == [api]


def test_ashby_adapter_synthetic_fixture(db, settings):
    src = job_svc.save_source(db, job_svc.SourceIn(name="Sample Labs", kind="ashby", company_name="Sample Labs",
                                                   config={"board": "samplelabs"}))
    http = make_http(settings, {"https://api.ashbyhq.com/posting-api/job-board/samplelabs": (200, fixture_text("ashby_board.json"))})
    summary = job_svc.collect_all(db, settings, http)
    assert summary["sources"]["Sample Labs"]["new"] == 2, "unlisted postings are skipped"
    be = db.scalar(select(Job).where(Job.title == "Backend Engineer (Python)"))
    assert be.remote_type == "remote" and be.posted_at is not None and be.category == "python_backend"
    da = db.scalar(select(Job).where(Job.title == "Data Analyst (Hybrid)"))
    assert da.remote_type == "hybrid" and "SQL" in da.description_text
    with pytest.raises(ValueError):
        job_svc.save_source(db, job_svc.SourceIn(name="x", kind="ashby", config={"board": "a/b"}))
    bad = make_http(settings, {"https://api.ashbyhq.com/posting-api/job-board/samplelabs": (200, '{"unexpected": 1}')})
    summary = job_svc.collect_all(db, settings, bad)
    assert summary["sources"]["Sample Labs"]["errors"] and src.last_status == "failed"
    assert db.scalar(select(Job).where(Job.title == "Backend Engineer (Python)")).status == "active", \
        "a malformed response must not expire existing jobs"


@pytest.mark.parametrize("status,expected_job_status,message", [
    (404, "expired", "expired (HTTP 404)"), (410, "expired", "expired (HTTP 410)"),
    (200, "active", "still reachable"), (500, "active", "could not verify"),
])
def test_check_alive(db, settings, status, expected_job_status, message):
    job, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title="Role", source_url="https://careers.example.com/jobs/9"))
    http = make_http(settings, {"https://careers.example.com/jobs/9": (status, "x")})
    assert job_svc.check_alive(db, http, job, initiated_by_user=True).startswith(message)
    assert job.status == expected_job_status


def test_check_alive_without_url(db, settings):
    job, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title="No URL"))
    assert job_svc.check_alive(db, make_http(settings, {}), job, initiated_by_user=True) == "no URL to check"


# ---- Scheduler --------------------------------------------------------------------------

def _enable(db, *names):
    from jobpilot.services import settings_store

    cfg = settings_store.get(db, "scheduler")
    for n in names:
        cfg["tasks"][n]["enabled"] = True
    settings_store.put(db, "scheduler", cfg)
    db.commit()


def test_scheduled_collect_jobs_task(app, settings, db, monkeypatch):
    from jobpilot.services import scheduler

    job_svc.save_source(db, job_svc.SourceIn(name="Examplecorp", kind="greenhouse", company_name="Examplecorp",
                                             config={"board_token": "examplecorp"}))
    _enable(db, "collect_jobs")
    routes = {"https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs": (200, fixture_text("greenhouse_jobs.json"))}
    monkeypatch.setattr(job_svc, "PoliteHttp", lambda s: make_http(s, routes))
    assert scheduler.run_due(settings, app.state.sessionmaker) == {"collect_jobs": "success"}
    db.expire_all()
    assert db.scalar(select(JobSource)).last_status == "success"
    assert len(list(db.scalars(select(Job)))) == 2


def test_scheduled_followup_and_gmail_tasks(app, settings, db):
    from jobpilot.services import contacts as cs
    from jobpilot.services import drafts as ds
    from jobpilot.services import scheduler

    seed_profile(db)
    c, _ = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", email="jane@example.com"))
    d = ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", contact_id=c.id))
    ds.edit_draft(db, d, ds.DraftEdit(to_address=d.to_address, subject="Hello", body="Hi (synthetic)"))
    ds.approve(db, d)
    ds.mark_sent(db, d)
    d.follow_up_due_at = utcnow() - timedelta(hours=1)
    _enable(db, "followup_check", "gmail_sync")
    res = scheduler.run_due(settings, app.state.sessionmaker)
    assert res == {"followup_check": "success", "gmail_sync": "success"}
    assert db.scalar(select(ReviewItem).where(ReviewItem.kind == "follow_up_due"))
    gm = db.scalar(select(TaskRun).where(TaskRun.task_name == "gmail_sync"))
    assert "not configured" in gm.message


def test_background_scheduler_thread_runs_and_stops(app, settings, db):
    from jobpilot.services import scheduler

    _enable(db, "maintenance")
    t = scheduler.start_background_scheduler(settings, app.state.sessionmaker, initial_delay=0.05)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            db.expire_all()
            if db.scalar(select(TaskRun).where(TaskRun.task_name == "maintenance", TaskRun.status == "success")):
                break
            time.sleep(0.1)
        else:
            pytest.fail("background scheduler did not run the due task")
    finally:
        t.stop_event.set()
        t.join(timeout=5)
    assert not t.is_alive()


def test_failing_scheduled_task_is_recorded_and_retried(app, settings, db, monkeypatch):
    from jobpilot.services import scheduler

    _enable(db, "maintenance")
    calls = {"n": 0}

    def boom(settings_):
        def run(s):
            calls["n"] += 1
            raise RuntimeError("synthetic failure")
        return run

    monkeypatch.setitem(scheduler.TASKS, "maintenance", boom)
    monkeypatch.setattr("jobpilot.services.tasks.time.sleep", lambda s: None)
    assert scheduler.run_due(settings, app.state.sessionmaker) == {"maintenance": "failed"}
    assert calls["n"] == 2, "one retry"
    # A failed task stays due, but only after a backoff (15 min after the first failure), not on every pass.
    assert "maintenance" not in scheduler.due_tasks(db), "backing off right after a failure"
    from datetime import timedelta

    from jobpilot.models import TaskRun

    for run in db.scalars(__import__("sqlalchemy").select(TaskRun)):
        run.started_at -= timedelta(minutes=16)
        run.finished_at = run.finished_at and run.finished_at - timedelta(minutes=16)
    db.flush()
    assert "maintenance" in scheduler.due_tasks(db), "failed tasks stay due once the backoff has passed"


# ---- Gmail (mocked Google endpoints) -------------------------------------------------------

class GoogleStub(requests.Session):
    """Scripted Google endpoints. Records every call; never touches the network."""

    def __init__(self, responses: dict[str, list[tuple[int, dict]]]):
        super().__init__()
        self.responses = responses
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        key = next((k for k in self.responses if url.endswith(k) or k in url), None)
        status, body = (self.responses[key].pop(0) if key and self.responses[key] else (404, {"error": "unscripted"}))
        r = requests.Response()
        r.status_code, r._content, r.url = status, json.dumps(body).encode(), url
        return r

    def post(self, url, **kw):
        return self.request("POST", url, **kw)


@pytest.fixture
def gmail_settings(settings):
    settings.gmail_enabled = True
    settings.ensure_dirs()
    settings.gmail_secrets_file.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}))
    return settings


def _token(settings, **extra):
    settings.gmail_token_file.write_text(json.dumps({"access_token": "old", "refresh_token": "rt",
                                                     "expires_at": time.time() + 3600, "email": "me@example.com", **extra}))


def test_gmail_token_refresh_when_expired(gmail_settings):
    from jobpilot.integrations import gmail

    _token(gmail_settings, expires_at=time.time() - 10)
    stub = GoogleStub({"/token": [(200, {"access_token": "new", "expires_in": 3600})],
                       "/profile": [(200, {"emailAddress": "me@example.com"})]})
    assert gmail.GmailClient(gmail_settings, http=stub).profile_email() == "me@example.com"
    tok = json.loads(gmail_settings.gmail_token_file.read_text())
    assert tok["access_token"] == "new" and tok["refresh_token"] == "rt"
    assert stub.calls[0][2]["data"]["grant_type"] == "refresh_token"
    assert stub.calls[1][2]["headers"]["Authorization"] == "Bearer new"


def test_gmail_401_triggers_one_refresh_then_fails_cleanly(gmail_settings):
    from jobpilot.integrations import gmail

    _token(gmail_settings)
    stub = GoogleStub({"/profile": [(401, {}), (200, {"emailAddress": "me@example.com"})],
                       "/token": [(200, {"access_token": "new", "expires_in": 3600})]})
    assert gmail.GmailClient(gmail_settings, http=stub).profile_email() == "me@example.com"
    stub2 = GoogleStub({"/profile": [(401, {}), (401, {})], "/token": [(200, {"access_token": "x", "expires_in": 3600})]})
    with pytest.raises(gmail.GmailError, match="401"):
        gmail.GmailClient(gmail_settings, http=stub2).profile_email()
    stub3 = GoogleStub({"/token": [(400, {"error": "invalid_grant"})]})
    _token(gmail_settings, expires_at=0)
    with pytest.raises(gmail.GmailError, match="refresh failed"):
        gmail.GmailClient(gmail_settings, http=stub3).profile_email()


def test_gmail_revoke_deletes_token(gmail_settings):
    from jobpilot.integrations import gmail

    _token(gmail_settings)
    stub = GoogleStub({"/revoke": [(200, {})]})
    gmail.revoke(gmail_settings, http=stub)
    assert not gmail_settings.gmail_token_file.exists()
    assert stub.calls[0][2]["params"]["token"] == "rt"


def test_gmail_message_body_parsing():
    from jobpilot.integrations.gmail import _payload_text

    enc = lambda s: base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")  # noqa: E731
    multipart = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/plain", "body": {"data": enc("Interview on 21 Oct 2026 at 3:00 PM")}},
        {"mimeType": "text/html", "body": {"data": enc("<p>ignored when plain exists</p>")}}]}
    assert _payload_text(multipart) == "Interview on 21 Oct 2026 at 3:00 PM"
    html_only = {"mimeType": "text/html", "body": {"data": enc("<p>Join <b>meet.google.com/abc</b></p>")}}
    assert "meet.google.com/abc" in _payload_text(html_only)
    nested = {"mimeType": "multipart/mixed", "parts": [{"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/plain", "body": {"data": enc("nested ✓")}}]}, {"mimeType": "application/pdf", "body": {}}]}
    assert _payload_text(nested) == "nested ✓"
    assert _payload_text({}) == ""


def test_gmail_interview_import_requires_opt_in_and_creates_suggestions_only(db, gmail_settings):
    from jobpilot.integrations import gmail
    from jobpilot.models import Interview
    from tests.test_stage5 import _applied

    _token(gmail_settings)
    job, app = _applied(db, gmail_settings)
    with pytest.raises(gmail.GmailError, match="ALLOW_READ_BODIES"):
        gmail.import_interview_emails(db, gmail_settings, gmail.GmailClient(gmail_settings, http=GoogleStub({})))
    gmail_settings.gmail_allow_read_bodies = True
    enc = lambda s: base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")  # noqa: E731
    body = "Examplecorp would like to schedule a technical interview on 21 October 2026, 15:00 IST. https://meet.google.com/x"
    # Ordered: the more specific path must be matched before the "/messages" list endpoint.
    stub = GoogleStub({
        "/messages/m1": [(200, {"payload": {"headers": [{"name": "Subject", "value": "Interview - Examplecorp"},
                                                        {"name": "From", "value": "hr@example.com"}],
                                            "mimeType": "text/plain", "body": {"data": enc(body)}}})],
        "/messages": [(200, {"messages": [{"id": "m1"}]})],
    })
    res = gmail.import_interview_emails(db, gmail_settings, gmail.GmailClient(gmail_settings, http=stub))
    assert res["suggestions"] == 1
    assert db.scalar(select(Interview)) is None
    item = db.scalar(select(ReviewItem).where(ReviewItem.kind == "interview_detected"))
    assert item.data["source_ref"] == "gmail:m1" and item.data["stage"] == "technical"


# ---- Browser assist failure & confirmation ----------------------------------------------------

def _app_for_assist(db, settings):
    from tests.test_stage4 import _setup
    from jobpilot.services import applications as apps

    job, _ = _setup(db, settings)
    return apps.create(db, job)


def test_assist_browser_launch_failure_recorded(db, settings):
    pytest.importorskip("playwright.sync_api")
    from jobpilot.automation.apply import run_assist

    app = _app_for_assist(db, settings)
    settings.browser_enabled, settings.browser_headless = True, True
    settings.browser_channel = "no-such-browser-channel"
    app.apply_url = "https://boards.greenhouse.io/examplecorp/jobs/1"
    run = run_assist(db, settings, app, wait_for_user=False, approved_host="boards.greenhouse.io")
    assert run.status == "failed" and run.finished_at is not None and run.message


def test_assist_supported_path_and_confirmation_capture(db, settings, monkeypatch):
    pytest.importorskip("playwright.sync_api")
    from jobpilot.automation import apply as apply_mod
    from jobpilot.models import ApplicationEvidence

    app = _app_for_assist(db, settings)
    form = ('<form><label for=fn>First Name *</label><input id=fn name=first_name required>'
            '<label for=em>Email *</label><input id=em name=email required></form>'
            '<p>Thank you for applying! We have received your application.</p>')
    from tests.helpers import LocalPage

    page = LocalPage(form)
    app.apply_url = page.url
    settings.browser_enabled, settings.browser_headless = True, True
    monkeypatch.setattr(apply_mod, "site_for", lambda url: "greenhouse")  # treat the synthetic page as supported
    try:
        run = apply_mod.run_assist(db, settings, app, wait_for_user=True, timeout_minutes=0.05, approved_host=page.host)
    finally:
        page.close()
    if run.status == "failed" and "Executable" in (run.message or ""):
        pytest.skip(run.message)
    assert run.status == "closed", (run.status, run.message)
    assert any("First Name" in f for f in run.filled_fields) and any("Email" in f for f in run.filled_fields)
    ev = db.scalar(select(ApplicationEvidence).where(ApplicationEvidence.application_id == app.id))
    assert ev and ev.kind == "confirmation_page" and ev.text.startswith("[auto-captured")
    assert app.status == "preparing" and app.applied_at is None, "capture never marks the application as applied"


# ---- CLI ---------------------------------------------------------------------------------------

def _cli(tmp_path, *args, env_extra=None, stdin=None):
    env = {"JOBPILOT_DATA_DIR": str(tmp_path / "d"), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path), **(env_extra or {})}
    return subprocess.run([sys.executable, "-m", "jobpilot", *args], capture_output=True, text=True, env=env,
                          timeout=180, cwd=tmp_path, input=stdin)


def test_cli_remaining_commands(tmp_path):
    ok = lambda r: (r.returncode == 0, r.stdout[-300:], r.stderr[-500:])  # noqa: E731
    assert _cli(tmp_path, "init").returncode == 0
    r = _cli(tmp_path, "import-jobs", str(FIXTURES / "jobs_import.json"))
    assert ok(r)[0] and json.loads(r.stdout)["new"] == 2, ok(r)
    r = _cli(tmp_path, "collect")
    assert ok(r)[0] and json.loads(r.stdout)["status"] == "success", ok(r)
    r = _cli(tmp_path, "rematch")
    assert ok(r)[0] and "Re-scored 2" in r.stdout, ok(r)
    r = _cli(tmp_path, "db", "current")
    assert ok(r)[0] and json.loads(r.stdout)["current"] == json.loads(r.stdout)["head"]
    assert _cli(tmp_path, "db", "upgrade").returncode == 0
    r = _cli(tmp_path, "add-url", "https://user:pw@careers.example.com/x")
    assert r.returncode == 1 and "credentials" in r.stderr and "Traceback" not in r.stderr
    r = _cli(tmp_path, "assist", "999", "--approve-host", "boards.greenhouse.io")
    assert r.returncode == 1 and "not found" in r.stderr.lower()
    r = _cli(tmp_path, "scheduler", "run", "--task", "maintenance")
    assert ok(r)[0] and json.loads(r.stdout)["status"] == "success"
    r = _cli(tmp_path, "scheduler", "run", "--task", "nope")
    assert r.returncode == 2
    r = _cli(tmp_path, "scheduler", "run-due")
    assert ok(r)[0] and json.loads(r.stdout) == {}
    r = _cli(tmp_path, "gmail", "sync")
    assert r.returncode == 1 and "disabled" in r.stderr.lower()
    assert _cli(tmp_path, "gmail", "revoke").returncode == 0
    out = tmp_path / "all.json"
    r = _cli(tmp_path, "export", "all-json", "--out", str(out))
    assert ok(r)[0] and json.loads(out.read_text())["tables"]["jobs"] and oct(out.stat().st_mode)[-3:] == "600"
    r = _cli(tmp_path, "backup", "create", "--label", "cli")
    name = r.stdout.strip().rsplit("/", 1)[-1]
    r = _cli(tmp_path, "backup", "list")
    assert name in r.stdout
    r = _cli(tmp_path, "backup", "restore", "--name", name, stdin="no\n")
    assert r.returncode == 1 and "Aborted" in r.stdout
    r = _cli(tmp_path, "backup", "restore", "--name", name, "--yes")
    assert ok(r)[0] and json.loads(r.stdout)["restored"] == name
    r = _cli(tmp_path, "backup", "restore", "--name", "jobpilot-nope.zip", "--yes")
    assert r.returncode == 1 and "not found" in r.stderr
    for scope in ("profile", "contacts", "everything"):
        r = _cli(tmp_path, "delete-data", scope, "--yes")
        assert ok(r)[0], ok(r)
    r = _cli(tmp_path, "delete-data", "profile", stdin="no\n")
    assert r.returncode == 1
    r = _cli(tmp_path, "export", "jobs")
    assert r.stdout.strip().splitlines() == [r.stdout.strip().splitlines()[0]], "everything deleted -> header only"
