"""Contacts (recruiters, hiring managers, referrers), do-not-contact list, email templates and drafts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jobpilot.models.base import Base, TimestampMixin, utcnow
from jobpilot.models.jobs import Company, Job
from jobpilot.models.resume import ResumeVersion

ROLE_TYPES = ("recruiter", "hiring_manager", "referral", "engineer", "other")
EMAIL_STATUSES = ("verified", "unverified", "guessed")
SOURCE_TYPES = ("job_posting", "company_website", "public_profile", "email_thread", "referral", "manual")


class Contact(TimestampMixin, Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), index=True)
    name: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str | None] = mapped_column(String(200))
    role_type: Mapped[str] = mapped_column(String(30), default="recruiter")
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    # verified: confirmed working (e.g. they replied) · unverified: published/obtained but not confirmed
    # guessed: inferred — NEVER treated as verified, never auto-used for sending
    email_status: Mapped[str | None] = mapped_column(String(20))
    linkedin_url: Mapped[str | None] = mapped_column(String(500))
    phone: Mapped[str | None] = mapped_column(String(50))
    source_type: Mapped[str] = mapped_column(String(30), default="manual")
    source_url: Mapped[str | None] = mapped_column(String(1000))
    source_note: Mapped[str | None] = mapped_column(Text)
    # Only for referral contacts: how you actually know them (entered by you).
    relationship_note: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    verification_note: Mapped[str | None] = mapped_column(Text)
    do_not_contact: Mapped[bool] = mapped_column(default=False)
    dnc_reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    company: Mapped[Company | None] = relationship()
    job: Mapped[Job | None] = relationship()
    interactions: Mapped[list[ContactInteraction]] = relationship(
        back_populates="contact", cascade="all, delete-orphan", order_by="ContactInteraction.ts.desc()")

    @property
    def display_name(self) -> str:
        return self.name or self.email or f"Contact #{self.id}"


class ContactInteraction(Base):
    __tablename__ = "contact_interactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    channel: Mapped[str] = mapped_column(String(20))  # email|linkedin|phone|in_person|other
    direction: Mapped[str] = mapped_column(String(10))  # outbound|inbound
    summary: Mapped[str] = mapped_column(Text)
    draft_id: Mapped[int | None] = mapped_column(ForeignKey("email_drafts.id", ondelete="SET NULL"))

    contact: Mapped[Contact] = relationship(back_populates="interactions")


class DoNotContact(Base):
    __tablename__ = "do_not_contact"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # email|domain|company
    value: Mapped[str] = mapped_column(String(320), index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EmailTemplate(TimestampMixin, Base):
    __tablename__ = "email_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30))  # recruiter_outreach|referral_request|hiring_manager|follow_up|thank_you|status_inquiry
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    builtin: Mapped[bool] = mapped_column(default=False)
    active: Mapped[bool] = mapped_column(default=True)


class EmailDraft(TimestampMixin, Base):
    __tablename__ = "email_drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    template_id: Mapped[int | None] = mapped_column(ForeignKey("email_templates.id", ondelete="SET NULL"))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"), index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), index=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id", ondelete="SET NULL"), index=True)
    resume_version_id: Mapped[int | None] = mapped_column(ForeignKey("resume_versions.id", ondelete="SET NULL"))
    # A validated resume tailored to this draft's job, attached instead of resume_version (email applications).
    tailored_resume_id: Mapped[int | None] = mapped_column(ForeignKey("tailored_resumes.id", ondelete="SET NULL"))
    to_address: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)  # draft|approved|sent|cancelled
    missing_fields: Mapped[list[Any] | None] = mapped_column(JSON)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_via: Mapped[str | None] = mapped_column(String(20))  # manual|gmail
    gmail_draft_id: Mapped[str | None] = mapped_column(String(100))
    gmail_message_id: Mapped[str | None] = mapped_column(String(100))
    gmail_thread_id: Mapped[str | None] = mapped_column(String(100))
    # Gmail synchronisation state, separate from the local ``status``. Only set from Gmail API responses:
    # None (local only) | pending (create request outcome unknown) | created | failed | outdated (local text changed)
    # | sending (send request in flight) | send_unknown (send outcome unknown: never retried blindly) | sent
    gmail_status: Mapped[str | None] = mapped_column(String(20))
    gmail_error: Mapped[str | None] = mapped_column(Text)
    gmail_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    gmail_content_hash: Mapped[str | None] = mapped_column(String(64))  # what the Gmail draft was built from
    gmail_attachment: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # filename/size/sha256 actually attached
    gmail_send_attempted_at: Mapped[datetime | None] = mapped_column(DateTime)
    gmail_stale_draft_id: Mapped[str | None] = mapped_column(String(100))  # Gmail draft holding outdated text
    follow_up_due_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    follow_up_of_id: Mapped[int | None] = mapped_column(ForeignKey("email_drafts.id", ondelete="SET NULL"))
    reply_received_at: Mapped[datetime | None] = mapped_column(DateTime)
    suppressed_reason: Mapped[str | None] = mapped_column(Text)

    contact: Mapped[Contact | None] = relationship(foreign_keys=[contact_id])
    job: Mapped[Job | None] = relationship()
    template: Mapped[EmailTemplate | None] = relationship()
    resume_version: Mapped[ResumeVersion | None] = relationship()


class DraftRevision(Base):
    """Previous text of a draft, kept on every edit."""

    __tablename__ = "draft_revisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("email_drafts.id", ondelete="CASCADE"), index=True)
    revision_no: Mapped[int] = mapped_column(Integer)
    to_address: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    status_before: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ContactJob(Base):
    """Association between a contact and a job (and therefore its application)."""

    __tablename__ = "contact_jobs"
    __table_args__ = (UniqueConstraint("contact_id", "job_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    relation: Mapped[str] = mapped_column(String(30), default="recruiter")  # recruiter|hiring_manager|referral|other
    source: Mapped[str | None] = mapped_column(String(200))  # how the association was established
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    contact: Mapped[Contact] = relationship()
    job: Mapped[Job] = relationship()
