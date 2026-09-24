"""PDF parser (pypdf): one section per page, streamed.

Limitation: scanned/image-only PDFs have no text layer and yield no sections (no OCR here).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import IO, Any

from pypdf import PasswordType, PdfReader

from rag_os.application.ports import DocumentParser
from rag_os.domain.documents import ParsedDocument, Section
from rag_os.domain.errors import ParseError
from rag_os.infrastructure.parsers._common import clean_text, ensure_seekable, iso, title_line
from rag_os.infrastructure.registry import PARSERS

log = logging.getLogger(__name__)


def _page_text(reader: PdfReader, index: int, stats: dict[str, Any]) -> str:
    try:
        text = clean_text(reader.pages[index].extract_text() or "").strip()
    except Exception as e:  # a single broken page must not fail the document
        log.warning("pdf page %d text extraction failed: %s", index + 1, e)
        stats["failed_pages"] = stats.get("failed_pages", 0) + 1
        return ""
    if not text:
        stats["pages_without_text"] = stats.get("pages_without_text", 0) + 1
    return text


@PARSERS.register("pdf", description="PDF documents via pypdf; one section per page (no OCR).")
class PdfParser(DocumentParser):
    extensions = (".pdf",)
    content_types = ("application/pdf", "application/x-pdf")

    def parse(self, stream: IO[bytes], filename: str) -> ParsedDocument:
        try:
            reader = PdfReader(ensure_seekable(stream), strict=False)
            if reader.is_encrypted and reader.decrypt("") == PasswordType.NOT_DECRYPTED:
                raise ParseError(f"'{filename}' is password protected", detail={"encrypted": True})
            page_count = len(reader.pages)
        except ParseError:
            raise
        except Exception as e:
            raise ParseError(f"cannot read PDF '{filename}': {e}") from e

        meta: dict[str, Any] = {"pages": page_count, "encrypted": reader.is_encrypted}
        info = _safe(lambda: reader.metadata)
        title: str | None = None
        if info is not None:
            title = _safe(lambda: str(info.title or "").strip()) or None
            fields = {
                "author": _safe(lambda: str(info.author or "").strip()),
                "created": _safe(lambda: iso(info.creation_date)),
                "modified": _safe(lambda: iso(info.modification_date)),
                "producer": _safe(lambda: str(info.producer or "").strip()),
            }
            meta.update({k: v for k, v in fields.items() if v})

        doc = ParsedDocument(title=title, metadata=meta)
        first = _page_text(reader, 0, doc.metadata) if page_count else ""
        if doc.title is None:
            doc.title = title_line(first)
        doc.sections = self._pages(reader, page_count, first, doc.metadata)
        return doc

    @staticmethod
    def _pages(reader: PdfReader, count: int, first: str, stats: dict[str, Any]) -> Iterator[Section]:
        for index in range(count):
            text = first if index == 0 else _page_text(reader, index, stats)
            if text:
                yield Section(text=text, page=index + 1)


def _safe(fn: Callable[[], Any]) -> Any:
    """Evaluate a metadata accessor; malformed PDF metadata yields None instead of failing the parse."""
    try:
        return fn()
    except Exception:
        return None
