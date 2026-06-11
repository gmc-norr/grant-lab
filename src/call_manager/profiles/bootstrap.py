from __future__ import annotations

import logging

from call_manager.profiles.bootstrap_prompt import (
    BootstrapParseError,
    build_bootstrap_messages,
    parse_bootstrap_response,
)
from call_manager.profiles.pubmed import PubMedClient, PubMedError
from call_manager.profiles.seed import DraftProfile, SeedInput
from call_manager.ranking.ollama_client import LLMError, OllamaClient

_logger = logging.getLogger(__name__)


def bootstrap_profile(
    *,
    seed: SeedInput,
    pubmed: PubMedClient,
    ollama: OllamaClient,
) -> DraftProfile:
    """Fetch publications (best-effort) + pasted grants → LLM draft.

    Partial failure semantics: if PubMed is unreachable or all members
    return zero hits, we still call the LLM with empty publications and
    whatever grants text the user pasted. If the LLM itself fails, we
    return an empty-ish DraftProfile so the edit screen has a starting
    point — never throws.
    """
    # 1. PubMed (best-effort).
    publications = []
    publications_skipped = 0
    if not seed.members:
        pubmed_status = "not_queried"
    else:
        try:
            publications = pubmed.fetch_publications(seed.members)
            pubmed_status = "ok" if publications else "empty"
        except PubMedError as e:
            pubmed_status = "unavailable"
            _logger.warning(
                "profiles.bootstrap.pubmed_failed",
                extra={"event": "bootstrap_pubmed_failed", "error": str(e)},
            )
    # Count members whose query would have returned zero — this is a
    # rough "how complete is the draft" signal shown on the edit page.
    # With the current batched impl we can't distinguish per-member
    # hits without re-walking, so approximate: if we got zero pubs
    # total but members were provided, that's "all skipped".
    if publications:
        publications_skipped = 0
    elif seed.members:
        publications_skipped = len(seed.members)

    # 2. LLM draft.
    description = ""
    structured: dict = {
        "areas": [],
        "budget_min": None,
        "budget_max": None,
        "trl_min": None,
        "trl_max": None,
        "must_include": [],
        "hard_exclusions": [],
    }
    llm_succeeded = False
    try:
        messages = build_bootstrap_messages(
            profile_name=seed.name,
            kind=seed.kind,
            level=seed.level,
            publications=publications,
            pasted_grants=seed.pasted_grants,
            mission_text=seed.mission_text,
        )
        envelope = ollama.chat(messages=messages)
        content = envelope["choices"][0]["message"].get("content")
        if content:
            draft = parse_bootstrap_response(content)
            description = draft.description
            structured = draft.structured
            llm_succeeded = bool(description and description.strip())
    except (LLMError, BootstrapParseError, KeyError, IndexError, TypeError, AttributeError) as e:
        _logger.warning(
            "profiles.bootstrap.llm_failed",
            extra={"event": "bootstrap_llm_failed", "error": str(e)},
        )

    return DraftProfile(
        description=description,
        structured=structured,
        publications_fetched=len(publications),
        publications_skipped=publications_skipped,
        llm_succeeded=llm_succeeded,
        pubmed_status=pubmed_status,
    )
