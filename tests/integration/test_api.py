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
