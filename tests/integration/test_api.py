"""HTTP API contract: auth, problem+json, correlation ids, roles, chat, facets, uploads, admin report."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rag_os.api.app import create_app
from rag_os.composition import Container
from rag_os.infrastructure.queue.in_memory import InMemoryQueue
from rag_os.infrastructure.settings import Settings

from .test_pipeline import principal


@pytest.fixture()
def client(settings: Settings, container: Container) -> TestClient:
    app = create_app(settings, container)
    with TestClient(app) as tc:
        yield tc  # type: ignore[misc]


def dev_token(client: TestClient, pid: str) -> dict[str, str]:
    r = client.post("/api/dev/token", json={"principal_id": pid})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def _drain(c: Container) -> None:
    q = c.queue
    assert isinstance(q, InMemoryQueue)
    while msgs := await q.receive(50, 0):
        for m in msgs:
            await c.processor.handle(m.message)
            await q.complete(m)


def test_health_and_problem_json(client: TestClient) -> None:
    assert client.get("/api/healthz").json() == {"status": "ok"}
    ready = client.get("/api/readyz")
    assert ready.status_code == 200, ready.text
    r = client.post("/api/chat", json={"question": "hi"}, headers={"X-Correlation-ID": "abc-123"})
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["correlation_id"] == "abc-123" and r.headers["x-correlation-id"] == "abc-123"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/openapi.json").status_code == 200


def test_roles_are_ignored_from_an_untrusted_issuer(client: TestClient, container: Container) -> None:
    """A token may assert attributes; only an issuer in trusted_for_roles may assert privileges."""
    trust = container.domain.policy.role_sources.trusted_for_roles
    container.domain.policy.role_sources.trusted_for_roles = ["entra"]  # dev is no longer trusted
    try:
        h = dev_token(client, "admin")  # this principal carries roles: [rag.admin]
        me = client.get("/api/me", headers=h).json()
        assert me["issuer_kind"] == "dev" and me["roles"] == []
        assert client.get("/api/admin/ingestion/summary", headers=h).status_code == 403
    finally:
        container.domain.policy.role_sources.trusted_for_roles = trust


def test_public_config_advertises_the_auth_mode(client: TestClient) -> None:
    cfg = client.get("/api/public-config").json()
    assert cfg["auth_mode"] == "dev" and cfg["dev_auth_enabled"] is True
    assert "embed_origins" in cfg and "portal_origins" not in cfg


async def test_chat_upload_and_admin_report(client: TestClient, container: Container) -> None:
    c = container
    cfg = c.domain.sources.get("sample-corpus")
    assert cfg is not None
    await c.discover.run(c.source_factory.create(cfg), "manual")
    await _drain(c)

    hr = dev_token(client, "hr-emea")
    r = client.post("/api/chat", headers=hr, json={"question": "How much paid parental leave do UK employees get?"})
    body = r.json()
    assert r.status_code == 200 and body["citations"] and body["usage"]["embedding"] > 0
    assert client.post("/api/chat", headers=hr, json={"question": "x", "filters": {"nope": ["a"]}}).status_code == 422
    facets = client.get("/api/facets", headers=hr).json()["facets"]
    assert "HR" in [v["id"] for v in facets["department"]["values"]]

    # upload requires contributor/admin
    files = {"file": ("note.txt", b"The Q3 offsite is in Lisbon on 12 September.", "text/plain")}
    assert client.post("/api/uploads", headers=hr, files=files).status_code == 403
    sme = dev_token(client, "sme-reviewer")
    up = client.post("/api/uploads", headers=sme, files=files)
    assert up.status_code == 202, up.text
    tid = up.json()["tracking_id"]
    await _drain(c)
    st = client.get(f"/api/uploads/{tid}", headers=sme).json()
    assert st["status"] == "INDEXED" and st["tags"]["acl"]["department"] == ["HR"]

    admin = dev_token(client, "admin")
    s = client.get("/api/admin/ingestion/summary?group_by=department", headers=admin).json()
    assert s["totals"]["INDEXED"] == 11
    docs = client.get("/api/admin/ingestion/documents?status=INDEXED&limit=5", headers=admin).json()
    assert len(docs["items"]) == 5 and docs["next"]
    detail = client.get(f"/api/admin/ingestion/documents/{docs['items'][0]['doc_id']}", headers=admin).json()
    assert detail["events"] and detail["events"][0]["status"] == "INDEXED"
    exp = client.post("/api/admin/ingestion/export", headers=admin, json={}).json()
    csv_text = client.get(exp["url"], headers=admin).text
    assert csv_text.startswith("doc_id,source_id") and csv_text.count("\n") == 12
    ctl = client.put("/api/admin/ingestion/controls", headers=admin, json={"paused": True, "max_concurrency": 2})
    assert ctl.json()["paused"] is True and ctl.json()["updated_by"] == "admin"
    exp_policy = client.post("/api/admin/access-policy/explain", headers=admin,
                             json={"attributes": {"department": "HR", "region": "UK"}}).json()
    assert "acl_department/any" in exp_policy["filter"]


async def test_review_queue_approve_retags_without_reembedding(client: TestClient, container: Container) -> None:
    c = container
    cfg = c.domain.sources.get("sample-corpus")
    assert cfg is not None
    await c.discover.run(c.source_factory.create(cfg), "manual")
    await _drain(c)
    calls_before = c.embed_ingest.calls  # type: ignore[attr-defined]

    sme = dev_token(client, "sme-reviewer")
    queue = client.get("/api/admin/review-queue", headers=sme).json()
    assert queue["items"], "documents with uncertain classification should be flagged for review"
    doc = next(i for i in queue["items"] if i["path"].startswith("support/"))

    # Review is a quality backlog, not a gate: flagged documents are already indexed and answerable.
    assert doc["review_status"] == "PENDING"
    assert doc["status"] == "INDEXED", "a flagged document must still be indexed, not held back"
    flagged = {i["path"] for i in queue["items"]}
    answered = await c.answer.ask(principal(c, "support-de"), "What is the maximum upload size?")
    assert any(cit.path in flagged for cit in answered.citations), (
        "a document awaiting review must still be retrievable and citable by an ordinary user"
    )

    # an SME corrects the facet and approves
    r = client.post(f"/api/admin/documents/{doc['doc_id']}/tags", headers=sme,
                    json={"facets": {"doc_type": ["FAQ"]}, "approve": True})
    assert r.status_code == 200, r.text
    assert r.json()["tags"]["facets"]["doc_type"] == ["FAQ"]
    assert r.json()["review_status"] == "APPROVED"

    await _drain(c)  # processes the RETAG message
    assert c.embed_ingest.calls == calls_before  # type: ignore[attr-defined]
    hits = await c.index.facets(None, ["f_doc_type"])
    assert hits["f_doc_type"].get("FAQ", 0) >= 1

    # approved tags survive re-discovery (rules must not overwrite an SME decision)
    await c.discover.run(c.source_factory.create(cfg), "manual")
    await _drain(c)
    assert c.state.get(doc["doc_id"]).tags.facets["doc_type"] == ["FAQ"]  # type: ignore[union-attr]

    # non-reviewers cannot change tags
    hr = dev_token(client, "hr-emea")
    assert client.post(f"/api/admin/documents/{doc['doc_id']}/tags", headers=hr,
                       json={"facets": {"doc_type": ["Policy"]}}).status_code == 403


def test_config_write_requires_etag_and_validates(client: TestClient) -> None:
    admin = dev_token(client, "admin")
    cur = client.get("/api/admin/config/facets", headers=admin).json()
    bad = client.put("/api/admin/config/facets", headers={**admin, "If-Match": cur["etag"]},
                     json={"yaml": "facets: [{name: x, field: 'bad field'}]"})
    assert bad.status_code == 422
    stale = client.put("/api/admin/config/facets", headers={**admin, "If-Match": "stale"}, json={"yaml": cur["yaml"]})
    assert stale.status_code == 409
    ok = client.put("/api/admin/config/facets", headers={**admin, "If-Match": cur["etag"]}, json={"yaml": cur["yaml"]})
    assert ok.status_code == 200
