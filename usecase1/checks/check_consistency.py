"""Cross-artifact consistency: the taxonomy, ontology, shapes, golden cases and SQL describe one model.

    python usecase1/checks/check_consistency.py

1. Every uc1: term used by the version files, shapes and golden graphs is declared in uc1-core.ttl.
2. Every uc1tx: concept they reference exists in the taxonomy.
3. Every derived value written in the golden graphs equals its recomputation from the dates (README rules).
4. The seed rows (uc1_seed.sql) describe exactly the golden applications: dates, hardship, parties, documents.
5. Every SQL column maps to an ontology property (or is a key), and every mapped property exists.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date

import common
from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import OWL, RDF

UC1 = Namespace(common.NS["uc1"])
UC1TX = Namespace(common.NS["uc1tx"])
APP = Namespace(common.NS["app"])

# SQL column -> ontology term it represents ("key" = identifier or FK, carried by IRIs and links).
COLUMN_MAP: dict[str, dict[str, str]] = {
    "uc1_investor": {"investor_id": "key", "name": "rdfs:label", "short_code": "key"},
    "uc1_guideline_version": {"guideline_version_id": "key", "investor_id": "issuedBy", "version_label": "versionLabel",
                              "effective_from_date": "effectiveFromDate", "effective_to_date": "effectiveToDate",
                              "requirements_file": "key", "source_doc_id": "rdfs:seeAlso"},
    "uc1_loan": {"loan_id": "key", "investor_id": "ownedBy", "lien_position": "lienPosition",
                 "property_state": "propertyState"},
    "uc1_borrower": {"borrower_id": "key", "display_name": "displayName"},
    "uc1_application": {"application_id": "key", "loan_id": "loan", "received_at": "receivedAt",
                        "foreclosure_sale_date": "foreclosureSaleDate", "hardship_reason": "hardshipReason"},
    "uc1_application_party": {"application_party_id": "key", "application_id": "parties", "borrower_id": "borrower",
                              "role": "role", "employment_type": "employmentType",
                              "has_rental_income": "hasRentalIncome"},
    "uc1_submitted_document": {"submitted_document_id": "key", "application_id": "documents",
                               "received_at": "receivedAt", "document_date": "documentDate",
                               "period_start_date": "startDate", "period_end_date": "endDate",
                               "signature_signed": "signed", "signature_date": "signatureDate",
                               "classification_doc_type": "docType", "classification_confidence": "confidence",
                               "classification_method": "classificationMethod",
                               "classification_reviewed_by": "reviewedBy", "rag_doc_id": "rdfs:seeAlso"},
    "uc1_evaluation": {"evaluation_id": "key", "application_id": "evaluations", "guideline_version_id": "prov:used",
                       "due_date": "dueDate", "engine_version": "engineVersion", "evaluated_at": "evaluatedAt"},
    "uc1_evaluation_item": {"evaluation_item_id": "key", "evaluation_id": "items", "requirement_id": "requirement",
                            "outcome": "outcome", "reason": "reason"},
    "uc1_evaluation_item_document": {"evaluation_item_id": "key", "submitted_document_id": "satisfiedBy"},
    "uc1_holiday": {"holiday_date": "key", "name": "key"},
}


def _d(value) -> date:
    return value.toPython().date() if hasattr(value, "toPython") else date.fromisoformat(str(value)[:10])


def _terms(g: Graph, ns: str) -> set[URIRef]:
    return {t for triple in g for t in triple if isinstance(t, URIRef) and str(t).startswith(ns)}


def check_terms(report: common.Report, core: Graph, taxonomy: Graph) -> None:
    declared = {s for t in (OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty, OWL.AnnotationProperty,
                            OWL.NamedIndividual) for s in core.subjects(RDF.type, t)}
    concepts = set(taxonomy.subjects(RDF.type, URIRef("http://www.w3.org/2004/02/skos/core#Concept")))
    sources = {p.name: p for p in [*common.VERSION_FILES.values(), common.SHAPES_TTL,
                                   common.ONTOLOGY_DIR / "uc1-dataquality.shapes.ttl",
                                   *sorted(common.GOLDEN_DIR.glob("G*.ttl"))]}
    undeclared, unknown = [], []
    for name, path in sources.items():
        g = Graph().parse(path, format="turtle")
        text = path.read_text(encoding="utf-8")
        used = _terms(g, common.NS["uc1"]) | {UC1[m] for m in re.findall(r"uc1:([A-Za-z]+)", text)}
        undeclared += [f"{name}: uc1:{str(t).split('#')[1]}" for t in sorted(used) if t not in declared]
        used_tx = _terms(g, common.NS["uc1tx"]) | {UC1TX[m] for m in re.findall(r"uc1tx:([A-Za-z0-9_]+)", text)}
        unknown += [f"{name}: uc1tx:{str(t).split('#')[1]}" for t in sorted(used_tx) if t not in concepts]
    report.check(not undeclared, f"every uc1: term used in {len(sources)} files is declared in the core",
                 "; ".join(undeclared[:12]))
    report.check(not unknown, "every uc1tx: concept they reference exists in the taxonomy", "; ".join(unknown[:12]))


def check_derived(report: common.Report) -> None:
    wrong, n = [], 0
    for path in sorted(common.GOLDEN_DIR.glob("G*.ttl")):
        g = Graph().parse(path, format="turtle")
        for app in g.subjects(RDF.type, UC1.Application):
            received = _d(g.value(app, UC1.receivedAt))
            for doc in g.objects(app, UC1.documents):
                age, months = g.value(doc, UC1.ageAtReceiptDays), g.value(doc, UC1.monthsBeforeReceipt)
                ddate = g.value(doc, UC1.documentDate)
                period = g.value(doc, UC1.period)
                end = g.value(period, UC1.endDate) if period is not None else None
                if (age is None) != (ddate is None) or (months is None) != (end is None):
                    wrong.append(f"{path.stem} {str(doc).split('#')[1]}: derived value present without its date "
                                 "(or missing)")
                    continue
                if age is not None:
                    n += 1
                    expect = (received - _d(ddate)).days
                    if int(age) != expect:
                        wrong.append(f"{path.stem} {str(doc).split('#')[1]}: ageAtReceiptDays {age} != {expect}")
                if months is not None:
                    n += 1
                    e = _d(end)
                    expect = (received.year * 12 + received.month) - (e.year * 12 + e.month)
                    if int(months) != expect:
                        wrong.append(f"{path.stem} {str(doc).split('#')[1]}: monthsBeforeReceipt {months} != {expect}")
    report.check(not wrong and n > 0, f"golden derived values recompute from the dates ({n} values)",
                 "; ".join(wrong[:10]))


def _seed_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript((common.SQL_DIR / "uc1_create.sql").read_text(encoding="utf-8"))
    con.executescript((common.SQL_DIR / "uc1_seed.sql").read_text(encoding="utf-8"))
    return con


def check_seed(report: common.Report) -> None:
    seed = common.SQL_DIR / "uc1_seed.sql"
    if not seed.exists():
        report.info("seed not present yet")
        return
    con = _seed_db()
    sql_apps = {r[0] for r in con.execute("SELECT application_id FROM uc1_application")}
    golden: dict[str, Graph] = {}
    for path in sorted(common.GOLDEN_DIR.glob("G*.ttl")):
        g = Graph().parse(path, format="turtle")
        for app in g.subjects(RDF.type, UC1.Application):
            golden[str(app).split("#")[1]] = g
    report.check(sql_apps == set(golden), f"seed applications == golden applications ({len(golden)})",
                 f"only in SQL {sorted(sql_apps - set(golden))}, only in golden {sorted(set(golden) - sql_apps)}")

    diffs: list[str] = []
    for app_id, g in sorted(golden.items()):
        app = APP[app_id]
        row = con.execute("SELECT received_at, hardship_reason FROM uc1_application WHERE application_id = ?",
                          (app_id,)).fetchone()
        if row is None:
            continue
        if row[0].replace(" ", "T") != str(g.value(app, UC1.receivedAt)):
            diffs.append(f"{app_id}: received_at {row[0]} vs {g.value(app, UC1.receivedAt)}")
        if UC1TX[row[1]] != g.value(app, UC1.hardshipReason):
            diffs.append(f"{app_id}: hardship {row[1]}")
        sql_parties = {(r[0], r[1], r[2], bool(r[3])) for r in con.execute(
            "SELECT application_party_id, role, employment_type, has_rental_income FROM uc1_application_party "
            "WHERE application_id = ?", (app_id,))}
        role_map = {UC1.PrimaryBorrower: "primary", UC1.CoBorrower: "co_borrower"}
        g_parties = {(str(p).split("#")[1], role_map.get(g.value(p, UC1.role)),
                      str(g.value(p, UC1.employmentType)).split("#")[1], bool(g.value(p, UC1.hasRentalIncome).toPython()))
                     for p in g.objects(app, UC1.parties)}
        if sql_parties != g_parties:
            diffs.append(f"{app_id}: parties differ {sorted(sql_parties ^ g_parties)}")
        sql_docs = {(r[0], r[1], r[2], r[3], None if r[4] is None else bool(r[4])) for r in con.execute(
            "SELECT submitted_document_id, classification_doc_type, document_date, period_end_date, signature_signed "
            "FROM uc1_submitted_document WHERE application_id = ?", (app_id,))}
        g_docs = set()
        for d in g.objects(app, UC1.documents):
            cls = g.value(d, UC1.classification)
            period = g.value(d, UC1.period)
            sig = g.value(d, UC1.signature)
            dd = g.value(d, UC1.documentDate)
            end = g.value(period, UC1.endDate) if period is not None else None
            signed = g.value(sig, UC1.signed) if sig is not None else None
            g_docs.add((str(d).split("#")[1], str(g.value(cls, UC1.docType)).split("#")[1],
                        str(_d(dd)) if dd is not None else None, str(_d(end)) if end is not None else None,
                        None if signed is None else bool(signed.toPython())))
        if sql_docs != g_docs:
            diffs.append(f"{app_id}: documents differ {sorted(sql_docs ^ g_docs, key=str)[:4]}")
    report.check(not diffs, "seed rows match the golden graphs (received date, hardship, parties, documents)",
                 "; ".join(diffs[:8]))


def check_columns(report: common.Report, core: Graph) -> None:
    con = sqlite3.connect(":memory:")
    con.executescript((common.SQL_DIR / "uc1_create.sql").read_text(encoding="utf-8"))
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'uc1_%'")]
    declared = {str(s).split("#")[1] for t in (OWL.ObjectProperty, OWL.DatatypeProperty)
                for s in core.subjects(RDF.type, t) if str(s).startswith(common.NS["uc1"])}
    unmapped, bad = [], []
    for t in tables:
        for col in (r[1] for r in con.execute(f"PRAGMA table_info({t})")):
            term = COLUMN_MAP.get(t, {}).get(col)
            if term is None:
                unmapped.append(f"{t}.{col}")
            elif term != "key" and ":" not in term and term not in declared:
                bad.append(f"{t}.{col} -> uc1:{term}")
    report.check(not unmapped, f"every SQL column maps to an ontology term or is a key ({len(tables)} tables)",
                 ", ".join(unmapped))
    report.check(not bad, "every mapped ontology property exists in the core", ", ".join(bad))


def run(argv: list[str] | None = None) -> int:
    report = common.header("Cross-artifact consistency")
    core = Graph().parse(common.CORE_TTL, format="turtle")
    taxonomy = Graph().parse(common.taxonomy_file(), format="turtle")
    check_terms(report, core, taxonomy)
    check_derived(report)
    check_seed(report)
    check_columns(report, core)
    return report.finish()


if __name__ == "__main__":
    raise SystemExit(run())
