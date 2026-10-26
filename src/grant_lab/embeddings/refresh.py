"""Embedding refresh — write paths for call/profile embeddings.

Used by:
- ingestion orchestrator (per new call)
- profile create/edit handler (sync re-embed on description change)
- daily refresh-missing tick (catches what slipped through)
- one-time backfill CLI
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import select

from grant_lab.db.models import Call, Profile
from grant_lab.embeddings.similarity import build_call_text, build_profile_text
from grant_lab.profiles.fingerprint import call_embedding_fingerprint, embedding_fingerprint
from grant_lab.ranking.ollama_client import LLMError

if TYPE_CHECKING:
    from grant_lab.embeddings.client import OllamaEmbedClient

_logger = logging.getLogger(__name__)


class CallEmbedOutcome(StrEnum):
    """What one call-embedding refresh actually did.

    Three outcomes, not two. EMBEDDED and SKIPPED_CURRENT both mean "this row's
    vector is correct now" and both read as True through the bool wrapper, but
    they are not the same event for an operator: the defect the skip closed was
    FOUND in a log (419 `POST /api/embed` for 0 inserts in one night), and after
    the fix a working skip and a step-2 that silently stopped running look
    identical unless the skips are counted apart. The ingestion orchestrator
    counts SKIPPED_CURRENT into its `ingestion.ok` record for exactly that
    reason.
    """

    EMBEDDED = "embedded"
    SKIPPED_CURRENT = "skipped_current"
    FAILED = "failed"


def refresh_call_embedding(
    session,
    embed_client: OllamaEmbedClient,
    call_id: int,
    *,
    force: bool = False,
    _http: httpx.Client | None = None,
) -> bool:
    """Embed a single call. True on success OR on a skipped current row, False
    on LLM error or missing call. Caller commits the session.

    Thin wrapper over :func:`refresh_call_embedding_with_outcome`, which owns
    the behaviour and the rationale. Callers that must tell an embed apart from
    a skip — the ingestion orchestrator, which counts the skips so the gate's
    effect is visible in a log — call that function directly.
    """
    outcome = refresh_call_embedding_with_outcome(
        session, embed_client, call_id, force=force, _http=_http
    )
    return outcome is not CallEmbedOutcome.FAILED


def refresh_call_embedding_with_outcome(
    session,
    embed_client: OllamaEmbedClient,
    call_id: int,
    *,
    force: bool = False,
    _http: httpx.Client | None = None,
) -> CallEmbedOutcome:
    """Embed a single call, reporting which of the three outcomes happened.
    Caller commits the session.

    Returns SKIPPED_CURRENT, writing nothing, when the row's vector is already
    current — a vector is present, `embedding_model` equals the model this
    client would use, and `embedding_fingerprint` equals the digest of the text
    that would be sent. `force=True` bypasses that. See the guard below for
    which of those conditions are load-bearing; the short version is that this
    used to embed unconditionally, so every ingestion tick re-embedded the
    entire feed (419 embeds for 0 inserts in one night's log).

    `embedding_fingerprint` moves with the vector, always: it is stamped
    alongside `embedding`/`embedding_model` on every successful embed, and
    nulled alongside them on every force-mode failure. A fingerprint that
    outlived its vector would claim "fresh" about a row that has none, and
    the widened staleness scan would then skip that row forever.

    When `force=True` and the embed call raises LLMError, ALL THREE of
    `embedding`, `embedding_model` and `embedding_fingerprint` are nulled
    out. Force mode means "the existing vector is no longer correct" (e.g.,
    the embedded text format changed). Leaving the row with its OLD vector,
    current `embedding_model` AND matching fingerprint would make it
    invisible to the daily refresh tick, whose staleness test is exactly
    those three — silent permanent staleness. (Since migration 0080 the
    fingerprint alone catches a force run prompted by a text-format change;
    nulling still covers every other reason to force.) Nulling lets the
    daily tick retry tomorrow; meanwhile the embedding gate sees None →
    UNKNOWN → falls back to the LLM, which is safer than ranking against a
    known-stale vector.

    ``_http`` — optional persistent :class:`httpx.Client` for batch callers
    (see :meth:`OllamaEmbedClient.session`). When supplied, the same TCP
    connection is reused across repeated calls instead of opening a fresh one
    per call. The caller is responsible for the client's lifetime.
    """
    call = session.get(Call, call_id)
    if call is None:
        return CallEmbedOutcome.FAILED
    text = build_call_text(call)
    if not text.strip():
        # Nothing to embed; leave embedding NULL.
        return CallEmbedOutcome.FAILED
    # ONE digest, used both to decide and to stamp. Computing it twice would be
    # two call sites that must agree about the row at two moments; this is the
    # same property `_digest_for_call_row` argues for in the scan.
    fingerprint = call_embedding_fingerprint(call)
    # A CURRENT row is skipped and nothing is written, not even an equal value
    # (`call_rank_fingerprint` hashes `embedding_model` + `embedding_fingerprint`).
    # MODEL is load-bearing: this digest hashes TEXT ONLY, so an EMBEDDING_MODEL
    # swap invalidates every vector while leaving every hash byte-identical
    # (shipped once, Codex H2). FINGERPRINT is load-bearing: a text edit changes
    # no other column here; dropping it reddens tests of the daily tick, the
    # scheduler's embedding cursors and the ingest orchestrator. The VECTOR
    # condition is REDUNDANT today and kept deliberately: with
    # `ck_calls_embedding_model_paired` making
    # `(embedding IS NULL) = (embedding_model IS NULL)` the model condition
    # implies it and deleting it leaves the whole suite green, but
    # `embedding_fingerprint` is deliberately NOT in that constraint (see
    # db/models/calls.py), so it goes live the day the pairing is relaxed.
    #
    # `force` must bypass the skip. Force means "the stored vector is wrong for
    # a reason no column can show" (the 2026-10-06 Ollama upgrade changed
    # vectors for byte-identical text under an unchanged model name). A skip
    # firing under force would not stop `backfill --force` early — it would make
    # it LIE: cli.py breaks on `embedded + failed == 0`, a skip returns True and
    # is therefore counted as `calls_embedded`, so the run walks its whole
    # cursor, prints `calls_embedded=N` and exits 0 having embedded nothing.
    if (
        not force
        and call.embedding is not None
        and call.embedding_model == embed_client.model
        and call.embedding_fingerprint == fingerprint
    ):
        return CallEmbedOutcome.SKIPPED_CURRENT
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
            call.embedding_fingerprint = None
        return CallEmbedOutcome.FAILED
    call.embedding = result.vector
    call.embedding_model = embed_client.model
    # The digest computed above, over the text that was just embedded.
    call.embedding_fingerprint = fingerprint
    return CallEmbedOutcome.EMBEDDED


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
    in force mode, NULL all three of `embedding`, `embedding_model` and
    `embedding_fingerprint`, so the daily tick's per-row staleness test
    (a NULL vector is stale) picks the row up again. See that helper for
    the full rationale.

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
    """Daily tick entry point. Drains the stale-or-missing backlog by looping
    `_refresh_missing_embeddings_batch` until a batch EXAMINES no rows (the
    scan ran off the end of both tables) or `max_batches` is reached.

    "Examines", not "makes progress": once the candidate scan is widened to
    include fingerprint mismatches, a page can legitimately examine rows and
    work none of them, and breaking on that stops the drain at page one with
    the rest of the table unrepaired.

    The summary carries THREE termination flags, one joint and one per half:

    - `scan_exhausted`: True when the drain ran off the end of BOTH tables,
      False when it stopped on `max_batches`. This is the drain's own
      termination condition. Do NOT key either cursor on it: a tick held
      capped by one half reports False while the other half's scan is long
      finished, which froze the profile cursor until it was fixed.
    - `profile_scan_exhausted`: True when the PROFILE half examined no rows on
      the batch the drain stopped at, i.e. the profile cursor is past the last
      profile id. The daily caller keys its profile cursor on this.
    - `call_scan_exhausted`: the same, for the CALL half. The daily caller
      keys its call cursor on this.

    They are not interchangeable, and reading the joint flag for a per-half
    one is a bug this module has already shipped once. `scan_exhausted` sums
    both halves, so a tick held at `max_batches` by the CALL half reports False
    even when the profile half ran off the end of its own table on the very
    first batch. A caller that reset the profile cursor on `scan_exhausted`
    therefore carried a cursor past the last profile FOREVER while any call
    kept failing to embed, and a profile that went stale behind it was never
    repaired. The call cursor has the mirror hazard: a tick held at the cap by
    the PROFILE half reports False while the call scan finished on its first
    batch. See `_embeddings_tick` in scheduler.py.

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
        "calls_seen": 0,
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
        # The PROFILE half's own exhaustion — see the docstring for why the
        # joint flag above cannot stand in for it. Overwritten by every batch,
        # so what survives the loop is the terminating batch's answer.
        #
        # Set on the non-force drain only, exactly like the flag above: under
        # force this function runs a single batch and returns that batch's
        # summary, which carries NEITHER flag. A force-mode consumer reading
        # this key would KeyError.
        #
        # Benign edge: when the cap lands EXACTLY on the last profile row, the
        # terminating batch examined that row, so this is False and the caller
        # carries a cursor that is already past the last id. That is not "rows
        # remain" — the next tick examines nothing, sets this True, resets, and
        # repairs. One tick is lost and the state self-corrects; False here
        # means "not proven exhausted", not "rows remain".
        "profile_scan_exhausted": False,
        # The CALL half's own exhaustion: the profile flag above, mirrored in
        # every respect — overwritten by every batch, set on the non-force
        # drain only (a force-mode consumer reading it would KeyError), and
        # with the same benign one-tick edge when the cap lands exactly on the
        # last call row. The joint flag cannot stand in for it either: a tick
        # held at the cap by the PROFILE half reports `scan_exhausted` False
        # while the call scan ran off the end of its table on the first batch.
        "call_scan_exhausted": False,
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
            "calls_seen",
            "profiles_embedded",
            "profiles_failed",
            "profiles_seen",
        ):
            totals[key] += summary[key]
        totals["max_call_id"] = max(totals["max_call_id"], summary["max_call_id"])
        totals["max_profile_id"] = max(totals["max_profile_id"], summary["max_profile_id"])
        # Break on rows EXAMINED, not rows worked. A page in which nothing was
        # stale is progress, not exhaustion: breaking on "processed == 0" here
        # would stop the drain at the first page with nothing to do, discard
        # the cursors advanced two lines below, and leave every stale row
        # behind that page unrepaired. With both halves widened, that page is
        # the common case. Pinned per half by
        # test_a_stale_profile_behind_a_full_fresh_page_is_still_reached and
        # test_a_stale_call_behind_a_full_fresh_page_is_still_reached.
        #
        # Termination still holds, by the same argument on both halves.
        # Neither half narrows in SQL any more (profiles since migration 0075,
        # calls since 0080): each returns every row past its cursor and decides
        # staleness per row in Python. So a half's worked count (embedded +
        # failed) is NOT the number of rows it examined -- a page of fresh rows
        # works none of them -- which is why `examined` sums calls_seen and
        # profiles_seen, the rows each half's page actually returned. Whenever
        # a half's page returns rows, the batch advances that half's cursor
        # strictly (calls: to the largest id on the page; profiles: to
        # max(profile_ids), or to max(row ids) when nothing was workable), and
        # every returned id is > the cursor it started from. So each
        # non-breaking iteration consumes at least one row of a finite table,
        # and the final page — the one past the last id — returns nothing on
        # both halves, making `examined` 0.
        #
        # `examined` is JOINT. Whether EACH half is done is a separate
        # question, answered by that half alone, and the daily caller needs
        # the per-half answers to decide about each cursor.
        totals["profile_scan_exhausted"] = summary["profiles_seen"] == 0
        totals["call_scan_exhausted"] = summary["calls_seen"] == 0
        examined = summary["calls_seen"] + summary["profiles_seen"]
        if examined == 0:
            # examined == 0 implies calls_seen == 0 and profiles_seen == 0, so
            # both per-half flags set just above are already True on this path.
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


def _digest_for_call_row(row) -> str:
    """call_embedding_fingerprint() over a column-limited Row rather than a Call.

    The call-side twin of `_digest_for_row`, and like it a thin wrapper, never
    a second implementation: the digest this scan compares must be the one
    refresh_call_embedding stamps. Both build_call_text and
    call_embedding_fingerprint duck-type on `.title`, `.summary` and
    `.full_text`, which the Row exposes.
    """
    return call_embedding_fingerprint(row)


def _refresh_missing_embeddings_batch(
    *,
    session_factory,
    embed_client: OllamaEmbedClient,
    batch_size: int = 200,
    force: bool = False,
    after_call_id: int = 0,
    after_profile_id: int = 0,
) -> dict[str, int]:
    """One batch: examine the rows past each cursor and embed the stale ones —
    a NULL embedding, an embedding_model that differs from the currently
    configured model, or an embedding_fingerprint that no longer describes
    the row's text. Per-row try/except so one bad row doesn't kill the
    batch. Returns a summary dict for logging.

    Neither half filters in SQL: each reads every row past its cursor and
    applies the staleness test above per row, in Python. A model swap (env
    change of EMBEDDING_MODEL, Codex H2) therefore makes every row stale and
    the daily tick recovers it, up to `batch_size * _TICK_MAX_BATCHES` rows a
    tick and the rest on later ticks. Operators can run the backfill CLI
    (`python -m grant_lab.embeddings.cli backfill`) for immediate refresh.

    `force=True` (Plan F Task 7) skips the staleness test: every call past the
    cursor goes to refresh_call_embedding (a call with no embeddable text
    counts as failed and is left unwritten), and every profile with
    embeddable text is re-embedded (a blank profile is skipped). Used after
    deploys that change the text being embedded (e.g., Plan F's full_text
    rollout).

    `after_call_id` / `after_profile_id` are id-cursors: the drain loop
    advances them batch to batch, the daily scheduler carries them across
    capped ticks, and the backfill CLI walks them in both modes (without
    them, the same rows would be re-selected every iteration). The summary
    includes `max_call_id` / `max_profile_id` so the caller can advance the
    cursor. Default 0 = start from the beginning.

    Migration 0075: the PROFILE half no longer narrows in SQL. "Current"
    now also means `embedding_fingerprint` describes the row's present text,
    and that fingerprint hashes `build_profile_text`, which is Python over
    JSONB. So every profile past the cursor is examined and the decision is
    made per row. The summary therefore carries `profiles_seen` — rows
    examined, distinct from rows embedded or failed — because the drain loop
    can no longer read "worked nothing" as "nothing left".

    Migration 0080 did the same to the CALL half, for the same reason: a
    call's `embedding_fingerprint` hashes `build_call_text`, which is Python.
    The summary carries `calls_seen` alongside `profiles_seen`, and the drain
    loop sums the two.
    """
    current_model = embed_client.model
    summary = {
        "calls_embedded": 0,
        "calls_failed": 0,
        "profiles_embedded": 0,
        "profiles_failed": 0,
        # Rows EXAMINED, not rows worked, one count per half. The drain loop
        # breaks on their sum: with the staleness decision made in Python, a
        # page can legitimately examine rows without working any, and that is
        # progress.
        "calls_seen": 0,
        "profiles_seen": 0,
        "max_call_id": after_call_id,
        "max_profile_id": after_profile_id,
    }

    with session_factory() as session:
        # Migration 0080: the staleness decision moved into Python, as it did
        # for profiles in 0075, because a call's fingerprint hashes
        # build_call_text, which is Python and cannot be evaluated in SQL. So
        # every call past the cursor is examined and decided per row below.
        #
        # Do NOT select Call wholesale or Call.embedding: that parses the
        # 1024-dim vector of every row examined just to decide whether to
        # replace it. Carry `embedding IS NULL` as a computed boolean instead,
        # as the profile half does.
        #
        # `full_text` is loaded WHOLE, with no SQL-side left(). build_call_text
        # already cuts it at CALL_TEXT_FULL_TEXT_LIMIT, and that cut must live
        # in exactly one place: a second, SQL-side cut is a second copy of the
        # limit, and the day the two disagree the fingerprint computed here
        # stops matching the one refresh_call_embedding stamps from the whole
        # row, so every call longer than the SQL cut re-embeds on every tick
        # and every backfill CLI pass, forever — the row never becomes
        # "current" no matter how many times it's re-embedded. (The CLI's
        # own non-force termination is cursor-exhaustion, not "found nothing
        # stale", so a run still completes one pass; it just repeats this
        # cost every time.) The price is reading every call's
        # whole full_text once per tick; at the size measured on the app
        # database on 2026-09-24 (686 calls, 81 of them over the 8000-char
        # limit, the longest 35,859 chars) that is a few MB.
        #
        # No SQL title pre-filter either: a call with a blank/whitespace title
        # but an embeddable summary/full_text must still be embedded.
        # refresh_call_embedding skips (returns False) only when ALL parts are
        # empty, keeping the embeddability rule in one place.
        call_q = (
            select(
                Call.id,
                Call.title,
                Call.summary,
                Call.full_text,
                Call.embedding_fingerprint,
                Call.embedding_model,
                Call.embedding.is_(None).label("embedding_is_null"),
            )
            .where(Call.id > after_call_id)
            .order_by(Call.id)
            .limit(batch_size)
        )
        call_rows = session.execute(call_q).all()
    summary["calls_seen"] = len(call_rows)

    call_ids = []
    for r in call_rows:
        if force:
            # Force sends every row past the cursor to refresh_call_embedding,
            # embeddable or not -- unchanged by 0080, since force never
            # narrowed: a blank call counts as failed there. The backfill
            # CLI's force loop stops on a batch that processed (embedded +
            # failed) nothing, so dropping blank rows here could end it early.
            call_ids.append(r.id)
            continue
        if (
            r.embedding_is_null
            or r.embedding_model != current_model
            or r.embedding_fingerprint != _digest_for_call_row(r)
        ):
            call_ids.append(r.id)
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
    if call_rows:
        # Past every row EXAMINED, not only the ones worked: each was either
        # worked above or judged current, so nothing on this page needs
        # re-reading this tick. (A row that failed stays stale behind the
        # cursor; a later tick retries it.) Under force every row is worked,
        # so this equals max(call_ids), the CLI's cursor contract.
        summary["max_call_id"] = max(r.id for r in call_rows)

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
