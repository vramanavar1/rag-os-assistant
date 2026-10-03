"""query traces and expectations

The first per-question record RAG-OS keeps. A trace holds what happened at each stage of answering one question
- the caller's attributes, the access filter, the hits and what the relevance bar removed, the model's stop
reason - plus a verdict on whether a refusal was correct. Expectations are a person's statement of what the
right outcome is, replayed to check it still holds.

Purely additive: two new tables, nothing existing changes. Traces hold question and answer text, are readable
by administrators only, and are purged after QUERY_TRACE_RETENTION_DAYS.

Revision ID: d4e7a1c9b302
Revises: c2d5f8a41b07
Create Date: 2026-10-03 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'd4e7a1c9b302'
down_revision: str | None = 'c2d5f8a41b07'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'query_traces',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('correlation_id', sa.String(length=64), nullable=True),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('subject', sa.String(length=256), nullable=True),
        sa.Column('display_name', sa.String(length=256), nullable=True),
        sa.Column('outcome', sa.String(length=16), nullable=False),
        sa.Column('reason', sa.String(length=48), nullable=True),
        sa.Column('verdict', sa.String(length=32), nullable=False),
        sa.Column('failed_stage', sa.String(length=24), nullable=True),
        sa.Column('question', sa.Text(), nullable=True),
        sa.Column('duration_ms', sa.Float(), nullable=False),
        sa.Column('tokens', sa.Integer(), nullable=False),
        sa.Column('model', sa.String(length=128), nullable=True),
        sa.Column('replay_of', sa.String(length=32), nullable=True),
        sa.Column('data', sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_query_traces_at', 'query_traces', ['at', 'id'], unique=False)
    op.create_index(op.f('ix_query_traces_correlation_id'), 'query_traces', ['correlation_id'], unique=False)
    op.create_index(op.f('ix_query_traces_subject'), 'query_traces', ['subject'], unique=False)
    op.create_index(op.f('ix_query_traces_verdict'), 'query_traces', ['verdict'], unique=False)
    op.create_index(op.f('ix_query_traces_replay_of'), 'query_traces', ['replay_of'], unique=False)
    op.create_table(
        'query_expectations',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('attributes', sa.JSON(), nullable=False),
        sa.Column('roles', sa.JSON(), nullable=False),
        sa.Column('filters', sa.JSON(), nullable=False),
        sa.Column('expected', sa.String(length=16), nullable=False),
        sa.Column('required_doc_ids', sa.JSON(), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_by', sa.String(length=256), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('from_trace_id', sa.String(length=32), nullable=True),
        sa.Column('last_result', sa.String(length=8), nullable=True),
        sa.Column('last_detail', sa.Text(), nullable=True),
        sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_trace_id', sa.String(length=32), nullable=True),
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_query_expectations_last_run_at'), 'query_expectations', ['last_run_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_query_expectations_last_run_at'), table_name='query_expectations')
    op.drop_table('query_expectations')
    op.drop_index(op.f('ix_query_traces_replay_of'), table_name='query_traces')
    op.drop_index(op.f('ix_query_traces_verdict'), table_name='query_traces')
    op.drop_index(op.f('ix_query_traces_subject'), table_name='query_traces')
    op.drop_index(op.f('ix_query_traces_correlation_id'), table_name='query_traces')
    op.drop_index('ix_query_traces_at', table_name='query_traces')
    op.drop_table('query_traces')
