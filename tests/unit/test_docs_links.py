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


# ---------------------------------------------------------------------- links BETWEEN the two documents
# Same-document anchors were checked from the start; cross-document ones never were, so README could point at a
# Deployment.md heading that had been renamed and nothing noticed. The failure mode is identical either way -
# the reader lands at the top of a 1400-line file and gives up.
CROSS = {"Deployment.md": DEPLOYMENT, "README.md": README}


def test_every_cross_document_anchor_resolves() -> None:
    broken = []
    for source_name, source in CROSS.items():
        md = source.read_text(encoding="utf-8")
        for target_name, target in CROSS.items():
            if target_name == source_name:
                continue
            known = anchors(target.read_text(encoding="utf-8"))
            pattern = rf"\]\({re.escape(target_name)}#([^)]+)\)"
            for n, anchor in links(md, pattern):
                if anchor not in known:
                    broken.append(f"{source_name}:{n} -> {target_name}#{anchor}")
    assert not broken, "links to headings in the other document that do not exist:\n  " + "\n  ".join(broken)


# ----------------------------------------------------- the documented workloads must match the ones 07 deploys
# Which container apps exist depends on the embedding profile's provider, and that fact is now written down in two
# tables. Documentation that contradicts the script is worse than none: it is what sent a reader looking for
# `rag-embed-query` on a deployment that deliberately does not have it. The list lives in exactly one place in
# code, so the tables can be checked against it.
SEVEN = ("rag-api", "rag-chat-ui", "rag-ingest-worker", "rag-scheduler", "rag-bootstrap",
         "rag-embed-query", "rag-embed-ingest")
LEGEND_MARKERS = ("both", "tei only", "aoai only")


def workloads_from_step_07() -> tuple[set[str], set[str]]:
    """(every workload 07 can deploy, the ones it gates behind $useTei) - read from the script, not a constant."""
    src = (REPO / "infra" / "scripts" / "07-container-apps.ps1").read_text(encoding="utf-8")
    start = src.index("$workloads = @(")
    unconditional = set(re.findall(r"Name = '([a-z-]+)'", src[start:src.index(")", start)]))
    gated_line = next(ln for ln in src.splitlines() if ln.startswith("if ($useTei) { $workloads ="))
    gated = set(re.findall(r"Name = '([a-z-]+)'", gated_line))
    assert unconditional and gated, "could not read the workload list out of 07"
    return unconditional | gated, gated


def test_the_script_still_deploys_the_seven_workloads_the_docs_describe() -> None:
    """A new workload has to be documented, not silently absent from both tables."""
    everything, gated = workloads_from_step_07()
    assert everything == set(SEVEN), (
        f"07 deploys a different set than the docs describe: only in 07 {everything - set(SEVEN)}, "
        f"only in the docs {set(SEVEN) - everything}")
    assert gated == {"rag-embed-query", "rag-embed-ingest"}, (
        f"the embedding-profile gate covers {gated}; the docs mark exactly the two embedder pools as 'tei only'")


def test_every_workload_appears_in_the_appendix_with_a_legend_marker() -> None:
    md = DEPLOYMENT.read_text(encoding="utf-8")
    appendix = md[md.index("## Appendix — what runs where"):]
    table = appendix[:appendix.index("\n\n", appendix.index("| Workload |"))]
    everything, gated = workloads_from_step_07()
    for name in everything:
        row = next((ln for ln in table.splitlines() if f"`{name}`" in ln), None)
        assert row, f"{name} is deployed by 07 but has no row in the appendix table"
        marker = "tei only" if name in gated else "both"
        assert f"**{marker}**" in row, f"{name} should be marked **{marker}** in the appendix, got: {row.strip()}"


def test_the_legend_defines_every_marker_the_tables_use() -> None:
    """A marker nobody defined is just a word in a column."""
    md = DEPLOYMENT.read_text(encoding="utf-8")
    legend = md[md.index("#### What you end up running"):]
    legend = legend[:legend.index("####", 10)]
    for marker in LEGEND_MARKERS:
        assert f"| **{marker}** |" in legend, f"the legend does not define '{marker}'"
    used = set(re.findall(r"\*\*(both|tei only|aoai only)\*\*", md))
    assert used <= set(LEGEND_MARKERS), f"undefined markers in use: {used - set(LEGEND_MARKERS)}"


def test_step_07s_expected_output_does_not_claim_the_embedder_pools_always_exist() -> None:
    """It used to, which made a correct remote-profile deployment look broken during verification."""
    md = DEPLOYMENT.read_text(encoding="utf-8")
    expectation = md[md.index("Expected, on **any** profile:"):][:600]
    assert "rag-embed-query" in expectation, "the self-hosted case still has to be stated"
    assert "not\ndeployed" in expectation or "not deployed" in expectation, (
        "it must say the pools are deliberately absent on a remote profile")


# ---------------------------------------------------------------- README.html
# Everything above reads Markdown. README.html is the same document in another format, with its own hand-built
# table of contents and its own anchors, and nothing checked it - so renumbering a section silently broke every
# link into the ones after it. That is exactly what inserting "User query handling scenarios" as section 11
# required, across six sections and both files.

HTML = REPO / "README.html"


def test_every_link_inside_readme_html_resolves() -> None:
    text = HTML.read_text(encoding="utf-8")
    ids = set(re.findall(r'\sid="([^"]+)"', text))
    refs = set(re.findall(r'href="#([^"]+)"', text))
    assert refs, "no internal links found - this test would pass vacuously"
    dangling = sorted(refs - ids)
    assert not dangling, (
        "these anchors point at nothing in README.html:\n  " + "\n  ".join(dangling) +
        "\nUsually a section was renumbered and its incoming links were not.")


def test_readme_html_section_numbers_match_their_anchors() -> None:
    """The badge and the id are written by hand on the same line, and a renumbering that updates one and not
    the other reads correctly while linking wrongly."""
    text = HTML.read_text(encoding="utf-8")
    wrong = [f"id={anchor} shows {shown}"
             for anchor, shown in re.findall(r'<h2 id="(\d+)-[^"]*"><span class="n">(\d+)</span>', text)
             if anchor != shown]
    assert not wrong, "section number and anchor disagree: " + "; ".join(wrong)


def test_both_readmes_have_the_same_sections_in_the_same_order() -> None:
    """They are hand-maintained copies of one document. Nothing has ever checked that they still agree, which
    is how a section ends up in one and not the other - and how the numbering drifts apart."""
    md = (REPO / "README.md").read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")
    md_sections = re.findall(r"^## (\d+)\. ", md, re.M)
    html_sections = [n for n, _ in re.findall(r'<h2 id="(\d+)-[^"]*"><span class="n">(\d+)</span>', html)]
    assert md_sections == html_sections, (
        f"README.md has sections {md_sections} and README.html has {html_sections}. "
        "They are the same document in two formats and must carry the same sections, numbered alike.")
    assert md_sections == [str(i) for i in range(1, len(md_sections) + 1)], (
        f"section numbers must run 1..n with no gaps; got {md_sections}")


def test_both_readmes_say_where_to_verify_the_graph_permissions() -> None:
    """The three Graph permissions sit on the managed identity, and the blade an operator reaches for first is the
    app registration's - where they are absent by design, which reads exactly like a failed grant. The portal also
    cannot grant them at all. Both facts have to be written down, in both formats, or the question gets asked
    again."""
    md = (REPO / "README.md").read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")
    assert "#### Seeing what is actually granted" in md
    assert 'id="graph-permissions-verify"' in html, "README.html needs the subsection and its anchor"
    assert '<a href="#graph-permissions-verify">' in html, "and a nav entry pointing at it"
    for name, text in (("README.md", md), ("README.html", html)):
        assert "Managed Identities" in text, (
            f"{name} must name the Application type filter value; the default hides managed identities, which is "
            "the step that makes the list look empty")
        assert "-List" in text, f"{name} must give the command-line check that is authoritative"
