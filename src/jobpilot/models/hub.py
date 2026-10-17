"""Personal information hub: provenance for profile facts, and the record of each import.

The hub does not hold a second copy of the profile. Facts live in the profile tables (``candidate_profile``,
``employment``, ``skills``...). A ``ProfileClaim`` records *where a value came from*: the source file and its
hash, the location in it, the original wording, and what the import did with it (created a fact, matched an
existing one, or found a conflict it left for you to resolve).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from jobpilot.models.base import Base, TimestampMixin

# How far a claim has been checked. A resume is a source of claims, not proof: imported values start as
# "resume_claim". The *fact's* own status (confirmed / learning / outdated) takes precedence when shown.
CLAIM_STATES = ("verified", "resume_claim", "unverified", "learning", "outdated", "rejected")
# What the last import did with the claim, or how you resolved it.
CLAIM_OUTCOMES = ("created", "updated", "unchanged", "conflict", "suggestion", "applied", "dismissed")
IMPORT_STATUSES = ("completed", "failed", "dry_run")


class ProfileImport(TimestampMixin, Base):
    __tablename__ = "profile_imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_kind: Mapped[str] = mapped_column(String(20), default="resume")
    source_filename: Mapped[str | None] = mapped_column(String(300))
    source_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    resume_version_id: Mapped[int | None] = mapped_column(ForeignKey("resume_versions.id", ondelete="SET NULL"))
    extraction_method: Mapped[str | None] = mapped_column(String(30))  # pdftotext | pypdf | docx | text
    parser_version: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="completed")
    counts: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    backup_path: Mapped[str | None] = mapped_column(String(500))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class ProfileClaim(TimestampMixin, Base):
    """One value stated by a source, linked to the profile fact it supports (or conflicts with)."""

    __tablename__ = "profile_claims"
    __table_args__ = (UniqueConstraint("source_ref", "claim_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stable identity across re-imports: e.g. source_ref "resume:<sha256>", claim_key "employment[0].bullet[2]".
    source_ref: Mapped[str] = mapped_column(String(300), index=True)
    claim_key: Mapped[str] = mapped_column(String(200))
    import_id: Mapped[int | None] = mapped_column(ForeignKey("profile_imports.id", ondelete="SET NULL"))
    category: Mapped[str] = mapped_column(String(30), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(30))  # profile | links | employment | skills | answers...
    entity_id: Mapped[int | None] = mapped_column(index=True)
    field: Mapped[str | None] = mapped_column(String(50))
    value: Mapped[str | None] = mapped_column(Text)  # normalised value as written to (or proposed for) the fact
    original_text: Mapped[str | None] = mapped_column(Text)  # the source's own wording
    existing_value: Mapped[str | None] = mapped_column(Text)  # the fact's value when it differed (conflicts)
    source_kind: Mapped[str] = mapped_column(String(20))  # resume | repository | database | manual
    source_label: Mapped[str | None] = mapped_column(String(300))  # file name or path
    source_location: Mapped[str | None] = mapped_column(String(300))  # "page 1 · EXPERIENCE · bullet 3"
    state: Mapped[str] = mapped_column(String(20), default="resume_claim")
    outcome: Mapped[str] = mapped_column(String(20), default="created")
    confidence: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    related: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
