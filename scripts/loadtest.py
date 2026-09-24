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
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import random
import statistics
import sys
import time
from pathlib import Path

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
    a = ap.parse_args()
    if a.cmd == "generate":
        generate(a.docs, a.out)
        return 0
    return asyncio.run(run(a))


if __name__ == "__main__":
    sys.exit(main())
