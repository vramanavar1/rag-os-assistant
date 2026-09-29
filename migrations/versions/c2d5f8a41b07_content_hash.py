"""content identity for documents

sha256 of the bytes, so the system can tell that two documents hold the same content. It was already being
computed on every upload and thrown at `version_key`, which is only ever compared with the same document's own
previous value - so the same file uploaded twice was two documents, two blobs and two sets of vectors.

Purely additive: a nullable column plus an index. Existing rows keep NULL and every path must keep working
with that, because there is no backfill - the hash of an already-staged blob is only knowable by reading it
back, which is not worth doing at migration time for a corpus of millions. Rows acquire a hash when they are
next staged.

Revision ID: c2d5f8a41b07
Revises: b1c4e7d92f30
Create Date: 2026-09-29 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c2d5f8a41b07'
down_revision: str | None = 'b1c4e7d92f30'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('documents', sa.Column('content_hash', sa.String(length=64), nullable=True))
    # What is in the index right now, so "did the bytes change?" can be answered without trusting version_key
    # - which for a local folder is only size+mtime, and so moves when a file is merely copied or touched.
    op.add_column('documents', sa.Column('indexed_content_hash', sa.String(length=64), nullable=True))
    # Finding every document that holds one content is the lookup the whole feature rests on: the upload
    # idempotency check, and later the reference count that decides when a blob may be freed.
    op.create_index('ix_documents_content', 'documents', ['content_hash'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_documents_content', table_name='documents')
    op.drop_column('documents', 'indexed_content_hash')
    op.drop_column('documents', 'content_hash')
