"""Optional local scheduling.

``run_due`` runs every enabled task whose interval has elapsed since its last
successful run. It is safe to call often: a global lock prevents overlapping
scheduler passes and each task has its own lock (see ``services.tasks``).

Two ways to run it, both only while your computer is on:
- ``JOBPILOT_SCHEDULER_ENABLED=true`` → background thread inside ``jobpilot serve``.
- systemd user timer calling ``jobpilot scheduler run-due`` (``Persistent=true``
  catches up after the machine was off). See ``jobpilot scheduler systemd``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.config import Settings
from jobpilot.models import TaskRun, utcnow
from jobpilot.services import settings_store
from jobpilot.services.tasks import TaskLocked, run_task, task_lock

log = logging.getLogger(__name__)


def _collect(settings: Settings) -> Callable[[Session], dict]:
    from jobpilot.services import jobs as job_svc

    return lambda s: job_svc.collect_all(s, settings, trigger="scheduled")


def _followups(settings: Settings) -> Callable[[Session], dict]:
    def run(s: Session) -> dict:
        from jobpilot.services import drafts as ds
        from jobpilot.services import review

        due = ds.due_followups(s, apply_suppression=True)
        for d in due:
            review.open_item(s, "follow_up_due", "draft", d.id, f"Follow-up due: {d.subject}",
                             f"sent {d.sent_at:%Y-%m-%d}; no reply recorded")
        return {"message": f"{len(due)} follow-up(s) due", "due": len(due)}
    return run


def _maintenance(settings: Settings) -> Callable[[Session], dict]:
    def run(s: Session) -> dict:
        from jobpilot.services import jobs as job_svc

        expired = job_svc.expire_past_deadline(s)
        return {"message": f"{expired} job(s) past their closing date marked expired", "expired": expired}
    return run


def _backup(settings: Settings) -> Callable[[Session], dict]:
    def run(s: Session) -> dict:
        from jobpilot.services import backup

        path = backup.create_backup(settings, s.get_bind(), label="scheduled")
        removed = backup.prune_backups(settings, keep=settings_store.get(s, "scheduler").get("keep_backups", 14))
        return {"message": f"backup {path.name}; pruned {removed}", "file": path.name}
    return run


def _gmail_sync(settings: Settings) -> Callable[[Session], dict]:
    def run(s: Session) -> dict:
        from jobpilot.integrations import gmail

        if not gmail.is_ready(settings):
            return {"message": "Gmail not configured; skipped"}
        return gmail.sync_replies(s, settings, gmail.GmailClient(settings))
    return run


def _rematch(settings: Settings) -> Callable[[Session], dict]:
    def run(s: Session) -> dict:
        from jobpilot.services import matching

        n = matching.rematch_stale(s)
        return {"message": f"{n} job score(s) updated (unscored, or scored before your profile/settings changed)",
                "rescored": n}
    return run


def _reminders(settings: Settings) -> Callable[[Session], dict]:
    def run(s: Session) -> dict:
        from jobpilot.services import reminders

        return reminders.check(s)
    return run


def _health_check(settings: Settings) -> Callable[[Session], dict]:
    def run(s: Session) -> dict:
        from jobpilot.services import health

        res = health.check(settings, s, deep=True)
        bad = [f"{k}: {v['detail']}" for k, v in res["checks"].items() if not v["ok"]]
        if bad:
            raise RuntimeError("health check failed — " + "; ".join(bad))
        return {"message": "all checks passed (" + res["checks"]["integrity"]["detail"] + " integrity)"}
    return run


TASK_LABELS = {
    "collect_jobs": "Job collection (enabled sources)",
    "followup_check": "Follow-up eligibility check (never sends)",
    "maintenance": "Job expiry check (stated closing dates; no fetching)",
    "backup": "Database backup",
    "gmail_sync": "Gmail reply sync (only after you authorise Gmail; read-only)",
    "rematch": "Score updates (only unscored jobs, or jobs scored before your profile changed)",
    "reminders": "Deadline and application-status reminders (review items only; never sends)",
    "health_check": "System health and database integrity check",
}

TASKS: dict[str, Callable[[Settings], Callable[[Session], dict]]] = {
    "collect_jobs": _collect,
    "followup_check": _followups,
    "maintenance": _maintenance,
    "backup": _backup,
    "gmail_sync": _gmail_sync,
    "rematch": _rematch,
    "reminders": _reminders,
    "health_check": _health_check,
}
# After a failed run, wait before trying again: 15 min, 30 min, 1 h, ... capped at the task's own interval.
# Stops a task whose source is down from being retried on every scheduler pass.
BACKOFF_BASE_MINUTES = 15


def last_success(session: Session, name: str):
    return session.scalar(select(TaskRun.started_at).where(TaskRun.task_name == name, TaskRun.status == "success")
                          .order_by(TaskRun.started_at.desc()).limit(1))


def next_runs(session: Session) -> dict[str, Any]:
    """When each enabled task is next due (None = disabled). Runs only happen while a scheduler is running
    (in-process or systemd timer) and the computer is awake; missed runs happen on the next pass."""
    cfg = settings_store.get(session, "scheduler")
    out: dict[str, Any] = {}
    for name in TASKS:
        tcfg = cfg.get("tasks", {}).get(name) or {}
        if not tcfg.get("enabled"):
            out[name] = None
            continue
        last = last_success(session, name)
        out[name] = (last + timedelta(hours=float(tcfg.get("every_hours", 24)))) if last else utcnow()
    return out


def backoff_until(session: Session, name: str, every_hours: float):
    """If the task's recent runs failed, the earliest time to retry it; otherwise None."""
    last_ok = last_success(session, name)
    stmt = select(TaskRun).where(TaskRun.task_name == name, TaskRun.status == "failed")
    if last_ok is not None:
        stmt = stmt.where(TaskRun.started_at > last_ok)
    failures = list(session.scalars(stmt.order_by(TaskRun.started_at.desc()).limit(10)))
    if not failures:
        return None
    wait = min(timedelta(minutes=BACKOFF_BASE_MINUTES * 2 ** (len(failures) - 1)), timedelta(hours=every_hours))
    return (failures[0].finished_at or failures[0].started_at) + wait


def due_tasks(session: Session) -> list[str]:
    cfg = settings_store.get(session, "scheduler")
    now = utcnow()
    out = []
    for name in TASKS:
        tcfg = cfg.get("tasks", {}).get(name) or {}
        if not tcfg.get("enabled"):
            continue
        every = float(tcfg.get("every_hours", 24))
        last = last_success(session, name)
        if last is None or now - last >= timedelta(hours=every):
            retry_at = backoff_until(session, name, every)
            if retry_at is None or now >= retry_at:
                out.append(name)
    return out


def run_due(settings: Settings, factory: sessionmaker[Session], trigger: str = "scheduled") -> dict[str, Any]:
    results: dict[str, Any] = {}
    try:
        with task_lock(settings, "scheduler"):
            with factory() as s:
                names = due_tasks(s)
            for name in names:
                run = run_task(settings, factory, name, TASKS[name](settings), trigger=trigger, retries=1, retry_delay=5)
                results[name] = run.status
    except TaskLocked:
        return {"skipped": "another scheduler pass is running"}
    return results


def start_background_scheduler(settings: Settings, factory: sessionmaker[Session],
                               initial_delay: float = 5.0) -> threading.Thread:
    """Run ``run_due`` periodically in a daemon thread. Stop with ``thread.stop_event.set()``."""
    stop = threading.Event()

    def loop() -> None:
        delay = initial_delay  # first pass shortly after start-up
        while not stop.wait(delay):
            delay = settings.scheduler_poll_seconds
            try:
                res = run_due(settings, factory)
                if res:
                    log.info("scheduler pass", extra={"results": res})
            except Exception:  # never kill the thread
                log.exception("scheduler pass failed")

    t = threading.Thread(target=loop, name="jobpilot-scheduler", daemon=True)
    t.stop_event = stop  # type: ignore[attr-defined]
    t.start()
    return t


def systemd_units(python: str, workdir: str, every_minutes: int = 60) -> dict[str, str]:
    service = f"""[Unit]
Description=JobPilot scheduled tasks (job collection, follow-up checks, backups)

[Service]
Type=oneshot
WorkingDirectory={workdir}
ExecStart={python} -m jobpilot.cli scheduler run-due
Nice=10
MemoryMax=600M
"""
    timer = f"""[Unit]
Description=Run JobPilot scheduled tasks every {every_minutes} minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec={every_minutes}min
Persistent=true

[Install]
WantedBy=timers.target
"""
    return {"jobpilot-scheduler.service": service, "jobpilot-scheduler.timer": timer}
