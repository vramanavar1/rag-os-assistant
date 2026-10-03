"""Permanent delete, full data reset, and the safety fixes they depend on.

Offline adapters (sqlite state + queue, in-memory index, filesystem raw store) and the sample corpus.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rag_os.api.app import create_app
from rag_os.application.services.query_trace import TraceRecorder
from rag_os.composition import Container
from rag_os.domain.documents import DocumentStatus
from rag_os.domain.errors import NotFound
from rag_os.infrastructure.search.in_memory import InMemorySearchIndex
from rag_os.infrastructure.settings import Settings
from rag_os.infrastructure.storage.raw_store import RawStore

from .test_pipeline import drain, ingest_sample, principal

PTO = "hr/us/policies/pto-policy.txt"


@pytest.fixture()
async def ready(container: Container) -> AsyncIterator[Container]:
    await ingest_sample(container)
    yield container


@pytest.fixture()
def client(settings: Settings, ready: Container) -> Any:
    with TestClient(create_app(settings, ready)) as tc:
        yield tc


def token(client: TestClient, pid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {client.post('/api/dev/token', json={'principal_id': pid}).json()['token']}"}


def chunks_of(c: Container, doc_id: str) -> int:
    idx = c.index
    assert isinstance(idx, InMemorySearchIndex)
    return sum(1 for d in idx.docs.values() if d["doc_id"] == doc_id)


def doc_id_of(c: Container, path: str) -> str:
    idx = c.index
    assert isinstance(idx, InMemorySearchIndex)
    return next(d["doc_id"] for d in idx.docs.values() if d["path"] == path)


def upload(client: TestClient, headers: dict[str, str], name: str, body: bytes) -> dict[str, Any]:
    r = client.post("/api/uploads", headers=headers, files={"file": (name, body, "text/plain")},
                    data={"facets": '{"department": ["HR"], "region": ["Global"]}'})
    assert r.status_code == 202, r.text
    return dict(r.json())


# --------------------------------------------------------------------------- safety fixes


def test_the_raw_store_never_deletes_what_it_did_not_stage(tmp_path: Path) -> None:
    store = RawStore(target="filesystem", raw_dir=str(tmp_path / "raw"))
    outside = tmp_path / "source.txt"
    outside.write_text("the customer's file")
    assert not store.owns(f"file://{outside.as_posix()}")
    assert not store.owns("https://someone.blob.core.windows.net/their-container/x.pdf")
    assert not store.owns("local://../../etc/passwd")
    assert store.delete("https://someone.blob.core.windows.net/their-container/x.pdf") is False
    import io

    staged = store.stage("s", "d", "a.txt", io.BytesIO(b"bytes"))
    assert store.owns(staged.uri) and store.delete(staged.uri)
    assert outside.exists()


async def test_rediscovery_keeps_where_the_bytes_are(ready: Container) -> None:
    """Re-listing a local folder used to blank blob_uri/content_hash on every unchanged row."""
    doc_id = doc_id_of(ready, PTO)
    before = ready.state.get(doc_id)
    assert before is not None and before.blob_uri and before.content_hash
    cfg = ready.domain.sources.get("sample-corpus")
    assert cfg is not None
    await ready.discover.run(ready.source_factory.create(cfg), "manual")
    after = ready.state.get(doc_id)
    assert after is not None and after.blob_uri == before.blob_uri and after.content_hash == before.content_hash


async def test_a_row_deleted_mid_index_takes_its_chunks_with_it(ready: Container,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    doc_id = doc_id_of(ready, PTO)
    rec = ready.state.get(doc_id)
    assert rec is not None
    real = ready.state.transition

    def vanishing(d: str, status: DocumentStatus, **kw: Any) -> Any:
        if status == DocumentStatus.INDEXED:
            raise NotFound("document deleted meanwhile")
        return real(d, status, **kw)

    monkeypatch.setattr(ready.state, "transition", vanishing)
    from rag_os.domain.ingestion import IngestMessage

    with pytest.raises(NotFound):
        await ready.processor._full(rec, IngestMessage(doc_id=doc_id, version_key=rec.version_key,
                                                       source_id=rec.source_id), 0.0)
    assert chunks_of(ready, doc_id) == 0


# --------------------------------------------------------------------------- permanent delete


async def test_permanent_delete_of_a_crawled_document_and_its_return_on_sync(client: TestClient,
                                                                             ready: Container) -> None:
    admin = token(client, "admin")
    doc_id = doc_id_of(ready, PTO)
    # A trace that mentions it, and an expectation that requires it.
    p = principal(ready, "sales-us")
    rec = TraceRecorder(principal=p, question="PTO carry over")
    rec.trace.near_miss.docs = []  # the answer itself mentions it via citations below
    answer = await ready.answer.ask(principal(ready, "hr-emea"), "How many PTO days?", trace=rec)
    t = rec.finish(answer)
    t.stages[0].data["mentions"] = doc_id
    ready.traces.save(t)
    ready.expectations.from_trace(t, expected="answer", required_doc_ids=[doc_id], note="", created_by="t")

    assert client.post("/api/admin/documents/delete", headers=token(client, "hr-emea"),
                       json={"doc_ids": [doc_id]}).status_code == 403
    r = client.post("/api/admin/documents/delete", headers=admin, json={"doc_ids": [doc_id, "nope"]})
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["documents"] == 1 and report["chunks"] > 0 and report["not_found"] == ["nope"]
    assert report["traces"] >= 1 and report["expectations_updated"] == 1
    assert chunks_of(ready, doc_id) == 0 and ready.state.get(doc_id) is None
    assert ready.state.events(doc_id) == []
    assert ready.traces.get(t.id) is None
    exp = ready.traces.list_expectations()[0]
    assert doc_id not in exp.required_doc_ids and "deleted" in exp.last_detail

    # Crawled: the file is still in the folder, so the next sync brings it back (as decided).
    cfg = ready.domain.sources.get("sample-corpus")
    assert cfg is not None
    await ready.discover.run(ready.source_factory.create(cfg), "manual")
    await drain(ready)
    assert chunks_of(ready, doc_id) > 0


async def test_permanent_delete_of_an_upload_frees_its_copy_unless_shared(client: TestClient,
                                                                         ready: Container) -> None:
    admin, sme = token(client, "admin"), token(client, "sme-reviewer")
    body = b"Shared bytes: the 401(k) plan matches 5 percent."
    mine = upload(client, sme, "a.txt", body)
    theirs = upload(client, admin, "b.txt", body)  # same bytes, another uploader: one staged blob
    await drain(ready)
    a, b = ready.state.get(mine["doc_id"]), ready.state.get(theirs["doc_id"])
    assert a is not None and b is not None and a.blob_uri == b.blob_uri
    path = Path(ready.settings.raw_dir) / a.blob_uri.removeprefix("local://")
    assert path.exists()

    first = client.post("/api/admin/documents/delete", headers=admin, json={"doc_ids": [mine["doc_id"]]}).json()
    assert first["blobs"] == 0 and first["blobs_kept_shared"] == 1 and path.exists(), "still used by the other"
    second = client.post("/api/admin/documents/delete", headers=admin, json={"doc_ids": [theirs["doc_id"]]}).json()
    assert second["blobs"] == 1 and not path.exists(), "the last user gone: the copy goes too"
    assert chunks_of(ready, mine["doc_id"]) == 0 and chunks_of(ready, theirs["doc_id"]) == 0


def test_access_tags_must_come_from_the_vocabulary(client: TestClient, ready: Container) -> None:
    admin = token(client, "admin")
    doc_id = doc_id_of(ready, PTO)
    ok = client.post(f"/api/admin/documents/{doc_id}/tags", headers=admin,
                     json={"acl": {"department": ["hr", "*"], "region": ["usa"], "clearance": 1}, "approve": False})
    assert ok.status_code == 200, ok.text
    assert ok.json()["tags"]["acl"]["department"] == ["HR", "*"], "case and synonyms map to the canonical id"
    assert ok.json()["tags"]["acl"]["region"] == ["US"]
    bad = client.post(f"/api/admin/documents/{doc_id}/tags", headers=admin,
                      json={"acl": {"department": ["Human Ressources"]}, "approve": False})
    assert bad.status_code == 422 and "not a" in bad.json()["title"]


# --------------------------------------------------------------------------- reset


async def test_reset_clears_every_store_and_keeps_configuration(client: TestClient, ready: Container) -> None:
    admin = token(client, "admin")
    upload(client, token(client, "sme-reviewer"), "u.txt", b"an upload that a reset must remove")
    client.post("/api/chat", headers=token(client, "hr-emea"), json={"question": "How many PTO days?"})
    preview = client.get("/api/admin/reset/preview", headers=admin).json()
    assert preview["index"] == ready.index_name and preview["documents"] >= 11 and preview["traces"] >= 1
    assert preview["chunks"] > 0 and any("configuration" in k for k in preview["keeps"])

    assert client.post("/api/admin/reset", headers=token(client, "hr-emea"),
                       json={"confirm": ready.index_name}).status_code == 403
    wrong = client.post("/api/admin/reset", headers=admin, json={"confirm": "kb-something-else"})
    assert wrong.status_code == 422 and ready.index_name in wrong.json()["title"]

    r = client.post("/api/admin/reset", headers=admin, json={"confirm": ready.index_name})
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["ok"], result
    assert result["steps"][0]["step"] == "pause ingestion"

    assert await ready.index.count() == 0
    assert ready.state.count_by_status(__import__("rag_os.application.ports", fromlist=["DocumentQuery"])
                                       .DocumentQuery()) == {}
    assert not (Path(ready.settings.raw_dir) / "staged").exists()
    assert sum((await ready.queue.depth()).values()) == 0
    assert ready.traces.list_expectations() == [] and ready.traces.window(
        __import__("datetime").datetime(1970, 1, 1, tzinfo=__import__("datetime").UTC)) == []
    assert ready.state.get_controls().paused, "left paused, so nothing re-ingests until an admin resumes"
    # The index is still usable: same profile stamp, so the guard passes and a question is simply not found.
    assert (await ready.index.read_profile() or {}).get("fingerprint") == ready.guard.fp
    ready.guard.invalidate()
    chat = client.post("/api/chat", headers=token(client, "hr-emea"), json={"question": "How many PTO days?"})
    assert chat.status_code == 200 and chat.json()["refusal_reason"] == "no_relevant_context"
    assert ready.domain.sources.get("sample-corpus") is not None, "configuration untouched"

    again = client.post("/api/admin/reset", headers=admin, json={"confirm": ready.index_name})
    assert again.status_code == 200 and again.json()["ok"], "a second reset is safe"


async def test_reset_can_keep_traces(client: TestClient, ready: Container) -> None:
    admin = token(client, "admin")
    client.post("/api/chat", headers=token(client, "hr-emea"), json={"question": "How many PTO days?"})
    r = client.post("/api/admin/reset", headers=admin, json={"confirm": ready.index_name, "include_traces": False})
    assert r.json()["ok"] and "delete query traces and expectations" not in [s["step"] for s in r.json()["steps"]]
    assert client.get("/api/admin/traces", headers=admin).json()["items"], "traces kept on request"
