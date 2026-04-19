"""Embeddings backfill CLI — `python -m call_monitor.embeddings.cli backfill`.

Walks every call + profile with NULL or model-mismatched embedding, calls
Ollama in batches, prints a per-batch summary. Exits 0 on full success,
1 if any row failed to embed.

Use as part of deploy:
    alembic upgrade head
    python -m call_monitor.embeddings.cli backfill
    # start app

Replaces what was originally going to be an Alembic data migration. Alembic
should stay schema-only; depending on Ollama during `alembic upgrade head`
makes deploys fragile. The CLI is run as an explicit, separate deploy step.
"""
from __future__ import annotations

import argparse
import logging
import sys

from call_monitor.config import Settings
from call_monitor.embeddings.client import OllamaEmbedClient
from call_monitor.embeddings.refresh import refresh_missing_embeddings_tick

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
    )


def backfill_main(argv: list[str] | None = None, *, session_factory=None) -> int:
    """Run the backfill until all NULL/mismatched rows are embedded, OR
    until a batch makes no progress (means everything left is failing).

    Returns 0 on full success, 1 if anything failed to embed.
    """
    parser = argparse.ArgumentParser(prog="call_monitor.embeddings.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_backfill = sub.add_parser(
        "backfill", help="Embed all NULL or model-mismatched rows."
    )
    p_backfill.add_argument("--batch-size", type=int, default=200)
    p_backfill.add_argument(
        "--force",
        action="store_true",
        help="Re-embed every row regardless of NULL/mismatched state. Use after "
             "deploys that change the text being embedded (e.g., Plan F's full_text rollout).",
    )
    args = parser.parse_args(argv)

    if args.cmd != "backfill":
        parser.error(f"unknown command: {args.cmd}")

    if session_factory is None:  # pragma: no cover — production path
        from call_monitor.db.session import make_engine, make_session_factory

        settings = Settings()
        engine = make_engine(settings.database_url)
        session_factory = make_session_factory(engine)

    embed_client = _build_embed_client()
    print(
        f"backfill starting — model={embed_client.model} "
        f"batch_size={args.batch_size}"
    )

    total = {
        "calls_embedded": 0,
        "calls_failed": 0,
        "profiles_embedded": 0,
        "profiles_failed": 0,
    }
    # Cursor pagination — required under --force (otherwise the same rows
    # are re-selected every iteration, infinite loop). For non-force mode
    # the cursors stay at 0 and the existing NULL/mismatch filter naturally
    # excludes rows already embedded in this run.
    after_call_id = 0
    after_profile_id = 0
    while True:
        summary = refresh_missing_embeddings_tick(
            session_factory=session_factory,
            embed_client=embed_client,
            batch_size=args.batch_size,
            force=args.force,
            after_call_id=after_call_id if args.force else 0,
            after_profile_id=after_profile_id if args.force else 0,
        )
        for k in ("calls_embedded", "calls_failed",
                  "profiles_embedded", "profiles_failed"):
            total[k] += summary[k]
        progress = summary["calls_embedded"] + summary["profiles_embedded"]
        # Codex Plan F fix: in --force mode the termination signal must be
        # "no rows were processed this batch" (i.e. cursors exhausted both
        # tables) — NOT "no successful embeds". A first batch of all-failed
        # rows previously caused immediate exit before later rows were
        # tried. In force mode the cursor advances regardless of success,
        # so checking processed-count is safe.
        processed = (summary["calls_embedded"] + summary["calls_failed"]
                     + summary["profiles_embedded"] + summary["profiles_failed"])
        print(
            f"  batch: calls_embedded={summary['calls_embedded']} "
            f"profiles_embedded={summary['profiles_embedded']} "
            f"calls_failed={summary['calls_failed']} "
            f"profiles_failed={summary['profiles_failed']}"
        )
        if args.force:
            # Advance the cursors so the next iteration picks up later rows.
            after_call_id = summary["max_call_id"]
            after_profile_id = summary["max_profile_id"]
            if processed == 0:
                # Cursors have walked past the end of both tables — done.
                break
        else:
            # Default mode: there's no cursor, so the same NULL/mismatched
            # rows are re-selected every iteration. "No successful embeds"
            # is the only safe termination — otherwise persistent failures
            # would infinite-loop.
            if progress == 0:
                break

    print(f"backfill done — totals: {total}")
    has_failures = total["calls_failed"] > 0 or total["profiles_failed"] > 0
    return 1 if has_failures else 0


def main() -> None:  # pragma: no cover
    """Entry point for `python -m call_monitor.embeddings.cli`."""
    sys.exit(backfill_main(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main()
