"""Milestone 8 — Stage H: browser assistance needs per-site approval, respects autofill settings, records
unknown questions, and never submits. Headless Chrome against synthetic pages on 127.0.0.1 only."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from jobpilot.models import Application, ApplicationQuestion, AuditLog
from jobpilot.services import applications as apps
from jobpilot.services import settings_store
from tests.helpers import LocalPage
from tests.test_stage4 import _setup

FORM = """<form><label for=fn>First Name *</label><input id=fn name=first_name required>
<label for=q>What interests you about this team? *</label><textarea id=q name=q required></textarea>
<label><input type=checkbox name=consent required> I agree to the privacy policy *</label>
<button type=submit>Submit application</button></form>
<script>document.querySelector('form').addEventListener('submit', e => { e.preventDefault(); document.title = 'SUBMITTED'; });</script>"""


@pytest.fixture
def page_app(db, settings, monkeypatch):
    pytest.importorskip("playwright.sync_api")
    from jobpilot.automation import apply as apply_mod

    job, _ = _setup(db, settings)
    app = apps.create(db, job)
    page = LocalPage(FORM)
    app.apply_url = page.url
    settings.browser_enabled, settings.browser_headless = True, True
    monkeypatch.setattr(apply_mod, "site_for", lambda url: "greenhouse")  # synthetic page treated as a supported form
    yield app, page, apply_mod
    page.close()


def test_supported_form_fills_records_questions_and_never_submits(db, settings, page_app):
    app, page, apply_mod = page_app
    run = apply_mod.run_assist(db, settings, app, wait_for_user=False, approved_host=page.host)
    if run.status == "failed" and "Executable" in (run.message or ""):
        pytest.skip(run.message)
    assert run.status == "paused" and any("First Name" in f for f in run.filled_fields)
    qs = [q.question for q in db.scalars(select(ApplicationQuestion).where(ApplicationQuestion.application_id == app.id))]
    assert any("interests you about this team" in q for q in qs)
    assert any("privacy policy" in q.lower() for q in qs)
    consent_q = next(q for q in db.scalars(select(ApplicationQuestion)) if "privacy" in q.question.lower())
    assert consent_q.sensitive, "consent is a sensitive question you answer yourself"
    assert app.status == "preparing" and app.applied_at is None
    assert db.scalar(select(AuditLog).where(AuditLog.action == "browser.site_approved"))


def test_wrong_or_missing_approval_opens_nothing(db, settings, page_app):
    app, page, apply_mod = page_app
    for host in (None, "", "evil.example", "127.0.0.2"):
        run = apply_mod.run_assist(db, settings, app, wait_for_user=False, approved_host=host)
        assert run.status == "failed" and "approval" in run.message
        assert not run.filled_fields


def test_autofill_disabled_for_site_opens_page_without_filling(db, settings, page_app):
    app, page, apply_mod = page_app
    settings_store.put(db, "browser", {"autofill_sites": []})
    run = apply_mod.run_assist(db, settings, app, wait_for_user=False, approved_host=page.host)
    if run.status == "failed" and "Executable" in (run.message or ""):
        pytest.skip(run.message)
    assert run.status == "unsupported" and not run.filled_fields


def test_web_assist_requires_explicit_host_approval(client, db, settings, monkeypatch):
    import subprocess

    job, _ = _setup(db, settings)
    app = apps.create(db, job)
    db.commit()
    settings.browser_enabled = True
    launched = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: launched.append(args))
    host = "boards.greenhouse.io"
    assert host in client.get(f"/applications/{app.id}").text
    r = client.post(f"/applications/{app.id}/assist", data={})
    assert "err=" in r.headers["location"] and not launched
    r = client.post(f"/applications/{app.id}/assist", data={"approve": "1", "approve_host": "other.example"})
    assert "err=" in r.headers["location"] and not launched
    r = client.post(f"/applications/{app.id}/assist", data={"approve": "1", "approve_host": host})
    assert "msg=" in r.headers["location"]
    assert launched and launched[0][-2:] == ["--approve-host", host] and "shell" not in str(launched)
    db.expire_all()
    assert db.get(Application, app.id).status == "preparing"


def test_cli_assist_requires_approve_host(tmp_path):
    import subprocess
    import sys

    env = {"JOBPILOT_DATA_DIR": str(tmp_path / "d"), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    subprocess.run([sys.executable, "-m", "jobpilot", "init"], env=env, cwd=tmp_path, capture_output=True, timeout=120)
    r = subprocess.run([sys.executable, "-m", "jobpilot", "assist", "1"], env=env, cwd=tmp_path, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 2 and "--approve-host" in r.stderr
