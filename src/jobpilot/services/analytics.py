"""Analytics computed from stored records, plus CSV export.

Rates are returned as ``Rate`` objects carrying numerator and denominator.
When the denominator is below ``MIN_SAMPLE`` the value is ``None`` with an
"insufficient data" note, so a 0/0 or 1/2 is never shown as a meaningful 0% or 50%.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobpilot.models import (
    Application,
    ApplicationEvent,
    Contact,
    EmailDraft,
    Interview,
    Job,
    JobSighting,
    JobSource,
    ResumeVersion,
    utcnow,
)
from jobpilot.services import settings_store

MIN_SAMPLE = 5
RESPONDED = {"assessment", "interview", "offer", "rejected"}
POSITIVE = {"assessment", "interview", "offer"}


@dataclass
class Rate:
    k: int
    n: int

    @property
    def value(self) -> float | None:
        return self.k / self.n if self.n >= MIN_SAMPLE else None

    @property
    def note(self) -> str:
        if self.n == 0:
            return "no data"
        if self.n < MIN_SAMPLE:
            return f"insufficient data ({self.k}/{self.n})"
        return f"{self.k}/{self.n}"

    def as_dict(self) -> dict:
        return {"k": self.k, "n": self.n, "value": self.value, "note": self.note}


def _reached(session: Session) -> dict[int, set[str]]:
    """Every status each application has ever reached (from its event history)."""
    out: dict[int, set[str]] = defaultdict(set)
    for app_id, to in session.execute(select(ApplicationEvent.application_id, ApplicationEvent.to_status)):
        out[app_id].add(to)
    return out


def summary(session: Session) -> dict[str, Any]:
    now = utcnow()
    threshold = settings_store.get(session, "matching")["relevance_threshold"]
    total_jobs = session.scalar(select(func.count(Job.id))) or 0
    matched = session.scalar(select(func.count(Job.id)).where(Job.relevance_score.is_not(None))) or 0
    relevant = session.scalar(select(func.count(Job.id)).where(Job.relevance_score >= threshold)) or 0
    jobs = {
        "total": total_jobs,
        "active": session.scalar(select(func.count(Job.id)).where(Job.status == "active")) or 0,
        "expired": session.scalar(select(func.count(Job.id)).where(Job.status == "expired")) or 0,
        "last_7_days": session.scalar(select(func.count(Job.id)).where(Job.first_seen_at >= now - timedelta(days=7))) or 0,
        "last_30_days": session.scalar(select(func.count(Job.id)).where(Job.first_seen_at >= now - timedelta(days=30))) or 0,
        "reposts": session.scalar(select(func.count(Job.id)).where(Job.repost_of_id.is_not(None))) or 0,
        "relevant": relevant,
        "relevant_rate": Rate(relevant, matched).as_dict(),
        "missing": {
            "posted_date": session.scalar(select(func.count(Job.id)).where(Job.posted_at.is_(None))) or 0,
            "salary": session.scalar(select(func.count(Job.id)).where(Job.salary_min.is_(None), Job.salary_max.is_(None))) or 0,
            "location": session.scalar(select(func.count(Job.id)).where(Job.location.is_(None))) or 0,
            "experience_requirement": session.scalar(select(func.count(Job.id)).where(Job.experience_min_years.is_(None))) or 0,
        },
        "eligibility": dict(session.execute(select(Job.eligibility, func.count()).where(Job.status == "active")
                                            .group_by(Job.eligibility)).all()),
    }
    apps = list(session.scalars(select(Application)))
    reached = _reached(session)
    submitted = [a for a in apps if a.applied_at is not None]
    responded = [a for a in submitted if reached[a.id] & RESPONDED]
    positive = [a for a in submitted if reached[a.id] & POSITIVE]
    interviewed = [a for a in submitted if "interview" in reached[a.id]]
    offers = [a for a in submitted if "offer" in reached[a.id]]
    status_counts = defaultdict(int)
    for a in apps:
        status_counts[a.status] += 1
    days_to_response = []
    for a in responded:
        first = min((e.ts for e in a.events if e.to_status in RESPONDED), default=None)
        if first and a.applied_at:
            days_to_response.append((first - a.applied_at).days)
    applications = {
        "total": len(apps), "submitted": len(submitted), "by_status": dict(status_counts),
        "response_rate": Rate(len(responded), len(submitted)).as_dict(),
        "positive_response_rate": Rate(len(positive), len(submitted)).as_dict(),
        "interview_rate": Rate(len(interviewed), len(submitted)).as_dict(),
        "offer_rate": Rate(len(offers), len(submitted)).as_dict(),
        "interview_to_offer": Rate(len(offers), len(interviewed)).as_dict(),
        "median_days_to_response": sorted(days_to_response)[len(days_to_response) // 2] if len(days_to_response) >= MIN_SAMPLE else None,
        "awaiting_response": sum(1 for a in submitted if a.status == "applied"),
    }
    interviews = {
        "total": session.scalar(select(func.count(Interview.id))) or 0,
        "upcoming": session.scalar(select(func.count(Interview.id)).where(Interview.status == "scheduled",
                                                                           Interview.scheduled_at >= now)) or 0,
        "by_outcome": dict(session.execute(select(Interview.outcome, func.count()).group_by(Interview.outcome)).all()),
    }
    sent = list(session.scalars(select(EmailDraft).where(EmailDraft.status == "sent")))
    outreach = {"drafts": session.scalar(select(func.count(EmailDraft.id))) or 0, "sent": len(sent),
                "replies": sum(1 for d in sent if d.reply_received_at),
                "reply_rate": Rate(sum(1 for d in sent if d.reply_received_at), len(sent)).as_dict(),
                "contacts": session.scalar(select(func.count(Contact.id))) or 0,
                "verified_contacts": session.scalar(select(func.count(Contact.id)).where(Contact.email_status == "verified")) or 0}
    return {"jobs": jobs, "applications": applications, "interviews": interviews, "outreach": outreach,
            "sources": source_effectiveness(session, threshold, reached),
            "resumes": resume_performance(session, reached), "min_sample": MIN_SAMPLE, "threshold": threshold}


def source_effectiveness(session: Session, threshold: float, reached: dict[int, set[str]]) -> list[dict]:
    rows = []
    for src in session.scalars(select(JobSource).order_by(JobSource.name)):
        job_ids = set(session.scalars(select(JobSighting.job_id).where(JobSighting.source_id == src.id)))
        if not job_ids:
            rows.append({"source": src.name, "kind": src.kind, "jobs": 0, "relevant": 0, "relevant_rate": Rate(0, 0).as_dict(),
                         "applications": 0, "responses": Rate(0, 0).as_dict(), "interviews": 0})
            continue
        relevant = session.scalar(select(func.count(Job.id)).where(Job.id.in_(job_ids), Job.relevance_score >= threshold)) or 0
        apps = [a for a in session.scalars(select(Application).where(Application.job_id.in_(job_ids))) if a.applied_at]
        resp = sum(1 for a in apps if reached[a.id] & RESPONDED)
        rows.append({"source": src.name, "kind": src.kind, "jobs": len(job_ids), "relevant": relevant,
                     "relevant_rate": Rate(relevant, len(job_ids)).as_dict(), "applications": len(apps),
                     "responses": Rate(resp, len(apps)).as_dict(),
                     "interviews": sum(1 for a in apps if "interview" in reached[a.id])})
    return rows


def resume_performance(session: Session, reached: dict[int, set[str]]) -> list[dict]:
    by_version: dict[int, list[Application]] = defaultdict(list)
    for a in session.scalars(select(Application).where(Application.applied_at.is_not(None))):
        if a.resume_version_id:
            by_version[a.resume_version_id].append(a)
    rows = []
    for vid, apps in by_version.items():
        v = session.get(ResumeVersion, vid)
        rows.append({"resume": v.label if v else f"#{vid}", "version_id": vid, "applications": len(apps),
                     "responses": Rate(sum(1 for a in apps if reached[a.id] & RESPONDED), len(apps)).as_dict(),
                     "interviews": Rate(sum(1 for a in apps if "interview" in reached[a.id]), len(apps)).as_dict()})
    return sorted(rows, key=lambda r: -r["applications"])


# ---- CSV export ------------------------------------------------------------------

def _safe(value: Any) -> Any:
    """Neutralise spreadsheet formula injection."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _csv(header: list[str], rows) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([_safe(x) if x is not None else "" for x in r])
    return buf.getvalue()


def export_csv(session: Session, kind: str) -> str:
    if kind == "jobs":
        return _csv(["id", "title", "company", "location", "remote_type", "category", "relevance", "eligibility",
                     "status", "user_status", "posted_at", "first_seen_at", "source_url", "apply_url", "repost_of_id"],
                    ((j.id, j.title, j.company_name, j.location, j.remote_type, j.category, j.relevance_score,
                      j.eligibility, j.status, j.user_status, j.posted_at, j.first_seen_at, j.source_url, j.apply_url,
                      j.repost_of_id) for j in session.scalars(select(Job).order_by(Job.id))))
    if kind == "applications":
        return _csv(["id", "job_id", "title", "company", "status", "created_at", "applied_at", "confirmation_method",
                     "resume_version_id", "resume", "closed_at"],
                    ((a.id, a.job_id, a.job.title, a.job.company_name, a.status, a.created_at, a.applied_at,
                      a.confirmation_method, a.resume_version_id, a.resume_version.label if a.resume_version else None,
                      a.closed_at) for a in session.scalars(select(Application).order_by(Application.id))))
    if kind == "contacts":
        return _csv(["id", "name", "title", "company", "role_type", "email", "email_status", "source_type", "source_url",
                     "confidence", "discovered_at", "verified_at", "do_not_contact"],
                    ((c.id, c.name, c.title, c.company.name if c.company else None, c.role_type, c.email, c.email_status,
                      c.source_type, c.source_url, c.confidence, c.discovered_at, c.verified_at, c.do_not_contact)
                     for c in session.scalars(select(Contact).order_by(Contact.id))))
    if kind == "drafts":
        return _csv(["id", "kind", "status", "to", "subject", "job_id", "contact_id", "approved_at", "sent_at", "sent_via",
                     "reply_received_at", "follow_up_due_at"],
                    ((d.id, d.kind, d.status, d.to_address, d.subject, d.job_id, d.contact_id, d.approved_at, d.sent_at,
                      d.sent_via, d.reply_received_at, d.follow_up_due_at)
                     for d in session.scalars(select(EmailDraft).order_by(EmailDraft.id))))
    if kind == "interviews":
        return _csv(["id", "application_id", "job", "company", "round", "stage", "scheduled_at", "timezone", "format",
                     "status", "outcome"],
                    ((i.id, i.application_id, i.application.job.title, i.application.job.company_name,
                      i.round_name or i.round_no, i.stage, i.scheduled_at, i.timezone, i.format, i.status, i.outcome)
                     for i in session.scalars(select(Interview).order_by(Interview.id))))
    if kind == "summary":
        s = summary(session)
        rows = []
        for section in ("jobs", "applications", "interviews", "outreach"):
            for key, val in s[section].items():
                if isinstance(val, dict) and "note" in val:
                    rows.append((section, key, "" if val["value"] is None else round(val["value"], 4), val["note"]))
                elif isinstance(val, dict):
                    for k2, v2 in val.items():
                        rows.append((section, f"{key}.{k2}", v2, ""))
                else:
                    rows.append((section, key, "" if val is None else val, "missing/insufficient" if val is None else ""))
        return _csv(["section", "metric", "value", "note"], rows)
    raise ValueError(f"Unknown export {kind!r}")


EXPORT_KINDS = ("jobs", "applications", "contacts", "drafts", "interviews", "summary")
