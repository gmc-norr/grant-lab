from __future__ import annotations

import logging

from call_manager.embeddings.refresh import refresh_profile_embedding

_logger = logging.getLogger(__name__)


def refresh_embedding_in_background(*, session_factory, embed_client, profile_id: int) -> None:
    """Run refresh_profile_embedding outside the request lifecycle so a slow
    Ollama doesn't stall the user. Swallow all errors — the daily refresh-missing
    tick will retry, and the embedding gate treats a missing vector as UNKNOWN."""
    try:
        with session_factory() as s:
            refresh_profile_embedding(s, embed_client, profile_id)
            s.commit()
    except Exception as e:  # noqa: BLE001
        _logger.warning(
            "profile.embedding_refresh_failed",
            extra={
                "event": "profile_embedding_refresh_failed",
                "profile_id": profile_id,
                "error": str(e),
            },
        )


def refresh_and_backfill_in_background(
    *, session_factory, embed_client, profile_id: int, refresh_embedding: bool
) -> None:
    """Refresh first (if the embed text changed), then enqueue backfill. Order
    matters: the ranking jobs must see the post-edit vector, not the pre-edit one."""
    if refresh_embedding:
        refresh_embedding_in_background(
            session_factory=session_factory, embed_client=embed_client, profile_id=profile_id
        )
    try:
        # Late import to allow test mocking of profiles.backfill_profile
        import call_manager.web.routes.profiles as profiles_mod

        profiles_mod.backfill_profile(session_factory=session_factory, profile_id=profile_id)
    except Exception as e:  # noqa: BLE001
        _logger.warning(
            "profile.backfill_failed",
            extra={"event": "profile_backfill_failed", "profile_id": profile_id, "error": str(e)},
        )
