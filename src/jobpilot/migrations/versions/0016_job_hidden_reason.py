"""job hidden_reason: why the relevance cleanup hid a job (reversible)

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-28
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0016'
down_revision: str | None = '0015'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('hidden_reason', sa.String(length=80), nullable=True))
        batch_op.create_index(batch_op.f('ix_jobs_hidden_reason'), ['hidden_reason'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_jobs_hidden_reason'))
        batch_op.drop_column('hidden_reason')
