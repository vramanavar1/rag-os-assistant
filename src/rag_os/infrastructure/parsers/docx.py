"""Word (.docx) parser: body walked in document order, headings tracked, tables rendered as rows."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import IO, Any

import docx
from docx.document import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from rag_os.application.ports import DocumentParser
from rag_os.domain.documents import ParsedDocument, Section
from rag_os.domain.errors import ParseError
from rag_os.infrastructure.parsers._common import (
    HeadingStack,
    cell_text,
    clean_text,
    ensure_seekable,
    iso,
    render_row,
)
from rag_os.infrastructure.registry import PARSERS

_P, _TBL, _SDT, _SDT_CONTENT = qn("w:p"), qn("w:tbl"), qn("w:sdt"), qn("w:sdtContent")
_TR, _TC = qn("w:tr"), qn("w:tc")
_HEADING = re.compile(r"^heading\s*(\d+)$", re.IGNORECASE)


def _style_name(p: Paragraph) -> str:
    try:
        return (p.style.name if p.style is not None else "") or ""
    except Exception:
        return ""


def _heading_level(style: str) -> int | None:
    m = _HEADING.match(style.strip())
    return int(m.group(1)) if m else None


def _block_elements(parent: Any) -> Iterator[Any]:
    """Body-level paragraphs and tables in order, descending into content controls (w:sdt)."""
    for child in parent.iterchildren():
        if child.tag in (_P, _TBL):
            yield child
        elif child.tag == _SDT:
            content = child.find(_SDT_CONTENT)
            if content is not None:
                yield from _block_elements(content)


def _table_text(tbl: Any, parent: Any) -> str:
    rows: list[str] = []
    for tr in tbl.iterchildren(_TR):
        cells = [
            cell_text(" ".join(Paragraph(p, parent).text for p in tc.iter(_P))) for tc in tr.iterchildren(_TC)
        ]
        if not any(cells):
            continue
        rows.append(render_row(cells))
        if len(rows) == 1:
            rows.append(render_row(["---"] * len(cells)))
    return "\n".join(rows)


@PARSERS.register("docx", description="Word .docx via python-docx; heading hierarchy + tables in order.")
class DocxParser(DocumentParser):
    extensions = (".docx",)
    content_types = ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",)

    def parse(self, stream: IO[bytes], filename: str) -> ParsedDocument:
        try:
            document = docx.Document(ensure_seekable(stream))
        except Exception as e:
            raise ParseError(f"cannot read Word document '{filename}': {e}") from e

        props = document.core_properties
        meta: dict[str, Any] = {
            k: v
            for k, v in {
                "author": props.author or None,
                "created": iso(props.created),
                "modified": iso(props.modified),
                "last_modified_by": props.last_modified_by or None,
            }.items()
            if v
        }
        title = (props.title or "").strip() or self._first_heading(document)
        return ParsedDocument(title=title, metadata=meta, sections=self._sections(document))

    @staticmethod
    def _first_heading(document: Document) -> str | None:
        body = document.element.body
        for el in _block_elements(body):
            if el.tag != _P:
                continue
            p = Paragraph(el, document)
            style = _style_name(p)
            if (style.lower() == "title" or _heading_level(style)) and p.text.strip():
                return clean_text(p.text).strip()[:200]
        return None

    @staticmethod
    def _sections(document: Document) -> Iterator[Section]:
        headings = HeadingStack()
        buffer: list[str] = []

        def flush() -> Iterator[Section]:
            if buffer:
                yield Section(text="\n\n".join(buffer), heading_path=headings.path)
                buffer.clear()

        for el in _block_elements(document.element.body):
            if el.tag == _TBL:
                yield from flush()
                table = _table_text(el, document)
                if table:
                    yield Section(text=table, heading_path=headings.path, kind="table")
                continue
            p = Paragraph(el, document)
            text = clean_text(p.text).strip()
            if not text:
                continue
            style = _style_name(p)
            level = _heading_level(style)
            if level is not None:
                yield from flush()
                headings.push(level, text)
            else:
                buffer.append(f"- {text}" if style.lower().startswith("list") else text)
        yield from flush()
