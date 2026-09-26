"""SQLAlchemy engine creation. PostgreSQL in Azure uses Microsoft Entra tokens (no passwords)."""

from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import Engine, create_engine, event

log = logging.getLogger(__name__)

_PG_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"


class _EntraTokenCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires = 0.0
        self._cred = None

    def get(self) -> str:
        # An unbounded lock around an unbounded get_token means one stuck acquisition serialises every later
        # database connection in the process, in every thread, with no log line and no way out. The lock
        # timeout is the guaranteed bound; the get_token timeout is forwarded to the azure-core transport and
        # is best-effort, since it does not necessarily bound the whole DefaultAzureCredential chain.
        if not self._lock.acquire(timeout=_TOKEN_LOCK_TIMEOUT_S):
            raise TimeoutError(
                f"waited {_TOKEN_LOCK_TIMEOUT_S}s for another thread's Entra token acquisition to finish"
            )
        try:
            if self._token and time.time() < self._expires - 300:
                return self._token
            if self._cred is None:
                from azure.identity import DefaultAzureCredential

                self._cred = DefaultAzureCredential()
            tok = self._cred.get_token(_PG_SCOPE, timeout=_TOKEN_TIMEOUT_S)
            self._token, self._expires = tok.token, float(tok.expires_on)
            return self._token
        finally:
            self._lock.release()


# Every wait below is bounded on purpose; see the comments at each use.
_PG_CONNECT_TIMEOUT_S = 10      # libpq's own connect timeout, in seconds
_TOKEN_TIMEOUT_S = 20           # one Entra token acquisition
_TOKEN_LOCK_TIMEOUT_S = 30      # waiting for another thread that is already acquiring one

_ENGINES: dict[str, Engine] = {}


def make_engine(url: str, *, entra_auth: bool = False) -> Engine:
    """Create (or reuse) an engine. SQLite is used for tests and zero-dependency local runs."""
    key = f"{url}|{entra_auth}"
    if key in _ENGINES:
        return _ENGINES[key]
    if url.startswith("sqlite"):
        eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30}, future=True)

        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()
    else:
        # connect_timeout: libpq waits forever by default, so a server that accepts TCP and then stalls - a
        # firewall dropping packets, a paused instance - blocks the thread with no timeout and no error. That
        # thread is never reclaimed even after the caller gives up, so the pool drains one connection at a time.
        eng = create_engine(url, pool_size=10, max_overflow=10, pool_pre_ping=True, pool_recycle=1800, future=True,
                            connect_args={"connect_timeout": _PG_CONNECT_TIMEOUT_S})
        if entra_auth:
            tokens = _EntraTokenCache()

            @event.listens_for(eng, "do_connect")
            def _inject_token(dialect, conn_rec, cargs, cparams):  # type: ignore[no-untyped-def]
                cparams["password"] = tokens.get()

    _ENGINES[key] = eng
    return eng
