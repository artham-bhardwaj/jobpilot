"""profile: certifications, achievements, project kinds, role types, preferences

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-16
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('certifications',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('issuer', sa.String(length=200), nullable=True),
    sa.Column('issued_on', sa.Date(), nullable=True),
    sa.Column('expires_on', sa.Date(), nullable=True),
    sa.Column('credential_url', sa.String(length=500), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('source', sa.String(length=200), nullable=True),
    sa.Column('confirmed_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_certifications'))
    )
    op.create_table('achievements',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('context', sa.String(length=20), nullable=False),
    sa.Column('achieved_on', sa.Date(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('source', sa.String(length=200), nullable=True),
    sa.Column('confirmed_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_achievements'))
    )
    with op.batch_alter_table('candidate_profile', schema=None) as batch_op:
        batch_op.add_column(sa.Column('preferred_titles', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('job_search_priorities', sa.Text(), nullable=True))
    with op.batch_alter_table('employment', schema=None) as batch_op:
        batch_op.add_column(sa.Column('role_type', sa.String(length=30), nullable=True))
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.add_column(sa.Column('kind', sa.String(length=20), nullable=False, server_default='personal'))


def downgrade() -> None:
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_column('kind')
    with op.batch_alter_table('employment', schema=None) as batch_op:
        batch_op.drop_column('role_type')
    with op.batch_alter_table('candidate_profile', schema=None) as batch_op:
        batch_op.drop_column('job_search_priorities')
        batch_op.drop_column('preferred_titles')
    op.drop_table('achievements')
    op.drop_table('certifications')
