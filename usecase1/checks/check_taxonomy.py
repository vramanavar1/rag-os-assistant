"""Verify the Use Case 1 SKOS taxonomy against the naming contract (usecase1/README.md).

    python usecase1/checks/check_taxonomy.py                  # check the export if present, else the seed
    python usecase1/checks/check_taxonomy.py --roundtrip      # compare the seed with the VocBench export
    python usecase1/checks/check_taxonomy.py --compare A B    # compare taxonomy content of two files

Default mode checks the VocBench export if present, else the seed. The contract's ids, hierarchy and top concepts
are asserted on either file; the contract's labels and synonyms only on the seed (SMEs may edit labels in VocBench).
--roundtrip compares the seed with the export (run once after the first VocBench import: the export is the source
of truth from then on, so later SME edits are expected to differ from the seed).
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, SKOS, XSD

import common

UC1TX = common.NS["uc1tx"]
MAX_DEPTH = 8  # levels, a top concept being level 1
NOTATION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
FLAT_SCHEMES = {"uc1_employment_type", "uc1_hardship_reason", "uc1_workout_option"}
LABEL_PROPS = {"prefLabel": SKOS.prefLabel, "altLabel": SKOS.altLabel, "hiddenLabel": SKOS.hiddenLabel}

# Mirrors the taxonomy table of the naming contract in usecase1/README.md. Change the README first.
# scheme -> concept id -> (prefLabel@en, altLabel@en set)
CONTRACT: dict[str, dict[str, tuple[str, frozenset[str]]]] = {
    "uc1_doc_type": {
        "Application": ("Application", frozenset()),
        "MAA": ("Mortgage assistance application",
                frozenset({"Form 710", "RMA", "request for mortgage assistance"})),
        "Income": ("Income evidence", frozenset()),
        "Paystub": ("Paystub", frozenset({"pay stub", "payslip", "earnings statement"})),
        "ProfitAndLoss": ("Profit and loss statement", frozenset({"P&L", "income statement"})),
        "BenefitLetter": ("Unemployment benefit award letter",
                          frozenset({"unemployment award letter", "unemployment insurance award letter"})),
        "Assets": ("Asset evidence", frozenset()),
        "BankStatement": ("Bank statement",
                          frozenset({"bank account statement", "checking account statement",
                                     "savings account statement"})),
        "Hardship": ("Hardship", frozenset()),
        "HardshipStatement": ("Hardship statement", frozenset({"hardship letter", "hardship affidavit"})),
        "Tax": ("Tax", frozenset()),
        "Form4506C": ("IRS Form 4506-C", frozenset({"4506-C", "tax transcript authorization"})),
        "Property": ("Property", frozenset()),
        "Lease": ("Lease agreement", frozenset({"rental agreement", "tenancy agreement"})),
    },
    "uc1_employment_type": {
        "Salaried": ("Salaried", frozenset()),
        "SelfEmployed": ("Self-employed", frozenset()),
        "Retired": ("Retired", frozenset()),
        "Unemployed": ("Unemployed", frozenset()),
    },
    "uc1_hardship_reason": {
        "Unemployment": ("Unemployment", frozenset()),
        "IncomeReduction": ("Income reduction", frozenset()),
        "Medical": ("Medical", frozenset()),
        "Divorce": ("Divorce or separation", frozenset()),
        "Death": ("Death of a borrower", frozenset()),
        "Disaster": ("Disaster", frozenset()),
    },
    "uc1_workout_option": {
        "Forbearance": ("Forbearance", frozenset()),
        "RepaymentPlan": ("Repayment plan", frozenset()),
        "Modification": ("Loan modification", frozenset()),
        "ShortSale": ("Short sale", frozenset()),
        "DeedInLieu": ("Deed-in-lieu of foreclosure", frozenset()),
    },
}
EXPECTED: dict[str, set[str]] = {scheme: set(ids) for scheme, ids in CONTRACT.items()}

# The contract hierarchy: child -> parent (skos:broader). Every other concept is a top concept.
HIERARCHY: dict[str, str] = {
    "MAA": "Application",
    "Paystub": "Income", "ProfitAndLoss": "Income", "BenefitLetter": "Income",
    "BankStatement": "Assets",
    "HardshipStatement": "Hardship",
    "Form4506C": "Tax",
    "Lease": "Property",
}
TOP_CONCEPTS: dict[str, set[str]] = {
    "uc1_doc_type": {"Application", "Income", "Assets", "Hardship", "Tax", "Property"},
    "uc1_employment_type": {"Salaried", "SelfEmployed", "Retired", "Unemployed"},
    "uc1_hardship_reason": {"Unemployment", "IncomeReduction", "Medical", "Divorce", "Death", "Disaster"},
    "uc1_workout_option": {"Forbearance", "RepaymentPlan", "Modification", "ShortSale", "DeedInLieu"},
}


# ---------------------------------------------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------------------------------------------

def local(iri: URIRef) -> str:
    """Local name of a uc1tx: IRI (or the full IRI if it is in another namespace)."""
    s = str(iri)
    return s[len(UC1TX):] if s.startswith(UC1TX) else s


def load(path: Path) -> Graph:
    g = Graph()
    g.parse(path, format="turtle")
    return g


def concepts(g: Graph) -> set[URIRef]:
    return {s for s in g.subjects(RDF.type, SKOS.Concept) if isinstance(s, URIRef)}


def concept_schemes(g: Graph) -> set[URIRef]:
    return {s for s in g.subjects(RDF.type, SKOS.ConceptScheme) if isinstance(s, URIRef)}


def membership(g: Graph, c: URIRef) -> set[URIRef]:
    """Schemes a concept belongs to: skos:inScheme plus skos:topConceptOf (a sub-property of inScheme)."""
    return set(g.objects(c, SKOS.inScheme)) | set(g.objects(c, SKOS.topConceptOf))


def broader_edges(g: Graph) -> set[tuple[URIRef, URIRef]]:
    """The hierarchy, normalised: skos:broader edges plus inverted skos:narrower edges, as (child, parent)."""
    return set(g.subject_objects(SKOS.broader)) | {(o, s) for s, o in g.subject_objects(SKOS.narrower)}


def top_pairs(g: Graph) -> tuple[set[tuple], set[tuple]]:
    """(concept, scheme) pairs stated by skos:topConceptOf and by skos:hasTopConcept."""
    return set(g.subject_objects(SKOS.topConceptOf)), {(c, s) for s, c in g.subject_objects(SKOS.hasTopConcept)}


def en(values) -> list[Literal]:
    return [v for v in values if isinstance(v, Literal) and v.language and v.language.lower() == "en"]


def lang(v: Literal) -> str | None:
    return v.language.lower() if v.language else None


def norm(v) -> str:
    """Canonical text of an RDF term for comparison: a literal is its lexical form plus its lower-cased language
    tag; xsd:string counts as a plain literal; any other datatype is kept. IRIs are shown as local names."""
    if isinstance(v, Literal):
        out = Literal(str(v)).n3()
        if v.language:
            out += "@" + v.language.lower()
        elif v.datatype is not None and v.datatype != XSD.string:
            out += f"^^<{v.datatype}>"
        return out
    return local(v) if isinstance(v, URIRef) else str(v)


def show(items) -> str:
    return ", ".join(sorted(local(i) if isinstance(i, URIRef) else str(i) for i in items)) or "-"


def diff_sets(want: set, got: set) -> str:
    return f"missing [{show(want - got)}], unexpected [{show(got - want)}]"


# ---------------------------------------------------------------------------------------------------------------
# Default mode: structural checks
# ---------------------------------------------------------------------------------------------------------------

def check_file(path: Path, report: common.Report) -> None:
    report.info(f"file: {path.relative_to(common.REPO) if path.is_relative_to(common.REPO) else path}")
    try:
        g = load(path)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a FAIL
        report.check(False, "file parses as Turtle", f"{type(exc).__name__}: {exc}")
        return
    report.check(True, "file parses as Turtle", "")
    report.info(f"{len(g)} triples")

    schemes = {name: URIRef(UC1TX + name) for name in EXPECTED}
    missing = [n for n, iri in schemes.items() if (iri, RDF.type, SKOS.ConceptScheme) not in g]
    report.check(not missing, "the 4 concept schemes exist", f"missing: {', '.join(missing)}")
    scheme_of_iri = {iri: name for name, iri in schemes.items()}

    cs = concepts(g)
    both = cs & concept_schemes(g)
    report.check(not both, "no resource is both a skos:Concept and a skos:ConceptScheme (S9)", show(both))

    bad = [n for n, iri in schemes.items() if not en(g.objects(iri, SKOS.prefLabel))]
    report.check(not bad, "every concept scheme has a prefLabel@en", ", ".join(bad))

    by_scheme: dict[str, set[str]] = defaultdict(set)
    for c in cs:
        for s in membership(g, c):
            if s in scheme_of_iri:
                by_scheme[scheme_of_iri[s]].add(local(c))
    diffs = [f"{name}: {diff_sets(want, by_scheme.get(name, set()))}"
             for name, want in EXPECTED.items() if by_scheme.get(name, set()) != want]
    report.check(not diffs, "concept ids per scheme equal the contract", "; ".join(diffs))

    bad = []
    for c in cs:
        n_pref, n_not = len(en(g.objects(c, SKOS.prefLabel))), len(list(g.objects(c, SKOS.notation)))
        if n_pref != 1 or n_not != 1:
            bad.append(f"{local(c)} (prefLabel@en x{n_pref}, notation x{n_not})")
    report.check(not bad, "every concept has exactly one prefLabel@en and one skos:notation", "; ".join(sorted(bad)))

    bad = [f"{c} has notation {show(g.objects(c, SKOS.notation))}"
           for c in cs if str(c) not in {UC1TX + str(n) for n in g.objects(c, SKOS.notation)}]
    report.check(not bad, "notation == IRI local name (in the uc1tx: namespace)", "; ".join(sorted(bad)))

    bad = [f"{local(s)}: {norm(n)}" for s, n in g.subject_objects(SKOS.notation)
           if not isinstance(n, Literal) or n.language or n.datatype not in (None, XSD.string)]
    report.check(not bad, "notations are plain literals (no language tag, no datatype other than xsd:string)",
                 "; ".join(sorted(bad)))

    bad = [f"{local(c)}: '{n}'" for c in cs for n in g.objects(c, SKOS.notation) if not NOTATION_RE.match(str(n))]
    report.check(not bad, f"notation matches {NOTATION_RE.pattern}", "; ".join(sorted(bad)))

    bad = [f"{local(c)} in [{show(membership(g, c))}]" for c in cs if len(membership(g, c)) != 1]
    report.check(not bad, "every concept is in exactly one scheme", "; ".join(sorted(bad)))
    scheme_of = {c: next(iter(membership(g, c))) for c in cs if len(membership(g, c)) == 1}

    edges = broader_edges(g)
    bad = []
    for child, parent in edges:
        if parent not in cs:
            bad.append(f"{local(child)} -> {local(parent)} (not a concept)")
        elif scheme_of.get(child) != scheme_of.get(parent):
            bad.append(f"{local(child)} -> {local(parent)} (different scheme)")
    report.check(not bad, "every broader target exists and is in the same scheme", "; ".join(sorted(bad)))

    parents: dict[URIRef, set[URIRef]] = defaultdict(set)
    for child, parent in edges:
        parents[child].add(parent)
    cycle, max_depth = hierarchy_depth(cs, parents)
    report.check(cycle is None, "the broader graph is acyclic", f"cycle through {cycle}")
    if cycle is None:
        report.check(max_depth <= MAX_DEPTH, f"hierarchy depth <= {MAX_DEPTH}", f"depth {max_depth}")
        report.info(f"hierarchy depth {max_depth} (levels)")

    bad = [f"{local(child)} -> {local(parent)}" for child, parent in edges
           if scheme_of_iri.get(scheme_of.get(child)) in FLAT_SCHEMES]
    report.check(not bad, "flat schemes have no broader", "; ".join(sorted(bad)))

    got = {f"{local(child)} -> {local(parent)}" for child, parent in edges}
    want = {f"{child} -> {parent}" for child, parent in HIERARCHY.items()}
    report.check(got == want, "hierarchy (child -> parent) equals the contract", diff_sets(want, got))

    top_of, has_top = top_pairs(g)
    tops = top_of | has_top
    bad = [local(c) for c in cs if (c, scheme_of.get(c)) not in tops and not parents.get(c)]
    report.check(not bad, "no orphans: every concept is a top concept of its scheme or has a broader",
                 ", ".join(sorted(bad)))

    no_top = [n for n, iri in schemes.items() if not any(s == iri for _, s in tops)]
    report.check(not no_top, "each scheme has at least one top concept", ", ".join(no_top))
    disagree = sorted(f"{local(c)} topConceptOf {local(s)}" for c, s in top_of - has_top) + \
        sorted(f"{local(s)} hasTopConcept {local(c)}" for c, s in has_top - top_of)
    report.check(not disagree, "hasTopConcept and topConceptOf agree", "only one side: " + "; ".join(disagree))

    diffs = []
    for name, iri in schemes.items():
        got = {local(c) for c, s in tops if s == iri}
        if got != TOP_CONCEPTS[name]:
            diffs.append(f"{name}: {diff_sets(TOP_CONCEPTS[name], got)}")
    report.check(not diffs, "top concepts per scheme equal the contract", "; ".join(diffs))

    bad = sorted(f"{local(c)} -> {show(parents[c])}" for c, _ in tops if parents.get(c))
    report.check(not bad, "top concepts have no broader", "; ".join(bad))

    check_labels(g, cs, scheme_of, report)

    bad = [local(c) for c in cs if not en(g.objects(c, SKOS.definition))]
    report.check(not bad, "every concept has a skos:definition@en", ", ".join(sorted(bad)))

    if path.resolve() == common.SEED_TTL.resolve():
        check_contract_labels(g, report)
    else:
        report.info("labels and synonyms are not compared with the contract (SMEs may edit them in VocBench)")


def check_labels(g: Graph, cs: set[URIRef], scheme_of: dict[URIRef, URIRef], report: common.Report) -> None:
    """SKOS label integrity (S13, S14) plus the RAG-OS synonym-collision rule."""
    labelled = {s for p in LABEL_PROPS.values() for s in g.subjects(p, None)}

    bad = sorted(f"{local(s)} skos:{name} {norm(v)}" for s in labelled for name, p in LABEL_PROPS.items()
                 for v in g.objects(s, p) if not isinstance(v, Literal) or not v.language)
    report.check(not bad, "every label (pref, alt, hidden) is a literal with a language tag", "; ".join(bad))

    bad = []
    for s in labelled:
        per_lang: dict[str | None, list[Literal]] = defaultdict(list)
        for v in g.objects(s, SKOS.prefLabel):
            per_lang[lang(v) if isinstance(v, Literal) else None].append(v)
        bad += [f"{local(s)} @{tag or '(none)'} x{len(vs)}" for tag, vs in per_lang.items() if len(vs) > 1]
    report.check(not bad, "at most one prefLabel per language tag, untagged counted as one tag (S14)",
                 "; ".join(sorted(bad)))

    bad = []
    for s in labelled:
        sets = {name: {norm(v) for v in g.objects(s, p)} for name, p in LABEL_PROPS.items()}
        names = list(LABEL_PROPS)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                bad += [f"{local(s)}: {v} is both {a} and {b}" for v in sorted(sets[a] & sets[b])]
    report.check(not bad, "per resource, pref, alt and hidden labels are pairwise disjoint (S13)", "; ".join(bad))

    # RAG-OS canonicalises a raw value by id, label or synonym, case-insensitively, first match wins:
    # a key shared by two concepts of one scheme silently maps to whichever comes first.
    owners: dict[tuple[URIRef, str], set[str]] = defaultdict(set)
    for c in cs:
        keys = [*(v for p in LABEL_PROPS.values() for v in g.objects(c, p)), *g.objects(c, SKOS.notation)]
        for k in keys:
            owners[(scheme_of.get(c), " ".join(str(k).split()).lower())].add(local(c))
    bad = [f"'{key}' in {local(s)}: {', '.join(sorted(who))}" for (s, key), who in owners.items() if len(who) > 1]
    report.check(not bad, "no key (pref, alt or hidden label, or notation) is shared by two concepts of one scheme",
                 "; ".join(sorted(bad)))


def check_contract_labels(g: Graph, report: common.Report) -> None:
    """Seed only: each concept's prefLabel@en and exact altLabel@en set equal the README contract."""
    bad = []
    for ids in CONTRACT.values():
        for cid, (pref, alts) in ids.items():
            c = URIRef(UC1TX + cid)
            got_pref = {str(v) for v in en(g.objects(c, SKOS.prefLabel))}
            got_alts = {str(v) for v in en(g.objects(c, SKOS.altLabel))}
            if got_pref != {pref}:
                bad.append(f"{cid} prefLabel@en: want '{pref}', got [{show(got_pref)}]")
            if got_alts != alts:
                bad.append(f"{cid} altLabel@en: {diff_sets(set(alts), got_alts)}")
    report.check(not bad, "seed: prefLabel@en and altLabel@en per concept equal the contract", "; ".join(bad))


def hierarchy_depth(nodes: set[URIRef], parents: dict[URIRef, set[URIRef]]) -> tuple[str | None, int]:
    """Return (a cycle description or None, the maximum number of levels)."""
    depth: dict[URIRef, int] = {}
    visiting: set[URIRef] = set()

    def visit(n: URIRef, path: list[URIRef]) -> int:
        if n in depth:
            return depth[n]
        if n in visiting:
            raise ValueError(" -> ".join(local(p) for p in path[path.index(n):] + [n]))
        visiting.add(n)
        d = 1 + max((visit(p, path + [n]) for p in parents.get(n, ())), default=0)
        visiting.discard(n)
        depth[n] = d
        return d

    try:
        return None, max((visit(n, []) for n in nodes | set(parents)), default=0)
    except ValueError as cycle:
        return str(cycle), 0


# ---------------------------------------------------------------------------------------------------------------
# Compare mode: taxonomy content only (VocBench adds its own metadata, which is ignored)
# ---------------------------------------------------------------------------------------------------------------

CONCEPT_PROPS = ("prefLabel", "altLabel", "hiddenLabel", "notation", "definition", "inScheme")
SCHEME_PROPS = ("prefLabel", "topConcepts")


def content(g: Graph) -> tuple[dict[URIRef, dict[str, frozenset]], dict[URIRef, dict[str, frozenset]], frozenset]:
    """Normalised content: per concept, per scheme, and the broader edges (all terms as norm() text)."""
    def vals(s, p) -> frozenset[str]:
        return frozenset(norm(v) for v in g.objects(s, p))

    per_concept = {}
    for c in concepts(g):
        per_concept[c] = {
            "prefLabel": vals(c, SKOS.prefLabel),
            "altLabel": vals(c, SKOS.altLabel),
            "hiddenLabel": vals(c, SKOS.hiddenLabel),
            "notation": vals(c, SKOS.notation),
            "definition": vals(c, SKOS.definition),
            "inScheme": frozenset(norm(s) for s in membership(g, c)),
        }
    top_of, has_top = top_pairs(g)
    per_scheme = {}
    for s in concept_schemes(g):
        per_scheme[s] = {
            "prefLabel": vals(s, SKOS.prefLabel),
            "topConcepts": frozenset(norm(c) for c, sc in top_of | has_top if sc == s),
        }
    edges = frozenset((norm(c), norm(p)) for c, p in broader_edges(g))
    return per_concept, per_scheme, edges


def fmt(values) -> str:
    return "{" + ", ".join(sorted(values)) + "}"


def compare_resources(kind: str, props: tuple[str, ...], a: dict, b: dict, report: common.Report) -> None:
    only_a, only_b = set(a) - set(b), set(b) - set(a)
    for r in sorted(only_a):
        print(f"        - {kind} only in A: {local(r)}")
    for r in sorted(only_b):
        print(f"        - {kind} only in B: {local(r)}")
    report.check(not only_a and not only_b, f"same {kind} IRIs", f"{len(only_a)} only in A, {len(only_b)} only in B")

    n = 0
    for r in sorted(set(a) & set(b)):
        for prop in props:
            if a[r][prop] != b[r][prop]:
                n += 1
                print(f"        - {local(r)} {prop}: A {fmt(a[r][prop])}  B {fmt(b[r][prop])}")
    report.check(n == 0, f"same {', '.join(props)} per {kind}", f"{n} difference(s)")


def compare(a: Path, b: Path, report: common.Report) -> None:
    report.info(f"A: {a}")
    report.info(f"B: {b}")
    report.info("literals compared by lexical form + lower-cased language tag; xsd:string equals a plain literal")
    missing = [str(p) for p in (a, b) if not p.exists()]
    if missing:
        report.check(False, "both files exist", "missing: " + ", ".join(missing))
        return
    try:
        ga, gb = load(a), load(b)
    except Exception as exc:  # noqa: BLE001
        report.check(False, "both files parse as Turtle", f"{type(exc).__name__}: {exc}")
        return
    report.check(True, "both files parse as Turtle")
    (ca, sa, ea), (cb, sb, eb) = content(ga), content(gb)

    compare_resources("concept scheme", SCHEME_PROPS, sa, sb, report)
    compare_resources("concept", CONCEPT_PROPS, ca, cb, report)

    for child, parent in sorted(ea - eb):
        print(f"        - broader only in A: {child} -> {parent}")
    for child, parent in sorted(eb - ea):
        print(f"        - broader only in B: {child} -> {parent}")
    report.check(ea == eb, "same hierarchy (broader + inverted narrower)",
                 f"{len(ea - eb)} only in A, {len(eb - ea)} only in B")


# ---------------------------------------------------------------------------------------------------------------

def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--compare", nargs=2, metavar=("A.ttl", "B.ttl"), type=Path,
                        help="compare the taxonomy content of two Turtle files")
    parser.add_argument("--roundtrip", action="store_true",
                        help="compare the seed with the VocBench export (the step 1.1u gate; run once after import)")
    args = parser.parse_args(argv)
    if args.roundtrip:
        args.compare = [common.SEED_TTL, common.EXPORT_TTL]

    if args.compare:
        report = common.header("Taxonomy compare")
        compare(*args.compare, report)
        return report.finish()

    report = common.header("Taxonomy (SKOS)")
    path = common.taxonomy_file()
    if not path.exists():
        report.check(False, "taxonomy file exists", str(path))
        return report.finish()
    report.info(f"checking {path.name}" + (" (VocBench export: the source of truth)" if path == common.EXPORT_TTL
                                          else " (seed; the VocBench export replaces it after step 1.1u)"))
    check_file(path, report)
    return report.finish()


if __name__ == "__main__":
    raise SystemExit(run())
