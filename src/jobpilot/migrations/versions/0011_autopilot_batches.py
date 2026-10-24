"""autopilot: submission batches; inline buttons on notifications

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-18
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0011'
down_revision: str | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('submission_batches',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('nonce', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('items', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('decided_at', sa.DateTime(), nullable=True),
    sa.Column('decided_via', sa.String(length=40), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_submission_batches'))
    )
    with op.batch_alter_table('submission_batches', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_submission_batches_status'), ['status'], unique=False)
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.add_column(sa.Column('reply_markup', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.drop_column('reply_markup')
    with op.batch_alter_table('submission_batches', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_submission_batches_status'))
    op.drop_table('submission_batches')
