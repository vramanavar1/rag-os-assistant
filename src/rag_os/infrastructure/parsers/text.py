"""Plain text, Markdown and log files: decoded lazily, split into paragraph blocks.

Markdown `#` headings (outside fenced code) build `heading_path`; logs skip heading detection.

An optional `---` fenced front-matter block at the very top supplies document metadata - today just
`effective_date`, which the answer prompt uses to prefer the newer of two documents that disagree. It is
stripped from the text, so it never reaches a chunk, an embedding or a citation snippet.
"""

from __future__ import annotations

import io
import itertools
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

_FM_FENCE = re.compile(r"^---[ \t]*$")
_FM_PAIR = re.compile(r"^([A-Za-z][A-Za-z0-9_]{0,63}):[ \t]*(.*?)[ \t]*$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# An ALLOW-LIST, not "whatever the document says". The same dict carries the parser's own stats (`lines`,
# `encoding`), so a document could otherwise overwrite them - and metadata from a file is untrusted input.
_FM_KEYS = frozenset({"effective_date"})
_FM_MAX_LINES = 20  # front matter is a header, not a payload; an unterminated fence must not eat the document


def _front_matter_value(key: str, value: str) -> str | None:
    """A value we are willing to believe. Unknown keys and malformed dates are dropped, never fatal."""
    if key not in _FM_KEYS or not value:
        return None
    # ISO-8601 only: the field is `sortable` in the index and is rendered into the prompt, so "1 April 2026"
    # would neither sort nor compare. Refusing it is better than showing the model something it cannot order.
    return value if _ISO_DATE.match(value) else None


def _strip_front_matter(sample: str) -> str:
    """The sample with a leading front-matter block removed, so it does not become the title."""
    lines = sample.splitlines()
    if not lines or not _FM_FENCE.match(lines[0]):
        return sample
    for i in range(1, min(len(lines), _FM_MAX_LINES + 2)):
        if _FM_FENCE.match(lines[i]):
            return "\n".join(lines[i + 1:])
    return sample


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
        title = _title(_strip_front_matter(clean_text(sample))) if markdown else None
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
            # Front matter, if any: consumed before the body so it cannot become a section. Written into
            # `stats`, which is doc.metadata - the same dict ProcessItem reads effective_date back out of.
            first: str | None = text.readline() or None
            if first is not None and _FM_FENCE.match(clean_text(first).rstrip()):
                stats["lines"] += 1
                first = None
                for _ in range(_FM_MAX_LINES):
                    raw = text.readline()
                    if not raw:
                        break
                    stats["lines"] += 1
                    line = clean_text(raw).rstrip()
                    if _FM_FENCE.match(line):
                        break
                    if (m := _FM_PAIR.match(line)) and (v := _front_matter_value(m.group(1).lower(), m.group(2))):
                        stats[m.group(1).lower()] = v
            for raw in (text if first is None else itertools.chain([first], text)):
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
