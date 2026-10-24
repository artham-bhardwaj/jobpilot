"""Historical only: batches from the removed auto-apply feature (2026-10-23). Kept so past records stay readable;
nothing creates or acts on them any more."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from jobpilot.models.base import Base, utcnow

# pending → approved | skipped | expired
BATCH_STATUSES = ("pending", "approved", "skipped", "expired")


class SubmissionBatch(Base):
    __tablename__ = "submission_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Random value embedded in the Telegram buttons: a callback must carry it, so old or forged buttons do nothing.
    nonce: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # [{"application_id": 1, "job_id": 2, "resume_version_id": 3, "title": "...", "company": "..."}]
    items: Mapped[list[Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    decided_via: Mapped[str | None] = mapped_column(String(40))
    note: Mapped[str | None] = mapped_column(Text)
