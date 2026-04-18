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
    while True:
        summary = refresh_missing_embeddings_tick(
            session_factory=session_factory,
            embed_client=embed_client,
            batch_size=args.batch_size,
        )
        for k, v in summary.items():
            total[k] += v
        progress = summary["calls_embedded"] + summary["profiles_embedded"]
        print(
            f"  batch: calls_embedded={summary['calls_embedded']} "
            f"profiles_embedded={summary['profiles_embedded']} "
            f"calls_failed={summary['calls_failed']} "
            f"profiles_failed={summary['profiles_failed']}"
        )
        if progress == 0:
            # No progress in this batch — either everything is embedded,
            # or all remaining rows are failing. Either way, stop looping.
            break

    print(f"backfill done — totals: {total}")
    has_failures = total["calls_failed"] > 0 or total["profiles_failed"] > 0
    return 1 if has_failures else 0


def main() -> None:  # pragma: no cover
    """Entry point for `python -m call_monitor.embeddings.cli`."""
    sys.exit(backfill_main(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main()
