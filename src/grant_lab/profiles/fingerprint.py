"""Fingerprints that say what a stored embedding or ranking was built from.

DERIVED, never stored as a claim about "current". There are four profile-text
write sites (profiles/create.py:25, :51, the constructor at :68-75, and
web/api/profiles.py:534 + :573); a fifth that forgot to maintain a stored hash
would assert "fresh" about a stale row. Recomputing from the row cannot rot.

Both functions return "v2:<sha256 hex>". The version tag is not decoration:
any later change to a hashed payload makes EVERY stored fingerprint mismatch
at once and re-ranks the entire corpus on the next tick, with no operator
signal. Bump the version and ship an explicit decision -- either "yes, re-rank
everything" or a skip rule for the old version.

v2 added "embedding_model" to rank_fingerprint's payload: adding this key is
exactly the deliberate, visible payload change the version tag exists to force.
(No line number here on purpose -- a self-referential citation in this same
file rots on the next edit.)

The decision shipped with v2 is "yes, re-rank everything", and it is free
BEFORE the first production deploy: migration 0075 already leaves
profiles.embedding_fingerprint NULL and so forces one full re-embed and
re-rank on its first night. Note the tag is SHARED with embedding_fingerprint
below, whose payload did not change in v2 -- so this bump invalidates stored
embedding fingerprints too, and that is the same one re-embed, not a second.
Shipping v2 after that first deploy buys an unplanned second corpus re-rank.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from grant_lab.embeddings.similarity import build_profile_text

FINGERPRINT_VERSION = "v2"
LEGACY_SENTINEL = "legacy"


def _canonical(value: Any) -> Any:
    """Normalize a JSONB round-trip so equal content hashes equally.

    Postgres does not preserve key order, and hands numbers back in whatever
    representation it stored: json.dumps(1) is "1" and json.dumps(1.0) is
    "1.0". The profile-update endpoint compares a DB-loaded row against a
    freshly built Python dict (web/api/profiles.py:534 vs :573), so the two
    sides genuinely differ in representation. expire_on_commit=False
    (db/session.py:58) means committing does NOT reload the row, so this
    cannot be fixed by moving a line -- it is fixed here.
    """
    if isinstance(value, bool):
        return value  # bool before int: bool IS an int in Python
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            cv = _canonical(v)
            if cv is None or cv == [] or cv == {} or cv == "":
                continue  # omit-vs-null-vs-empty must not change the hash
            out[str(k)] = cv
        return out
    if isinstance(value, list):
        return [_canonical(v) for v in value]  # list ORDER is meaningful
    return value


def _digest(payload: Any) -> str:
    blob = json.dumps(
        _canonical(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return f"{FINGERPRINT_VERSION}:{hashlib.sha256(blob.encode('utf-8')).hexdigest()}"


def embedding_fingerprint(profile) -> str:
    """What the stored `embedding` describes.

    Hashes build_profile_text's OUTPUT, not its inputs. That function's own
    docstring says changing it changes every profile's vector and that callers
    "must re-embed profiles for the new text to take effect" -- a manual step
    performed by hand once already. Hashing the output makes it automatic.
    """
    return _digest(build_profile_text(profile))


def rank_fingerprint(profile) -> str:
    """What a stored ranking describes.

    The WHOLE `structured` blob, not an enumerated subset. An earlier revision
    enumerated the keys the ranker reads and was already wrong: it omitted
    budget_min/max and trl_min/max, which ranking/criteria.py:111 and :129 read
    straight off `structured`. An enumeration is a list maintained in step with
    a pipeline it does not live next to. It rots, and the symptom is a ranking
    that is stale forever.

    `embedding_fingerprint` is in the payload so that editing build_profile_text
    -- which changes no text field -- still invalidates rankings. Usually that
    costs no extra re-rank round, because the worker defers a pair whose vector
    has not caught up (ranking/worker.py:184-190). It is NOT a guarantee: that
    deferral has two deliberate escapes, and a ranking written through either
    one is stamped with a fingerprint the vector does not yet match, so the
    pair re-ranks once more when the vector settles.

      * A NULL `embedding_fingerprint` proceeds. It means unknown provenance,
        not a mismatch: on first deploy every profile has a vector and a NULL
        fingerprint, and deferring on that would pause all ranking until the
        03:30 tick. (A NULL `embedding` proceeds for the same reason.)
      * A job older than _STALE_VECTOR_MAX_DEFER_HOURS (24) proceeds. The
        deferral is bounded because refresh_profile_embedding with force=False
        preserves the old vector across an embedder outage, so an unbounded
        deferral would strand the pair forever.

    `name` and `kind` are insurance, not cost: they reach the LLM prompt at
    ranking/prompt.py:218-219, but ProfileUpdateIn (web/api/profiles.py:453-480)
    has 22 fields and neither is among them, and nothing assigns either after
    profiles/create.py:68-75.
    """
    return _digest(
        {
            "description": profile.description or "",
            "structured": profile.structured or {},
            "budget_currency": profile.budget_currency,
            "name": profile.name,
            "kind": getattr(profile.kind, "value", profile.kind),
            "embedding_fingerprint": profile.embedding_fingerprint,
            # The MODEL as well as the fingerprint. embedding_fingerprint
            # digests build_profile_text's output, which is text only, so
            # swapping EMBEDDING_MODEL replaces every vector while leaving that
            # hash byte-identical -- and reconcile would then treat every
            # ranking as current while its stored embed_similarity describes
            # the previous vectors. The embedding repair predicate already
            # knows about model identity; this is what carries it into ranking
            # freshness.
            #
            # Every caller must supply this column. ranking/queue.py passes a
            # column-limited Row, not a Profile; direct attribute access here is
            # deliberate so a missing column fails loudly at the boundary
            # instead of silently hashing None. tests/test_ranking_queue_row_parity.py
            # is the gate.
            "embedding_model": profile.embedding_model,
        }
    )
