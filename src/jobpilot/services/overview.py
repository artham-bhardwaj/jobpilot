"""Dashboard overview statistics."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobpilot.models import AuditLog, Job, JobSource, Resume, ResumeVersion, TaskRun, utcnow
from jobpilot.services import applications as apps
from jobpilot.services import drafts as ds
from jobpilot.services import interviews as iv_svc
from jobpilot.services import profile as profile_svc
from jobpilot.services import review, settings_store


def stats(session: Session) -> dict:
    now = utcnow()
    threshold = settings_store.get(session, "matching")["relevance_threshold"]
    active = select(func.count(Job.id)).where(Job.status == "active")
    out: dict = {
        "jobs_active": session.scalar(active) or 0,
        "jobs_new_7d": session.scalar(select(func.count(Job.id)).where(Job.first_seen_at >= now - timedelta(days=7))) or 0,
        "jobs_relevant": session.scalar(active.where(Job.relevance_score >= threshold, Job.user_status != "dismissed")) or 0,
        "shortlisted": session.scalar(active.where(Job.user_status == "shortlisted")) or 0,
        "applications": apps.counts(session),
        "upcoming": iv_svc.upcoming(session, limit=5),
        "review_count": review.open_count(session),
        "followups_due": len(ds.due_followups(session)),
        "threshold": threshold,
        "top_jobs": list(session.scalars(select(Job).where(Job.status == "active", Job.user_status == "new",
                                                          Job.relevance_score >= threshold)
                                         .order_by(Job.relevance_score.desc()).limit(8))),
        "recent_activity": list(session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(12))),
        "last_failure": session.scalar(select(TaskRun).where(TaskRun.status == "failed").order_by(TaskRun.id.desc()).limit(1)),
    }
    out["submitted"] = sum(v for k, v in out["applications"].items() if k in apps.SUBMITTED_STATES)
    out["in_progress"] = sum(v for k, v in out["applications"].items() if k in apps.OPEN_STATES)
    approved_resumes = session.scalar(select(func.count(func.distinct(ResumeVersion.resume_id)))
                                      .where(ResumeVersion.status == "approved")) or 0
    p = profile_svc.get_profile(session)
    out["setup"] = [
        ("Fill in and confirm your profile", p.confirmed_at is not None, "/profile"),
        ("Confirm your skills", bool(profile_svc.confirmed_skills(session)), "/profile#skills"),
        ("Import and approve at least one resume", approved_resumes > 0, "/resumes"),
        ("Add a job source or import jobs", (session.scalar(select(func.count(JobSource.id))) or 0) > 0
         or (session.scalar(select(func.count(Job.id))) or 0) > 0, "/jobs/new"),
        ("Review search preferences", _prefs_saved(session), "/settings"),
    ]
    out["resume_slots"] = session.scalar(select(func.count(Resume.id))) or 0
    return out


def _prefs_saved(session: Session) -> bool:
    from jobpilot.models import AppSetting

    return session.get(AppSetting, "search_preferences") is not None
