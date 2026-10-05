"""Structured-JSON LLM wrapper for the citation subsystem (internal).

Built on the ranking clients (AnthropicClient / OllamaClient), NOT the
streaming drafting SDK. Public callers go through CitationLLMService, never
this class directly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from grant_lab.config import Settings

logger = logging.getLogger(__name__)


class CitationLLMConfigError(RuntimeError):
    """Misconfigured citation LLM (e.g. provider=anthropic with no API key)."""


class CitationLLMOutputError(RuntimeError):
    """The LLM returned content that wasn't a JSON object."""


@dataclass
class LLMUsage:
    tokens_in: int
    tokens_out: int


@dataclass
class LLMResult:
    data: dict
    usage: LLMUsage
    model: str


def tokens_from_usage(usage: dict) -> tuple[int, int]:
    """(tokens_in, tokens_out) from an envelope's `usage`. Anthropic reports
    cache-read / cache-creation tokens SEPARATELY from prompt_tokens; true input
    usage is their sum. Those fields are present but may be None (not absent)
    when caching isn't used, so coerce None -> 0. Ollama omits them entirely
    (the `or 0` handles both)."""
    tokens_in = (
        int(usage.get("prompt_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
    )
    return tokens_in, int(usage.get("completion_tokens") or 0)


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
        tokens_in, tokens_out = tokens_from_usage(usage)
        try:
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise CitationLLMOutputError(f"expected a JSON object, got {type(parsed).__name__}")
        except Exception as e:
            # Answered and billed before parsing failed: the service records
            # this instead of 0.
            e.usage = usage
            raise
        return LLMResult(
            data=parsed,
            usage=LLMUsage(tokens_in=tokens_in, tokens_out=tokens_out),
            model=resp.get("model", self.model),
        )


def build_citation_llm(settings: Settings) -> StructuredLLM:
    provider = settings.citation_llm_provider.lower()
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise CitationLLMConfigError(
                "CITATION_LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is unset"
            )
        if not settings.citation_llm_allow_cloud:
            # Refuse to BUILD, not to send. `build_citation_service` runs inside
            # `create_app` and is not caught, so this is a boot failure — the
            # only gate that can cover the unattended claim-analysis ticks,
            # which reach this service with no request and no user to consent.
            raise CitationLLMConfigError(
                "CITATION_LLM_PROVIDER=anthropic uploads proposal and reference text "
                "to an external API. Set CITATION_LLM_ALLOW_CLOUD=true to permit it, "
                "or CITATION_LLM_PROVIDER=ollama to keep extraction on-prem."
            )
        logger.warning(
            "citation LLM provider is 'anthropic' — uploaded reference text will "
            "be sent to an EXTERNAL API. Set CITATION_LLM_PROVIDER=ollama to keep "
            "extraction on-prem.",
        )
        from grant_lab.ranking.anthropic_client import AnthropicClient

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
        from grant_lab.ranking.ollama_client import OllamaClient

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
