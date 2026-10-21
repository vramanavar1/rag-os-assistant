"""Verify the Use Case 1 semantic model: uc1_create.sql / uc1_drop.sql against the README naming contract.

Run from the repo root:  python usecase1/checks/check_sql.py [--postgres]
Standard library only (sqlite3). Leaves .data/uc1.db holding the empty schema (plus the seed, once it exists).

What it proves, in order:
  1. a static portability lint over uc1_create.sql (SQLite-only / PostgreSQL-only syntax, no IF NOT EXISTS);
  2. the script runs on a fresh DB and a second run fails loudly (it expects an empty schema);
  3. tables, columns (order, declared type, NOT NULL, PK position) match the hard-coded contract;
  4. naming rules, FK index coverage (autoindexes count; partial indexes do not) and no redundant FK index;
  5. a positive control inserts, and every violation case fails with its expected message;
  6. each invariant query detects a planted violation and returns no rows on valid data;
  7. uc1_drop.sql drops each table only once nothing remaining references it, and removes every table;
  8. the seed (if present) loads, its dates / timestamps are well formed and every invariant holds on it.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import date, datetime

from common import DB_PATH, SQL_DIR, Report, exit_with, header

CREATE_SQL = SQL_DIR / "uc1_create.sql"
DROP_SQL = SQL_DIR / "uc1_drop.sql"
SEED_SQL = SQL_DIR / "uc1_seed.sql"

T, I, D, B, DT, TS = "TEXT", "INTEGER", "DOUBLE PRECISION", "BOOLEAN", "DATE", "TIMESTAMP"
NN, NULL = True, False

# The contract (usecase1/README.md, "Semantic model"): every table with its exact columns, in order:
# (name, declared type, NOT NULL, position in the primary key or 0).
EXPECTED_COLUMNS: dict[str, list[tuple[str, str, bool, int]]] = {
    "uc1_investor": [("investor_id", T, NN, 1), ("name", T, NN, 0), ("short_code", T, NN, 0)],
    "uc1_guideline_version": [
        ("guideline_version_id", T, NN, 1), ("investor_id", T, NN, 0), ("version_label", T, NN, 0),
        ("effective_from_date", DT, NN, 0), ("effective_to_date", DT, NULL, 0), ("requirements_file", T, NN, 0),
        ("source_doc_id", T, NULL, 0),
    ],
    "uc1_loan": [("loan_id", T, NN, 1), ("investor_id", T, NN, 0), ("lien_position", I, NN, 0),
                 ("property_state", T, NN, 0)],
    "uc1_borrower": [("borrower_id", T, NN, 1), ("display_name", T, NN, 0)],
    "uc1_application": [
        ("application_id", T, NN, 1), ("loan_id", T, NN, 0), ("received_at", TS, NN, 0),
        ("foreclosure_sale_date", DT, NULL, 0), ("hardship_reason", T, NN, 0),
    ],
    "uc1_application_party": [
        ("application_party_id", T, NN, 1), ("application_id", T, NN, 0), ("borrower_id", T, NN, 0),
        ("role", T, NN, 0), ("employment_type", T, NN, 0), ("has_rental_income", B, NN, 0),
    ],
    "uc1_submitted_document": [
        ("submitted_document_id", T, NN, 1), ("application_id", T, NN, 0), ("received_at", TS, NN, 0),
        ("document_date", DT, NULL, 0), ("period_start_date", DT, NULL, 0), ("period_end_date", DT, NULL, 0),
        ("signature_signed", B, NULL, 0), ("signature_date", DT, NULL, 0), ("classification_doc_type", T, NN, 0),
        ("classification_confidence", D, NULL, 0), ("classification_method", T, NN, 0),
        ("classification_reviewed_by", T, NULL, 0), ("rag_doc_id", T, NULL, 0),
    ],
    "uc1_evaluation": [
        ("evaluation_id", T, NN, 1), ("application_id", T, NN, 0), ("guideline_version_id", T, NN, 0),
        ("due_date", DT, NULL, 0), ("engine_version", T, NN, 0), ("evaluated_at", TS, NN, 0),
    ],
    "uc1_evaluation_item": [
        ("evaluation_item_id", T, NN, 1), ("evaluation_id", T, NN, 0), ("requirement_id", T, NN, 0),
        ("outcome", T, NN, 0), ("reason", T, NULL, 0),
    ],
    "uc1_evaluation_item_document": [("evaluation_item_id", T, NN, 1), ("submitted_document_id", T, NN, 2)],
    "uc1_holiday": [("holiday_date", DT, NN, 1), ("name", T, NN, 0)],
}
EXPECTED: dict[str, list[str]] = {t: [c[0] for c in cols] for t, cols in EXPECTED_COLUMNS.items()}

ALLOWED_TYPES = {T, I, D, B, DT, TS}
BOOLEAN_COLUMNS = {("uc1_application_party", "has_rental_income"), ("uc1_submitted_document", "signature_signed")}
# Generic names (status, value) and derived values that must never be stored.
FORBIDDEN_EXACT = {"status", "value", "age_at_receipt_days", "months_before_receipt", "governing_version_id",
                   "satisfied_by_document_id"}
# SQLite-only or PostgreSQL-only syntax; the script must run unchanged on both. Matched outside comments.
NON_PORTABLE = {
    "IF NOT EXISTS (the script expects an empty schema)": r"\bIF\s+NOT\s+EXISTS\b",
    "AUTOINCREMENT": r"\bAUTOINCREMENT\b",
    "STRICT": r"\bSTRICT\b",
    "WITHOUT ROWID": r"\bWITHOUT\s+ROWID\b",
    "GLOB": r"\bGLOB\b",
    "typeof()": r"\btypeof\s*\(",
    "date()": r"\bdate\s*\(",
    "datetime()": r"\bdatetime\s*\(",
    "IIF()": r"\bIIF\s*\(",
    "SERIAL": r"\b(BIG|SMALL)?SERIAL\b",
    ":: cast": r"::",
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def _tables(con: sqlite3.Connection) -> set[str]:
    rows = con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    return {r[0] for r in rows}


def _table_info(con: sqlite3.Connection, table: str) -> list[tuple[str, str, bool, int]]:
    """(name, declared type upper-cased, NOT NULL, PK position) per column, in declaration order."""
    return [(r[1], (r[2] or "").upper(), bool(r[3]), r[5]) for r in con.execute(f"PRAGMA table_info({table})")]


def _fresh_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit; transactions are explicit
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


# --- 1. static lint ---------------------------------------------------------------------------------------------

def _check_portability_lint(report: Report, sql: str) -> None:
    code = _strip_comments(sql)
    found = [name for name, pattern in NON_PORTABLE.items() if re.search(pattern, code, re.IGNORECASE)]
    report.check(not found, "uc1_create.sql has no non-portable syntax (static lint)", ", ".join(found))


# --- 3/4. contract and naming -------------------------------------------------------------------------------------

def _check_contract(report: Report, con: sqlite3.Connection, tables: list[str]) -> None:
    report.check(set(tables) == set(EXPECTED), f"table set matches the contract ({len(EXPECTED)} tables)",
                 f"extra {sorted(set(tables) - set(EXPECTED))}, missing {sorted(set(EXPECTED) - set(tables))}")
    present = [t for t in EXPECTED if t in tables]

    names = [f"{t}: {[c[0] for c in _table_info(con, t)]}" for t in present
             if [c[0] for c in _table_info(con, t)] != EXPECTED[t]]
    report.check(not names, "columns per table match the contract (names and order)", "; ".join(names))

    bad_types = [f"{t}.{c} {typ}" for t in tables for c, typ, _, _ in _table_info(con, t) if typ not in ALLOWED_TYPES]
    report.check(not bad_types, f"declared types are within {sorted(ALLOWED_TYPES)}", "; ".join(bad_types))

    for what, pos in (("declared type", 1), ("NOT NULL flag", 2), ("primary-key position", 3)):
        diffs = []
        for t in present:
            actual = {c[0]: c for c in _table_info(con, t)}
            for col in EXPECTED_COLUMNS[t]:
                if col[0] in actual and actual[col[0]][pos] != col[pos]:
                    diffs.append(f"{t}.{col[0]} is {actual[col[0]][pos]!r}, contract {col[pos]!r}")
        report.check(not diffs, f"every column's {what} matches the contract", "; ".join(diffs))


def _check_naming(report: Report, con: sqlite3.Connection, tables: list[str]) -> None:
    bad_prefix = [t for t in tables if not t.startswith("uc1_")]
    report.check(not bad_prefix, "every table starts uc1_", ", ".join(bad_prefix))

    date_bad, at_bad, bool_bad, forbidden = [], [], [], []
    for t in tables:
        for name, typ, _, _ in _table_info(con, t):
            if name.endswith("_date") != (typ == DT):
                date_bad.append(f"{t}.{name} {typ}")
            if name.endswith("_at") != (typ == TS):
                at_bad.append(f"{t}.{name} {typ}")
            if ((t, name) in BOOLEAN_COLUMNS) != (typ == B):
                bool_bad.append(f"{t}.{name} {typ}")
            if name in FORBIDDEN_EXACT or name.startswith("resolved_") or "status" in name:
                forbidden.append(f"{t}.{name}")
    report.check(not date_bad, "columns ending _date are exactly the DATE columns", "; ".join(date_bad))
    report.check(not at_bad, "columns ending _at are exactly the TIMESTAMP columns", "; ".join(at_bad))
    report.check(not bool_bad, "boolean columns are declared BOOLEAN (and only those)", "; ".join(bool_bad))
    report.check(not forbidden, "no generic or derived column (status, value, resolved_*, *status*, "
                 "age_at_receipt_days, months_before_receipt, governing_version_id, satisfied_by_document_id)",
                 "; ".join(forbidden))


def _check_indexes(report: Report, con: sqlite3.Connection, tables: list[str]) -> None:
    """Every FK column leads a full (non-partial) index, autoindexes included; no plain index duplicates one."""
    missing, redundant, fk_count = [], [], 0
    for t in tables:
        full_leading, unique_leading, plain = set(), set(), []
        for _, name, unique, origin, partial in con.execute(f"PRAGMA index_list({t})"):
            cols = [r[2] for r in con.execute(f"PRAGMA index_info({name})")]
            if not cols or partial:
                continue  # a partial index does not cover every row, so it does not serve the FK
            full_leading.add(cols[0])
            if unique:
                unique_leading.add(cols[0])
            elif origin == "c":
                plain.append((name, cols[0]))
        redundant += [f"{name} ({t}.{col})" for name, col in plain if col in unique_leading]
        for fk in con.execute(f"PRAGMA foreign_key_list({t})"):
            fk_count += 1
            if fk[3] not in full_leading:
                missing.append(f"{t}.{fk[3]}")
    report.check(fk_count > 0 and not missing, f"every FK column leads a full index ({fk_count} FKs, autoindexes "
                 "count)", ", ".join(missing))
    report.check(not redundant, "no plain index whose column already leads a PK / UNIQUE index", ", ".join(redundant))


# --- 5. enforcement -----------------------------------------------------------------------------------------------

_DOC_COLS = ("submitted_document_id, application_id, received_at, document_date, period_start_date, "
             "period_end_date, signature_signed, signature_date, classification_doc_type, "
             "classification_confidence, classification_method, classification_reviewed_by, rag_doc_id")
_GV_COLS = ("guideline_version_id, investor_id, version_label, effective_from_date, effective_to_date, "
            "requirements_file, source_doc_id")


def _doc(values: str) -> str:
    return f"INSERT INTO uc1_submitted_document ({_DOC_COLS}) VALUES ({values})"


def _gv(values: str) -> str:
    return f"INSERT INTO uc1_guideline_version ({_GV_COLS}) VALUES ({values})"


# A valid graph: also the positive control (each row exercises an allowed edge: a closed and an open version,
# a primary plus a co-borrower, a machine- and a human-classified document, a document linked to an item).
_FIXTURE = [
    "INSERT INTO uc1_investor (investor_id, name, short_code) VALUES ('investor-a', 'Investor A', 'IA')",
    _gv("'investor-a/2025.1', 'investor-a', '2025.1', '2025-01-01', '2025-12-31', 'investor-a-2025.1.ttl', NULL"),
    _gv("'investor-a/2026.1', 'investor-a', '2026.1', '2026-01-01', NULL, 'investor-a-2026.1.ttl', NULL"),
    "INSERT INTO uc1_loan (loan_id, investor_id, lien_position, property_state) VALUES ('L1', 'investor-a', 1, 'TX')",
    "INSERT INTO uc1_borrower (borrower_id, display_name) VALUES ('B1', 'Synthetic Borrower 1')",
    "INSERT INTO uc1_borrower (borrower_id, display_name) VALUES ('B2', 'Synthetic Borrower 2')",
    "INSERT INTO uc1_borrower (borrower_id, display_name) VALUES ('B3', 'Synthetic Borrower 3')",
    "INSERT INTO uc1_application (application_id, loan_id, received_at, foreclosure_sale_date, hardship_reason)"
    " VALUES ('A1', 'L1', '2026-03-02 10:00:00', NULL, 'Unemployment')",
    "INSERT INTO uc1_application_party (application_party_id, application_id, borrower_id, role, employment_type,"
    " has_rental_income) VALUES ('P1', 'A1', 'B1', 'primary', 'Unemployed', FALSE)",
    "INSERT INTO uc1_application_party (application_party_id, application_id, borrower_id, role, employment_type,"
    " has_rental_income) VALUES ('P2', 'A1', 'B2', 'co_borrower', 'Salaried', TRUE)",
    _doc("'D1', 'A1', '2026-03-02 10:00:00', '2026-02-27', '2026-02-01', '2026-02-28', NULL, NULL,"
         " 'BankStatement', 0.93, 'classifier', NULL, NULL"),
    _doc("'D2', 'A1', '2026-03-02 10:00:00', '2026-02-27', NULL, NULL, TRUE, '2026-02-27',"
         " 'MAA', NULL, 'human', 'reviewer-1', NULL"),
    "INSERT INTO uc1_evaluation (evaluation_id, application_id, guideline_version_id, due_date, engine_version,"
    " evaluated_at) VALUES ('E1', 'A1', 'investor-a/2026.1', '2026-03-09', 'test', '2026-03-02 11:00:00')",
    "INSERT INTO uc1_evaluation_item (evaluation_item_id, evaluation_id, requirement_id, outcome, reason)"
    " VALUES ('I1', 'E1', 'R5', 'met', NULL)",
    "INSERT INTO uc1_evaluation_item_document (evaluation_item_id, submitted_document_id) VALUES ('I1', 'D1')",
]

_ITEM = "INSERT INTO uc1_evaluation_item (evaluation_item_id, evaluation_id, requirement_id, outcome, reason) VALUES "
_LINK = "INSERT INTO uc1_evaluation_item_document (evaluation_item_id, submitted_document_id) VALUES "
_LOAN = "INSERT INTO uc1_loan (loan_id, investor_id, lien_position, property_state) VALUES "
_APP = ("INSERT INTO uc1_application (application_id, loan_id, received_at, foreclosure_sale_date, hardship_reason)"
        " VALUES ")
_PARTY = ("INSERT INTO uc1_application_party (application_party_id, application_id, borrower_id, role,"
          " employment_type, has_rental_income) VALUES ")
_FK = "FOREIGN KEY constraint failed"
_CK = "CHECK constraint failed: "
_UQ = "UNIQUE constraint failed: "

# (label, setup run after the fixture, the one statement that must fail, expected message fragment).
# A trailing $ anchors the fragment to the end of the message (to tell a one-column unique from a wider one).
_VIOLATIONS: list[tuple[str, list[str], str, str]] = [
    ("orphan uc1_application (unknown loan_id)", [],
     _APP + "('A2', 'NO-SUCH-LOAN', '2026-03-02 10:00:00', NULL, 'Medical')", _FK),
    ("NULL primary key", [],
     "INSERT INTO uc1_investor (investor_id, name, short_code) VALUES (NULL, 'Investor B', 'IB')",
     "NOT NULL constraint failed: uc1_investor.investor_id"),
    ("duplicate short_code", [],
     "INSERT INTO uc1_investor (investor_id, name, short_code) VALUES ('investor-b', 'Investor B', 'IA')",
     _UQ + "uc1_investor.short_code"),
    ("guideline_version_id not '<investor_id>/<version_label>'", [],
     _gv("'investor-a/v2027', 'investor-a', '2027.1', '2027-01-01', NULL, 'f.ttl', NULL"),
     _CK + "guideline_version_id = investor_id || '/' || version_label"),
    ("effective_to_date before effective_from_date", [],
     _gv("'investor-a/2024.1', 'investor-a', '2024.1', '2024-06-01', '2024-01-31', 'f.ttl', NULL"),
     _CK + "effective_to_date IS NULL OR effective_to_date >= effective_from_date"),
    ("duplicate (investor_id, version_label)", [],
     _gv("'investor-a/2026.1', 'investor-a', '2026.1', '2024-01-01', '2024-12-31', 'f.ttl', NULL"),
     _UQ + "uc1_guideline_version.investor_id, uc1_guideline_version.version_label"),
    ("duplicate (investor_id, effective_from_date)", [],
     _gv("'investor-a/2026.2', 'investor-a', '2026.2', '2026-01-01', '2026-06-30', 'f.ttl', NULL"),
     _UQ + "uc1_guideline_version.investor_id, uc1_guideline_version.effective_from_date"),
    ("two open-ended versions for one investor", [],
     _gv("'investor-a/2027.1', 'investor-a', '2027.1', '2027-01-01', NULL, 'f.ttl', NULL"),
     _UQ + "uc1_guideline_version.investor_id$"),
    ("lien_position 0", [], _LOAN + "('L2', 'investor-a', 0, 'TX')", _CK + "lien_position BETWEEN 1 AND 9"),
    ("property_state 'Texas' (not two letters)", [], _LOAN + "('L2', 'investor-a', 1, 'Texas')",
     _CK + "length(property_state) = 2"),
    ("property_state 'tx' (lower case)", [], _LOAN + "('L2', 'investor-a', 1, 'tx')",
     _CK + "length(property_state) = 2 AND upper(property_state) = property_state"),
    ("empty hardship_reason", [], _APP + "('A2', 'L1', '2026-03-02 10:00:00', NULL, '')",
     _CK + "length(hardship_reason) > 0"),
    ("invalid role", [], _PARTY + "('P3', 'A1', 'B3', 'guarantor', 'Salaried', FALSE)", _CK + "role IN"),
    ("empty employment_type", [], _PARTY + "('P3', 'A1', 'B3', 'co_borrower', '', FALSE)",
     _CK + "length(employment_type) > 0"),
    ("has_rental_income 'true' (text, not a boolean)", [],
     _PARTY + "('P3', 'A1', 'B3', 'co_borrower', 'Salaried', 'true')", _CK + "has_rental_income IN (TRUE, FALSE)"),
    ("duplicate (application_id, borrower_id) party", [],
     _PARTY + "('P3', 'A1', 'B1', 'co_borrower', 'Salaried', TRUE)",
     _UQ + "uc1_application_party.application_id, uc1_application_party.borrower_id"),
    ("two primary parties on one application", [],
     _PARTY + "('P3', 'A1', 'B3', 'primary', 'Salaried', FALSE)", _UQ + "uc1_application_party.application_id$"),
    ("classification_confidence 1.5", [],
     _doc("'D9', 'A1', '2026-03-02 10:00:00', NULL, NULL, NULL, NULL, NULL, 'Paystub', 1.5, 'classifier', NULL, NULL"),
     _CK + "classification_confidence BETWEEN 0 AND 1"),
    ("invalid classification_method", [],
     _doc("'D9', 'A1', '2026-03-02 10:00:00', NULL, NULL, NULL, NULL, NULL, 'Paystub', 0.9, 'ocr', NULL, NULL"),
     _CK + "classification_method IN"),
    ("NULL classification_method", [],
     _doc("'D9', 'A1', '2026-03-02 10:00:00', NULL, NULL, NULL, NULL, NULL, 'Paystub', 0.9, NULL, NULL, NULL"),
     "NOT NULL constraint failed: uc1_submitted_document.classification_method"),
    ("empty classification_doc_type", [],
     _doc("'D9', 'A1', '2026-03-02 10:00:00', NULL, NULL, NULL, NULL, NULL, '', 0.9, 'classifier', NULL, NULL"),
     _CK + "length(classification_doc_type) > 0"),
    ("'human' classification with no reviewer", [],
     _doc("'D9', 'A1', '2026-03-02 10:00:00', NULL, NULL, NULL, NULL, NULL, 'MAA', NULL, 'human', NULL, NULL"),
     _CK + "classification_method <> 'human' OR classification_reviewed_by IS NOT NULL"),
    ("'classifier' classification with no confidence", [],
     _doc("'D9', 'A1', '2026-03-02 10:00:00', NULL, NULL, NULL, NULL, NULL, 'Paystub', NULL, 'classifier', NULL,"
          " NULL"),
     _CK + "classification_method = 'human' OR classification_confidence IS NOT NULL"),
    ("signature_signed 'yes' (text, not a boolean)", [],
     _doc("'D9', 'A1', '2026-03-02 10:00:00', NULL, NULL, NULL, 'yes', NULL, 'MAA', 0.9, 'llm', NULL, NULL"),
     _CK + "signature_signed IN (TRUE, FALSE)"),
    ("signature_date with signature_signed FALSE", [],
     _doc("'D9', 'A1', '2026-03-02 10:00:00', NULL, NULL, NULL, FALSE, '2026-02-27', 'MAA', 0.9, 'llm', NULL, NULL"),
     _CK + "signature_date IS NULL OR signature_signed = TRUE"),
    ("period start with no end", [],
     _doc("'D9', 'A1', '2026-03-02 10:00:00', NULL, '2026-02-01', NULL, NULL, NULL, 'BankStatement', 0.9, 'llm',"
          " NULL, NULL"),
     _CK + "period_start_date IS NULL OR period_end_date IS NOT NULL"),
    ("period end before start", [],
     _doc("'D9', 'A1', '2026-03-02 10:00:00', NULL, '2026-02-28', '2026-02-01', NULL, NULL, 'BankStatement', 0.9,"
          " 'llm', NULL, NULL"),
     _CK + "period_end_date IS NULL OR period_start_date IS NULL OR period_end_date >= period_start_date"),
    ("invalid outcome", [], _ITEM + "('I9', 'E1', 'R9', 'pending', NULL)", _CK + "outcome IN"),
    ("duplicate (evaluation_id, requirement_id) item", [], _ITEM + "('I9', 'E1', 'R5', 'missing', NULL)",
     _UQ + "uc1_evaluation_item.evaluation_id, uc1_evaluation_item.requirement_id"),
    ("item-document link to an unknown document", [_ITEM + "('I2', 'E1', 'R6', 'met', NULL)"],
     _LINK + "('I2', 'NO-SUCH-DOC')", _FK),
    ("item-document link to an unknown evaluation_item_id", [], _LINK + "('NO-SUCH-ITEM', 'D1')", _FK),
    ("duplicate item-document link (composite PK)", [], _LINK + "('I1', 'D1')",
     _UQ + "uc1_evaluation_item_document.evaluation_item_id, uc1_evaluation_item_document.submitted_document_id"),
]


def _matches(expected: str, message: str) -> bool:
    return message.endswith(expected[:-1]) if expected.endswith("$") else expected in message


def _in_rollback(con: sqlite3.Connection, statements: list[str]) -> None:
    """Open a transaction and run statements; the caller must ROLLBACK."""
    con.execute("BEGIN")
    for stmt in statements:
        con.execute(stmt)


def _check_enforcement(report: Report, con: sqlite3.Connection) -> None:
    try:
        _in_rollback(con, _FIXTURE)
        rows = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in EXPECTED}
        report.check(all(rows[t] for t in EXPECTED if t != "uc1_holiday"),
                     "positive control: a valid graph (incl. document, item and item-document link) inserts",
                     f"empty tables: {[t for t, n in rows.items() if not n and t != 'uc1_holiday']}")
    except sqlite3.Error as exc:
        report.check(False, "positive control: a valid graph inserts", str(exc))
    finally:
        con.execute("ROLLBACK")

    for label, setup, bad_sql, expected in _VIOLATIONS:
        label = f"rejected: {label}"
        try:
            _in_rollback(con, _FIXTURE + setup)
        except sqlite3.Error as exc:
            report.check(False, label, f"fixture/setup failed: {exc}")
            con.execute("ROLLBACK")
            continue
        try:
            con.execute(bad_sql)
            report.check(False, label, "insert succeeded")
        except sqlite3.IntegrityError as exc:
            report.check(_matches(expected, str(exc)), f"{label} ({exc})", f"expected '{expected}'")
        except sqlite3.Error as exc:
            report.check(False, label, f"{type(exc).__name__}, not IntegrityError: {exc}")
        finally:
            con.execute("ROLLBACK")
    left = sum(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in EXPECTED)
    report.check(left == 0, "enforcement tests rolled back (no rows left)", f"{left} rows")


# --- 6. invariants (cross-row rules no portable constraint can express) ----------------------------------------------

_INVARIANTS: dict[str, str] = {
    "a linked document belongs to the evaluation's application": """
        SELECT l.evaluation_item_id, l.submitted_document_id
        FROM uc1_evaluation_item_document l
        JOIN uc1_evaluation_item i ON i.evaluation_item_id = l.evaluation_item_id
        JOIN uc1_evaluation e ON e.evaluation_id = i.evaluation_id
        JOIN uc1_submitted_document d ON d.submitted_document_id = l.submitted_document_id
        WHERE d.application_id <> e.application_id""",
    "only a 'met' item has satisfying documents": """
        SELECT l.evaluation_item_id, l.submitted_document_id
        FROM uc1_evaluation_item_document l
        JOIN uc1_evaluation_item i ON i.evaluation_item_id = l.evaluation_item_id
        WHERE i.outcome <> 'met'""",
    "a linked document was received by the evaluation's evaluated_at": """
        SELECT l.evaluation_item_id, l.submitted_document_id
        FROM uc1_evaluation_item_document l
        JOIN uc1_evaluation_item i ON i.evaluation_item_id = l.evaluation_item_id
        JOIN uc1_evaluation e ON e.evaluation_id = i.evaluation_id
        JOIN uc1_submitted_document d ON d.submitted_document_id = l.submitted_document_id
        WHERE d.received_at > e.evaluated_at""",
    "every application has exactly one primary party": """
        SELECT a.application_id, COUNT(p.application_party_id)
        FROM uc1_application a
        LEFT JOIN uc1_application_party p ON p.application_id = a.application_id AND p.role = 'primary'
        GROUP BY a.application_id
        HAVING COUNT(p.application_party_id) <> 1""",
    "an investor's guide versions do not overlap in time": """
        SELECT v1.guideline_version_id, v2.guideline_version_id
        FROM uc1_guideline_version v1
        JOIN uc1_guideline_version v2
          ON v2.investor_id = v1.investor_id AND v2.guideline_version_id > v1.guideline_version_id
        WHERE v1.effective_from_date <= COALESCE(v2.effective_to_date, '9999-12-31')
          AND v2.effective_from_date <= COALESCE(v1.effective_to_date, '9999-12-31')""",
}

# One planted violation per invariant (run after the valid fixture), to prove each query can find something.
_PLANTS: dict[str, list[str]] = {
    "a linked document belongs to the evaluation's application": [
        _APP + "('A2', 'L1', '2026-03-01 10:00:00', NULL, 'Medical')",
        _doc("'D9', 'A2', '2026-03-01 10:00:00', NULL, NULL, NULL, NULL, NULL, 'Paystub', 0.9, 'llm', NULL, NULL"),
        _LINK + "('I1', 'D9')",
    ],
    "only a 'met' item has satisfying documents": [
        _ITEM + "('I9', 'E1', 'R9', 'missing', NULL)", _LINK + "('I9', 'D1')",
    ],
    "a linked document was received by the evaluation's evaluated_at": [
        _doc("'D9', 'A1', '2026-03-03 09:00:00', NULL, NULL, NULL, NULL, NULL, 'Paystub', 0.9, 'llm', NULL, NULL"),
        _LINK + "('I1', 'D9')",
    ],
    "every application has exactly one primary party": [
        _APP + "('A2', 'L1', '2026-03-01 10:00:00', NULL, 'Medical')",
    ],
    "an investor's guide versions do not overlap in time": [
        _gv("'investor-a/2025.2', 'investor-a', '2025.2', '2025-06-01', '2025-06-30', 'f.ttl', NULL"),
    ],
}


def _check_invariants_detect(report: Report, con: sqlite3.Connection) -> None:
    for name, query in _INVARIANTS.items():
        try:
            _in_rollback(con, _FIXTURE)
            clean = con.execute(query).fetchall()
            for stmt in _PLANTS[name]:
                con.execute(stmt)
            planted = con.execute(query).fetchall()
            report.check(not clean and bool(planted), f"invariant query detects a planted violation: {name}",
                         f"valid fixture -> {clean}, planted -> {planted}")
        except sqlite3.Error as exc:
            report.check(False, f"invariant query detects a planted violation: {name}", str(exc))
        finally:
            con.execute("ROLLBACK")


def _check_invariants(report: Report, con: sqlite3.Connection, scope: str) -> None:
    for name, query in _INVARIANTS.items():
        rows = con.execute(query).fetchall()
        report.check(not rows, f"invariant on {scope}: {name}", f"{len(rows)} rows, e.g. {rows[:5]}")


# --- 7. drop order ---------------------------------------------------------------------------------------------------

def _check_drop(report: Report, con: sqlite3.Connection) -> None:
    statements = [s.strip() for s in _strip_comments(DROP_SQL.read_text(encoding="utf-8")).split(";") if s.strip()]
    dropped = [m.group(1) for s in statements if (m := re.fullmatch(r"DROP TABLE IF EXISTS (\w+)", s, re.I))]
    report.check(len(dropped) == len(statements) and sorted(dropped) == sorted(EXPECTED),
                 "uc1_drop.sql is one DROP TABLE IF EXISTS per contract table", f"{statements}")

    order_bad = []
    for stmt, target in zip(statements, dropped):
        referrers = sorted(t for t in _tables(con) if t != target
                           and any(fk[2] == target for fk in con.execute(f"PRAGMA foreign_key_list({t})")))
        if referrers:
            order_bad.append(f"{target} dropped while referenced by {referrers}")
        con.execute(stmt)
    report.check(not order_bad, "uc1_drop.sql drops each table only after every table referencing it",
                 "; ".join(order_bad))
    left = sorted(t for t in _tables(con) if t.startswith("uc1_"))
    report.check(not left, "uc1_drop.sql removes every uc1_ table", ", ".join(left))
    try:
        con.executescript(DROP_SQL.read_text(encoding="utf-8"))
        report.check(True, "uc1_drop.sql is idempotent (runs again on an empty schema)")
    except sqlite3.Error as exc:
        report.check(False, "uc1_drop.sql is idempotent (runs again on an empty schema)", str(exc))


# --- 8. seed ---------------------------------------------------------------------------------------------------------

def _valid(value: object, pattern: re.Pattern[str], parse) -> bool:
    if not isinstance(value, str) or not pattern.match(value):
        return False
    try:
        parse(value)
    except ValueError:
        return False
    return True


def _check_seed_formats(report: Report, con: sqlite3.Connection) -> None:
    bad_dates, bad_ts = [], []
    for t in EXPECTED:
        for col in EXPECTED[t]:
            if col.endswith("_date"):
                pattern, parse, sink = DATE_RE, date.fromisoformat, bad_dates
            elif col.endswith("_at"):
                pattern, parse, sink = TIMESTAMP_RE, lambda v: datetime.strptime(v, "%Y-%m-%d %H:%M:%S"), bad_ts
            else:
                continue
            for (v,) in con.execute(f"SELECT {col} FROM {t} WHERE {col} IS NOT NULL"):
                if not _valid(v, pattern, parse):
                    sink.append(f"{t}.{col}={v!r}")
    report.check(not bad_dates, "seed: every *_date value is a valid 'YYYY-MM-DD'", "; ".join(bad_dates[:10]))
    report.check(not bad_ts, "seed: every *_at value is a valid 'YYYY-MM-DD HH:MM:SS'", "; ".join(bad_ts[:10]))


def _check_seed(report: Report, con: sqlite3.Connection) -> None:
    if not SEED_SQL.exists():
        report.info("seed not present yet (step 1.7)")
        return
    try:
        con.executescript(SEED_SQL.read_text(encoding="utf-8"))
        report.check(True, "uc1_seed.sql loads")
    except sqlite3.Error as exc:
        report.check(False, "uc1_seed.sql loads", str(exc))
        return
    for t in EXPECTED:
        report.info(f"{t}: {con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]} rows")
    _check_seed_formats(report, con)
    _check_invariants(report, con, "the seed")


# --- run -------------------------------------------------------------------------------------------------------------

def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres", action="store_true", help="also prove the scripts on PostgreSQL 16")
    args = parser.parse_args(argv)

    report = header("Semantic model (uc1_create.sql / uc1_drop.sql)")
    create_sql = CREATE_SQL.read_text(encoding="utf-8")
    _check_portability_lint(report, create_sql)

    con = _fresh_db()
    try:
        try:
            con.executescript(create_sql)
            report.check(True, f"fresh {DB_PATH.name} created from uc1_create.sql (foreign_keys ON)")
        except sqlite3.Error as exc:
            report.check(False, "uc1_create.sql runs on a fresh SQLite DB", str(exc))
            return report.finish()
        try:
            con.executescript(create_sql)
            report.check(False, "uc1_create.sql fails loudly on an existing schema", "second run succeeded")
        except sqlite3.OperationalError as exc:
            report.check("already exists" in str(exc), f"uc1_create.sql fails loudly on an existing schema ({exc})")

        tables = sorted(_tables(con))
        _check_contract(report, con, tables)
        _check_naming(report, con, tables)
        _check_indexes(report, con, tables)
        _check_enforcement(report, con)
        _check_invariants_detect(report, con)

        _check_drop(report, con)
        con.executescript(create_sql)
        report.check(_tables(con) == set(EXPECTED), "schema re-created after the drop test")
        _check_seed(report, con)
    finally:
        con.close()

    if args.postgres:
        report.check(False, "PostgreSQL proof is step 1.7b", "not run")
    return report.finish()


if __name__ == "__main__":
    exit_with(run())
