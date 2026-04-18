import json
import re
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from call_monitor.db.models import (
    Call,
    CallRanking,
    Profile,
    ProfileKind,
    Source,
    User,
)
from call_monitor.ranking.ollama_client import OllamaClient
from call_monitor.ranking.setwise import (
    SETWISE_OFFSETS,
    run_setwise_for_profile,
)


def _seed_profile_with_borderline_calls(s, *, scores: list[int]) -> tuple[int, list[int]]:
    u = User(email=f"sw-{len(scores)}@ki.se")
    s.add(u)
    s.flush()
    p = Profile(
        name=f"SW-{len(scores)}", kind=ProfileKind.GROUP, description="Description",
        structured={"areas": [], "hard_exclusions": []},
        created_by=u.id, ranking_scheme="v2",
    )
    s.add(p)
    s.flush()
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    cids: list[int] = []
    for i, score in enumerate(scores):
        c = Call(
            source_id=src.id, external_id=f"sw-{i}", url=f"https://x/{i}",
            title=f"Call {i}", summary="x",
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        s.add(CallRanking(
            call_id=c.id, profile_id=p.id, scheme="v2",
            aggregate_score=score, rationale="r",
            model="m", prompt_version="v2.0", ranked_at=datetime.now(UTC),
        ))
        cids.append(c.id)
    return p.id, cids


def _setwise_client_returning(perm: list[int]):
    """Fake Ollama returning a permutation wrapped as {"order": [...]} —
    matches the actual contract enforced by response_format=json_object."""
    body = {
        "model": "gemma4:e4b-it-q4_K_M",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": json.dumps({"order": perm})},
        }],
    }
    def handler(req):
        return httpx.Response(200, json=body)
    return OllamaClient(
        base_url="http://x", model="gemma4:e4b-it-q4_K_M",
        transport=httpx.MockTransport(handler),
    )


def _setwise_client_returning_bare_array(perm: list[int]):
    """Adversarial: model returns a bare array instead of {"order": [...]}.
    Should be rejected (parser only accepts object form)."""
    body = {
        "model": "gemma4:e4b-it-q4_K_M",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": json.dumps(perm)},
        }],
    }
    def handler(req):
        return httpx.Response(200, json=body)
    return OllamaClient(
        base_url="http://x", model="gemma4:e4b-it-q4_K_M",
        transport=httpx.MockTransport(handler),
    )


def test_setwise_skipped_when_fewer_than_4_borderline(pg_session_factory):
    """With < 4 borderline calls, no setwise pass runs; offsets stay 0."""
    with pg_session_factory() as s:
        pid, cids = _seed_profile_with_borderline_calls(s, scores=[50, 55, 58])
        s.commit()

    n_groups = run_setwise_for_profile(
        session_factory=pg_session_factory,
        client=_setwise_client_returning(cids),  # never called
        profile_id=pid, now=datetime.now(UTC),
    )
    assert n_groups == 0
    with pg_session_factory() as s:
        offsets = [r.setwise_offset for r in s.execute(select(CallRanking)).scalars().all()]
    assert offsets == [0, 0, 0]


def test_setwise_applies_offsets_to_one_group_of_four(pg_session_factory):
    with pg_session_factory() as s:
        pid, cids = _seed_profile_with_borderline_calls(s, scores=[58, 55, 50, 47])
        s.commit()

    # LLM ranks: cids[0] best, cids[2] second, cids[3] third, cids[1] worst.
    perm = [cids[0], cids[2], cids[3], cids[1]]
    n_groups = run_setwise_for_profile(
        session_factory=pg_session_factory,
        client=_setwise_client_returning(perm),
        profile_id=pid, now=datetime.now(UTC),
    )
    assert n_groups == 1

    expected = {
        cids[0]: SETWISE_OFFSETS[1],   # +5
        cids[2]: SETWISE_OFFSETS[2],   # +2
        cids[3]: SETWISE_OFFSETS[3],   # -2
        cids[1]: SETWISE_OFFSETS[4],   # -5
    }
    with pg_session_factory() as s:
        rows = s.execute(select(CallRanking)).scalars().all()
        actual = {r.call_id: r.setwise_offset for r in rows}
    assert actual == expected


def test_setwise_drops_remainder_group_smaller_than_4(pg_session_factory):
    """5 borderline calls → 1 group of 4 + 1 remainder of 1; remainder is dropped."""
    with pg_session_factory() as s:
        pid, cids = _seed_profile_with_borderline_calls(s, scores=[58, 56, 54, 52, 50])
        s.commit()
    perm = [cids[0], cids[1], cids[2], cids[3]]  # only the first 4
    run_setwise_for_profile(
        session_factory=pg_session_factory,
        client=_setwise_client_returning(perm),
        profile_id=pid, now=datetime.now(UTC),
    )
    with pg_session_factory() as s:
        rows = {r.call_id: r.setwise_offset for r in s.execute(select(CallRanking)).scalars().all()}
    # Remainder (cids[4]) is untouched.
    assert rows[cids[4]] == 0
    # First group has the offsets.
    assert rows[cids[0]] == SETWISE_OFFSETS[1]


def test_setwise_caps_borderline_at_12(pg_session_factory):
    """Spec line 187: cap at 12. So max 3 groups."""
    with pg_session_factory() as s:
        pid, cids = _seed_profile_with_borderline_calls(s, scores=list(range(46, 60)))
        s.commit()
    # 14 borderline calls; capped to 12; 3 groups of 4.
    # We need a realistic handler that returns a valid permutation of the
    # 4 IDs the worker actually shows. Easiest: parse the IDs out of the
    # prompt body and echo them back as the permutation.
    def handler(req):
        # Echo back the first 4 IDs in JSON; even if they're "wrong",
        # the handler should at least apply offsets to whatever it returns.
        # The prompt body contains the IDs — extract them.
        data = json.loads(req.read().decode("utf-8"))
        # The user message contains the IDs as a list.
        user_text = data["messages"][-1]["content"]
        ids_in_prompt = [int(x) for x in re.findall(r"\b(\d+)\b", user_text)][:4]
        return httpx.Response(200, json={
            "model": "m",
            "choices": [{"message": {"role": "assistant",
                                     "content": json.dumps({"order": ids_in_prompt})}}],
        })
    client = OllamaClient(
        base_url="http://x", model="m",
        transport=httpx.MockTransport(handler),
    )

    n = run_setwise_for_profile(
        session_factory=pg_session_factory,
        client=client, profile_id=pid, now=datetime.now(UTC),
    )
    assert n == 3  # 12 calls / 4 per group


def test_setwise_skips_group_on_invalid_permutation(pg_session_factory):
    """If LLM returns a non-permutation (wrong length, unknown IDs, parse fail),
    skip the group with a log and move on."""
    with pg_session_factory() as s:
        pid, cids = _seed_profile_with_borderline_calls(s, scores=[58, 56, 54, 52])
        s.commit()
    # Return a permutation of length 3 — invalid.
    bad_client = _setwise_client_returning([cids[0], cids[1], cids[2]])
    n = run_setwise_for_profile(
        session_factory=pg_session_factory,
        client=bad_client, profile_id=pid, now=datetime.now(UTC),
    )
    # Group was attempted but skipped → return value reflects attempted, not applied.
    # Implementation choice: count == 0 (no offsets applied).
    assert n == 0
    with pg_session_factory() as s:
        rows = s.execute(select(CallRanking)).scalars().all()
        offsets = {r.call_id: r.setwise_offset for r in rows}
    assert all(o == 0 for o in offsets.values())


def test_setwise_rejects_bare_array_response(pg_session_factory):
    """Codex H1 regression: model returning a bare JSON array instead of
    {"order": [...]} is rejected. Without the rejection, all groups would
    silently apply offsets from the wrong shape."""
    with pg_session_factory() as s:
        pid, cids = _seed_profile_with_borderline_calls(s, scores=[58, 55, 50, 47])
        s.commit()
    n = run_setwise_for_profile(
        session_factory=pg_session_factory,
        client=_setwise_client_returning_bare_array(cids),
        profile_id=pid, now=datetime.now(UTC),
    )
    assert n == 0
    with pg_session_factory() as s:
        offsets = [r.setwise_offset for r in s.execute(select(CallRanking)).scalars().all()]
    assert all(o == 0 for o in offsets)


def test_setwise_resets_stale_offsets_at_pass_start(pg_session_factory):
    """Codex H2 regression: a call with a stale setwise_offset (left over
    from a previous pass) gets cleared at the start of the next pass —
    even if it's no longer in the borderline band, even if its current
    group is skipped, even if there's no new group at all."""
    with pg_session_factory() as s:
        pid, cids = _seed_profile_with_borderline_calls(s, scores=[58, 55, 50, 47])
        # Manually set stale offsets on all four calls (as if a previous
        # pass had ranked them). Also push one of them out of the borderline
        # band by raising its score to 70.
        for i, cid in enumerate(cids):
            rk = s.get(CallRanking, (cid, pid, "v2"))
            rk.setwise_offset = SETWISE_OFFSETS[i + 1]
        # Bump cids[0] above the borderline upper bound — it shouldn't
        # participate in this pass and its stale offset must clear.
        s.get(CallRanking, (cids[0], pid, "v2")).aggregate_score = 70
        s.commit()

    # Only 3 borderline calls remain → no group of 4 → setwise won't run any group.
    # But the reset should still fire and clear ALL stale offsets for the profile.
    def handler(req):
        raise AssertionError("LLM should not be called — < 4 borderline calls")
    client = OllamaClient(
        base_url="http://x", model="m",
        transport=httpx.MockTransport(handler),
    )
    n = run_setwise_for_profile(
        session_factory=pg_session_factory,
        client=client, profile_id=pid, now=datetime.now(UTC),
    )
    assert n == 0
    with pg_session_factory() as s:
        rows = s.execute(select(CallRanking)).scalars().all()
        offsets = {r.call_id: r.setwise_offset for r in rows}
    # Every offset is back to 0, including the call that left the band.
    assert all(o == 0 for o in offsets.values()), offsets


def test_setwise_returns_zero_for_missing_profile(pg_session_factory):
    """Early-bail: profile_id doesn't exist → return 0, never call the LLM."""
    def handler(req):
        raise AssertionError("LLM should not be called — profile does not exist")
    client = OllamaClient(
        base_url="http://x", model="m",
        transport=httpx.MockTransport(handler),
    )
    n = run_setwise_for_profile(
        session_factory=pg_session_factory,
        client=client,
        profile_id=999999,
        now=datetime.now(UTC),
    )
    assert n == 0


def test_setwise_returns_zero_for_v1_profile(pg_session_factory):
    """Early-bail: profile.ranking_scheme == 'v1' → return 0, never call the
    LLM. The v2 borderline rows we seed below must remain untouched, proving
    the early-bail fires BEFORE the candidate fetch."""
    with pg_session_factory() as s:
        # Seed a v1 profile.
        u = User(email="v1@ki.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="V1-profile", kind=ProfileKind.GROUP, description="d",
            structured={"areas": [], "hard_exclusions": []},
            created_by=u.id, ranking_scheme="v1",
        )
        s.add(p)
        s.flush()
        # Seed a borderline-band v2 CallRanking row attached to that v1 profile.
        # If the early-bail were missing, the candidate fetch would still find
        # this v2 row by profile_id+scheme; the early-bail must fire first.
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id, external_id="v1-borderline", url="https://x/v1",
            title="V1 call", summary="x",
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        s.add(CallRanking(
            call_id=c.id, profile_id=p.id, scheme="v2",
            aggregate_score=55, rationale="r",
            model="m", prompt_version="v2.0", ranked_at=datetime.now(UTC),
        ))
        s.commit()
        pid = p.id

    def handler(req):
        raise AssertionError("LLM should not be called — profile is v1")
    client = OllamaClient(
        base_url="http://x", model="m",
        transport=httpx.MockTransport(handler),
    )
    n = run_setwise_for_profile(
        session_factory=pg_session_factory,
        client=client,
        profile_id=pid,
        now=datetime.now(UTC),
    )
    assert n == 0
