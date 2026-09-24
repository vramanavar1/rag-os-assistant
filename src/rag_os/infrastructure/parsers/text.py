"""Plain text, Markdown and log files: decoded lazily, split into paragraph blocks.

Markdown `#` headings (outside fenced code) build `heading_path`; logs skip heading detection.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterator
from typing import IO, Any

from rag_os.application.ports import DocumentParser
from rag_os.domain.documents import ParsedDocument, Section
from rag_os.infrastructure.parsers._common import HeadingStack, clean_text, open_text, title_line
from rag_os.infrastructure.registry import PARSERS

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(```|~~~)")
_MAX_BLOCK_CHARS = 16_000  # bounds memory for files without blank lines (e.g. logs)


def _title(sample: str) -> str | None:
    """First markdown heading, else the first non-empty line."""
    for line in sample.splitlines():
        m = _HEADING.match(line)
        if m:
            return m.group(2).strip()[:200]
    return title_line(sample)


@PARSERS.register("text", description="Plain text, Markdown (heading hierarchy) and log files.")
class TextParser(DocumentParser):
    extensions = (".txt", ".text", ".md", ".markdown", ".log")
    content_types = ("text/plain", "text/markdown", "text/x-markdown")

    def parse(self, stream: IO[bytes], filename: str) -> ParsedDocument:
        text, encoding, sample = open_text(stream)
        markdown = not filename.lower().endswith(".log")
        title = _title(clean_text(sample)) if markdown else None
        doc = ParsedDocument(title=title, metadata={"encoding": encoding, "lines": 0})
        doc.sections = self._sections(text, markdown, doc.metadata)
        return doc

    @staticmethod
    def _sections(text: io.TextIOWrapper, markdown: bool, stats: dict[str, Any]) -> Iterator[Section]:
        headings = HeadingStack()
        block: list[str] = []
        size = 0
        fenced = False

        def emit() -> Iterator[Section]:
            nonlocal size
            body = "\n".join(block).strip("\n")
            block.clear()
            size = 0
            if body.strip():
                yield Section(text=body, heading_path=headings.path)

        try:
            for raw in text:
                stats["lines"] += 1
                line = clean_text(raw).rstrip()
                if markdown and _FENCE.match(line):
                    fenced = not fenced
                elif markdown and not fenced and (m := _HEADING.match(line)):
                    yield from emit()
                    headings.push(len(m.group(1)), m.group(2).strip())
                    continue
                elif not fenced and not line.strip():
                    yield from emit()
                    continue
                block.append(line)
                size += len(line) + 1
                if size >= _MAX_BLOCK_CHARS:
                    yield from emit()
            yield from emit()
        finally:
            text.close()
