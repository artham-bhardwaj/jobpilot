"""Milestone 16: scheduler observability, failure handling, backups (temporary databases; no network)."""
from __future__ import annotations

import json
import os
import zipfile
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from jobpilot.models import Job, SourceRun, TaskRun, utcnow
from jobpilot.services import backup, notifications, scheduler, settings_store
from jobpilot.services import jobs as job_svc
from tests.helpers import make_http, seed_profile


def _enable(db, **tasks):
    settings_store.put(db, "scheduler", {"tasks": {n: {"enabled": True, "every_hours": h} for n, h in tasks.items()}})
    db.commit()


@pytest.fixture
def alive(monkeypatch):
    """Treat the recorded runner pid as a live JobPilot process (tests run under pytest, not `jobpilot serve`)."""
    monkeypatch.setattr(scheduler, "_alive", lambda pid: pid == os.getpid())


# ---- Pass persistence --------------------------------------------------------------------------------------

def test_empty_pass_is_recorded_without_inventing_anything(app, settings, db):
    rep = scheduler.status_report(db, settings)
    assert rep["last_pass"] is None and rep["next_pass"] is None and not rep["live"], "nothing recorded yet"
    assert scheduler.run_due(settings, app.state.sessionmaker) == {}
    db.expire_all()
    lp = scheduler.status_report(db, settings)["last_pass"]
    assert lp["ran"] == {} and lp["runner"] == "cli" and lp["pid"] == os.getpid() and lp["finished_at"] >= lp["started_at"]


def test_runner_state_next_pass_and_expected_task_runs(app, settings, db, alive):
    _enable(db, maintenance=24, notify=0.25)
    t = scheduler.start_background_scheduler(settings, app.state.sessionmaker, initial_delay=3600)
    t.stop_event.set()
    t.join(timeout=5)
    db.expire_all()
    rep = scheduler.status_report(db, settings)
    assert rep["live"] and "running in this JobPilot server" in rep["scheduler"]
    first = rep["runner"]["first_pass_at"]
    assert rep["next_pass"].isoformat(timespec="seconds") == first, "before any pass: the first pass is next"
    scheduler.run_due(settings, app.state.sessionmaker, runner="in-process")
    db.expire_all()
    rep = scheduler.status_report(db, settings)
    finished = scheduler._parse(rep["last_pass"]["finished_at"])
    assert rep["next_pass"] == finished + timedelta(seconds=settings.scheduler_poll_seconds)
    assert set(rep["last_pass"]["ran"]) == {"maintenance", "notify"}
    notify = rep["tasks"]["notify"]
    assert not notify["due"] and notify["last_run"]["trigger"] == "scheduled"
    # next due 15 min after the run; expected at the first 300 s pass at/after that
    assert notify["expected_run"] >= notify["next_due"]
    assert (notify["expected_run"] - rep["next_pass"]).total_seconds() % settings.scheduler_poll_seconds == 0
    assert rep["tasks"]["collect_jobs"]["enabled"] is False and rep["tasks"]["collect_jobs"]["expected_run"] is None
    assert "not at the exact moment it becomes due" in rep["note"]


def test_due_task_is_expected_at_the_next_pass(app, settings, db, alive):
    _enable(db, maintenance=24)
    t = scheduler.start_background_scheduler(settings, app.state.sessionmaker, initial_delay=3600)
    t.stop_event.set()
    t.join(timeout=5)
    db.expire_all()
    rep = scheduler.status_report(db, settings)
    assert rep["tasks"]["maintenance"]["due"] and rep["tasks"]["maintenance"]["expected_run"] == rep["next_pass"]


def test_restart_recovers_from_persisted_state(app, settings, db, monkeypatch):
    """After a restart the old runner is recognised as gone; no next pass is predicted until the new one records."""
    from jobpilot.services.notifications import set_state

    set_state(db, scheduler.RUNNER_KEY, {"mode": "in-process", "pid": 999_999_999, "started_at": "2026-01-01T00:00:00",
                                         "poll_seconds": 300, "first_pass_at": "2026-01-01T00:00:05"})
    set_state(db, scheduler.PASS_KEY, {"started_at": "2026-01-01T00:00:05", "finished_at": "2026-01-01T00:00:06",
                                       "runner": "in-process", "pid": 999_999_999, "ran": {}})
    db.commit()
    settings.scheduler_enabled = True
    rep = scheduler.status_report(db, settings)
    assert not rep["live"] and rep["next_pass"] is None and "is not running" in rep["scheduler"]
    assert rep["last_pass"]["finished_at"] == "2026-01-01T00:00:06", "the last real pass is still shown"


def test_manual_and_scheduled_runs_are_labelled(app, settings, db):
    from jobpilot.services.tasks import run_task

    _enable(db, maintenance=24)
    run_task(settings, app.state.sessionmaker, "maintenance", scheduler.TASKS["maintenance"](settings), trigger="manual")
    db.expire_all()
    assert scheduler.status_report(db, settings)["tasks"]["maintenance"]["last_run"]["trigger"] == "manual"
    assert scheduler.run_due(settings, app.state.sessionmaker) == {}, "a manual success counts: not due again"


# ---- Failures ----------------------------------------------------------------------------------------------

def test_failure_is_visible_backs_off_and_recovers(app, settings, db, monkeypatch):
    _enable(db, maintenance=24, reminders=12)
    calls = {"n": 0}

    def flaky(_settings):
        def run(s):
            calls["n"] += 1
            if calls["n"] <= 2:  # first pass: attempt + one retry both fail
                raise RuntimeError("synthetic failure")
            return {"message": "ok now"}
        return run

    monkeypatch.setitem(scheduler.TASKS, "maintenance", flaky)
    monkeypatch.setattr("jobpilot.services.tasks.time.sleep", lambda s: None)
    res = scheduler.run_due(settings, app.state.sessionmaker)
    assert res == {"maintenance": "failed", "reminders": "success"}, "one failure does not stop the pass"
    db.expire_all()
    rep = scheduler.status_report(db, settings)["tasks"]["maintenance"]
    assert rep["last_success"] is None and rep["last_run"]["status"] == "failed" and rep["retry_after_failure_at"]
    assert rep["next_due"] == rep["retry_after_failure_at"], "a failure never counts as a success"
    assert scheduler.run_due(settings, app.state.sessionmaker) == {}, "backing off: not retried every pass"
    failed = db.scalar(select(TaskRun).where(TaskRun.status == "failed"))
    failed.finished_at = failed.started_at = utcnow() - timedelta(hours=1)
    db.commit()
    assert scheduler.run_due(settings, app.state.sessionmaker) == {"maintenance": "success"}
    db.expire_all()
    assert scheduler.status_report(db, settings)["tasks"]["maintenance"]["last_success"] is not None


def test_scheduler_loop_survives_a_crashing_pass(app, settings, monkeypatch):
    import threading

    passes = threading.Event()
    calls = []

    def boom(*a, **k):
        calls.append(1)
        if len(calls) >= 2:
            passes.set()
        raise RuntimeError("pass crashed")

    monkeypatch.setattr(scheduler, "run_due", boom)
    monkeypatch.setattr(settings, "scheduler_poll_seconds", 0.05, raising=False)
    t = scheduler.start_background_scheduler(settings, app.state.sessionmaker, initial_delay=0.01)
    try:
        assert passes.wait(5), "the thread kept running after an exception"
    finally:
        t.stop_event.set()
        t.join(timeout=5)
    assert not t.is_alive()


def _sources(db, n=2):
    for i in range(n):
        job_svc.save_source(db, job_svc.SourceIn(name=f"Board {i}", kind="greenhouse", config={"board_token": f"ex{i}"}))
    db.commit()


def test_collection_with_every_source_down_is_a_failure_not_an_empty_success(app, settings, db, monkeypatch):
    _sources(db)
    _enable(db, collect_jobs=24)
    monkeypatch.setattr(job_svc, "PoliteHttp", lambda s: make_http(s, {}))  # every request: 404
    monkeypatch.setattr("jobpilot.services.tasks.time.sleep", lambda s: None)
    assert scheduler.run_due(settings, app.state.sessionmaker) == {"collect_jobs": "failed"}
    db.expire_all()
    run = db.scalar(select(TaskRun).where(TaskRun.task_name == "collect_jobs").order_by(TaskRun.id.desc()))
    assert "all 2 source(s) failed" in run.message
    assert scheduler.last_success(db, "collect_jobs") is None
    assert db.scalar(select(func.count(SourceRun.id))) >= 2, "the per-source failure records are kept"


def test_collection_with_one_source_up_succeeds_and_repeats_without_duplicates(app, settings, db, monkeypatch):
    from tests.helpers import fixture_text

    seed_profile(db)
    _sources(db)
    db.commit()
    routes = {"https://boards-api.greenhouse.io/v1/boards/ex0/jobs": (200, fixture_text("greenhouse_jobs.json"))}
    monkeypatch.setattr(job_svc, "PoliteHttp", lambda s: make_http(s, routes))
    collect = scheduler.TASKS["collect_jobs"](settings)
    with app.state.sessionmaker() as s:
        first = collect(s)
        s.commit()
    assert first["failed"] == 1 and first["new"] > 0
    with app.state.sessionmaker() as s:
        n = s.scalar(select(func.count(Job.id)))
        second = collect(s)
        s.commit()
        assert second["new"] == 0 and s.scalar(select(func.count(Job.id))) == n


# ---- Notifications -----------------------------------------------------------------------------------------

def test_notify_with_nothing_to_report_is_a_successful_noop(app, settings, db):
    settings.telegram_enabled = True
    from pydantic import SecretStr

    settings.telegram_bot_token = SecretStr("123456789:AAFakeTokenForTestsOnly_abcdefghijklmnopqrs")
    settings.telegram_chat_id = "4242"
    settings_store.put(db, "notifications", {**settings_store.get(db, "notifications"), "kinds": {"daily_summary": False}})
    notifications.set_state(db, notifications.BASELINE_KEY, {"at": "2026-01-01T00:00:00", "events": 0})
    db.commit()

    class NoNetwork:
        def send_text(self, text):
            raise AssertionError("nothing should be sent")

    res = notifications.run(db, settings, client=NoNetwork())
    assert res["sent"] == 0 and res["created"] == 0 and "nothing to send" in res["message"]


# ---- Backups -----------------------------------------------------------------------------------------------

def test_backups_are_verified_and_retention_keeps_valid_ones(app, settings):
    good = backup.create_backup(settings, app.state.engine, label="one")
    assert backup.verify_backup(good) is None and oct(good.stat().st_mode)[-3:] == "600"
    assert not list(settings.backup_dir.glob("*.part")), "no partial files left behind"
    broken = settings.backup_dir / "jobpilot-29991231T000000000000-broken.zip"  # newest by name, but unreadable
    broken.write_bytes(b"not a zip")
    assert backup.verify_backup(broken)
    assert backup.prune_backups(settings, keep=1) == 0, "the broken newest file doesn't push out the valid one"
    assert good.exists() and broken.exists()
    second = backup.create_backup(settings, app.state.engine, label="two")
    assert backup.prune_backups(settings, keep=1) == 1 and second.exists() and not good.exists()
    for f in list(settings.backup_dir.glob("jobpilot-*.zip")):
        if f != broken:
            f.unlink()
    assert backup.prune_backups(settings, keep=1) == 0, "nothing is deleted when no valid backup exists"


def test_backup_refuses_to_write_an_unverifiable_archive(app, settings, monkeypatch):
    monkeypatch.setattr(backup, "verify_backup", lambda p: "synthetic problem")
    with pytest.raises(backup.BackupError, match="verification failed"):
        backup.create_backup(settings, app.state.engine, label="x")
    assert not list(settings.backup_dir.glob("*-x.zip*"))


# ---- Health, CLI, dashboard ---------------------------------------------------------------------------------

def test_health_flags_an_overdue_scheduler_pass(app, settings, db, alive):
    from jobpilot.services import health
    from jobpilot.services.notifications import set_state

    old = (utcnow() - timedelta(hours=2)).isoformat(timespec="seconds")
    set_state(db, scheduler.RUNNER_KEY, {"mode": "in-process", "pid": os.getpid(), "started_at": old,
                                         "poll_seconds": 300, "first_pass_at": old})
    set_state(db, scheduler.PASS_KEY, {"started_at": old, "finished_at": old, "runner": "in-process",
                                       "pid": os.getpid(), "ran": {}})
    db.commit()
    res = health.check(settings, db)
    assert not res["checks"]["scheduler"]["ok"] and "OVERDUE" in res["checks"]["scheduler"]["detail"]


def test_cli_status_and_system_page(app, client, settings, db, monkeypatch, capsys):
    from jobpilot import cli
    from jobpilot.config import get_settings

    _enable(db, maintenance=24)
    scheduler.run_due(settings, app.state.sessionmaker)
    page = client.get("/system").text
    assert "Last pass" in page and "Next pass" in page and "Expected run" in page and "maintenance (success)" in page
    monkeypatch.setenv("JOBPILOT_DATA_DIR", str(settings.data_dir))
    get_settings.cache_clear()
    try:
        capsys.readouterr()
        assert cli.main(["scheduler", "status"]) == 0
        out = json.loads(capsys.readouterr().out)
    finally:
        get_settings.cache_clear()
    assert out["last_pass"]["ran"] == {"maintenance": "success"}
    assert out["tasks"]["maintenance"]["last_run"].startswith("success (scheduled)")
    assert out["next_pass_expected"].startswith("unknown"), "no live scheduler in this test: none predicted"
    assert "due" in out and "last_success" in out, "existing keys kept for compatibility"


def test_only_serve_starts_background_workers(app):
    """create_app (used by tests and the dashboard) never starts the scheduler or the Telegram poller."""
    import threading

    names = {t.name for t in threading.enumerate()}
    assert "jobpilot-scheduler" not in names and "jobpilot-telegram" not in names
    assert getattr(app.state, "scheduler_thread", None) is None and getattr(app.state, "telegram_thread", None) is None
