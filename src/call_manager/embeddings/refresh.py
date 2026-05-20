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

from call_manager.db.models import Call, Profile
from call_manager.embeddings.similarity import build_call_text, build_profile_text
from call_manager.ranking.ollama_client import LLMError

if TYPE_CHECKING:
    from call_manager.embeddings.client import OllamaEmbedClient

_logger = logging.getLogger(__name__)


def refresh_call_embedding(
    session,
    embed_client: OllamaEmbedClient,
    call_id: int,
    *,
    force: bool = False,
) -> bool:
    """Embed a single call. Returns True on success, False on LLM error
    or missing call. Caller commits the session.

    When `force=True` and the embed call raises LLMError, BOTH `embedding`
    and `embedding_model` are nulled out. Force mode means "the existing
    vector is no longer correct" (e.g., the embedded text format changed).
    Leaving the row with its OLD vector AND current `embedding_model` would
    make it invisible to the daily refresh tick (which selects on
    NULL-or-mismatched-model) — silent permanent staleness. Nulling lets
    the daily tick retry tomorrow; meanwhile the embedding gate sees None
    → UNKNOWN → falls back to the LLM, which is safer than ranking against
    a known-stale vector.
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
                "kind": "call",
                "id": call_id,
                "error": str(e),
                "force": force,
            },
        )
        if force:
            call.embedding = None
            call.embedding_model = None
        return False
    call.embedding = vec
    call.embedding_model = embed_client.model
    return True


def refresh_profile_embedding(
    session,
    embed_client: OllamaEmbedClient,
    profile_id: int,
    *,
    force: bool = False,
) -> bool:
    """Embed a single profile (description only). Returns True on success.

    Codex M2 fix: when the description is blank or whitespace, NULL out
    any existing embedding + embedding_model. Otherwise a profile that
    used to have description='Cancer research' but was edited to '' would
    keep its stale 'cancer' vector and be silently gated against future
    on-topic calls.

    Same `force=True` semantics as refresh_call_embedding: on LLMError
    in force mode, NULL both columns so the daily tick can re-pick the
    row up via its NULL filter. See that helper for the full rationale.
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
                "kind": "profile",
                "id": profile_id,
                "error": str(e),
                "force": force,
            },
        )
        if force:
            profile.embedding = None
            profile.embedding_model = None
        return False
    profile.embedding = vec
    profile.embedding_model = embed_client.model
    return True


def refresh_missing_embeddings_tick(
    *,
    session_factory,
    embed_client: OllamaEmbedClient,
    batch_size: int = 200,
    force: bool = False,
    after_call_id: int = 0,
    after_profile_id: int = 0,
) -> dict[str, int]:
    """Daily tick: scan for NULL-embedding rows AND rows whose embedding_model
    differs from the currently-configured model — embed both. Per-row
    try/except so one bad row doesn't kill the batch. Returns a summary
    dict for logging.

    Codex H2 fix: scoping by `embedding IS NULL OR embedding_model != current`
    means a model swap (env change of EMBEDDING_MODEL) is recovered by the
    daily tick within at most 24 h. Operators can run the backfill CLI
    (`python -m call_manager.embeddings.cli backfill`) for immediate refresh.

    Plan F Task 7: when `force=True`, the NULL/mismatch filter is dropped and
    every row is re-embedded. Used after deploys that change the text being
    embedded (e.g., Plan F's full_text rollout). The blank-title and
    blank-description guards are KEPT under force=True — otherwise rows with
    no embeddable text would infinite-loop the backfill (select NULL → no-op
    early-return → still NULL → re-select).

    `after_call_id` / `after_profile_id` are id-cursors used by the backfill
    CLI to walk forward through the table when force=True (without them, the
    same rows would be re-selected every iteration). The summary includes
    `max_call_id` / `max_profile_id` so the caller can advance the cursor.
    Default 0 = start from the beginning (matches the daily-tick behavior).
    """
    current_model = embed_client.model
    summary = {
        "calls_embedded": 0,
        "calls_failed": 0,
        "profiles_embedded": 0,
        "profiles_failed": 0,
        "max_call_id": after_call_id,
        "max_profile_id": after_profile_id,
    }

    with session_factory() as session:
        call_q = select(Call.id)
        if not force:
            call_q = call_q.where(
                or_(Call.embedding.is_(None), Call.embedding_model != current_model)
            )
        call_q = (
            call_q.where(Call.id > after_call_id)
            .where(func.length(func.trim(Call.title)) > 0)
            .order_by(Call.id)
            .limit(batch_size)
        )
        call_ids = session.execute(call_q).scalars().all()
    for cid in call_ids:
        with session_factory() as session:
            ok = refresh_call_embedding(session, embed_client, cid, force=force)
            session.commit()
        if ok:
            summary["calls_embedded"] += 1
        else:
            summary["calls_failed"] += 1
    if call_ids:
        summary["max_call_id"] = max(call_ids)

    with session_factory() as session:
        # Codex #2: `build_profile_text` now uses description + areas, so
        # the old SQL pre-filter on `length(trim(description)) > 0` would
        # incorrectly skip an areas-only profile. Selecting all candidates
        # and filtering in Python via `build_profile_text` is robust to
        # future text-composition changes (no duplicated rules in SQL) and
        # keeps the CLI's "no progress ⇒ break" termination correct: a
        # truly blank profile isn't selected, so `profiles_embedded` stays
        # at 0 and the CLI breaks. Batch-size overshoot is fine — callers
        # aren't counting rows returned, they're counting embeds performed.
        profile_q = select(Profile)
        if not force:
            profile_q = profile_q.where(
                or_(Profile.embedding.is_(None), Profile.embedding_model != current_model)
            )
        profile_q = (
            profile_q.where(Profile.id > after_profile_id).order_by(Profile.id).limit(batch_size)
        )
        profiles = session.execute(profile_q).scalars().all()
        profile_ids = [p.id for p in profiles if build_profile_text(p).strip()]
        if profiles and not profile_ids:
            # We paged past the end of the embeddable set — advance the
            # cursor anyway so the next CLI iteration doesn't re-read the
            # same blank profiles and infinite-loop in force mode.
            summary["max_profile_id"] = max(p.id for p in profiles)
    for pid in profile_ids:
        with session_factory() as session:
            ok = refresh_profile_embedding(session, embed_client, pid, force=force)
            session.commit()
        if ok:
            summary["profiles_embedded"] += 1
        else:
            summary["profiles_failed"] += 1
    if profile_ids:
        summary["max_profile_id"] = max(profile_ids)

    _logger.info(
        "embeddings.refresh_missing_tick.summary",
        extra={"event": "embedding_refresh_tick_summary", **summary},
    )
    return summary
