"""TokenChunker tests."""

from __future__ import annotations

import io
import itertools
from collections.abc import Iterator

from rag_os.application.ports import Chunker
from rag_os.application.services.chunker import TokenChunker, split_text, tail_text
from rag_os.domain.documents import ChunkDraft, ParsedDocument, Section, estimate_tokens
from rag_os.domain.embedding import ChunkingConfig
from rag_os.infrastructure.parsers import parser_for

CFG = ChunkingConfig(chunk_tokens=100, overlap_tokens=20, max_chunk_tokens=200)


def chunk(sections: list[Section], config: ChunkingConfig = CFG) -> list[ChunkDraft]:
    return list(TokenChunker().chunk(ParsedDocument(sections=iter(sections)), config))


def sentences(n: int, start: int = 0) -> str:
    return " ".join(f"Sentence number {i} talks about topic {i} in detail." for i in range(start, start + n))


def test_implements_chunker_protocol() -> None:
    chunker: Chunker = TokenChunker()
    assert list(chunker.chunk(ParsedDocument(), CFG)) == []


def test_small_sections_with_same_heading_are_packed() -> None:
    chunks = chunk(
        [
            Section(text="Alpha paragraph.", heading_path=["Guide", "Intro"], page=2),
            Section(text="Beta paragraph.", heading_path=["Guide", "Intro"], page=3),
            Section(text="Gamma paragraph.", heading_path=["Guide", "Other"], page=3),
        ]
    )
    assert [(c.ordinal, c.heading, c.page, c.kind) for c in chunks] == [
        (0, "Guide > Intro", 2, "text"),
        (1, "Guide > Other", 3, "text"),
    ]
    assert chunks[0].text == "Alpha paragraph.\n\nBeta paragraph."
    assert chunks[0].token_estimate == estimate_tokens(chunks[0].text)


def test_long_text_split_with_overlap_within_budget() -> None:
    chunks = chunk([Section(text=sentences(60))])
    assert len(chunks) > 3
    for c in chunks:
        assert c.token_estimate <= CFG.chunk_tokens
    for prev, nxt in itertools.pairwise(chunks):
        first_sentence = nxt.text.split(". ")[0] + "."
        assert first_sentence in prev.text, "next chunk must start with the tail of the previous one"
        overlap = nxt.text[: nxt.text.index(first_sentence) + len(first_sentence)]
        assert estimate_tokens(overlap) <= CFG.overlap_tokens
    # every sentence survives
    joined = " ".join(c.text for c in chunks)
    assert all(f"Sentence number {i} " in joined for i in range(60))


def test_no_overlap_when_disabled() -> None:
    cfg = ChunkingConfig(chunk_tokens=100, overlap_tokens=0, max_chunk_tokens=200)
    chunks = chunk([Section(text=sentences(40))], cfg)
    total = sum(c.text.count("Sentence number") for c in chunks)
    assert total == 40


def test_paragraph_boundaries_preferred() -> None:
    para = sentences(6)  # ~70 tokens: fits alone, two do not
    chunks = chunk(
        [Section(text=f"{para}\n\n{sentences(6, 100)}")],
        ChunkingConfig(chunk_tokens=100, overlap_tokens=0, max_chunk_tokens=200),
    )
    assert [c.text for c in chunks] == [para, sentences(6, 100)]


def test_overlap_not_carried_across_headings() -> None:
    chunks = chunk(
        [Section(text=sentences(20), heading_path=["A"]), Section(text="Fresh start.", heading_path=["B"])]
    )
    assert chunks[-1].text == "Fresh start."
    assert chunks[-1].heading == "B"


def test_table_and_records_never_merged_with_prose() -> None:
    table = "| k | v |\n| --- | --- |\n| a | 1 |"
    chunks = chunk(
        [
            Section(text="Before the table.", heading_path=["S"]),
            Section(text=table, heading_path=["S"], kind="table"),
            Section(text="After the table.", heading_path=["S"]),
            Section(text="id: 1\n\nid: 2", heading_path=["S"], kind="record"),
        ]
    )
    assert [(c.kind, c.text) for c in chunks] == [
        ("text", "Before the table."),
        ("table", table),
        ("text", "After the table."),
        ("record", "id: 1\n\nid: 2"),
    ]
    assert [c.ordinal for c in chunks] == [0, 1, 2, 3]


def test_table_between_chunk_and_max_tokens_kept_whole() -> None:
    rows = "\n".join(f"| row {i} | value {i} |" for i in range(20))
    table = f"| name | value |\n| --- | --- |\n{rows}"
    assert CFG.chunk_tokens < estimate_tokens(table) <= CFG.max_chunk_tokens
    chunks = chunk([Section(text=table, kind="table")])
    assert [c.text for c in chunks] == [table]


def test_huge_table_split_by_rows_with_header_repeated() -> None:
    header = "| name | amount | note |\n| --- | --- | --- |"
    rows = [f"| item {i} | {i * 10} | some descriptive note {i} |" for i in range(400)]
    table = Section(text=header + "\n" + "\n".join(rows), heading_path=["Sheet1"], kind="table", page=1)
    chunks = chunk([table])
    assert len(chunks) > 5
    seen: list[str] = []
    for c in chunks:
        lines = c.text.splitlines()
        assert "\n".join(lines[:2]) == header
        assert c.kind == "table" and c.heading == "Sheet1" and c.page == 1
        assert c.token_estimate <= CFG.max_chunk_tokens
        seen.extend(lines[2:])
    assert seen == rows  # every row exactly once, in order
    sizes = [c.token_estimate for c in chunks]
    assert min(sizes) > max(sizes) // 3, "pieces are balanced, no tiny tail"


def test_huge_record_section_split_at_record_boundaries() -> None:
    records = [f"id: {i}\nname: Record {i}\ndescription: {sentences(1, i)}" for i in range(60)]
    chunks = chunk([Section(text="\n\n".join(records), kind="record")])
    assert len(chunks) > 1
    rebuilt = [r for c in chunks for r in c.text.split("\n\n")]
    assert rebuilt == records
    assert all(c.token_estimate <= CFG.max_chunk_tokens for c in chunks)


def test_whitespace_sections_skipped_and_ordinals_contiguous() -> None:
    chunks = chunk(
        [
            Section(text="   \n\n  ", heading_path=["A"]),
            Section(text="", kind="table"),
            Section(text="Real content.", heading_path=["B"]),
            Section(text="\n", kind="record"),
            Section(text="More content.", heading_path=["C"]),
        ]
    )
    assert [(c.ordinal, c.text) for c in chunks] == [(0, "Real content."), (1, "More content.")]


def test_unbreakable_token_is_hard_split() -> None:
    blob = "x" * 5000
    chunks = chunk([Section(text=blob)])
    assert "".join(c.text for c in chunks) == blob  # no word-level overlap possible inside one token
    assert all(c.token_estimate <= CFG.chunk_tokens for c in chunks)
    assert len(chunks) == -(-5000 // (CFG.chunk_tokens * 4))


def test_page_is_first_section_page() -> None:
    chunks = chunk([Section(text="Page one text.", page=1), Section(text="Page two text.", page=2)])
    assert len(chunks) == 1 and chunks[0].page == 1


def test_deterministic_output() -> None:
    def build() -> list[Section]:
        return [
            Section(text=sentences(30), heading_path=["H1"], page=1),
            Section(text="| a |\n| --- |\n" + "\n".join(f"| {i} |" for i in range(300)), kind="table"),
            Section(text=sentences(25, 50), heading_path=["H2"], page=4),
        ]

    first = [c.model_dump() for c in chunk(build())]
    second = [c.model_dump() for c in chunk(build())]
    assert first == second
    assert [c["ordinal"] for c in first] == list(range(len(first)))


def test_sections_consumed_lazily() -> None:
    pulled = 0

    def gen() -> Iterator[Section]:
        nonlocal pulled
        for i in range(1000):
            pulled += 1
            yield Section(text=sentences(5, i * 5), heading_path=[f"H{i}"])

    chunks = TokenChunker().chunk(ParsedDocument(sections=gen()), CFG)
    next(chunks)
    assert pulled < 5


def test_split_text_and_tail_helpers() -> None:
    pieces = list(split_text(sentences(30), 50))
    assert all(estimate_tokens(p) <= 50 for _, p in pieces)
    assert pieces[0][0] == "" and all(sep == " " for sep, _ in pieces[1:])
    tail = tail_text("One short. Two short. " + "word " * 200, 10)
    assert estimate_tokens(tail) <= 10 and tail.split()[-1] == "word"


def test_parser_to_chunker_end_to_end() -> None:
    md = "# Guide\n\n" + sentences(30) + "\n\n## FAQ\n\nShort answer.\n"
    doc = parser_for("guide.md").parse(io.BytesIO(md.encode()), "guide.md")
    chunks = list(TokenChunker().chunk(doc, CFG))
    assert chunks[0].heading == "Guide"
    assert chunks[-1].heading == "Guide > FAQ" and chunks[-1].text == "Short answer."
