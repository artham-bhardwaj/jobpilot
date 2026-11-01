"""agents: email drafts can attach a validated tailored resume (email applications by the Apply agent)

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-24
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0014'
down_revision: str | None = '0013'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('email_drafts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tailored_resume_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_email_drafts_tailored_resume_id', 'tailored_resumes', ['tailored_resume_id'],
                                    ['id'], ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('email_drafts', schema=None) as batch_op:
        batch_op.drop_constraint('fk_email_drafts_tailored_resume_id', type_='foreignkey')
        batch_op.drop_column('tailored_resume_id')
