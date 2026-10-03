"""Query health: the numbers behind the live strip on the troubleshooting page.

Rates count PROBLEM verdicts, not refusals. A correct "not in the documents" or "withheld by policy" is the
system working, and an alert that fires on it teaches people to ignore the alert. Replays are excluded from the
rates - they are tests, not users.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from rag_os.domain.trace import PROBLEM_VERDICTS, VERDICT_LABELS, Expectation, TraceSummaryRow, Verdict

REPEATED_REFUSALS = 3


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, round(p / 100 * (len(s) - 1))))
    return round(s[k], 1)


def _status(value: float | None, threshold: float) -> str:
    if value is None:
        return "ok"
    if value > threshold:
        return "fail"
    if value > threshold / 2:
        return "warn"
    return "ok"


def summarise(rows: list[TraceSummaryRow], *, since: datetime, minutes: int, expectations: list[Expectation],
              problem_rate: float, error_rate: float, p95_ms: float) -> dict[str, Any]:
    real = [r for r in rows if not r.replay_of]
    total = len(real)
    by_verdict = Counter(r.verdict.value for r in real)
    problems = sum(1 for r in real if r.verdict in PROBLEM_VERDICTS)
    errors = by_verdict.get(Verdict.ERROR.value, 0)
    durations = [r.duration_ms for r in real if r.outcome != "error"]
    p50, p95 = _percentile(durations, 50), _percentile(durations, 95)
    pr = round(problems / total, 4) if total else None
    er = round(errors / total, 4) if total else None

    refusals: dict[str, dict[str, Any]] = {}
    for r in real:
        if r.outcome == "answered":
            continue
        u = refusals.setdefault(r.subject, {"subject": r.subject, "display_name": r.display_name, "refusals": 0,
                                            "problems": 0, "last_at": r.at, "last_trace_id": r.id})
        u["refusals"] += 1
        u["problems"] += int(r.verdict in PROBLEM_VERDICTS)
    repeated = sorted((u for u in refusals.values() if u["refusals"] >= REPEATED_REFUSALS),
                      key=lambda u: (-u["problems"], -u["refusals"]))

    failing = [e for e in expectations if e.last_result == "fail"]
    last_error = next((r for r in real if r.verdict == Verdict.ERROR), None)

    metrics: list[dict[str, Any]] = [
        {"key": "problem_rate", "label": "Problem rate", "value": pr, "threshold": problem_rate,
         "status": _status(pr, problem_rate), "detail": f"{problems} of {total} questions"},
        {"key": "error_rate", "label": "Error rate", "value": er, "threshold": error_rate,
         "status": _status(er, error_rate), "detail": f"{errors} errors"},
        {"key": "p95_ms", "label": "p95 latency", "value": p95, "threshold": p95_ms,
         "status": _status(p95, p95_ms), "detail": f"p50 {p50} ms" if p50 is not None else "no answers yet"},
        {"key": "expectations", "label": "Failing expectations", "value": len(failing), "threshold": 0,
         "status": "fail" if failing else "ok", "detail": f"{len(expectations)} defined"},
        {"key": "repeated", "label": "People repeatedly refused", "value": len(repeated), "threshold": 0,
         "status": "warn" if repeated else "ok", "detail": f"{REPEATED_REFUSALS}+ refusals each"},
    ]
    worst = "fail" if any(m["status"] == "fail" for m in metrics) else (
        "warn" if any(m["status"] == "warn" for m in metrics) else "ok")
    return {
        "since": since.isoformat(),
        "minutes": minutes,
        "total": total,
        "replays": len(rows) - total,
        "status": worst,
        "metrics": metrics,
        "by_verdict": [{"verdict": v.value, "label": VERDICT_LABELS[v], "count": by_verdict.get(v.value, 0),
                        "problem": v in PROBLEM_VERDICTS} for v in Verdict],
        "by_reason": dict(Counter(r.reason or r.outcome for r in real)),
        "repeated_refusals": repeated[:20],
        "failing_expectations": [{"id": e.id, "question": e.question[:200], "detail": e.last_detail,
                                  "last_trace_id": e.last_trace_id} for e in failing[:20]],
        "last_error": None if last_error is None else {
            "id": last_error.id, "at": last_error.at.isoformat(), "failed_stage": last_error.failed_stage,
            "reason": last_error.reason},
    }
