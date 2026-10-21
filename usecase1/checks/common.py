"""Shared helpers for the Use Case 1 verification scripts: paths, namespaces and a PASS/FAIL report."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

# Labels contain §, → and ✓; the Windows console defaults to cp1252.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

UC1 = Path(__file__).resolve().parents[1]
REPO = UC1.parent
TAXONOMY_DIR = UC1 / "taxonomy"
ONTOLOGY_DIR = UC1 / "ontology"
GOLDEN_DIR = UC1 / "golden"
SQL_DIR = UC1 / "sql"
DB_PATH = REPO / ".data" / "uc1.db"

SEED_TTL = TAXONOMY_DIR / "uc1-taxonomy.seed.skos.ttl"
EXPORT_TTL = TAXONOMY_DIR / "uc1-taxonomy.skos.ttl"
CORE_TTL = ONTOLOGY_DIR / "uc1-core.ttl"
SHAPES_TTL = ONTOLOGY_DIR / "uc1-requirements.shapes.ttl"
VERSION_FILES = {"2025.1": ONTOLOGY_DIR / "investor-a-2025.1.ttl", "2026.1": ONTOLOGY_DIR / "investor-a-2026.1.ttl"}

NS = {
    "uc1": "https://rag-os.example/uc1#",
    "uc1tx": "https://rag-os.example/uc1/taxonomy#",
    "uc1shape": "https://rag-os.example/uc1/shapes#",
    "ia": "https://rag-os.example/uc1/investor-a#",
    "ia25": "https://rag-os.example/uc1/investor-a/2025.1#",
    "ia26": "https://rag-os.example/uc1/investor-a/2026.1#",
    "app": "https://rag-os.example/uc1/app#",
}
VERSION_IRIS = {"2025.1": NS["ia"] + "v2025.1", "2026.1": NS["ia"] + "v2026.1"}


def taxonomy_file() -> Path:
    """The VocBench export once it exists (the source of truth); the seed until then."""
    return EXPORT_TTL if EXPORT_TTL.exists() else SEED_TTL


@dataclass
class Report:
    """Collects check results and prints them as PASS/FAIL lines."""

    title: str
    failures: list[str] = field(default_factory=list)
    passes: int = 0

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        if ok:
            self.passes += 1
            print(f"  PASS  {label}")
        else:
            self.failures.append(label)
            print(f"  FAIL  {label}" + (f": {detail}" if detail else ""))
        return ok

    def info(self, message: str) -> None:
        print(f"  info  {message}")

    def finish(self) -> int:
        status = "PASS" if not self.failures else f"FAIL ({len(self.failures)})"
        print(f"== {self.title}: {status} ({self.passes} passed)\n")
        return 0 if not self.failures else 1


def header(title: str) -> Report:
    print(f"== {title}")
    return Report(title)


def exit_with(code: int) -> None:
    sys.exit(code)
