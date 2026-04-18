"""Setwise borderline pass — phase 2 of ranking v2.

Per spec (Zhuang et al., SIGIR 2024), pointwise scoring is least
consistent in the borderline band where ordering matters most. We
group borderline calls into sets of 4 and ask the LLM to rank within
each set. Apply fixed offsets {1: +5, 2: +2, 3: -2, 4: -5} to
`setwise_offset`; final displayed score is `aggregate_score + setwise_offset`
clamped to [0, 100] (handled at render time).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import select, update

from call_monitor.db.models import Call, CallRanking, Profile
from call_monitor.ranking.ollama_client import LLMError, OllamaClient

_logger = logging.getLogger(__name__)

SETWISE_BORDERLINE_LOWER = 45
SETWISE_BORDERLINE_UPPER = 60  # exclusive
SETWISE_GROUP_SIZE = 4
SETWISE_BORDERLINE_CAP = 12
SETWISE_OFFSETS = {1: 5, 2: 2, 3: -2, 4: -5}


def run_setwise_for_profile(
    *,
    session_factory,
    client: OllamaClient,
    profile_id: int,
    now: datetime,
) -> int:
    """Run setwise on the v2 borderline band for one profile.

    Returns the number of groups for which offsets were successfully applied.
    Skips entire pass if the profile has < 4 borderline calls; skips
    individual groups if the LLM's permutation is invalid.

    **Reset semantics:** at the start of every pass, all setwise_offset
    values for this profile's v2 rows are zeroed. Then offsets are
    re-applied for whichever groups the LLM successfully ranked in this
    run. This prevents stale offsets from accumulating on calls that
    dropped out of the borderline band (or were in skipped groups, or
    were dropped as remainder).

    **Stable group membership under ties:** the candidate query orders by
    (aggregate_score DESC, call_id ASC). Without the stable secondary
    key, Postgres reorders tied rows between runs and the same call can
    flip groups daily, making offsets flap.
    """
    # Pass-start reset. One UPDATE scoped to this profile's v2 rows.
    with session_factory() as session:
        profile = session.get(Profile, profile_id)
        if profile is None or profile.ranking_scheme != "v2":
            return 0
        session.execute(
            update(CallRanking)
            .where(
                CallRanking.profile_id == profile_id,
                CallRanking.scheme == "v2",
                CallRanking.setwise_offset != 0,
            )
            .values(setwise_offset=0)
        )
        session.commit()

    # Fetch borderline candidates with stable ordering.
    with session_factory() as session:
        rows = session.execute(
            select(Call.id, Call.title, Call.summary)
            .join(CallRanking, CallRanking.call_id == Call.id)
            .where(
                CallRanking.profile_id == profile_id,
                CallRanking.scheme == "v2",
                CallRanking.aggregate_score >= SETWISE_BORDERLINE_LOWER,
                CallRanking.aggregate_score < SETWISE_BORDERLINE_UPPER,
            )
            # Stable secondary key — prevents Postgres from arbitrarily
            # reordering tied scores between daily runs (Codex M3).
            .order_by(CallRanking.aggregate_score.desc(), Call.id.asc())
            .limit(SETWISE_BORDERLINE_CAP)
        ).all()
        # Re-fetch profile in this session for use in prompt building.
        profile = session.get(Profile, profile_id)
        # Snapshot the fields we need so the Profile object isn't bound
        # to a closed session by the time _ask_for_permutation reads it.
        profile_name = profile.name
        profile_description = profile.description or ""

    if len(rows) < SETWISE_GROUP_SIZE:
        return 0

    # Partition into groups of 4; drop remainder.
    groups: list[list[tuple[int, str, str | None]]] = []
    for i in range(0, len(rows) - len(rows) % SETWISE_GROUP_SIZE, SETWISE_GROUP_SIZE):
        groups.append([(r[0], r[1], r[2]) for r in rows[i : i + SETWISE_GROUP_SIZE]])

    applied = 0
    for group in groups:
        perm = _ask_for_permutation(
            client,
            profile_name=profile_name,
            profile_description=profile_description,
            group=group,
        )
        if perm is None:
            continue
        with session_factory() as session:
            for rank, cid in enumerate(perm, start=1):
                offset = SETWISE_OFFSETS.get(rank, 0)
                rk = session.get(CallRanking, (cid, profile_id, "v2"))
                if rk is not None:
                    rk.setwise_offset = offset
            session.commit()
        applied += 1
    return applied


def _ask_for_permutation(
    client: OllamaClient,
    *,
    profile_name: str,
    profile_description: str,
    group: list[tuple[int, str, str | None]],
) -> list[int] | None:
    """Ask the LLM to rank the calls. Return the permutation as a list of
    call_ids in best→worst order, or None on parse failure / wrong length /
    unknown IDs.

    Wraps the response in `{"order": [...]}` because Ollama's
    `response_format={"type": "json_object"}` enforces an OBJECT shape.
    A bare-array response would either fail at the provider or push the
    model toward an object anyway. Without response_format, Gemma sometimes
    prepends prose explanations.
    """
    ids = [cid for cid, _, _ in group]
    items = "\n".join(
        f"  ID {cid}: {title} — {(summary or '')[:200]}"
        for cid, title, summary in group
    )
    example = json.dumps({"order": ids})
    messages = [
        {
            "role": "system",
            "content": (
                f"You are a research-funding analyst evaluating calls for the profile "
                f"'{profile_name}': {profile_description}\n\n"
                f"Rank the {len(ids)} calls below from MOST to LEAST relevant. "
                f'Output ONLY a JSON object of the form {{"order": [<id>, ...]}} '
                f"with the call IDs in best-to-worst order. Example: {example}. "
                f"No prose, no other keys."
            ),
        },
        # Lead the user message with the bare ID list so the model sees the
        # exact integer set it must permute (and so prompt-aware tests can
        # cheaply parse them out without re-implementing the prompt format).
        {"role": "user", "content": f"CALL IDS: {ids}\nCALLS:\n{items}"},
    ]
    try:
        envelope = client.chat(messages=messages, response_format={"type": "json_object"})
        content = envelope["choices"][0]["message"]["content"]
        data = json.loads(content)
    except (LLMError, ValueError, KeyError, IndexError) as e:
        _logger.warning(
            "ranking.setwise.parse_failed",
            extra={"event": "setwise_parse_failed", "error": str(e)},
        )
        return None
    if not isinstance(data, dict):
        _logger.warning(
            "ranking.setwise.not_an_object",
            extra={"event": "setwise_not_an_object", "got": data},
        )
        return None
    perm = data.get("order")
    if not isinstance(perm, list) or len(perm) != len(ids) or set(perm) != set(ids):
        _logger.warning(
            "ranking.setwise.invalid_permutation",
            extra={
                "event": "setwise_invalid_permutation",
                "expected_ids": ids,
                "got": perm,
            },
        )
        return None
    return [int(x) for x in perm]
