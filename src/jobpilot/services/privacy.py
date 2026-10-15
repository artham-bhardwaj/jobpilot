"""Personal-data export and deletion."""

from __future__ import annotations

import shutil
from datetime import date, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from jobpilot import __version__
from jobpilot.config import Settings
from jobpilot.models import (
    Base,
    CandidateProfile,
    Contact,
    ContactInteraction,
    Education,
    EmailDraft,
    Employment,
    LearningItem,
    ProfileLink,
    Project,
    Skill,
    utcnow,
)
from jobpilot.services import audit


def _jsonable(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def export_all(session: Session) -> dict[str, Any]:
    """Every row of every table, as plain JSON-serialisable data (files are referenced by path)."""
    out: dict[str, Any] = {"exported_at": utcnow().isoformat(), "app_version": __version__, "tables": {}}
    for table in Base.metadata.sorted_tables:
        rows = session.execute(select(table)).mappings().all()
        out["tables"][table.name] = [{k: _jsonable(v) for k, v in r.items()} for r in rows]
    audit.record(session, "privacy.exported")
    return out


SCOPES = {
    "profile": [ProfileLink, Education, Employment, Project, Skill, LearningItem, CandidateProfile],
    "contacts": [ContactInteraction, Contact],
    "outreach": [EmailDraft],
}


def delete_data(session: Session, settings: Settings, scope: str, include_backups: bool = False) -> dict[str, Any]:
    """Delete personal data. ``scope``: profile | contacts | outreach | everything.

    ``everything`` removes all records and stored files (resumes, evidence, screenshots),
    the browser profile and the Gmail token. Backups are removed only if requested.
    """
    counts: dict[str, int] = {}
    if scope in SCOPES:
        if scope == "contacts":
            session.execute(EmailDraft.__table__.update().values(contact_id=None))
        for model in SCOPES[scope]:
            counts[model.__tablename__] = session.execute(delete(model)).rowcount or 0
    elif scope == "everything":
        for table in reversed(Base.metadata.sorted_tables):
            counts[table.name] = session.execute(table.delete()).rowcount or 0
        for path in (settings.files_dir, settings.browser_profile_dir):
            if path.exists():
                shutil.rmtree(path)
        settings.files_dir.mkdir(parents=True, exist_ok=True)
        settings.gmail_token_file.unlink(missing_ok=True)
        from jobpilot.services.drafts import ensure_builtin_templates

        ensure_builtin_templates(session)  # built-in templates are reference data, not personal data
    else:
        raise ValueError("Unknown scope")
    if include_backups and settings.backup_dir.exists():
        for p in settings.backup_dir.glob("jobpilot-*.zip"):
            p.unlink()
        counts["backup_files"] = -1
    session.flush()
    audit.record(session, "privacy.deleted", scope=scope, include_backups=include_backups,
                 tables=sorted(k for k, v in counts.items() if v))
    return counts

