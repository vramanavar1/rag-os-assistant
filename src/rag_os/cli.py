"""rag-os command line.

    rag-os serve                     run the API (uvicorn)
    rag-os worker [--once]           run the ingestion worker (--once: exit when the queue is empty)
    rag-os bootstrap                 create tables + index, record the embedding profile (idempotent)
    rag-os discover --source ID      run discovery for one source (use where a local folder is mounted)
    rag-os schedule-tick             run due scheduled sources + reconcile stuck documents (cron job)
    rag-os doctor                    why /api/readyz is refusing (read-only; prints the real errors)
    rag-os status                    ingestion summary
    rag-os sources                   configured instances + registered source types
    rag-os explain --attr k=v ...    show the access filter for a set of attributes
    rag-os ask "question" --as ID    ask as a dev principal (config/dev/principals.yaml)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from rag_os.infrastructure.settings import get_settings
from rag_os.infrastructure.telemetry import setup_telemetry


def _container():  # type: ignore[no-untyped-def]
    from rag_os.composition import Container

    s = get_settings()
    c = Container(s)
    setup_telemetry(s.service_name, s.log_level, c.settings.applicationinsights_connection_string, s.otel_enabled)
    return c


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


async def _bootstrap() -> int:
    c = _container()
    try:
        _print(await c.bootstrap())
        st = await c.guard.check(c.index, {"query": c.embed_query}, force=True)
        _print({"guard_ok": st.ok, "reasons": st.reasons})
        return 0
    finally:
        await c.aclose()


async def _discover(source_id: str, trigger: str) -> int:
    c = _container()
    try:
        cfg = c.domain.sources.get(source_id)
        if cfg is None:
            print(f"unknown source '{source_id}'. Configured: {[s.id for s in c.domain.sources.sources]}")
            return 2
        run = await c.discover.run(c.source_factory.create(cfg), trigger)
        _print(run.model_dump(mode="json"))
        return 0 if run.status.value == "COMPLETED" else 1
    finally:
        await c.aclose()


async def _schedule_tick() -> int:
    c = _container()
    try:
        _print(await c.scheduler().run(c.domain.sources))
        return 0
    finally:
        await c.aclose()


async def _doctor() -> int:
    """Why `/api/readyz` is refusing, from inside the container, without changing anything.

    readyz answers the same question but is deliberately terse: it is public and unauthenticated through the
    chat UI, so it names exception types rather than their messages. This runs where the operator is already
    authenticated, so it can show the real errors.

    Read-only on purpose. The only other command that reports guard reasons is `rag-os bootstrap`, which also
    creates the index and stamps the profile - so until now there was no way to look without changing something,
    and the act of looking destroyed the evidence of what had been wrong.
    """
    c = _container()
    try:
        return await _doctor_report(c)
    finally:
        await c.aclose()


async def _doctor_report(c: Any) -> int:
    report: dict[str, Any] = {"index": c.index_name, "expected_profile_fingerprint": c.guard.fp}
    ok = True
    try:
        report["index_exists"] = await c.index.index_exists()
    except Exception as e:
        ok = False
        report["index_exists"] = f"could not determine: {type(e).__name__}: {e}"
    try:
        stored = await c.index.read_profile()
        report["stored_profile"] = stored
        report["stored_profile_fingerprint"] = stored.get("fingerprint") if stored else None
    except Exception as e:
        ok = False
        report["stored_profile"] = f"unreadable: {type(e).__name__}: {e}"

    def _ping() -> str:
        from sqlalchemy import text

        with c.state.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return "ok"

    try:
        report["state_db"] = await asyncio.to_thread(_ping)
    except Exception as e:
        ok = False
        # The full message, unlike readyz: this output is not public.
        report["state_db"] = f"{type(e).__name__}: {e}"

    embedders = {"query": c.embed_query, "ingest": c.embed_ingest}
    pools: dict[str, Any] = {}
    for pool, emb in embedders.items():
        try:
            pools[pool] = (await emb.info()).model_dump()
        except Exception as e:
            # The ingestion pool is allowed to be asleep; only the query pool must always answer.
            if pool != "ingest":
                ok = False
            pools[pool] = f"{type(e).__name__}: {e}"
    report["embedders"] = pools
    report["profile"] = c.guard.profile.model_dump()

    try:
        st = await c.guard.check(c.index, {"query": c.embed_query, "ingest": c.embed_ingest}, force=True,
                                 advisory_pools=frozenset({"ingest"}))
        report["guard_ok"] = st.ok
        report["reasons"] = st.reasons
        report["notes"] = st.notes
        ok = ok and st.ok
    except Exception as e:
        ok = False
        report["guard_ok"] = False
        report["reasons"] = [f"{type(e).__name__}: {e}"]
    _print(report)
    return 0 if ok else 1


async def _status() -> int:
    c = _container()
    try:
        rows = c.state.summary(None)
        _print({"by_source": rows, "queue": await c.queue.depth(), "controls": c.state.get_controls().model_dump()})
        return 0
    finally:
        await c.aclose()


async def _purge(retention_days: int, limit: int, apply: bool) -> int:
    """Dry run unless --apply. The only command here that destroys data, so it does not do so by accident."""
    c = _container()
    try:
        report = await c.purge.run(retention_days=retention_days, limit=limit, dry_run=not apply)
        _print(report.as_dict())
        if report.dry_run and report.documents:
            print("Dry run - nothing was deleted. Re-run with --apply to free this.")
        return 1 if report.errors else 0
    finally:
        await c.aclose()


async def _ask(question: str, principal_id: str) -> int:
    from rag_os.api.routers.dev import _principals

    c = _container()
    try:
        p = next((x for x in _principals(c).principals if x.id == principal_id), None)
        if p is None:
            print(f"unknown dev principal '{principal_id}'")
            return 2
        principal = c.claims.map({**p.claims, "sub": p.id, "roles": p.roles}, "dev")
        ans = await c.answer.ask(principal, question)
        _print(ans.model_dump(mode="json"))
        return 0
    finally:
        await c.aclose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rag-os", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve")
    sp.add_argument("--host", default="0.0.0.0")  # noqa: S104 - container entrypoint
    sp.add_argument("--port", type=int, default=8000)
    wp = sub.add_parser("worker")
    wp.add_argument("--once", action="store_true")
    sub.add_parser("bootstrap")
    dp = sub.add_parser("discover")
    dp.add_argument("--source", required=True)
    dp.add_argument("--trigger", default="manual")
    sub.add_parser("schedule-tick")
    sub.add_parser("doctor")
    sub.add_parser("status")
    sub.add_parser("sources")
    ep = sub.add_parser("explain")
    ep.add_argument("--attr", action="append", default=[], help="name=value[,value2]  (repeatable)")
    ep.add_argument("--role", action="append", default=[])
    pp = sub.add_parser("purge", help="free deleted documents: index chunks, staged blobs and state rows")
    pp.add_argument("--retention-days", type=int, default=7,
                    help="only touch documents deleted longer ago than this (default 7)")
    pp.add_argument("--limit", type=int, default=1000)
    pp.add_argument("--apply", action="store_true",
                    help="actually delete. Without it this is a dry run, which is the default on purpose.")
    qp = sub.add_parser("ask")
    qp.add_argument("question")
    qp.add_argument("--as", dest="principal", default="hr-emea")
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("rag_os.api.main:app", host=args.host, port=args.port, proxy_headers=True, log_config=None)
        return 0
    if args.cmd == "worker":
        from rag_os.worker.main import main_async

        c = _container()
        n = asyncio.run(main_async(c, once=args.once))
        print(f"processed {n} messages")
        return 0
    if args.cmd == "bootstrap":
        return asyncio.run(_bootstrap())
    if args.cmd == "discover":
        return asyncio.run(_discover(args.source, args.trigger))
    if args.cmd == "schedule-tick":
        return asyncio.run(_schedule_tick())
    if args.cmd == "doctor":
        return asyncio.run(_doctor())
    if args.cmd == "status":
        return asyncio.run(_status())
    if args.cmd == "sources":
        from rag_os.infrastructure.registry import SOURCES

        c = _container()
        _print({"configured": [s.model_dump(mode="json") for s in c.domain.sources.sources],
                "registered_types": SOURCES.describe()})
        return 0
    if args.cmd == "explain":
        from rag_os.domain.access import Principal

        c = _container()
        attrs: dict[str, list[str] | int] = {}
        for a in args.attr:
            k, _, v = a.partition("=")
            attrs[k] = int(v) if v.isdigit() else [x for x in v.split(",") if x]
        _print(c.engine.explain(Principal(subject="cli", issuer_kind="cli", attributes=attrs, roles=set(args.role))))
        return 0
    if args.cmd == "purge":
        return asyncio.run(_purge(args.retention_days, args.limit, apply=args.apply))
    if args.cmd == "ask":
        return asyncio.run(_ask(args.question, args.principal))
    return 2


if __name__ == "__main__":
    sys.exit(main())
