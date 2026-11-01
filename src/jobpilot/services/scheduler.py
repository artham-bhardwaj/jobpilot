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
import os
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


class SourcesUnavailable(RuntimeError):
    """Every source failed: not a successful empty collection."""


def _collect(settings: Settings) -> Callable[[Session], dict]:
    from jobpilot.services import jobs as job_svc

    def run(s: Session) -> dict:
        res = job_svc.collect_all(s, settings, trigger="scheduled")
        attempted = len(res["sources"])
        if attempted and res["failed"] == attempted:
            s.commit()  # keep the per-source failure records; the task itself is recorded as failed (and backs off)
            detail = "; ".join(f"{n}: {v['status']} {(v.get('errors') or [''])[0][:120]}" for n, v in res["sources"].items())
            raise SourcesUnavailable(f"all {attempted} source(s) failed or were unavailable — {detail}")
        return res
    return run


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
        client = gmail.GmailClient(settings)
        res = gmail.sync_replies(s, settings, client)
        conf = gmail.detect_confirmation_emails(s, settings, client)  # headers only; records confirmed submissions
        from jobpilot.services import tracker

        out = tracker.detect_outcome_emails(s, settings, client)  # headers only; rejections, interviews …
        return {**res, "confirmations": conf, "outcomes": out,
                "message": "; ".join(x for x in (res.get("message", ""), conf["message"], out["message"]) if x)}
    return run


def _rematch(settings: Settings) -> Callable[[Session], dict]:
    def run(s: Session) -> dict:
        from jobpilot.services import matching

        n = matching.rematch_stale(s)
        out = {"message": f"{n} job score(s) updated (unscored, or scored before your profile/settings changed)",
               "rescored": n}
        from jobpilot.services import cleanup, settings_store

        if n and settings_store.get(s, "job_cleanup")["auto"]:
            out["hidden"] = cleanup.run(s, trigger="rematch")["hidden"]
        return out
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


def _notify(settings: Settings) -> Callable[[Session], dict]:
    def run(s: Session) -> dict:
        from jobpilot.services import notifications

        return notifications.run(s, settings)
    return run


def _outreach(settings: Settings) -> Callable[[Session], dict]:
    def run(s: Session) -> dict:
        from jobpilot.services import outreach_auto

        return outreach_auto.run(s, settings)
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
    "notify": "Telegram notifications and daily summary (only when Telegram is configured; sends to your chat only)",
    "outreach": "Recruiter outreach (only when outreach.auto_enabled; sends only to addresses you verified)",
    "agents": "Agents cycle: search → tailored resumes → apply where safe (sends nothing in dry-run mode)",
}


def _agents(settings: Settings) -> Callable[[Session], dict]:
    def run(s: Session) -> dict:
        from jobpilot.agents import run_cycle

        return run_cycle(s, settings)
    return run


TASKS: dict[str, Callable[[Settings], Callable[[Session], dict]]] = {
    "collect_jobs": _collect,
    "followup_check": _followups,
    "maintenance": _maintenance,
    "backup": _backup,
    "gmail_sync": _gmail_sync,
    "rematch": _rematch,
    "reminders": _reminders,
    "health_check": _health_check,
    "outreach": _outreach,  # after collection and rematch, before notify
    "agents": _agents,  # search → tailored resumes → apply; before notify, so its summary goes out in the same pass
    "notify": _notify,  # last: a pass that collects jobs notifies about them in the same pass
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


# Persisted in integration_state (no extra table): the running scheduler, and its most recent pass.
RUNNER_KEY = "scheduler.runner"
PASS_KEY = "scheduler.last_pass"


def _record(factory: sessionmaker[Session], key: str, value: dict) -> None:
    """Best effort: observability must never break a pass."""
    from jobpilot.services.notifications import set_state

    try:
        with factory() as s:
            set_state(s, key, value)
            s.commit()
    except Exception:  # pragma: no cover - e.g. database locked for too long
        log.warning("could not record scheduler state", extra={"key": key}, exc_info=True)


def _iso(dt) -> str:
    return dt.isoformat(timespec="seconds")


def run_due(settings: Settings, factory: sessionmaker[Session], trigger: str = "scheduled",
            runner: str = "cli") -> dict[str, Any]:
    """One scheduler pass: run every enabled task that is due. Records the pass (empty passes too)."""
    results: dict[str, Any] = {}
    started = utcnow()
    try:
        with task_lock(settings, "scheduler"):
            with factory() as s:
                names = due_tasks(s)
            for name in names:
                run = run_task(settings, factory, name, TASKS[name](settings), trigger=trigger, retries=1, retry_delay=5)
                results[name] = run.status
    except TaskLocked:
        log.debug("scheduler pass skipped: another pass is running")
        return {"skipped": "another scheduler pass is running"}
    finished = utcnow()
    if not results:
        log.debug("scheduler pass: nothing due")
    _record(factory, PASS_KEY, {"started_at": _iso(started), "finished_at": _iso(finished), "runner": runner,
                                "pid": os.getpid(), "ran": results})
    return results


def start_background_scheduler(settings: Settings, factory: sessionmaker[Session],
                               initial_delay: float = 5.0) -> threading.Thread:
    """Run ``run_due`` periodically in a daemon thread. Stop with ``thread.stop_event.set()``."""
    stop = threading.Event()
    started = utcnow()
    _record(factory, RUNNER_KEY, {"mode": "in-process", "pid": os.getpid(), "started_at": _iso(started),
                                  "poll_seconds": settings.scheduler_poll_seconds,
                                  "first_pass_at": _iso(started + timedelta(seconds=initial_delay))})

    def loop() -> None:
        delay = initial_delay  # first pass shortly after start-up
        while not stop.wait(delay):
            delay = settings.scheduler_poll_seconds
            try:
                res = run_due(settings, factory, runner="in-process")
                if res:
                    log.info("scheduler pass", extra={"results": res})
            except Exception:  # never kill the thread
                log.exception("scheduler pass failed")

    t = threading.Thread(target=loop, name="jobpilot-scheduler", daemon=True)
    t.stop_event = stop  # type: ignore[attr-defined]
    t.start()
    return t


def _alive(pid: int | None) -> bool:
    """Is ``pid`` a running JobPilot process (guards against a reused pid)?"""
    if not pid:
        return False
    try:
        cmdline = open(f"/proc/{int(pid)}/cmdline", "rb").read().replace(b"\0", b" ")
    except OSError:
        return False
    return b"jobpilot" in cmdline


def _parse(value: str | None):
    from datetime import datetime

    return datetime.fromisoformat(value) if value else None


def status_report(session: Session, settings: Settings, now=None) -> dict[str, Any]:
    """Everything needed to tell whether scheduling works: the running scheduler, its last and next pass, and per
    task: enabled, interval, last run (manual or scheduled, success or failure), last success, due, next due, and the
    pass expected to run it. Times are naive UTC like everything stored by JobPilot.

    A due task runs at the next scheduler pass (every ``poll_seconds``), not at the instant it becomes due. The next
    pass is only predicted when a live in-process scheduler is known; nothing is invented otherwise.
    """
    from jobpilot.services.notifications import get_state

    now = now or utcnow()
    runner = get_state(session, RUNNER_KEY)
    last_pass = get_state(session, PASS_KEY)
    poll = int((runner or {}).get("poll_seconds") or settings.scheduler_poll_seconds)
    live = bool(runner) and runner.get("mode") == "in-process" and _alive(runner.get("pid"))
    next_pass = None
    if live:
        base = None
        if last_pass and last_pass.get("pid") == runner.get("pid") and last_pass.get("runner") == "in-process":
            base = _parse(last_pass["finished_at"]) + timedelta(seconds=poll)
        else:  # no pass by this process yet
            base = _parse(runner.get("first_pass_at"))
        next_pass = base
    if live:
        scheduler_state = "running in this JobPilot server (pid %s)" % runner["pid"]
    elif settings.scheduler_enabled and not runner:
        scheduler_state = ("enabled, but no scheduler has recorded its state yet (a server started before this version "
                           "doesn't; restart `jobpilot serve`)")
    elif settings.scheduler_enabled:
        scheduler_state = (f"enabled, but the scheduler that last ran (pid {runner.get('pid')}) is not running "
                           "(is `jobpilot serve` running?)")
    else:
        scheduler_state = "not enabled in the server (JOBPILOT_SCHEDULER_ENABLED is not true); tasks run only when started"
    overdue = bool(next_pass and now - next_pass > timedelta(seconds=2 * poll + 300))
    cfg = settings_store.get(session, "scheduler")
    due = set(due_tasks(session))
    tasks = {}
    for name in TASKS:
        tcfg = cfg.get("tasks", {}).get(name) or {}
        every = float(tcfg.get("every_hours", 24))
        last_ok = last_success(session, name)
        last_run = session.scalar(select(TaskRun).where(TaskRun.task_name == name).order_by(TaskRun.id.desc()).limit(1))
        retry_at = backoff_until(session, name, every) if tcfg.get("enabled") else None
        next_due = None
        expected = None
        if tcfg.get("enabled"):
            next_due = (last_ok + timedelta(hours=every)) if last_ok else now
            if retry_at and retry_at > next_due:
                next_due = retry_at
            if next_pass:  # the first pass at or after next_due (approximate: passes drift by their duration)
                if next_due <= next_pass:
                    expected = next_pass
                else:
                    steps = -(-(next_due - next_pass).total_seconds() // poll)
                    expected = next_pass + timedelta(seconds=steps * poll)
        tasks[name] = {
            "enabled": bool(tcfg.get("enabled")), "every_hours": every, "label": TASK_LABELS.get(name, name),
            "last_success": last_ok,
            "last_run": None if last_run is None else {
                "id": last_run.id, "trigger": last_run.trigger, "status": last_run.status,
                "started_at": last_run.started_at, "finished_at": last_run.finished_at,
                "message": (last_run.message or "")[:200]},
            "due": name in due, "retry_after_failure_at": retry_at, "next_due": next_due, "expected_run": expected,
        }
    return {"scheduler": scheduler_state, "live": live, "enabled_in_settings": settings.scheduler_enabled,
            "poll_seconds": poll, "runner": runner, "last_pass": last_pass, "next_pass": next_pass,
            "pass_overdue": overdue, "now": now, "tasks": tasks,
            "note": f"A due task runs at the next scheduler pass (every {poll} s), not at the exact moment it becomes "
                    "due. Expected times are approximate."}


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
