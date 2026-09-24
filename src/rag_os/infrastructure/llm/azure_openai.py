"""Azure OpenAI (GPT on the Foundry account) chat provider - keyless Entra auth by default."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from rag_os.application.ports import LlmMessage, LlmProvider, LlmResult
from rag_os.domain.answers import TokenUsage
from rag_os.domain.errors import DependencyUnavailable
from rag_os.infrastructure.registry import LLMS

log = logging.getLogger(__name__)


def normalise_openai_usage(usage: Any) -> TokenUsage:
    if usage is None:
        return TokenUsage(calls=1)
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    details = getattr(usage, "prompt_tokens_details", None)
    cached = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
    return TokenUsage(
        input=max(prompt - cached, 0),
        output=int(getattr(usage, "completion_tokens", 0) or 0),
        cache_read=cached,
        calls=1,
    )


@LLMS.register("aoai", description="Azure OpenAI chat deployment (e.g. GPT) on the Foundry account.")
class AzureOpenAIChatProvider(LlmProvider):
    name = "aoai"

    def __init__(self, endpoint: str, deployment: str, api_version: str, api_key: str | None = None, **_: Any):
        from openai import AsyncAzureOpenAI

        if not endpoint or not deployment:
            raise DependencyUnavailable("AOAI_ENDPOINT / AOAI_CHAT_DEPLOYMENT not configured")
        kwargs: dict[str, Any] = {"azure_endpoint": endpoint, "api_version": api_version, "max_retries": 3}
        self._cred = None
        if api_key:
            kwargs["api_key"] = api_key
        else:
            from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

            self._cred = DefaultAzureCredential()
            kwargs["azure_ad_token_provider"] = get_bearer_token_provider(
                self._cred, "https://cognitiveservices.azure.com/.default"
            )
        self._client = AsyncAzureOpenAI(**kwargs)
        self.deployment = deployment

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[LlmMessage],
        max_tokens: int = 2048,
        purpose: str = "answer",
        json_output: bool = False,
    ) -> LlmResult:
        payload: list[dict[str, str]] = [{"role": "system", "content": system}]
        payload += [{"role": m.role, "content": m.content} for m in messages]
        kwargs: dict[str, Any] = {"model": self.deployment, "messages": payload, "max_completion_tokens": max_tokens}
        if json_output:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except Exception as e:
            raise DependencyUnavailable("Azure OpenAI request failed", detail={"error": type(e).__name__}) from e
        choice = resp.choices[0]
        text = choice.message.content or ""
        refused = bool(getattr(choice.message, "refusal", None)) or choice.finish_reason == "content_filter"
        return LlmResult(
            text=text,
            usage=normalise_openai_usage(resp.usage),
            provider=self.name,
            model=resp.model or self.deployment,
            stop_reason=choice.finish_reason,
            refused=refused,
        )

    async def aclose(self) -> None:
        await self._client.close()
        if self._cred is not None:
            await self._cred.close()
