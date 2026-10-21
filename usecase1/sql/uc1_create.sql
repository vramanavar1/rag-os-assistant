-- usecase1/sql/uc1_create.sql
-- Use Case 1 (loss-mitigation document chase): the semantic model.
--
-- SYNTHETIC MVP. Every row these tables will hold is synthetic. The tables are created by this standalone
-- script, not by product migrations, so they can be removed in one step (uc1_drop.sql; see usecase1/CHANGES.md).
--
-- Expects an EMPTY schema: run uc1_drop.sql first. There is deliberately no IF NOT EXISTS, so a re-run against
-- an existing schema fails loudly instead of silently keeping an older table definition.
--
-- Portable: runs unchanged on SQLite 3 (>= 3.23: TRUE/FALSE, partial indexes) and PostgreSQL 16.
-- PostgreSQL prerequisite, once per session:
--     CREATE SCHEMA IF NOT EXISTS uc1; SET search_path TO uc1;
-- Types used: TEXT, INTEGER, DOUBLE PRECISION, BOOLEAN, DATE, TIMESTAMP only. Ids are TEXT; no generated keys.
-- Every primary-key column is declared NOT NULL as well (SQLite would otherwise accept a NULL TEXT key).
--
-- Naming rules (usecase1/README.md, "Semantic model"):
--   * singular tables, all prefixed uc1_;
--   * dates end _date (DATE, ISO 'YYYY-MM-DD'); timestamps end _at (TIMESTAMP, stored in UTC as
--     'YYYY-MM-DD HH:MM:SS'); booleans are written TRUE / FALSE;
--   * taxonomy values are stored as concept ids (skos:notation), e.g. 'SelfEmployed', 'Paystub';
--   * qualified names (outcome, classification_confidence) instead of a generic status or value.
--
-- Facts only, each stored once. No rules live here: requirements are ontology data, not rows.
-- Deliberately absent:
--   * no resolved guide version on uc1_application: it is derived (the investor's version in effect on the
--     application's received date);
--   * no completeness status on uc1_application: it is derived by the engine.
--   Document age and months-before-receipt are derived per evaluation too, never stored.
--
-- Frozen evaluation snapshot (append-only audit; a decision must be reproducible exactly as it was made):
--   * uc1_evaluation.guideline_version_id - the guide version the engine used;
--   * uc1_evaluation_item (+ uc1_evaluation_item_document) - one outcome per requirement, and the documents
--     that satisfied it;
--   * uc1_evaluation.due_date - the deadline for the notice of missing documents (12 CFR 1024.41(b)(2)(i):
--     five business days after receipt, skipping weekends and uc1_holiday). NULL means no notice is due
--     because the application was complete at that evaluation.

-- Owner of loans; publishes servicing guides.
CREATE TABLE uc1_investor (
    investor_id             TEXT NOT NULL PRIMARY KEY,
    name                    TEXT NOT NULL,
    short_code              TEXT NOT NULL UNIQUE
);

-- One published version of an investor's guide. The id is '<investor_id>/<version_label>', e.g.
-- 'investor-a/2026.1'. effective_to_date NULL = open-ended (at most one per investor).
-- Overlapping date ranges are not expressible as a portable constraint; check_sql.py tests that invariant.
CREATE TABLE uc1_guideline_version (
    guideline_version_id    TEXT NOT NULL PRIMARY KEY,
    investor_id             TEXT NOT NULL REFERENCES uc1_investor (investor_id),
    version_label           TEXT NOT NULL,          -- e.g. '2026.1'
    effective_from_date     DATE NOT NULL,
    effective_to_date       DATE,
    requirements_file       TEXT NOT NULL,          -- the ontology file holding this version's requirements
    source_doc_id           TEXT,                   -- the guide document in RAG-OS, once ingested
    UNIQUE (investor_id, version_label),
    UNIQUE (investor_id, effective_from_date),
    CHECK (guideline_version_id = investor_id || '/' || version_label),
    CHECK (effective_to_date IS NULL OR effective_to_date >= effective_from_date)
);

-- The mortgage. MVP simplification: investor ownership is a direct link (see UseCase1.html 8.2).
CREATE TABLE uc1_loan (
    loan_id                 TEXT NOT NULL PRIMARY KEY,
    investor_id             TEXT NOT NULL REFERENCES uc1_investor (investor_id),
    lien_position           INTEGER NOT NULL CHECK (lien_position BETWEEN 1 AND 9),
    property_state          TEXT NOT NULL           -- two-letter upper-case state code, e.g. 'TX'
        CHECK (length(property_state) = 2 AND upper(property_state) = property_state)
);

-- A person: identity only. PII: display_name (column-restricted; never in the engine's graph).
-- Minimal PII: no tax id or other identifier is kept; no competency question needs one.
CREATE TABLE uc1_borrower (
    borrower_id             TEXT NOT NULL PRIMARY KEY,
    display_name            TEXT NOT NULL           -- PII
);

-- One loss-mitigation application.
CREATE TABLE uc1_application (
    application_id          TEXT NOT NULL PRIMARY KEY,
    loan_id                 TEXT NOT NULL REFERENCES uc1_loan (loan_id),
    received_at             TIMESTAMP NOT NULL,
    foreclosure_sale_date   DATE,
    hardship_reason         TEXT NOT NULL           -- taxonomy id (uc1_hardship_reason): a fact of this application
        CHECK (length(hardship_reason) > 0)
    -- deliberately absent: resolved guide version, completeness status (both derived; see header)
);

-- Object-backed link: Application x Borrower. Role and circumstances as of this application.
-- Exactly one 'primary' per application: at most one is enforced by ux_uc1_application_party_primary below;
-- at least one is an invariant tested by check_sql.py (a row-level constraint cannot see other rows).
CREATE TABLE uc1_application_party (
    application_party_id    TEXT NOT NULL PRIMARY KEY,
    application_id          TEXT NOT NULL REFERENCES uc1_application (application_id),
    borrower_id             TEXT NOT NULL REFERENCES uc1_borrower (borrower_id),
    role                    TEXT NOT NULL CHECK (role IN ('primary', 'co_borrower')),
    employment_type         TEXT NOT NULL           -- taxonomy id (uc1_employment_type), as of this application
        CHECK (length(employment_type) > 0),
    has_rental_income       BOOLEAN NOT NULL CHECK (has_rental_income IN (TRUE, FALSE)),
    UNIQUE (application_id, borrower_id)
);

-- A document received for an application (an Evidence in the ontology).
-- Structs, as prefixed column groups (the bridge maps each group to one struct):
--   period_*          -> Period         (start + end; a start needs an end, end >= start)
--   signature_*       -> Signature      (signed + date; NULL signed = not applicable; a date only when signed)
--   classification_*  -> Classification (doc type + confidence + method + reviewer; method always known;
--                                        'human' needs a reviewer, a machine method needs a confidence)
CREATE TABLE uc1_submitted_document (
    submitted_document_id       TEXT NOT NULL PRIMARY KEY,
    application_id              TEXT NOT NULL REFERENCES uc1_application (application_id),
    received_at                 TIMESTAMP NOT NULL,
    document_date               DATE,
    -- struct: period
    period_start_date           DATE,
    period_end_date             DATE,
    -- struct: signature
    signature_signed            BOOLEAN CHECK (signature_signed IN (TRUE, FALSE)),
    signature_date              DATE,
    -- struct: classification (taxonomy id + how it was decided)
    classification_doc_type     TEXT NOT NULL       -- taxonomy id (uc1_doc_type)
        CHECK (length(classification_doc_type) > 0),
    classification_confidence   DOUBLE PRECISION CHECK (classification_confidence BETWEEN 0 AND 1),
    classification_method       TEXT NOT NULL CHECK (classification_method IN ('classifier', 'llm', 'human')),
    classification_reviewed_by  TEXT,
    rag_doc_id                  TEXT,               -- the uploaded file in RAG-OS
    CHECK (period_start_date IS NULL OR period_end_date IS NOT NULL),
    CHECK (period_end_date IS NULL OR period_start_date IS NULL OR period_end_date >= period_start_date),
    CHECK (signature_date IS NULL OR signature_signed = TRUE),
    CHECK (classification_method <> 'human' OR classification_reviewed_by IS NOT NULL),
    CHECK (classification_method = 'human' OR classification_confidence IS NOT NULL)
);

-- One completeness decision (prov:Activity). Freezes the guide version used (the one documented
-- denormalisation: a decision must be reproducible exactly as it was made). Append-only.
CREATE TABLE uc1_evaluation (
    evaluation_id           TEXT NOT NULL PRIMARY KEY,
    application_id          TEXT NOT NULL REFERENCES uc1_application (application_id),
    guideline_version_id    TEXT NOT NULL REFERENCES uc1_guideline_version (guideline_version_id),  -- prov:used
    due_date                DATE,                   -- frozen notice deadline; NULL = complete, no notice due
    engine_version          TEXT NOT NULL,
    evaluated_at            TIMESTAMP NOT NULL
);

-- Object-backed link: Evaluation x Requirement. One outcome per requirement per evaluation.
CREATE TABLE uc1_evaluation_item (
    evaluation_item_id          TEXT NOT NULL PRIMARY KEY,
    evaluation_id               TEXT NOT NULL REFERENCES uc1_evaluation (evaluation_id),
    requirement_id              TEXT NOT NULL,      -- requirement id within the evaluation's guide version, e.g. 'R4'
    outcome                     TEXT NOT NULL CHECK (outcome IN ('met', 'missing', 'not_applicable')),
    reason                      TEXT,               -- engine explanation; must not contain PII (no names, ids)
    UNIQUE (evaluation_id, requirement_id)
);

-- Which documents satisfied an item. Many-to-many: 'two most recent bank statements' is met by two documents.
CREATE TABLE uc1_evaluation_item_document (
    evaluation_item_id          TEXT NOT NULL REFERENCES uc1_evaluation_item (evaluation_item_id),
    submitted_document_id       TEXT NOT NULL REFERENCES uc1_submitted_document (submitted_document_id),
    PRIMARY KEY (evaluation_item_id, submitted_document_id)
);

-- Non-business days, used to compute due dates.
CREATE TABLE uc1_holiday (
    holiday_date            DATE NOT NULL PRIMARY KEY,
    name                    TEXT NOT NULL
);

-- At most one 'primary' party per application; at most one open-ended guide version per investor.
CREATE UNIQUE INDEX ux_uc1_application_party_primary ON uc1_application_party (application_id) WHERE role = 'primary';
CREATE UNIQUE INDEX ux_uc1_guideline_version_open ON uc1_guideline_version (investor_id) WHERE effective_to_date IS NULL;

-- Every foreign-key column is the leading column of an index. These FKs are already covered by a PK or UNIQUE:
--   uc1_guideline_version.investor_id          UNIQUE (investor_id, version_label)
--   uc1_application_party.application_id       UNIQUE (application_id, borrower_id)
--   uc1_evaluation_item.evaluation_id          UNIQUE (evaluation_id, requirement_id)
--   uc1_evaluation_item_document.evaluation_item_id   PRIMARY KEY (evaluation_item_id, submitted_document_id)
-- The rest get one index each.
CREATE INDEX ix_uc1_loan_investor_id ON uc1_loan (investor_id);
CREATE INDEX ix_uc1_application_loan_id ON uc1_application (loan_id);
CREATE INDEX ix_uc1_application_party_borrower_id ON uc1_application_party (borrower_id);
CREATE INDEX ix_uc1_submitted_document_application_id ON uc1_submitted_document (application_id);
CREATE INDEX ix_uc1_evaluation_application_id ON uc1_evaluation (application_id);
CREATE INDEX ix_uc1_evaluation_guideline_version_id ON uc1_evaluation (guideline_version_id);
CREATE INDEX ix_uc1_evaluation_item_document_submitted_document_id
    ON uc1_evaluation_item_document (submitted_document_id);
