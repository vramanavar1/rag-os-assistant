"""Deterministic offline LLM for local development and tests.

Produces an extractive answer from the numbered context blocks so the full pipeline (citations, usage,
refusals) can be exercised without model access. Never selected in production configuration.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from rag_os.application.ports import LlmMessage, LlmProvider, LlmResult
from rag_os.domain.answers import TokenUsage
from rag_os.domain.documents import estimate_tokens
from rag_os.infrastructure.registry import LLMS

_BLOCK = re.compile(r"\[(\d+)\][^\n]*\n(.*?)(?=\n\[\d+\]|\Z)", re.S)


@LLMS.register("fake", description="Offline extractive responder (dev/tests only).")
class FakeLlmProvider(LlmProvider):
    name = "fake"

    def __init__(self, **_: Any) -> None:
        pass

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[LlmMessage],
        max_tokens: int = 2048,
        purpose: str = "answer",
        json_output: bool = False,
    ) -> LlmResult:
        prompt = messages[-1].content if messages else ""
        usage = TokenUsage(input=estimate_tokens(system + prompt), calls=1)
        if json_output:
            text = json.dumps({"facets": {}, "confidence": 0.0})
        elif purpose == "condense":
            text = prompt.rsplit("Follow-up question:", 1)[-1].strip() or prompt
        else:
            blocks = _BLOCK.findall(prompt)
            if not blocks:
                text = "I could not find this in the documents available to you."
            else:
                idx, body = blocks[0]
                sentence = re.split(r"(?<=[.!?])\s", body.strip(), maxsplit=1)[0][:400]
                text = f"{sentence} [{idx}]"
        usage.output = estimate_tokens(text)
        return LlmResult(text=text, usage=usage, provider=self.name, model="fake-extractive", stop_reason="end_turn")
