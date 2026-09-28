"""indexes for newest-first document paging

The recent-uploads list orders by `discovered_at DESC, doc_id DESC`. No existing index covers that:
ix_documents_status_updated is on (status, updated_at), a different column and the wrong sort for paging,
because updated_at moves as a document progresses.

Purely additive - two CREATE INDEX statements. `discovered_at` stays nullable: the one INSERT path
(SqlStateStore.upsert_discovered) always sets it and nothing else writes the column, so there is nothing to
backfill and no table rewrite to risk.

Revision ID: b1c4e7d92f30
Revises: 90aaab2a0b69
Create Date: 2026-09-28 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = 'b1c4e7d92f30'
down_revision: str | None = '90aaab2a0b69'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The All tab: no status filter, ordered by the cursor columns.
    op.create_index('ix_documents_discovered', 'documents', ['discovered_at', 'doc_id'], unique=False)
    # The Failed and In progress tabs: status first, then the sort column.
    op.create_index('ix_documents_status_discovered', 'documents', ['status', 'discovered_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_documents_status_discovered', table_name='documents')
    op.drop_index('ix_documents_discovered', table_name='documents')
