"""The figures exist twice, and nothing kept the two copies in step.

`README.html` does not link the diagrams — it **inlines** all eight of them. So every figure exists as both
`docs/diagrams/<name>.svg` and a single very long line inside README.html, and an edit applied to one and not the
other ships silently: the standalone file and the GitHub rendering would say one thing while the HTML page says
another. There is no generator to regenerate either from, because the layout script that produced the coordinates
was never committed, so the SVG is the source and the HTML copy is maintained by hand.

The inline copy is not a byte copy. It differs by exactly four documented transformations, and pinning them is
what makes the comparison possible at all:

1. the `<style>` block is dropped — README.html carries those rules once, in its own `<head>`
2. the full-canvas background `<rect ... fill="var(--d-bg)"/>` is dropped — the `.fig-frame` panel supplies it
3. `<title>` moves from after the `<style>` block to immediately after the opening `<svg>` tag
4. a `data-figure="<name>"` attribute is added to the `<svg>` tag

Anything else that differs is drift.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DIAGRAMS = REPO / "docs" / "diagrams"
README_HTML = REPO / "README.html"

# The two pools that exist only when the embedding profile is self-hosted, and the tag that says so.
CONDITIONAL_POOLS = ("rag-embed-query", "rag-embed-ingest")
TEI_TAG = "tei only"
AOAI_TAG = "aoai only"


def diagram_names() -> list[str]:
    names = sorted(p.stem for p in DIAGRAMS.glob("*.svg"))
    assert names, "no diagrams found - this test would pass vacuously"
    return names


def canonical(markup: str) -> str:
    """The comparable form: no style block, no background rect, no title, whitespace collapsed."""
    markup = re.sub(r"<style>.*?</style>", "", markup, flags=re.S)
    markup = re.sub(r"<title>.*?</title>", "", markup, flags=re.S)
    # The full-canvas background. Matched on the fill token rather than on the dimensions, which differ per figure.
    markup = re.sub(r'<rect width="\d+" height="[\d.]+" fill="var\(--d-bg\)"\s*/>', "", markup)
    markup = re.sub(r'\sdata-figure="[a-z-]+"', "", markup)
    return re.sub(r"\s+", " ", markup).strip()


def inline_copy(name: str) -> str:
    html = README_HTML.read_text(encoding="utf-8")
    match = re.search(rf'<svg class="ragos-diagram" data-figure="{re.escape(name)}".*?</svg>', html, re.S)
    assert match, f"README.html has no inline copy of {name}"
    return match.group(0)


@pytest.mark.parametrize("name", diagram_names())
def test_the_inline_copy_matches_the_diagram_file(name: str) -> None:
    """The guard that did not exist. An edit to one copy and not the other is caught here."""
    on_disk = canonical((DIAGRAMS / f"{name}.svg").read_text(encoding="utf-8"))
    in_html = canonical(inline_copy(name))
    if on_disk != in_html:
        # Point at the first divergence rather than printing two 16 KB strings.
        for i, (a, b) in enumerate(zip(on_disk, in_html, strict=False)):
            if a != b:
                raise AssertionError(
                    f"{name}.svg and its README.html copy diverge at character {i}:\n"
                    f"  file: ...{on_disk[max(0, i - 70):i + 70]}\n"
                    f"  html: ...{in_html[max(0, i - 70):i + 70]}")
        raise AssertionError(
            f"{name}: one copy is longer - file {len(on_disk)} chars, README.html {len(in_html)}")


@pytest.mark.parametrize("name", diagram_names())
def test_every_diagram_is_well_formed_xml(name: str) -> None:
    """A malformed figure renders as nothing at all on GitHub, with no error anywhere."""
    ET.fromstring((DIAGRAMS / f"{name}.svg").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", diagram_names())
def test_every_inline_copy_is_well_formed_xml(name: str) -> None:
    """The inline copies are edited by hand in a 16 KB line; a stray bracket is easy and invisible."""
    ET.fromstring(inline_copy(name))


# ------------------------------------------------------------ the profile-conditional workloads are marked
def diagrams_naming_the_pools() -> list[str]:
    found = [n for n in diagram_names()
             if any(p in (DIAGRAMS / f"{n}.svg").read_text(encoding="utf-8") for p in CONDITIONAL_POOLS)]
    assert found, "no diagram mentions the embedder pools - this test would pass vacuously"
    return found


@pytest.mark.parametrize("name", diagrams_naming_the_pools())
def test_a_diagram_showing_an_embedder_pool_says_it_is_conditional(name: str) -> None:
    """Those two apps exist only when the profile has `provider: tei`. A figure that shows them without saying
    so sent a reader hunting for an app their deployment deliberately does not have."""
    text = (DIAGRAMS / f"{name}.svg").read_text(encoding="utf-8")
    shown = [p for p in CONDITIONAL_POOLS if p in text]
    assert TEI_TAG in text, (
        f"{name}.svg shows {', '.join(shown)} but never says '{TEI_TAG}' - the reader cannot tell it is "
        "conditional on the embedding profile")


# The two topology figures. The sequence diagram is deliberately not in this list: its notes panel is full to the
# bottom edge and its legend row has ~50px of clear space, so it keys the tag and leaves the fuller explanation to
# these two rather than carrying a cramped sentence.
TOPOLOGY_FIGURES = ("architecture", "deployment")


@pytest.mark.parametrize("name", TOPOLOGY_FIGURES)
def test_the_topology_figures_state_the_azure_openai_alternative(name: str) -> None:
    """Marking the pools as conditional only answers half of it - a reader still needs to know what replaces
    them. Both figures name the alternative, so neither can be read as "embeddings simply disappear"."""
    text = (DIAGRAMS / f"{name}.svg").read_text(encoding="utf-8")
    assert "text-embedding-3-small" in text, (
        f"{name}.svg tags the pools but never names what a remote profile uses instead")
    # Either spelling of the marker - the figures phrase it to fit the space they have.
    assert AOAI_TAG in text or "azure_openai" in text, (
        f"{name}.svg names the remote model but does not mark it as profile-conditional")
