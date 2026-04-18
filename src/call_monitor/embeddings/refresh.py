"""Embedding refresh — write paths for call/profile embeddings.

Used by:
- ingestion orchestrator (per new call)
- profile create/edit handler (sync re-embed on description change)
- daily refresh-missing tick (catches what slipped through)
- one-time backfill CLI
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select

from call_monitor.db.models import Call, Profile
from call_monitor.embeddings.similarity import build_call_text, build_profile_text
from call_monitor.ranking.ollama_client import LLMError

if TYPE_CHECKING:
    from call_monitor.embeddings.client import OllamaEmbedClient

_logger = logging.getLogger(__name__)


def refresh_call_embedding(session, embed_client: OllamaEmbedClient, call_id: int) -> bool:
    """Embed a single call. Returns True on success, False on LLM error
    or missing call. Caller commits the session.
    """
    call = session.get(Call, call_id)
    if call is None:
        return False
    text = build_call_text(call)
    if not text.strip():
        # Nothing to embed; leave embedding NULL.
        return False
    try:
        vec = embed_client.embed(text)
    except LLMError as e:
        _logger.warning(
            "embeddings.embedding_compute_failed",
            extra={
                "event": "embedding_compute_failed",
                "kind": "call", "id": call_id, "error": str(e),
            },
        )
        return False
    call.embedding = vec
    call.embedding_model = embed_client.model
    return True


def refresh_profile_embedding(session, embed_client: OllamaEmbedClient, profile_id: int) -> bool:
    """Embed a single profile (description only). Returns True on success.

    Codex M2 fix: when the description is blank or whitespace, NULL out
    any existing embedding + embedding_model. Otherwise a profile that
    used to have description='Cancer research' but was edited to '' would
    keep its stale 'cancer' vector and be silently gated against future
    on-topic calls.
    """
    profile = session.get(Profile, profile_id)
    if profile is None:
        return False
    text = build_profile_text(profile)
    if not text.strip():
        # Description cleared → invalidate the embedding. Counts as a
        # successful refresh (we're keeping state consistent), so return True.
        if profile.embedding is not None or profile.embedding_model is not None:
            profile.embedding = None
            profile.embedding_model = None
            _logger.info(
                "embeddings.profile_embedding_cleared",
                extra={"event": "profile_embedding_cleared", "profile_id": profile_id},
            )
        return True
    try:
        vec = embed_client.embed(text)
    except LLMError as e:
        _logger.warning(
            "embeddings.embedding_compute_failed",
            extra={
                "event": "embedding_compute_failed",
                "kind": "profile", "id": profile_id, "error": str(e),
            },
        )
        return False
    profile.embedding = vec
    profile.embedding_model = embed_client.model
    return True


def refresh_missing_embeddings_tick(
    *,
    session_factory,
    embed_client: OllamaEmbedClient,
    batch_size: int = 200,
) -> dict[str, int]:
    """Daily tick: scan for NULL-embedding rows AND rows whose embedding_model
    differs from the currently-configured model — embed both. Per-row
    try/except so one bad row doesn't kill the batch. Returns a summary
    dict for logging.

    Codex H2 fix: scoping by `embedding IS NULL OR embedding_model != current`
    means a model swap (env change of EMBEDDING_MODEL) is recovered by the
    daily tick within at most 24 h. Operators can run the backfill CLI
    (`python -m call_monitor.embeddings.cli backfill`) for immediate refresh.
    """
    current_model = embed_client.model
    summary = {"calls_embedded": 0, "calls_failed": 0,
               "profiles_embedded": 0, "profiles_failed": 0}

    with session_factory() as session:
        call_ids = session.execute(
            select(Call.id)
            .where(or_(Call.embedding.is_(None), Call.embedding_model != current_model))
            .where(func.length(func.trim(Call.title)) > 0)
            .limit(batch_size)
        ).scalars().all()
    for cid in call_ids:
        with session_factory() as session:
            ok = refresh_call_embedding(session, embed_client, cid)
            session.commit()
        if ok:
            summary["calls_embedded"] += 1
        else:
            summary["calls_failed"] += 1

    with session_factory() as session:
        profile_ids = session.execute(
            select(Profile.id)
            .where(or_(Profile.embedding.is_(None), Profile.embedding_model != current_model))
            .where(func.length(func.trim(Profile.description)) > 0)
            .limit(batch_size)
        ).scalars().all()
    for pid in profile_ids:
        with session_factory() as session:
            ok = refresh_profile_embedding(session, embed_client, pid)
            session.commit()
        if ok:
            summary["profiles_embedded"] += 1
        else:
            summary["profiles_failed"] += 1

    _logger.info(
        "embeddings.refresh_missing_tick.summary",
        extra={"event": "embedding_refresh_tick_summary", **summary},
    )
    return summary
