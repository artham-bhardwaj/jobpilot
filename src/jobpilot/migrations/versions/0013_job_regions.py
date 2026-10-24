"""job regions: india / remote / dubai tags for filtering and source coverage

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0013'
down_revision: str | None = '0012'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('regions', sa.String(length=40), nullable=True))
        batch_op.create_index(batch_op.f('ix_jobs_regions'), ['regions'], unique=False)
    # Tag existing jobs from their location, work mode and title (the classifier has no other dependencies).
    from jobpilot.services.regions import tags

    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, location, remote_type, title FROM jobs")).fetchall()
    for job_id, location, remote_type, title in rows:
        conn.execute(sa.text("UPDATE jobs SET regions = :r WHERE id = :id"),
                     {"r": tags(location, remote_type, title), "id": job_id})


def downgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_jobs_regions'))
        batch_op.drop_column('regions')
