"""Token-budget chunker: packs prose by heading, keeps tables/records separate, streams lazily.

Output is a pure function of (sections, config) so chunk ordinals, and therefore chunk ids, are
stable across re-indexing runs.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from rag_os.domain.documents import ChunkDraft, ParsedDocument, Section, estimate_tokens
from rag_os.domain.embedding import ChunkingConfig

# Split hierarchy for oversized text: paragraph -> line -> sentence -> word (then hard character cut).
_SPLITTERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\n[ \t]*\n\s*"), "\n\n"),
    (re.compile(r"\n"), "\n"),
    (re.compile(r"(?<=[.!?;:])\s+"), " "),
    (re.compile(r"\s+"), " "),
)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")
_MD_TABLE_RULE = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


@dataclass
class _Piece:
    text: str
    heading: str
    page: int | None
    kind: str


class _Size:
    """Exact-or-over running estimate of `estimate_tokens(sep.join(parts))`."""

    __slots__ = ("chars", "words")

    def __init__(self) -> None:
        self.chars = 0
        self.words = 0

    @property
    def tokens(self) -> int:
        return max(self.chars // 4, self.words)

    def tokens_with(self, sep: str, text: str) -> int:
        chars = self.chars + (len(sep) if self.chars else 0) + len(text)
        return max(chars // 4, self.words + len(text.split()))

    def add(self, sep: str, text: str) -> None:
        self.chars += (len(sep) if self.chars else 0) + len(text)
        self.words += len(text.split())


def split_text(text: str, limit: int, level: int = 0) -> Iterator[tuple[str, str]]:
    """Yield (separator, piece) with every piece <= `limit` tokens, splitting on the coarsest boundary.

    The separator is what joins the piece to the previous one ("" for the first piece).
    """
    if estimate_tokens(text) <= limit:
        yield "", text
        return
    if level >= len(_SPLITTERS):
        step = max(1, limit * 4)
        for i in range(0, len(text), step):
            yield "", text[i : i + step]
        return
    pattern, sep = _SPLITTERS[level]
    parts = [p for p in pattern.split(text) if p.strip()]
    if len(parts) <= 1:
        yield from split_text(text, limit, level + 1)
        return
    for i, part in enumerate(parts):
        for j, (inner_sep, piece) in enumerate(split_text(part, limit, level + 1)):
            yield (inner_sep if j else (sep if i else "")), piece


def tail_text(text: str, budget: int) -> str:
    """Trailing sentences (or, failing that, words) of `text` within `budget` tokens."""
    if budget <= 0:
        return ""
    for units, joiner in ((_SENTENCE_END.split(text), " "), (text.split(), " ")):
        picked: list[str] = []
        size = _Size()
        for unit in reversed([u.strip() for u in units if u.strip()]):
            if size.tokens_with(joiner, unit) > budget:
                break
            size.add(joiner, unit)
            picked.append(unit)
        if picked:
            return joiner.join(reversed(picked))
    return ""


class _TextPacker:
    """Packs consecutive text sections with one heading_path into overlapping chunks."""

    def __init__(self, heading_path: list[str], heading: str, limit: int, overlap: int) -> None:
        self.heading_path = heading_path
        self.heading = heading
        self.limit = limit
        self.overlap = overlap
        self._reset()

    def _reset(self) -> None:
        self._parts: list[str] = []
        self._size = _Size()
        self._page: int | None = None
        self._last_page: int | None = None
        self._fresh = False  # holds content beyond carried-over overlap

    def add(self, section: Section) -> Iterator[_Piece]:
        for i, (sep, piece) in enumerate(split_text(section.text.strip(), self.limit)):
            if piece.strip():
                yield from self._push("\n\n" if i == 0 else sep, piece, section.page)

    def _push(self, sep: str, piece: str, page: int | None) -> Iterator[_Piece]:
        if self._parts and self._size.tokens_with(sep, piece) > self.limit:
            if self._fresh:
                yield self._emit()
                self._carry_overlap()
            if self._parts and self._size.tokens_with(sep, piece) > self.limit:
                self._reset()
        if not self._parts:
            self._page = page
            sep = ""
        self._parts.append(sep + piece)
        self._size.add(sep, piece)
        self._last_page = page
        self._fresh = True

    def _emit(self) -> _Piece:
        return _Piece("".join(self._parts), self.heading, self._page, "text")

    def _carry_overlap(self) -> None:
        tail = tail_text("".join(self._parts), self.overlap)
        page = self._last_page
        self._reset()
        if tail:
            self._parts = [tail]
            self._size.add("", tail)
            self._page = self._last_page = page

    def finish(self) -> Iterator[_Piece]:
        if self._fresh:
            yield self._emit()
        self._reset()


def _structured_pieces(section: Section, heading: str, max_tokens: int) -> Iterator[_Piece]:
    """Table/record section as one piece, or split by rows/records (tables repeat their header)."""
    text = section.text.strip()
    if estimate_tokens(text) <= max_tokens:
        yield _Piece(text, heading, section.page, section.kind)
        return
    header = ""
    if section.kind == "table":
        lines = [line for line in text.split("\n") if line.strip()]
        n = 2 if len(lines) > 1 and _MD_TABLE_RULE.match(lines[1].strip()) else 1
        header, units, joiner = "\n".join(lines[:n]), lines[n:], "\n"
        if estimate_tokens(header) > max_tokens // 2:  # pathological header: don't repeat it
            header, units = "", lines
    else:
        units, joiner = [u for u in re.split(r"\n[ \t]*\n", text) if u.strip()], "\n\n"
        if len(units) <= 1:
            units, joiner = [line for line in text.split("\n") if line.strip()], "\n"

    budget = max(1, max_tokens - (estimate_tokens(header) + 1 if header else 0))
    parts = [part for unit in units for _, part in split_text(unit, budget)]
    for rows in _balanced(parts, joiner, budget):
        body = joiner.join(rows)
        yield _Piece(f"{header}\n{body}" if header else body, heading, section.page, section.kind)


def _pack(parts: list[str], joiner: str, cap: int) -> list[list[str]]:
    """Greedy contiguous grouping with each group <= cap tokens (the minimal group count)."""
    groups: list[list[str]] = []
    rows: list[str] = []
    size = _Size()
    for part in parts:
        if rows and size.tokens_with(joiner, part) > cap:
            groups.append(rows)
            rows, size = [], _Size()
        rows.append(part)
        size.add(joiner, part)
    if rows:
        groups.append(rows)
    return groups


def _fits(rows: list[str], joiner: str, cap: int) -> bool:
    size = _Size()
    for row in rows:
        size.add(joiner, row)
    return size.tokens <= cap


def _balanced(parts: list[str], joiner: str, cap: int) -> list[list[str]]:
    """Same group count as greedy packing, but boundaries at even fractions of the total size so
    the last group is not a tiny remainder. Falls back to greedy if balancing would break the cap."""
    greedy = _pack(parts, joiner, cap)
    n = len(greedy)
    if n <= 1:
        return greedy
    weights = [max((len(p) + len(joiner)) / 4, len(p.split())) for p in parts]
    total = sum(weights)
    groups: list[list[str]] = []
    rows: list[str] = []
    done = 0.0
    for part, weight in zip(parts, weights, strict=True):
        if rows and done + weight / 2 > total * (len(groups) + 1) / n:
            groups.append(rows)
            rows = []
        rows.append(part)
        done += weight
    groups.append(rows)
    return groups if all(_fits(g, joiner, cap) for g in groups) else greedy


class TokenChunker:
    """Chunker port implementation (see rag_os.application.ports.Chunker)."""

    def chunk(self, doc: ParsedDocument, config: ChunkingConfig) -> Iterator[ChunkDraft]:
        ordinal = 0
        for piece in self._pieces(doc.sections, config):
            text = piece.text.strip()
            if not text:
                continue
            yield ChunkDraft(
                ordinal=ordinal,
                text=text,
                heading=piece.heading,
                page=piece.page,
                kind=piece.kind,
                token_estimate=estimate_tokens(text),
            )
            ordinal += 1

    @staticmethod
    def _pieces(sections: Iterable[Section], config: ChunkingConfig) -> Iterator[_Piece]:
        limit = max(1, config.chunk_tokens)
        overlap = max(0, min(config.overlap_tokens, limit // 2))
        max_tokens = max(limit, config.max_chunk_tokens)
        packer: _TextPacker | None = None
        for section in sections:
            path = [h.strip() for h in section.heading_path if h and h.strip()]
            heading = " > ".join(path)
            if section.kind == "text":
                if packer is None or packer.heading_path != path:
                    if packer is not None:
                        yield from packer.finish()
                    packer = _TextPacker(path, heading, limit, overlap)
                yield from packer.add(section)
                continue
            if packer is not None:
                yield from packer.finish()
                packer = None
            yield from _structured_pieces(section, heading, max_tokens)
        if packer is not None:
            yield from packer.finish()
