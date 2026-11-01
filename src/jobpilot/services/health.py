"""System health checks."""

from __future__ import annotations

import importlib.util
import shutil
from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from jobpilot import __version__
from jobpilot.config import Settings
from jobpilot.db import current_revision, head_revision
from jobpilot.models import JobSource, SourceRun, TaskRun


def check(settings: Settings, session: Session, deep: bool = False) -> dict:
    """``deep`` adds an SQLite integrity check (reads the whole file; used by the scheduled check and ``doctor``)."""
    checks: dict[str, dict] = {}
    try:
        session.execute(text("SELECT 1"))
        rev = current_revision(session.get_bind())
        head = head_revision(settings)
        checks["database"] = {"ok": rev == head, "detail": f"revision {rev} (head {head})"}
    except Exception as exc:  # pragma: no cover - defensive
        checks["database"] = {"ok": False, "detail": str(exc)}

    loose = [str(p) for p in (settings.data_path, settings.db_file) if p and p.exists() and p.stat().st_mode & 0o077]
    checks["data_permissions"] = {"ok": not loose,
                                  "detail": "owner-only" if not loose else f"readable by others: {', '.join(loose)}"}

    usage = shutil.disk_usage(settings.data_path)
    checks["disk"] = {"ok": usage.free > 500_000_000, "detail": f"{usage.free / 1e9:.1f} GB free"}

    pw = importlib.util.find_spec("playwright") is not None
    checks["browser_automation"] = {
        "ok": True,
        "detail": ("enabled" if settings.browser_enabled else "disabled")
        + ("" if pw else " (playwright not installed)"),
    }
    gmail_cfg = settings.gmail_secrets_file.exists()
    gmail_tok = settings.gmail_token_file.exists()
    from jobpilot.integrations import gmail as gmail_int

    gmail_err = gmail_int.auth_error(settings) if gmail_tok else None
    checks["gmail"] = {
        # A warning, not a failure: the app itself is healthy and a restart can't fix an expired sign-in (failing
        # here would mark the Docker container unhealthy).
        "ok": True,
        "warn": bool(settings.gmail_enabled and gmail_err),
        "detail": "disabled" if not settings.gmail_enabled
        else f"enabled; client secrets {'found' if gmail_cfg else 'MISSING'}; "
             + ("sign-in expired: " + gmail_err if gmail_err else 'authorized' if gmail_tok else 'not authorized'),
    }
    from jobpilot.services import scheduler as sched_svc

    rep = sched_svc.status_report(session, settings)
    lp = rep["last_pass"] or {}
    checks["scheduler"] = {
        "ok": not rep["pass_overdue"],
        "detail": ("in-process" if settings.scheduler_enabled else "manual / external timer")
        + (f"; running (pid {rep['runner']['pid']})" if rep["live"] else "")
        + (f"; last pass {lp['finished_at']} UTC" if lp else "; no pass recorded")
        + ("; next pass OVERDUE" if rep["pass_overdue"] else ""),
    }
    from jobpilot.integrations import telegram
    from jobpilot.models import Notification

    tg = telegram.config_status(settings)
    stuck = session.scalar(select(func.count(Notification.id)).where(Notification.status == "failed")) or 0
    checks["telegram"] = {
        "ok": not (tg["enabled"] and stuck),
        "detail": ("disabled" if not tg["enabled"] else "configured" if tg["ready"] else "incomplete: " + "; ".join(tg["problems"]))
        + (f"; {stuck} notification(s) failed to deliver" if stuck else ""),
    }

    failed = session.scalar(select(TaskRun).where(TaskRun.status == "failed").order_by(TaskRun.id.desc()).limit(1))
    checks["last_task_failure"] = {
        "ok": True,
        "detail": f"{failed.task_name} at {failed.started_at:%Y-%m-%d %H:%M}" if failed else "none",
    }
    if deep:
        try:
            res = session.execute(text("PRAGMA quick_check")).scalar()
            checks["integrity"] = {"ok": res == "ok", "detail": res}
        except Exception as exc:  # pragma: no cover - defensive
            checks["integrity"] = {"ok": False, "detail": str(exc)}

    from jobpilot.services.backup import list_backups

    backups = list_backups(settings)
    if backups:
        age = (datetime.now() - backups[0]["modified"]).days
        checks["backups"] = {"ok": True, "detail": f"{len(backups)} backup(s); newest {age} day(s) old"
                             + (" — consider `jobpilot backup create`" if age > 7 else "")}
    else:
        checks["backups"] = {"ok": True, "detail": "no backups yet — run `jobpilot backup create`"}

    failing = []
    for src in session.scalars(select(JobSource).where(JobSource.enabled.is_(True))):
        last = session.scalar(select(SourceRun).where(SourceRun.source_id == src.id, SourceRun.dry_run.is_(False))
                              .order_by(SourceRun.id.desc()).limit(1))
        if last and last.status in ("failed", "unavailable", "anomaly"):
            failing.append(f"{src.name} ({last.status})")
    checks["sources"] = {"ok": True, "detail": ("last run not successful: " + ", ".join(failing)) if failing
                         else "no failing sources"}
    return {"version": __version__, "ok": all(c["ok"] for c in checks.values()), "checks": checks}
