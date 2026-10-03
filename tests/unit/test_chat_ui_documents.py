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
    assert "/api/uploads/options" in common and "o?.required" in common


def _function(text: str, name: str) -> str:
    start = text.index(f"function {name}(")
    end = text.find("\nfunction ", start + 10)
    end2 = text.find("\nexport ", start + 10)
    stops = [e for e in (end, end2) if e != -1]
    return text[start:min(stops) if stops else len(text)]


def test_access_tags_are_chosen_from_the_same_dropdowns_as_an_upload() -> None:
    """Matching is exact: a typed `hr` never matches `HR`. So nothing that sets a tag accepts free text, and the
    upload form and the admin editor share one builder so their lists cannot drift."""
    editor = _function(source("admin/common.ts"), "accessEditor")
    assert "vocabularySelect(" in editor and "clearanceSelect(" in editor
    assert "type: 'text'" not in editor and "type: 'number'" not in editor and "h('input'" not in editor, (
        "the access-tag editor must not take typed values")
    upload = source("upload.ts")
    assert "vocabularySelect(" in upload and "clearanceSelect(" in upload, "the upload form uses the same builders"
    controls = source("tag-controls.ts")
    assert "export function vocabularySelect(" in controls and "export function clearanceSelect(" in controls
    assert chr(0xA0) in controls, "hierarchy indent uses non-breaking spaces (<option> collapses ordinary ones)"


def test_permanent_delete_and_reset_say_what_they_cannot_undo() -> None:
    common = source("admin/common.ts")
    assert "cannot be recovered" in common and "COMES BACK" in common, (
        "the delete confirmation must warn that uploads are unrecoverable and crawled files return on sync")
    reset = source("admin/reset.ts")
    assert "go.disabled = confirmInput.value.trim() !== p.index" in reset, "reset is typed-confirmation only"
    assert "/api/admin/reset" in reset and "include_traces" in reset
