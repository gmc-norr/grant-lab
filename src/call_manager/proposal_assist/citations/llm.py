"""Structured-JSON LLM wrapper for the citation subsystem (internal).

Built on the ranking clients (AnthropicClient / OllamaClient), NOT the
streaming drafting SDK. Public callers go through CitationLLMService, never
this class directly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from call_manager.config import Settings


class CitationLLMConfigError(RuntimeError):
    """Misconfigured citation LLM (e.g. provider=anthropic with no API key)."""


@dataclass
class LLMUsage:
    tokens_in: int
    tokens_out: int


@dataclass
class LLMResult:
    data: dict
    usage: LLMUsage
    model: str


class StructuredLLM:
    def __init__(self, client, model: str) -> None:
        self._client = client
        self.model = model

    def complete_json(self, *, system: str, user: str) -> LLMResult:
        resp = self._client.chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
        )
        content = resp["choices"][0]["message"]["content"]
        usage = resp.get("usage") or {}
        # Anthropic reports cache-read / cache-creation tokens SEPARATELY from
        # prompt_tokens; true input usage is their sum. Ollama returns neither
        # (the .get(...,0) defaults make this safe for both).
        tokens_in = (
            int(usage.get("prompt_tokens", 0))
            + int(usage.get("cache_read_input_tokens", 0))
            + int(usage.get("cache_creation_input_tokens", 0))
        )
        return LLMResult(
            data=json.loads(content),
            usage=LLMUsage(tokens_in=tokens_in, tokens_out=int(usage.get("completion_tokens", 0))),
            model=resp.get("model", self.model),
        )


def build_citation_llm(settings: Settings) -> StructuredLLM:
    provider = settings.citation_llm_provider.lower()
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise CitationLLMConfigError(
                "CITATION_LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is unset"
            )
        from call_manager.ranking.anthropic_client import AnthropicClient

        model = settings.citation_llm_model or settings.remote_rank_model
        return StructuredLLM(
            AnthropicClient(
                api_key=settings.anthropic_api_key,
                model=model,
                max_tokens=settings.citation_llm_max_output_tokens,
                timeout=settings.citation_llm_timeout_seconds,
            ),
            model,
        )
    if provider == "ollama":
        from call_manager.ranking.ollama_client import OllamaClient

        model = settings.citation_llm_model or settings.gemma_model
        return StructuredLLM(
            OllamaClient(
                base_url=settings.ollama_url,
                model=model,
                timeout=settings.citation_llm_timeout_seconds,
                num_predict=settings.citation_llm_max_output_tokens,
            ),
            model,
        )
    raise CitationLLMConfigError(f"unknown CITATION_LLM_PROVIDER: {provider!r}")
