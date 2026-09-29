"""Schema drift must be reported, not discovered as a 500 in an unrelated feature.

Why this file exists. On 2026-09-29 a new image was deployed without running its migrations. `documents` had
gained `content_hash` and `indexed_content_hash`, and because SQLAlchemy builds the column list from the CODE's
table metadata rather than from the database, `select(documents)` named columns the database did not have. Every
full-row read of that table raised `UndefinedColumn`, which the catch-all handler turned into "Internal server
error. The error has been logged."

The whole Upload and Documents surface was down. Chat, sign-in, the dashboard and the runs pages kept working,
`/api/healthz` and `/api/readyz` both returned healthy, and `rag-os doctor` exited zero - because every one of
them checked the database with `SELECT 1`, which succeeds against any schema at any revision.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rag_os.api.app import create_app
from rag_os.composition import Container
from rag_os.infrastructure.settings import Settings

REPO = Path(__file__).resolve().parents[2]
# The revision immediately before content_hash landed - the exact state the deployed database was in.
BEHIND = "b1c4e7d92f30"


def _stamped_db(tmp_path: Path, revision: str) -> str:
    """A database migrated to `revision` and no further."""
    db = tmp_path / f"{revision}.db"
    done = subprocess.run(
        ["uv", "run", "--extra", "dev", "alembic", "upgrade", revision],
        cwd=REPO, capture_output=True, text=True, timeout=300, check=False,
        env={**__import__("os").environ, "STATE_DB_URL": f"sqlite:///{db.as_posix()}"},
    )
    assert done.returncode == 0, done.stderr
    return f"sqlite:///{db.as_posix()}"


@pytest.fixture()
def drifted(tmp_path: Path) -> Container:
    """A container whose code is at head and whose database is one migration behind."""
    shutil.copytree(REPO / "config", tmp_path / "config")
    return Container(Settings(
        app_env="test", config_dir=str(tmp_path / "config"), state_db_url=_stamped_db(tmp_path, BEHIND),
        raw_dir=str(tmp_path / "raw"), queue="in_memory", search_backend="in_memory",
        embedding_profile="test-fake-256", llm_answer="fake", llm_utility="fake", classifier="embedding",
        dev_auth_enabled=True, otel_enabled=False, _env_file=None,  # type: ignore[call-arg]
    ))


def test_a_database_behind_the_code_is_named_not_merely_unwell(drifted: Container) -> None:
    status = drifted.schema_status()
    assert status.needs_migration is True
    assert status.current == BEHIND, "the revision the database is actually at"
    assert status.head and status.head != BEHIND, "and the one this build expects"
    # Both ids in the message: "run the migrations" without saying which is a second round trip.
    assert BEHIND in status.detail and status.head in status.detail
    assert "bootstrap" in status.detail, "the fix belongs in the message"


def test_readyz_refuses_to_call_a_drifted_deployment_ready(drifted: Container) -> None:
    """THE regression. readyz returned 200 "ready" throughout the outage, because SELECT 1 always succeeds."""
    with TestClient(create_app(drifted.settings, drifted)) as tc:
        r = tc.get("/api/readyz")
        body = r.json()
    assert r.status_code == 503 and body["status"] == "not_ready"
    assert body["checks"]["state_db"] == "ok", "the database IS reachable - that was never the problem"
    schema = body["checks"]["schema"]
    assert schema["ok"] is False
    assert schema["current"] == BEHIND and schema["expected"] != BEHIND


def test_a_query_against_a_drifted_schema_explains_itself(drifted: Container) -> None:
    """The exact request that produced "Internal server error": GET /api/uploads, fired by the Upload panel
    the moment it opens, before any file is chosen."""
    with TestClient(create_app(drifted.settings, drifted)) as tc:
        token = tc.post("/api/dev/token", json={"principal_id": "sme-reviewer"}).json()["token"]
        r = tc.get("/api/uploads?limit=10", headers={"Authorization": f"Bearer {token}"})
        body = r.json()
    assert r.status_code == 503, "a dependency in the wrong state, not an internal error"
    assert "schema" in body["title"].lower() and "out of date" in body["title"].lower()
    assert "bootstrap" in body["detail"], "name the command that fixes it"
    assert body["correlation_id"], "still traceable to the log line"
    assert "no such column" not in body["detail"], "driver text can carry hostnames; it belongs in the log"


def test_a_database_at_head_is_ready(tmp_path: Path) -> None:
    """The other half: the check must not cry wolf on a correctly migrated deployment."""
    shutil.copytree(REPO / "config", tmp_path / "config")
    c = Container(Settings(
        app_env="test", config_dir=str(tmp_path / "config"), state_db_url=_stamped_db(tmp_path, "head"),
        raw_dir=str(tmp_path / "raw"), queue="in_memory", search_backend="in_memory",
        embedding_profile="test-fake-256", llm_answer="fake", llm_utility="fake", classifier="embedding",
        dev_auth_enabled=True, otel_enabled=False, _env_file=None,  # type: ignore[call-arg]
    ))
    status = c.schema_status()
    assert status.needs_migration is False
    assert status.current == status.head and "at head" in status.detail


def test_the_check_never_raises_when_it_cannot_answer(tmp_path: Path) -> None:
    """It runs inside a health check. A probe that throws while reporting a problem leaves two mysteries, and
    an unknowable answer must not be reported as an outage either."""
    shutil.copytree(REPO / "config", tmp_path / "config")
    c = Container(Settings(
        app_env="test", config_dir=str(tmp_path / "config"),
        state_db_url="postgresql+psycopg://nobody:nobody@127.0.0.1:1/nothing",
        raw_dir=str(tmp_path / "raw"), queue="in_memory", search_backend="in_memory",
        embedding_profile="test-fake-256", llm_answer="fake", llm_utility="fake", classifier="embedding",
        dev_auth_enabled=True, otel_enabled=False, _env_file=None,  # type: ignore[call-arg]
    ))
    status = c.schema_status()  # must not raise
    assert status.needs_migration is False, "unreachable is reported by the connectivity ping, not as drift"
    assert "could not determine" in status.detail


def test_no_request_path_relies_on_a_bare_assert() -> None:
    """An AssertionError is another anonymous 500 - and under `python -O` asserts are stripped entirely, so the
    check silently disappears and the None it was guarding fails somewhere less obvious."""
    offenders = []
    for path in (REPO / "src" / "rag_os" / "api").rglob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith("assert "):
                offenders.append(f"{path.relative_to(REPO)}:{n}")
    assert not offenders, "raise a real error instead:\n  " + "\n  ".join(offenders)
