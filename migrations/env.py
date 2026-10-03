"""Alembic environment: migrates every table RAG-OS owns, using the app's own engine (keyless on Azure)."""

from __future__ import annotations

from alembic import context
from sqlalchemy import MetaData

from rag_os.infrastructure.queue.sql_queue import _meta as queue_meta
from rag_os.infrastructure.search.in_memory import _meta as search_meta
from rag_os.infrastructure.settings import get_settings
from rag_os.infrastructure.state.db import make_engine
from rag_os.infrastructure.state.sql_store import metadata as state_meta
from rag_os.infrastructure.state.trace_store import _meta as trace_meta

target_metadata = MetaData()
for md in (state_meta, queue_meta, search_meta, trace_meta):
    for table in md.tables.values():
        table.to_metadata(target_metadata)


def _url_and_auth() -> tuple[str, bool]:
    """The caller (bootstrap) may pass the URL in the Alembic config; otherwise use the app settings."""
    url = context.config.get_main_option("sqlalchemy.url", None)
    if url:
        return url, context.config.get_main_option("rag_os.entra_auth", "false").lower() == "true"
    s = get_settings()
    return s.state_db_url, s.pg_entra_auth


def run_migrations_offline() -> None:
    url, _ = _url_and_auth()
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url, entra = _url_and_auth()
    engine = make_engine(url, entra_auth=entra)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
