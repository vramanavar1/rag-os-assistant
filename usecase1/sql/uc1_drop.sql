-- usecase1/sql/uc1_drop.sql
-- Use Case 1 (synthetic MVP): removes every uc1_ table created by uc1_create.sql (see usecase1/CHANGES.md).
-- Reverse dependency order (every table is dropped before the tables it references), so it needs no CASCADE;
-- check_sql.py asserts that order statement by statement. Indexes go with their tables.
-- Idempotent (IF EXISTS): safe to run on an empty schema, and it is the step before re-running uc1_create.sql.
-- Portable: runs unchanged on SQLite 3 and PostgreSQL 16 (on PostgreSQL after SET search_path TO uc1).

DROP TABLE IF EXISTS uc1_holiday;
DROP TABLE IF EXISTS uc1_evaluation_item_document;
DROP TABLE IF EXISTS uc1_evaluation_item;
DROP TABLE IF EXISTS uc1_evaluation;
DROP TABLE IF EXISTS uc1_submitted_document;
DROP TABLE IF EXISTS uc1_application_party;
DROP TABLE IF EXISTS uc1_application;
DROP TABLE IF EXISTS uc1_borrower;
DROP TABLE IF EXISTS uc1_loan;
DROP TABLE IF EXISTS uc1_guideline_version;
DROP TABLE IF EXISTS uc1_investor;
