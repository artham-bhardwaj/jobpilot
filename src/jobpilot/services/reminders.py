"""Deadline and application-status reminders, raised as review items. Never sends anything.

Each reminder carries a ``key`` describing the fact it is about (the deadline date, the last status change, the
listing's expiry). A reminder is created at most once per key: running the check again — on schedule, after a
restart, or after the laptop was off for days — never duplicates one, and a reminder you dismissed stays dismissed
until the underlying fact changes. Reminders whose condition no longer holds are resolved automatically.

Nothing here infers an outcome: an application without a reply is "check on it", never "rejected", and an expired
listing is not a rejection.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.models import Application, ApplicationEvent, Job, ReviewItem, utcnow
from jobpilot.services import review, settings_store

KINDS = ("deadline_soon", "application_stale", "listing_expired")
WATCHED_JOB_STATUSES = ("shortlisted",)
OPEN_APP_STATES = ("discovered", "shortlisted", "preparing", "ready_for_review")
WAITING_APP_STATES = ("applied", "assessment", "interview")


def _raise_once(session: Session, kind: str, entity_type: str, entity_id: int, key: str, title: str,
                reason: str) -> bool:
    """Create the reminder unless one for the same fact (key) already exists, in any state."""
    for item in session.scalars(select(ReviewItem).where(ReviewItem.kind == kind, ReviewItem.entity_type == entity_type,
                                                         ReviewItem.entity_id == entity_id)):
        if (item.data or {}).get("key") == key:
            return False
    # An open reminder about an older fact (e.g. a deadline that moved) is replaced, not stacked.
    review.resolve_items(session, kind, entity_type, entity_id, "superseded")
    session.add(ReviewItem(kind=kind, entity_type=entity_type, entity_id=entity_id, title=title[:300], reason=reason,
                           data={"key": key}))
    session.flush()
    return True


def _close_unless(session: Session, kind: str, keep: set[tuple[str, int]], resolution: str) -> int:
    n = 0
    for item in session.scalars(select(ReviewItem).where(ReviewItem.kind == kind, ReviewItem.status == "open")):
        if (item.entity_type, item.entity_id) not in keep:
            item.status, item.resolved_at, item.resolution = "resolved", utcnow(), resolution
            n += 1
    return n


def deadline_of(job: Job) -> datetime | None:
    """End of the stated deadline day (UTC), from structured source data or an explicit sentence only."""
    from jobpilot.services.applications import job_deadline

    d = job_deadline(job)
    if not d:
        return None
    try:
        return datetime.combine(datetime.fromisoformat(d["date"]).date(), time(23, 59, 59))
    except ValueError:
        return None


def check(session: Session, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    cfg = settings_store.get(session, "scheduler")
    warn_days = int(cfg.get("deadline_warning_days", 3))
    stale_days = int(cfg.get("stale_application_days", 21))
    created = {k: 0 for k in KINDS}

    # 1. Deadlines approaching: shortlisted jobs and jobs with an application still being prepared.
    open_apps = {a.job_id: a for a in session.scalars(select(Application).where(Application.status.in_(OPEN_APP_STATES)))}
    watched = list(session.scalars(select(Job).where(Job.status == "active", (Job.user_status.in_(WATCHED_JOB_STATUSES))
                                                     | Job.id.in_(list(open_apps) or [-1]))))
    keep_deadline: set[tuple[str, int]] = set()
    for job in watched:
        due = deadline_of(job)
        if due is None or due < now or due - now > timedelta(days=warn_days):
            continue
        keep_deadline.add(("job", job.id))
        days = max(0, (due.date() - now.date()).days)
        when = "today" if days == 0 else f"in {days} day(s)"
        app = open_apps.get(job.id)
        state = f"application {app.status.replace('_', ' ')}" if app else "shortlisted, no application started"
        if _raise_once(session, "deadline_soon", "job", job.id, due.date().isoformat(),
                       f"Deadline {when}: {job.title} at {job.company_name or '?'}",
                       f"stated deadline {due.date()} ({state})"):
            created["deadline_soon"] += 1
    _close_unless(session, "deadline_soon", keep_deadline, "deadline passed or job no longer watched")

    # 2. Submitted applications with no recorded update for a while. Silence is not a rejection.
    keep_stale: set[tuple[str, int]] = set()
    for app in session.scalars(select(Application).where(Application.status.in_(WAITING_APP_STATES))):
        last = session.scalar(select(ApplicationEvent).where(ApplicationEvent.application_id == app.id)
                              .order_by(ApplicationEvent.ts.desc(), ApplicationEvent.id.desc()).limit(1))
        since = last.ts if last else (app.applied_at or app.created_at)
        if since is None or now - since < timedelta(days=stale_days):
            continue
        keep_stale.add(("application", app.id))
        if _raise_once(session, "application_stale", "application", app.id, f"event:{last.id if last else 0}",
                       f"No update for {(now - since).days} days: {app.job.title} at {app.job.company_name or '?'}",
                       f"status '{app.status}' since {since:%Y-%m-%d}. Check the portal or consider a polite follow-up. "
                       "No reply does not mean rejected."):
            created["application_stale"] += 1
    _close_unless(session, "application_stale", keep_stale, "status changed")

    # 3. Listing gone while an application is still being prepared.
    keep_expired: set[tuple[str, int]] = set()
    for job_id, app in open_apps.items():
        job = app.job
        if job.status == "active":
            continue
        keep_expired.add(("application", app.id))
        key = f"{job.status}:{job.expired_at.isoformat() if getattr(job, 'expired_at', None) else ''}"
        if _raise_once(session, "listing_expired", "application", app.id, key,
                       f"Listing {job.status}: {job.title} at {job.company_name or '?'}",
                       f"your application is still '{app.status}'. Check whether the posting is really closed; "
                       "withdraw it here if so. An expired listing is not a rejection."):
            created["listing_expired"] += 1
    _close_unless(session, "listing_expired", keep_expired, "listing active again or application moved on")

    total = sum(created.values())
    return {"message": f"{total} new reminder(s): " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in created.items()),
            **created}
