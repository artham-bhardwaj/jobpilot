"""Applications, their lifecycle events and evidence, review queue items and browser runs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jobpilot.models.base import Base, TimestampMixin, utcnow
from jobpilot.models.jobs import Job
from jobpilot.models.resume import ResumeVersion

APPLICATION_STATUSES = ("discovered", "shortlisted", "preparing", "ready_for_review", "applied",
                        "assessment", "interview", "rejected", "withdrawn", "offer")


class Application(TimestampMixin, Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="RESTRICT"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="discovered", index=True)
    # The exact resume version used. Set when preparing; frozen once applied.
    resume_version_id: Mapped[int | None] = mapped_column(ForeignKey("resume_versions.id", ondelete="RESTRICT"))
    resume_selection_reason: Mapped[str | None] = mapped_column(Text)
    apply_url: Mapped[str | None] = mapped_column(String(1000))
    checklist: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text)
    duplicate_override_note: Mapped[str | None] = mapped_column(Text)
    submission_approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    confirmation_method: Mapped[str | None] = mapped_column(String(30))
    confirmation_note: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Snapshot taken when preparation is (re)generated: match, warnings, facts, missing info, URL.
    preparation: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    prepared_at: Mapped[datetime | None] = mapped_column(DateTime)

    job: Mapped[Job] = relationship()
    resume_version: Mapped[ResumeVersion | None] = relationship()
    events: Mapped[list[ApplicationEvent]] = relationship(
        back_populates="application", cascade="all, delete-orphan", order_by="ApplicationEvent.ts")
    evidence: Mapped[list[ApplicationEvidence]] = relationship(
        back_populates="application", cascade="all, delete-orphan", order_by="ApplicationEvidence.created_at")
    browser_runs: Mapped[list[BrowserRun]] = relationship(
        back_populates="application", cascade="all, delete-orphan", order_by="BrowserRun.started_at.desc()")


class ApplicationEvent(Base):
    __tablename__ = "application_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    from_status: Mapped[str | None] = mapped_column(String(30))
    to_status: Mapped[str] = mapped_column(String(30))
    note: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(20), default="user")

    application: Mapped[Application] = relationship(back_populates="events")


class ApplicationEvidence(Base):
    __tablename__ = "application_evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    # confirmation_email | confirmation_page | portal_status | screenshot | file | note
    kind: Mapped[str] = mapped_column(String(30))
    text: Mapped[str | None] = mapped_column(Text)
    file_path: Mapped[str | None] = mapped_column(String(500))
    original_filename: Mapped[str | None] = mapped_column(String(300))
    sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    application: Mapped[Application] = relationship(back_populates="evidence")


class ReviewItem(Base):
    """Something that needs a human decision."""

    __tablename__ = "review_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    # job_review | resume_missing | submission_approval | browser_paused | contact_check | interview_detected
    kind: Mapped[str] = mapped_column(String(30), index=True)
    entity_type: Mapped[str] = mapped_column(String(30))
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(300))
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)  # open|resolved|dismissed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolution: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class BrowserRun(Base):
    __tablename__ = "browser_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    # running | paused | filled | unsupported | failed | closed
    status: Mapped[str] = mapped_column(String(20), default="running")
    site: Mapped[str | None] = mapped_column(String(50))
    filled_fields: Mapped[list[Any] | None] = mapped_column(JSON)
    pause_reasons: Mapped[list[Any] | None] = mapped_column(JSON)
    unknown_fields: Mapped[list[Any] | None] = mapped_column(JSON)
    screenshot_path: Mapped[str | None] = mapped_column(String(500))
    message: Mapped[str | None] = mapped_column(Text)

    application: Mapped[Application] = relationship(back_populates="browser_runs")


ANSWER_CATEGORIES = ("general", "experience", "motivation", "logistics", "sensitive")


class AnswerBankEntry(TimestampMixin, Base):
    """A reusable answer you wrote. Sensitive answers are never suggested or filled automatically."""

    __tablename__ = "answer_bank"

    id: Mapped[int] = mapped_column(primary_key=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(20), default="general")
    # Role categories this answer applies to (empty = any).
    role_categories: Mapped[list[Any] | None] = mapped_column(JSON)
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)


class ApplicationQuestion(TimestampMixin, Base):
    """A question asked by one application form, and your answer for this application."""

    __tablename__ = "application_questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual|browser
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    answer: Mapped[str | None] = mapped_column(Text)
    answer_source: Mapped[str | None] = mapped_column(String(40))  # manual|bank:<id>
    status: Mapped[str] = mapped_column(String(20), default="unanswered")  # unanswered|approved
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)

    application: Mapped[Application] = relationship()
