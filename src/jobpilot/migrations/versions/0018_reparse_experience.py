"""re-parse experience requirements with the corrected rule (highest required figure, not the lowest)

"7+ years in software engineering … 2+ years with CI/CD" was stored as 2 years, so such jobs passed as fresher /
SDE-1. This recomputes the stored years, seniority and fresher / SDE-1 fit for every job from its description, and
marks the changed ones unscored, so the next collection (or Re-score all) re-scores them and the cleanup re-checks
them. Nothing else changes.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-29
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0018'
down_revision: str | None = '0017'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    from jobpilot.services.job_parser import level_from, parse_experience
    from jobpilot.services.levels import classify

    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, title, description_text, experience_min_years, experience_max_years, "
                                "experience_level, level_fit FROM jobs")).fetchall()
    for job_id, title, text, old_min, old_max, old_level, old_fit in rows:
        lo, hi, _ = parse_experience(text or "")
        level = level_from(title or "", lo)
        fit = classify(title, lo)
        if (lo, hi, level, fit) != (old_min, old_max, old_level, old_fit):
            conn.execute(sa.text("UPDATE jobs SET experience_min_years = :lo, experience_max_years = :hi, "
                                 "experience_level = :lvl, level_fit = :fit, matched_at = NULL WHERE id = :id"),
                         {"lo": lo, "hi": hi, "lvl": level, "fit": fit, "id": job_id})


def downgrade() -> None:
    pass  # data only: the previous values were the incorrect ones
