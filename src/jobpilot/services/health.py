"""System health checks."""

from __future__ import annotations

import importlib.util
import shutil

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from jobpilot import __version__
from jobpilot.config import Settings
from jobpilot.db import current_revision, head_revision
from jobpilot.models import TaskRun


def check(settings: Settings, session: Session) -> dict:
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
    checks["gmail"] = {
        "ok": True,
        "detail": "disabled" if not settings.gmail_enabled
        else f"enabled; client secrets {'found' if gmail_cfg else 'MISSING'}; "
             f"{'authorized' if gmail_tok else 'not authorized'}",
    }
    checks["scheduler"] = {"ok": True, "detail": "in-process" if settings.scheduler_enabled else "manual / external timer"}

    failed = session.scalar(select(TaskRun).where(TaskRun.status == "failed").order_by(TaskRun.id.desc()).limit(1))
    checks["last_task_failure"] = {
        "ok": True,
        "detail": f"{failed.task_name} at {failed.started_at:%Y-%m-%d %H:%M}" if failed else "none",
    }
    return {"version": __version__, "ok": all(c["ok"] for c in checks.values()), "checks": checks}
