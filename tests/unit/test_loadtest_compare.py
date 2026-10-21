"""scripts/loadtest.py compare_runs: the gate behind 10-loadtest.ps1 -CompareToBaseline.

It decides whether moving a tier to the Consumption plan cost latency, so both of its failure modes matter: a
false PASS hides a regression, and a false FAIL - from comparing unlike runs, or from gating on the hosted LLM's
latency - teaches people to ignore it.
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path
from typing import Any

import pytest

_spec = importlib.util.spec_from_file_location(
    "loadtest", Path(__file__).resolve().parents[2] / "scripts" / "loadtest.py")
assert _spec and _spec.loader
loadtest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(loadtest)


def run(retrieval_p95: float = 100.0, total_p95: float = 2000.0, errors: int = 0, n: int = 300,
        rps: float = 5, during_retrieval_p95: float | None = None) -> dict[str, Any]:
    def phase(r95: float) -> dict[str, Any]:
        return {"n": n, "errors": errors, "retrieval_p50": r95 / 2, "retrieval_p95": r95,
                "total_p50": total_p95 / 2, "total_p95": total_p95}
    return {"rps": rps, "baseline_s": 60, "during_s": 180, "baseline": phase(retrieval_p95),
            "during": phase(during_retrieval_p95 if during_retrieval_p95 is not None else retrieval_p95)}


def test_an_unchanged_deployment_passes() -> None:
    failures, notes = loadtest.compare_runs(run(), run(retrieval_p95=105), 1.10)
    assert failures == []
    assert any("baseline retrieval_p95: 100ms -> 105ms" in n for n in notes)


def test_a_retrieval_regression_in_either_phase_fails() -> None:
    failures, _ = loadtest.compare_runs(run(), run(during_retrieval_p95=130), 1.10)
    assert len(failures) == 1 and failures[0].startswith("during retrieval_p95")


def test_end_to_end_latency_is_reported_but_never_gated() -> None:
    """End-to-end is the hosted LLM's latency, which the compute plan cannot move."""
    failures, notes = loadtest.compare_runs(run(), run(total_p95=5000), 1.10)
    assert failures == []
    assert any("total_p95" in n and "not gated" in n for n in notes)


def test_a_higher_error_rate_fails() -> None:
    failures, _ = loadtest.compare_runs(run(), run(errors=15), 1.10)
    assert any("error rate" in f for f in failures)


def test_a_single_extra_error_is_tolerated() -> None:
    failures, _ = loadtest.compare_runs(run(), run(errors=1), 1.10)
    assert failures == []


def test_runs_with_different_load_are_refused_not_compared() -> None:
    with pytest.raises(loadtest.NotComparableError, match="rps 5 vs 10"):
        loadtest.compare_runs(run(), run(rps=10), 1.10)


def test_a_phase_without_data_is_noted_rather_than_divided_by() -> None:
    empty = run()
    empty["baseline"]["retrieval_p95"] = None
    failures, notes = loadtest.compare_runs(empty, run(), 1.10)
    assert failures == []
    assert any("no data" in n for n in notes)


def test_summarize_writes_valid_json_values() -> None:
    summary = loadtest.summarize({"retrieval": [], "total": [], "errors": [3.0]})
    assert summary["retrieval_p95"] is None and summary["errors"] == 3 and summary["n"] == 0
    assert not any(isinstance(v, float) and math.isnan(v) for v in summary.values())
