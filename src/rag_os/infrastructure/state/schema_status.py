"""Is the database schema the one this code expects?

Nothing used to ask. `/api/readyz` and `rag-os doctor` both checked the database with `SELECT 1`, which
succeeds against any schema at any revision - so an image deployed without running its migrations reported
healthy and then failed on the first query that touched a new column, as a bare 500 naming nothing.

The column list SQLAlchemy emits comes from the Table metadata in the code, not from the database, so a single
missing column breaks every full-row read of that table. That makes schema drift a whole-feature outage rather
than a degraded corner, and worth naming directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine


@dataclass(frozen=True)
class SchemaStatus:
    """What the database is at, and what the code wants."""

    current: str | None  # revision stamped in the database, None when never migrated
    head: str | None  # revision this code expects
    detail: str  # one line, for a health check or an operator

    @property
    def needs_migration(self) -> bool:
        """The database is not where this build expects it, and we are sure of that.

        Covers both "stamped at an older revision" and "never migrated at all". It is deliberately False when
        the answer is unknowable - an unreachable database is already reported by the connectivity ping, and a
        deployment with no migrations directory is managing its schema some other way. Reporting an outage we
        cannot substantiate would train people to ignore this field.
        """
        return self.head is not None and self.current != self.head


def schema_status(engine: Engine, config: Any) -> SchemaStatus:
    """Compare the database's Alembic revision with the code's head.

    Never raises. This runs inside a health check and in `doctor`, and a probe that throws while reporting a
    problem is worse than the problem - the operator then has two mysteries instead of one.
    """
    if config is None:
        return SchemaStatus(None, None, "no migrations directory; schema not managed by Alembic")
    try:
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory

        head = ScriptDirectory.from_config(config).get_current_head()
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
    except Exception as e:  # unreachable database, unreadable migrations directory, anything
        return SchemaStatus(None, None, f"could not determine schema revision: {type(e).__name__}: {e}"[:300])

    if current is None:
        return SchemaStatus(None, head, f"database has no Alembic revision; expected {head}. Run rag-os bootstrap.")
    if current != head:
        return SchemaStatus(
            current, head,
            f"schema behind: database is at {current}, this build expects {head}. Run rag-os bootstrap "
            "(in Azure: ./infra/scripts/08-bootstrap.ps1).",
        )
    return SchemaStatus(current, head, f"at head ({head})")
