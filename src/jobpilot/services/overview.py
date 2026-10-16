"""Dashboard overview statistics."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobpilot.models import Application, AuditLog, Job, JobSource, Resume, ResumeVersion, SourceRun, TaskRun, utcnow
from jobpilot.services import applications as apps
from jobpilot.services import drafts as ds
from jobpilot.services import interviews as iv_svc
from jobpilot.services import profile as profile_svc
from jobpilot.services import queue, settings_store


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
    strong = session.scalar(active.where(Job.relevance_score >= settings_store.get(session, "matching").get("strong_threshold", 75),
                                         Job.hard_conflict.is_(False))) or 0
    out["funnel"] = [
        ("Active jobs", out["jobs_active"], "/jobs"),
        ("Relevant", out["jobs_relevant"], f"/jobs?min_score={threshold}"),
        ("Strong (no hard conflict)", strong, "/review?category=strong_match"),
        ("Shortlisted", out["shortlisted"], "/jobs?user_status=shortlisted"),
        ("Preparing / ready", out["in_progress"], "/applications"),
        ("Applied (with evidence)", out["submitted"], "/applications"),
    ]
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
    out.update(_attention(session, now))
    return out


def _attention(session: Session, now) -> dict:
    """Counts that say what needs doing next. Read-only: nothing here writes (reminders are raised by the scheduler)."""
    from jobpilot.services import reminders

    awaiting = session.scalar(select(func.count(Application.id)).where(Application.status == "ready_for_review",
                                                                      Application.submission_approved_at.is_(None))) or 0
    open_job_ids = select(Application.job_id).where(Application.status.in_(reminders.OPEN_APP_STATES))
    deadlines = []
    for job in session.scalars(select(Job).where(Job.status == "active", Job.user_status != "dismissed",
                                                 (Job.user_status == "shortlisted") | Job.id.in_(open_job_ids))):
        due = reminders.deadline_of(job)
        if due is not None and now <= due <= now + timedelta(days=14):
            deadlines.append((due, job))
    deadlines.sort(key=lambda x: x[0])
    failing = []
    for src in session.scalars(select(JobSource).where(JobSource.enabled.is_(True))):
        last = session.scalar(select(SourceRun).where(SourceRun.source_id == src.id, SourceRun.dry_run.is_(False))
                              .order_by(SourceRun.id.desc()).limit(1))
        if last and last.status in ("failed", "unavailable", "anomaly"):
            failing.append((src, last))
    last_ok = session.scalar(select(func.max(SourceRun.finished_at)).where(
        SourceRun.dry_run.is_(False), SourceRun.status.in_(("success", "partial"))))
    items = queue.build(session)  # built once: the count and the single most important item
    top = min(items, key=lambda i: (-i["priority"], -(i["score"] or 0)), default=None)
    return {"awaiting_approval": awaiting, "deadlines": deadlines[:8], "deadlines_count": len(deadlines),
            "source_failures": failing, "last_discovery": last_ok, "next_item": top, "review_count": len(items)}


def _prefs_saved(session: Session) -> bool:
    from jobpilot.models import AppSetting

    return session.get(AppSetting, "search_preferences") is not None
