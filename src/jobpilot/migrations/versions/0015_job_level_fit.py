"""job level fit: fresher / SDE-1 tag (fit | unstated | no) for the default Jobs view

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-28
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0015'
down_revision: str | None = '0014'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('level_fit', sa.String(length=10), nullable=True))
        batch_op.create_index(batch_op.f('ix_jobs_level_fit'), ['level_fit'], unique=False)
    # Tag existing jobs from their title and stated years (the classifier only needs those two).
    from jobpilot.services.levels import classify

    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, title, experience_min_years FROM jobs")).fetchall()
    for job_id, title, min_years in rows:
        conn.execute(sa.text("UPDATE jobs SET level_fit = :v WHERE id = :id"),
                     {"v": classify(title, min_years), "id": job_id})


def downgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_jobs_level_fit'))
        batch_op.drop_column('level_fit')
