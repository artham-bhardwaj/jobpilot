"""Stage 1: configuration, migrations, logging, security middleware, backups, tasks."""

from __future__ import annotations

import json

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

from jobpilot.config import Settings
from jobpilot.db import current_revision, head_revision, make_engine, migrate
from jobpilot.models import AuditLog, Base
from jobpilot.services import audit, backup, settings_store
from jobpilot.services.tasks import run_task, task_lock, TaskLocked


def test_migrations_match_models(app, settings):
    engine = app.state.engine
    assert current_revision(engine) == head_revision(settings)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == [], f"models and migrations differ: {diff}"


def test_dashboard_and_health(client):
    r = client.get("/")
    assert r.status_code == 200 and "JobPilot" in r.text
    h = client.get("/health")
    assert h.status_code == 200
    body = h.json()
    assert body["checks"]["database"]["ok"] is True
    assert body["checks"]["gmail"]["detail"] == "disabled"


def test_system_and_settings_pages(client):
    assert client.get("/system").status_code == 200
    assert client.get("/settings").status_code == 200


def test_cross_origin_post_blocked(client):
    r = client.post("/system/backup", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/system/backup", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = client.post("/system/backup", headers={"Origin": "http://127.0.0.1:8765"})
    assert r.status_code == 303


def test_untrusted_host_rejected(client):
    r = client.get("/", headers={"Host": "attacker.example"})
    assert r.status_code == 400


def test_remote_bind_refused(tmp_path):
    s = Settings(_env_file=None, data_dir=tmp_path, host="0.0.0.0")
    with pytest.raises(RuntimeError):
        s.check_bind()
    Settings(_env_file=None, data_dir=tmp_path, host="0.0.0.0", allow_remote_bind=True).check_bind()


def test_settings_validation(client, db):
    bad = client.post("/settings/matching", data={"value": json.dumps({"relevance_threshold": 500})})
    assert "err=" in bad.headers["location"]
    good = {"relevance_threshold": 60, "review_band_low": 30, "max_missing_mandatory_ratio": 0.4}
    ok = client.post("/settings/matching", data={"value": json.dumps(good)})
    assert "msg=" in ok.headers["location"]
    db.expire_all()
    assert settings_store.get(db, "matching")["relevance_threshold"] == 60


def test_structured_log_written(app, settings, db):
    audit.record(db, "test.event", "thing", 7, note="hello")
    db.commit()
    log_file = settings.log_dir / "jobpilot.log"
    assert log_file.exists()
    entries = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert any(e["msg"] == "test.event" and e.get("entity_id") == 7 for e in entries)


def test_backup_and_restore_roundtrip(app, settings, db):
    engine = app.state.engine
    audit.record(db, "before.backup")
    db.commit()
    (settings.files_dir / "resumes").mkdir(parents=True, exist_ok=True)
    (settings.files_dir / "resumes" / "r.txt").write_text("synthetic resume")
    path = backup.create_backup(settings, engine, label="test")

    audit.record(db, "after.backup")
    db.commit()
    (settings.files_dir / "resumes" / "r.txt").unlink()
    db.close()

    result = backup.restore_backup(settings, path, engine)
    assert result["pre_restore_backup"]
    fresh = make_engine(settings)
    with fresh.connect() as conn:
        actions = [r[0] for r in conn.execute(text("SELECT action FROM audit_log"))]
    fresh.dispose()
    assert "before.backup" in actions and "after.backup" not in actions
    assert (settings.files_dir / "resumes" / "r.txt").read_text() == "synthetic resume"


def test_restore_rejects_unsafe_zip(settings, tmp_path):
    import zipfile

    settings.ensure_dirs()
    bad = tmp_path / "jobpilot-evil.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("jobpilot.db", b"")
        zf.writestr("manifest.json", "{}")
        zf.writestr("../escape.txt", "x")
    with pytest.raises(backup.BackupError):
        backup.restore_backup(settings, bad)
    with pytest.raises(backup.BackupError):
        backup.resolve_backup(settings, "../../etc/passwd")


def test_migrate_takes_backup_when_data_exists(settings):
    settings.ensure_dirs()
    engine = make_engine(settings)
    from alembic import command

    from jobpilot.db import alembic_config

    cfg = alembic_config(settings)
    first = __import__("alembic.script", fromlist=["ScriptDirectory"]).ScriptDirectory.from_config(cfg)
    base_rev = list(first.walk_revisions())[-1].revision
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, base_rev)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO audit_log (ts, actor, action) VALUES ('2026-01-01', 'user', 'keep.me')"))
    result = migrate(settings, engine)
    if result["from"] != result["to"]:
        assert result["backup"]
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM audit_log WHERE action='keep.me'")).scalar() == 1
    engine.dispose()


def test_task_runner_records_and_locks(app, settings):
    factory = app.state.sessionmaker

    def ok(session):
        return {"message": "done", "n": 1}

    run = run_task(settings, factory, "unit-test", ok)
    assert run.status == "success" and run.message == "done"

    calls = {"n": 0}

    def flaky(session):
        calls["n"] += 1
        if calls["n"] < 2:
            raise ValueError("transient")
        return {}

    run = run_task(settings, factory, "flaky", flaky, retries=2, retry_delay=0)
    assert run.status == "success" and run.attempts == 2

    with task_lock(settings, "unit-test"):
        skipped = run_task(settings, factory, "unit-test", ok)
        assert skipped.status == "skipped"
        with pytest.raises(TaskLocked):
            with task_lock(settings, "unit-test"):
                pass

    def boom(session):
        raise RuntimeError("nope")

    failed = run_task(settings, factory, "boom", boom, retries=1, retry_delay=0)
    assert failed.status == "failed" and "nope" in failed.message
