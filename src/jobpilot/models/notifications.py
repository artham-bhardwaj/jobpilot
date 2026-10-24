"""Outbound notifications (Telegram), their dedupe keys, processed inbound updates, and small integration state."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from jobpilot.models.base import Base, TimestampMixin, utcnow

# pending → sent | failed (gave up after max attempts) | skipped (channel disabled when it was due)
NOTIFICATION_STATUSES = ("pending", "sent", "failed", "skipped")


class Notification(TimestampMixin, Base):
    """One message to deliver. ``status=sent`` only after Telegram returned the message id."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    channel: Mapped[str] = mapped_column(String(20), default="telegram")
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    external_ids: Mapped[list[Any] | None] = mapped_column(JSON)  # Telegram message ids (one per chunk)
    refs: Mapped[list[Any] | None] = mapped_column(JSON)  # event keys covered (grouped messages)
    reply_markup: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # Telegram inline buttons, if any


class NotificationKey(Base):
    """An event that has been notified (or deliberately skipped): never announced twice."""

    __tablename__ = "notification_keys"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    notification_id: Mapped[int | None] = mapped_column(ForeignKey("notifications.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TelegramUpdate(Base):
    """Each processed inbound update, so none is handled twice (and unauthorised attempts are visible)."""

    __tablename__ = "telegram_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    chat_id: Mapped[str | None] = mapped_column(String(40))
    authorized: Mapped[bool] = mapped_column(default=False)
    command: Mapped[str | None] = mapped_column(String(40))
    result: Mapped[str | None] = mapped_column(String(40))  # answered | ignored | rejected | error
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class IntegrationState(Base):
    """Small persistent state: Telegram polling offset, notification baseline, daily-summary markers."""

    __tablename__ = "integration_state"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
