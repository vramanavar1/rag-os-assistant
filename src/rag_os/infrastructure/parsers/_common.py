"""Shared helpers for parsers: text decoding, stream adapters, table/record section builders."""

from __future__ import annotations

import codecs
import io
import shutil
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from datetime import date, datetime, time
from typing import IO, Any

import charset_normalizer

from rag_os.domain.documents import Section

SAMPLE_BYTES = 64 * 1024
SPOOL_MAX_BYTES = 64 * 1024 * 1024
ROWS_PER_SECTION = 50
RECORDS_PER_SECTION = 20
MAX_SECTION_CHARS = 24_000

_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
# When charset-normalizer scores several legacy code pages equally, prefer the most common ones.
_PREFERRED = ("utf_8", "cp1252", "latin_1", "iso8859_15")


def clean_text(text: str) -> str:
    """Drop NUL characters and normalise line endings."""
    return text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")


def title_line(text: str, limit: int = 200) -> str | None:
    """First non-empty line of `text`, trimmed to `limit` characters."""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return None


def detect_encoding(sample: bytes) -> str:
    """Best-effort encoding of a byte sample (BOM, then strict UTF-8, then charset-normalizer)."""
    for bom, name in _BOMS:
        if sample.startswith(bom):
            return name
    try:
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
        return "utf-8"
    except UnicodeDecodeError:
        pass
    results = charset_normalizer.from_bytes(sample)
    if results.best() is None and b"\x00" in sample:  # stray NULs make everything look binary
        results = charset_normalizer.from_bytes(sample.replace(b"\x00", b""))
    best = results.best()
    if best is None:
        return "utf-8"
    # Coherence (language fit) is noisy on short samples, so equal-chaos candidates count as ties.
    ties = {r.encoding for r in results if abs(r.chaos - best.chaos) < 0.01}
    for name in _PREFERRED:
        if name in ties:
            return name
    return "utf-8" if best.encoding == "ascii" else best.encoding


class _PrefixedReader(io.RawIOBase):
    """Raw stream that replays already-read bytes, then continues from the source. Never closes it."""

    def __init__(self, prefix: bytes, source: IO[bytes]) -> None:
        self._prefix = memoryview(prefix)
        self._source = source

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        view = memoryview(buffer).cast("B")
        if self._prefix:
            n = min(len(view), len(self._prefix))
            view[:n] = self._prefix[:n]
            self._prefix = self._prefix[n:]
            return n
        data = self._source.read(len(view))
        if not data:
            return 0
        view[: len(data)] = data
        return len(data)


def open_text(stream: IO[bytes], *, newline: str | None = None) -> tuple[io.TextIOWrapper, str, str]:
    """Wrap a byte stream as lazily decoded text. Returns (text stream, encoding, decoded sample).

    The sample holds complete lines only (unless the whole input fits in it) and is used for sniffing.
    """
    head = stream.read(SAMPLE_BYTES) or b""
    encoding = detect_encoding(head)
    sample = head.decode(encoding, errors="replace")
    if len(head) >= SAMPLE_BYTES and "\n" in sample:
        sample = sample[: sample.rfind("\n") + 1]
    text = io.TextIOWrapper(
        io.BufferedReader(_PrefixedReader(head, stream)),
        encoding=encoding,
        errors="replace",
        newline=newline,
    )
    return text, encoding, sample


def read_all_text(stream: IO[bytes]) -> tuple[str, str]:
    """Read and decode an entire stream. Returns (text, encoding)."""
    data = stream.read() or b""
    encoding = detect_encoding(data[:SAMPLE_BYTES])
    return clean_text(data.decode(encoding, errors="replace")), encoding


def ensure_seekable(stream: IO[bytes]) -> IO[bytes]:
    """Return a seekable stream, spooling non-seekable input to memory/disk."""
    try:
        if stream.seekable():
            return stream
    except (AttributeError, OSError, ValueError):
        pass
    spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES)  # noqa: SIM115 - lives with the doc
    shutil.copyfileobj(stream, spool)
    spool.seek(0)
    return spool


def iso(value: Any) -> str | None:
    """ISO string for date-like metadata values (None when absent or not a date)."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    return None


def cell_text(value: object) -> str:
    """Render a cell value on one line, escaping the table delimiter."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        text = str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)
    elif isinstance(value, datetime):
        text = value.date().isoformat() if value.time() == time(0) else value.isoformat(sep=" ")
    elif isinstance(value, date | time):
        text = value.isoformat()
    else:
        text = str(value)
    text = clean_text(text).replace("\n", " ").replace("|", "\\|")
    return text.strip()


def render_row(cells: Sequence[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def table_sections(
    rows: Iterable[Sequence[object]],
    heading_path: list[str],
    stats: dict[str, Any],
    *,
    rows_per_section: int = ROWS_PER_SECTION,
    page: int | None = None,
) -> Iterator[Section]:
    """Group rows into table sections; the first non-empty row is the header, repeated in every block.

    Fully empty rows are skipped. `stats["rows"]` is incremented per data row.
    """
    header: str | None = None
    block: list[str] = []
    size = 0
    emitted = False
    for raw in rows:
        cells = [cell_text(v) for v in raw]
        while cells and not cells[-1]:
            cells.pop()
        if not cells:
            continue
        if header is None:
            header = render_row(cells) + "\n" + render_row(["---"] * len(cells))
            continue
        line = render_row(cells)
        block.append(line)
        size += len(line)
        stats["rows"] = stats.get("rows", 0) + 1
        if len(block) >= rows_per_section or size >= MAX_SECTION_CHARS:
            yield Section(
                text=header + "\n" + "\n".join(block), heading_path=heading_path, page=page, kind="table"
            )
            emitted = True
            block, size = [], 0
    if header is not None and (block or not emitted):
        text = header + ("\n" + "\n".join(block) if block else "")
        yield Section(text=text, heading_path=heading_path, page=page, kind="table")


def record_sections(
    records: Iterable[tuple[list[str], str]],
    stats: dict[str, Any],
    *,
    per_section: int = RECORDS_PER_SECTION,
) -> Iterator[Section]:
    """Group rendered records (heading_path, text) into record sections; consecutive paths only."""
    path: list[str] | None = None
    block: list[str] = []
    size = 0
    for rec_path, text in records:
        text = text.strip()
        if not text:
            continue
        if block and (rec_path != path or len(block) >= per_section or size >= MAX_SECTION_CHARS):
            yield Section(text="\n\n".join(block), heading_path=path or [], kind="record")
            block, size = [], 0
        path = rec_path
        block.append(text)
        size += len(text)
        stats["records"] = stats.get("records", 0) + 1
    if block:
        yield Section(text="\n\n".join(block), heading_path=path or [], kind="record")


class HeadingStack:
    """Tracks a heading hierarchy (levels may skip)."""

    def __init__(self) -> None:
        self._items: list[tuple[int, str]] = []

    def push(self, level: int, title: str) -> None:
        while self._items and self._items[-1][0] >= level:
            self._items.pop()
        self._items.append((level, title))

    @property
    def path(self) -> list[str]:
        return [t for _, t in self._items]
