"""Conflict handling: two handbooks that disagree, and a caller who can see both.

`ANSWER_SYSTEM` tells the model to say so when context blocks conflict, and to prefer the one with the most
recent effective date "if shown". It was never shown. `effective_date` was declared in the index schema and
written at index time, but it was absent from `BASE_SELECT`, absent from `SearchHit`, and absent from
`build_context` - three independent breaks, so the second half of that rule could not fire and no parser ever
supplied a date to begin with.

These tests cover everything up to the model. They deliberately do not assert that an answer *worded* a
conflict: the offline LLM is extractive (`infrastructure/llm/fake.py`), taking the first sentence of the
top-ranked block and emitting exactly one citation, and it ignores the system prompt entirely. Seeing the
behaviour itself needs a real model - the README section says so, and says how.
"""

from __future__ import annotations

import pytest

from rag_os.application.ports import DocumentQuery
from rag_os.application.services.index_schema import CURRENT_FILTER
from rag_os.application.services.prompts import build_context
from rag_os.composition import Container
from rag_os.domain.answers import SearchHit

from .test_pipeline import drain, principal

QUESTION = "How much paid parental leave do I get?"
GLOBAL_DOC = "parental-leave-standard"  # 18 weeks, effective 2025-01-01, region Global
UK_DOC = "parental-leave-uk"  # 26 weeks, effective 2026-04-01, region UK


async def ingest_scenario(c: Container) -> None:
    """Both handbooks. They are separate sources on purpose - the disagreement is between systems of record."""
    for source_id in ("scenario-global-handbook", "scenario-uk-handbook"):
        cfg = c.domain.sources.get(source_id)
        assert cfg is not None, f"{source_id} is missing from sources.yaml"
        run = await c.discover.run(c.source_factory.create(cfg), "manual")
        assert run.status.value == "COMPLETED" and run.discovered == 1, (source_id, run)
    assert (await drain(c)).get("indexed") == 2


async def retrieve(c: Container, pid: str, question: str = QUESTION) -> list[SearchHit]:
    """What this caller actually gets back - the same path `ask` takes, before the model sees anything.

    Going through `ask` would not do: the fake LLM cites exactly one block, so `.citations` can never show
    that two documents were both retrieved, which is the whole precondition for a conflict.
    """
    p = principal(c, pid)
    access, deny_all = c.answer.access_filter(p)
    if deny_all:
        return []
    result = await c.retriever.retrieve(query=question, keyword_query=question,
                                        odata_filter=c.answer.combine(CURRENT_FILTER, access), top=8)
    return result.hits


def paths(hits: list[SearchHit]) -> str:
    return " ".join(h.path for h in hits)


async def test_a_uk_employee_retrieves_both_handbooks(container: Container) -> None:
    """The precondition for a conflict: one ordinary caller, two documents that disagree.

    Region matching expands the CALLER's values upward, so Priya in the UK reaches UK and Global alike. That
    is why the conflict had to be Global-vs-UK: the corpus's existing UK-vs-US pair is invisible to everyone
    except an admin bypassing the filter, which would demonstrate the wrong thing.
    """
    c = container
    await ingest_scenario(c)
    hits = await retrieve(c, "hr-emea")
    assert GLOBAL_DOC in paths(hits), "the group standard (18 weeks) must be in scope for a UK employee"
    assert UK_DOC in paths(hits), "and so must the UK handbook (26 weeks)"


@pytest.mark.parametrize("pid", ["sales-us", "support-de"])
async def test_a_caller_outside_hr_sees_neither(container: Container, pid: str) -> None:
    """The control. A conflict is surfaced from what the CALLER can see, not from the corpus as a whole - so
    the same question produces no conflict here, and the fix for a conflict is never to widen access."""
    c = container
    await ingest_scenario(c)
    hits = await retrieve(c, pid)
    assert GLOBAL_DOC not in paths(hits) and UK_DOC not in paths(hits)


async def test_both_documents_carry_their_effective_date_into_a_hit(container: Container) -> None:
    """The round trip that had three breaks in it: front matter -> parser metadata -> index -> BASE_SELECT
    -> SearchHit. Any one of them missing and the model is told nothing about recency."""
    c = container
    await ingest_scenario(c)
    dates = {h.path.split("/")[-1]: h.effective_date for h in await retrieve(c, "hr-emea")}
    assert dates.get(f"{GLOBAL_DOC}.md") == "2025-01-01"
    assert dates.get(f"{UK_DOC}.md") == "2026-04-01"


async def test_the_disagreement_reaches_the_model_as_two_dated_blocks(container: Container) -> None:
    """What the model is actually handed. The dates are what turn "these disagree" into "this one supersedes
    that one" - without them the prompt's rule is unactionable, which is what it was."""
    c = container
    await ingest_scenario(c)
    hits = await retrieve(c, "hr-emea")
    context = build_context(hits)

    assert "(effective 2025-01-01)" in context and "(effective 2026-04-01)" in context
    assert GLOBAL_DOC in context and UK_DOC in context
    # Two distinct numbered blocks: de-duplication collapses byte-identical passages, and these differ.
    assert "[1]" in context and "[2]" in context
    # And the disagreement itself is visible in the text the model reads.
    assert "18 weeks" in context and "26 weeks" in context


async def test_front_matter_never_reaches_the_answer(container: Container) -> None:
    """It is metadata, not content. Left in, it would be embedded, chunked and quoted back in a snippet."""
    c = container
    await ingest_scenario(c)
    hits = await retrieve(c, "hr-emea")
    assert hits, "nothing retrieved, so this would pass vacuously"
    for h in hits:
        assert "effective_date:" not in h.content, f"front matter leaked into {h.path}"
        assert not h.content.lstrip().startswith("---")


async def test_the_scenario_needs_no_classification_configuration(container: Container) -> None:
    """The documents sit at <department>/<region>/<doc_type>/, so the shipped path rules tag them with no
    manifest, no sidecar and no new rules. If that stops being true the scenario has become a special case."""
    c = container
    await ingest_scenario(c)
    for source_id, region in (("scenario-global-handbook", "Global"), ("scenario-uk-handbook", "UK")):
        recs, _ = c.state.query(DocumentQuery(source_id=source_id, limit=10))
        assert len(recs) == 1
        tags = recs[0].tags
        assert tags.facets["department"] == ["HR"]
        assert tags.facets["region"] == [region]
        assert tags.facets["doc_type"] == ["Policy"]
        assert tags.sources["facet:department"] == "path_rule:hr/**", "tagged by path rule, not by hand"
