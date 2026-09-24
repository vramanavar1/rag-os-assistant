"""JSON family parsers: .json, .jsonl/.ndjson and .jsonp (callback-wrapped JSON; never evaluated).

Records are rendered as flattened `key.path: value` lines and grouped into kind="record" sections.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from typing import IO, Any

from rag_os.application.ports import DocumentParser
from rag_os.domain.documents import ParsedDocument
from rag_os.domain.errors import ParseError
from rag_os.infrastructure.parsers._common import clean_text, open_text, read_all_text, record_sections
from rag_os.infrastructure.registry import PARSERS

_MAX_DEPTH = 64
_MISSING = object()

_JSONP_PREFIX = re.compile(
    r"""\A\s*(?:/\*.*?\*/\s*|//[^\n]*\n\s*)*                       # leading comments, e.g. /**/
    (?:typeof\s+[\w$.]+\s*===?\s*(['"])function\1\s*&&\s*)?     # optional Express-style guard
    (?P<callback>[A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*|\[\s*(?:\d+|'[^']*'|"[^"]*")\s*\])*)
    \s*\(""",
    re.DOTALL | re.VERBOSE,
)
_JSONP_SUFFIX = re.compile(r"\)\s*;?\s*(?:/\*.*?\*/\s*|//[^\n]*\s*)*\Z", re.DOTALL)
_JSONP_TAIL = 4096


# --------------------------------------------------------------------------- rendering


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return clean_text(value).replace("\n", " ").strip()
    return str(value)


def flatten(value: Any, prefix: str = "", depth: int = 0) -> Iterator[tuple[str, str]]:
    """Yield (key path, value) pairs; scalar lists are joined, deep nesting is truncated."""
    key = prefix or "value"
    if depth > _MAX_DEPTH:
        yield key, json.dumps(value, ensure_ascii=False, default=str)[:2000]
    elif isinstance(value, dict):
        if not value:
            yield key, "{}"
        for k, v in value.items():
            name = _scalar(k)
            yield from flatten(v, f"{prefix}.{name}" if prefix else name, depth + 1)
    elif isinstance(value, list):
        if not value:
            yield key, "[]"
        elif not any(isinstance(x, dict | list) for x in value):
            yield key, ", ".join(_scalar(x) for x in value)
        else:
            for i, item in enumerate(value):
                yield from flatten(item, f"{prefix}[{i}]", depth + 1)
    else:
        yield key, _scalar(value)


def render_record(value: Any) -> str:
    return "\n".join(f"{k}: {v}" for k, v in flatten(value))


def _is_record_list(value: Any) -> bool:
    return isinstance(value, list) and len(value) > 1 and all(isinstance(x, dict) for x in value)


def _top_level_records(obj: Any) -> Iterator[tuple[list[str], str]]:
    """Top-level arrays are records; an object's arrays of objects become records under their key."""
    if isinstance(obj, list):
        for item in obj:
            yield [], render_record(item)
    elif isinstance(obj, dict):
        record_keys = [k for k, v in obj.items() if _is_record_list(v)]
        rest = {k: v for k, v in obj.items() if k not in record_keys}
        if rest:
            yield [], render_record(rest)
        for k in record_keys:
            for item in obj[k]:
                yield [_scalar(k)], render_record(item)
    else:
        yield [], render_record(obj)


def _title_of(obj: Any) -> str | None:
    if isinstance(obj, dict):
        for key in ("title", "name"):
            if isinstance(obj.get(key), str) and obj[key].strip():
                return clean_text(obj[key]).strip()[:200]
    return None


def _document(obj: Any, meta: dict[str, Any]) -> ParsedDocument:
    doc = ParsedDocument(title=_title_of(obj), metadata={**meta, "records": 0})
    doc.sections = record_sections(_top_level_records(obj), doc.metadata)
    return doc


def _loads(text: str, filename: str, what: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ParseError(
            f"invalid {what} in '{filename}' at line {e.lineno} column {e.colno}: {e.msg}",
            detail={"line": e.lineno, "column": e.colno},
        ) from e
    except RecursionError as e:
        raise ParseError(f"{what} in '{filename}' is nested too deeply") from e


# --------------------------------------------------------------------------- json lines


def parse_json_lines(lines: Iterable[str], filename: str, meta: dict[str, Any]) -> ParsedDocument:
    """Bad lines are skipped and counted; ParseError only when no line is valid JSON."""
    numbered = enumerate(lines, start=1)
    bad = 0
    first_bad: tuple[int, str] | None = None
    first: Any = _MISSING
    for lineno, line in numbered:
        if not line.strip():
            continue
        try:
            first = json.loads(line)
            break
        except (json.JSONDecodeError, RecursionError) as e:
            bad += 1
            first_bad = first_bad or (lineno, str(e))
    if first is _MISSING:
        if first_bad is not None:
            lineno, reason = first_bad
            raise ParseError(
                f"no valid JSON records in '{filename}': line {lineno}: {reason}",
                detail={"line": lineno, "bad_lines": bad},
            )
        return ParsedDocument(metadata={**meta, "records": 0, "skipped_lines": 0})

    doc = ParsedDocument(metadata={**meta, "records": 0, "skipped_lines": bad})
    if first_bad is not None:
        doc.metadata["first_skipped_line"] = first_bad[0]

    def records() -> Iterator[tuple[list[str], str]]:
        yield [], render_record(first)
        for lineno, line in numbered:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, RecursionError):
                doc.metadata["skipped_lines"] += 1
                doc.metadata.setdefault("first_skipped_line", lineno)
                continue
            yield [], render_record(value)

    doc.sections = record_sections(records(), doc.metadata)
    return doc


# --------------------------------------------------------------------------- parsers


@PARSERS.register("json", description="JSON documents (object or array) rendered as flattened records.")
class JsonParser(DocumentParser):
    extensions = (".json",)
    content_types = ("application/json", "text/json")

    def parse(self, stream: IO[bytes], filename: str) -> ParsedDocument:
        text, encoding = read_all_text(stream)
        meta = {"format": "json", "encoding": encoding}
        try:
            obj = _loads(text, filename, "JSON")
        except ParseError as e:
            if isinstance(e.__cause__, json.JSONDecodeError) and e.__cause__.msg == "Extra data":
                # concatenated documents (JSON Lines saved as .json)
                return parse_json_lines(text.splitlines(), filename, {**meta, "format": "jsonl"})
            raise
        return _document(obj, meta)


@PARSERS.register("jsonl", description="JSON Lines / NDJSON; bad lines skipped and counted.")
class JsonLinesParser(DocumentParser):
    extensions = (".jsonl", ".ndjson")
    content_types = ("application/x-ndjson", "application/jsonl", "application/x-jsonlines")

    def parse(self, stream: IO[bytes], filename: str) -> ParsedDocument:
        text, encoding, _ = open_text(stream)
        return parse_json_lines(
            (clean_text(line) for line in text), filename, {"format": "jsonl", "encoding": encoding}
        )


@PARSERS.register("jsonp", description="JSONP: callback wrapper stripped by regex (never evaluated).")
class JsonpParser(DocumentParser):
    extensions = (".jsonp",)
    content_types = ()

    def parse(self, stream: IO[bytes], filename: str) -> ParsedDocument:
        text, encoding = read_all_text(stream)
        prefix = _JSONP_PREFIX.match(text)
        tail_start = max(prefix.end() if prefix else 0, len(text) - _JSONP_TAIL)
        suffix = _JSONP_SUFFIX.search(text, tail_start) if prefix else None
        if prefix is None or suffix is None:
            raise ParseError(f"'{filename}' is not JSONP: expected callback(<json>) wrapper")
        obj = _loads(text[prefix.end() : suffix.start()], filename, "JSONP payload")
        callback = re.sub(r"\s+", "", prefix.group("callback"))
        return _document(obj, {"format": "jsonp", "encoding": encoding, "callback": callback})
