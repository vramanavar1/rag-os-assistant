"""Invariants of the Settings (Security) admin page.

`chat-ui` has no test runner, so these are source-level assertions in the style of test_chat_ui_account.py.

Why this file exists. This is the one page in the product that changes who can read what, and it is the page
most tempted to grow its own copy of the access rules: a list of departments, the four clearance names, a
special case for "admin". Every one of those copies would be a second place to keep in step with
access-policy.yaml, in a language that cannot read it, inside a bundle nobody audits. So the assertions below
mostly pin what the page must NOT contain.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "chat-ui" / "src"
VIEW = "admin/settings-security.ts"


def source(rel: str) -> str:
    path = SRC / rel
    assert path.exists(), f"{path} is missing - this file is asserting about nothing"
    return path.read_text(encoding="utf-8")


def without_comments(text: str) -> str:
    """The code only. These assertions are about behaviour, and a comment explaining a trap must be able to
    name it without tripping a ban on the literal."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))


def test_the_page_is_registered_in_the_console_nav() -> None:
    """ROUTES is the nav, the router and the title in one array, so an unregistered view is unreachable."""
    admin = source("admin.ts")
    assert "settingsSecurityView" in admin, "the view is not imported"
    assert re.search(r"id:\s*'settings-security'.*view:\s*settingsSecurityView", admin), "no ROUTES entry"


def test_the_browser_does_not_reimplement_the_access_rules() -> None:
    """What hierarchical, max_level and the any_of wildcard mean is decided server-side. This page writes
    access, so a second implementation here would not merely disagree - it would disagree while granting."""
    text = without_comments(source(VIEW))
    for rule in ("max_level", "hierarchical", "any_of", "acl_"):
        assert rule not in text, f"{VIEW} mentions {rule}; the semantics must come from the API"


def test_the_master_lists_are_not_hardcoded() -> None:
    """The assignable values live in access-policy.yaml because a deployment's departments, regions and
    clearance ladder are its own. A copy here would be wrong in every deployment but this one - and it is the
    copy an administrator would be picking from."""
    text = without_comments(source(VIEW))
    for name in ("Public", "Internal", "Confidential", "Restricted", "HR", "Finance", "EMEA"):
        assert not re.search(rf"['\"]{name}['\"]", text), f"{name} is hardcoded in {VIEW}"
    assert "/api/admin/directory" in text, "the lists must be fetched"


def test_the_administrator_role_is_not_singled_out_by_name() -> None:
    """Which role bypasses the document filter is configuration - `roles:` in access-policy.yaml maps it - so
    the API marks the roles that need confirming and the page reads that flag. Hardcoding 'rag.admin' would
    silently stop confirming anything in a deployment that renamed it."""
    text = without_comments(source(VIEW))
    assert "rag.admin" not in text, f"{VIEW} hardcodes rag.admin; use needs_confirmation from the API"
    assert "needs_confirmation" in text, "the page must honour the flag the API sets"


def test_the_page_writes_with_put_and_if_match_only() -> None:
    """A desired-state PUT is what makes the write idempotent against a directory that does not deduplicate
    assignments, and it is why neither the CORS allow-list in api/app.py nor the API client needed a DELETE or
    PATCH method adding. Reaching for one of those would quietly require both."""
    text = without_comments(source(VIEW))
    assert "If-Match" in text, "the write must carry the etag it read, or a stale tab can revoke silently"
    assert ".put<" in text or ".put(" in text
    for method in (".patch(", ".delete(", ".post("):
        assert method not in text, f"{VIEW} uses {method}; the write is a PUT of the desired state"


def test_a_group_derived_role_is_shown_and_not_submitted() -> None:
    """The failure mode most likely to be reported as a security incident: a role assigned to a group reaches
    the token exactly as a direct assignment does. Showing it as unheld would have an administrator grant it
    (creating a second, direct assignment), later revoke that one, and the person would still hold it."""
    text = without_comments(source(VIEW))
    assert "via_group" in text, "a group-derived role must be visible"
    assert re.search(r"disabled:\s*Boolean\(viaGroup\)", text), "and not removable from here"
    assert re.search(r"filter\(\(r\) =>[^\n]*via_group", text), (
        "a group-derived role must be excluded from the submitted role set, or the desired-state write would "
        "try to revoke something it cannot")


def test_the_staleness_warning_comes_from_the_api() -> None:
    """How long an already-issued token keeps the old values is a property of the identity provider, not of
    this page. Writing the sentence here would leave two versions of it to drift."""
    text = without_comments(source(VIEW))
    assert "propagation_note" in text, "the page must render the note the API sends"
    for phrase in ("60", "90", "an hour", "60-90"):
        assert phrase not in text, f"{VIEW} states a token lifetime ({phrase}); that belongs to the API"


def test_the_page_explains_what_revoking_sessions_actually_does() -> None:
    """It invalidates refresh tokens, so it does not end the session the person is currently in, and it signs
    them out of every other application in the tenant. An administrator ticking it deserves both facts."""
    text = source(VIEW)
    assert "revoke_sessions" in text
    assert "tenant" in text, "the tenant-wide effect must be stated beside the checkbox"


def test_clearing_a_value_is_offered_only_where_the_api_permits_it() -> None:
    """A required attribute cleared leaves the person able to read nothing at all. The server refuses it; the
    option should not be there to choose."""
    text = without_comments(source(VIEW))
    assert "clearable" in text, "the clear option must be gated on the flag the API sets"


def test_the_view_does_not_reach_into_the_chat_bundle() -> None:
    """admin.ts and chat.ts are separate esbuild entry points, and this page has no business in the chat one."""
    text = source(VIEW)
    for rel in ("account.ts", "dialog.ts", "uploads-list.ts", "upload.ts"):
        assert rel not in text, f"{VIEW} imports {rel}"
    for other in ("account.ts", "dialog.ts", "uploads-list.ts"):
        assert "settings-security" not in source(other), f"{other} must not import the admin-only page"


def test_every_writable_attribute_gets_a_field_even_when_it_has_no_value() -> None:
    """A person with nothing set is the normal case here, and is exactly who needs the field. So the fields are
    built from the API's attribute list, never gated on the person already having a value - a `state.attributes`
    test around the field would hide the control from the only people it is for."""
    code = without_comments(source(VIEW))
    build = code.split("const attributeFields", 1)
    assert len(build) == 2, "the attribute fields moved; re-point this test"
    assert "cap.attributes.map(" in build[1], (
        "the fields must come from the API's writable-attribute list, one each, unconditionally")
    head = build[1][: build[1].index("selects.set")]
    assert "state.attributes[spec.name] ?? null" in head, (
        "the person's current value selects an option; it must not decide whether the field exists")
    for gate in ("if (state.attributes", "state.attributes[spec.name] &&", ".filter("):
        assert gate not in head, f"the field list is gated on the current value by {gate!r}"


def test_an_attribute_with_no_configured_values_explains_why() -> None:
    """The symptom that started this: a dropdown holding only "Not set", with the reason (the policy in the
    config store, which is not the repo checkout) nowhere on screen. An empty master list has to say so."""
    code = without_comments(source(VIEW))
    assert "spec.values.length" in code, "the empty master list must be a case the page handles"
    assert "allowed_values" in code, "name the policy key, so the reader knows what to add"
    assert "Config" in code, "and where to add it - the console edits the copy the API actually reads"
    assert re.search(r"if \(!spec\.values\.length\)\s*select\.disabled = true", code), (
        "a select with nothing to choose must be disabled, not merely empty")
    assert "No writable attributes are configured" in code, (
        "an empty attribute list must say so rather than rendering an empty card")
