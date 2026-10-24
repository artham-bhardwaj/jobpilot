"""Milestone 24: assisted application workflow (synthetic pages on 127.0.0.1, fake Telegram, fake Gmail).

No employer, Telegram chat or mailbox is contacted. Data is synthetic.
"""
from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import func, select

from jobpilot.models import ApplicationEvidence, ApplicationQuestion, ReviewItem
from jobpilot.services import applications as apps
from tests.helpers import LocalPage
from tests.apply_fixtures import chrome, job, tg, world  # noqa: F401
from tests.test_m22_forms import COMBO_JS, combo


# ---- challenges stop automation (real browser, synthetic pages) ------------------------------------------------

LOGIN = ("<html><body><h1>Sign in to apply</h1><form><label for=u>Email</label><input id=u name=email>"
         "<label for=p>Password</label><input id=p name=password type=password><button type=submit>Submit</button>"
         "</form></body></html>")
MFA = ("<html><body><p>Enter the verification code we sent to your phone.</p><form><label for=c>Code</label>"
       "<input id=c name=otp_code autocomplete=one-time-code><button type=submit>Submit</button></form></body></html>")
INVISIBLE = ("<html><head><script async src='https://www.recaptcha.net/recaptcha/enterprise.js?render=6LfTEST'>"
             "</script></head><body><form><label for=fn>First Name*</label><input id=fn name=first_name required>"
             "<button type=submit>Submit application</button></form></body></html>")
VISIBLE = INVISIBLE.replace("enterprise.js?render=6LfTEST", "api.js").replace(
    "<form>", "<div class='g-recaptcha' data-sitekey='x'></div><form>")


@pytest.mark.parametrize("html,reason", [(LOGIN, "login required"), (MFA, "verification code"),
                                         (VISIBLE, "CAPTCHA"), (INVISIBLE, "invisible")])
def test_challenges_are_detected_and_nothing_is_clicked(html, reason, chrome):  # noqa: F811
    sync = pytest.importorskip("playwright.sync_api")
    from jobpilot.automation.apply import detect_blockers
    from jobpilot.config import get_settings

    site = LocalPage(html)
    try:
        with sync.sync_playwright() as pw:
            b = pw.chromium.launch(channel=get_settings().browser_channel or None, headless=True)
            try:
                p = b.new_page()
                p.goto(site.url, wait_until="domcontentloaded")
                reasons = detect_blockers(p)
            finally:
                b.close()
        assert any(reason.lower() in r.lower() for r in reasons), reasons
        assert not any(r.startswith("POST") for r in site.requests), "nothing was submitted"
    finally:
        site.close()


# ---- outcomes ------------------------------------------------------------------------------------------------

def test_confirmation_needs_wording_and_no_form_errors(chrome):  # noqa: F811
    sync = pytest.importorskip("playwright.sync_api")
    from jobpilot.automation.apply import classify_outcome
    from jobpilot.config import get_settings

    pages = {"thanks": "<h1>Thank you for applying!</h1>",
             "thanks_with_errors": "<h1>Thank you for applying!</h1><div role=alert>Email is required</div>",
             "nothing": "<h1>Loading…</h1>"}
    with sync.sync_playwright() as pw:
        b = pw.chromium.launch(channel=get_settings().browser_channel or None, headless=True)
        try:
            got = {}
            for name, body in pages.items():
                p = b.new_page()
                p.set_content(f"<html><body>{body}</body></html>")
                got[name] = classify_outcome(p, "http://127.0.0.1/apply", timeout_s=1.0, poll_s=0.2)["outcome"]
        finally:
            b.close()
    assert got == {"thanks": "confirmed", "thanks_with_errors": "failed", "nothing": "uncertain"}


def test_recording_a_submission_requires_evidence_approval_and_attestation(world, db):  # noqa: F811
    j = job(db, "https://job-boards.greenhouse.io/x/jobs/1")
    app_ = apps.create(db, j)
    app_.status = "ready_for_review"
    db.commit()
    with pytest.raises(apps.ApplicationError, match="Approve the submission"):
        apps.record_submission(db, app_, "confirmation_email", "", attest=True)
    apps.approve_submission(db, app_)
    with pytest.raises(apps.ApplicationError, match="confirmation evidence"):
        apps.record_submission(db, app_, "confirmation_email", "", attest=True)
    db.add(ApplicationEvidence(application_id=app_.id, kind="confirmation_email", text="Thank you for applying (synthetic)"))
    db.commit()
    db.refresh(app_)
    with pytest.raises(apps.ApplicationError, match="actually submitted"):
        apps.record_submission(db, app_, "confirmation_email", "", attest=False)
    apps.record_submission(db, app_, "confirmation_email", "", attest=True)
    assert app_.status == "applied" and app_.applied_at is not None


# ---- assisted Greenhouse workflow (run_assist, the path `jobpilot assist` uses) ------------------------------

# Structure of the live Greenhouse job-board form (2026-10-18): plain inputs, resume + cover-letter uploads,
# react-select drop-downs with hidden proxies, a legal question, and one submit button.
GH_FORM = ("<html><body><h1>Software Engineer</h1><form id=application-form>"
           "<label for=first_name>First Name*</label><input id=first_name required>"
           "<label for=last_name>Last Name*</label><input id=last_name required>"
           "<label for=email>Email*</label><input id=email type=text required>"
           "<label for=phone>Phone</label><input id=phone type=tel>"
           "<label for=resume>Resume/CV*</label><input id=resume type=file required>"
           "<label for=cover_letter>Cover Letter</label><input id=cover_letter type=file>"
           "<label for=q1>Why do you want to work here?*</label><textarea id=q1 required></textarea>"
           + combo("q2", "Are you located in India?", ["Yes", "No"])
           + combo("q3", "Will you now or in the future require sponsorship for a visa?", ["Yes", "No"])
           + "<button type=submit>Submit application</button></form>"
           "<script>document.getElementById('application-form').addEventListener('submit', e => {"
           "e.preventDefault(); document.body.innerHTML = '<h1>Thank you for applying!</h1>'; });</script>"
           + COMBO_JS + "</body></html>")


@pytest.fixture
def assisted(world, db, monkeypatch):  # noqa: F811
    world.browser_headless = True
    site = LocalPage(GH_FORM)
    j = job(db, site.url, title="Software Engineer", company="Rubrik (synthetic)")
    app_ = apps.create(db, j)
    db.add(ApplicationQuestion(application_id=app_.id, question="Are you located in India?", answer="Yes",
                               status="approved", source="manual", sensitive=False, answer_source="manual"))
    db.commit()
    yield world, site, app_
    site.close()


def test_assisted_greenhouse_fills_uploads_and_hands_over_before_submit(assisted, db, chrome):  # noqa: F811
    from jobpilot.automation.apply import run_assist

    settings, site, app_ = assisted
    refused = run_assist(db, settings, app_, wait_for_user=False, approved_host="example.org")
    assert refused.status == "failed" and "Not approved" in refused.message, "each site is approved explicitly"
    run = run_assist(db, settings, app_, wait_for_user=False, approved_host="127.0.0.1")
    filled = "\n".join(run.filled_fields)
    for expected in ("First Name* ← first_name", "Last Name* ← last_name", "Email* ← email", "Resume/CV* ← resume file",
                     "Why do you want to work here?* ← approved answer", "Are you located in India?* ← approved answer"):
        assert expected in filled, expected
    assert "Cover Letter" not in filled, "only the approved resume is ever attached"
    assert any("sponsorship" in u for u in run.unknown_fields), "the legal question is left for you"
    assert run.status == "paused" and "submit yourself" in run.message.lower()
    assert run.screenshot_path, "a screenshot of the filled form is kept"
    assert not any(r.startswith("POST") for r in site.requests), "JobPilot never clicks submit"
    db.refresh(app_)
    assert app_.status == "preparing" and app_.applied_at is None
    assert db.scalar(select(ReviewItem.kind).where(ReviewItem.entity_id == app_.id)) == "browser_paused"
    assert db.scalar(select(ApplicationQuestion.status).where(
        ApplicationQuestion.application_id == app_.id, ApplicationQuestion.question.like("%sponsorship%"))) is not None


def test_assisted_confirmation_is_captured_but_not_claimed(assisted, db, chrome, monkeypatch):  # noqa: F811
    """While the window is open, you submit; JobPilot captures the confirmation page as evidence, and the application
    becomes 'applied' only when you record it."""
    from jobpilot.automation import apply as browser

    settings, site, app_ = assisted
    real_assist = browser.assist_page

    def assist_then_user_submits(page, *a, **kw):
        res = real_assist(page, *a, **kw)
        # you finish the form and click Submit; the employer's page then shows its confirmation
        page.evaluate("setTimeout(() => { document.body.innerHTML = '<h1>Thank you for applying!</h1>'; }, 500)")
        return res

    monkeypatch.setattr(browser, "assist_page", assist_then_user_submits)
    started = time.monotonic()
    run = browser.run_assist(db, settings, app_, wait_for_user=True, timeout_minutes=0.08, approved_host="127.0.0.1")
    assert time.monotonic() - started < 30
    ev = db.scalar(select(ApplicationEvidence).where(ApplicationEvidence.application_id == app_.id))
    assert ev.kind == "confirmation_page" and "Thank you for applying" in ev.text and "[auto-captured" in ev.text
    assert "confirm the submission in JobPilot" in run.message
    db.refresh(app_)
    assert app_.applied_at is None and app_.status == "preparing", "a captured page is not a recorded submission"


# ---- Gmail: draft with the resume attached, never sent --------------------------------------------------------

def test_test_draft_with_resume_attachment_is_created_and_never_sent(world, db):  # noqa: F811
    from jobpilot.cli_ops import _make_test_draft
    from jobpilot.integrations import gmail
    from jobpilot.models import ResumeVersion
    from tests.test_m13_gmail import FakeGmail, _mime

    world.gmail_enabled = True
    world.ensure_dirs()
    world.gmail_secrets_file.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}))
    world.gmail_token_file.write_text(json.dumps({
        "access_token": "at", "refresh_token": "rt", "expires_at": time.time() + 3600, "email": "test.candidate@example.com",
        "scope": "https://www.googleapis.com/auth/gmail.compose https://www.googleapis.com/auth/gmail.metadata"}))
    fake = FakeGmail()
    rv = db.scalar(select(ResumeVersion).where(ResumeVersion.status == "approved"))
    d = _make_test_draft(db, "jobpilot-test@example.com", rv.id)
    res = gmail.create_gmail_draft(db, world, d, gmail.GmailClient(world, http=fake))
    m = _mime(fake)
    (att,) = list(m.iter_attachments())
    assert att.get_content() == (world.files_dir / rv.file_path).read_bytes(), "the approved resume's exact bytes"
    assert att.get_filename() == res["attachment"]["filename"] == rv.original_filename
    assert m["To"] == "jobpilot-test@example.com" and "do not send" in m["Subject"]
    assert "A resume file is attached" in m.get_body(preferencelist=("plain",)).get_content()
    assert d.kind == "gmail_test" and d.gmail_status == "created" and d.sent_at is None
    assert fake.count("POST", "/drafts/send") == 0 and fake.count("POST", "/messages/send") == 0


def test_assisted_fills_around_a_bot_check_and_never_submits(world, db, chrome):  # noqa: F811
    """Found on the live Rubrik form: every Greenhouse board has an invisible reCAPTCHA, and assisted mode used to
    fill nothing. You are at the screen in assisted mode, so the form is filled and the check is left to you."""
    from jobpilot.automation.apply import run_assist

    world.browser_headless = True
    html = GH_FORM.replace("<html><body>", "<html><head><script async src='https://www.recaptcha.net/recaptcha/"
                                           "enterprise.js?render=6LfTEST'></script></head><body>")
    site = LocalPage(html)
    try:
        j = job(db, site.url, title="Software Engineer")
        app_ = apps.create(db, j)
        db.commit()
        run = run_assist(db, world, app_, wait_for_user=False, approved_host="127.0.0.1")
        assert "First Name* ← first_name" in run.filled_fields and "Resume/CV* ← resume file" in run.filled_fields
        assert "check only you can complete" in run.message and "invisible" in run.message
        assert any("invisible" in r for r in run.pause_reasons) and run.status == "paused"
        assert not any(r.startswith("POST") for r in site.requests)
    finally:
        site.close()
