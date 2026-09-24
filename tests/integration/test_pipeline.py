"""End-to-end (offline): discover sample corpus -> queue -> worker pipeline -> permission-aware answers.

Uses the real factories/use cases with offline adapters (sqlite state, in-memory queue + index, fake embedder
and extractive fake LLM). Proves: access filtering per principal, status reporting, idempotency, change
detection (unchanged / retag / delete) and adding an access attribute purely through configuration.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from rag_os.application.ports import DocumentQuery
from rag_os.composition import Container
from rag_os.domain.documents import DocumentStatus
from rag_os.domain.ingestion import IngestMessage
from rag_os.infrastructure.queue.in_memory import InMemoryQueue
from rag_os.infrastructure.settings import Settings


async def drain(c: Container) -> dict[str, int]:
    q = c.queue
    assert isinstance(q, InMemoryQueue)
    outcomes: dict[str, int] = {}
    while True:
        msgs = await q.receive(50, 0)
        if not msgs:
            return outcomes
        for m in msgs:
            out = await c.processor.handle(m.message)
            outcomes[out.status] = outcomes.get(out.status, 0) + 1
            await q.complete(m)


def principal(c: Container, pid: str):  # type: ignore[no-untyped-def]
    from rag_os.api.routers.dev import _principals

    p = next(x for x in _principals(c).principals if x.id == pid)
    return c.claims.map({**p.claims, "sub": p.id, "roles": p.roles}, "dev")


async def ingest_sample(c: Container) -> None:
    cfg = c.domain.sources.get("sample-corpus")
    assert cfg is not None
    run = await c.discover.run(c.source_factory.create(cfg), "manual")
    assert run.status.value == "COMPLETED" and run.discovered == 10 and run.queued == 10
    outcomes = await drain(c)
    assert outcomes.get("indexed") == 10, outcomes


async def test_end_to_end_access_filtering(container: Container) -> None:
    c = container
    await ingest_sample(c)
    summary = {r["status"]: r["count"] for r in c.state.summary(None)}
    assert summary == {"INDEXED": 10}

    # HR (UK) sees the UK parental leave policy
    hr = await c.answer.ask(principal(c, "hr-emea"), "How many weeks of paid parental leave in the UK?")
    assert not hr.refused and hr.citations
    assert any("parental-leave-policy" in cit.path for cit in hr.citations)
    assert hr.usage.embedding > 0 and hr.usage.calls >= 1

    # Sales (US) can never retrieve HR documents
    sales = await c.answer.ask(principal(c, "sales-us"), "How many weeks of paid parental leave in the UK?")
    assert all("hr/" not in cit.path for cit in sales.citations)

    # Clearance is a ladder: a level-0 caller only reaches public content, never the confidential contract
    pub = await c.answer.ask(principal(c, "support-de"), "What is the maximum upload size?")
    assert pub.citations and all(cit.path.startswith("support/") for cit in pub.citations)
    facets = await c.answer.facet_counts(principal(c, "support-de"), c.index)
    assert set(v["id"] for v in facets["department"]["values"]) <= {"Support"}
    conf = await c.answer.ask(principal(c, "support-de"), "What is the initial term of the Fabrikam agreement?")
    assert all("msa-fabrikam" not in cit.path for cit in conf.citations)

    # Explicit per-person grant via sidecar: HR employee E1001 can read the Fabrikam contract
    msa = await c.answer.ask(principal(c, "hr-emea"), "What is the initial term of the Fabrikam agreement?")
    assert any("msa-fabrikam" in cit.path for cit in msa.citations)
    fin = await c.answer.ask(principal(c, "finance-global"), "What is the initial term of the Fabrikam agreement?")
    assert all("msa-fabrikam" not in cit.path for cit in fin.citations)


async def test_idempotency_change_detection_and_deletion(container: Container, config_dir: Path) -> None:
    c = container
    await ingest_sample(c)
    chunks_before = await c.index.count()
    emb = c.embed_ingest
    calls_before = emb.calls  # type: ignore[attr-defined]

    # Unchanged re-run: nothing queued, no embedding calls
    cfg = c.domain.sources.get("sample-corpus")
    assert cfg is not None
    run = await c.discover.run(c.source_factory.create(cfg), "manual")
    assert run.queued == 0 and run.unchanged == 10
    await drain(c)
    assert emb.calls == calls_before  # type: ignore[attr-defined]

    # Redelivered message for an indexed version: skipped, no duplicate chunks
    rec = next(iter(c.state.query(__import__("rag_os.application.ports", fromlist=["DocumentQuery"])
                                  .DocumentQuery(limit=1))[0]))
    from rag_os.domain.ingestion import IngestMessage

    out = await c.processor.handle(IngestMessage(doc_id=rec.doc_id, version_key=rec.version_key,
                                                 source_id=rec.source_id))
    assert out.status == "skipped" and await c.index.count() == chunks_before

    # Tag-only change (manifest edit) -> RETAG, no re-embedding
    corpus = Path(cfg.settings["root"])
    manifest = corpus / "manifest.csv"
    manifest.write_text(manifest.read_text().replace("hr/us/policies/pto-policy.txt,Policy,Leave",
                                                     "hr/us/policies/pto-policy.txt,FAQ,Leave"))
    run = await c.discover.run(c.source_factory.create(cfg), "manual")
    outcomes = await drain(c)
    assert outcomes == {"retagged": 1}
    assert emb.calls == calls_before  # type: ignore[attr-defined]

    # Delete a file -> DELETED in the report and its chunks removed
    (corpus / "hr/us/policies/pto-policy.txt").unlink()
    run = await c.discover.run(c.source_factory.create(cfg), "manual")
    assert run.deleted == 1
    await drain(c)
    deleted = [r for r in c.state.query(__import__("rag_os.application.ports", fromlist=["DocumentQuery"])
                                        .DocumentQuery(status=[DocumentStatus.DELETED]))[0]]
    assert len(deleted) == 1 and await c.index.count() < chunks_before


async def test_add_access_attribute_via_yaml_only(container: Container, config_dir: Path) -> None:
    c = container
    path = config_dir / "access-policy" / "access-policy.yaml"
    policy = yaml.safe_load(path.read_text())
    policy["attributes"].append({"name": "cost_center", "field": "acl_cost_center", "match": "any_of",
                                 "wildcard": "*", "required": False, "claims": {"dev": "cost_center"}})
    policy["combine"]["all_of"].append("cost_center")
    path.write_text(yaml.safe_dump(policy))
    assert await c.reload_config() is True
    assert "acl_cost_center" in c.schema.field_names()
    explain = c.engine.explain(principal(c, "hr-emea"))
    assert "acl_cost_center/any(v: v eq '*')" in str(explain["filter"])


async def test_changing_the_embedding_profile_requeues_everything(settings: Settings) -> None:
    """A profile change must re-embed the corpus. This used to queue ZERO documents.

    `discover` classified every already-indexed document as `unchanged`, because neither the discovery skip
    (`upsert_discovered`) nor the worker skip (`ProcessItem.handle`) looked at the embedding profile. The
    operator followed the runbook, saw `queued: 0`, and was left with a green /readyz over an empty index
    answering "I could not find that" to everything.
    """
    c = Container(settings)
    await c.bootstrap()
    try:
        await ingest_sample(c)
        first_index, first_fp = c.index_name, c.guard.fp
    finally:
        await c.aclose()

    # Same corpus, same state DB, different embedding profile -> a different index that starts empty.
    changed = settings.model_copy(update={"embedding_profile": "test-fake-512"})
    c2 = Container(changed)
    await c2.bootstrap()
    try:
        assert c2.guard.fp != first_fp, "the profile change must change the fingerprint"
        assert c2.index_name != first_index, "a new fingerprint must mean a new index"
        assert await c2.index.count() == 0, "the new index starts empty"

        cfg = c2.domain.sources.get("sample-corpus")
        assert cfg is not None
        run = await c2.discover.run(c2.source_factory.create(cfg), "manual")
        assert run.queued == 10, f"profile change must re-queue every document, got queued={run.queued}"
        assert run.unchanged == 0

        outcomes = await drain(c2)
        assert outcomes.get("indexed") == 10, outcomes
        assert await c2.index.count() > 0, "the new index must actually be populated"
    finally:
        await c2.aclose()


async def test_the_worker_alone_refuses_to_skip_a_stale_profile_document(settings: Settings) -> None:
    """The second skip layer, tested independently of discovery.

    `ProcessItem.handle` short-circuits on (INDEXED, same version, same tags). A redelivered or manually
    re-queued message for a document still marked INDEXED must NOT be skipped once the embedding profile has
    moved on - otherwise the new index silently keeps a hole where that document should be.
    """
    c = Container(settings)
    await c.bootstrap()
    try:
        await ingest_sample(c)
        rec = c.state.query(DocumentQuery(limit=1))[0][0]
        assert rec.status == DocumentStatus.INDEXED
        msg = IngestMessage(doc_id=rec.doc_id, version_key=rec.version_key, source_id=rec.source_id)
        # Same profile: correctly skipped, nothing to redo.
        assert (await c.processor.handle(msg)).status == "skipped"
    finally:
        await c.aclose()

    changed = settings.model_copy(update={"embedding_profile": "test-fake-512"})
    c2 = Container(changed)
    await c2.bootstrap()
    try:
        # Same row, still INDEXED, same version and tags - only the profile differs.
        assert (await c2.processor.handle(msg)).status != "skipped"
    finally:
        await c2.aclose()
