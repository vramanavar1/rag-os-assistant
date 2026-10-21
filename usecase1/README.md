# Use Case 1: loss-mitigation document chase (MVP)

Everything for Use Case 1 lives in this folder, so it can be removed without trace (see [CHANGES.md](CHANGES.md)).
Design: [docs/UseCase1.html](../docs/UseCase1.html). All guideline content is **synthetic** ("Investor A Servicing
Guide v2025.1 / v2026.1"); nothing here is a real GSE, FHA or VA rule.

| Folder | What | Edited in |
|---|---|---|
| `taxonomy/` | SKOS taxonomy: the seed, and the VocBench export (source of truth) | VocBench 3 |
| `ontology/` | OWL core, one requirements file per guide version, the SHACL templates | Protégé (SHACL by an engineer) |
| `golden/` | Hand-written application graphs with the expected missing items per guide version | text editor |
| `sql/` | The semantic model: create, seed, drop scripts | text editor |
| `checks/` | Verification scripts; `run_all.py` runs everything | — |

## Run the checks

Mutation tests (in memory, nothing on disk changes):
`uv run --no-project --with rdflib==7.6.0 --with pyshacl==0.40.1 --with pyyaml python usecase1/checks/mutation_tests.py`

```powershell
# rdflib, pySHACL and owlready2 come into a throwaway environment; nothing is added to the project.
# Add --native-tls behind a TLS-inspecting proxy.
uv run --no-project --with rdflib==7.6.0 --with pyshacl==0.40.1 --with owlready2 --with pyyaml python usecase1/checks/run_all.py
```

The `uc1_*` tables live in their own SQLite file, `.data/uc1.db` (git-ignored), never in the product state database.
The same scripts are to be proven on PostgreSQL 16 in a dedicated `uc1` schema (step 1.7b; open).

---

## Naming contract

Every artifact uses exactly these names. Change a name here first, then everywhere.

### Prefixes

| Prefix | Namespace | Holds |
|---|---|---|
| `uc1:` | `https://rag-os.example/uc1#` | Classes, properties, value individuals |
| `uc1tx:` | `https://rag-os.example/uc1/taxonomy#` | Taxonomy concepts and schemes |
| `uc1shape:` | `https://rag-os.example/uc1/shapes#` | SHACL shapes |
| `ia:` | `https://rag-os.example/uc1/investor-a#` | Investor A and its guideline versions |
| `ia25:` | `https://rag-os.example/uc1/investor-a/2025.1#` | v2025.1 requirements, conditions, recency rules, sections |
| `ia26:` | `https://rag-os.example/uc1/investor-a/2026.1#` | v2026.1 requirements, conditions, recency rules, sections |
| `app:` | `https://rag-os.example/uc1/app#` | Golden-case applications, parties, documents |

Ontology IRI `https://rag-os.example/uc1`, version IRI `https://rag-os.example/uc1/1.0`.

### Taxonomy (`uc1tx:`)

Labels and synonyms use real industry document names (e.g. Form 710) so the classifier can recognise real
documents; only the *requirements* are synthetic. A concept's IRI local name = its `skos:notation` = the RAG-OS
facet value `id`. Group concepts (Application, Income, Assets, Hardship, Tax, Property) are **not** classification
targets: the phase 2 converter excludes them, and a parent match means needs-review. Each scheme's IRI local name = its
facet name. Synonyms are `skos:altLabel@en`; there is one `skos:prefLabel@en` per concept. Hierarchy uses
`skos:broader`; top concepts carry `skos:topConceptOf`.

| Scheme (facet) | Concepts (`id`: preferred label; *synonyms*) |
|---|---|
| `uc1_doc_type` (hierarchical) | **Application**: Application › **MAA**: Mortgage assistance application; *Form 710, RMA, request for mortgage assistance* · **Income**: Income evidence › **Paystub**: Paystub; *pay stub, payslip, earnings statement* · **ProfitAndLoss**: Profit and loss statement; *P&L, income statement* · **BenefitLetter**: Unemployment benefit award letter; *unemployment award letter, unemployment insurance award letter* · **Assets**: Asset evidence › **BankStatement**: Bank statement; *bank account statement, checking account statement, savings account statement* · **Hardship**: Hardship › **HardshipStatement**: Hardship statement; *hardship letter, hardship affidavit* · **Tax**: Tax › **Form4506C**: IRS Form 4506-C; *4506-C, tax transcript authorization* · **Property**: Property › **Lease**: Lease agreement; *rental agreement, tenancy agreement* |
| `uc1_employment_type` | **Salaried**: Salaried · **SelfEmployed**: Self-employed · **Retired**: Retired · **Unemployed**: Unemployed |
| `uc1_hardship_reason` | **Unemployment**: Unemployment · **IncomeReduction**: Income reduction · **Medical**: Medical · **Divorce**: Divorce or separation · **Death**: Death of a borrower · **Disaster**: Disaster |
| `uc1_workout_option` | **Forbearance**: Forbearance · **RepaymentPlan**: Repayment plan · **Modification**: Loan modification · **ShortSale**: Short sale · **DeedInLieu**: Deed-in-lieu of foreclosure |

### Ontology (`uc1:`)

**Classes.** Interfaces are `Evidence` and `Citable`. The other classes are pairwise disjoint.

| Class | Meaning | Notes |
|---|---|---|
| `Evidence` | Interface: anything submitted as proof | Has `documentDate`, `signature`, `period`, `classification` |
| `Citable` | Interface: anything that cites a guide section | Has `guideSection` |
| `Investor` | Owner of loans; publishes guides | |
| `GuidelineVersion` ⊑ `prov:Entity` | One published version of an investor's guide | |
| `Requirement` ⊑ `Citable` | A rule of one guide version that asks for a kind of document, under a condition and a recency rule, citing its section (n-ary) | |
| `ApplicabilityCondition` | When a requirement applies | Rule parameters `onProperty` and `expectedValue` (annotation properties) |
| `RecencyRule` | How recent, signed or how many | |
| `GuideSection` | A section of a guide version | `rdfs:seeAlso` points at the section |
| `Loan` | The mortgage | |
| `Borrower` | A person (identity only; PII) | |
| `Application` | One loss-mitigation application | |
| `ApplicationParty` | Object-backed link Application × Borrower | Role and circumstances as of this application |
| `PartyRole` | Value partition | Individuals `uc1:PrimaryBorrower`, `uc1:CoBorrower` |
| `SubmittedDocument` ⊑ `Evidence` | A document received | **Personal data** |
| `Signature`, `Period`, `Classification` | Structs | |
| `Evaluation` ⊑ `prov:Activity` | One completeness decision | `⊑ prov:used some GuidelineVersion` |
| `EvaluationItem` ⊑ `Citable`, `prov:Entity` | Object-backed link Evaluation × Requirement | `⊑ prov:wasGeneratedBy some Evaluation`; its `guideSection` is derived from its requirement |
| `Outcome` | Value partition | Individuals `uc1:Met`, `uc1:Missing`, `uc1:NotApplicable` |

**Object properties** (domain → range; inverse):

| Property | Domain → Range | Inverse |
|---|---|---|
| `guidelineVersions` | Investor → GuidelineVersion | `issuedBy` |
| `requirements` | GuidelineVersion → Requirement | `guidelineVersion` |
| `documentType` | Requirement → skos:Concept | |
| `condition` | Requirement → ApplicabilityCondition | |
| `recency` | Requirement → RecencyRule | |
| `guideSection` | Citable → GuideSection | |
| `loan` | Application → Loan | |
| `ownedBy` | Loan → Investor | |
| `governingVersion` | Application → GuidelineVersion | (**derived**: never stored) |
| `hardshipReason` | Application → skos:Concept (**sensitive**: medical, divorce, death) | |
| `parties` | Application → ApplicationParty | `partyTo` |
| `borrower` | ApplicationParty → Borrower | |
| `role` | ApplicationParty → PartyRole | |
| `employmentType` | ApplicationParty → skos:Concept | |
| `documents` | Application → SubmittedDocument | `application` |
| `signature` | Evidence → Signature | |
| `period` | Evidence → Period | |
| `classification` | Evidence → Classification | |
| `docType` | Classification → skos:Concept | |
| `evaluations` | Application → Evaluation | `evaluates` |
| `items` | Evaluation → EvaluationItem | `evaluation` |
| `requirement` | EvaluationItem → Requirement | |
| `satisfiedBy` | EvaluationItem → SubmittedDocument | |
| `outcome` | EvaluationItem → Outcome | |

**Functional** (at most one value): outcome, role, borrower, partyTo, evaluation, requirement, guidelineVersion, issuedBy,
application, loan, ownedBy, signature, period, classification, docType, documentType, recency, governingVersion, evaluates.
**Never functional:** satisfiedBy, documents, parties, employmentType, condition (all conditions must hold), items,
evaluations (order by `evaluatedAt`), requirements, guidelineVersions.

**Stored direction** of each inverse pair (the other is inferred only; the SHACL template reads the stored one):
`issuedBy`, `guidelineVersion`, `parties`, `documents`, `evaluations`, `items`. Look-alikes that are *not* inverse pairs:
guidelineVersions/guidelineVersion, evaluations/evaluation, requirements/requirement.

External terms declared in the core: skos:Concept, prov:Activity, prov:Entity, prov:used, prov:wasGeneratedBy. The
taxonomy file declares the SKOS/DCT terms it uses, so the whole import closure is OWL 2 DL (verified with ROBOT).

**Datatype properties** (domain → range). *(date)* means a calendar date written as `xsd:dateTime` at midnight
(`2026-01-01T00:00:00`): OWL 2 has no `xsd:date`, and HermiT (also inside Protégé) rejects it. The SQL tables keep
real `DATE` columns; the bridge writes them at midnight.

| Property | Domain → Range |
|---|---|
| `versionLabel` | GuidelineVersion → xsd:string |
| `effectiveFromDate`, `effectiveToDate` | GuidelineVersion → xsd:dateTime (date) |
| `requirementId` | Requirement → xsd:string ("R1"…"R8") |
| `sectionNumber` | GuideSection → xsd:string ("4.3.2") |
| `maxAgeDays` | RecencyRule → xsd:nonNegativeInteger |
| `signatureRequired` | RecencyRule → xsd:boolean |
| `minCount` | RecencyRule → xsd:positiveInteger (default 1) |
| `mostRecentMonths` | RecencyRule → xsd:positiveInteger: the window of whole months before the month of receipt (1 = the month before) in which periods must end; with `minCount`, that many distinct months must be covered |
| `lienPosition` | Loan → xsd:positiveInteger |
| `propertyState` | Loan → xsd:string |
| `displayName` | Borrower → xsd:string (**PII**: never put in the engine's graph) |
| `receivedAt` | Application ⊔ SubmittedDocument → xsd:dateTime |
| `foreclosureSaleDate` | Application → xsd:dateTime (date) |
| `hasRentalIncome` | ApplicationParty → xsd:boolean |
| `documentDate` | Evidence → xsd:dateTime (date) |
| `signed` | Signature → xsd:boolean (signed by every required signer; MVP: one flag) |
| `signatureDate` | Signature → xsd:dateTime (date) |
| `startDate`, `endDate` | Period → xsd:dateTime (date) |
| `confidence` | Classification → xsd:decimal |
| `classificationMethod` | Classification → xsd:string ("classifier" / "llm" / "human") |
| `reviewedBy` | Classification → xsd:string (staff personal data) |
| `ageAtReceiptDays` | Evidence → xsd:integer (**derived**) |
| `monthsBeforeReceipt` | Evidence → xsd:integer (**derived**) |
| `dueDate` | Evaluation → xsd:dateTime (date) |
| `evaluatedAt` | Evaluation → xsd:dateTime |
| `engineVersion` | Evaluation → xsd:string |
| `reason` | EvaluationItem → xsd:string |

**Annotation properties** (rule parameters read by the SHACL templates):

| Property | Meaning |
|---|---|
| `onProperty` | The `uc1:` property the condition tests, on the application or any of its parties |
| `expectedValue` | The value it must have (a taxonomy concept or a literal) |

### Derived values (computed per evaluation; never stored, except `dueDate`)

| Value | Rule |
|---|---|
| `ageAtReceiptDays` | Date part of the application's `receivedAt` − the document's `documentDate`, in days |
| `monthsBeforeReceipt` | (year × 12 + month of the application's `receivedAt`) − (year × 12 + month of the document's period `endDate`) |
| `governingVersion` | The investor's version with `effectiveFromDate` ≤ received date ≤ `effectiveToDate` (open-ended if absent) |
| `dueDate` | The 5th business day after the date part of the application's `receivedAt`, skipping Saturdays, Sundays and `uc1_holiday` (12 CFR 1024.41(b)(2)(i)); received Fri 2026-02-06 → due Fri 2026-02-13. The one derived value that is stored: frozen in `uc1_evaluation.due_date` as part of the append-only evaluation snapshot. Absent when the application is complete (no notice due) |

### Semantic model (`sql/`)

The create script expects an empty schema (run `uc1_drop.sql` first; PostgreSQL: `CREATE SCHEMA IF NOT EXISTS uc1;
SET search_path TO uc1;`). Dates end `_date` (ISO `YYYY-MM-DD`); timestamps end `_at` (UTC, `YYYY-MM-DD HH:MM:SS`); booleans are `TRUE`/`FALSE`;
taxonomy values are stored as concept ids (notations).

| Table | Columns |
|---|---|
| `uc1_investor` | `investor_id` PK, `name`, `short_code` |
| `uc1_guideline_version` | `guideline_version_id` PK (`<investor_id>/<version_label>`, e.g. `investor-a/2026.1`), `investor_id` FK, `version_label`, `effective_from_date`, `effective_to_date` (absent = open-ended; at most one open-ended per investor), `requirements_file`, `source_doc_id`; UNIQUE (`investor_id`, `version_label`), UNIQUE (`investor_id`, `effective_from_date`); an investor's versions never overlap |
| `uc1_loan` | `loan_id` PK, `investor_id` FK, `lien_position` (1–9), `property_state` (two-letter upper-case code) |
| `uc1_borrower` | `borrower_id` PK, `display_name` (PII) |
| `uc1_application` | `application_id` PK, `loan_id` FK, `received_at`, `foreclosure_sale_date`, `hardship_reason` |
| `uc1_application_party` | `application_party_id` PK, `application_id` FK, `borrower_id` FK, `role` (`primary` / `co_borrower`; exactly one `primary` per application), `employment_type`, `has_rental_income`; UNIQUE (`application_id`, `borrower_id`) |
| `uc1_submitted_document` | `submitted_document_id` PK, `application_id` FK, `received_at`, `document_date`, `period_start_date`, `period_end_date` (a start needs an end; end ≥ start), `signature_signed` (absent = not applicable), `signature_date` (only when signed), `classification_doc_type`, `classification_confidence` (0–1; required unless `human`), `classification_method` (`classifier` / `llm` / `human`; required), `classification_reviewed_by` (required when `human`), `rag_doc_id` |
| `uc1_evaluation` | `evaluation_id` PK, `application_id` FK, `guideline_version_id` FK, `due_date` (frozen notice deadline; absent = complete, no notice due), `engine_version`, `evaluated_at` |
| `uc1_evaluation_item` | `evaluation_item_id` PK, `evaluation_id` FK, `requirement_id` (`R4`: the id within the evaluation's guide version), `outcome` (`met` / `missing` / `not_applicable`), `reason` (no PII); UNIQUE (`evaluation_id`, `requirement_id`) |
| `uc1_evaluation_item_document` | (`evaluation_item_id` FK, `submitted_document_id` FK) PK: the documents that satisfied an item (two bank statements satisfy R5) |
| `uc1_holiday` | `holiday_date` PK, `name` |

## Open items carried into phase 2

- **Group concepts in the classifier.** RAG-OS `FacetDef` has no per-value `classify` flag, so the converter must
  keep group concepts (Application, Income, Assets, Hardship, Tax, Property) out of the classifier's prototypes and
  treat a parent match as needs-review. Decide how (filtered prototypes, or a separate classification facet).
- **Confidence type.** SQL stores `classification_confidence` as `DOUBLE PRECISION`; the data-quality shape expects
  `xsd:decimal`. The bridge converts it.
- **ROBOT (OWL 2 DL profile check)** was run manually during the phase 1 review; `run_all.py` does not run it.
- **Stored directions.** The bridge must emit only the stored direction of each inverse pair (see "Ontology").
