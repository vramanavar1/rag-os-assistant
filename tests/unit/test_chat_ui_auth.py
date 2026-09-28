"""Invariants of the browser sign-in that nothing else in this repo can check.

`chat-ui` has no test runner — its package.json has `build` and `typecheck` and nothing else — so the MSAL wiring
is the one part of the sign-in path with no coverage at all. These are source-level assertions rather than
behavioural tests, and they are deliberately few: only the things that have actually broken, or that must agree
across artefacts nothing links together.

Why this file exists. Sign-in reached a state where the panel answered every click with

    interaction_in_progress: See https://aka.ms/msal.js.errors#interaction_in_progress for details

and only a private window worked. The cause was that `createMsal` called `initialize()` but never
`handleRedirectPromise()`. MSAL writes an "interaction in progress" flag before navigating to Entra, and
`handleRedirectPromise` is the only thing that clears it — with no response to process it calls
`resetRequestCache`, which sets the flag false (verified in
`@azure/msal-browser/dist/cache/BrowserCacheManager.mjs`). The callback page called it; the page that *starts*
the sign-in did not. So any redirect that did not come back cleanly — Back button, or Entra refusing the request,
which is exactly what `AADSTS65005` did — left the flag set for the life of that tab's sessionStorage, and every
later attempt threw. A whole class of "I cannot sign in" with no server-side trace.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "chat-ui" / "src"
ENTRA_TS = SRC / "entra.ts"

# One path, three artefacts, nothing linking them: the browser redirects here, Set-EntraAppRegistration.ps1
# registers it on the app registration, and 00-prereqs.ps1 checks it. Disagree and sign-in fails AADSTS50011.
REDIRECT_PATH = "/auth/callback"


def entra_source() -> str:
    assert ENTRA_TS.exists(), f"{ENTRA_TS} is missing - this file is asserting about nothing"
    return ENTRA_TS.read_text(encoding="utf-8")


def function_body(source: str, name: str) -> str:
    """The text of a top-level `export ... function <name>` up to the closing brace in column 0."""
    match = re.search(rf"^export (?:async )?function {re.escape(name)}\b.*?^}}", source, re.S | re.M)
    assert match, f"{name}() not found in entra.ts - it was renamed or restructured, so re-check the invariant"
    return match.group(0)


def test_creating_msal_completes_any_pending_redirect() -> None:
    """The regression. Without this call a stale interaction flag is never cleared and the sign-in button dies
    for the rest of the tab's life, recoverable only by closing it."""
    body = function_body(entra_source(), "createMsal")
    assert "handleRedirectPromise" in body, (
        "createMsal() must call handleRedirectPromise() on every page load, not only on /auth/callback: it is "
        "the only thing that clears MSAL's interaction-in-progress flag, and a redirect that does not return "
        "cleanly otherwise wedges every later sign-in with interaction_in_progress")


def test_msal_is_initialised_before_the_redirect_is_handled() -> None:
    """MSAL blocks API calls made before initialize() resolves, so the order is load-bearing, not tidiness."""
    body = function_body(entra_source(), "createMsal")
    assert body.index("initialize(") < body.index("handleRedirectPromise"), (
        "initialize() must come before handleRedirectPromise() - MSAL rejects API calls before initialisation")


def test_a_failed_previous_redirect_does_not_disable_the_next_attempt() -> None:
    """handleRedirectPromise rejects when the last round trip failed. Letting that propagate out of createMsal
    would take setUpEntra's catch, return null, and silently remove the Microsoft button - turning one bad
    sign-in into a deployment with no sign-in at all."""
    body = function_body(entra_source(), "createMsal")
    handled = re.search(r"try\s*{[^}]*handleRedirectPromise[^}]*}\s*catch", body, re.S)
    assert handled, "the handleRedirectPromise() call in createMsal must be wrapped in try/catch"


def test_the_scope_is_requested_exactly_as_configured() -> None:
    """The AADSTS65005 lesson. Entra matches the scope string exactly, and the value is whatever
    /api/public-config sent. Building or normalising it here would make the server's setting a lie."""
    source = entra_source()
    for call in re.findall(r"scopes:\s*\[([^\]]*)\]", source):
        assert call.strip() == "this.scope", (
            f"scopes must be passed through verbatim as [this.scope]; found [{call.strip()}]. Appending or "
            "rewriting the scope here is how a configured value stops matching what Entra has.")
    assert "access_as_user" not in source, (
        "the scope name must never be hardcoded in the browser - it comes from ENTRA_API_SCOPE")


def test_the_redirect_path_matches_what_the_infra_scripts_register() -> None:
    """Three files have to agree on this string and nothing imports it across the boundary."""
    assert f"REDIRECT_PATH = '{REDIRECT_PATH}'" in entra_source(), (
        f"the browser's redirect path changed; the scripts below still register {REDIRECT_PATH}")
    for rel in ("infra/scripts/Set-EntraAppRegistration.ps1", "infra/scripts/common.ps1"):
        text = (REPO / rel).read_text(encoding="utf-8")
        assert f"{REDIRECT_PATH}\"" in text or f"{REDIRECT_PATH}'" in text, (
            f"{rel} no longer references {REDIRECT_PATH} - the registered redirect URI and the one the browser "
            "asks for must be identical or sign-in fails with AADSTS50011")


@pytest.mark.parametrize("page", ["chat.ts", "admin.ts", "authcallback.ts"])
def test_every_page_that_uses_msal_goes_through_create_msal(page: str) -> None:
    """The invariant above is only worth anything if no page builds a PublicClientApplication of its own."""
    source = (SRC / page).read_text(encoding="utf-8")
    assert "new PublicClientApplication" not in source, (
        f"{page} constructs MSAL directly and so skips the handleRedirectPromise() in createMsal()")
