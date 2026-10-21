"""Run every golden case under every guide version and compare the missing items with golden/expected.yaml.

    python usecase1/checks/run_golden.py [--case G01] [--show-report]

Per case and version the data graph is: core + taxonomy + that version's requirements + the case graph + one
`uc1:governingVersion` triple (the "what if this version governed" view the design's section 10 compares). The
completeness template must report exactly the expected requirement ids; the data-quality shapes must conform.
Also checks that each case's `governs` matches the guide versions' effective dates.
"""

from __future__ import annotations

import argparse
import re
from datetime import date

import common
import yaml
from pyshacl import validate
from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import SH

UC1 = Namespace(common.NS["uc1"])
APP = Namespace(common.NS["app"])
DQ_SHAPES = common.ONTOLOGY_DIR / "uc1-dataquality.shapes.ttl"
RID = re.compile(r"^(R\d+):")


def _base(version: str) -> Graph:
    g = Graph()
    for path in (common.CORE_TTL, common.taxonomy_file(), common.VERSION_FILES[version]):
        g.parse(path, format="turtle")
    return g


def _effective_ranges() -> dict[str, tuple[date, date | None]]:
    ranges = {}
    for version, path in common.VERSION_FILES.items():
        g = Graph().parse(path, format="turtle")
        gv = URIRef(common.VERSION_IRIS[version])
        start = g.value(gv, UC1.effectiveFromDate).toPython().date()
        end = g.value(gv, UC1.effectiveToDate)
        ranges[version] = (start, end.toPython().date() if end is not None else None)
    return ranges


def missing_ids(data: Graph, shapes: Graph) -> tuple[list[str], str]:
    _, report_graph, text = validate(data, shacl_graph=shapes, advanced=False, inference="none")
    ids = []
    for result in report_graph.subjects(SH.resultSeverity, SH.Violation):
        message = str(report_graph.value(result, SH.resultMessage) or "")
        match = RID.match(message)
        ids.append(match.group(1) if match else f"?{message[:40]}")
    return sorted(set(ids), key=lambda r: (len(r), r)), text


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", help="run one case only, e.g. G01")
    parser.add_argument("--show-report", action="store_true", help="print the SHACL report for each failure")
    args = parser.parse_args(argv)

    report = common.header("Golden cases (pySHACL)")
    expected = yaml.safe_load((common.GOLDEN_DIR / "expected.yaml").read_text(encoding="utf-8"))["cases"]
    shapes = Graph().parse(common.SHAPES_TTL, format="turtle")
    dq_shapes = Graph().parse(DQ_SHAPES, format="turtle")
    ranges = _effective_ranges()
    bases = {v: _base(v) for v in common.VERSION_FILES}

    case_files = sorted(p.stem for p in common.GOLDEN_DIR.glob("G*.ttl"))
    report.check(set(case_files) == set(expected), f"every golden graph has an expectation ({len(case_files)} cases)",
                 f"graphs {case_files}, expectations {sorted(expected)}")

    table = []
    for case_id in sorted(expected):
        if args.case and case_id != args.case:
            continue
        spec = expected[case_id]
        case_graph = Graph().parse(common.GOLDEN_DIR / f"{case_id}.ttl", format="turtle")
        app = APP[spec["application"]]

        received = case_graph.value(app, UC1.receivedAt).toPython().date()
        governs = [v for v, (a, b) in ranges.items() if a <= received and (b is None or received <= b)]
        report.check(governs == [str(spec["governs"])], f"{case_id}: governing version by received date is "
                     f"{spec['governs']}", f"derived {governs} for {received}")

        row = [case_id]
        for version in sorted(common.VERSION_FILES):
            data = bases[version] + case_graph
            data.add((app, UC1.governingVersion, URIRef(common.VERSION_IRIS[version])))
            dq_ok, _, dq_text = validate(data, shacl_graph=dq_shapes, advanced=False, inference="none")
            report.check(dq_ok, f"{case_id} @ v{version}: data-quality shapes conform", dq_text[:400])
            got, text = missing_ids(data, shapes)
            if version not in (spec.get("missing") or {}):
                report.check(False, f"{case_id} @ v{version}: has an expectation in expected.yaml",
                             "add the expected missing ids for this version (test-first)")
                row.append("no expectation")
                continue
            want = [str(r) for r in spec["missing"][version]]
            ok = report.check(got == want, f"{case_id} @ v{version}: missing {got or 'none'}",
                              f"expected {want or 'none'}")
            if not ok and args.show_report:
                print(text)
            row.append(", ".join(got) or "—")
        table.append(row + [spec["title"]])

    versions = sorted(common.VERSION_FILES)
    print("\n  case | " + " | ".join(f"v{v:<10}" for v in versions) + " | scenario")
    for row in table:
        case_id, results, title = row[0], row[1:-1], row[-1]
        print(f"  {case_id:<4} | " + " | ".join(f"{r:<11}" for r in results) + f" | {title[:70]}")
    print()
    return report.finish()


if __name__ == "__main__":
    raise SystemExit(run())
