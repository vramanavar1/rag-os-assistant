"""Bulk tagging files shared by all sources.

manifest.csv (at the source root): one row per document, SMEs edit it in Excel::

    path,facet.department,facet.region,facet.doc_type,acl.department,acl.region,acl.clearance
    policies/leave.pdf,HR,EMEA,Policy,HR,EMEA,1

Multi-values are ``;`` separated. ``<file>.meta.json`` sidecars carry the same data for one file::

    {"facets": {"doc_type": ["Contract"]}, "acl": {"department": ["Legal"], "clearance": 3}}
"""

from __future__ import annotations

import csv
import io
import json
from typing import IO

from charset_normalizer import from_bytes

from rag_os.domain.documents import TagSet

SIDECAR_SUFFIX = ".meta.json"
MANIFEST_NAME = "manifest.csv"
_MAX_META_BYTES = 256 * 1024


def _split(v: str) -> list[str]:
    return [p.strip() for p in v.split(";") if p.strip()]


def parse_manifest(stream: IO[bytes]) -> dict[str, TagSet]:
    raw = stream.read()
    best = from_bytes(raw).best()
    text = str(best) if best is not None else raw.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    out: dict[str, TagSet] = {}
    for row in reader:
        path = (row.get("path") or "").strip().replace("\\", "/").lstrip("/")
        if not path:
            continue
        facets: dict[str, list[str]] = {}
        acl: dict[str, list[str] | int] = {}
        for col, val in row.items():
            if not col or val is None or not str(val).strip():
                continue
            col = col.strip()
            if col.startswith("facet."):
                facets[col[6:]] = _split(val)
            elif col.startswith("acl."):
                acl[col[4:]] = _split(val)
        out[path.lower()] = TagSet(facets=facets, acl=acl)
    return out


def parse_sidecar(data: bytes) -> TagSet | None:
    if len(data) > _MAX_META_BYTES:
        return None
    try:
        obj = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    facets = {
        k: [str(x) for x in (v if isinstance(v, list) else [v])]
        for k, v in (obj.get("facets") or {}).items()
    }
    acl: dict[str, list[str] | int] = {}
    for k, v in (obj.get("acl") or {}).items():
        acl[k] = v if isinstance(v, int) else [str(x) for x in (v if isinstance(v, list) else [v])]
    return TagSet(facets=facets, acl=acl)
