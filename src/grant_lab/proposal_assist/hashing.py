"""sha256 hashing for extractor freshness checks.

Both the section extractor and the rubric extractor record the hash of the
call.full_text they ran against. When the call body changes (re-ingest),
the hash changes, and Phase 0 re-extracts.
"""

from __future__ import annotations

import hashlib


def source_text_hash(text: str) -> str:
    """Stable sha256 hex digest of a UTF-8 string. Used by both extractors to
    decide whether their cached output is fresh against the current call body.

    Non-str input raises TypeError eagerly so callers don't silently store the
    wrong hash for bytes / None / etc.
    """
    if not isinstance(text, str):
        raise TypeError(f"source_text_hash expects str, got {type(text).__name__}")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
