"""Mutation tests: change one fact in a golden case (in memory) and check the answer changes as it should.

    python usecase1/checks/mutation_tests.py

Proves the golden runner is not passing by accident. Nothing on disk is modified.
"""

from __future__ import annotations

import common
import run_golden as rg
from rdflib import Graph, URIRef

shapes = Graph().parse(common.SHAPES_TTL)
bases = {v: rg._base(v) for v in common.VERSION_FILES}


def ev(case: str, app: str, version: str, old: str = "", new: str = "") -> list[str]:
    text = (common.GOLDEN_DIR / f"{case}.ttl").read_text(encoding="utf-8")
    if old:
        assert text.count(old) >= 1, (case, old)
        text = text.replace(old, new, 1)
    g = bases[version] + Graph().parse(data=text, format="turtle")
    g.add((rg.APP[app], rg.UC1.governingVersion, URIRef(common.VERSION_IRIS[version])))
    return rg.missing_ids(g, shapes)[0]


cases = [
    ("G04 baseline", "G04", "APP-1004", "2026.1", "", "", []),
    ("G04 P&L unsigned", "G04", "APP-1004", "2026.1",
     "uc1:monthsBeforeReceipt 3 ;\n    uc1:signature [ uc1:signed true ]",
     "uc1:monthsBeforeReceipt 3 ;\n    uc1:signature [ uc1:signed false ]", ["R4"]),
    ("G04 P&L unsigned, v2025.1 (no signature rule)", "G04", "APP-1004", "2025.1",
     "uc1:monthsBeforeReceipt 3 ;\n    uc1:signature [ uc1:signed true ]",
     "uc1:monthsBeforeReceipt 3 ;\n    uc1:signature [ uc1:signed false ]", []),
    ("G02 one bank statement re-typed as Paystub", "G02", "APP-1002", "2026.1",
     "uc1:docType uc1tx:BankStatement", "uc1:docType uc1tx:Paystub", ["R5"]),
    ("G02 employment changed to self-employed", "G02", "APP-1002", "2026.1",
     "uc1:employmentType uc1tx:Salaried", "uc1:employmentType uc1tx:SelfEmployed", ["R4"]),
    ("G05 hardship changed to Medical (R8 no longer applies)", "G05", "APP-1005", "2026.1",
     "uc1:hardshipReason uc1tx:Unemployment", "uc1:hardshipReason uc1tx:Medical", []),
    ("G06 rental flag off (R7 no longer applies)", "G06", "APP-1006", "2026.1",
     "uc1:hasRentalIncome true", "uc1:hasRentalIncome false", []),
]
report = common.header("Mutation tests (golden cases, in memory)")
for label, case, app, version, old, new, want in cases:
    got = ev(case, app, version, old, new)
    report.check(got == want, f"{label} @ v{version}: {got or 'none'}", f"expected {want or 'none'}")
# A threshold mutation that must change an answer: R4 90 -> 30 days makes G04's 60-day P&L too old.
v26 = common.VERSION_FILES["2026.1"].read_text(encoding="utf-8")
assert "uc1:maxAgeDays 90 ;" in v26
bases["2026.1"] = rg._base("2025.1").__class__()
for path in (common.CORE_TTL, common.taxonomy_file()):
    bases["2026.1"].parse(path, format="turtle")
bases["2026.1"].parse(data=v26.replace("uc1:maxAgeDays 90 ;", "uc1:maxAgeDays 30 ;", 1), format="turtle")
got = ev("G04", "APP-1004", "2026.1")
report.check(got == ["R4"], f"R4 tightened to 30 days -> G04 @ v2026.1: {got or 'none'}", "expected ['R4']")
raise SystemExit(report.finish())
