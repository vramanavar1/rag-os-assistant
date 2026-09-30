"""Shared fixtures: a fully offline Container (sqlite state + queue, in-memory/local index, fake embedder/LLM)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from rag_os.composition import Container
from rag_os.infrastructure.settings import Settings

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    """A writable copy of the repo configuration (tests may edit YAML)."""
    dst = tmp_path / "config"
    shutil.copytree(REPO / "config", dst)
    src_yaml = dst / "sources" / "sources.yaml"
    text = src_yaml.read_text(encoding="utf-8")
    # Every local_folder root is repointed at a copy, so a test that edits a corpus cannot change the repo -
    # test_pipeline deletes a file and rewrites manifest.csv to exercise change detection.
    for rel in ("corpus", "scenarios/global-handbook", "scenarios/uk-handbook"):
        copy = tmp_path / rel
        shutil.copytree(REPO / "samples" / rel, copy)
        text = text.replace(f"root: ./samples/{rel}", f"root: {copy.as_posix()}")
    src_yaml.write_text(text, encoding="utf-8")
    return dst


@pytest.fixture()
def settings(tmp_path: Path, config_dir: Path) -> Settings:
    return Settings(
        app_env="test",
        config_dir=str(config_dir),
        state_db_url=f"sqlite:///{(tmp_path / 'state.db').as_posix()}",
        raw_dir=str(tmp_path / "raw"),
        queue="in_memory",
        search_backend="in_memory",
        embedding_profile="test-fake-256",
        llm_answer="fake",
        llm_utility="fake",
        classifier="embedding",
        dev_auth_enabled=True,
        otel_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )


@pytest.fixture()
async def container(settings: Settings) -> Container:
    c = Container(settings)
    await c.bootstrap()
    yield c  # type: ignore[misc]
    await c.aclose()
