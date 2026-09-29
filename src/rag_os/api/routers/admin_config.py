"""Governance endpoints: versioned YAML configuration (sources, access policy, facets, path rules),
hot reload, access-policy explain and the registry of available adapters."""

from __future__ import annotations

import asyncio
from typing import Any

import yaml
from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse

from rag_os.api.deps import get_container, require_role
from rag_os.api.schemas import ConfigWrite, ExplainRequest
from rag_os.application.services.access_policy import AccessPolicyEngine
from rag_os.application.services.index_schema import build_schema
from rag_os.composition import Container
from rag_os.domain.access import AccessPolicy, Principal
from rag_os.domain.classification import FacetSchema
from rag_os.domain.errors import ConfigError, NotFound, ValidationFailed
from rag_os.domain.ingestion import SourcesFile
from rag_os.infrastructure.registry import EMBEDDERS, LLMS, PARSERS, QUEUES, SEARCH_INDEXES, SOURCES
from rag_os.infrastructure.storage.config_repo import validate_yaml

router = APIRouter(prefix="/api/admin", tags=["admin: governance"])
admin = require_role("admin")
editor = require_role("admin", "taxonomy_editor")
EDITABLE = {"sources": admin, "access-policy": admin, "facets": editor, "path-rules": editor}


def _cross_validate(c: Container, kind: str, text: str) -> None:
    model = validate_yaml(kind, text)
    if kind == "sources":
        # A real check, not an assert: this is a request path, and `python -O` strips asserts - leaving the
        # narrowing gone and `model.sources` to fail as an anonymous 500 instead.
        if not isinstance(model, SourcesFile):
            raise ConfigError(f"expected a sources file for kind '{kind}', got {type(model).__name__}")
        for s in model.sources:
            c.source_factory.validate(s)
    if kind in ("access-policy", "facets"):
        policy = model if isinstance(model, AccessPolicy) else c.domain.policy
        facets = model if isinstance(model, FacetSchema) else c.domain.facets
        try:
            new = build_schema(c.index_name, c.profile, policy, facets, c.settings.search_compression)
        except ValueError as e:
            raise ValidationFailed(str(e)) from e
        current = {f.name: f.type for f in c.schema.fields}
        changed = [f.name for f in new.fields if f.name in current and current[f.name] != f.type]
        if changed:
            raise ValidationFailed("field type changes need a new index version", detail={"errors": changed})


@router.get("/config/{kind}", summary="Read a configuration document (YAML + ETag)")
async def read_config(kind: str, _: Principal = Depends(editor), c: Container = Depends(get_container)) -> JSONResponse:
    if kind not in EDITABLE and kind != "embedding-profiles":
        raise NotFound("unknown configuration kind")
    text, etag = await asyncio.to_thread(c.config.read_raw, kind)
    return JSONResponse({"kind": kind, "yaml": text, "etag": etag}, headers={"ETag": f'"{etag}"'})


@router.put("/config/{kind}", summary="Validate and store a new configuration version (optimistic concurrency)")
async def write_config(kind: str, body: ConfigWrite, if_match: str | None = Header(default=None),
                       principal: Principal = Depends(editor), c: Container = Depends(get_container)) -> JSONResponse:
    if kind not in EDITABLE:
        raise NotFound("unknown or read-only configuration kind")
    if EDITABLE[kind] is admin and not principal.is_admin:
        raise ValidationFailed(f"editing '{kind}' requires the admin role")
    if if_match is None:
        raise ValidationFailed("If-Match header (ETag from GET) is required")
    _cross_validate(c, kind, body.yaml)
    etag = await asyncio.to_thread(c.config.write_raw, kind, body.yaml, if_match)
    await c.reload_config()
    return JSONResponse({"kind": kind, "etag": etag}, headers={"ETag": f'"{etag}"'})


@router.post("/config/reload", summary="Reload configuration on this replica (others refresh within 60s)")
async def reload_config(_: Principal = Depends(admin), c: Container = Depends(get_container)) -> dict[str, Any]:
    changed = await c.reload_config()
    return {"reloaded": changed, "etags": c.domain.etags}


@router.post("/access-policy/explain", summary="Show the filter the policy produces for given attributes")
async def explain(req: ExplainRequest, _: Principal = Depends(admin),
                  c: Container = Depends(get_container)) -> dict[str, Any]:
    attrs: dict[str, list[str] | int] = {}
    for k, v in req.attributes.items():
        attrs[k] = v if isinstance(v, int | list) else [s.strip() for s in str(v).split(",") if s.strip()]
    principal = Principal(subject="explain", issuer_kind="explain", attributes=attrs, roles=set(req.roles))
    return AccessPolicyEngine(c.domain.policy, c.domain.facets).explain(principal)


@router.get("/registry", summary="Adapters available to the factories (source types, parsers, providers)")
async def registry(_: Principal = Depends(admin)) -> dict[str, Any]:
    return {
        "sources": SOURCES.describe(),
        "parsers": [{"name": n} for n in PARSERS.names()],
        "embedding_providers": EMBEDDERS.names(),
        "llm_providers": LLMS.names(),
        "search_indexes": SEARCH_INDEXES.names(),
        "queues": QUEUES.names(),
    }


@router.get("/config-schema/{kind}", summary="JSON schema of a configuration document")
async def config_schema(kind: str, _: Principal = Depends(editor)) -> dict[str, Any]:
    from rag_os.infrastructure.storage.config_repo import KINDS

    if kind not in KINDS:
        raise NotFound("unknown configuration kind")
    return KINDS[kind][1].model_json_schema()


def dump_yaml(obj: Any) -> str:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True)
