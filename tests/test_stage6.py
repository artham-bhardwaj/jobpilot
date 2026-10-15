"""Stage 6: scheduling, reliability, privacy export/deletion, security hardening, CLI."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from sqlalchemy import select

from jobpilot.models import Job, Skill, TaskRun
from jobpilot.services import jobs as job_svc, privacy, scheduler, settings_store
from jobpilot.services.text_utils import safe_url
from jobpilot.sources.base import JobData
from tests.helpers import make_http, seed_profile


def test_scheduler_runs_only_enabled_due_tasks(app, settings, db):
    factory = app.state.sessionmaker
    assert scheduler.due_tasks(db) == [], "all scheduled tasks are off by default"
    cfg = settings_store.get(db, "scheduler")
    cfg["tasks"]["maintenance"]["enabled"] = True
    cfg["tasks"]["backup"]["enabled"] = True
    settings_store.put(db, "scheduler", cfg)
    db.commit()
    assert set(scheduler.due_tasks(db)) == {"maintenance", "backup"}
    res = scheduler.run_due(settings, factory)
    assert res == {"maintenance": "success", "backup": "success"}
    db.expire_all()
    assert scheduler.due_tasks(db) == [], "not due again until the interval passes"
    assert scheduler.run_due(settings, factory) == {}
    assert db.scalar(select(TaskRun).where(TaskRun.task_name == "backup")).trigger == "scheduled"


def test_scheduler_no_overlap(app, settings):
    from jobpilot.services.tasks import task_lock

    with task_lock(settings, "scheduler"):
        assert "skipped" in scheduler.run_due(settings, app.state.sessionmaker)


def test_systemd_units_persistent():
    units = scheduler.systemd_units("/usr/bin/python3", "/home/x/jobpilot", 60)
    assert "Persistent=true" in units["jobpilot-scheduler.timer"]
    assert "run-due" in units["jobpilot-scheduler.service"]


def test_export_and_delete(db, settings):
    seed_profile(db)
    db.commit()
    data = privacy.export_all(db)
    assert data["tables"]["candidate_profile"][0]["full_name"] == "Test Candidate"
    json.dumps(data)  # serialisable
    privacy.delete_data(db, settings, "profile")
    db.commit()
    assert db.scalar(select(Skill)) is None
    (settings.files_dir / "resumes").mkdir(parents=True, exist_ok=True)
    (settings.files_dir / "resumes" / "x.pdf").write_bytes(b"%PDF-")
    privacy.delete_data(db, settings, "everything")
    db.commit()
    assert not any(settings.files_dir.rglob("*.pdf"))
    with pytest.raises(ValueError):
        privacy.delete_data(db, settings, "nope")


def test_web_delete_requires_confirmation(client, db):
    seed_profile(db)
    db.commit()
    r = client.post("/system/delete", data={"scope": "profile", "confirm": "nope"})
    assert "err=" in r.headers["location"]
    r = client.post("/system/delete", data={"scope": "profile", "confirm": "DELETE"})
    assert "msg=" in r.headers["location"]
    assert client.get("/system/export.json").json()["tables"]["skills"] == []


# ---- Security ---------------------------------------------------------------------

def test_untrusted_urls_sanitised_on_ingest(db):
    src = job_svc.get_or_create_source(db, "Import", "import")
    job, _ = job_svc.upsert(db, src, JobData(title="X", source_url="javascript:alert(1)",
                                             apply_url="data:text/html,<script>"), "import")
    assert job.source_url is None and job.apply_url is None
    assert safe_url("https://example.com/a?b=1") == "https://example.com/a?b=1"
    assert safe_url("//evil.example") is None


def test_ssrf_guard_blocks_private_addresses(settings):
    from jobpilot.sources.http import FetchError

    settings.block_private_addresses = True
    http = make_http(settings, {"http://127.0.0.1:8765/": (200, "secret")})
    with pytest.raises(FetchError, match="private"):
        http.get("http://127.0.0.1:8765/")
    with pytest.raises(FetchError):
        http.get("http://10.0.0.5/jobs")
    with pytest.raises(FetchError):
        http.get("file:///etc/passwd")


def test_security_headers_and_escaping(client, db):
    r = client.get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["referrer-policy"] == "same-origin"
    job, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title="<script>alert(1)</script>", company_name="<b>x</b>"))
    db.commit()
    page = client.get(f"/jobs/{job.id}").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_http_retries_are_bounded(settings):
    from jobpilot.sources.http import FetchError

    http = make_http(settings, {"https://example.org/x": (503, "busy", {"Retry-After": "0"})})
    with pytest.raises(FetchError):
        http.get("https://example.org/x")
    assert http.session.calls.count("https://example.org/x") == settings.http_max_retries + 1


def test_secrets_not_tracked_by_git():
    from pathlib import Path

    gi = (Path(__file__).parents[1] / ".gitignore").read_text()
    for pattern in (".env", "data/", "*.db", "token*.json", "client_secret*.json", "browser_profile/"):
        assert pattern in gi


# ---- CLI ---------------------------------------------------------------------------

def _cli(tmp_path, *args):
    env = {"JOBPILOT_DATA_DIR": str(tmp_path / "cli-data"), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    return subprocess.run([sys.executable, "-m", "jobpilot.cli", *args], capture_output=True, text=True, env=env,
                          timeout=120, cwd=tmp_path)


def test_cli_end_to_end(tmp_path):
    from tests.helpers import FIXTURES

    assert _cli(tmp_path, "init").returncode == 0
    r = _cli(tmp_path, "import-jobs", str(FIXTURES / "jobs_import.csv"))
    assert r.returncode == 0 and json.loads(r.stdout)["new"] == 2
    r = _cli(tmp_path, "export", "jobs")
    assert "Java Developer" in r.stdout
    r = _cli(tmp_path, "backup", "create", "--label", "cli")
    assert r.returncode == 0 and "cli" in r.stdout
    r = _cli(tmp_path, "scheduler", "status")
    assert r.returncode == 0 and "due" in json.loads(r.stdout)
    r = _cli(tmp_path, "scheduler", "systemd")
    assert "Persistent=true" in r.stdout
    r = _cli(tmp_path, "gmail", "status")
    assert json.loads(r.stdout)["enabled"] is False
    r = _cli(tmp_path, "delete-data", "outreach", "--yes")
    assert r.returncode == 0


def test_every_page_renders_on_empty_database(client):
    import re

    from jobpilot.web.deps import TEMPLATES_DIR

    nav = re.findall(r'\("[^"]+", "(/[^"]*)", "[^"]+"\)', (TEMPLATES_DIR / "base.html").read_text())
    assert len(nav) >= 14
    for url in nav + ["/jobs/new", "/drafts/templates", "/gmail", "/health", "/system/export.json"]:
        r = client.get(url)
        assert r.status_code == 200, url
        assert "Traceback" not in r.text
