"""Records related to a job, gathered for the job detail page."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from jobpilot.models import Contact, EmailDraft, EmailTemplate, Job
from jobpilot.services import drafts as ds


def for_job(session: Session, job: Job) -> dict:
    conds = [Contact.job_id == job.id]
    if job.company_id:
        conds.append(Contact.company_id == job.company_id)
    contacts = list(session.scalars(select(Contact).where(or_(*conds)).order_by(Contact.confidence.desc())))
    drafts = list(session.scalars(select(EmailDraft).where(EmailDraft.job_id == job.id).order_by(EmailDraft.id.desc())))
    templates = list(session.scalars(select(EmailTemplate).where(EmailTemplate.active.is_(True))))
    posted_emails = ((job.match_details or {}).get("requirements") or {}).get("emails") or []
    known = {c.email.lower() for c in contacts if c.email}
    from jobpilot.services import applications as apps

    application = apps.for_job(session, job.id)
    duplicates = [] if application else apps.find_duplicates(session, job)
    return {"application": application, "duplicates": duplicates, "contacts": contacts, "drafts": drafts, "templates": templates, "kind_labels": ds.KIND_LABELS,
            "new_posting_emails": [e for e in posted_emails if e.lower() not in known]}
