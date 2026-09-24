"""CSV/TSV parser: encoding detection, delimiter sniffing, header-repeating row blocks (streamed)."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from typing import IO, Any

from rag_os.application.ports import DocumentParser
from rag_os.domain.documents import ParsedDocument, Section
from rag_os.domain.errors import ParseError
from rag_os.infrastructure.parsers._common import open_text, table_sections
from rag_os.infrastructure.registry import PARSERS

# Large enough for legitimately big cells; bounded so an unbalanced quote cannot swallow the whole file.
FIELD_SIZE_LIMIT = 16 * 1024 * 1024
_SNIFF_LINES = 50


def sniff_delimiter(sample: str, default: str = ",") -> str:
    head = "\n".join(sample.splitlines()[:_SNIFF_LINES])
    if not head.strip():
        return default
    try:
        return csv.Sniffer().sniff(head, delimiters=",;\t|").delimiter
    except csv.Error:
        return default


@PARSERS.register("csv", description="CSV/TSV with encoding + delimiter detection; header-repeating rows.")
class CsvParser(DocumentParser):
    extensions = (".csv", ".tsv")
    content_types = ("text/csv", "application/csv", "text/tab-separated-values")

    def parse(self, stream: IO[bytes], filename: str) -> ParsedDocument:
        text, encoding, sample = open_text(stream, newline="")
        delimiter = "\t" if filename.lower().endswith(".tsv") else sniff_delimiter(sample)
        doc = ParsedDocument(metadata={"encoding": encoding, "delimiter": delimiter, "rows": 0})
        doc.sections = self._sections(text, delimiter, filename, doc.metadata)
        return doc

    @staticmethod
    def _sections(
        text: io.TextIOWrapper, delimiter: str, filename: str, stats: dict[str, Any]
    ) -> Iterator[Section]:
        csv.field_size_limit(FIELD_SIZE_LIMIT)
        reader = csv.reader((line.replace("\x00", "") for line in text), delimiter=delimiter)
        try:
            yield from table_sections(reader, [], stats)
        except csv.Error as e:
            line = reader.line_num
            raise ParseError(
                f"malformed CSV '{filename}' near line {line}: {e}", detail={"line": line}
            ) from e
        finally:
            text.close()
