"""Load test: does bulk ingestion degrade end-user retrieval latency?

1) generate a synthetic corpus (multi-department/region, unique facts per document):
       uv run python scripts/loadtest.py generate --docs 10000 --out samples/synthetic
   (Azure: upload it to the `knowledge` container and enable the `knowledge-blob` source.)
2) measure:
       uv run python scripts/loadtest.py run --base-url http://localhost:8080 --source synthetic-load \
           --rps 5 --baseline-s 60 --during-s 180

Server-side retrieval latency (embed_query + search timings returned by /api/chat) is compared between the
baseline phase and the phase while the bulk backfill is running. PASS if p95(during) <= 1.25 x p95(baseline).
Ingestion throughput (documents/minute) is read from the run report for the capacity model.

3) compare two deployments (e.g. Dedicated vs Consumption plan) with identical load parameters:
       ... run ... --results-json before.json --label "query=Dedicated"
       ... run ... --compare-to before.json --max-regression 1.10
   FAILs when retrieval p95 in either phase is more than --max-regression times the saved run, or the error rate
   rose by more than a percentage point. End-to-end p95 is reported but not gated: it is dominated by the hosted
   LLM's latency, which no change to this deployment's compute moves, and gating on it fails runs at random.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import random
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

DEPTS = ["hr", "finance", "sales", "it", "legal", "support"]
REGIONS = ["global", "emea", "uk", "us", "apac"]
TYPES = ["policies", "procedures", "pricing", "contracts"]
TOPICS = ["leave", "travel", "security", "pricing", "onboarding", "troubleshooting", "benefits", "expenses"]
QUESTIONS = [
    "What is the travel policy for {r}?", "How do I request {t} approval?", "What are the {t} rules in {r}?",
    "Who owns the {t} procedure?", "What is the limit for {t} claims?", "How long is the {t} retention period?",
]


def generate(n: int, out: Path, seed: int = 7) -> None:
    rnd = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(n):
        d, r, t, topic = rnd.choice(DEPTS), rnd.choice(REGIONS), rnd.choice(TYPES), rnd.choice(TOPICS)
        folder = out / d / r / t
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{topic}-{i:07d}.md"
        paras = [f"# {topic.title()} {t[:-1]} {i}", f"Applies to {d.upper()} in {r.upper()}."]
        for k in range(rnd.randint(3, 12)):
            paras.append(f"Rule {k + 1}: for {topic} the limit is {rnd.randint(1, 500)} units and approvals go to "
                         f"{rnd.choice(['manager', 'director', 'owner', 'committee'])} within {rnd.randint(1, 30)} days. "
                         f"Reference code {d[:2].upper()}-{i}-{k}.")
        (folder / name).write_text("\n\n".join(paras), encoding="utf-8")
        rows.append({"path": f"{d}/{r}/{t}/{name}", "facet.topic": topic.title() if topic.title() in
                     {"Travel", "Security", "Pricing", "Onboarding", "Troubleshooting", "Benefits"} else ""})
    with open(out / "manifest.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["path", "facet.topic"])
        w.writeheader()
        w.writerows(rows)
    print(f"generated {n} documents under {out}")


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    return s[min(len(s) - 1, round(p / 100 * (len(s) - 1)))]


async def phase(c: httpx.AsyncClient, headers: dict[str, str], rps: float, seconds: float) -> dict[str, list[float]]:
    retrieval: list[float] = []
    total: list[float] = []
    errors = 0
    rnd = random.Random()

    async def one() -> None:
        nonlocal errors
        q = rnd.choice(QUESTIONS).format(r=rnd.choice(REGIONS).upper(), t=rnd.choice(TOPICS))
        t0 = time.perf_counter()
        try:
            r = await c.post("/api/chat", headers=headers, json={"question": q})
            r.raise_for_status()
            tm = r.json().get("timings_ms", {})
            retrieval.append(float(tm.get("embed_query", 0)) + float(tm.get("search", 0)))
            total.append((time.perf_counter() - t0) * 1000)
        except httpx.HTTPError:
            errors += 1

    tasks = []
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        tasks.append(asyncio.create_task(one()))
        await asyncio.sleep(1 / rps)
    await asyncio.gather(*tasks)
    return {"retrieval": retrieval, "total": total, "errors": [float(errors)]}


def summarize(r: dict[str, list[float]]) -> dict[str, float | int | None]:
    """One phase as plain numbers for --results-json. NaN (no successful request) becomes null to stay valid JSON."""
    def ms(v: float) -> float | None:
        return None if math.isnan(v) else round(v, 1)
    return {"n": len(r["retrieval"]), "errors": int(r["errors"][0]),
            "retrieval_p50": ms(pct(r["retrieval"], 50)), "retrieval_p95": ms(pct(r["retrieval"], 95)),
            "total_p50": ms(pct(r["total"], 50)), "total_p95": ms(pct(r["total"], 95))}


# Two runs only measure the same thing when they offered the same load for the same time.
COMPARABLE = ("rps", "baseline_s", "during_s")
GATED = ("retrieval_p95",)
REPORTED = ("retrieval_p95", "total_p95")


class NotComparableError(ValueError):
    """The saved run used different load parameters; a ratio between the two would mean nothing."""


def compare_runs(prev: dict[str, Any], now: dict[str, Any],
                 max_regression: float) -> tuple[list[str], list[str]]:
    """(failures, notes) for `now` against a saved run `prev`. Raises NotComparableError on mismatched load."""
    mismatched = [k for k in COMPARABLE if prev.get(k) != now.get(k)]
    if mismatched:
        detail = ", ".join(f"{k} {prev.get(k)} vs {now.get(k)}" for k in mismatched)
        raise NotComparableError(f"the saved run used different load parameters ({detail})")
    failures: list[str] = []
    notes: list[str] = []
    for phase_name in ("baseline", "during"):
        before, after = prev[phase_name], now[phase_name]
        for metric in REPORTED:
            b, a = before.get(metric), after.get(metric)
            if not b or a is None:
                notes.append(f"{phase_name} {metric}: no data to compare")
                continue
            ratio = a / b
            line = f"{phase_name} {metric}: {b:.0f}ms -> {a:.0f}ms ({ratio:.2f}x, limit {max_regression:.2f}x)"
            if ratio > max_regression and metric in GATED:
                failures.append(line)
            else:
                notes.append(line + ("  [not gated: LLM latency]" if ratio > max_regression else ""))
        rate_before = before["errors"] / before["n"] if before.get("n") else 0.0
        rate_after = after["errors"] / after["n"] if after.get("n") else 0.0
        if rate_after > rate_before + 0.01:
            failures.append(f"{phase_name} error rate: {rate_before:.1%} -> {rate_after:.1%}")
    return failures, notes


def report(name: str, r: dict[str, list[float]]) -> None:
    print(f"{name:>9}: n={len(r['retrieval'])} errors={int(r['errors'][0])} "
          f"retrieval p50={pct(r['retrieval'], 50):.0f}ms p95={pct(r['retrieval'], 95):.0f}ms | "
          f"end-to-end p50={pct(r['total'], 50):.0f}ms p95={pct(r['total'], 95):.0f}ms")


async def run(a: argparse.Namespace) -> int:
    async with httpx.AsyncClient(base_url=a.base_url.rstrip("/"), timeout=120, verify=not a.insecure) as c:
        # queries run as a normal (filtered) principal so the access-filter cost is part of the measurement
        cfg = (await c.get("/api/public-config")).json()
        user_tok, admin_tok = a.token, a.admin_token
        if not user_tok and cfg.get("dev_auth_enabled"):
            user_tok = (await c.post("/api/dev/token", json={"principal_id": "hr-emea"})).json()["token"]
        if not admin_tok and cfg.get("dev_auth_enabled"):
            admin_tok = (await c.post("/api/dev/token", json={"principal_id": "admin"})).json()["token"]
        if not user_tok or not admin_tok:
            # Entra-only deployment: pass access tokens acquired for the API scope, e.g.
            #   az account get-access-token --scope "api://<app-id>/access_as_user" --query accessToken -o tsv
        # That command needs the Azure CLI pre-authorised for the scope first, or Entra refuses to consent:
        #   ./infra/scripts/Set-EntraAppRegistration.ps1 -Env dev -PreAuthorizeAzureCli
            print("need a query token (--token) and an admin token (--admin-token) to start ingestion")
            return 2
        H = {"Authorization": f"Bearer {user_tok}"}
        ADM = {"Authorization": f"Bearer {admin_tok}"}
        print(f"baseline: {a.baseline_s}s at {a.rps} rps ...")
        base = await phase(c, H, a.rps, a.baseline_s)
        report("baseline", base)
        r = await c.post(f"/api/admin/sources/{a.source}/sync", headers=ADM)
        if r.status_code != 200:
            print(f"could not start ingestion for '{a.source}': {r.status_code} {r.text[:300]}")
            return 2
        run_id = r.json()["run_id"]
        print(f"bulk ingestion started (run {run_id}); measuring for {a.during_s}s ...")
        during = await phase(c, H, a.rps, a.during_s)
        report("during", during)
        detail = (await c.get(f"/api/admin/ingestion/runs/{run_id}", headers=ADM)).json()
        print(f"ingestion: {detail.get('percent')}% done, {detail.get('throughput_per_min')} docs/min, "
              f"progress={detail.get('progress')}")
        b95, d95 = pct(base["retrieval"], 95), pct(during["retrieval"], 95)
        ratio = d95 / b95 if b95 else float("inf")
        ok = ratio <= a.max_ratio
        print(f"\nretrieval p95 ratio during/baseline = {ratio:.2f} (limit {a.max_ratio}) -> {'PASS' if ok else 'FAIL'}")
        if statistics.fmean(during["errors"]) > 0:
            print("note: errors occurred during the ingestion phase")
        results = {"taken_at": datetime.now(UTC).isoformat(timespec="seconds"), "label": a.label,
                   "base_url": a.base_url, "rps": a.rps, "baseline_s": a.baseline_s, "during_s": a.during_s,
                   "baseline": summarize(base), "during": summarize(during)}
        if a.results_json:
            a.results_json.parent.mkdir(parents=True, exist_ok=True)
            a.results_json.write_text(json.dumps(results, indent=2), encoding="utf-8")
            print(f"results written to {a.results_json}")
        if a.compare_to:
            prev = json.loads(a.compare_to.read_text(encoding="utf-8"))
            try:
                failures, notes = compare_runs(prev, results, a.max_regression)
            except NotComparableError as exc:
                print(f"\ncannot compare with {a.compare_to}: {exc}. Re-run with the same --rps/--baseline-s/--during-s.")
                return 2
            print(f"\ncompared with {a.compare_to} ({prev.get('label') or 'no label'}, {prev.get('taken_at')}):")
            for line in notes:
                print(f"  ok    {line}")
            for line in failures:
                print(f"  FAIL  {line}")
            print(f"comparison -> {'FAIL' if failures else 'PASS'}")
            ok = ok and not failures
        return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--docs", type=int, default=10_000)
    g.add_argument("--out", type=Path, default=Path("samples/synthetic"))
    r = sub.add_parser("run")
    r.add_argument("--base-url", required=True)
    r.add_argument("--source", default="synthetic-load")
    r.add_argument("--token")
    r.add_argument("--admin-token")
    r.add_argument("--rps", type=float, default=5)
    r.add_argument("--baseline-s", type=float, default=60)
    r.add_argument("--during-s", type=float, default=180)
    r.add_argument("--max-ratio", type=float, default=1.25)
    r.add_argument("--insecure", action="store_true")
    r.add_argument("--results-json", type=Path, help="write this run's percentiles to a JSON file")
    r.add_argument("--compare-to", type=Path, help="a --results-json file from an earlier run to compare against")
    r.add_argument("--max-regression", type=float, default=1.10, help="allowed retrieval p95 ratio vs --compare-to")
    r.add_argument("--label", default="", help="free text stored with --results-json, e.g. the compute plan")
    a = ap.parse_args()
    if a.cmd == "generate":
        generate(a.docs, a.out)
        return 0
    return asyncio.run(run(a))


if __name__ == "__main__":
    sys.exit(main())
