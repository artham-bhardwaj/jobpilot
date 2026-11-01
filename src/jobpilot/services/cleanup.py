"""Hide jobs that aren't relevant to you, reversibly.

A job is relevant when it is fresher / SDE-1 (or the level isn't stated), in India, remote (open to you) or Dubai,
in one of your role categories, an SDE / backend role by its title (``services/roles.py``; switch off with
``job_cleanup.sde_backend_only``), scores at least ``job_cleanup.min_score``, and has no hard conflict (e.g. an
experience requirement far above yours). Anything else is *hidden*: its
``user_status`` becomes "dismissed" (so the Jobs page, agents, Telegram and the review queue all skip it, and a
re-collected copy stays hidden) and ``hidden_reason`` says why, prefixed ``cleanup:``. Nothing is deleted.

Never touched: jobs you applied to (or have an application for), saved (shortlisted) or dismissed yourself.
``undo`` restores every job the cleanup hid; changing a job's status yourself clears its cleanup mark.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.models import Application, Job
from jobpilot.services import audit, levels, regions, roles, settings_store

PREFIX = "cleanup:"
REASONS = {
    "level": "above SDE-1",
    "region": "outside India / Dubai / remote",
    "role": "not an SDE / backend role",
    "score": "low match score",
    "conflict": "not suitable (hard conflict)",
}


def reasons(job: Job, min_score: float, sde_backend_only: bool = True) -> list[str]:
    """Why a job isn't relevant (keys of REASONS); empty when it is."""
    out = []
    if levels.of(job) == "no":
        out.append("level")
    if not set(regions.of(job)) & set(regions.REGIONS):
        out.append("region")
    if job.category is None or (sde_backend_only and not roles.is_sde_backend(job.title)):
        out.append("role")
    if job.relevance_score is not None and job.relevance_score < min_score:
        out.append("score")
    if job.hard_conflict:  # e.g. asks for 7+ years when you have 1: never suitable, whatever the score
        out.append("conflict")
    return out


def label(hidden_reason: str | None) -> str:
    keys = (hidden_reason or "").removeprefix(PREFIX).split(",")
    return ", ".join(REASONS.get(k, k) for k in keys if k)


def _candidates(session: Session) -> list[Job]:
    with_app = select(Application.job_id)
    return list(session.scalars(select(Job).where(Job.user_status == "new", Job.id.not_in(with_app))))


def plan(session: Session) -> dict:
    """What a cleanup would hide, without changing anything: {"hide": [(job, [reason keys])], "by_reason": {...}}."""
    cfg = settings_store.get(session, "job_cleanup")
    min_score, sde_only = cfg["min_score"], bool(cfg.get("sde_backend_only", True))
    hide = [(j, r) for j in _candidates(session) if (r := reasons(j, min_score, sde_only))]
    by_reason = {k: sum(k in r for _, r in hide) for k in REASONS}
    return {"hide": hide, "by_reason": by_reason, "min_score": min_score}


def run(session: Session, trigger: str = "manual") -> dict:
    """Hide the irrelevant jobs. Returns {"hidden": n, "by_reason": {...}}. The caller commits."""
    p = plan(session)
    for job, r in p["hide"]:
        job.user_status = "dismissed"
        job.hidden_reason = PREFIX + ",".join(r)
    if p["hide"]:
        audit.record(session, "jobs.cleanup", "job", None, hidden=len(p["hide"]), trigger=trigger, **p["by_reason"])
    return {"hidden": len(p["hide"]), "by_reason": p["by_reason"]}


def hidden_count(session: Session) -> int:
    from sqlalchemy import func

    return session.scalar(select(func.count(Job.id)).where(Job.hidden_reason.like(f"{PREFIX}%"))) or 0


def undo(session: Session) -> int:
    """Restore every job the cleanup hid (back to "new"). Returns how many. The caller commits."""
    jobs = list(session.scalars(select(Job).where(Job.hidden_reason.like(f"{PREFIX}%"))))
    for job in jobs:
        if job.user_status == "dismissed":
            job.user_status = "new"
        job.hidden_reason = None
    if jobs:
        audit.record(session, "jobs.cleanup_undone", "job", None, restored=len(jobs))
    return len(jobs)
