"""Deleting jobs for good.

``delete`` removes the job and everything that only exists for it (sightings, tailored resumes and their PDFs,
match feedback, contact links, open review items). Records that stand on their own keep their history with the job
link cleared (email drafts, contact history). A job you have an application for can't be deleted: the application
is your record of applying (dismiss the job instead).

So that a source doesn't simply add it again, a ``DeletedJob`` remembers its fingerprint, canonical apply URL and
source id; collection and job-alert import skip postings that match. Adding the same job yourself (manual entry or
URL) clears that memory.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete as sql_delete
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from jobpilot.config import Settings
from jobpilot.models import Application, DeletedJob, Job, ReviewItem, TailoredResume

log = logging.getLogger(__name__)


class DeleteError(ValueError):
    pass


def blocker(session: Session, job: Job) -> str | None:
    """Why ``job`` can't be deleted, or None."""
    app = session.scalar(select(Application).where(Application.job_id == job.id))
    if app is not None:
        return (f"you have an application for it (#{app.id}{', applied' if app.applied_at else ''}); "
                "dismiss it instead to keep your record")
    return None


def delete(session: Session, settings: Settings, job: Job) -> str:
    """Delete ``job``; returns its "title — company" label. Raises DeleteError when it can't be deleted.
    The caller commits."""
    from jobpilot.services import audit

    why = blocker(session, job)
    if why:
        raise DeleteError(f"Can't delete this job: {why}")
    label = f"{job.title} — {job.company_name or 'unknown company'}"
    session.add(DeletedJob(fingerprint=job.fingerprint, apply_url_canonical=job.apply_url_canonical,
                           source_id=job.source_id, external_id=job.external_id, title=job.title[:300],
                           company_name=job.company_name))
    pdfs = [tr.pdf_path for tr in session.scalars(select(TailoredResume).where(TailoredResume.job_id == job.id))
            if tr.pdf_path]
    session.execute(sql_delete(ReviewItem).where(ReviewItem.entity_type == "job", ReviewItem.entity_id == job.id,
                                                 ReviewItem.status == "open"))
    job_id = job.id
    session.expunge(job)
    # The database cascades (foreign keys are on): sightings, tailored resumes, feedback, contact links go with it;
    # drafts and contact events keep their history with job_id cleared.
    session.execute(sql_delete(Job).where(Job.id == job_id))
    audit.record(session, "job.deleted", "job", job_id, label=label[:200])
    for rel in pdfs:  # generated files for this job only; never anything outside the tailored folder
        path = (settings.files_dir / rel).resolve()
        if settings.files_dir.resolve() in path.parents and path.is_file():
            try:
                path.unlink()
            except OSError as exc:
                log.warning("could not remove %s: %s", path, exc)
    return label


def is_deleted(session: Session, source, data) -> bool:
    """Was this posting (a JobData) deleted by you before?"""
    from jobpilot.services.jobs import _canonicals, fingerprint

    conds = [DeletedJob.fingerprint == fingerprint(data.company_name, data.title, data.location)]
    canon = _canonicals(data)
    if canon:
        conds.append(DeletedJob.apply_url_canonical.in_(canon))
    if source is not None and data.external_id:
        conds.append((DeletedJob.source_id == source.id) & (DeletedJob.external_id == data.external_id))
    return session.scalar(select(DeletedJob.id).where(or_(*conds)).limit(1)) is not None


def forget(session: Session, job: Job) -> None:
    """You added this job yourself: stop treating it as deleted."""
    conds = [DeletedJob.fingerprint == job.fingerprint]
    if job.apply_url_canonical:
        conds.append(DeletedJob.apply_url_canonical == job.apply_url_canonical)
    session.execute(sql_delete(DeletedJob).where(or_(*conds)))
