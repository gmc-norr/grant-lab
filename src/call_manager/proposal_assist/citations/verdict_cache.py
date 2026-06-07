"""Content-keyed cache for citation audit semantic verdicts.

A verdict is keyed by the content that determines it — the live claim, the
chunk, the cited quote, the stance, and a validator_version. Any change to those
invalidates the entry (the key simply differs). Bumping VALIDATOR_VERSION
invalidates the whole cache. ``store_verdict`` is best-effort: a write failure
must never fail the audit (a missed cache entry just means a re-judge).
"""

from __future__ import annotations

import hashlib
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from call_manager.db.models import CitationAuditVerdict

logger = logging.getLogger(__name__)

# Bump to invalidate every cached verdict (e.g. when the audit prompt changes).
VALIDATOR_VERSION = "v1"


def build_cache_key(
    *,
    claim_hash: str,
    chunk_content_hash: str,
    evidence_quote_hash: str,
    stance: str,
    validator_version: str,
) -> str:
    """Deterministic cache key. NUL-joined so component boundaries can't collide
    (e.g. "ab"+"c" vs "a"+"bc")."""
    joined = "\x00".join(
        [claim_hash, chunk_content_hash, evidence_quote_hash, stance, validator_version]
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def get_verdict(
    session: Session, *, workspace_id: uuid.UUID, cache_key: str
) -> CitationAuditVerdict | None:
    """Look up a cached verdict on the caller's (read) session. Newest first."""
    return session.scalars(
        select(CitationAuditVerdict)
        .where(
            CitationAuditVerdict.workspace_id == workspace_id,
            CitationAuditVerdict.cache_key == cache_key,
        )
        .order_by(CitationAuditVerdict.created_at.desc(), CitationAuditVerdict.id.desc())
    ).first()


def store_verdict(
    session_factory: sessionmaker,
    *,
    workspace_id: uuid.UUID,
    cache_key: str,
    verdict: str,
    rationale: str | None,
    validator_version: str,
) -> None:
    """Best-effort write on a fresh short-lived session (mirrors
    CitationLLMService._safe_record). Never raises — a failed cache write logs a
    warning and is ignored (the verdict will just be recomputed next time)."""
    try:
        with session_factory() as s:
            s.add(
                CitationAuditVerdict(
                    workspace_id=workspace_id,
                    cache_key=cache_key,
                    verdict=verdict,
                    rationale=rationale,
                    validator_version=validator_version,
                )
            )
            s.commit()
    except Exception:  # noqa: BLE001
        logger.warning(
            "verdict cache write failed (workspace=%s) — will recompute",
            workspace_id,
            exc_info=True,
        )
