"""Ingestion CLI — `python -m grant_lab.ingestion.cli run --source <slug>`.

Used by the deploy procedure (spec 2026-04-19 §9) to force an ingest tick
without waiting for the next APScheduler cron. Operators run this after
resetting `Source.last_fetched_at` to repopulate `Call.full_text` on
historical rows.

    # Trigger one-shot ingest of a single source:
    python -m grant_lab.ingestion.cli run --source vinnova

    # List available sources:
    python -m grant_lab.ingestion.cli list-sources

The exit code is load-bearing: deploy scripts chain on `&&`, so any silent
zero on a real ingest failure (Codex H2) would proceed past a broken step.
We translate `IngestionResult.error` into `exit 1`, with the message routed
to stderr.
"""

from __future__ import annotations

import argparse
import logging
import sys

from grant_lab.auth.email import ConsoleEmailSender
from grant_lab.config import Settings
from grant_lab.embeddings.client import OllamaEmbedClient
from grant_lab.ingestion.alerts import ConsecutiveFailureTracker
from grant_lab.ingestion.orchestrator import IngestionResult, run_ingestion
from grant_lab.sources.registry import build_adapter, known_slugs

_logger = logging.getLogger(__name__)


def _build_embed_client(settings: Settings) -> OllamaEmbedClient:
    """Construct an OllamaEmbedClient from Settings.

    Separate function so tests can monkeypatch this without needing real
    OLLAMA_URL / EMBEDDING_MODEL env vars set.
    """
    return OllamaEmbedClient(
        base_url=settings.ollama_url,
        model=settings.embedding_model,
        expected_dim=settings.embedding_dim,
    )


def _run_for_source(
    *,
    slug: str,
    session_factory,
    settings: Settings,
) -> IngestionResult:
    """Run one ingest tick for the named source.

    Returns the orchestrator's `IngestionResult` so the caller can inspect
    `result.error` and propagate failure to the shell exit code.

    Raises `ValueError` on unknown slug — this is a usage error (operator
    typo, not an operational failure), so it gets a different exit path
    than an `IngestionResult` with `.error` set.
    """
    adapter = build_adapter(slug, settings)
    if adapter is None:
        raise ValueError(f"unknown source: {slug}")
    embed_client = _build_embed_client(settings)
    # CLI runs are one-shot and out-of-band from the scheduler. We construct
    # a fresh alerter that nobody else holds a reference to, so its in-memory
    # streak counter is per-invocation. This is intentional: the CLI is run
    # by an operator who already sees the failure (stderr + exit 1); we don't
    # also want to spam the admin email on every manual retry.
    alerter = ConsecutiveFailureTracker(
        threshold=3,
        sender=ConsoleEmailSender(),
        admin_email=None,
    )
    return run_ingestion(
        adapter=adapter,
        session_factory=session_factory,
        embed_client=embed_client,
        alerter=alerter,
    )


def ingest_main(argv: list[str] | None = None, *, session_factory=None) -> int:
    """Entry point. Returns:
    - 0 on full success (orchestrator's IngestionResult.error is None)
    - 1 on usage error, unknown source, or orchestrator-reported failure

    The exit code is load-bearing: deploy scripts (per spec §9) chain
    `python -m grant_lab.ingestion.cli run --source X && next-step`.
    A silent zero on a real ingest failure would break that chain.
    """
    parser = argparse.ArgumentParser(prog="grant_lab.ingestion.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Force a one-shot ingest tick for a source.")
    p_run.add_argument("--source", required=True, help="Source slug (e.g., vinnova).")

    sub.add_parser("list-sources", help="List registered source slugs.")

    args = parser.parse_args(argv)

    if args.cmd == "list-sources":
        for slug in known_slugs():
            print(slug)
        return 0

    if args.cmd == "run":
        # Settings is required regardless of injection: even when the test
        # supplies a session_factory, registry.build_adapter needs the source
        # base_urls and _build_embed_client needs ollama config.
        settings = Settings()
        if session_factory is None:  # pragma: no cover — production path
            from grant_lab.db.session import (
                make_engine,
                make_session_factory,
            )

            engine = make_engine(settings.database_url)
            session_factory = make_session_factory(engine)

        try:
            result = _run_for_source(
                slug=args.source,
                session_factory=session_factory,
                settings=settings,
            )
        except ValueError as e:
            # Unknown slug → usage error. Print the available slugs so the
            # operator doesn't have to consult `list-sources` separately.
            print(f"error: {e}", file=sys.stderr)
            print(
                f"available sources: {', '.join(known_slugs())}",
                file=sys.stderr,
            )
            return 1

        # Codex H2: orchestrator reports operational failures via
        # IngestionResult.error (adapter HTTP error, DB error during enqueue,
        # unexpected exception). Those are ALSO failures from the deploy
        # script's perspective — exit 1 with the message on stderr.
        if result is not None and getattr(result, "error", None):
            print(f"ingest failed: {result.error}", file=sys.stderr)
            return 1

        # Surface counts so the operator sees what was done.
        if result is not None:
            inserted = getattr(result, "inserted", None)
            updated = getattr(result, "updated", None)
            if inserted is not None and updated is not None:
                print(f"ingest ok: {inserted} new, {updated} updated")

        return 0

    parser.error(f"unknown command: {args.cmd}")
    return 2  # unreachable — parser.error exits — but keeps mypy happy


def main() -> None:  # pragma: no cover
    """Entry point for `python -m grant_lab.ingestion.cli`."""
    sys.exit(ingest_main(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main()
