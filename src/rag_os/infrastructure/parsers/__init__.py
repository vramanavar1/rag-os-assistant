"""Document parsers + ParserFactory.

Importing this package registers every parser in `PARSERS` (the registry autoloads it). The factory
picks a parser by file extension first, then by content type.
"""

from __future__ import annotations

import os
from typing import Any

from rag_os.application.ports import DocumentParser
from rag_os.domain.errors import NotSupported
from rag_os.infrastructure.parsers import (  # noqa: F401 - imported for @PARSERS.register side effects
    csv_parser,
    docx,
    json_parser,
    pdf,
    spreadsheet,
    text,
    xml_parser,
)
from rag_os.infrastructure.registry import PARSERS

__all__ = ["parser_for", "supported_content_types", "supported_extensions"]


def _norm_ext(ext: str) -> str:
    ext = ext.strip().lower()
    return ext if ext.startswith(".") else f".{ext}"


def _maps() -> tuple[dict[str, Any], dict[str, Any]]:
    """(extension -> factory, content type -> factory); first registration by name wins."""
    by_ext: dict[str, Any] = {}
    by_type: dict[str, Any] = {}
    for name in PARSERS.names():
        reg = PARSERS.get(name)
        if reg.stub:
            continue
        for ext in getattr(reg.factory, "extensions", ()):
            by_ext.setdefault(_norm_ext(ext), reg.factory)
        for ctype in getattr(reg.factory, "content_types", ()):
            by_type.setdefault(ctype.strip().lower(), reg.factory)
    return by_ext, by_type


def supported_extensions() -> list[str]:
    return sorted(_maps()[0])


def supported_content_types() -> list[str]:
    return sorted(_maps()[1])


def parser_for(filename: str, content_type: str | None = None) -> DocumentParser:
    """Instantiate the parser for a file (extension first, then content type)."""
    by_ext, by_type = _maps()
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    ext = os.path.splitext(base)[1].lower()
    factory = by_ext.get(ext)
    if factory is None and content_type:
        factory = by_type.get(content_type.split(";", 1)[0].strip().lower())
    if factory is None:
        raise NotSupported(
            f"no parser for '{base}' (supported: {', '.join(sorted(by_ext))})",
            detail={"extension": ext, "content_type": content_type, "supported_extensions": sorted(by_ext)},
        )
    parser: DocumentParser = factory()
    return parser
