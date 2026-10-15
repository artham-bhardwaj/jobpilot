"""Human-review queue.

Combines stored review items (paused browser runs, submissions awaiting approval,
missing resumes, detected interviews) with live checks (jobs flagged by matching,
drafts awaiting approval, unverified skill suggestions).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobpilot.models import EmailDraft, Job, ReviewItem, Skill, utcnow
from jobpilot.services import audit


def open_item(session: Session, kind: str, entity_type: str, entity_id: int, title: str,
              reason: str | None = None, data: dict | None = None) -> ReviewItem:
    existing = session.scalar(select(ReviewItem).where(ReviewItem.kind == kind, ReviewItem.entity_type == entity_type,
                                                       ReviewItem.entity_id == entity_id, ReviewItem.status == "open"))
    if existing:
        existing.reason, existing.data = reason or existing.reason, data or existing.data
        return existing
    item = ReviewItem(kind=kind, entity_type=entity_type, entity_id=entity_id, title=title[:300], reason=reason, data=data)
    session.add(item)
    session.flush()
    return item


def resolve_items(session: Session, kind: str, entity_type: str, entity_id: int, resolution: str,
                  status: str = "resolved") -> int:
    n = 0
    for item in session.scalars(select(ReviewItem).where(ReviewItem.kind == kind, ReviewItem.entity_type == entity_type,
                                                         ReviewItem.entity_id == entity_id, ReviewItem.status == "open")):
        item.status, item.resolved_at, item.resolution = status, utcnow(), resolution
        n += 1
    return n


def resolve(session: Session, item_id: int, status: str, note: str | None = None) -> ReviewItem | None:
    item = session.get(ReviewItem, item_id)
    if item and item.status == "open":
        item.status = "dismissed" if status == "dismissed" else "resolved"
        item.resolved_at, item.resolution = utcnow(), note
        audit.record(session, f"review.{item.status}", "review_item", item.id, kind=item.kind)
    return item


def mark_job_reviewed(session: Session, job: Job, note: str | None = None) -> None:
    item = open_item(session, "job_review", "job", job.id, f"Review job: {job.title}",
                     "; ".join((job.match_details or {}).get("review_reasons", [])))
    item.status, item.resolved_at, item.resolution = "resolved", utcnow(), note or "reviewed"
    audit.record(session, "review.job_reviewed", "job", job.id)


def jobs_needing_review(session: Session, limit: int = 100) -> list[Job]:
    reviewed = select(ReviewItem.entity_id).where(ReviewItem.kind == "job_review", ReviewItem.entity_type == "job",
                                                  ReviewItem.status != "open")
    from jobpilot.models import Application

    applied = select(Application.job_id)
    out = []
    for job in session.scalars(select(Job).where(Job.status == "active", Job.user_status != "dismissed",
                                                 Job.id.not_in(reviewed), Job.id.not_in(applied))
                               .order_by(Job.relevance_score.desc().nulls_last())):
        if (job.match_details or {}).get("needs_review"):
            out.append(job)
            if len(out) >= limit:
                break
    return out


def queue(session: Session) -> dict:
    items = list(session.scalars(select(ReviewItem).where(ReviewItem.status == "open", ReviewItem.kind != "job_review")
                                 .order_by(ReviewItem.created_at.desc())))
    drafts = list(session.scalars(select(EmailDraft).where(EmailDraft.status == "draft").order_by(EmailDraft.updated_at.desc())))
    unverified_skills = session.scalar(select(func.count(Skill.id)).where(Skill.status == "unverified")) or 0
    return {"actions": items, "jobs": jobs_needing_review(session), "drafts": drafts,
            "unverified_skills": unverified_skills}


def open_count(session: Session) -> int:
    q = queue(session)
    return len(q["actions"]) + len(q["jobs"]) + len(q["drafts"]) + (1 if q["unverified_skills"] else 0)
