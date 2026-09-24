"""Claude on Microsoft Foundry via the official Anthropic SDK (``AsyncAnthropicFoundry``).

Keyless by default: an Entra token provider (managed identity) is passed as ``azure_ad_token_provider``.
The stable system prompt is marked for prompt caching; per-request retrieved context goes in the user turn.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from rag_os.application.ports import LlmMessage, LlmProvider, LlmResult
from rag_os.domain.answers import TokenUsage
from rag_os.domain.errors import DependencyUnavailable
from rag_os.infrastructure.registry import LLMS

log = logging.getLogger(__name__)


def normalise_anthropic_usage(usage: Any) -> TokenUsage:
    if usage is None:
        return TokenUsage(calls=1)
    return TokenUsage(
        input=int(getattr(usage, "input_tokens", 0) or 0),
        output=int(getattr(usage, "output_tokens", 0) or 0),
        cache_read=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        cache_write=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        calls=1,
    )


@LLMS.register("claude", description="Claude (default claude-opus-5) deployed in Microsoft Foundry.")
class ClaudeFoundryProvider(LlmProvider):
    name = "claude"

    def __init__(self, resource: str, model: str = "claude-opus-5", api_key: str | None = None,
                 effort: str | None = None, **_: Any) -> None:
        import anthropic

        if not resource:
            raise DependencyUnavailable("CLAUDE_FOUNDRY_RESOURCE not configured")
        kwargs: dict[str, Any] = {"resource": resource, "max_retries": 3}
        self._cred = None
        if api_key:
            kwargs["api_key"] = api_key
        else:
            from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

            self._cred = DefaultAzureCredential()
            kwargs["azure_ad_token_provider"] = get_bearer_token_provider(
                self._cred, "https://cognitiveservices.azure.com/.default"
            )
        self._client = anthropic.AsyncAnthropicFoundry(**kwargs)
        self._errors = anthropic
        self.model = model
        self.effort = effort

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[LlmMessage],
        max_tokens: int = 2048,
        purpose: str = "answer",
        json_output: bool = False,
    ) -> LlmResult:
        sys_prompt = system
        if json_output:
            sys_prompt += "\n\nRespond with a single JSON object and nothing else."
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": sys_prompt, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        a = self._errors
        try:
            resp = await self._client.messages.create(**kwargs)
        except a.RateLimitError as e:
            raise DependencyUnavailable("Claude rate limited", detail={"status": 429}) from e
        except a.APIStatusError as e:
            raise DependencyUnavailable("Claude request failed", detail={"status": e.status_code}) from e
        except a.APIConnectionError as e:
            raise DependencyUnavailable("Claude endpoint unreachable") from e
        refused = resp.stop_reason == "refusal"
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        return LlmResult(
            text=text,
            usage=normalise_anthropic_usage(resp.usage),
            provider=self.name,
            model=resp.model,
            stop_reason=resp.stop_reason,
            refused=refused,
        )

    async def aclose(self) -> None:
        await self._client.close()
        if self._cred is not None:
            await self._cred.close()
