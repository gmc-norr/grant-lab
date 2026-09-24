"""Embeddings backfill CLI — `python -m grant_lab.embeddings.cli backfill`.

Walks every call + profile whose embedding is stale — NULL, model-mismatched,
or fingerprint-mismatched (the row's text changed under a vector computed
from the old text) — calls Ollama in batches, prints a per-batch summary.
Exits 0 on full success, 1 if any row failed to embed.

Use as part of deploy:
    alembic upgrade head
    python -m grant_lab.embeddings.cli backfill
    # start app

Replaces what was originally going to be an Alembic data migration. Alembic
should stay schema-only; depending on Ollama during `alembic upgrade head`
makes deploys fragile. The CLI is run as an explicit, separate deploy step.
"""

from __future__ import annotations

import argparse
import logging
import sys

from grant_lab.config import Settings
from grant_lab.embeddings.client import OllamaEmbedClient
from grant_lab.embeddings.refresh import refresh_missing_embeddings_tick

_logger = logging.getLogger(__name__)


def _build_embed_client() -> OllamaEmbedClient:
    """Construct an OllamaEmbedClient from environment-loaded Settings.

    Separate function so tests can monkeypatch this without needing real
    OLLAMA_URL / EMBEDDING_MODEL env vars set.
    """
    settings = Settings()
    return OllamaEmbedClient(
        base_url=settings.ollama_url,
        model=settings.embedding_model,
        expected_dim=settings.embedding_dim,
    )


def backfill_main(argv: list[str] | None = None, *, session_factory=None) -> int:
    """Run the backfill to completion.

    Non-force mode walks both id-cursors forward exactly as force mode does
    and stops when the tick reports `scan_exhausted` — both the call and
    profile scans ran past their last row from the cursors given. It does
    NOT stop on "no successful embeds": since the call/profile scans widened
    (migrations 0075/0080) to examine every row past the cursor rather than
    a SQL-narrowed stale set, a tick can legitimately examine a full page of
    already-current rows and embed nothing while rows further on are still
    stale.

    Force mode is unchanged: it stops when a tick processes (embeds + fails)
    nothing, i.e. both cursors have walked past both tables.

    Returns 0 on full success, 1 if anything failed to embed.
    """
    parser = argparse.ArgumentParser(prog="grant_lab.embeddings.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_backfill = sub.add_parser(
        "backfill", help="Embed all NULL, model-mismatched, or fingerprint-mismatched rows."
    )
    p_backfill.add_argument("--batch-size", type=int, default=200)
    p_backfill.add_argument(
        "--force",
        action="store_true",
        help="Re-embed every row regardless of staleness (NULL vector, model "
        "mismatch, or fingerprint mismatch). Use after deploys that change the "
        "text being embedded (e.g., Plan F's full_text rollout).",
    )
    args = parser.parse_args(argv)

    if args.cmd != "backfill":
        parser.error(f"unknown command: {args.cmd}")

    if session_factory is None:  # pragma: no cover — production path
        from grant_lab.db.session import make_engine, make_session_factory

        settings = Settings()
        engine = make_engine(settings.database_url)
        session_factory = make_session_factory(engine)

    embed_client = _build_embed_client()
    print(f"backfill starting — model={embed_client.model} batch_size={args.batch_size}")

    total = {
        "calls_embedded": 0,
        "calls_failed": 0,
        "profiles_embedded": 0,
        "profiles_failed": 0,
    }
    # Cursor pagination — both modes now walk id-cursors forward across
    # calls to refresh_missing_embeddings_tick, never resetting to 0 mid-run
    # (a CLI run is one pass; the scheduler, not the CLI, owns revisiting on
    # a later day). Required under --force so the same rows aren't
    # re-selected every iteration (infinite loop). Also required in
    # non-force mode since migrations 0075/0080: the call/profile scans no
    # longer narrow to stale rows in SQL, so re-reading from 0 every
    # iteration would just re-examine the same already-current prefix and
    # never reach a stale row beyond one tick's `batch_size * max_batches`
    # cap (Task 5b regression).
    after_call_id = 0
    after_profile_id = 0
    while True:
        summary = refresh_missing_embeddings_tick(
            session_factory=session_factory,
            embed_client=embed_client,
            batch_size=args.batch_size,
            force=args.force,
            after_call_id=after_call_id,
            after_profile_id=after_profile_id,
        )
        for k in ("calls_embedded", "calls_failed", "profiles_embedded", "profiles_failed"):
            total[k] += summary[k]
        # Codex Plan F fix: in --force mode the termination signal must be
        # "no rows were processed this batch" (i.e. cursors exhausted both
        # tables) — NOT "no successful embeds". A first batch of all-failed
        # rows previously caused immediate exit before later rows were
        # tried. In force mode the cursor advances regardless of success,
        # so checking processed-count is safe.
        processed = (
            summary["calls_embedded"]
            + summary["calls_failed"]
            + summary["profiles_embedded"]
            + summary["profiles_failed"]
        )
        print(
            f"  batch: calls_embedded={summary['calls_embedded']} "
            f"profiles_embedded={summary['profiles_embedded']} "
            f"calls_failed={summary['calls_failed']} "
            f"profiles_failed={summary['profiles_failed']}"
        )
        # Advance the cursors so the next iteration picks up later rows —
        # both modes. A batch's max_call_id/max_profile_id default to the
        # cursor passed in and only move when that half's page returned
        # rows, so an exhausted half holds its end-of-table position
        # instead of wrapping back to 0.
        after_call_id = summary["max_call_id"]
        after_profile_id = summary["max_profile_id"]
        if args.force:
            if processed == 0:
                # Cursors have walked past the end of both tables — done.
                break
        else:
            # Non-force: neither scan narrows to stale rows in SQL any
            # more, so a tick can legitimately examine a full page and
            # embed nothing while stale rows remain further on ("no
            # successful embeds" broke here and stopped early — the Task 5b
            # regression). `scan_exhausted` is the tick's own answer to "did
            # the drain run off the end of BOTH tables from the cursors
            # given", which is exactly what one CLI pass needs to know
            # before stopping.
            if summary["scan_exhausted"]:
                break

    print(f"backfill done — totals: {total}")
    has_failures = total["calls_failed"] > 0 or total["profiles_failed"] > 0
    return 1 if has_failures else 0


def main() -> None:  # pragma: no cover
    """Entry point for `python -m grant_lab.embeddings.cli`."""
    sys.exit(backfill_main(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main()
