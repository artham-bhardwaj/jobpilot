"""Candidate profile and verified-facts repository.

Every fact row has a ``status``:
- ``confirmed``  – verified by the candidate; usable in resumes, drafts, autofill, eligibility.
- ``unverified`` – recorded (e.g. imported) but not yet confirmed; never used as fact.
Skills additionally support ``learning`` (actively learning; not claimed).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from jobpilot.models.base import Base, TimestampMixin

# "outdated" = no longer accurate or rejected by you: kept for history, never used.
FACT_STATUSES = ("confirmed", "unverified", "outdated")
SKILL_STATUSES = ("confirmed", "learning", "unverified", "outdated")
# What kind of work a role was. Only "software_development" years count as development experience.
ROLE_TYPES = ("software_development", "testing_qa", "data", "devops_infra", "support", "other")
# Projects are never professional experience; this says what kind of non-professional work it was.
PROJECT_KINDS = ("personal", "academic", "learning_exercise", "open_source")


class CandidateProfile(TimestampMixin, Base):
    """Single-row table holding the candidate's core details. All optional."""

    __tablename__ = "candidate_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str | None] = mapped_column(String(200))
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    headline: Mapped[str | None] = mapped_column(String(300))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(50))
    current_location: Mapped[str | None] = mapped_column(String(200))
    summary: Mapped[str | None] = mapped_column(Text)
    total_experience_years: Mapped[float | None] = mapped_column(Float)
    notice_period_days: Mapped[int | None] = mapped_column(Integer)
    salary_expectation_min: Mapped[int | None] = mapped_column(Integer)
    salary_expectation_max: Mapped[int | None] = mapped_column(Integer)
    salary_currency: Mapped[str | None] = mapped_column(String(10))
    work_authorization: Mapped[str | None] = mapped_column(String(300))
    needs_sponsorship: Mapped[bool | None] = mapped_column()
    willing_to_relocate: Mapped[bool | None] = mapped_column()
    acceptable_locations: Mapped[list[Any] | None] = mapped_column(JSON)
    remote_preferences: Mapped[list[Any] | None] = mapped_column(JSON)
    preferred_titles: Mapped[list[Any] | None] = mapped_column(JSON)
    job_search_priorities: Mapped[str | None] = mapped_column(Text)
    # Whether the core details above have been confirmed by the candidate.
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)


class ProfileLink(TimestampMixin, Base):
    __tablename__ = "profile_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="confirmed")


class Education(TimestampMixin, Base):
    __tablename__ = "education"

    id: Mapped[int] = mapped_column(primary_key=True)
    institution: Mapped[str] = mapped_column(String(200))
    degree: Mapped[str | None] = mapped_column(String(200))
    field_of_study: Mapped[str | None] = mapped_column(String(200))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    grade: Mapped[str | None] = mapped_column(String(50))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="unverified")
    source: Mapped[str | None] = mapped_column(String(200))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)


class Employment(TimestampMixin, Base):
    __tablename__ = "employment"

    id: Mapped[int] = mapped_column(primary_key=True)
    company: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(200))
    location: Mapped[str | None] = mapped_column(String(200))
    employment_type: Mapped[str | None] = mapped_column(String(50))  # full_time, internship, contract...
    role_type: Mapped[str | None] = mapped_column(String(30))  # see ROLE_TYPES; None = not recorded
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)  # None = current
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="unverified")
    source: Mapped[str | None] = mapped_column(String(200))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)


class Project(TimestampMixin, Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(500))
    technologies: Mapped[list[Any] | None] = mapped_column(JSON)
    kind: Mapped[str] = mapped_column(String(20), default="personal")  # see PROJECT_KINDS
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="unverified")
    source: Mapped[str | None] = mapped_column(String(200))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)


class Certification(TimestampMixin, Base):
    __tablename__ = "certifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    issuer: Mapped[str | None] = mapped_column(String(200))
    issued_on: Mapped[date | None] = mapped_column(Date)
    expires_on: Mapped[date | None] = mapped_column(Date)
    credential_url: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="unverified")
    source: Mapped[str | None] = mapped_column(String(200))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)


class Achievement(TimestampMixin, Base):
    __tablename__ = "achievements"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    # Where it happened, so professional and non-professional achievements are never mixed up.
    context: Mapped[str] = mapped_column(String(20), default="other")  # work | academic | project | other
    achieved_on: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="unverified")
    source: Mapped[str | None] = mapped_column(String(200))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)


class Skill(TimestampMixin, Base):
    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    normalized_name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    category: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="unverified")
    years: Mapped[float | None] = mapped_column(Float)
    evidence: Mapped[str | None] = mapped_column(Text)  # where/how it was used
    source: Mapped[str | None] = mapped_column(String(200))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
