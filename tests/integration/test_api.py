"""HTTP API contract: auth, problem+json, correlation ids, roles, chat, facets, uploads, admin report."""

from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rag_os.api.app import create_app
from rag_os.composition import Container
from rag_os.domain.answers import SearchHit
from rag_os.domain.documents import DocumentStatus
from rag_os.infrastructure.queue.in_memory import InMemoryQueue
from rag_os.infrastructure.settings import Settings

from .test_pipeline import drain, principal


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


def test_public_config_hands_the_browser_the_scope_verbatim(client: TestClient, settings: Settings) -> None:
    """This endpoint is the whole reason AADSTS65005 was a configuration problem rather than a code one.

    The browser never knows the scope: it asks this endpoint, then passes the answer straight to MSAL. So the
    value going out here has to be byte-for-byte what ENTRA_API_SCOPE was set to - no normalising, no appending a
    default scope name, no lower-casing - because whatever comes back is what Entra is asked for, and Entra
    matches it exactly. Only the dev branch was covered before.
    """
    scope = "api://72f70e5a-291a-4c27-a6c9-1a7d1fbe7f9e/access_as_user"
    before = (settings.entra_tenant_id, settings.entra_client_id, settings.entra_api_scope)
    settings.entra_tenant_id = "c2ff8ba6-8824-4dbb-85d6-b12c6fc80d0c"
    settings.entra_client_id = "72f70e5a-291a-4c27-a6c9-1a7d1fbe7f9e"
    settings.entra_api_scope = scope
    try:
        cfg = client.get("/api/public-config").json()
        # Entra wins over dev auth whenever all three are present, even with DEV_AUTH_ENABLED still true.
        assert cfg["auth_mode"] == "entra"
        assert cfg["entra_api_scope"] == scope
        assert cfg["entra_client_id"] == "72f70e5a-291a-4c27-a6c9-1a7d1fbe7f9e"
        assert cfg["entra_tenant_id"] == "c2ff8ba6-8824-4dbb-85d6-b12c6fc80d0c"
        # Identifiers only. ENTRA_AUDIENCE is not among them: the browser has no use for it and it is the API's
        # own validation input, not something a client should be able to read back.
        assert "entra_audience" not in cfg
    finally:
        settings.entra_tenant_id, settings.entra_client_id, settings.entra_api_scope = before


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


# --------------------------------------------------------------- "nothing to answer from" states
# Four states look alike from outside and must not be answered alike. Three of them are the caller's normal
# experience and get a plain sentence; one is a correctness hazard and must never return retrieved content.


def _ask(client: TestClient, container: Container) -> dict:
    container.guard.invalidate()  # the guard caches for 60s; each case needs its own verdict
    r = client.post("/api/chat", json={"question": "what is the leave policy?"},
                    headers=dev_token(client, "admin"))
    assert r.status_code == 200, r.text
    return r.json()


def test_an_empty_but_bootstrapped_index_still_answers_politely(client: TestClient, container: Container) -> None:
    """The regression this whole change most endangered.

    A deployment on day one has a real index with nothing in it. That is not an error and never was - the query
    runs, finds nothing, and AnswerQuery refuses with NOT_FOUND_MESSAGE. Adding a guard to the chat path must
    not turn this ordinary state into a failure.
    """
    body = _ask(client, container)
    assert body["refused"] is True
    assert body["refusal_reason"] == "no_relevant_context", body
    assert body["answer"] == "I could not find this in the documents available to you."
    assert body["citations"] == []


def test_an_index_that_was_never_bootstrapped_says_so_without_a_stack_trace(
        client: TestClient, container: Container, monkeypatch: pytest.MonkeyPatch) -> None:
    """Before: DependencyUnavailable("search query failed", status 404) - a dependency error shown to whoever
    asked a question. Now: a plain sentence, and a reason code the admin UI can act on."""
    monkeypatch.setattr(container.index, "read_profile", lambda: _none())
    body = _ask(client, container)
    assert body["refused"] is True
    assert body["refusal_reason"] == "index_not_ready", body
    assert "has not been set up yet" in body["answer"]
    assert body["citations"] == []


def test_a_profile_mismatch_refuses_and_returns_no_content(
        client: TestClient, container: Container, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one state where a polite "no information" would be actively harmful.

    An index built with a different embedding model is a different vector space: results would be confidently
    wrong rather than empty. The property that matters is that no retrieved content comes back at all.
    """
    monkeypatch.setattr(container.index, "read_profile",
                        lambda: _value({"fingerprint": "0000000000", "model": "some-other-model"}))
    body = _ask(client, container)
    assert body["refused"] is True
    assert body["refusal_reason"] == "embedding_profile_mismatch", body
    assert body["citations"] == [], "a mismatched index must never return retrieved content"


async def _none() -> None:
    return None


async def _value(v: dict) -> dict:
    return v


def test_readyz_still_reports_the_detail_for_the_operator(client: TestClient, container: Container,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """Readiness moved off /readyz for routing, but /readyz itself is unchanged - it is how an operator finds
    out why search is refusing, and it must still say 503 with reasons."""
    container.guard.invalidate()
    monkeypatch.setattr(container.index, "read_profile", lambda: _none())
    r = client.get("/api/readyz")
    assert r.status_code == 503, r.text
    reasons = r.json()["checks"]["embedding_profile"]["reasons"]
    assert any("no recorded embedding profile" in x for x in reasons), reasons
    assert client.get("/api/healthz").status_code == 200, "healthz is what keeps the replica in the ingress"


# ------------------------------------------------------------------ telling apart the reasons readyz can give
# A 503 from /api/readyz is the only signal an operator gets: no probe points at it, so the platform reports
# everything green while queries refuse. The body is therefore the whole diagnosis, and two states that need
# different remedies must not arrive wearing the same sentence.
def readyz_reasons(client: TestClient, query: str = "") -> list[str]:
    r = client.get(f"/api/readyz{query}")
    assert r.status_code == 503, f"expected the guard to refuse: {r.status_code} {r.text}"
    checks = r.json()["checks"]["embedding_profile"]
    assert checks["index"] and checks["fingerprint"], f"a 503 must still say which index it judged: {checks}"
    return list(checks["reasons"])


def test_a_missing_index_and_an_unstamped_index_read_differently(client: TestClient, container: Container,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """Both leave read_profile() returning None, and they used to produce the same sentence.

    "run bootstrap" is right for both, but only one of them means the index was created and then something
    stopped before it was stamped - which is a different thing to go and look at.
    """
    monkeypatch.setattr(container.index, "read_profile", lambda: _none())

    container.guard.invalidate()
    monkeypatch.setattr(container.index, "index_exists", lambda: _false())
    missing = " ".join(readyz_reasons(client))
    assert "does not exist" in missing, missing

    container.guard.invalidate()
    monkeypatch.setattr(container.index, "index_exists", lambda: _true())
    unstamped = " ".join(readyz_reasons(client))
    assert "exists but has no recorded embedding profile" in unstamped, unstamped
    assert missing != unstamped, "two different causes must not produce the same sentence"


def test_a_search_failure_is_reported_as_one_not_as_a_missing_index(client: TestClient, container: Container,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """read_profile() used to be called unguarded, so a Search outage escaped the guard entirely.

    The body then lost `index` and `fingerprint` and carried a bare exception name, and - worse - the refusal
    path classified it as "index_not_ready", telling the user to run bootstrap when Search was the fault.
    """
    container.guard.invalidate()
    monkeypatch.setattr(container.index, "read_profile", lambda: _raise(RuntimeError("search says no")))
    reasons = " ".join(readyz_reasons(client))
    assert "unreadable" in reasons and "RuntimeError" in reasons, reasons
    assert "search says no" not in reasons, "readyz is public; the message belongs in the log, not the body"
    assert "bootstrap" not in reasons, "Search being down is not a reason to tell someone to bootstrap"


def test_fresh_bypasses_the_guard_cache_and_the_default_does_not(client: TestClient, container: Container,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard caches for 60s, so re-curling to watch a dependency recover can show a stale verdict."""
    calls: list[bool] = []
    real = container.index.read_profile

    def counted():  # type: ignore[no-untyped-def]
        calls.append(True)
        return real()

    container.guard.invalidate()
    monkeypatch.setattr(container.index, "read_profile", counted)

    client.get("/api/readyz")
    after_first = len(calls)
    assert after_first == 1, "the first call has nothing cached, so it must do the work"

    client.get("/api/readyz")
    assert len(calls) == after_first, "the default must use the cached verdict - 08 polls this every 15s"

    client.get("/api/readyz?fresh=1")
    assert len(calls) == after_first + 1, "?fresh=1 must re-ask the dependencies"


async def _true() -> bool:
    return True


async def _false() -> bool:
    return False


async def _raise(exc: Exception) -> None:
    raise exc


# ------------------------------------------------------------------------------- rag-os doctor, from inside
def test_doctor_shows_the_real_error_that_readyz_withholds(container: Container,
                                                           monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """readyz is public, so it names exception types only. The CLI runs where the operator is authenticated."""
    import asyncio as _asyncio

    from rag_os.cli import _doctor_report

    container.guard.invalidate()
    monkeypatch.setattr(container.index, "read_profile", lambda: _raise(RuntimeError("search says no")))
    code = _asyncio.run(_doctor_report(container))
    out = capsys.readouterr().out
    assert code == 1, "an unusable index must be a non-zero exit, so a script can act on it"
    assert "search says no" in out, "the point of running it in the container is seeing the actual error"
    assert container.index_name in out and container.guard.fp in out, out


def test_doctor_never_writes(container: Container, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Looking must not change what is being looked at.

    `rag-os bootstrap` also reports guard reasons, but it creates the index and stamps the profile on the way -
    so using it to diagnose destroys the evidence of what was wrong.
    """
    import asyncio as _asyncio

    from rag_os.cli import _doctor_report

    def _forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError("doctor must not write")

    monkeypatch.setattr(container.index, "write_profile", _forbidden)
    monkeypatch.setattr(container.index, "ensure_index", _forbidden)
    container.guard.invalidate()
    _asyncio.run(_doctor_report(container))
    assert "index" in capsys.readouterr().out


# ---------------------------------------------- the ingestion pool: visible, but allowed to be asleep
# rag-embed-ingest runs with GpuMinReplicas = 0, so "not running" is its normal resting state. Folding that
# into readiness would put /api/readyz at 503 on a healthy idle system, every operator would learn to ignore
# it, and the case it exists to catch would arrive looking exactly like the noise.
class _Pool:
    def __init__(self, info: object | None = None, error: Exception | None = None) -> None:
        self._info, self._error = info, error

    async def info(self) -> object:
        if self._error:
            raise self._error
        return self._info


def test_readyz_stays_healthy_while_the_ingestion_pool_is_scaled_to_zero(
        client: TestClient, container: Container, monkeypatch: pytest.MonkeyPatch) -> None:
    container.guard.invalidate()
    monkeypatch.setattr(type(container), "embed_ingest",
                        property(lambda _self: _Pool(error=ConnectionError("no replicas"))))
    r = client.get("/api/readyz")
    assert r.status_code == 200, f"an idle ingestion pool is not a fault:\n{r.text}"
    profile = r.json()["checks"]["embedding_profile"]
    assert profile["ok"] is True
    assert any("ingest" in n for n in profile["notes"]), f"it must still be reported: {profile}"


def test_readyz_fails_when_the_ingestion_pool_answers_with_a_different_model(
        client: TestClient, container: Container, monkeypatch: pytest.MonkeyPatch) -> None:
    """The corruption case: documents would be embedded into a space the queries do not share."""
    from rag_os.domain.embedding import EmbedderInfo

    container.guard.invalidate()
    # The test container runs the `fake` provider, and _compare_embedder deliberately skips every comparison
    # for it - there is no server to disagree with. Give the guard a provider that does compare, without
    # touching guard.fp, so the index-stamp check keeps passing and only the pool is under test.
    monkeypatch.setattr(container.guard, "profile", container.profile.model_copy(update={"provider": "tei"}))
    drifted = EmbedderInfo(model="some-other/model", revision="zzz", dimensions=container.profile.dimensions)
    monkeypatch.setattr(type(container), "embed_ingest", property(lambda _self: _Pool(info=drifted)))
    matching = EmbedderInfo(model=container.profile.model, revision=container.profile.model_revision,
                            dimensions=container.profile.dimensions)
    monkeypatch.setattr(type(container), "embed_query", property(lambda _self: _Pool(info=matching)))
    r = client.get("/api/readyz")
    assert r.status_code == 503, "a pool serving the wrong model is exactly what this must catch"
    reasons = " ".join(r.json()["checks"]["embedding_profile"]["reasons"])
    assert "ingest" in reasons and "some-other/model" in reasons, reasons


def test_doctor_tolerates_a_sleeping_ingestion_pool(container: Container, monkeypatch: pytest.MonkeyPatch,
                                                    capsys) -> None:
    import asyncio as _asyncio

    from rag_os.cli import _doctor_report

    container.guard.invalidate()
    monkeypatch.setattr(type(container), "embed_ingest",
                        property(lambda _self: _Pool(error=ConnectionError("no replicas"))))
    code = _asyncio.run(_doctor_report(container))
    out = capsys.readouterr().out
    assert code == 0, f"scaled to zero is not a fault:\n{out}"
    assert "ConnectionError" in out, "it should still be visible in the report"


async def test_recent_uploads_list_is_paged_scoped_and_tabbed(client: TestClient, container: Container) -> None:
    """GET /api/uploads - the list behind the chat page's panel and the admin console's Uploads view.

    Before this existed the tracking id returned by POST /api/uploads was the only handle a document ever had,
    and nothing persisted it: closing the tab lost the document for good, because no endpoint could list it
    back. That is the gap being closed here, so the scope rule is the part worth pinning down hardest.
    """
    c = container
    sme = dev_token(client, "sme-reviewer")
    admin = dev_token(client, "admin")
    for i in range(12):
        up = client.post("/api/uploads", headers=sme,
                         files={"file": (f"note{i}.txt", f"Offsite number {i} is in Lisbon.".encode(), "text/plain")})
        assert up.status_code == 202, up.text
    await _drain(c)

    # ---- paging: ten at a time, and the second page continues rather than repeating
    page1 = client.get("/api/uploads?limit=10", headers=sme).json()
    assert len(page1["items"]) == 10 and page1["next"]
    page2 = client.get(f"/api/uploads?limit=10&after={page1['next']}", headers=sme).json()
    ids1 = [r["doc_id"] for r in page1["items"]]
    ids2 = [r["doc_id"] for r in page2["items"]]
    assert len(ids2) == 2 and not page2["next"]
    assert not set(ids1) & set(ids2), "the second page repeated rows from the first"

    # ---- newest first. Each upload is its own submit, so these have distinct arrival times.
    assert page1["items"][0]["path"].endswith("note11.txt"), "the most recent upload must lead"
    assert page2["items"][-1]["path"].endswith("note0.txt"), "and the oldest must be last"

    # ---- the tabs. All sends no status; Failed and In progress send theirs.
    indexed = client.get("/api/uploads?limit=50&status=INDEXED", headers=sme).json()
    assert len(indexed["items"]) == 12
    failed = client.get("/api/uploads?limit=50&status=FAILED", headers=sme).json()
    assert failed["items"] == []
    # counts label every tab from one call, so they must NOT narrow to the status being filtered
    assert failed["counts"]["INDEXED"] == 12 and "FAILED" not in failed["counts"]

    # ---- scope, in both directions. Somebody else uploads:
    assert client.post("/api/uploads", headers=admin,
                       files={"file": ("private.txt", b"Board pack, not for sharing.", "text/plain")}
                       ).status_code == 202
    await _drain(c)
    mine = client.get("/api/uploads?limit=50", headers=sme).json()
    assert len(mine["items"]) == 12, "a contributor must not see another principal's uploads"
    assert not any(r["path"].endswith("private.txt") for r in mine["items"])
    # ...while an admin sees everyone's, which is what makes the console's Uploads view work
    everyone = client.get("/api/uploads?limit=50", headers=admin).json()
    assert len(everyone["items"]) == 13
    assert everyone["items"][0]["path"].endswith("private.txt"), "still newest first across principals"
    # a principal with no uploads at all gets an empty list, not somebody else's
    assert client.get("/api/uploads?limit=50", headers=dev_token(client, "hr-emea")).json()["items"] == []

    # ---- the row model is deliberately narrower than DocumentRecord
    row = page1["items"][0]
    assert "blob_uri" not in row and "tags" not in row, "a list must not publish storage URIs or access tags"
    assert {"doc_id", "status", "path", "source_id", "error_type", "chunk_count"} <= set(row)

    # ---- a cursor that did not come from us is refused, not silently treated as page one
    assert client.get("/api/uploads?after=rubbish", headers=sme).status_code == 422


def _upload(client: TestClient, headers: dict[str, str], name: str, body: bytes | None = None, **form: str):
    """Content varies with the filename unless `body` says otherwise.

    Identical bytes from one uploader now collapse into a single document, so a helper that always sent the
    same text would silently make every test below a duplicate of the one before it. Pass `body` explicitly
    when duplication IS the thing under test.
    """
    payload = body if body is not None else f"Some text about {name}.".encode()
    return client.post("/api/uploads", headers=headers, files={"file": (name, payload, "text/plain")},
                       data=form or None)


async def test_a_client_supplied_folder_sets_facets_and_can_never_widen_access(client: TestClient) -> None:
    """The security property of folder-derived facets, stated as a test.

    A path rule carries both halves: `it/**` sets facets.department AND `acl: { department: ["*"] }` - readable
    by everyone. The folder path comes from the browser, so honouring the ACL half would let any contributor
    publish a document to the whole company by naming a folder `it`. Facets are descriptive and safe to take;
    ACL keeps coming from the uploader's own identity attributes.

    This test checks the OUTCOME, and two things produce it: `facets_from_path` never reads `rule.acl`, and the
    identity ACL is merged afterwards so it would overwrite a leak for every key the access policy sets. That
    second layer means this test still passes if the first is broken - so the sharp guard is the unit test
    `test_path_rules_from_an_untrusted_path_yield_facets_and_never_acl`, which asserts on the helper directly
    and does catch it. Both are kept: this one for the property users care about, that one for the mechanism.
    """
    sme = dev_token(client, "sme-reviewer")  # departments: [HR]
    control = _upload(client, sme, "plain.txt").json()
    assert control["facets_from_path"] == [] and control["relative_path"] is None

    claimed = _upload(client, sme, "sneaky.txt", relative_path="it/global/policies/sneaky.txt").json()
    # the facet half DID apply - this is the feature working
    assert claimed["facets"]["department"] == ["IT"]
    assert claimed["facet_sources"]["department"] == "path_rule:it/**"
    assert set(claimed["facets_from_path"]) == {"department", "region", "doc_type"}

    # ...and the ACL is what an upload with no path at all produced, key for key.
    acl_control = client.get(f"/api/uploads/{control['tracking_id']}", headers=sme).json()["tags"]["acl"]
    acl_claimed = client.get(f"/api/uploads/{claimed['tracking_id']}", headers=sme).json()["tags"]["acl"]
    assert acl_claimed["department"] == ["HR"], "ACL must come from identity, not from the folder"
    assert acl_claimed["department"] != ["*"], "the it/** rule made this document world-readable"
    assert acl_control == acl_claimed, "a folder path changed the ACL in some way this test did not name"


async def test_the_folder_supplies_department_region_and_doc_type(client: TestClient) -> None:
    """The original report: a PDF from an HR folder should arrive tagged HR."""
    sme = dev_token(client, "sme-reviewer")
    body = _upload(client, sme, "Benefits.pdf", relative_path="HR/UK/policies/Benefits.pdf").json()
    assert body["facets"]["department"] == ["HR"]
    assert body["facets"]["region"] == ["UK"]
    assert body["facets"]["doc_type"] == ["Policy"]
    assert body["relative_path"] == "HR/UK/policies/Benefits.pdf"
    # the folders live inside the ownership prefix, so listing still scopes and the path stays searchable
    rec = client.get(f"/api/uploads/{body['tracking_id']}", headers=sme).json()
    assert rec["path"] == "sme-reviewer/HR/UK/policies/Benefits.pdf"


async def test_the_wrong_folder_root_is_reported_rather_than_silently_doing_nothing(client: TestClient) -> None:
    """A browser gives a path relative to the folder the person PICKED, so picking `policies` instead of `HR`
    yields one segment and matches no rule. That has to be visible, or it is the same afternoon of confusion
    this whole feature exists to prevent."""
    sme = dev_token(client, "sme-reviewer")
    body = _upload(client, sme, "Benefits.pdf", relative_path="policies/Benefits.pdf").json()
    assert body["relative_path"] == "policies/Benefits.pdf", "the path was accepted"
    assert body["facets_from_path"] == [], "...and matched nothing, which the UI must be able to say"


async def test_an_explicit_choice_beats_the_folder_it_sat_in(client: TestClient) -> None:
    sme = dev_token(client, "sme-reviewer")
    picked = '{"department": ["Legal"], "confidentiality": ["Restricted"]}'
    body = _upload(client, sme, "x.txt", relative_path="HR/UK/policies/x.txt", facets=picked).json()
    assert body["facets"]["department"] == ["Legal"], "the pick must win over the folder"
    assert body["facet_sources"]["department"] == "uploader"
    assert body["facets"]["confidentiality"] == ["Restricted"], "a facet no rule sets at all"
    assert body["facets"]["region"] == ["UK"], "facets the uploader did not pick still come from the folder"
    assert body["facet_sources"]["region"] == "path_rule:*/uk/**"


async def test_each_file_is_tagged_from_its_own_folder(client: TestClient) -> None:
    """A dropped tree spans departments, so the rules run per file, not per batch."""
    sme = dev_token(client, "sme-reviewer")
    hr = _upload(client, sme, "a.txt", relative_path="hr/uk/policies/a.txt").json()
    fin = _upload(client, sme, "b.txt", relative_path="finance/global/policies/b.txt").json()
    assert hr["facets"]["department"] == ["HR"] and fin["facets"]["department"] == ["Finance"]
    assert hr["facets"]["region"] == ["UK"] and fin["facets"]["region"] == ["Global"]


async def test_no_language_is_asserted_for_an_upload(client: TestClient) -> None:
    """There is no language detection in the pipeline, so the old `language: en` source default was a claim
    about content nobody had read - every German upload was labelled English."""
    sme = dev_token(client, "sme-reviewer")
    body = _upload(client, sme, "de.txt").json()
    assert "language" not in body["facets"], "an unset facet is honest; a guessed one is not"
    picked = _upload(client, sme, "de2.txt", facets='{"language": ["de"]}').json()
    assert picked["facets"]["language"] == ["de"]


async def test_a_facet_outside_the_controlled_vocabulary_is_refused_with_the_reason(client: TestClient) -> None:
    """These used to be dropped silently at index time, so a typo looked like the feature not working."""
    sme = dev_token(client, "sme-reviewer")
    unknown = _upload(client, sme, "x.txt", facets='{"nope": ["HR"]}')
    assert unknown.status_code == 422 and "nope" in unknown.text
    bad_value = _upload(client, sme, "x.txt", facets='{"department": ["Marketing"]}')
    assert bad_value.status_code == 422 and "Marketing" in bad_value.text
    assert _upload(client, sme, "x.txt", facets="not json").status_code == 422


async def test_a_hostile_relative_path_is_refused_and_nothing_is_written(client: TestClient) -> None:
    """`path` is also the ownership boundary for listing (see `_scope`), so quietly repairing a traversal is
    how one becomes a cross-principal read."""
    sme = dev_token(client, "sme-reviewer")
    before = len(client.get("/api/uploads?limit=100", headers=sme).json()["items"])
    hostile = ["../../etc/passwd", "/absolute/x.txt", "C:\\windows\\x.txt", "a//b/x.txt",
               "hr/./x.txt", "hr/../../x.txt", "a\nb/x.txt", "/".join(["d"] * 40) + "/x.txt"]
    for bad in hostile:
        r = _upload(client, sme, "x.txt", relative_path=bad)
        assert r.status_code == 422, f"this path was accepted: {bad}"
    assert len(client.get("/api/uploads?limit=100", headers=sme).json()["items"]) == before


async def test_the_facets_endpoint_offers_the_vocabulary_even_with_an_empty_index(client: TestClient) -> None:
    """The counts and the vocabulary answer different questions. An upload picker needs the values that COULD
    be set, which is precisely the list that is empty when nothing is indexed yet."""
    facets = client.get("/api/facets", headers=dev_token(client, "sme-reviewer")).json()["facets"]
    assert set(facets) == {"department", "region", "doc_type", "topic", "confidentiality", "language"}
    assert facets["department"]["values"] == [], "nothing indexed, so no counts"
    assert [v["id"] for v in facets["department"]["vocabulary"]] == ["HR", "Finance", "Sales", "IT", "Legal", "Support"]
    assert facets["region"]["hierarchical"] is True
    assert next(v for v in facets["region"]["vocabulary"] if v["id"] == "UK")["parent"] == "EMEA"
    assert facets["language"]["closed"] is False and facets["department"]["closed"] is True


# ---------------------------------------------------------------- New Scenarios: the same bytes, more than once
# Identity used to be (source_id, item_id) and never the content, so the same file uploaded twice was two
# documents, two blobs and two sets of vectors - and an upload's sha256 was computed, used as that one
# document's version_key, and never compared with anything. These are the scenarios from the New Scenarios
# table in README.md, one test each, so the documented behaviour and the tested behaviour cannot drift.

SAME_BYTES = b"The Q3 offsite is in Lisbon on 12 September, and the budget is fixed."


def _blobs(settings: Settings) -> set[str]:
    """Every staged blob on disk. Content-addressed, so the count IS the number of distinct contents."""
    staged = Path(settings.raw_dir) / "staged"
    if not staged.exists():
        return set()
    # Relative paths, not names: under the old doc_id-keyed layout two copies of one file shared a filename
    # and differed only by directory, so comparing names would call that deduplicated when it was not.
    return {p.relative_to(staged).as_posix() for p in staged.rglob("*") if p.is_file()}


async def test_scenario_1_the_same_person_uploading_twice_gets_one_document(
    client: TestClient, container: Container, settings: Settings,
) -> None:
    """Clicking upload twice is the common case and used to cost everything twice."""
    sme = dev_token(client, "sme-reviewer")
    first = _upload(client, sme, "offsite.txt", body=SAME_BYTES).json()
    await _drain(container)
    calls_after_first = container.embed_ingest.calls  # type: ignore[attr-defined]

    second = _upload(client, sme, "offsite.txt", body=SAME_BYTES).json()
    await _drain(container)

    assert second["doc_id"] == first["doc_id"], "a repeat upload must return the existing document"
    assert second["duplicate_of"] == first["doc_id"], "...and say so, rather than pretending it ingested"
    assert len(_blobs(settings)) == 1
    assert container.embed_ingest.calls == calls_after_first, "nothing should have been embedded a second time"  # type: ignore[attr-defined]
    mine = client.get("/api/uploads?limit=50", headers=sme).json()
    assert len(mine["items"]) == 1, "and it must not appear twice in the uploader's own list"


async def test_scenario_2_two_people_uploading_the_same_file_keep_their_own_documents(
    client: TestClient, container: Container, settings: Settings,
) -> None:
    """Each owns theirs and each sees it in their own list - they have different access tags, so collapsing
    them would take a document away from one of them. The bytes are still stored once."""
    sme = dev_token(client, "sme-reviewer")
    admin = dev_token(client, "admin")
    a = _upload(client, sme, "shared.txt", body=SAME_BYTES).json()
    b = _upload(client, admin, "shared.txt", body=SAME_BYTES).json()
    await _drain(container)

    assert a["doc_id"] != b["doc_id"], "two owners, two documents"
    assert b["duplicate_of"] is None, "somebody else's upload is not this uploader's duplicate"
    assert len(_blobs(settings)) == 1, "but identical bytes are stored once"
    assert len(client.get("/api/uploads?limit=50", headers=sme).json()["items"]) == 1
    # each keeps their own access tags; nothing is merged
    for token, dept in ((sme, "HR"), (admin, "IT")):
        tid = (a if dept == "HR" else b)["tracking_id"]
        assert client.get(f"/api/uploads/{tid}", headers=token).json()["tags"]["acl"]["department"] == [dept]


async def test_scenario_3_the_same_file_from_a_crawl_and_an_upload_shares_one_blob(
    client: TestClient, container: Container, settings: Settings, tmp_path: Path,
) -> None:
    """Provenance per source is kept deliberately - a crawled copy and an uploaded copy are different
    documents with different tags - but there is no reason to store the bytes twice."""
    c = container
    cfg = c.domain.sources.get("sample-corpus")
    assert cfg is not None
    corpus = Path(cfg.settings["root"]) / "hr" / "uk" / "policies"
    corpus.mkdir(parents=True, exist_ok=True)
    (corpus / "shared-note.txt").write_bytes(SAME_BYTES)
    await c.discover.run(c.source_factory.create(cfg), "manual")
    blobs_after_crawl = _blobs(settings)

    _upload(client, dev_token(client, "sme-reviewer"), "shared-note.txt", body=SAME_BYTES)
    assert _blobs(settings) == blobs_after_crawl, "the uploaded copy must reuse the crawled bytes"


async def test_scenario_5_a_touched_file_is_not_re_embedded(container: Container) -> None:
    """A local folder has no cheap content hash, so version_key is size+mtime - and a robocopy or a `touch`
    across a corpus therefore re-parsed and re-embedded every file to produce byte-identical vectors. The
    hash computed while staging (which reads the bytes anyway) is what settles it."""
    c = container
    cfg = c.domain.sources.get("sample-corpus")
    assert cfg is not None
    source = c.source_factory.create(cfg)
    await c.discover.run(source, "manual")
    await drain(c)
    calls_after_first = c.embed_ingest.calls  # type: ignore[attr-defined]

    # touch every file: new mtime, identical bytes
    for path in Path(cfg.settings["root"]).rglob("*"):
        if path.is_file():
            path.touch()
    run = await c.discover.run(source, "manual")
    outcomes = await drain(c)

    assert run.queued > 0, "the mtime moved, so discovery must still re-examine them"
    assert outcomes.get("indexed", 0) == 0, "...but nothing should have been re-indexed"
    assert c.embed_ingest.calls == calls_after_first, "a touch must not re-embed a corpus"  # type: ignore[attr-defined]


async def test_blobs_are_addressed_by_content_not_by_document(
    client: TestClient, container: Container, settings: Settings,
) -> None:
    """The property every scenario above rests on."""
    sme = dev_token(client, "sme-reviewer")
    _upload(client, sme, "one.txt", body=b"alpha")
    _upload(client, sme, "two.txt", body=b"beta")
    assert len(_blobs(settings)) == 2, "different bytes, different blobs"
    _upload(client, dev_token(client, "admin"), "three.txt", body=b"alpha")
    assert len(_blobs(settings)) == 2, "identical bytes must not add a blob, even for another uploader"


def test_the_raw_store_refuses_to_delete_what_it_did_not_stage(tmp_path: Path) -> None:
    """`delete` exists for purge and permanent delete, which free content nothing references any more. A source
    that reads its own files in place (file://, or an azure_blob source's https url) is not ours to delete from.
    Refused by returning False rather than raising, so one foreign uri cannot abort a batch delete."""
    from rag_os.infrastructure.storage.raw_store import RawStore

    store = RawStore(target="filesystem", raw_dir=str(tmp_path / "raw"))
    victim = tmp_path / "victim.txt"
    victim.write_text("not ours")
    staged = store.stage("s", "d", "x.txt", io.BytesIO(b"hello"))
    assert store.delete(staged.uri) is True
    assert store.delete(staged.uri) is False, "already gone is not an error"
    assert store.delete(f"file:///{victim.as_posix()}") is False
    assert store.delete("local://../victim.txt") is False, "a path that escapes the raw root is refused"
    assert victim.exists()


def test_staging_the_same_bytes_twice_writes_one_blob_and_is_idempotent(tmp_path: Path) -> None:
    from rag_os.infrastructure.storage.raw_store import RawStore

    store = RawStore(target="filesystem", raw_dir=str(tmp_path))
    a = store.stage("source-one", "doc-a", "a.txt", io.BytesIO(SAME_BYTES))
    b = store.stage("source-two", "doc-b", "b.txt", io.BytesIO(SAME_BYTES), content_hash=a.content_hash)
    assert a.uri == b.uri and a.content_hash == b.content_hash
    assert len([p for p in (tmp_path / "staged").rglob("*") if p.is_file()]) == 1
    # the hash is returned even when the caller had none to give - that is how a local folder gets one
    assert a.content_hash == hashlib.sha256(SAME_BYTES).hexdigest()
    with store.open(a.uri) as fh:
        assert fh.read() == SAME_BYTES


def test_scenario_10_identical_passages_do_not_each_take_a_context_slot() -> None:
    """Duplicates arrive in the index from several directions - the same file crawled from two sources, and an
    ingest that died between upserting the new version and sweeping the old one, which leaves both live.

    Retrieval ranks byte-identical text identically and adjacently, so with top_k of 8 each duplicate costs a
    slot. The sharper harm is the answer prompt telling the model to prefer the most recent when blocks
    conflict: two copies of one passage then read as two independent sources agreeing.
    """
    from rag_os.application.use_cases.answer_query import _collapse_duplicates

    def hit(chunk: str, path: str, text: str, score: float) -> SearchHit:
        return SearchHit(chunk_id=chunk, doc_id=f"doc-{chunk}", title="Leave policy", path=path,
                         content=text, score=score, heading="", page=None, source_id="s")

    passage = "Employees in the UK receive 26 weeks of paid parental leave."
    hits = [
        hit("a", "hr/uk/policies/leave.md", passage, 0.9),
        hit("b", "uploads/sam/leave.md", f"  {passage.upper()}  ", 0.9),   # same text, whitespace and case
        hit("c", "hr/us/policies/pto.md", "US employees receive 12 weeks.", 0.7),
        hit("d", "archive/leave-old.md", passage, 0.6),
    ]
    kept, also_at = _collapse_duplicates(hits)

    assert [h.chunk_id for h in kept] == ["a", "c"], "one block per distinct passage, best-scoring kept"
    assert also_at["a"] == ["uploads/sam/leave.md", "archive/leave-old.md"], (
        "the other locations are still reported - they are documents the caller already passed the access "
        "filter for, so naming them discloses nothing new and answers 'where else does this live?'")
    assert "c" not in also_at, "different text must never be collapsed"


def test_deduplication_leaves_a_corpus_without_duplicates_untouched() -> None:
    """The guard against over-collapsing: ordinary results must pass through in their original order."""
    from rag_os.application.use_cases.answer_query import _collapse_duplicates

    hits = [
        SearchHit(chunk_id=str(i), doc_id=f"d{i}", title="t", path=f"p/{i}.md", content=f"passage {i}",
                  score=1.0, heading="", page=None, source_id="s")
        for i in range(5)
    ]
    kept, also_at = _collapse_duplicates(hits)
    assert kept == hits and also_at == {}


# ---------------------------------------------------------------- purge: the only thing that reclaims space
# Nothing in this system used to free anything: a deleted document lost its chunks and kept its state row, its
# event timeline and its staged bytes forever, because RawDocumentStore had no delete at all.


async def _delete_doc(c: Container, doc_id: str) -> None:
    """Mark a document deleted and age it past the retention window, as a source dropping it would."""
    from sqlalchemy import update

    from rag_os.infrastructure.state.sql_store import documents

    c.state.transition(doc_id, DocumentStatus.DELETED)
    # Through the table, not raw SQL: sqlite needs SQLAlchemy's adapter for the timestamp column.
    with c.state.engine.begin() as conn:  # type: ignore[attr-defined]
        conn.execute(update(documents).where(documents.c.doc_id == doc_id)
                     .values(updated_at=datetime.now(UTC) - timedelta(days=30)))


async def test_scenario_9_purging_the_last_holder_of_some_content_frees_its_blob(
    client: TestClient, container: Container, settings: Settings,
) -> None:
    c = container
    body = _upload(client, dev_token(client, "sme-reviewer"), "only.txt", body=b"the only copy").json()
    await _drain(c)
    assert len(_blobs(settings)) == 1

    await _delete_doc(c, body["doc_id"])
    dry = await c.purge.run(retention_days=7, dry_run=True)
    assert dry.documents == 1 and dry.blobs == 1 and dry.dry_run is True
    assert len(_blobs(settings)) == 1, "a dry run must not delete anything"

    applied = await c.purge.run(retention_days=7, dry_run=False)
    assert applied.documents == 1 and applied.blobs == 1
    assert _blobs(settings) == set(), "the last reference is gone, so the bytes go too"
    assert c.state.get(body["doc_id"]) is None, "and the row with them"


async def test_scenario_8_purging_one_of_several_sharers_keeps_the_shared_blob(
    client: TestClient, container: Container, settings: Settings,
) -> None:
    """The reason a content-addressed blob may not be deleted from a document's point of view: it would take
    another document's bytes with it."""
    c = container
    shared = b"a report that two people both uploaded"
    mine = _upload(client, dev_token(client, "sme-reviewer"), "r.txt", body=shared).json()
    theirs = _upload(client, dev_token(client, "admin"), "r.txt", body=shared).json()
    await _drain(c)
    assert len(_blobs(settings)) == 1

    await _delete_doc(c, mine["doc_id"])
    report = await c.purge.run(retention_days=7, dry_run=False)

    assert report.documents == 1 and report.blobs == 0
    assert report.blobs_kept_shared == 1, "the blob is still referenced and must be reported as kept"
    assert len(_blobs(settings)) == 1, "the surviving document's content must still be there"
    with c.raw.open(c.state.get(theirs["doc_id"]).blob_uri) as fh:  # type: ignore[union-attr,arg-type]
        assert fh.read() == shared, "and still readable - this is the regression that would lose data"


async def test_purge_leaves_documents_inside_the_retention_window_alone(
    client: TestClient, container: Container,
) -> None:
    """A source that briefly fails to list a file - a dropped mount, a permissions blip - marks it DELETED.
    Purging immediately would make that transient failure permanent."""
    c = container
    body = _upload(client, dev_token(client, "sme-reviewer"), "recent.txt", body=b"deleted just now").json()
    await _drain(c)
    c.state.transition(body["doc_id"], DocumentStatus.DELETED)

    report = await c.purge.run(retention_days=7, dry_run=False)
    assert report.documents == 0, "deleted moments ago is inside the window"
    assert c.state.get(body["doc_id"]) is not None


# ---------------------------------------------------------------- Account Information
# A signed-in user could see nothing about their own identity beyond a name: the chip showed `clearance=2`
# with nothing anywhere in the repo that could turn 2 into a word, because the ladder lived in a YAML comment
# and the application roles lived in a PowerShell provisioning script.


def _account(client: TestClient, pid: str) -> dict:
    r = client.get("/api/me/account", headers=dev_token(client, pid))
    assert r.status_code == 200, r.text
    return r.json()


def _attr(info: dict, name: str) -> dict:
    return next(a for a in info["attributes"] if a["name"] == name)


async def test_the_account_panel_explains_a_clearance_number(client: TestClient) -> None:
    """The gap that started this: 2 means nothing on its own, and the ladder was a comment."""
    info = _account(client, "sme-reviewer")  # clearance 2
    clearance = _attr(info, "clearance")
    assert clearance["level"] == 2
    assert [(lvl["value"], lvl["label"]) for lvl in clearance["levels"]] == [
        (0, "Public"), (1, "Internal"), (2, "Confidential"), (3, "Restricted")]
    assert all(lvl["description"] for lvl in clearance["levels"]), "every rung needs a sentence, not just a name"
    assert "Confidential" in clearance["meaning"]


async def test_a_principal_with_no_clearance_is_told_what_that_means(client: TestClient, container: Container) -> None:
    """The claims mapper drops a numeric attribute entirely rather than defaulting it, and the policy then
    reads it as 0 - so "no value" and "level 0" are the same access but very different things to be shown."""
    from rag_os.domain.access import Principal

    info = container.engine.account(Principal(subject="x", issuer_kind="dev",
                                              attributes={"department": ["HR"], "region": ["UK"]}))
    clearance = next(a for a in info["attributes"] if a["name"] == "clearance")
    assert clearance["present"] is False and clearance["level"] is None
    assert "lowest level" in clearance["meaning"], "a blank here answers nothing"


async def test_the_app_roles_shown_are_the_ones_actually_assigned(client: TestClient) -> None:
    """The regression guard for this whole feature.

    RAG-OS role mapping is many-to-many: rag.admin grants BOTH admin and contributor
    (access-policy.yaml roles:). So the mapped roles cannot be inverted to say which application role was
    assigned - doing so would tell a user they hold rag.contributor when nobody granted it. The token's own
    roles claim is the only truthful source, which is why it is now kept.
    """
    info = _account(client, "admin")  # roles: [rag.admin] only
    assert "contributor" in info["roles"], "the mapped roles do include contributor - that is the trap"
    held = {r["value"] for r in info["app_roles"] if r["held"]}
    assert held == {"rag.admin"}, f"only rag.admin was assigned; reported {held}"
    admin_role = next(r for r in info["app_roles"] if r["value"] == "rag.admin")
    assert set(admin_role["grants"]) == {"admin", "contributor"}, "what it grants is still shown"
    assert admin_role["display_name"] and admin_role["description"]


async def test_every_app_role_is_listed_whether_held_or_not(client: TestClient) -> None:
    """The panel answers "what could I be given?" as well as "what do I have?"."""
    info = _account(client, "hr-emea")  # no roles at all
    assert [r["value"] for r in info["app_roles"]] == [
        "rag.admin", "rag.contributor", "rag.sme", "rag.reviewer"]
    assert not any(r["held"] for r in info["app_roles"])
    assert info["unrecognised_roles"] == []


async def test_a_role_value_matching_nothing_is_reported_not_dropped(client: TestClient, container: Container) -> None:
    """A misspelled assignment used to be indistinguishable from no assignment, which README calls out as
    needing you to decode the token by hand."""
    principal = container.claims.map({"sub": "u", "roles": ["rag.contrbutor", "rag.admin"]}, "dev")
    assert principal.claimed_roles == ["rag.admin", "rag.contrbutor"]
    assert "contributor" in principal.roles, "rag.admin still grants it, so the typo is easy to miss"


async def test_the_panel_says_what_you_can_actually_read(client: TestClient) -> None:
    hr = _account(client, "hr-emea")
    assert hr["bypass"] is False
    # all_of is a conjunction; grant_any_of is an escape hatch, not another hurdle
    assert "every one of" in hr["summary"] and "shared with you individually" in hr["summary"]
    assert _attr(hr, "region")["values"] == ["UK"], "your own claim, not the expansion"
    assert set(_attr(hr, "region")["also_reaches"]) == {"EMEA", "Global"}

    admin = _account(client, "admin")
    assert admin["bypass"] is True
    assert "every document" in admin["summary"], "an admin bypassing the filter is worth saying plainly"


async def test_each_attribute_names_the_claim_it_came_from(client: TestClient) -> None:
    """"Why is my department wrong?" is nearly always a claim that is not arriving."""
    info = _account(client, "hr-emea")
    assert _attr(info, "department")["claim"] == "departments"   # the dev issuer's claim name
    oid = _attr(info, "employee_id")
    assert oid["label"] == "Employee OID" and oid["values"] == ["E1001"]


async def test_the_account_endpoint_needs_no_role(client: TestClient) -> None:
    """It describes only the caller, from the token they already hold - so gating it behind admin would put it
    out of reach of everyone it is for."""
    assert client.get("/api/me/account", headers=dev_token(client, "support-de")).status_code == 200
    assert client.get("/api/me/account").status_code == 401, "but it is not anonymous"


# ---------------------------------------------------------------- Settings (Security) over HTTP
# The rules are tested exhaustively in tests/unit/test_directory_admin.py. These pin the HTTP contract: who may
# call it, that the ETag round-trips through real headers, and that a half-applied write is not an error status.


def _as_entra_admin(app: object, oid: str) -> None:
    """Sign the caller in as an Entra administrator.

    A dev token cannot be used for these endpoints by design - it is self-asserted and trusted for roles, so
    with directory write permissions in hand it would be a tenant-wide escalation - and the test client has no
    way to mint a real Entra token. Overriding the principal dependency is the only way to exercise the write
    path over HTTP at all.
    """
    from rag_os.api.deps import get_principal
    from rag_os.domain.access import Principal

    def principal_override() -> Principal:
        return Principal(subject="pairwise-sub", issuer_kind="entra", display_name="Ada", roles={"admin"},
                         raw_claims={"sub": "pairwise-sub", "oid": oid})

    app.dependency_overrides[get_principal] = principal_override  # type: ignore[attr-defined]


def _with_fake_directory(container: Container) -> object:
    from rag_os.application.services.directory_admin import DirectoryAdminService
    from rag_os.infrastructure.directory.fake import FakeDirectory, FakeUser

    d = FakeDirectory()
    d.seed(FakeUser(object_id="admin-oid", user_principal_name="ada@contoso.com"))
    d.seed(FakeUser(object_id="target-oid", user_principal_name="priya@contoso.com", mail="priya@contoso.com",
                    attributes={"department": "HR"}))
    container.__dict__["directory_admin"] = DirectoryAdminService(d, container.domain.policy,
                                                                 container.domain.facets)
    return d


async def test_the_directory_endpoints_require_the_admin_role(client: TestClient) -> None:
    sme = dev_token(client, "sme-reviewer")
    assert client.get("/api/admin/directory", headers=sme).status_code == 403
    assert client.get("/api/admin/directory/users/x@y.com", headers=sme).status_code == 403
    assert client.put("/api/admin/directory/users/x@y.com", headers=sme, json={}).status_code == 403
    assert client.get("/api/admin/directory").status_code == 401


async def test_the_capability_is_served_even_with_no_directory_configured(client: TestClient) -> None:
    """The page must be able to say what to switch on, which a bare 501 does not."""
    body = client.get("/api/admin/directory", headers=dev_token(client, "admin")).json()
    assert body["enabled"] is False
    assert {a["name"] for a in body["attributes"]} == {"department", "region", "clearance"}
    assert body["propagation_note"], "the token-staleness sentence comes from the API, never the browser"


async def test_a_dev_token_cannot_write_the_directory(client: TestClient, container: Container) -> None:
    """Self-asserted roles plus Graph write permissions is a tenant-wide escalation, so this is refused at the
    service rather than merely discouraged in documentation."""
    _with_fake_directory(container)
    h = dev_token(client, "admin")
    # Reading is allowed - it describes somebody else, not the caller - so the etag is obtainable and the
    # refusal below is about the write itself rather than a missing header.
    got = client.get("/api/admin/directory/users/priya@contoso.com", headers=h)
    assert got.status_code == 200, got.text
    r = client.put("/api/admin/directory/users/priya@contoso.com",
                   headers={**h, "If-Match": got.headers["ETag"]}, json={"roles": []})
    assert r.status_code == 403, r.text
    assert "Entra sign-in" in r.json()["title"]


async def test_reading_a_person_returns_an_etag_that_the_write_requires(
    client: TestClient, container: Container
) -> None:
    _with_fake_directory(container)
    _as_entra_admin(client.app, "admin-oid")
    got = client.get("/api/admin/directory/users/priya@contoso.com")
    assert got.status_code == 200, got.text
    etag = got.headers["ETag"]
    assert etag and got.json()["attributes"]["department"] == "HR"

    without = client.put("/api/admin/directory/users/priya@contoso.com", json={"roles": ["rag.reviewer"]})
    assert without.status_code == 422, "a write with no If-Match must be refused"
    assert "If-Match" in without.json()["title"]

    ok = client.put("/api/admin/directory/users/priya@contoso.com", headers={"If-Match": etag},
                    json={"attributes": {"region": "UK"}, "roles": ["rag.reviewer"]})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["ok"] is True and body["attributes"]["region"] == "UK"
    assert body["roles"] == [{"value": "rag.reviewer", "via_group": None, "removable": True, "duplicates": 1}]
    assert ok.headers["ETag"] != etag, "the caller needs the new etag to write again"


async def test_a_stale_etag_is_a_conflict_over_http(client: TestClient, container: Container) -> None:
    _with_fake_directory(container)
    _as_entra_admin(client.app, "admin-oid")
    stale = client.get("/api/admin/directory/users/priya@contoso.com").headers["ETag"]
    client.put("/api/admin/directory/users/priya@contoso.com", headers={"If-Match": stale},
               json={"roles": ["rag.reviewer"]})
    r = client.put("/api/admin/directory/users/priya@contoso.com", headers={"If-Match": stale}, json={"roles": []})
    assert r.status_code == 409, r.text
    assert r.json()["etag"], "the fresh etag must reach the client so it can reload and retry"


async def test_an_admin_cannot_edit_their_own_access_over_http(
    client: TestClient, container: Container
) -> None:
    _with_fake_directory(container)
    _as_entra_admin(client.app, "admin-oid")
    etag = client.get("/api/admin/directory/users/ada@contoso.com").headers["ETag"]
    r = client.put("/api/admin/directory/users/ada@contoso.com", headers={"If-Match": etag},
                   json={"attributes": {"clearance": "3"}})
    assert r.status_code == 403, r.text
    assert "your own" in r.json()["title"]


async def test_a_partial_write_returns_two_hundred_with_a_report(
    client: TestClient, container: Container
) -> None:
    """Deliberately not a 5xx. An error body carries a message and nothing else, and what an administrator needs
    after a half-applied write is the list of steps that already reached the directory."""
    d = _with_fake_directory(container)
    _as_entra_admin(client.app, "admin-oid")
    etag = client.get("/api/admin/directory/users/priya@contoso.com").headers["ETag"]
    d.fail_on["grant_role"] = "Graph said no"  # type: ignore[attr-defined]
    r = client.put("/api/admin/directory/users/priya@contoso.com", headers={"If-Match": etag},
                   json={"attributes": {"region": "UK"}, "roles": ["rag.reviewer"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert any("set region" in line for line in body["applied"]), body["applied"]
    assert body["failed"] and "Graph said no" in body["failed"][0]
    assert body["attributes"]["region"] == "UK", "the response states what really landed"


async def test_a_value_outside_the_master_list_is_refused_over_http(
    client: TestClient, container: Container
) -> None:
    _with_fake_directory(container)
    _as_entra_admin(client.app, "admin-oid")
    etag = client.get("/api/admin/directory/users/priya@contoso.com").headers["ETag"]
    r = client.put("/api/admin/directory/users/priya@contoso.com", headers={"If-Match": etag},
                   json={"attributes": {"department": "Executive"}})
    assert r.status_code == 422, r.text
    assert "not an allowed value" in r.json()["title"]
