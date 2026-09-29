"""Invariants of the Account Information panel.

`chat-ui` has no test runner, so these are source-level assertions in the style of test_chat_ui_auth.py and
test_chat_ui_uploads.py; those files' docstrings explain the tradeoff.

Why this file exists. A signed-in user could learn nothing about their own identity: the chip said "Signed in
as X" and hid everything else in a `title` tooltip, where `attributeSummary` stringified raw pairs with no
lookup table - so a caller was shown `clearance=2` with nothing anywhere in the product that could turn 2 into
a word. The ladder lived in a YAML comment and the application roles lived in a PowerShell provisioning
script, neither of which a browser can read.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "chat-ui" / "src"


def source(rel: str) -> str:
    path = SRC / rel
    assert path.exists(), f"{path} is missing - this file is asserting about nothing"
    return path.read_text(encoding="utf-8")


def test_the_identity_chip_is_operable_by_keyboard_when_it_opens_the_panel() -> None:
    """The chip is a <div>. Hanging an onclick on it makes a control that a mouse can use and a keyboard
    cannot - the classic way a feature ships inaccessible."""
    body = source("ui.ts")
    chip = body[body.index("export function identityChip"):]
    assert "role: 'button'" in chip and "tabindex: '0'" in chip, "a clickable div needs both to be a control"
    assert "onkeydown" in chip and "'Enter'" in chip and "' '" in chip, "Enter and Space must activate it"


def test_both_pages_offer_the_panel() -> None:
    """It describes the caller, not the console, so it belongs everywhere a person is signed in."""
    for rel in ("chat.ts", "admin.ts"):
        assert "openAccountDialog" in source(rel), f"{rel} does not wire Account Information"


def test_the_panel_does_not_drag_the_admin_console_into_the_chat_bundle() -> None:
    """chat.ts and admin.ts are separate esbuild entry points. The dialog helper was extracted out of
    admin/common.ts precisely so this import could not happen."""
    for rel in ("account.ts", "dialog.ts"):
        text = source(rel)
        assert "./admin/" not in text and "from './admin" not in text, f"{rel} imports from admin/"


def test_the_panel_renders_the_whole_ladder_not_just_your_rung() -> None:
    """Marking your level is only useful beside the others: the request was for the levels "with note
    description of the all levels for clarity"."""
    text = source("account.ts")
    assert "attr.levels.map" in text, "every rung must be rendered"
    assert "is-current" in text, "...and yours marked among them"
    assert "lvl.description" in text, "...with its description, which is the clarity being asked for"


def test_roles_are_shown_as_the_app_roles_from_the_registration() -> None:
    """Permissions are granted by assigning an application role in Entra, so that is the unit to show -
    including the ones you do not hold, which answers "what could I be given?"."""
    text = source("account.ts")
    assert "app_roles" in text and "r.held" in text
    assert "unrecognised_roles" in text, (
        "a token role matching no app role is a misspelled assignment; it used to be dropped silently and "
        "must now be visible")


def test_the_browser_does_not_reimplement_the_access_rules() -> None:
    """The meaning of hierarchical, max_level and the any_of wildcard is decided server-side and rendered
    here. Two implementations of an access rule is one too many, and the copy in a downloadable bundle is the
    worse place to discover they disagree."""
    text = source("account.ts")
    for rule in ("max_level", "hierarchical", "any_of", "acl_"):
        assert rule not in text, f"account.ts mentions {rule}; the summary must come from the API"
    assert "/api/me/account" in text


def test_the_clearance_ladder_is_configuration_rather_than_four_hardcoded_names() -> None:
    """max_level is generic - a deployment may run 0-5 - so the names belong in access-policy.yaml, and a
    browser that hardcoded them would be wrong everywhere but here."""
    text = source("account.ts")
    for name in ("Public", "Internal", "Confidential", "Restricted"):
        assert not re.search(rf"['\"]{name}['\"]", text), f"{name} is hardcoded in account.ts"
    policy = (REPO / "config" / "access-policy" / "access-policy.yaml").read_text(encoding="utf-8")
    assert "levels:" in policy, "the ladder must exist as data for the panel to render"
