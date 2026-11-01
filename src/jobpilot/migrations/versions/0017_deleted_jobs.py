"""deleted_jobs: jobs you deleted, so sources don't add them again

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-28
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0017'
down_revision: str | None = '0016'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'deleted_jobs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('fingerprint', sa.String(length=64), nullable=True),
        sa.Column('apply_url_canonical', sa.String(length=1000), nullable=True),
        sa.Column('source_id', sa.Integer(), nullable=True),
        sa.Column('external_id', sa.String(length=200), nullable=True),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('company_name', sa.String(length=200), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('deleted_jobs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_deleted_jobs_fingerprint'), ['fingerprint'], unique=False)
        batch_op.create_index(batch_op.f('ix_deleted_jobs_apply_url_canonical'), ['apply_url_canonical'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('deleted_jobs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_deleted_jobs_apply_url_canonical'))
        batch_op.drop_index(batch_op.f('ix_deleted_jobs_fingerprint'))
    op.drop_table('deleted_jobs')
