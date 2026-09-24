"""XML parser via defusedxml (rejects XXE / entity expansion), streamed with iterparse.

Elements render as indented `tag[@attr=value]: text` lines. When the root has repeating children
(e.g. <items><item/>...</items>) each child is a record, grouped into kind="record" sections.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from typing import IO, Any
from xml.etree.ElementTree import Element
from xml.etree.ElementTree import ParseError as XmlSyntaxError

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeET

from rag_os.application.ports import DocumentParser
from rag_os.domain.documents import ParsedDocument, Section
from rag_os.domain.errors import ParseError
from rag_os.infrastructure.parsers._common import clean_text, record_sections
from rag_os.infrastructure.registry import PARSERS

_LOOKAHEAD = 5
_MAX_DEPTH = 100


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _squash(text: str) -> str:
    return " ".join(clean_text(text).split())


def _own_text(el: Element) -> str:
    parts = [el.text or "", *((child.tail or "") for child in el)]
    return _squash(" ".join(parts))


def _line(el: Element, depth: int, text: str) -> str:
    attrs = "".join(f"[@{_local(k)}={_squash(v)}]" for k, v in el.attrib.items())
    return "  " * depth + _local(el.tag) + attrs + (f": {text}" if text else "")


def render(el: Element, depth: int = 0) -> Iterator[str]:
    """Indented lines for an element subtree."""
    if not isinstance(el.tag, str):  # comments / processing instructions
        return
    yield _line(el, depth, _own_text(el))
    if depth >= _MAX_DEPTH:
        rest = _squash(" ".join(el.itertext()))
        if rest:
            yield "  " * (depth + 1) + rest
        return
    for child in el:
        yield from render(child, depth + 1)


def _find_title(el: Element) -> str | None:
    for node in el.iter():
        if isinstance(node.tag, str) and _local(node.tag).lower() == "title":
            text = _squash("".join(node.itertext()))
            if text:
                return text[:200]
    return None


def _walk(stream: IO[bytes], filename: str) -> Iterator[tuple[str, Element]]:
    """Yield ("start", root), ("child", <root child, complete>), ("end", root). Children are
    detached from the root after being consumed so memory stays bounded."""
    depth = 0
    root: Element | None = None
    try:
        for event, el in SafeET.iterparse(stream, events=("start", "end")):
            if event == "start":
                depth += 1
                if depth == 1:
                    root = el
                    yield "start", el
                continue
            if depth == 2 and root is not None:
                yield "child", el
                root.remove(el)
            elif depth == 1:
                yield "end", el
            depth -= 1
    except DefusedXmlException as e:
        raise ParseError(
            f"unsafe XML rejected in '{filename}': {type(e).__name__}", detail={"reason": type(e).__name__}
        ) from e
    except XmlSyntaxError as e:
        raise ParseError(f"malformed XML in '{filename}': {e}") from e


@PARSERS.register("xml", description="XML via defusedxml (XXE-safe); repeating children become records.")
class XmlParser(DocumentParser):
    extensions = (".xml",)
    content_types = ("application/xml", "text/xml")

    def parse(self, stream: IO[bytes], filename: str) -> ParsedDocument:
        walker = _walk(stream, filename)
        root: Element | None = None
        head: list[tuple[str, str]] = []  # (child tag, rendered)
        title: str | None = None
        ended = False
        # Eager lookahead: DTD-based attacks and early syntax errors fail here, not mid-pipeline.
        for kind, el in walker:
            if kind == "start":
                root = el
                continue
            if kind == "end":
                ended = True
                break
            title = title or _find_title(el)
            head.append((_local(el.tag), "\n".join(render(el))))
            if len(head) >= _LOOKAHEAD:
                break
        if root is None:
            raise ParseError(f"'{filename}' contains no XML elements")

        root_tag = _local(root.tag)
        counts = Counter(tag for tag, _ in head)
        repeating = len(head) >= 2 and counts.most_common(1)[0][1] >= min(3, len(head))
        meta: dict[str, Any] = {"root": root_tag, "mode": "records" if repeating else "document"}
        doc = ParsedDocument(title=title, metadata=meta)
        units = self._units(root, head, walker, ended)
        if repeating:
            doc.sections = record_sections((([root_tag], text) for text in units), doc.metadata)
        else:
            doc.metadata["elements"] = 0
            doc.sections = self._text_sections(units, root_tag, doc.metadata)
        return doc

    @staticmethod
    def _units(
        root: Element, head: list[tuple[str, str]], walker: Iterator[tuple[str, Element]], ended: bool
    ) -> Iterator[str]:
        if root.attrib:
            yield _line(root, 0, "")
        for _, text in head:
            yield text
        if not ended:
            for kind, el in walker:
                if kind == "child":
                    yield "\n".join(render(el))
                elif kind == "end":
                    break
        text = _own_text(root)
        if text:
            yield _line(root, 0, text) if not root.attrib else text

    @staticmethod
    def _text_sections(units: Iterator[str], root_tag: str, stats: dict[str, Any]) -> Iterator[Section]:
        for text in units:
            if text.strip():
                stats["elements"] += 1
                yield Section(text=text, heading_path=[root_tag])
