"""Companies, job sources, jobs and their provenance (sightings)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jobpilot.models.base import Base, TimestampMixin, utcnow

SOURCE_KINDS = ("greenhouse", "lever", "ashby", "jsonld", "manual", "import")


class Company(TimestampMixin, Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    normalized_name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    website: Mapped[str | None] = mapped_column(String(500))
    careers_url: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)


class JobSource(TimestampMixin, Base):
    """A configured place jobs come from (one board, one careers page, imports...)."""

    __tablename__ = "job_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(30))
    # greenhouse: {"board_token": "..."}; lever: {"site": "..."}; jsonld: {"urls": [...]}
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    company_name: Mapped[str | None] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(default=True)
    terms_note: Mapped[str | None] = mapped_column(Text)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_status: Mapped[str | None] = mapped_column(String(20))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_stats: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("job_sources.id", ondelete="SET NULL"), index=True)
    external_id: Mapped[str | None] = mapped_column(String(200))
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    company_name: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(300))
    location: Mapped[str | None] = mapped_column(String(300))
    remote_type: Mapped[str] = mapped_column(String(20), default="unknown")  # remote|hybrid|onsite|unknown
    employment_type: Mapped[str | None] = mapped_column(String(50))
    department: Mapped[str | None] = mapped_column(String(200))
    experience_level: Mapped[str | None] = mapped_column(String(20))  # entry|mid|senior|None
    experience_min_years: Mapped[float | None] = mapped_column(Float)
    experience_max_years: Mapped[float | None] = mapped_column(Float)
    salary_min: Mapped[int | None] = mapped_column(Integer)
    salary_max: Mapped[int | None] = mapped_column(Integer)
    salary_currency: Mapped[str | None] = mapped_column(String(10))
    salary_period: Mapped[str | None] = mapped_column(String(20))
    description_text: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    valid_through: Mapped[datetime | None] = mapped_column(DateTime)
    source_url: Mapped[str | None] = mapped_column(String(1000))
    apply_url: Mapped[str | None] = mapped_column(String(1000))
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)  # active|expired
    expired_at: Mapped[datetime | None] = mapped_column(DateTime)
    repost_of_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))

    # Matching results (recomputed on demand).
    category: Mapped[str | None] = mapped_column(String(50), index=True)
    category_confidence: Mapped[float | None] = mapped_column(Float)
    relevance_score: Mapped[float | None] = mapped_column(Float, index=True)
    eligibility: Mapped[str | None] = mapped_column(String(20), index=True)  # eligible|likely_eligible|ineligible|unknown
    match_details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    matched_at: Mapped[datetime | None] = mapped_column(DateTime)

    # User triage.
    user_status: Mapped[str] = mapped_column(String(20), default="new", index=True)  # new|shortlisted|dismissed
    notes: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    # Milestone 8: provenance, liveness and assessment summary columns.
    description_raw: Mapped[str | None] = mapped_column(Text)  # exactly as received (HTML or text)
    apply_url_canonical: Mapped[str | None] = mapped_column(String(1000), index=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_check_status: Mapped[str | None] = mapped_column(String(60))
    # Consecutive successful snapshots of the job's own source that did not list it.
    miss_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    confidence: Mapped[str | None] = mapped_column(String(10), index=True)  # high|medium|low
    hard_conflict: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), index=True)

    sightings: Mapped[list[JobSighting]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobSighting.seen_at"
    )
    source: Mapped[JobSource | None] = relationship()
    company: Mapped[Company | None] = relationship()


class JobSighting(Base):
    """Provenance: each time/place a job was observed."""

    __tablename__ = "job_sightings"
    __table_args__ = (UniqueConstraint("job_id", "source_id", "external_id", "url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("job_sources.id", ondelete="SET NULL"))
    external_id: Mapped[str | None] = mapped_column(String(200))
    url: Mapped[str | None] = mapped_column(String(1000))
    method: Mapped[str] = mapped_column(String(30))  # api|html|manual|import
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    times_seen: Mapped[int] = mapped_column(Integer, default=1)

    job: Mapped[Job] = relationship(back_populates="sightings")
    source: Mapped[JobSource | None] = relationship()


class SourceRun(Base):
    """History of each collection attempt for a source (including dry runs)."""

    __tablename__ = "source_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("job_sources.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    trigger: Mapped[str] = mapped_column(String(20), default="manual")  # manual|scheduled
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # success | partial | unavailable (network/5xx/429: nothing expired) | failed (config/parse) | anomaly
    status: Mapped[str] = mapped_column(String(20), default="running")
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    new: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    unchanged: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)  # matched a job first seen via another source
    reposts: Mapped[int] = mapped_column(Integer, default=0)
    expired: Mapped[int] = mapped_column(Integer, default=0)
    missing: Mapped[int] = mapped_column(Integer, default=0)  # absent this run but not yet expired
    requests: Mapped[int] = mapped_column(Integer, default=0)
    rate_limited: Mapped[int] = mapped_column(Integer, default=0)
    http_errors: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    warnings: Mapped[list[Any] | None] = mapped_column(JSON)

    source: Mapped[JobSource] = relationship()


class MatchFeedback(Base):
    """Your judgement of a match. Used for calibration reports only; never changes scoring by itself."""

    __tablename__ = "match_feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    verdict: Mapped[str] = mapped_column(String(20))  # useful|irrelevant
    note: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Float)
    eligibility: Mapped[str | None] = mapped_column(String(20))
    weights: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    job: Mapped[Job] = relationship()
