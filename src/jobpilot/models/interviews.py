"""Interviews, preparation packs and the learning backlog."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jobpilot.models.applications import Application
from jobpilot.models.base import Base, TimestampMixin, utcnow

INTERVIEW_STAGES = ("recruiter_screen", "online_assessment", "technical", "coding", "system_design",
                    "low_level_design", "hiring_manager", "behavioral", "hr", "onsite", "other")
INTERVIEW_FORMATS = ("phone", "video", "onsite", "take_home", "online_test", "other")
LEARNING_STATUSES = ("backlog", "in_progress", "practicing", "ready_for_confirmation", "done", "dropped")


class Interview(TimestampMixin, Base):
    __tablename__ = "interviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    round_no: Mapped[int | None] = mapped_column(Integer)
    round_name: Mapped[str | None] = mapped_column(String(200))
    stage: Mapped[str] = mapped_column(String(30), default="other")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)  # local time as entered
    timezone: Mapped[str | None] = mapped_column(String(50))
    duration_minutes: Mapped[int | None] = mapped_column(Integer)
    format: Mapped[str] = mapped_column(String(20), default="video")
    location_or_link: Mapped[str | None] = mapped_column(String(1000))
    interviewers: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="scheduled")  # scheduled|completed|cancelled|rescheduled|no_show
    outcome: Mapped[str] = mapped_column(String(20), default="pending")  # pending|passed|failed|unknown
    feedback: Mapped[str | None] = mapped_column(Text)
    follow_up_actions: Mapped[str | None] = mapped_column(Text)
    follow_up_done: Mapped[bool] = mapped_column(default=False)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual|email
    source_ref: Mapped[str | None] = mapped_column(String(200))

    application: Mapped[Application] = relationship()
    prep_packs: Mapped[list[PrepPack]] = relationship(back_populates="interview", cascade="all, delete-orphan",
                                                      order_by="PrepPack.generated_at.desc()")


class PrepPack(Base):
    __tablename__ = "prep_packs"

    id: Mapped[int] = mapped_column(primary_key=True)
    interview_id: Mapped[int] = mapped_column(ForeignKey("interviews.id", ondelete="CASCADE"), index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    inputs: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    interview: Mapped[Interview] = relationship(back_populates="prep_packs")


class LearningItem(TimestampMixin, Base):
    __tablename__ = "learning_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    skill_name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    priority: Mapped[float | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(30), default="backlog")
    progress_pct: Mapped[int] = mapped_column(Integer, default=0)
    target_date: Mapped[date | None] = mapped_column(Date)
    resources: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    evidence_of_mastery: Mapped[str | None] = mapped_column(Text)
