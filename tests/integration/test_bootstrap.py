"""Bootstrap must deliver the index even when something non-essential is wrong.

The job's critical outputs are the Alembic schema, the search index and the recorded embedding profile - without
them nothing works, and nothing else creates them. Source configuration is validated last, after all three have
succeeded, and it used to raise: one typo in sources.yaml failed the job, so 08 reported a failed bootstrap and
every piece of work it had already completed became invisible. The operator's reasonable conclusion was that the
index had not been built, when in fact it had.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from rag_os.composition import Container
from rag_os.infrastructure.settings import Settings


def _break_a_source(config_dir: Path) -> str:
    """Give one source an invalid settings block, leaving the rest of the file valid."""
    path = config_dir / "sources" / "sources.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    broken = dict(doc["sources"][0])
    broken["id"] = "typo-corpus"
    broken["settings"] = {"root": 12345, "include": "not-a-list"}  # wrong types for local_folder
    doc["sources"].append(broken)
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return broken["id"]


@pytest.mark.anyio
async def test_a_malformed_source_does_not_cost_us_the_index(settings: Settings, config_dir: Path) -> None:
    bad_id = _break_a_source(config_dir)
    c = Container(settings)
    try:
        result = await c.bootstrap()
        # The three things that matter actually happened.
        assert result["migrations"], f"migrations should still have run: {result}"
        assert result["action"] in {"profile recorded", "profile verified"}, result
        assert await c.index.read_profile() is not None, "the profile must be recorded on the index"
        # And the bad source is reported rather than swallowed.
        problems = result["source_problems"]
        assert any(bad_id in p for p in problems), f"the broken source should be named: {problems}"
    finally:
        await c.aclose()


@pytest.mark.anyio
async def test_a_healthy_config_reports_no_source_problems(settings: Settings) -> None:
    """Otherwise the new field fills with noise and stops meaning anything."""
    c = Container(settings)
    try:
        result = await c.bootstrap()
        assert result["source_problems"] == [], result["source_problems"]
    finally:
        await c.aclose()
