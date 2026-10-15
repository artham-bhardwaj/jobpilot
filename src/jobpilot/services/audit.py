"""Audit logging helpers."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.models import AuditLog

log = logging.getLogger("jobpilot.audit")


def record(
    session: Session,
    action: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
    actor: str = "user",
    **details: Any,
) -> AuditLog:
    entry = AuditLog(
        action=action, entity_type=entity_type, entity_id=entity_id, actor=actor, details=details or None
    )
    session.add(entry)
    log.info(action, extra={"entity_type": entity_type, "entity_id": entity_id, "actor": actor})
    return entry


def recent(session: Session, limit: int = 100, entity_type: str | None = None, entity_id: int | None = None):
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if entity_type:
        q = q.where(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        q = q.where(AuditLog.entity_id == entity_id)
    return list(session.scalars(q))
