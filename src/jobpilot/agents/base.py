"""Shared pieces of the four agents: the policy, the pause switch, the report each agent returns."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobpilot.models import AuditLog, Job, utcnow
from jobpilot.services import notifications, settings_store

# What counts against the daily limit: every application the Apply agent sent or submitted (or attempted to:
# a click whose outcome is unknown still counts).
APPLIED_ACTIONS = ("agents.email_sent", "agents.form_submitted", "agents.form_uncertain", "agents.form_rejected")


def policy(session: Session) -> dict[str, Any]:
    return settings_store.get(session, "agents")


def is_paused(session: Session) -> bool:
    """One switch for all automatic sending: Telegram /pause, the System page and the Agents page share it."""
    from jobpilot.services import outreach_auto

    return outreach_auto.is_paused(session)


def set_paused(session: Session, paused: bool, via: str) -> None:
    from jobpilot.services import outreach_auto

    outreach_auto.set_paused(session, paused, via)


def utc_midnight():
    return utcnow().replace(hour=0, minute=0, second=0, microsecond=0)


def applied_today(session: Session) -> int:
    return session.scalar(select(func.count(AuditLog.id)).where(AuditLog.action.in_(APPLIED_ACTIONS),
                                                                AuditLog.ts >= utc_midnight())) or 0


def job_fits(job: Job, pol: dict[str, Any]) -> list[str]:
    """Why ``job`` is outside the agents' policy ([] = it fits)."""
    out = []
    if job.status != "active":
        out.append(f"listing is {job.status}")
    if job.user_status == "dismissed":
        out.append("you dismissed it")
    if job.relevance_score is None or job.relevance_score < float(pol["min_score"]):
        out.append(f"score {job.relevance_score if job.relevance_score is not None else '—'} below {pol['min_score']}")
    if job.eligibility not in pol["allowed_eligibility"]:
        out.append(f"eligibility {job.eligibility}")
    if job.hard_conflict:
        out.append("hard eligibility conflict")
    if pol["job_families"] and job.category not in pol["job_families"]:
        out.append(f"job family {job.category} not in the policy")
    seen = job.last_seen_at or job.first_seen_at
    if seen is not None and seen < utcnow() - timedelta(days=int(pol["max_job_age_days"])):
        out.append(f"not seen in the last {pol['max_job_age_days']} days")
    return out


@dataclass
class Report:
    """What one agent did in one cycle. ``items`` are per-job lines for the Agents page and the digest."""

    agent: str
    summary: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def item(self, job: Job | None, outcome: str, detail: str = "", **extra: Any) -> None:
        self.add(outcome)
        row = {"outcome": outcome, "detail": detail[:500], **extra}
        if job is not None:
            row.update(job_id=job.id, title=job.title, company=job.company_name)
        self.items.append(row)

    def as_dict(self) -> dict[str, Any]:
        return {"agent": self.agent, "summary": self.summary, "counts": dict(self.counts), "items": self.items[:100],
                "errors": self.errors[:20], "at": utcnow().isoformat(timespec="seconds")}


STATE_PREFIX = "agents.last."


def save_report(session: Session, report: Report) -> None:
    notifications.set_state(session, STATE_PREFIX + report.agent, copy.deepcopy(report.as_dict()))


def last_report(session: Session, agent: str) -> dict[str, Any] | None:
    return notifications.get_state(session, STATE_PREFIX + agent)
