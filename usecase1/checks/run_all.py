"""Run every Use Case 1 Phase 1 check and print one summary.

    uv run --no-project --with rdflib==7.6.0 --with pyshacl==0.40.1 --with owlready2 --with pyyaml \
        python usecase1/checks/run_all.py [--no-reasoner]

Order: taxonomy → ontology (+ HermiT) → semantic model (SQLite) → golden cases → cross-artifact consistency.
Exit code 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import time

import check_consistency
import check_ontology
import check_sql
import check_taxonomy
import common  # noqa: F401  (sets UTF-8 console output)
import run_golden


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-reasoner", action="store_true", help="skip the HermiT consistency check (no Java)")
    args = parser.parse_args(argv)

    steps = [
        ("taxonomy", lambda: check_taxonomy.run([])),
        ("ontology", lambda: check_ontology.run(["--no-reasoner"] if args.no_reasoner else [])),
        ("semantic model", lambda: check_sql.run([])),
        ("golden cases", lambda: run_golden.run([])),
        ("consistency", lambda: check_consistency.run([])),
    ]
    results = []
    for name, step in steps:
        started = time.perf_counter()
        code = step()
        results.append((name, code, time.perf_counter() - started))

    print("== Phase 1 summary")
    for name, code, seconds in results:
        print(f"  {'PASS' if code == 0 else 'FAIL'}  {name:<15} ({seconds:.1f}s)")
    failed = [name for name, code, _ in results if code]
    print(f"== {'ALL PASS' if not failed else 'FAILED: ' + ', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
