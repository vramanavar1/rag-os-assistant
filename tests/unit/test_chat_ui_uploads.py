"""Invariants of the recent-uploads list and the route into the admin console.

`chat-ui` has no test runner - its package.json has `build` and `typecheck` and nothing else - so, like
test_chat_ui_auth.py, these are source-level assertions rather than behavioural tests. That file's docstring
explains the tradeoff; the same applies here. They are deliberately few: each one pins a decision that is
invisible in the code it constrains, or an agreement between artefacts that nothing links together.

Why this file exists. After uploading a document from the chat page there was no way to find out what became
of it. The upload widget polled and showed a live badge, but only for as long as the panel stayed open - and
the tracking id it printed, the only durable handle a non-admin ever gets, was erased about two seconds later
by the first stage update. There was no endpoint that could list a person's uploads back to them, and nothing
in the chat UI linked to the admin console, which could. Close the tab and the document was gone.

Worse, the widget reported FAILED for documents that were indexing perfectly well: its catch block covered the
POST and the poll loop alike, so a single transient 500 or a laptop waking from sleep produced a red badge and
an error message about a document the server was still happily processing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "chat-ui" / "src"


def source(rel: str) -> str:
    path = SRC / rel
    assert path.exists(), f"{path} is missing - this file is asserting about nothing"
    return path.read_text(encoding="utf-8")


def function_body(text: str, name: str, where: str) -> str:
    """The text of a top-level `export ... function <name>` up to the closing brace in column 0."""
    match = re.search(rf"^export (?:async )?function {re.escape(name)}\b.*?^}}", text, re.S | re.M)
    assert match, f"{name}() not found in {where} - it was renamed or restructured, so re-check the invariant"
    return match.group(0)


# ---------------------------------------------------------------- the way into the admin console
def test_the_admin_link_is_shown_only_to_an_admin() -> None:
    """/admin refuses everyone else, so this is not access control - it is not advertising a door that will
    not open. The check and the href must both gate it: an admin on a page that did not ask for the link
    (the console itself) must not get one either."""
    body = function_body(source("ui.ts"), "identityChip", "ui.ts")
    guard = re.search(r"opts\.adminHref\s*&&\s*isAdmin\(me\)", body)
    assert guard, "the admin link must be gated on BOTH the caller passing adminHref and the principal being an admin"
    assert guard.start() < body.index("'Admin'"), "the guard must precede the link it guards"


def test_the_chat_page_links_to_the_console_and_the_console_does_not_link_to_itself() -> None:
    assert "adminHref: '/admin'" in source("chat.ts"), (
        "chat.ts must pass adminHref - without it an administrator on the chat page has no on-screen "
        "indication that the admin console exists, which is how this whole thing started")
    assert "adminHref" not in source("admin.ts"), "the admin console must not link to itself"


def test_there_is_one_spelling_of_the_admin_role_check() -> None:
    """Two copies of "is this person an administrator" in one UI is how they come to disagree."""
    assert "export function isAdmin" in source("ui.ts"), "isAdmin must be exported from ui.ts"
    for rel in ("chat.ts", "admin.ts", "uploads-list.ts", "upload.ts"):
        text = source(rel)
        assert not re.search(r"roles\s*\?\?\s*\[\]\s*\)\s*\.some", text), (
            f"{rel} spells the admin-role check inline; import isAdmin from ui.ts instead")


def test_a_document_link_from_the_chat_page_is_absolute() -> None:
    """Inside the console a bare `#/documents/<id>` resolves. On the chat page it silently does nothing - it
    is the same origin but a different document, so only the fragment changes."""
    assert "/admin#/documents/" in source("chat.ts"), (
        "the chat page must build an absolute /admin#/documents/... href; a bare hash goes nowhere from /")
    assert re.search(r"documentHref:\s*\(docId\)\s*=>\s*`#/documents/", source("admin/documents.ts")), (
        "inside the console the bare hash is correct - an absolute href there would reload the page")


# ---------------------------------------------------------------- the widget must not lie about failure
def test_a_failed_poll_is_never_reported_as_a_failed_document() -> None:
    """The regression this file exists for. Once the POST returns 202 the server owns the document and nothing
    that happens in this browser can change its fate, so no code path after that point may paint it FAILED."""
    text = source("upload.ts")
    body = function_body(text, "createUploadWidget", "upload.ts")
    # From where the tracking id is written: the POST has returned 202 and its own catch is behind us, so a
    # FAILED badge below this point can only be describing something that happened to the browser.
    after_accept = body[body.index("mount(track,"):]
    stray = [m.group(0) for m in re.finditer(r"statusBadge\('FAILED'\)", after_accept)]
    assert not stray, (
        "statusBadge('FAILED') appears after the upload is accepted. A transient poll error, an expired token "
        "or a sleeping laptop would then report a perfectly healthy document as failed.")
    assert "StoppedWatching" in text, (
        "losing contact with a document needs to be its own outcome, distinct from the document failing")


def test_the_poll_loop_tolerates_transient_errors() -> None:
    body = function_body(source("upload.ts"), "createUploadWidget", "upload.ts")
    poll = body[body.index("async function poll("):]
    assert "MAX_POLL_FAILURES" in poll, "a single failed poll must not end the watch"
    assert "continue;" in poll, "a tolerated poll failure has to retry rather than fall through"


def test_the_tracking_id_is_not_overwritten_by_the_next_status_update() -> None:
    """`mount` is replaceChildren. The id and the stage shared one node, so the id - the only handle a
    non-admin has on their document - vanished with the first progress update."""
    body = function_body(source("upload.ts"), "createUploadWidget", "upload.ts")
    assert re.search(r"mount\(track,\s*'Tracking '", body), "the tracking id must have a node of its own"
    assert "mount(detail, h('span', { class: 'muted' }, `Stage:" in body, "the stage must write to a different node"


@pytest.mark.parametrize("status", ["SKIPPED_UNCHANGED", "DELETED"])
def test_every_terminal_status_is_explained(status: str) -> None:
    """These are terminal and used to fall through the success branch as "0 chunks", which reads as a
    successful upload that indexed nothing."""
    assert status in source("upload.ts"), f"{status} is terminal and needs wording of its own"


def test_the_client_size_cap_matches_the_server() -> None:
    """A cross-artefact check, like REDIRECT_PATH next door. nginx allows 60m and the API allowed 50, so a
    55 MB file passed the browser, passed nginx and was refused by the API after the whole upload."""
    client = re.search(r"const MAX_MB = (\d+)", source("upload.ts"))
    assert client, "MAX_MB not found in upload.ts"
    settings = (REPO / "src" / "rag_os" / "infrastructure" / "settings.py").read_text(encoding="utf-8")
    server = re.search(r"upload_max_mb:\s*int\s*=\s*(\d+)", settings)
    assert server, "upload_max_mb not found in settings.py"
    assert client.group(1) == server.group(1), (
        f"the browser refuses above {client.group(1)} MB but the API refuses above {server.group(1)} MB - "
        "anything in between uploads in full and is then rejected")


# ---------------------------------------------------------------- the list itself
def test_the_list_does_not_drag_the_admin_console_into_the_chat_bundle() -> None:
    """chat.ts and admin.ts are separate esbuild entry points. An import from admin/ here would bundle the
    entire console into the page every visitor loads."""
    text = source("uploads-list.ts")
    assert "./admin/" not in text and "from './admin" not in text, (
        "uploads-list.ts is shared by both entry points, so it must build on dom.ts/ui.ts primitives only")


def test_the_three_tabs_cover_the_statuses_they_claim() -> None:
    text = source("uploads-list.ts")
    assert "IN_FLIGHT_STATUSES" in text, (
        "the In progress tab must use the shared status list, not its own copy - types.ts and documents.py "
        "already maintain two copies of the pipeline statuses and a third would be one too many")
    assert re.search(r"label:\s*'Failed'", text) and re.search(r"label:\s*'All'", text)


def test_a_non_admin_gets_no_link_into_a_console_they_cannot_open() -> None:
    """The row link goes to an admin-only detail endpoint. A link that 403s is worse than no link."""
    assert re.search(r"isAdmin\(signedInAs\)\s*\?", source("chat.ts")), (
        "the chat page must only pass documentHref when the viewer can actually open the target")
