"""Resume library: named resumes (per role category) with immutable versions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jobpilot.models.base import Base, TimestampMixin

VERSION_STATUSES = ("draft", "approved", "archived")


class Resume(TimestampMixin, Base):
    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # Role-category id from settings (java_backend, sde1, ...) or "master".
    category: Mapped[str] = mapped_column(String(50), index=True)
    is_master: Mapped[bool] = mapped_column(default=False)
    description: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(default=True)

    versions: Mapped[list[ResumeVersion]] = relationship(
        back_populates="resume", order_by="ResumeVersion.version_no", cascade="all, delete-orphan"
    )

    @property
    def latest_approved(self) -> ResumeVersion | None:
        approved = [v for v in self.versions if v.status == "approved"]
        return approved[-1] if approved else None


class ResumeVersion(Base):
    """An immutable file + its approved content. Changes create a new version."""

    __tablename__ = "resume_versions"
    __table_args__ = (UniqueConstraint("resume_id", "version_no"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    resume_id: Mapped[int] = mapped_column(ForeignKey("resumes.id", ondelete="CASCADE"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    source: Mapped[str] = mapped_column(String(30))  # imported | generated
    source_note: Mapped[str | None] = mapped_column(Text)  # original filename, generator inputs...
    parent_version_id: Mapped[int | None] = mapped_column(ForeignKey("resume_versions.id"))
    change_summary: Mapped[str | None] = mapped_column(Text)
    file_path: Mapped[str | None] = mapped_column(String(500))  # relative to files_dir
    original_filename: Mapped[str | None] = mapped_column(String(300))
    mime_type: Mapped[str | None] = mapped_column(String(100))
    file_size: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    page_count: Mapped[int | None] = mapped_column(Integer)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    # For generated versions: the exact profile fact ids used.
    content: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    validation: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)

    resume: Mapped[Resume] = relationship(back_populates="versions")

    @property
    def label(self) -> str:
        return f"{self.resume.name} v{self.version_no}" if self.resume else f"v{self.version_no}"
