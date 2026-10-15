"""Stage 4: application lifecycle, duplicate detection, evidence-gated submission,
review queue, browser assistance (synthetic local form), migration data preservation."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from jobpilot.models import Application, ReviewItem
from jobpilot.services import applications as apps
from jobpilot.services import jobs as job_svc
from jobpilot.services import resumes as rs
from jobpilot.services import review
from tests.helpers import make_pdf_bytes, seed_profile


def _setup(db, settings, approve=True):
    seed_profile(db)
    rs.create_standard_slots(db)
    from jobpilot.models import Resume

    java = db.scalar(select(Resume).where(Resume.category == "java_backend"))
    v = rs.import_version(db, settings, java, "java.pdf", make_pdf_bytes(["Test Candidate", "Java Spring Boot SQL"]))
    if approve:
        rs.approve_version(db, v)
    db.flush()
    db.expire_all()
    job, _ = job_svc.add_manual(db, job_svc.ManualJobIn(
        title="Java Backend Engineer", company_name="Examplecorp", location="Bengaluru",
        description_text="Requirements: Java, Spring Boot, SQL. 1+ years of experience.",
        apply_url="https://boards.greenhouse.io/examplecorp/jobs/1001"))
    return job, v


def _ready(db, app):
    for key in ("review_job", "apply_link", "answers"):
        apps.set_check(db, app, key, True)
    apps.transition(db, app, "ready_for_review")


def test_full_lifecycle_requires_genuine_confirmation(db, settings):
    job, v = _setup(db, settings)
    app = apps.create(db, job)
    assert app.status == "preparing" and app.resume_version_id == v.id
    with pytest.raises(apps.ApplicationError):
        apps.transition(db, app, "ready_for_review")  # checklist incomplete
    _ready(db, app)
    assert db.scalar(select(ReviewItem).where(ReviewItem.kind == "submission_approval", ReviewItem.status == "open"))
    with pytest.raises(apps.ApplicationError):
        apps.transition(db, app, "applied")  # never via plain transition
    with pytest.raises(apps.ApplicationError):
        apps.record_submission(db, app, "confirmation_email", "", attest=True)  # not approved
    apps.approve_submission(db, app)
    with pytest.raises(apps.ApplicationError):
        apps.record_submission(db, app, "confirmation_email", "", attest=True)  # no evidence
    apps.add_evidence(db, settings, app, "note", "I think I applied")
    db.refresh(app)
    with pytest.raises(apps.ApplicationError):
        apps.record_submission(db, app, "confirmation_email", "", attest=True)  # note is not confirmation
    apps.add_evidence(db, settings, app, "confirmation_email", "Thank you for applying to Examplecorp (synthetic)")
    db.refresh(app)
    with pytest.raises(apps.ApplicationError):
        apps.record_submission(db, app, "confirmation_email", "", attest=False)
    apps.record_submission(db, app, "confirmation_email", "ref 123", attest=True)
    assert app.status == "applied" and app.applied_at and app.resume_version_id == v.id
    with pytest.raises(apps.ApplicationError):
        apps.select_resume(db, app, v.id)  # frozen
    for to in ("assessment", "interview", "offer"):
        apps.transition(db, app, to)
    assert [e.to_status for e in app.events][-4:] == ["applied", "assessment", "interview", "offer"]


def test_duplicate_detection(db, settings):
    job, _ = _setup(db, settings)
    apps.create(db, job)
    with pytest.raises(apps.ApplicationError, match="already exists"):
        apps.create(db, job)
    same, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title="Java Backend Engineer", company_name="Examplecorp Inc",
                                                         location="Pune", description_text="Java"))
    with pytest.raises(apps.ApplicationError, match="duplicate"):
        apps.create(db, same)
    app2 = apps.create(db, same, override_note="Different team and location (Pune)")
    assert app2.duplicate_override_note


def test_missing_resume_flagged(db, settings):
    job, _ = _setup(db, settings, approve=False)
    app = apps.create(db, job)
    assert app.resume_version_id is None
    item = db.scalar(select(ReviewItem).where(ReviewItem.kind == "resume_missing"))
    assert item and item.status == "open"
    assert not app.checklist["resume"]["done"]


def test_closing_application_suppresses_followups(db, settings):
    from jobpilot.services import contacts as cs
    from jobpilot.services import drafts as ds

    job, _ = _setup(db, settings)
    app = apps.create(db, job)
    c, _ = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", email="jane@example.com", company_name="Examplecorp"))
    d = ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=job.id, contact_id=c.id,
                                           application_id=app.id))
    ds.approve(db, d)
    ds.mark_sent(db, d)
    assert d.follow_up_due_at
    apps.transition(db, app, "withdrawn", "no longer interested")
    assert d.follow_up_due_at is None and "withdrawn" in d.suppressed_reason


def test_review_queue(db, settings):
    seed_profile(db)
    job, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title="Java Software Engineer", company_name="Examplecorp",
                                                        description_text="Requirements: Java, Spring Boot, SQL, REST APIs"))
    q = review.queue(db)
    if job.match_details["needs_review"]:
        assert job in q["jobs"]
        review.mark_job_reviewed(db, job)
        assert job not in review.queue(db)["jobs"]


def test_migration_preserves_drafts_through_table_rebuild(settings):
    """0004 rebuilds email_drafts (batch add column); existing rows must survive."""
    from alembic import command

    from jobpilot.db import alembic_config, make_engine, migrate

    settings.ensure_dirs()
    engine = make_engine(settings)
    cfg = alembic_config(settings)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0003")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO email_drafts (kind, subject, body, status, created_at, updated_at) "
                          "VALUES ('recruiter_outreach', 'keep subject', 'keep body', 'draft', '2026-01-01', '2026-01-01')"))
    result = migrate(settings, engine)
    assert result["backup"], "a pre-migration backup must be taken"
    with engine.connect() as conn:
        row = conn.execute(text("SELECT subject, body, application_id FROM email_drafts")).one()
    assert row == ("keep subject", "keep body", None)
    engine.dispose()


# ---- Browser assistance on a synthetic local form -------------------------------

FORM = """<!doctype html><html><body><form>
<label for="first_name">First Name *</label><input id="first_name" name="first_name" required>
<label for="last_name">Last Name *</label><input id="last_name" name="last_name" required>
<label for="email">Email *</label><input id="email" name="email" type="email" required>
<label for="phone">Phone</label><input id="phone" name="phone">
<label for="resume">Resume/CV *</label><input id="resume" name="resume" type="file" required>
<label for="q1">Why do you want to work here? *</label><textarea id="q1" name="q1" required></textarea>
<label for="gender">Gender</label><select id="gender" name="gender"><option></option><option>x</option></select>
<label><input type="checkbox" name="consent" required> I agree to the privacy policy *</label>
<button type="submit" id="submit">Submit application</button>
</form><script>document.querySelector('form').addEventListener('submit', e => { e.preventDefault(); document.body.dataset.submitted = '1'; });</script></body></html>"""


@pytest.fixture
def chrome_page(settings):
    pw_mod = pytest.importorskip("playwright.sync_api")
    try:
        pw = pw_mod.sync_playwright().start()
        browser = pw.chromium.launch(channel="chrome", headless=True)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Chrome not available: {exc}")
    page = browser.new_page()
    yield page
    browser.close()
    pw.stop()


def test_assist_fills_known_fields_and_never_submits(chrome_page, tmp_path):
    from jobpilot.automation.apply import assist_page

    chrome_page.set_content(FORM)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(make_pdf_bytes(["Test Candidate"]))
    res = assist_page(chrome_page, {"first_name": "Test", "last_name": "Candidate", "email": "test.candidate@example.com"},
                      resume)
    assert chrome_page.input_value("#first_name") == "Test"
    assert chrome_page.input_value("#email") == "test.candidate@example.com"
    assert chrome_page.input_value("#phone") == "", "no confirmed phone given -> left empty"
    assert chrome_page.eval_on_selector("#resume", "e => e.files.length") == 1
    assert not chrome_page.is_checked("input[name=consent]"), "consent must never be ticked"
    assert chrome_page.evaluate("document.body.dataset.submitted") is None, "must never submit"
    assert res.status == "paused"
    assert any("Why do you want" in u for u in res.unknown)
    assert any("privacy" in s.lower() for s in res.sensitive) and any("Gender" in s for s in res.sensitive)


def test_assist_pauses_on_captcha(chrome_page):
    from jobpilot.automation.apply import assist_page

    chrome_page.set_content('<form><label for="e">Email</label><input id="e" name="email"></form>'
                            '<p>Please verify you are human</p>')
    res = assist_page(chrome_page, {"email": "test.candidate@example.com"}, None)
    assert res.status == "paused" and res.pause_reasons
    assert chrome_page.input_value("#e") == "", "nothing is filled once a security check is detected"


def test_site_support_and_classification():
    from jobpilot.automation.apply import classify, site_for

    assert site_for("https://boards.greenhouse.io/examplecorp/jobs/1") == "greenhouse"
    assert site_for("https://jobs.lever.co/example/abc/apply") == "lever"
    assert site_for("https://evil.greenhouse.io.example.com/x") is None
    assert classify({"type": "text", "label": "Are you authorized to work in India?", "name": "q", "id": "",
                     "required": True})[0] == "sensitive"
    assert classify({"type": "text", "label": "LinkedIn Profile", "name": "", "id": "", "required": False}) == ("fill", "linkedin")


# ---- Web ------------------------------------------------------------------------

def test_application_web_flow(client, db, settings):
    job, v = _setup(db, settings)
    db.commit()
    page = client.get(f"/jobs/{job.id}").text
    assert "Start application" in page
    r = client.post(f"/jobs/{job.id}/application", data={})
    assert r.status_code == 303 and "/applications/" in r.headers["location"]
    app = db.scalar(select(Application))
    assert client.get(f"/applications/{app.id}").status_code == 200
    for key in ("review_job", "apply_link", "answers"):
        client.post(f"/applications/{app.id}/check", data={"key": key, "done": "1"})
    client.post(f"/applications/{app.id}/status", data={"to": "ready_for_review"})
    client.post(f"/applications/{app.id}/approve-submission")
    r = client.post(f"/applications/{app.id}/submitted", data={"method": "screenshot", "attest": "1"})
    assert "err=" in r.headers["location"]
    client.post(f"/applications/{app.id}/evidence", data={"kind": "screenshot"},
                files={"file": ("shot.png", b"\x89PNG\r\n\x1a\nfake", "image/png")})
    r = client.post(f"/applications/{app.id}/submitted", data={"method": "screenshot", "attest": "1"})
    assert "msg=" in r.headers["location"]
    db.expire_all()
    app = db.get(Application, app.id)
    assert app.status == "applied"
    ev = app.evidence[0]
    assert client.get(f"/applications/evidence/{ev.id}").status_code == 200
    assert client.get("/applications").status_code == 200
    assert client.get("/review").status_code == 200
    r = client.post(f"/applications/{app.id}/assist")
    assert "disabled" in r.headers["location"].lower()


def test_run_assist_unsupported_site_end_to_end(db, settings):
    """Full launch path (persistent Chrome profile, headless) on a non-supported page: no autofill, review item."""
    pytest.importorskip("playwright.sync_api")
    from jobpilot.automation.apply import run_assist

    job, _ = _setup(db, settings)
    app = apps.create(db, job)
    app.apply_url = "data:text/html,<form><label for=e>Email</label><input id=e name=email></form>"
    settings.browser_enabled, settings.browser_headless = True, True
    run = run_assist(db, settings, app, wait_for_user=False)
    if run.status == "failed" and "Executable" in (run.message or ""):
        pytest.skip(run.message)
    assert run.status == "unsupported", run.message
    assert not run.filled_fields
    assert db.scalar(select(ReviewItem).where(ReviewItem.kind == "browser_paused"))
    assert run.screenshot_path


def test_run_assist_disabled_by_default(db, settings):
    from jobpilot.automation.apply import run_assist

    job, _ = _setup(db, settings)
    app = apps.create(db, job)
    run = run_assist(db, settings, app, wait_for_user=False)
    assert run.status == "failed" and "disabled" in run.message
