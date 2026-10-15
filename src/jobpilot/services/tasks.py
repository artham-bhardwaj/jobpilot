"""Task execution with overlap prevention, bounded retries and run records.

Every manual or scheduled background operation goes through :func:`run_task`
so that it is logged in ``task_runs`` and cannot run twice concurrently
(an exclusive ``fcntl`` lock file per task name, which the OS releases even
if the process dies).
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.config import Settings
from jobpilot.models import TaskRun, utcnow

log = logging.getLogger(__name__)


class TaskLocked(RuntimeError):
    pass


@contextmanager
def task_lock(settings: Settings, name: str):
    safe = re.sub(r"[^a-z0-9_.-]", "_", name.lower())
    lock_dir = settings.data_path / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{safe}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TaskLocked(f"Task {name!r} is already running") from exc
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


INTERRUPTED = "Interrupted: JobPilot stopped before this task finished (crash, kill or power loss)"


def mark_interrupted(session: Session, settings: Settings, *, name: str | None = None, before_id: int | None = None,
                     min_age: timedelta = timedelta(0)) -> int:
    """Mark ``running`` task records whose process is gone as failed, so a crash shows up as a failure.

    With ``name`` the caller must hold that task's lock (so no older run of it can still be alive).
    Without it, a record counts as orphaned only if its task lock is free.
    """
    q = select(TaskRun).where(TaskRun.status == "running")
    if name is not None:
        q = q.where(TaskRun.task_name == name)
    if before_id is not None:
        q = q.where(TaskRun.id < before_id)
    cutoff = utcnow() - min_age
    count = 0
    for run in session.scalars(q):
        if run.started_at and run.started_at > cutoff:
            continue
        if name is None:
            try:
                with task_lock(settings, run.task_name):
                    pass
            except TaskLocked:
                continue  # still running in another process
        run.status, run.message, run.finished_at = "failed", INTERRUPTED, utcnow()
        count += 1
    if count:
        log.warning("interrupted task runs marked failed", extra={"count": count})
    return count


def run_task(
    settings: Settings,
    factory: sessionmaker[Session],
    name: str,
    func: Callable[[Session], dict[str, Any] | None],
    trigger: str = "manual",
    retries: int = 0,
    retry_delay: float = 2.0,
) -> TaskRun:
    """Run ``func(session)`` under a lock; retries transient failures up to ``retries`` times."""
    with factory() as s:
        run = TaskRun(task_name=name, trigger=trigger, status="running")
        s.add(run)
        s.commit()
        run_id = run.id
    try:
        with task_lock(settings, name):
            with factory() as s:
                mark_interrupted(s, settings, name=name, before_id=run_id)
                s.commit()
            attempt = 0
            while True:
                attempt += 1
                try:
                    with factory() as s:
                        details = func(s) or {}
                        s.commit()
                    status, message = "success", details.pop("message", None) if isinstance(details, dict) else None
                    break
                except Exception as exc:
                    log.exception("task attempt failed", extra={"task": name, "attempt": attempt})
                    if attempt > retries:
                        status, message, details = "failed", f"{type(exc).__name__}: {exc}", {}
                        break
                    time.sleep(retry_delay * attempt)
    except TaskLocked as exc:
        status, message, details, attempt = "skipped", str(exc), {}, 0

    with factory() as s:
        run = s.get(TaskRun, run_id)
        run.status = status
        run.message = message
        run.details = details or None
        run.attempts = attempt
        run.finished_at = utcnow()
        s.commit()
        s.refresh(run)
        s.expunge(run)
    log.info("task finished", extra={"task": name, "status": status, "trigger": trigger})
    return run
