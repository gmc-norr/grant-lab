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

import httpx
from sqlalchemy import or_, select

from grant_lab.db.models import Call, Profile
from grant_lab.embeddings.similarity import build_call_text, build_profile_text
from grant_lab.profiles.fingerprint import embedding_fingerprint
from grant_lab.ranking.ollama_client import LLMError

if TYPE_CHECKING:
    from grant_lab.embeddings.client import OllamaEmbedClient

_logger = logging.getLogger(__name__)


def refresh_call_embedding(
    session,
    embed_client: OllamaEmbedClient,
    call_id: int,
    *,
    force: bool = False,
    _http: httpx.Client | None = None,
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

    ``_http`` — optional persistent :class:`httpx.Client` for batch callers
    (see :meth:`OllamaEmbedClient.session`). When supplied, the same TCP
    connection is reused across repeated calls instead of opening a fresh one
    per call. The caller is responsible for the client's lifetime.
    """
    call = session.get(Call, call_id)
    if call is None:
        return False
    text = build_call_text(call)
    if not text.strip():
        # Nothing to embed; leave embedding NULL.
        return False
    try:
        result = embed_client.embed(text, _client=_http)
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
    call.embedding = result.vector
    call.embedding_model = embed_client.model
    return True


def refresh_profile_embedding(
    session,
    embed_client: OllamaEmbedClient,
    profile_id: int,
    *,
    force: bool = False,
    _http: httpx.Client | None = None,
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

    ``_http`` — optional persistent :class:`httpx.Client` for batch callers.
    See :func:`refresh_call_embedding` for usage notes.
    """
    profile = session.get(Profile, profile_id)
    if profile is None:
        return False
    text = build_profile_text(profile)
    if not text.strip():
        # Description cleared → invalidate the embedding. Counts as a
        # successful refresh (we're keeping state consistent), so return True.
        if (
            profile.embedding is not None
            or profile.embedding_model is not None
            or profile.embedding_fingerprint is not None
        ):
            profile.embedding = None
            profile.embedding_model = None
            profile.embedding_fingerprint = None
            _logger.info(
                "embeddings.profile_embedding_cleared",
                extra={"event": "profile_embedding_cleared", "profile_id": profile_id},
            )
        return True
    try:
        result = embed_client.embed(text, _client=_http)
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
            profile.embedding_fingerprint = None
        return False
    profile.embedding = result.vector
    profile.embedding_model = embed_client.model
    profile.embedding_fingerprint = embedding_fingerprint(profile)
    return True


# Safety cap on the number of batches the daily tick will drain in one call.
# At batch_size=200 this is 200*200 = 40k rows of progress per tick — well
# beyond any realistic backlog, while still bounding the worst case so a
# pathological "always-makes-1-row-of-progress" loop can't run unbounded.
_TICK_MAX_BATCHES = 200


def refresh_missing_embeddings_tick(
    *,
    session_factory,
    embed_client: OllamaEmbedClient,
    batch_size: int = 200,
    force: bool = False,
    after_call_id: int = 0,
    after_profile_id: int = 0,
    max_batches: int = _TICK_MAX_BATCHES,
) -> dict[str, int]:
    """Daily tick entry point. Drains the NULL/mismatched-model backlog by
    looping `_refresh_missing_embeddings_batch` until a batch makes no
    progress (everything left is failing) or `max_batches` is reached.

    The summary carries `scan_exhausted`: True when the drain ran off the end
    of both tables, False when it stopped on `max_batches` with rows still
    ahead of the cursor. The daily caller needs that distinction to decide
    whether to carry `max_profile_id` into tomorrow's tick or reset to 0 —
    see `_embeddings_tick` in scheduler.py.

    A single batch processes at most `batch_size` rows. Before this drain
    loop, recovery after a model swap silently took (backlog / batch_size)
    DAYS — one batch per daily tick. Now a single tick catches up the whole
    embeddable backlog (bounded by `max_batches` as a safety cap).

    Force mode (cursor-driven backfill from the CLI) is NOT drained here:
    the CLI owns the cursor walk via `after_call_id`/`after_profile_id` and
    calls the batch directly through this function with `max_batches=1`
    semantics. So under `force=True` we run exactly one batch and return,
    preserving the cursor contract.
    """
    if force:
        # Single batch — the CLI's outer loop drives cursor pagination.
        return _refresh_missing_embeddings_batch(
            session_factory=session_factory,
            embed_client=embed_client,
            batch_size=batch_size,
            force=True,
            after_call_id=after_call_id,
            after_profile_id=after_profile_id,
        )

    totals = {
        "calls_embedded": 0,
        "calls_failed": 0,
        "profiles_embedded": 0,
        "profiles_failed": 0,
        "profiles_seen": 0,
        "max_call_id": after_call_id,
        "max_profile_id": after_profile_id,
        # Did the drain walk PAST THE LAST ROW, or did it stop on max_batches
        # with rows still ahead of the cursor? The counts cannot tell those
        # apart: both come back with a cursor and possibly some work done.
        # Named for the condition it reports rather than for its consumer
        # ("capped" would name the other branch and reads as a config flag),
        # because the caller's rule is stated in those terms: carry the cursor
        # across a capped tick, reset it only on an exhausted scan. It is a
        # bool; `bool` subclasses `int`, so the dict[str, int] return holds.
        # Set on the non-force drain only — under force this function runs a
        # single batch and the CLI owns the cursor walk and its termination.
        "scan_exhausted": False,
    }
    # Walk forward with id-cursors so each batch processes NEW rows. This
    # both lets us drain a backlog larger than one batch AND avoids re-trying
    # the same just-failed rows within a single tick (a failed row stays NULL
    # but its id is behind the cursor, so the next daily tick retries it).
    cursor_call_id = after_call_id
    cursor_profile_id = after_profile_id
    for _ in range(max_batches):
        summary = _refresh_missing_embeddings_batch(
            session_factory=session_factory,
            embed_client=embed_client,
            batch_size=batch_size,
            force=False,
            after_call_id=cursor_call_id,
            after_profile_id=cursor_profile_id,
        )
        for key in (
            "calls_embedded",
            "calls_failed",
            "profiles_embedded",
            "profiles_failed",
            "profiles_seen",
        ):
            totals[key] += summary[key]
        totals["max_call_id"] = max(totals["max_call_id"], summary["max_call_id"])
        totals["max_profile_id"] = max(totals["max_profile_id"], summary["max_profile_id"])
        # Break on rows EXAMINED, not rows worked. A page in which nothing was
        # stale is progress, not exhaustion: breaking on "processed == 0" here
        # would discard the advanced cursor two lines below, and every later
        # tick would restart at after_profile_id=0 and halt at the same wall.
        # Once the candidate set is widened, that page is the common case.
        #
        # Termination still holds. The call half is SQL-narrowed, so
        # calls_embedded + calls_failed IS the number of call rows examined.
        # The profile half is not, which is why it contributes profiles_seen.
        # Whenever a page returns rows, the batch advances that cursor strictly
        # (to max(profile_ids), or to max(row ids) when nothing was workable),
        # and every returned id is > the cursor it started from. So each
        # non-breaking iteration consumes at least one row of a finite table,
        # and the final page — the one past the last id — returns nothing on
        # both halves, making `examined` 0.
        examined = summary["calls_embedded"] + summary["calls_failed"] + summary["profiles_seen"]
        if examined == 0:
            totals["scan_exhausted"] = True
            break
        cursor_call_id = summary["max_call_id"]
        cursor_profile_id = summary["max_profile_id"]

    _logger.info(
        "embeddings.refresh_missing_tick.drain_summary",
        extra={"event": "embedding_refresh_tick_drain_summary", **totals},
    )
    return totals


def _digest_for_row(row) -> str:
    """embedding_fingerprint() over a column-limited Row rather than a Profile.

    Both build_profile_text and embedding_fingerprint duck-type on
    `.description` and `.structured`, which the Row exposes. This exists so
    the call site reads as a deliberate choice rather than an accident.
    """
    return embedding_fingerprint(row)


def _refresh_missing_embeddings_batch(
    *,
    session_factory,
    embed_client: OllamaEmbedClient,
    batch_size: int = 200,
    force: bool = False,
    after_call_id: int = 0,
    after_profile_id: int = 0,
) -> dict[str, int]:
    """One batch: scan for NULL-embedding rows AND rows whose embedding_model
    differs from the currently-configured model — embed both. Per-row
    try/except so one bad row doesn't kill the batch. Returns a summary
    dict for logging.

    Codex H2 fix: scoping by `embedding IS NULL OR embedding_model != current`
    means a model swap (env change of EMBEDDING_MODEL) is recovered by the
    daily tick within at most 24 h. Operators can run the backfill CLI
    (`python -m grant_lab.embeddings.cli backfill`) for immediate refresh.

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

    Migration 0075: the PROFILE half no longer narrows in SQL. "Current"
    now also means `embedding_fingerprint` describes the row's present text,
    and that fingerprint hashes `build_profile_text`, which is Python over
    JSONB. So every profile past the cursor is examined and the decision is
    made per row. The summary therefore carries `profiles_seen` — rows
    examined, distinct from rows embedded or failed — because the drain loop
    can no longer read "worked nothing" as "nothing left".
    """
    current_model = embed_client.model
    summary = {
        "calls_embedded": 0,
        "calls_failed": 0,
        "profiles_embedded": 0,
        "profiles_failed": 0,
        # Rows EXAMINED, not rows worked. The drain loop breaks on this: with
        # the staleness decision made in Python, a page can legitimately
        # examine rows without working any, and that is progress.
        "profiles_seen": 0,
        "max_call_id": after_call_id,
        "max_profile_id": after_profile_id,
    }

    with session_factory() as session:
        call_q = select(Call.id)
        if not force:
            call_q = call_q.where(
                or_(Call.embedding.is_(None), Call.embedding_model != current_model)
            )
        # No SQL title pre-filter: a call with a blank/whitespace title but an
        # embeddable summary/full_text must still be embedded. build_call_text
        # composes title+summary+full_text and refresh_call_embedding skips
        # (returns False) only when ALL are empty — mirrors the profile path
        # below, keeping the embeddability rule in one place (no SQL duplicate).
        call_q = call_q.where(Call.id > after_call_id).order_by(Call.id).limit(batch_size)
        call_ids = session.execute(call_q).scalars().all()
    # D2-3 perf: reuse one HTTP connection across the entire call-id batch
    # instead of opening/closing a fresh TCP socket per embed call.
    with embed_client.session() as http:
        for cid in call_ids:
            with session_factory() as session:
                ok = refresh_call_embedding(session, embed_client, cid, force=force, _http=http)
                session.commit()
            if ok:
                summary["calls_embedded"] += 1
            else:
                summary["calls_failed"] += 1
    if call_ids:
        summary["max_call_id"] = max(call_ids)

    with session_factory() as session:
        # The staleness decision moved into Python because build_profile_text
        # is Python over JSONB and cannot be evaluated in SQL. (The older
        # reason still holds too: an areas-only profile has a blank
        # description, so any SQL pre-filter on the description text would
        # wrongly skip it.)
        #
        # Do NOT select Profile wholesale: `p.embedding is None` on a loaded
        # Profile pulls the 1024-dim vector, which is the measured dominant
        # cost of a profile load. Select a computed boolean instead.
        profile_q = (
            select(
                Profile.id,
                Profile.description,
                Profile.structured,
                Profile.embedding_model,
                Profile.embedding_fingerprint,
                Profile.embedding.is_(None).label("embedding_is_null"),
            )
            .where(Profile.id > after_profile_id)
            .order_by(Profile.id)
            .limit(batch_size)
        )
        rows = session.execute(profile_q).all()
        summary["profiles_seen"] = len(rows)

        profile_ids = []
        for r in rows:
            text_now = build_profile_text(r)
            if force:
                # Force mode re-embeds every embeddable row, fingerprint or not.
                if text_now.strip():
                    profile_ids.append(r.id)
                continue
            if not text_now.strip():
                # Hole 4: a profile cleared to blank while it still carries
                # embedding state MUST reach refresh_profile_embedding, which
                # is the only thing that wipes the vector. Previously these
                # were filtered out, so the invalidation branch could only
                # ever fire from the lossy request path. An already-cleared
                # blank row is still skipped, so the scan still terminates.
                if (
                    not r.embedding_is_null
                    or r.embedding_model is not None
                    or r.embedding_fingerprint is not None
                ):
                    profile_ids.append(r.id)
                continue
            if (
                r.embedding_is_null
                or r.embedding_model != current_model
                or r.embedding_fingerprint != _digest_for_row(r)
            ):
                profile_ids.append(r.id)

        if rows and not profile_ids:
            # Paged past the end of the work set -- advance the cursor anyway
            # so the next iteration does not re-read the same rows.
            summary["max_profile_id"] = max(r.id for r in rows)
    # D2-3 perf: one HTTP connection for the whole profile batch too.
    with embed_client.session() as http:
        for pid in profile_ids:
            with session_factory() as session:
                ok = refresh_profile_embedding(session, embed_client, pid, force=force, _http=http)
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
