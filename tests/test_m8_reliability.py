"""Milestone 8 — Stage J: scheduler shutdown, expiry from stated deadlines, next runs, backup/restore across
schema versions, no sending/submitting from scheduled tasks (offline; synthetic)."""

from __future__ import annotations

import sqlite3
import time
import zipfile

from fastapi.testclient import TestClient
from sqlalchemy import select, text

from jobpilot.models import Job
from jobpilot.services import backup, scheduler, settings_store
from jobpilot.services import jobs as job_svc


def test_scheduler_thread_stops_on_app_shutdown(app, settings):
    t = scheduler.start_background_scheduler(settings, app.state.sessionmaker, initial_delay=3600)
    app.state.scheduler_thread = t
    with TestClient(app):
        assert t.is_alive()
    t.join(timeout=5)
    assert not t.is_alive(), "lifespan shutdown stops the scheduler thread"


def test_expiry_check_uses_explicit_deadlines_only(db):
    past, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title="Role A", description_text="Applications close on 1 January 2020."))
    vague, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title="Role B", description_text="Apply soon, closing in January."))
    future, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title="Role C", description_text="Apply by 31 December 2099."))
    assert job_svc.expire_past_deadline(db) == 1
    assert past.status == "expired" and "stated application deadline 2020-01-01 passed" in \
        db.execute(text("select details from audit_log where action='job.expired'")).scalar()
    assert vague.status == "active" and future.status == "active"


def test_next_runs_and_labels(db):
    assert all(v is None for v in scheduler.next_runs(db).values()), "all disabled by default"
    cfg = settings_store.get(db, "scheduler")
    cfg["tasks"]["backup"]["enabled"] = True
    settings_store.put(db, "scheduler", cfg)
    assert scheduler.next_runs(db)["backup"] is not None
    assert set(scheduler.TASK_LABELS) == set(scheduler.TASKS)
    labels = " ".join(scheduler.TASK_LABELS.values()).lower()
    assert "never sends" in labels and "no fetching" in labels


def test_scheduled_tasks_never_send_or_submit():
    import inspect

    for name, factory in scheduler.TASKS.items():
        src = inspect.getsource(factory)
        for forbidden in ("send_via_gmail", "send_draft", "record_submission", "mark_sent", "approve("):
            assert forbidden not in src, (name, forbidden)


def test_backup_restore_preserves_new_tables_and_upgrades_old_backups(app, settings, db):
    from jobpilot.services import calibration

    from tests.helpers import seed_profile

    seed_profile(db)
    job, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title="Java Backend Engineer", description_text="Requirements: Java"))
    calibration.record_feedback(db, job, "useful")
    db.commit()
    path = backup.create_backup(settings, app.state.engine, label="m8")
    db.execute(text("DELETE FROM match_feedback"))
    db.commit()
    db.close()
    backup.restore_backup(settings, path, app.state.engine)
    con = sqlite3.connect(settings.db_file)
    assert con.execute("select count(*) from match_feedback").fetchone()[0] == 1
    assert con.execute("select title from jobs").fetchone()[0] == "Java Backend Engineer"
    con.close()

    # A genuine schema-0005 backup (built with Alembic) is upgraded on restore, keeping its rows.
    from alembic import command

    from jobpilot.config import Settings
    from jobpilot.db import alembic_config, make_engine

    old_settings = Settings(_env_file=None, data_dir=settings.data_path.parent / "old5")
    old_settings.ensure_dirs()
    eng = make_engine(old_settings)
    cfg = alembic_config(old_settings)
    with eng.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0005")
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO jobs (title, fingerprint, remote_type, status, user_status, first_seen_at, "
                          "last_seen_at, created_at, updated_at) VALUES ('Legacy Role', 'fp', 'unknown', 'active', 'new', "
                          "'2026-01-01', '2026-01-01', '2026-01-01', '2026-01-01')"))
    old_backup = backup.create_backup(old_settings, eng, label="schema5")
    eng.dispose()
    target = settings.backup_dir / old_backup.name
    target.write_bytes(old_backup.read_bytes())
    res = backup.restore_backup(settings, target, app.state.engine)
    assert res["migration"]["from"] == "0005" and res["migration"]["to"] == "0006"
    con = sqlite3.connect(settings.db_file)
    assert con.execute("select title, miss_count, hard_conflict from jobs").fetchall() == [("Legacy Role", 0, 0)]
    assert con.execute("select count(*) from source_runs").fetchone()[0] == 0
    con.close()


def test_system_page_shows_next_runs(client):
    page = client.get("/system").text
    assert "Next run" in page and "asleep" in page and "Job expiry check" in page
