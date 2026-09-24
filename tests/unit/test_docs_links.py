"""Documentation cross-references must resolve.

Deployment.md is ~1300 lines and is navigated by its table of contents and by "see section N" links. Those are
only useful if they actually land somewhere, and a broken one is invisible until a reader gives up looking -
which is exactly how this test came to exist. Pure text processing: no network, no rendering.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DEPLOYMENT = REPO / "Deployment.md"
README = REPO / "README.md"
TOC_BEGIN, TOC_END = "<!-- toc:begin -->", "<!-- toc:end -->"


def slug(text: str) -> str:
    """GitHub's heading anchor: lowercase, delete punctuation, spaces to hyphens.

    Deleting (rather than replacing) is what makes `Step 07 — \\`07-container-apps.ps1\\`` become
    `step-07--07-container-appsps1`: the em dash vanishes and leaves the spaces either side of it.
    """
    return re.sub(r"[^a-z0-9 _-]", "", text.strip().lower()).replace(" ", "-")


def _outside_code(md: str) -> list[tuple[int, str]]:
    """(line number, text) for lines outside fenced code blocks.

    Code samples legitimately contain `#` comments and bracket syntax; linking or anchor-checking them would
    produce false positives in both directions.
    """
    out, fence = [], False
    for n, line in enumerate(md.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if not fence:
            out.append((n, line))
    return out


def anchors(md: str, levels: str = "1,6") -> set[str]:
    lo, hi = levels.split(",")
    return {slug(m.group(1)) for n, line in _outside_code(md)
            if (m := re.match(rf"^#{{{lo},{hi}}} (.+?)\s*$", line))}


def links(md: str, pattern: str) -> list[tuple[int, str]]:
    return [(n, m.group(1)) for n, line in _outside_code(md) for m in re.finditer(pattern, line)]


DEPLOY_MD = DEPLOYMENT.read_text(encoding="utf-8")
README_MD = README.read_text(encoding="utf-8")
DEPLOY_ANCHORS = anchors(DEPLOY_MD)


def test_deployment_has_a_table_of_contents() -> None:
    assert TOC_BEGIN in DEPLOY_MD and TOC_END in DEPLOY_MD, "the generated contents block is missing"


def test_every_section_and_subsection_is_listed_in_the_contents() -> None:
    """A heading nobody can find from the top of the file may as well not be written.

    Scoped to ## and ### deliberately: the contents list is two levels deep, like README's. #### headings are
    detail inside a subsection and would bury the list rather than help anyone navigate it.
    """
    toc = DEPLOY_MD.split(TOC_BEGIN)[1].split(TOC_END)[0]
    listed = {a for _, a in links(toc, r"\]\(#([^)]+)\)")}
    body = DEPLOY_MD.split(TOC_END)[1]
    missing = sorted(a for a in anchors(body, "2,3") if a not in listed)
    assert not missing, f"sections absent from the contents list: {missing}"


@pytest.mark.parametrize("name", ["Deployment.md", "README.md"])
def test_every_same_document_anchor_resolves(name: str) -> None:
    md = DEPLOY_MD if name == "Deployment.md" else README_MD
    known = anchors(md)
    broken = [(n, a) for n, a in links(md, r"\]\(#([^)]+)\)") if a not in known]
    assert not broken, f"{name} links to anchors that do not exist: {broken}"


def test_every_cross_document_anchor_into_deployment_resolves() -> None:
    """README.md pointed at `Deployment.md#app-settings` for a section actually called `5. Application settings`."""
    broken = [(n, a) for n, a in links(README_MD, r"Deployment\.md#([a-z0-9-]+)") if a not in DEPLOY_ANCHORS]
    assert not broken, f"README.md links into Deployment.md anchors that do not exist: {broken}"


def test_no_markdown_link_hides_inside_a_code_block() -> None:
    """A link written inside a fence renders as literal text - it looks like a typo to the reader."""
    fence, bad = False, []
    for n, line in enumerate(DEPLOY_MD.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence and "](#" in line:
            bad.append(n)
    assert not bad, f"Deployment.md has markdown links inside code blocks at lines {bad}"


def test_prose_section_references_are_links() -> None:
    """`section 4` in prose should be clickable; the whole point is not having to scroll 1300 lines."""
    bare = [(n, line.strip()) for n, line in _outside_code(DEPLOY_MD)
            if re.search(r"(?<!\[)\bsection \d+\b", line) and "mean a numbered section" not in line]
    assert not bare, f"Deployment.md still has unlinked 'section N' references: {bare}"


# Deliberate HTML: Markdown cannot put a bullet list inside a table cell, so README's test-coverage table
# uses real <ul>/<li>. Everything else in prose must be backticked.
ALLOWED_HTML = {"ul", "/ul", "li", "/li", "br", "/br"}
# These do not merely render oddly - they consume everything after them until a closing tag.
SWALLOWING = {"script", "style", "textarea", "iframe", "title"}


def raw_tags(md: str) -> list[tuple[int, str]]:
    """Tag-like text in prose: outside fenced blocks, outside inline code spans, ignoring comments."""
    found = []
    for n, line in _outside_code(md):
        stripped = re.sub(r"`[^`]*`", "", line)          # inline code is safe
        stripped = re.sub(r"<!--.*?-->", "", stripped)   # comments are not elements
        for m in re.finditer(r"<(/?[a-zA-Z][a-zA-Z0-9-]*)\s*/?>", stripped):
            if m.group(1).lower() not in ALLOWED_HTML:
                found.append((n, m.group(0)))
    return found


@pytest.mark.parametrize("name", ["Deployment.md", "README.md"])
def test_angle_bracket_placeholders_are_backticked(name: str) -> None:
    """`Run <script> first` opened a real script element and swallowed the rest of the document.

    Markdown passes raw HTML straight through. An unbackticked `<placeholder>` is not a placeholder, it is a
    tag: most unknown ones are silently dropped, so the text just vanishes - and `script`, `style`, `textarea`
    and `iframe` consume everything after them, which is how 57 headings disappeared and every table-of-contents
    link stopped resolving.
    """
    md = DEPLOY_MD if name == "Deployment.md" else README_MD
    found = raw_tags(md)
    assert not found, f"{name} has raw HTML in prose (backtick it): {found}"


def test_no_unclosed_content_swallowing_tag() -> None:
    """The specific failure: an opening tag with no closing partner eats the remainder of the file."""
    for tag in SWALLOWING:
        opens = [n for n, t in raw_tags(DEPLOY_MD) if t.lower().startswith(f"<{tag}")]
        closes = [n for n, t in raw_tags(DEPLOY_MD) if t.lower().startswith(f"</{tag}")]
        assert len(opens) == len(closes), f"unbalanced <{tag}> in Deployment.md: opens={opens} closes={closes}"
