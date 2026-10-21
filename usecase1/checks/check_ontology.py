"""Verify the Use Case 1 OWL core against the naming contract (usecase1/README.md), then run HermiT on it.

Run from the repo root:
    uv run --native-tls --no-project --with rdflib==7.6.0 --with owlready2 python usecase1/checks/check_ontology.py

Checks: the core against the contract (vocabulary, an axiom allow-list, domains and ranges, inverses, functional
properties, PROV, OWL 2 datatypes, naming); the Protégé catalog; the per-version requirements files; then HermiT over
core + taxonomy + version files.
Options: --core PATH checks another copy of the core (used to prove the checks catch problems).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
from rdflib import BNode, Graph, Literal, Namespace, URIRef  # noqa: E402
from rdflib.collection import Collection  # noqa: E402
from rdflib.namespace import OWL, RDF, RDFS, SKOS  # noqa: E402

UC1 = Namespace(common.NS["uc1"])
PROV = Namespace("http://www.w3.org/ns/prov#")
DCT = Namespace("http://purl.org/dc/terms/")
SKOS_CONCEPT = URIRef("http://www.w3.org/2004/02/skos/core#Concept")
ONTOLOGY_IRI = URIRef("https://rag-os.example/uc1")
VERSION_IRI = URIRef("https://rag-os.example/uc1/1.0")
TAXONOMY_IRI = URIRef("https://rag-os.example/uc1/taxonomy")
CATALOG = common.ONTOLOGY_DIR / "catalog-v001.xml"
CATALOG_NS = "{urn:oasis:names:tc:entity:xmlns:xml:catalog}"


def version_ontology_iri(version: str) -> str:
    return f"https://rag-os.example/uc1/investor-a/{version}"

# --- The naming contract, hard-coded (usecase1/README.md is authoritative) -------------------------------------------

INTERFACES = {"Evidence", "Citable"}
CONCRETE_CLASSES = {
    "Investor", "GuidelineVersion", "Requirement", "ApplicabilityCondition", "RecencyRule", "GuideSection",
    "Loan", "Borrower", "Application", "ApplicationParty", "PartyRole", "SubmittedDocument",
    "Signature", "Period", "Classification", "Evaluation", "EvaluationItem", "Outcome",
}
CLASSES = INTERFACES | CONCRETE_CLASSES

# (subclass, superclass); a restriction is written "property some Class" (the only restriction form allowed)
SUBCLASS_AXIOMS = {
    ("SubmittedDocument", "Evidence"),
    ("Requirement", "Citable"),
    ("EvaluationItem", "Citable"),
    ("Evaluation", "prov:Activity"),
    ("GuidelineVersion", "prov:Entity"),
    ("EvaluationItem", "prov:Entity"),
    ("Evaluation", "prov:used some GuidelineVersion"),
    ("EvaluationItem", "prov:wasGeneratedBy some Evaluation"),
}

# External terms: declared only, so the file stays OWL 2 DL; no other axiom may mention them as subject.
EXTERNAL_DECLARATIONS = {
    (SKOS_CONCEPT, OWL.Class),
    (PROV.Activity, OWL.Class),
    (PROV.Entity, OWL.Class),
    (PROV.used, OWL.ObjectProperty),
    (PROV.wasGeneratedBy, OWL.ObjectProperty),
    (DCT.title, OWL.AnnotationProperty),
}
HEADER_PREDICATES = {RDF.type, OWL.versionIRI, OWL.versionInfo, OWL.imports, DCT.title, RDFS.comment}

# The axiom allow-list: the only predicates a uc1 term may be the subject of, and the only rdf:type objects
# (a value partition's members are also typed with their partition class).
ALLOWED_PREDICATES = {RDF.type, RDFS.label, RDFS.comment, RDFS.domain, RDFS.range, RDFS.subClassOf, OWL.inverseOf}
ALLOWED_TYPES = {OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty, OWL.AnnotationProperty, OWL.NamedIndividual,
                 OWL.FunctionalProperty}
# Blank nodes: lists, the disjointness and all-different axioms, the receivedAt union domain, the PROV restrictions.
BLANK_PREDICATES = {RDF.type, RDF.first, RDF.rest, OWL.members, OWL.distinctMembers, OWL.unionOf, OWL.onProperty,
                    OWL.someValuesFrom}
BLANK_TYPES = {OWL.AllDisjointClasses, OWL.AllDifferent, OWL.Class, OWL.Restriction}

# property -> (domain, range); a union domain is a frozenset of class names
OBJECT_PROPERTIES = {
    "guidelineVersions": ("Investor", "GuidelineVersion"),
    "issuedBy": ("GuidelineVersion", "Investor"),
    "requirements": ("GuidelineVersion", "Requirement"),
    "guidelineVersion": ("Requirement", "GuidelineVersion"),
    "documentType": ("Requirement", "skos:Concept"),
    "condition": ("Requirement", "ApplicabilityCondition"),
    "recency": ("Requirement", "RecencyRule"),
    "guideSection": ("Citable", "GuideSection"),
    "loan": ("Application", "Loan"),
    "ownedBy": ("Loan", "Investor"),
    "governingVersion": ("Application", "GuidelineVersion"),
    "hardshipReason": ("Application", "skos:Concept"),
    "parties": ("Application", "ApplicationParty"),
    "partyTo": ("ApplicationParty", "Application"),
    "borrower": ("ApplicationParty", "Borrower"),
    "role": ("ApplicationParty", "PartyRole"),
    "employmentType": ("ApplicationParty", "skos:Concept"),
    "documents": ("Application", "SubmittedDocument"),
    "application": ("SubmittedDocument", "Application"),
    "signature": ("Evidence", "Signature"),
    "period": ("Evidence", "Period"),
    "classification": ("Evidence", "Classification"),
    "docType": ("Classification", "skos:Concept"),
    "evaluations": ("Application", "Evaluation"),
    "evaluates": ("Evaluation", "Application"),
    "items": ("Evaluation", "EvaluationItem"),
    "evaluation": ("EvaluationItem", "Evaluation"),
    "requirement": ("EvaluationItem", "Requirement"),
    "satisfiedBy": ("EvaluationItem", "SubmittedDocument"),
    "outcome": ("EvaluationItem", "Outcome"),
}

DATATYPE_PROPERTIES = {
    "versionLabel": ("GuidelineVersion", "xsd:string"),
    "effectiveFromDate": ("GuidelineVersion", "xsd:dateTime"),
    "effectiveToDate": ("GuidelineVersion", "xsd:dateTime"),
    "requirementId": ("Requirement", "xsd:string"),
    "sectionNumber": ("GuideSection", "xsd:string"),
    "maxAgeDays": ("RecencyRule", "xsd:nonNegativeInteger"),
    "signatureRequired": ("RecencyRule", "xsd:boolean"),
    "minCount": ("RecencyRule", "xsd:positiveInteger"),
    "mostRecentMonths": ("RecencyRule", "xsd:positiveInteger"),
    "lienPosition": ("Loan", "xsd:positiveInteger"),
    "propertyState": ("Loan", "xsd:string"),
    "displayName": ("Borrower", "xsd:string"),
    "receivedAt": (frozenset({"Application", "SubmittedDocument"}), "xsd:dateTime"),
    "foreclosureSaleDate": ("Application", "xsd:dateTime"),
    "hasRentalIncome": ("ApplicationParty", "xsd:boolean"),
    "documentDate": ("Evidence", "xsd:dateTime"),
    "signed": ("Signature", "xsd:boolean"),
    "signatureDate": ("Signature", "xsd:dateTime"),
    "startDate": ("Period", "xsd:dateTime"),
    "endDate": ("Period", "xsd:dateTime"),
    "confidence": ("Classification", "xsd:decimal"),
    "classificationMethod": ("Classification", "xsd:string"),
    "reviewedBy": ("Classification", "xsd:string"),
    "ageAtReceiptDays": ("Evidence", "xsd:integer"),
    "monthsBeforeReceipt": ("Evidence", "xsd:integer"),
    "dueDate": ("Evaluation", "xsd:dateTime"),
    "evaluatedAt": ("Evaluation", "xsd:dateTime"),
    "engineVersion": ("Evaluation", "xsd:string"),
    "reason": ("EvaluationItem", "xsd:string"),
}

ANNOTATION_PROPERTIES = {"onProperty", "expectedValue"}

# Each inverse pair -> the direction stored in data and read by the template (the other one is inferred only)
STORED_DIRECTION = {
    frozenset({"guidelineVersions", "issuedBy"}): "issuedBy",
    frozenset({"requirements", "guidelineVersion"}): "guidelineVersion",
    frozenset({"parties", "partyTo"}): "parties",
    frozenset({"documents", "application"}): "documents",
    frozenset({"evaluations", "evaluates"}): "evaluations",
    frozenset({"items", "evaluation"}): "items",
}
INVERSE_PAIRS = set(STORED_DIRECTION)

# owl:FunctionalProperty: exactly these (at most one value per subject). Never the multi-valued links:
# satisfiedBy, documents, parties, employmentType, condition, items, evaluations, requirements, guidelineVersions.
FUNCTIONAL = {
    "outcome", "role", "borrower", "partyTo", "evaluation", "requirement", "guidelineVersion", "issuedBy",
    "application", "loan", "ownedBy", "signature", "period", "classification", "docType", "documentType",
    "recency", "governingVersion", "evaluates",
}

DERIVED = {"governingVersion", "ageAtReceiptDays", "monthsBeforeReceipt"}

VALUE_PARTITIONS = {
    "PartyRole": {"PrimaryBorrower", "CoBorrower"},
    "Outcome": {"Met", "Missing", "NotApplicable"},
}

CLASS_NAME = re.compile(r"^[A-Z][A-Za-z0-9]*$")
PROPERTY_NAME = re.compile(r"^[a-z][A-Za-z0-9]*$")
VERSION_TOKEN = re.compile(r"(19|20)\d\d|v\d")
GENERIC_NAMES = {"status", "value", "data", "item"}

XSD_NS = "http://www.w3.org/2001/XMLSchema#"
# The OWL 2 datatype map (https://www.w3.org/TR/owl2-syntax/#Datatype_Maps), plus rdf:langString from RDF 1.1.
OWL2_DATATYPES = {XSD_NS + n for n in (
    "decimal", "integer", "nonNegativeInteger", "nonPositiveInteger", "positiveInteger", "negativeInteger", "long",
    "int", "short", "byte", "unsignedLong", "unsignedInt", "unsignedShort", "unsignedByte", "double", "float",
    "string", "normalizedString", "token", "language", "Name", "NCName", "NMTOKEN", "boolean", "hexBinary",
    "base64Binary", "anyURI", "dateTime", "dateTimeStamp")} | {
    "http://www.w3.org/2002/07/owl#real", "http://www.w3.org/2002/07/owl#rational",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#XMLLiteral", "http://www.w3.org/1999/02/22-rdf-syntax-ns#PlainLiteral",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString", "http://www.w3.org/2000/01/rdf-schema#Literal"}

PREFIXES = {
    common.NS["uc1"]: "",
    "http://www.w3.org/2001/XMLSchema#": "xsd:",
    "http://www.w3.org/2004/02/skos/core#": "skos:",
    "http://www.w3.org/ns/prov#": "prov:",
    "http://www.w3.org/2002/07/owl#": "owl:",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#": "rdf:",
    "http://www.w3.org/2000/01/rdf-schema#": "rdfs:",
    "http://purl.org/dc/terms/": "dct:",
}


# --- Helpers ---------------------------------------------------------------------------------------------------------


def local(term: URIRef) -> str:
    return str(term)[len(common.NS["uc1"]):]


def is_uc1(term) -> bool:
    return isinstance(term, URIRef) and str(term).startswith(common.NS["uc1"])


def short(g: Graph, node):
    """A uc1 term as its local name, other known terms as a CURIE, a union class as a frozenset of member names,
    an existential restriction as "property some Class" (any other restriction is spelled out in full)."""
    if isinstance(node, BNode):
        members = g.value(node, OWL.unionOf)
        if members is not None:
            return frozenset(short(g, m) for m in Collection(g, members))
        if (node, RDF.type, OWL.Restriction) in g:
            pairs = sorted((short(g, p), short(g, o)) for p, o in g.predicate_objects(node))
            on, some = g.value(node, OWL.onProperty), g.value(node, OWL.someValuesFrom)
            if len(pairs) == 3 and on is not None and some is not None:
                return f"{short(g, on)} some {short(g, some)}"
            return "restriction(" + "; ".join(f"{p} {o}" for p, o in pairs) + ")"
        return "_:blank"
    if isinstance(node, Literal):
        return node.n3()
    for ns, prefix in PREFIXES.items():
        if str(node).startswith(ns):
            return prefix + str(node)[len(ns):]
    return str(node)


def uc1_typed(g: Graph, rdf_type: URIRef) -> set[str]:
    return {local(s) for s in g.subjects(RDF.type, rdf_type) if is_uc1(s)}


def compare_sets(report: common.Report, label: str, actual: set, expected: set) -> None:
    missing, extra = sorted(expected - actual), sorted(actual - expected)
    report.check(not missing and not extra, label, f"missing {missing}, unexpected {extra}")


def datatypes_outside_owl2(g: Graph) -> list[str]:
    """Datatypes a graph uses (XSD IRIs as objects, e.g. ranges, and typed literals) that OWL 2 does not support."""
    used = {str(o) for o in g.objects() if isinstance(o, URIRef) and str(o).startswith(XSD_NS)}
    used |= {str(o.datatype) for o in g.objects() if isinstance(o, Literal) and o.datatype is not None}
    return sorted(short(g, URIRef(d)) for d in used - OWL2_DATATYPES)


def en_literal(g: Graph, subject: URIRef, predicate: URIRef) -> Literal | None:
    for value in g.objects(subject, predicate):
        if isinstance(value, Literal) and value.language == "en" and str(value).strip():
            return value
    return None


# --- Core checks -----------------------------------------------------------------------------------------------------


def check_core(report: common.Report, g: Graph) -> None:
    report.check((ONTOLOGY_IRI, RDF.type, OWL.Ontology) in g, "ontology IRI <https://rag-os.example/uc1>")
    report.check((ONTOLOGY_IRI, OWL.versionIRI, VERSION_IRI) in g, "versionIRI <https://rag-os.example/uc1/1.0>")
    report.check((ONTOLOGY_IRI, OWL.imports, TAXONOMY_IRI) in g, "imports <https://rag-os.example/uc1/taxonomy>")
    header_comment = en_literal(g, ONTOLOGY_IRI, RDFS.comment)
    report.check(header_comment is not None and "synthetic" in str(header_comment).lower(),
                 "ontology comment says the content is synthetic")

    # External terms: declared (so the file stays OWL 2 DL) and nothing else
    external = {(s, o) if p == RDF.type else (s, p, o) for s, p, o in g
                if isinstance(s, URIRef) and not is_uc1(s) and s != ONTOLOGY_IRI}
    compare_sets(report, "external terms are only declared: skos:Concept, prov:Activity, prov:Entity (classes), "
                 "prov:used, prov:wasGeneratedBy (object properties), dct:title (annotation)",
                 {tuple(short(g, t) for t in x) for x in external},
                 {tuple(short(g, t) for t in x) for x in EXTERNAL_DECLARATIONS})
    header = sorted(short(g, p) for p in set(g.predicates(ONTOLOGY_IRI)) - HEADER_PREDICATES)
    report.check(not header, "the ontology header uses only declaration and annotation predicates", f"{header}")

    # Datatypes: OWL 2 has no xsd:date (or xsd:time, xsd:gYear ...); HermiT, also inside Protégé, rejects them
    outside = datatypes_outside_owl2(g)
    report.check(not outside, "only OWL 2 datatypes are used (no xsd:date etc.)", f"outside the OWL 2 map: {outside}")

    # Exact vocabulary
    classes = uc1_typed(g, OWL.Class)
    object_props = uc1_typed(g, OWL.ObjectProperty)
    datatype_props = uc1_typed(g, OWL.DatatypeProperty)
    annotation_props = uc1_typed(g, OWL.AnnotationProperty)
    compare_sets(report, f"uc1 classes = contract ({len(CLASSES)})", classes, CLASSES)
    compare_sets(report, f"uc1 object properties = contract ({len(OBJECT_PROPERTIES)})",
                 object_props, set(OBJECT_PROPERTIES))
    compare_sets(report, f"uc1 datatype properties = contract ({len(DATATYPE_PROPERTIES)})",
                 datatype_props, set(DATATYPE_PROPERTIES))
    compare_sets(report, f"uc1 annotation properties = contract ({len(ANNOTATION_PROPERTIES)})",
                 annotation_props, ANNOTATION_PROPERTIES)

    # Axiom allow-list: a uc1 term is only declared, labelled, commented, given a domain/range, subclassed or
    # paired with its inverse. Anything else (equivalentClass, subPropertyOf, disjointWith, other property
    # characteristics ...) is a modelling change the contract has not made.
    not_allowed = []
    for s, p, o in g:
        if not is_uc1(s):
            continue
        if p not in ALLOWED_PREDICATES:
            not_allowed.append(f"{local(s)} {short(g, p)} {short(g, o)}")
        elif p == RDF.type and o not in ALLOWED_TYPES and local(s) not in VALUE_PARTITIONS.get(short(g, o), ()):
            not_allowed.append(f"{local(s)} a {short(g, o)}")
    report.check(not not_allowed, "axiom allow-list: uc1 terms carry only type, label, comment, domain, range, "
                 "subClassOf and inverseOf, typed only as declarations (or their value partition)",
                 "; ".join(sorted(not_allowed)))
    blank = sorted({f"{short(g, p)} {short(g, o) if p == RDF.type else ''}".strip() for s, p, o in g
                    if isinstance(s, BNode) and (p not in BLANK_PREDICATES or (p == RDF.type and o not in BLANK_TYPES))})
    report.check(not blank, "blank nodes are only lists, the disjointness/all-different axioms, the union domain "
                 "and the PROV restrictions", f"{blank}")
    functional = uc1_typed(g, OWL.FunctionalProperty)
    compare_sets(report, f"owl:FunctionalProperty = contract ({len(FUNCTIONAL)}; never the multi-valued links)",
                 functional, FUNCTIONAL)

    # Subclass axioms (interfaces, PROV classes and PROV restrictions)
    actual_subclasses = {(local(s), short(g, o)) for s, o in g.subject_objects(RDFS.subClassOf) if is_uc1(s)}
    compare_sets(report, "subclass axioms = contract (Evidence, Citable, prov:Activity, prov:Entity, "
                 "prov:used / prov:wasGeneratedBy restrictions)", actual_subclasses, SUBCLASS_AXIOMS)

    # Domain and range: present, single, and as the contract says
    properties = object_props | datatype_props
    lacking = sorted(p for p in properties
                     if g.value(UC1[p], RDFS.domain) is None or g.value(UC1[p], RDFS.range) is None)
    report.check(not lacking, "every uc1 object/datatype property has rdfs:domain and rdfs:range", f"{lacking}")
    multiple = sorted(p for p in properties
                      if len(list(g.objects(UC1[p], RDFS.domain))) > 1 or len(list(g.objects(UC1[p], RDFS.range))) > 1)
    report.check(not multiple, "no property has more than one domain or range (that would mean intersection)",
                 f"{multiple}")
    expected_dr = {**OBJECT_PROPERTIES, **DATATYPE_PROPERTIES}
    wrong = []
    for name, (domain, range_) in sorted(expected_dr.items()):
        actual = (short(g, g.value(UC1[name], RDFS.domain)), short(g, g.value(UC1[name], RDFS.range)))
        if actual != (domain, range_):
            wrong.append(f"{name}: {actual} != {(domain, range_)}")
    report.check(not wrong, "domain and range match the contract", "; ".join(wrong))
    with_dr = sorted(p for p in annotation_props
                     if g.value(UC1[p], RDFS.domain) is not None or g.value(UC1[p], RDFS.range) is not None)
    report.check(not with_dr, "annotation properties have no domain or range", f"{with_dr}")

    # Inverses: the pairs, their mirrored domains and ranges (as stated in the graph), and the stored direction
    inverse_triples = list(g.subject_objects(OWL.inverseOf))
    compare_sets(report, f"inverse pairs = contract ({len(INVERSE_PAIRS)})",
                 {tuple(sorted((short(g, s), short(g, o)))) for s, o in inverse_triples},
                 {tuple(sorted(p)) for p in INVERSE_PAIRS})
    mirrored = []
    for s, o in inverse_triples:
        s_dr = (short(g, g.value(s, RDFS.domain)), short(g, g.value(s, RDFS.range)))
        o_dr = (short(g, g.value(o, RDFS.domain)), short(g, g.value(o, RDFS.range)))
        if s_dr != tuple(reversed(o_dr)):
            mirrored.append(f"{short(g, s)} {s_dr} / {short(g, o)} {o_dr}")
    report.check(not mirrored, "each inverse's domain and range (in the graph) mirror its pair", "; ".join(mirrored))
    unstated = sorted(name for pair, stored in STORED_DIRECTION.items() for name in pair
                      if f"Stored direction: uc1:{stored};" not in str(en_literal(g, UC1[name], RDFS.comment) or ""))
    report.check(not unstated, "both members of each inverse pair name the stored direction in their comment",
                 f"{unstated}")

    # Date-time naming: a calendar date ends in Date, a timestamp in At (mirrors the SQL _date / _at columns)
    badly_named = sorted(p for p in datatype_props
                         if g.value(UC1[p], RDFS.range) == URIRef(XSD_NS + "dateTime")
                         and not (p.endswith("Date") or p.endswith("At")))
    report.check(not badly_named, "every xsd:dateTime property's name ends in Date or At", f"{badly_named}")

    # Labels and comments
    terms = classes | object_props | datatype_props | annotation_props
    unlabelled = sorted(t for t in terms
                        if en_literal(g, UC1[t], RDFS.label) is None or en_literal(g, UC1[t], RDFS.comment) is None)
    report.check(not unlabelled, "every uc1 class and property has rdfs:label@en and rdfs:comment@en", f"{unlabelled}")
    not_interface = sorted(c for c in INTERFACES
                           if not str(en_literal(g, UC1[c], RDFS.comment) or "").startswith("Interface:"))
    report.check(not not_interface, "interface comments start with \"Interface:\"", f"{not_interface}")
    not_derived = sorted(p for p in DERIVED
                         if not str(en_literal(g, UC1[p], RDFS.comment) or "").startswith("Derived:"))
    report.check(not not_derived, "derived properties' comments start with \"Derived:\"", f"{not_derived}")
    pii = str(en_literal(g, UC1.displayName, RDFS.comment) or "")
    report.check("PII" in pii and "engine" in pii, "displayName is marked PII, never in the engine's graph")

    # Naming
    bad_classes = sorted(c for c in classes if not CLASS_NAME.match(c))
    bad_props = sorted(p for p in object_props | datatype_props | annotation_props if not PROPERTY_NAME.match(p))
    report.check(not bad_classes and not bad_props, "naming: classes UpperCamel, properties lowerCamel",
                 f"classes {bad_classes}, properties {bad_props}")
    all_uc1 = {local(t) for triple in g for t in triple if is_uc1(t)}
    versioned = sorted(n for n in all_uc1 if VERSION_TOKEN.search(n))
    report.check(not versioned, "no version-specific uc1 names (anti-pattern: a class per guide version)",
                 f"{versioned}")
    generic = sorted(n for n in all_uc1 if n in GENERIC_NAMES)
    report.check(not generic, "no generic names (status, value, data, item)", f"{generic}")

    # Disjointness: exactly the concrete classes, never the interfaces
    disjoint_sets = [{short(g, m) for m in Collection(g, g.value(node, OWL.members))}
                     for node in g.subjects(RDF.type, OWL.AllDisjointClasses)]
    report.check(len(disjoint_sets) == 1, "one owl:AllDisjointClasses axiom", f"found {len(disjoint_sets)}")
    covered = set().union(*disjoint_sets) if disjoint_sets else set()
    compare_sets(report, f"owl:AllDisjointClasses covers exactly the {len(CONCRETE_CLASSES)} concrete classes",
                 covered, CONCRETE_CLASSES)
    report.check(not covered & INTERFACES, "interfaces Evidence and Citable are not in the disjointness axiom",
                 f"{sorted(covered & INTERFACES)}")

    # Value partitions
    individuals = uc1_typed(g, OWL.NamedIndividual)
    compare_sets(report, "uc1 named individuals = the value partitions' members", individuals,
                 set().union(*VALUE_PARTITIONS.values()))
    different_sets = [{short(g, m) for m in Collection(g, g.value(node, OWL.distinctMembers))}
                      for node in g.subjects(RDF.type, OWL.AllDifferent)]
    for cls, members in VALUE_PARTITIONS.items():
        typed = {local(s) for s in g.subjects(RDF.type, UC1[cls]) if is_uc1(s)}
        report.check(typed == members and members in different_sets,
                     f"value partition {cls} = {sorted(members)}, all different",
                     f"typed {sorted(typed)}, AllDifferent sets {different_sets}")


# --- Protégé catalog ---------------------------------------------------------------------------------------------------


def check_catalog(report: common.Report) -> None:
    """catalog-v001.xml maps each ontology IRI to the right local file, and every target exists."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.parse(CATALOG).getroot()
    except Exception as exc:  # noqa: BLE001 - any parse error is a failure to report
        report.check(False, f"{CATALOG.name} parses", str(exc))
        return
    entries = {e.get("name"): (CATALOG.parent / e.get("uri", "")).resolve() for e in root.iter(CATALOG_NS + "uri")}
    report.check(True, f"{CATALOG.name} parses ({len(entries)} uri entries)")

    version_targets = {p.resolve() for p in common.VERSION_FILES.values()}
    missing = sorted(f"{name} -> {path.name}" for name, path in entries.items()
                     if not path.exists() and path not in version_targets)  # a version file may not be generated yet
    report.check(not missing, "every catalog target exists (a version file only once it has been generated)",
                 f"missing: {missing}")

    expected = {str(ONTOLOGY_IRI): common.CORE_TTL, str(VERSION_IRI): common.CORE_TTL,
                str(TAXONOMY_IRI): common.taxonomy_file(),
                **{version_ontology_iri(v): p for v, p in common.VERSION_FILES.items()}}
    compare_sets(report, f"catalog maps exactly the {len(expected)} expected IRIs", set(entries), set(expected))
    drift = sorted(f"{name} -> {entries[name].name}, expected {path.name}" for name, path in expected.items()
                   if name in entries and entries[name] != path.resolve())
    report.check(not drift, f"catalog targets are current (the taxonomy entry points at {common.taxonomy_file().name})",
                 "drift: " + "; ".join(drift))

    undeclared = []
    for name, path in entries.items():
        if path.exists() and path.suffix == ".ttl":
            g = Graph().parse(path, format="turtle")
            if (URIRef(name), RDF.type, OWL.Ontology) not in g and (None, OWL.versionIRI, URIRef(name)) not in g:
                undeclared.append(f"{path.name} does not declare <{name}>")
    report.check(not undeclared, "each catalog target declares the ontology (or version) IRI it is mapped from",
                 "; ".join(undeclared))


# --- Version files ---------------------------------------------------------------------------------------------------


def check_version_files(report: common.Report) -> None:
    present = {v: p for v, p in common.VERSION_FILES.items() if p.exists()}
    if not present:
        report.info("version files not present yet")
        return
    for version, path in sorted(common.VERSION_FILES.items()):
        if version not in present:
            report.info(f"{path.name} not present yet")
            continue
        g = Graph()
        try:
            g.parse(path, format="turtle")
        except Exception as exc:  # noqa: BLE001 - any parse error is a failure to report
            report.check(False, f"{path.name} parses", str(exc))
            continue
        report.check(True, f"{path.name} parses ({len(g)} triples)")
        declared = sorted(local(s) for t in (OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty,
                                             OWL.AnnotationProperty)
                          for s in g.subjects(RDF.type, t) if is_uc1(s))
        report.check(not declared, f"{path.name} declares no uc1 classes or properties (versions are data)",
                     f"{declared}")
        _check_requirements(report, version, path, g)
    if {"2025.1", "2026.1"} <= set(present):
        _check_version_diff(report)
    else:
        report.info("v2025.1 → v2026.1 diff skipped: both files are needed")
    if set(present) - {"2025.1", "2026.1"}:
        report.info(f"no expected diff is defined for {sorted(set(present) - {'2025.1', '2026.1'})}: "
                    "add one to _check_version_diff (recipe 13.1)")


# The design (UseCase1.html section 7): v2026.1 = v2025.1 with R4 tightened and R8 added; nothing else differs.
EXPECTED_REQUIREMENT_IDS = {"2025.1": {f"R{i}" for i in range(1, 8)}, "2026.1": {f"R{i}" for i in range(1, 9)}}
EXPECTED_CHANGED = {"R4"}


def _requirement_facts(g: Graph) -> dict[str, dict[str, object]]:
    """Per requirement id: its document type, condition and recency parameters (version-independent view)."""
    facts: dict[str, dict[str, object]] = {}
    for req in g.subjects(RDF.type, UC1.Requirement):
        rid = str(g.value(req, UC1.requirementId))
        cond = g.value(req, UC1.condition)
        rec = g.value(req, UC1.recency)
        facts[rid] = {
            "documentType": g.value(req, UC1.documentType),
            "condition": (g.value(cond, UC1.onProperty), g.value(cond, UC1.expectedValue)) if cond else None,
            "recency": tuple(sorted((local(pr), str(o)) for pr, o in g.predicate_objects(rec)
                                    if is_uc1(pr))) if rec else None,
        }
    return facts


def _check_requirements(report: common.Report, version: str, path: Path, g: Graph) -> None:
    tax = Graph().parse(common.taxonomy_file(), format="turtle")
    concepts = set(tax.subjects(RDF.type, SKOS.Concept))
    core = Graph().parse(common.CORE_TTL, format="turtle")
    core_props = {s for t in (OWL.ObjectProperty, OWL.DatatypeProperty) for s in core.subjects(RDF.type, t)}
    gv = URIRef(common.VERSION_IRIS[version])
    reqs = list(g.subjects(RDF.type, UC1.Requirement))
    ids = {str(g.value(r, UC1.requirementId)) for r in reqs}
    report.check(ids == EXPECTED_REQUIREMENT_IDS[version], f"{path.name}: requirement ids are {sorted(ids)}",
                 f"expected {sorted(EXPECTED_REQUIREMENT_IDS[version])}")
    report.check((gv, RDF.type, UC1.GuidelineVersion) in g and g.value(gv, UC1.effectiveFromDate) is not None
                 and g.value(gv, UC1.issuedBy) is not None,
                 f"{path.name}: guideline version {version} has an issuer and an effective-from date")
    problems: list[str] = []
    for r in reqs:
        rid = g.value(r, UC1.requirementId)
        name = local(r)
        if not g.value(r, RDFS.label):
            problems.append(f"{name}: no label")
        if g.value(r, UC1.guidelineVersion) != gv:
            problems.append(f"{name}: guidelineVersion is not {version}")
        if g.value(r, UC1.documentType) not in concepts:
            problems.append(f"{name}: documentType not in the taxonomy")
        sec = g.value(r, UC1.guideSection)
        if sec is None or (sec, RDF.type, UC1.GuideSection) not in g or not g.value(sec, RDFS.seeAlso):
            problems.append(f"{name}: no guide section with rdfs:seeAlso")
        elif str(g.value(sec, UC1.sectionNumber)) not in str(g.value(r, RDFS.label)):
            problems.append(f"{name}: label does not cite its section number")
        if rid is None or not local(r).endswith(str(rid)):
            problems.append(f"{name}: requirementId does not match the IRI")
        cond = g.value(r, UC1.condition)
        if cond is not None:
            on = g.value(cond, UC1.onProperty)
            exp = g.value(cond, UC1.expectedValue)
            if (cond, RDF.type, UC1.ApplicabilityCondition) not in g or on not in core_props or exp is None:
                problems.append(f"{name}: condition must be an ApplicabilityCondition on a core property with a value")
            elif isinstance(exp, URIRef) and exp not in concepts:
                problems.append(f"{name}: condition value {local(exp)} not in the taxonomy")
        rec = g.value(r, UC1.recency)
        if rec is not None and (rec, RDF.type, UC1.RecencyRule) not in g:
            problems.append(f"{name}: recency is not a RecencyRule")
    report.check(not problems, f"{path.name}: every requirement is complete (label, version, taxonomy type, "
                 "cited section, valid condition and recency)", "; ".join(problems))


def _check_version_diff(report: common.Report) -> None:
    a = _requirement_facts(Graph().parse(common.VERSION_FILES["2025.1"], format="turtle"))
    b = _requirement_facts(Graph().parse(common.VERSION_FILES["2026.1"], format="turtle"))
    changed = {rid for rid in set(a) & set(b) if a[rid] != b[rid]}
    added, removed = set(b) - set(a), set(a) - set(b)
    report.check(changed == EXPECTED_CHANGED and added == {"R8"} and not removed,
                 "v2025.1 → v2026.1 differ exactly as the design says (R4 changed, R8 added)",
                 f"changed {sorted(changed)}, added {sorted(added)}, removed {sorted(removed)}")
    report.info(f"R4 recency v2025.1 {a['R4']['recency']} → v2026.1 {b['R4']['recency']}")


# --- Reasoner --------------------------------------------------------------------------------------------------------


def check_reasoner(report: common.Report, core_path: Path = common.CORE_TTL) -> None:
    try:
        import owlready2
    except ImportError:
        report.check(False, "owlready2 is installed", "run with: uv run --no-project --with owlready2 ...")
        return
    try:
        from importlib.metadata import version as dist_version
        owlready_version = dist_version("owlready2")
    except Exception:  # noqa: BLE001
        owlready_version = getattr(owlready2, "VERSION", "unknown")
    report.info(f"owlready2 {owlready_version} (HermiT)")

    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        java = Path(java_home) / "bin" / ("java.exe" if os.name == "nt" else "java")
        if java.exists():
            owlready2.JAVA_EXE = str(java)
    report.info(f"java: {owlready2.JAVA_EXE}")

    merged = Graph()
    merged.parse(core_path, format="turtle")
    parts = [core_path.name]
    taxonomy = common.taxonomy_file()
    if taxonomy.exists():
        merged.parse(taxonomy, format="turtle")
        parts.append(taxonomy.name)
    else:
        report.info(f"taxonomy not present yet ({taxonomy.name}): reasoning over the core alone")
    for path in common.VERSION_FILES.values():
        if path.exists():
            merged.parse(path, format="turtle")
            parts.append(path.name)
    merged.remove((None, OWL.imports, None))  # everything is merged already; nothing to fetch
    report.info(f"reasoning over {' + '.join(parts)} ({len(merged)} triples)")

    # Dates are xsd:dateTime at midnight: OWL 2 has no xsd:date, and HermiT refuses datatypes outside the OWL 2
    # datatype map. The whole closure (core, taxonomy, version files) must stay inside it.
    outside_map = datatypes_outside_owl2(merged)
    report.check(not outside_map, "only OWL 2 datatypes in the import closure (HermiT would refuse others)",
                 f"outside the OWL 2 map: {outside_map}")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        nt = Path(tmp) / "uc1-merged.nt"
        merged.serialize(nt, format="nt", encoding="utf-8")
        world = owlready2.World()
        try:
            onto = world.get_ontology("file://" + nt.as_posix()).load(format="ntriples")
            with onto:
                owlready2.sync_reasoner(world, infer_property_values=False, debug=0)
        except owlready2.OwlReadyInconsistentOntologyError as exc:
            report.check(False, "HermiT: the ontology is consistent",
                         "inconsistent (e.g. an individual typed as two disjoint classes)"
                         + (f": {str(exc).strip()[:300]}" if str(exc).strip() else ""))
            return
        except Exception as exc:  # noqa: BLE001 - Java missing, HermiT errors, parse errors
            report.check(False, "HermiT ran", f"{type(exc).__name__}: {str(exc).strip()[:500]}")
            return
        inconsistent = [str(c.iri) for c in world.inconsistent_classes() if c is not owlready2.Nothing]
        for iri in inconsistent:
            print(f"        unsatisfiable: {iri}")
        report.check(not inconsistent, "HermiT: the ontology is consistent and no class is unsatisfiable",
                     f"{len(inconsistent)} unsatisfiable classes")
        world.close()


# --- Entry point -----------------------------------------------------------------------------------------------------


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--core", type=Path, default=common.CORE_TTL, help="the core Turtle file to check")
    parser.add_argument("--no-reasoner", action="store_true", help="skip the HermiT consistency check")
    args = parser.parse_args(argv)

    report = common.header("Ontology core (uc1-core.ttl)")
    g = Graph()
    try:
        g.parse(args.core, format="turtle")
    except Exception as exc:  # noqa: BLE001
        report.check(False, f"{args.core.name} parses", str(exc))
        return report.finish()
    report.check(True, f"{args.core.name} parses ({len(g)} triples)")

    check_core(report, g)
    check_catalog(report)
    check_version_files(report)
    if args.no_reasoner:
        report.info("reasoner skipped (--no-reasoner)")
    else:
        check_reasoner(report, args.core)
    return report.finish()


if __name__ == "__main__":
    common.exit_with(run())
