"""profile hub: claim provenance and import records

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-17
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('profile_imports',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('source_kind', sa.String(length=20), nullable=False),
    sa.Column('source_filename', sa.String(length=300), nullable=True),
    sa.Column('source_sha256', sa.String(length=64), nullable=True),
    sa.Column('resume_version_id', sa.Integer(), nullable=True),
    sa.Column('extraction_method', sa.String(length=30), nullable=True),
    sa.Column('parser_version', sa.String(length=20), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('counts', sa.JSON(), nullable=True),
    sa.Column('report', sa.JSON(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('backup_path', sa.String(length=500), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['resume_version_id'], ['resume_versions.id'],
                            name=op.f('fk_profile_imports_resume_version_id_resume_versions'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_profile_imports'))
    )
    with op.batch_alter_table('profile_imports', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_profile_imports_source_sha256'), ['source_sha256'], unique=False)

    op.create_table('profile_claims',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('source_ref', sa.String(length=300), nullable=False),
    sa.Column('claim_key', sa.String(length=200), nullable=False),
    sa.Column('import_id', sa.Integer(), nullable=True),
    sa.Column('category', sa.String(length=30), nullable=False),
    sa.Column('entity_type', sa.String(length=30), nullable=True),
    sa.Column('entity_id', sa.Integer(), nullable=True),
    sa.Column('field', sa.String(length=50), nullable=True),
    sa.Column('value', sa.Text(), nullable=True),
    sa.Column('original_text', sa.Text(), nullable=True),
    sa.Column('existing_value', sa.Text(), nullable=True),
    sa.Column('source_kind', sa.String(length=20), nullable=False),
    sa.Column('source_label', sa.String(length=300), nullable=True),
    sa.Column('source_location', sa.String(length=300), nullable=True),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.Column('outcome', sa.String(length=20), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('related', sa.JSON(), nullable=True),
    sa.Column('extracted_at', sa.DateTime(), nullable=True),
    sa.Column('resolved_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['import_id'], ['profile_imports.id'],
                            name=op.f('fk_profile_claims_import_id_profile_imports'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_profile_claims')),
    sa.UniqueConstraint('source_ref', 'claim_key', name=op.f('uq_profile_claims_source_ref'))
    )
    with op.batch_alter_table('profile_claims', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_profile_claims_category'), ['category'], unique=False)
        batch_op.create_index(batch_op.f('ix_profile_claims_entity_id'), ['entity_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_profile_claims_source_ref'), ['source_ref'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('profile_claims', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_profile_claims_source_ref'))
        batch_op.drop_index(batch_op.f('ix_profile_claims_entity_id'))
        batch_op.drop_index(batch_op.f('ix_profile_claims_category'))
    op.drop_table('profile_claims')
    with op.batch_alter_table('profile_imports', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_profile_imports_source_sha256'))
    op.drop_table('profile_imports')
