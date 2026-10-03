"""Invariants of the admin Documents page that are invisible from the code that renders it.

Source-level assertions, like test_chat_ui_uploads.py (chat-ui has no test runner; see that file's docstring).

Why this file exists. A document uploaded with no Department or Region could be read by nobody but its uploader,
and nothing in the console said so: the Documents list showed it as Indexed, it never reached the review queue
(Department and Region are not auto-classified), and the control that fixes it sat collapsed inside a dialog.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "chat-ui" / "src"


def source(rel: str) -> str:
    path = SRC / rel
    assert path.exists(), f"{path} is missing - this file is asserting about nothing"
    return path.read_text(encoding="utf-8")


def test_the_list_shows_the_doc_id_and_who_can_read_each_document() -> None:
    text = source("admin/documents.ts")
    assert "label: 'Doc ID'" in text, "the Documents list must show the document id"
    assert "copyTag(r.doc_id" in text, "...as a copyable id, since it is what an administrator pastes elsewhere"
    assert "label: 'Access'" in text and "accessSummary(r, required" in text, "and who can read each document"


def test_a_document_nobody_can_read_is_flagged_and_one_click_from_the_fix() -> None:
    common = source("admin/common.ts")
    assert "export function missingAccess(" in common
    assert "visibility === 'private'" in common, "a deliberately private document is not a mistake"
    assert ": invisible`" in common, "the badge must say what the consequence is, not only what is missing"
    docs = source("admin/documents.ts")
    assert "openDoc(r.doc_id, true)" in docs, "the badge opens the document with the access editor expanded"
    assert "opts.focusAccess || missingAccess(r, required).length" in common, (
        "the editor opens by itself when a required access tag is missing")


def test_the_required_attributes_come_from_the_policy_not_a_hardcoded_list() -> None:
    common = source("admin/common.ts")
    assert "/api/uploads/options" in common and ".then((o) => o.required)" in common
