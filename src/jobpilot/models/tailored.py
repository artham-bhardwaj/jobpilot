"""Job-specific resumes generated from the approved Master resume's LaTeX source (see services/tailored.py).

Kept apart from the resume library: a tailored resume is a re-ordering of approved content for one job, never a new
approved version, and it can only be attached to an application for that same job."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jobpilot.models.base import Base, utcnow

TAILORED_STATUSES = ("pending", "valid", "failed", "superseded", "outdated")


class TailoredResume(Base):
    __tablename__ = "tailored_resumes"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    company_name: Mapped[str | None] = mapped_column(String(200))  # at generation time
    job_title: Mapped[str | None] = mapped_column(String(300))
    job_url: Mapped[str | None] = mapped_column(String(1000))
    source_version_id: Mapped[int | None] = mapped_column(ForeignKey("resume_versions.id", ondelete="SET NULL"))
    template_sha256: Mapped[str | None] = mapped_column(String(64))
    facts_sha256: Mapped[str | None] = mapped_column(String(64))  # the confirmed profile facts used
    job_sha256: Mapped[str | None] = mapped_column(String(64))  # job description + extracted skills
    inputs_sha256: Mapped[str] = mapped_column(String(64), index=True)  # everything above: same → reuse
    content_sha256: Mapped[str | None] = mapped_column(String(64))  # the rendered LaTeX source
    pdf_path: Mapped[str | None] = mapped_column(String(500))  # relative to files_dir
    pdf_sha256: Mapped[str | None] = mapped_column(String(64))
    page_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    tailoring_status: Mapped[str | None] = mapped_column(String(20))  # ok | failed
    compile_status: Mapped[str | None] = mapped_column(String(20))  # ok | failed | unavailable | skipped
    validation_status: Mapped[str | None] = mapped_column(String(20))  # passed | failed
    decisions: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # what was reordered and why; fact mapping
    validation: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    attached_application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id", ondelete="SET NULL"))
    attached_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime)

    job = relationship("Job")
    source_version = relationship("ResumeVersion")
