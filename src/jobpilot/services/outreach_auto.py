"""Automatic recruiter outreach (scheduler task ``outreach``), under the ``outreach`` settings.

For each active job that passes the outreach filter (``min_score``, ``allowed_eligibility``, no hard conflict,
``job_families``, not dismissed) and was seen within ``max_job_age_days``:

1. A recruiter email for this job already exists (draft, approved, sent) → nothing more for it (one per job).
2. No recruiter contact linked to the job at the same employer → the job is queued for recruiter discovery
   (review item ``recruiter_needed``; Telegram tells you how to add one). JobPilot never searches LinkedIn, and uses
   the Apollo API only when you run it yourself.
3. A contact exists but its address is not one **you** verified (``apollo_reported``, ``unverified``, ``guessed``) →
   queued for your verification (``recruiter_unverified``). Nothing is sent.
4. A verified contact → a personalised draft from confirmed facts. If anything is missing or the domain doesn't fit
   the employer, it waits for your review (``outreach_review``). Otherwise, with ``auto_send``, it is approved under
   your standing authorization and sent through the existing Gmail send path — the same checks as a manual send
   (verified recipient, not blocked or bounced, daily send limit, no retry after an unknown outcome) — at most
   ``auto_daily_limit`` per day. Without ``auto_send`` it is put in Gmail Drafts for you to send.

Never: follow-ups, old drafts (only drafts this task created in the same run are sent), placeholder or guessed
recipients, or anything while outreach is paused (/pause or the System page).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobpilot.config import Settings
from jobpilot.models import AuditLog, Contact, ContactJob, EmailDraft, Job, utcnow
from jobpilot.services import audit, notifications, review, settings_store
from jobpilot.services import contacts as contact_svc
from jobpilot.services import drafts as draft_svc
from jobpilot.services.text_utils import normalize_company

log = logging.getLogger(__name__)

LIVE = ("draft", "approved", "sent")
SENT_ACTION = "outreach.auto_sent"
# Stored under its historical name, so a pause you set earlier still holds.
PAUSE_KEY = "autopilot.paused"


def is_paused(session: Session) -> bool:
    return bool((notifications.get_state(session, PAUSE_KEY) or {}).get("paused"))


def set_paused(session: Session, paused: bool, via: str) -> None:
    notifications.set_state(session, PAUSE_KEY, {"paused": paused, "via": via, "at": utcnow().isoformat(timespec="seconds")})
    audit.record(session, "outreach.paused" if paused else "outreach.resumed", "outreach", None, via=via)


def job_fits(job: Job, cfg: dict[str, Any]) -> list[str]:
    """Why ``job`` is outside the outreach filter ([] = it fits)."""
    out = []
    if job.status != "active":
        out.append(f"listing is {job.status}")
    if job.user_status == "dismissed":
        out.append("you dismissed it")
    if job.relevance_score is None or job.relevance_score < float(cfg["min_score"]):
        out.append(f"score {job.relevance_score} below {cfg['min_score']}")
    if job.eligibility not in cfg["allowed_eligibility"]:
        out.append(f"eligibility {job.eligibility}")
    if job.hard_conflict:
        out.append("hard eligibility conflict")
    if cfg["job_families"] and job.category not in cfg["job_families"]:
        out.append(f"job family {job.category} not allowed")
    return out


def sent_today(session: Session) -> int:
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return session.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == SENT_ACTION, AuditLog.ts >= start)) or 0


def candidate_jobs(session: Session) -> list[Job]:
    cfg = settings_store.get(session, "outreach")
    since = utcnow() - timedelta(days=int(cfg["max_job_age_days"]))
    jobs = session.scalars(select(Job).where(Job.status == "active", Job.last_seen_at >= since)
                           .order_by(Job.relevance_score.desc().nulls_last(), Job.id))
    return [j for j in jobs if not job_fits(j, cfg)]


def _same_employer(contact: Contact, job: Job) -> bool:
    if contact.company_id and job.company_id:
        return contact.company_id == job.company_id
    a = normalize_company(contact.company.name if contact.company else None)
    return bool(a) and a == normalize_company(job.company_name)


def recruiters_for(session: Session, job: Job) -> list[Contact]:
    ids = [link.contact_id for link in session.scalars(select(ContactJob).where(ContactJob.job_id == job.id))]
    rows = session.scalars(select(Contact).where(Contact.id.in_(ids))) if ids else []
    return [c for c in rows if c.role_type == "recruiter" and _same_employer(c, job) and not c.do_not_contact]


def _has_outreach(session: Session, job: Job) -> bool:
    return session.scalar(select(EmailDraft.id).where(EmailDraft.job_id == job.id,
                                                      EmailDraft.kind.in_(draft_svc.OUTREACH_KINDS),
                                                      EmailDraft.status.in_(LIVE)).limit(1)) is not None


def run(session: Session, settings: Settings, client=None) -> dict[str, Any]:
    cfg = settings_store.get(session, "outreach")
    if not cfg["auto_enabled"]:
        return {"message": "automatic outreach is off (Settings → outreach: auto_enabled)"}
    if is_paused(session):
        return {"message": "paused (/resume to continue); nothing drafted or sent"}
    from jobpilot.integrations import gmail

    gmail_ready = gmail.is_ready(settings)
    stats = {"jobs": 0, "already_contacted": 0, "recruiter_needed": 0, "unverified": 0, "review": 0, "sent": 0,
             "in_gmail_drafts": 0, "failed": 0}
    for job in candidate_jobs(session):
        stats["jobs"] += 1
        if _has_outreach(session, job):
            stats["already_contacted"] += 1
            continue
        contacts = [c for c in recruiters_for(session, job) if c.email and c.email_status != "bounced"
                    and not contact_svc.is_blocked(session, c.email, c.company_id)]
        if not contacts:
            review.open_item(session, "recruiter_needed", "job", job.id,
                             f"Find a recruiter for {job.title} at {job.company_name or '?'}",
                             f"Add one you found (LinkedIn/Apollo): jobpilot recruiters add --job {job.id} --name … "
                             f"--company \"{job.company_name or ''}\" --email … --email-status verified "
                             "--verification-note …", {"job_id": job.id})
            stats["recruiter_needed"] += 1
            continue
        review.resolve_items(session, "recruiter_needed", "job", job.id, "recruiter contact added")
        verified = [c for c in contacts if c.email_status == "verified"]
        if not verified:
            c = contacts[0]
            review.open_item(session, "recruiter_unverified", "contact", c.id,
                             f"Verify {c.display_name}'s email before JobPilot writes to them",
                             f"{c.email} is {c.email_status}; confirm it (e.g. on the company site) and run: jobpilot "
                             f"recruiters update --id {c.id} --email-status verified --verification-note …",
                             {"job_id": job.id})
            stats["unverified"] += 1
            continue
        contact = sorted(verified, key=lambda c: (-c.confidence, c.id))[0]
        try:
            d = draft_svc.create_draft(session, draft_svc.DraftCreate(template_key="recruiter_outreach", job_id=job.id,
                                                                      contact_id=contact.id))
        except draft_svc.DraftError as exc:
            review.open_item(session, "outreach_review", "job", job.id, f"Outreach for {job.title} needs you", str(exc))
            stats["review"] += 1
            continue
        audit.record(session, "outreach.auto_drafted", "email_draft", d.id, actor="system", job_id=job.id,
                     contact_id=contact.id)
        warn = contact_svc.domain_warning(contact)
        if d.missing_fields or warn:
            review.open_item(session, "outreach_review", "email_draft", d.id, f"Review the email to {contact.display_name}",
                             "; ".join(([f"missing: {', '.join(d.missing_fields)}"] if d.missing_fields else [])
                                       + ([warn] if warn else [])))
            stats["review"] += 1
            continue
        if not gmail_ready:
            review.open_item(session, "outreach_review", "email_draft", d.id, f"Email to {contact.display_name} is ready",
                             "Gmail is not connected; send it yourself or connect Gmail")
            stats["review"] += 1
            continue
        try:
            draft_svc.approve(session, d)
            audit.record(session, "outreach.auto_approved", "email_draft", d.id, actor="system",
                         basis="standing outreach authorization")
            client = client or gmail.GmailClient(settings)
            if cfg["auto_send"] and sent_today(session) < int(cfg["auto_daily_limit"]):
                gmail.send_via_gmail(session, settings, d, client, "SEND")  # same checks as a manual send
                audit.record(session, SENT_ACTION, "email_draft", d.id, actor="system", to=d.to_address, job_id=job.id)
                stats["sent"] += 1
            else:
                gmail.create_gmail_draft(session, settings, d, client)
                stats["in_gmail_drafts"] += 1
        except (gmail.GmailError, draft_svc.DraftError) as exc:  # nothing is marked sent unless Gmail confirmed it
            review.open_item(session, "outreach_failed", "email_draft", d.id, f"Email to {contact.display_name} not sent",
                             str(exc)[:500])
            stats["failed"] += 1
        session.commit()
    msg = (f"{stats['jobs']} suitable job(s): {stats['sent']} email(s) sent, {stats['in_gmail_drafts']} in Gmail Drafts, "
           f"{stats['recruiter_needed']} need a recruiter, {stats['unverified']} need a verified address, "
           f"{stats['review']} need your review, {stats['already_contacted']} already contacted"
           + (f", {stats['failed']} failed" if stats["failed"] else ""))
    return {"message": msg, **stats}
