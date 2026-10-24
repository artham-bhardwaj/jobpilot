"""Milestone 26: application lifecycle safety (synthetic data, local pages on 127.0.0.1, fake runners)."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from jobpilot.models import Application, ApplicationEvent, BrowserRun, utcnow
from jobpilot.services import applications as apps
from tests.helpers import LocalPage
from tests.apply_fixtures import chrome, job, world  # noqa: F401


# ---- radio groups: exact approved answer only ---------------------------------------------------------------

def _radio(name, question, options, required=True):
    opts = "".join(f"<label><input type=radio name={name} value='{o}' {'required' if required else ''}> {o}</label>"
                   for o in options)
    return f"<fieldset><legend>{question}{'*' if required else ''}</legend>{opts}</fieldset>"


RADIO_FORM = ("<html><body><form><label for=fn>First Name*</label><input id=fn name=first_name required>"
              + _radio("india", "Are you located in India?", ["Yes", "No"])
              + _radio("level", "Which level fits you?", ["Yes, junior", "Yes, junior "])  # two options equal "yes, junior"
              + _radio("visa", "Will you require visa sponsorship?", ["Yes", "No"])
              + _radio("prev", "Have you worked here before?", ["Yes", "No"])
              + _radio("opt", "Preferred pronouns", ["She", "He", "They"], required=False)
              + "<label><input type=checkbox name=agree required> I agree to the privacy policy</label>"
              + "<button type=submit>Submit application</button></form></body></html>")


@pytest.fixture
def page_on():
    sync = pytest.importorskip("playwright.sync_api")
    from jobpilot.config import get_settings

    sites = []
    with sync.sync_playwright() as pw:
        b = pw.chromium.launch(channel=get_settings().browser_channel or None, headless=True)

        def open_(html):
            site = LocalPage(html)
            sites.append(site)
            p = b.new_page()
            p.goto(site.url, wait_until="domcontentloaded")
            return p, site

        yield open_
        b.close()
    for s in sites:
        s.close()


ANSWERS = {"Are you located in India?*": "Yes", "Which level fits you?*": "Yes, junior",
           "Have you worked here before?*": "Maybe"}


def _answer_for(label, sensitive):
    return None if sensitive else ANSWERS.get(label)


def test_radio_groups_get_only_an_exact_unambiguous_approved_answer(page_on):
    from jobpilot.automation.apply import assist_page

    page, site = page_on(RADIO_FORM)
    res = assist_page(page, {"first_name": "Test"}, None, answer_for=_answer_for)
    assert "Are you located in India?* ← approved answer (Yes)" in res.filled
    assert page.is_checked("input[name=india][value=Yes]") and not page.is_checked("input[name=india][value=No]")
    assert not page.is_checked("input[name=level][value='Yes, junior']"), "two options match: nothing is chosen"
    assert "Which level fits you?* (no option equals your approved answer)" not in res.filled
    assert any(u.startswith("Which level fits you?*") for u in res.unknown)
    assert any(u.startswith("Have you worked here before?*") for u in res.unknown), "'Maybe' is not an option"
    assert "Will you require visa sponsorship?*" in res.sensitive and not page.is_checked("input[name=visa]")
    assert not page.is_checked("input[name=opt][value=They]"), "optional personal questions are left empty"
    assert not page.is_checked("input[name=agree]"), "checkboxes (consent) are never ticked"
    assert not any(r.startswith("POST") for r in site.requests)


def test_sensitive_radio_uses_only_this_applications_own_approved_answer(page_on):
    from jobpilot.automation.apply import assist_page

    page, _ = page_on(RADIO_FORM)
    own = {"Will you require visa sponsorship?*": "No"}  # you answered it for this application
    res = assist_page(page, {}, None, answer_for=lambda label, sensitive: own.get(label) or _answer_for(label, sensitive))
    assert page.is_checked("input[name=visa][value=No]") and "Will you require visa sponsorship?*" not in res.sensitive


def test_plan_page_reports_radio_groups_without_choosing(page_on):
    from jobpilot.automation.apply import plan_page

    page, _ = page_on(RADIO_FORM)
    plan = plan_page(page, {"first_name": "Test"}, resume_available=True, answer_for=_answer_for)
    by = {f["label"]: f["plan"] for f in plan["fields"] if f["type"] == "radio"}
    assert by["Are you located in India?*"] == "approved answer (Yes)"
    assert by["Will you require visa sponsorship?*"].startswith("you answer")
    assert by["Preferred pronouns"] == "left empty (optional)"
    assert not page.is_checked("input[name=india][value=Yes]"), "the plan chooses nothing"


# ---- the exact approved resume, or nothing --------------------------------------------------------------------

def test_tampered_resume_file_is_never_uploaded(world, db):  # noqa: F811
    from jobpilot.services import resumes as rs
    from jobpilot.services import tailored

    j = job(db, "https://job-boards.greenhouse.io/x/jobs/1")
    app_ = apps.create(db, j)
    path = rs.verified_file(world, app_.resume_version)
    path.write_bytes(path.read_bytes() + b"\n% changed after approval")
    with pytest.raises(rs.ResumeFileError, match="checksum"):
        rs.verified_file(world, app_.resume_version)
    with pytest.raises(rs.ResumeFileError, match="checksum"):
        tailored.attachment_for(db, world, app_)  # what the browser helper would upload


def test_assisted_mode_does_not_attach_a_changed_resume(world, db, chrome):  # noqa: F811
    from jobpilot.automation.apply import run_assist
    from jobpilot.services import resumes as rs
    from tests.test_m24_assisted import GH_FORM

    world.browser_headless = True
    site = LocalPage(GH_FORM)
    try:
        j = job(db, site.url, title="Software Engineer")
        app_ = apps.create(db, j)
        db.commit()
        p = rs.verified_file(world, app_.resume_version)
        p.write_bytes(b"%PDF-1.4 not the approved file")
        run = run_assist(db, world, app_, wait_for_user=False, approved_host="127.0.0.1")
        assert not any("resume file" in f for f in run.filled_fields)
        assert "Resume NOT attached" in run.message and "checksum" in run.message
    finally:
        site.close()


# ---- transitions, retries, duplicates, persistence -----------------------------------------------------------

def test_transitions_are_validated_centrally(world, db):  # noqa: F811
    j = job(db, "https://job-boards.greenhouse.io/x/jobs/1")
    app_ = apps.create(db, j)
    for bad in ("applied", "interview", "offer", "rejected"):
        with pytest.raises(apps.ApplicationError):
            apps.transition(db, app_, bad)
    with pytest.raises(apps.ApplicationError, match="checklist"):
        apps.transition(db, app_, "ready_for_review")
    with pytest.raises(apps.ApplicationError, match="already exists"):
        apps.create(db, j)
    apps.transition(db, app_, "withdrawn")
    assert app_.closed_at and app_.submission_approved_at is None
    assert [e.to_status for e in db.scalars(select(ApplicationEvent).where(ApplicationEvent.application_id == app_.id)
                                            .order_by(ApplicationEvent.id))] == ["preparing", "withdrawn"]


def test_application_state_survives_a_restart(world, db, settings):  # noqa: F811
    from jobpilot.db import make_engine, make_sessionmaker

    app_ = apps.create(db, job(db, "https://job-boards.greenhouse.io/x/jobs/1"))
    db.add(BrowserRun(application_id=app_.id, status="paused", finished_at=utcnow(), message="CAPTCHA"))
    db.commit()
    db.close()
    engine = make_engine(settings)  # a new process: fresh engine and session
    with make_sessionmaker(engine)() as s:
        a = s.get(Application, app_.id)
        assert a.status == "preparing" and a.applied_at is None
        assert [r.status for r in a.browser_runs] == ["paused"]
    engine.dispose()


def test_unsupported_platform_opens_the_page_and_says_what_to_do(world, db, chrome, monkeypatch):  # noqa: F811
    from jobpilot.automation import apply as browser

    world.browser_headless = True
    site = LocalPage("<html><body><h1>Workday sign in</h1><input type=password></body></html>")
    try:
        j = job(db, site.url, title="Software Engineer")
        app_ = apps.create(db, j)
        db.commit()
        monkeypatch.setattr(browser, "site_for", lambda url: None)  # not a supported hosted form
        run = browser.run_assist(db, world, app_, wait_for_user=False, approved_host="127.0.0.1")
        assert run.status == "unsupported" and site.url in run.message and "record the submission" in run.message
        assert run.filled_fields in (None, []) and any("login" in r for r in run.pause_reasons)
        assert not any(r.startswith("POST") for r in site.requests)
    finally:
        site.close()
