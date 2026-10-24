"""tailored resumes: job-specific PDFs generated from the approved Master resume's LaTeX source

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-18
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0012'
down_revision: str | None = '0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('tailored_resumes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('job_id', sa.Integer(), nullable=False),
    sa.Column('company_name', sa.String(length=200), nullable=True),
    sa.Column('job_title', sa.String(length=300), nullable=True),
    sa.Column('job_url', sa.String(length=1000), nullable=True),
    sa.Column('source_version_id', sa.Integer(), nullable=True),
    sa.Column('template_sha256', sa.String(length=64), nullable=True),
    sa.Column('facts_sha256', sa.String(length=64), nullable=True),
    sa.Column('job_sha256', sa.String(length=64), nullable=True),
    sa.Column('inputs_sha256', sa.String(length=64), nullable=False),
    sa.Column('content_sha256', sa.String(length=64), nullable=True),
    sa.Column('pdf_path', sa.String(length=500), nullable=True),
    sa.Column('pdf_sha256', sa.String(length=64), nullable=True),
    sa.Column('page_count', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('tailoring_status', sa.String(length=20), nullable=True),
    sa.Column('compile_status', sa.String(length=20), nullable=True),
    sa.Column('validation_status', sa.String(length=20), nullable=True),
    sa.Column('decisions', sa.JSON(), nullable=True),
    sa.Column('validation', sa.JSON(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('attached_application_id', sa.Integer(), nullable=True),
    sa.Column('attached_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('validated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], name=op.f('fk_tailored_resumes_job_id_jobs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_version_id'], ['resume_versions.id'],
                            name=op.f('fk_tailored_resumes_source_version_id_resume_versions'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['attached_application_id'], ['applications.id'],
                            name=op.f('fk_tailored_resumes_attached_application_id_applications'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tailored_resumes'))
    )
    with op.batch_alter_table('tailored_resumes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tailored_resumes_job_id'), ['job_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_tailored_resumes_inputs_sha256'), ['inputs_sha256'], unique=False)
        batch_op.create_index(batch_op.f('ix_tailored_resumes_status'), ['status'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('tailored_resumes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tailored_resumes_status'))
        batch_op.drop_index(batch_op.f('ix_tailored_resumes_inputs_sha256'))
        batch_op.drop_index(batch_op.f('ix_tailored_resumes_job_id'))
    op.drop_table('tailored_resumes')
