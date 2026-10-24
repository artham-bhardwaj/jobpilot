"""email drafts: Gmail synchronisation state

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-17
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0009'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('email_drafts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('gmail_status', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('gmail_error', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('gmail_synced_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('gmail_content_hash', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('gmail_attachment', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('gmail_send_attempted_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('gmail_stale_draft_id', sa.String(length=100), nullable=True))
    # Existing rows that already hold a Gmail id came from a confirmed API response.
    op.execute("UPDATE email_drafts SET gmail_status = 'sent' WHERE sent_via = 'gmail' AND gmail_message_id IS NOT NULL")
    op.execute("UPDATE email_drafts SET gmail_status = 'created' WHERE gmail_status IS NULL AND gmail_draft_id IS NOT NULL")


def downgrade() -> None:
    with op.batch_alter_table('email_drafts', schema=None) as batch_op:
        for col in ('gmail_stale_draft_id', 'gmail_send_attempted_at', 'gmail_attachment', 'gmail_content_hash',
                    'gmail_synced_at', 'gmail_error', 'gmail_status'):
            batch_op.drop_column(col)
