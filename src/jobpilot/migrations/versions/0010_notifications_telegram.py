"""notifications (Telegram): messages, dedupe keys, processed updates, integration state

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-17
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0010'
down_revision: str | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('notifications',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=40), nullable=False),
    sa.Column('channel', sa.String(length=20), nullable=False),
    sa.Column('dedupe_key', sa.String(length=200), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
    sa.Column('sent_at', sa.DateTime(), nullable=True),
    sa.Column('external_ids', sa.JSON(), nullable=True),
    sa.Column('refs', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_notifications')),
    sa.UniqueConstraint('dedupe_key', name=op.f('uq_notifications_dedupe_key'))
    )
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_notifications_kind'), ['kind'], unique=False)
        batch_op.create_index(batch_op.f('ix_notifications_status'), ['status'], unique=False)
    op.create_table('notification_keys',
    sa.Column('key', sa.String(length=200), nullable=False),
    sa.Column('notification_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['notification_id'], ['notifications.id'],
                            name=op.f('fk_notification_keys_notification_id_notifications'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_notification_keys'))
    )
    op.create_table('telegram_updates',
    sa.Column('update_id', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('chat_id', sa.String(length=40), nullable=True),
    sa.Column('authorized', sa.Boolean(), nullable=False),
    sa.Column('command', sa.String(length=40), nullable=True),
    sa.Column('result', sa.String(length=40), nullable=True),
    sa.Column('processed_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('update_id', name=op.f('pk_telegram_updates'))
    )
    op.create_table('integration_state',
    sa.Column('key', sa.String(length=100), nullable=False),
    sa.Column('value', sa.JSON(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_integration_state'))
    )


def downgrade() -> None:
    op.drop_table('integration_state')
    op.drop_table('telegram_updates')
    op.drop_table('notification_keys')
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_notifications_status'))
        batch_op.drop_index(batch_op.f('ix_notifications_kind'))
    op.drop_table('notifications')
