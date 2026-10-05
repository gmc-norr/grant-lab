import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial

import httpx

from grant_lab.ranking.ollama_client import OllamaClient
from grant_lab.ranking.prompt import rank_response_format
from grant_lab.ranking.rank_pair import RankResult
from grant_lab.ranking.rank_pair import rank_pair as _rank_pair

_NOW = datetime(2026, 4, 17, 12, 0, tzinfo=UTC)
# Mirrors Settings.embedding_pregate_threshold default. Most tests don't tune
# the threshold; they just need *some* value because rank_pair now requires it.
_DEFAULT_EMBEDDING_THRESHOLD = 0.30
rank_pair = partial(_rank_pair, embedding_threshold=_DEFAULT_EMBEDDING_THRESHOLD)


@dataclass
class _Profile:
    name: str = "P"
    description: str = ""
    structured: dict = None
    budget_currency: str | None = None

    @property
    def kind(self):
        return type("K", (), {"value": "group"})()


@dataclass
class _Call:
    title: str = "T"
    summary: str | None = None
    programme: str | None = None
    deadline: datetime | None = None
    budget_min: int | None = None
    budget_max: int | None = None
    currency: str | None = None
    structured: dict | None = None
    raw: dict = None


def _ollama_mock(*, content: dict, logprobs: list[dict] | dict | None = None):
    """Build a fake Ollama returning the given content + logprobs.

    `logprobs` is either a bare token list (wrapped into the OpenAI-shaped
    `{"content": [...]}`) or a pre-shaped dict placed at
    `choices[0].logprobs` verbatim."""
    body = {
        "model": "gemma4:e4b-it-q4_K_M",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps(content)},
            }
        ],
    }
    if logprobs is not None:
        body["choices"][0]["logprobs"] = (
            logprobs if isinstance(logprobs, dict) else {"content": logprobs}
        )

    def handler(request):
        return httpx.Response(200, json=body)

    return OllamaClient(
        base_url="http://x",
        model="gemma4:e4b-it-q4_K_M",
        transport=httpx.MockTransport(handler),
    )


def test_rank_pair_clean_pass_with_logprobs():
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    client = _ollama_mock(
        content={
            "topic_fit": {"rating": 4, "rationale": "ok", "quoted_evidence": "T"},
            "eligibility": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
            "exclusions": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
        },
        logprobs=[
            {
                "token": "4",
                "logprob": -0.05,
                "top_logprobs": [
                    {"token": "4", "logprob": -0.05},
                    {"token": "5", "logprob": -3.0},
                    {"token": "3", "logprob": -3.5},
                ],
            },
        ],
    )
    r = rank_pair(client=client, profile=profile, call=call, now=_NOW, logprobs_enabled=True)
    assert isinstance(r, RankResult)
    # 4.0 (essentially argmax with strong probability) × 20 = 80
    assert 75 <= r.score <= 85
    assert r.reason is None
    assert r.parsed.eligibility_rating == "pass"
    assert r.gates.timeline.status.value == "pass"


def test_rank_pair_short_circuits_on_gate_fail():
    """Timeline gate fails (deadline expired) — no LLM call. Ranking spec §2:
    the gate no longer fails on a merely-soon deadline, only a closed one."""
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    from datetime import timedelta

    call = _Call(deadline=_NOW - timedelta(days=1))

    # Build a client that asserts it's NEVER called.
    def handler(request):
        raise AssertionError("LLM should not be called when gate fails")

    client = OllamaClient(
        base_url="http://x",
        model="gemma4:e4b-it-q4_K_M",
        transport=httpx.MockTransport(handler),
    )
    r = rank_pair(client=client, profile=profile, call=call, now=_NOW)
    assert r.score == 0
    assert r.reason == "timeline"
    assert r.parsed is None  # no LLM call → no parsed response


def test_rank_pair_eligibility_fail_zeros_score():
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    client = _ollama_mock(
        content={
            "topic_fit": {"rating": 4, "rationale": "x"},
            "eligibility": {"rating": "fail", "rationale": "company-only"},
            "exclusions": {"rating": "pass", "rationale": "x"},
        }
    )
    r = rank_pair(client=client, profile=profile, call=call, now=_NOW)
    assert r.score == 0
    assert r.reason == "eligibility"


def _remote_mock(*, model: str = "claude-sonnet-4-6", content: dict | None = None):
    """Stand-in for AnthropicClient — exposes .chat() + .model, records calls.
    Used so escalation tests don't depend on the real Anthropic SDK being
    installed or reachable (they assert escalation behaviour only)."""
    from unittest.mock import MagicMock

    payload = json.dumps(
        content
        or {
            "topic_fit": {"rating": 5, "rationale": "ideal", "quoted_evidence": "match"},
            "eligibility": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
            "exclusions": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
        }
    )
    client = MagicMock()
    client.model = model
    client.chat.return_value = {
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": payload}}],
    }
    return client


def test_rank_pair_escalates_when_local_score_at_or_above_threshold():
    """Local Gemma scores 80 (≥45) ⇒ escalate; Sonnet's result replaces it."""
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    local = _ollama_mock(
        content={
            "topic_fit": {"rating": 4, "rationale": "ok"},
            "eligibility": {"rating": "pass"},
            "exclusions": {"rating": "pass"},
        }
    )
    remote = _remote_mock()  # returns rating 5 → 100
    r = rank_pair(
        client=local,
        profile=profile,
        call=call,
        now=_NOW,
        remote_client=remote,
        escalation_min_score=45,
    )
    assert remote.chat.call_count == 1
    assert "logprobs" not in remote.chat.call_args.kwargs  # remote client takes no logprobs kwargs
    assert r.escalated is True
    assert r.escalated_from_score == 80
    assert r.score == 100
    assert r.model == "claude-sonnet-4-6"
    assert r.analysis_model == "claude-sonnet-4-6"
    assert r.escalation_attempted is True
    assert r.escalation_outcome == "success"


def test_rank_pair_holds_only_the_remote_reply_to_the_rank_schema():
    """The escalation asks for schema-constrained JSON (structured outputs);
    the local Gemma call keeps plain JSON mode."""
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    local = _ollama_mock(
        content={
            "topic_fit": {"rating": 4, "rationale": "ok"},
            "eligibility": {"rating": "pass"},
            "exclusions": {"rating": "pass"},
        }
    )
    from unittest.mock import MagicMock

    local.chat = MagicMock(wraps=local.chat)
    remote = _remote_mock()
    rank_pair(
        client=local,
        profile=profile,
        call=call,
        now=_NOW,
        remote_client=remote,
        escalation_min_score=45,
    )
    assert remote.chat.call_args.kwargs["response_format"] == rank_response_format()
    assert local.chat.call_args.kwargs["response_format"] == {"type": "json_object"}


def test_rank_pair_does_not_escalate_below_threshold_clean_eligibility():
    """Local score 40 (<45), eligibility pass ⇒ stay local."""
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    local = _ollama_mock(
        content={
            "topic_fit": {"rating": 2, "rationale": "ok"},
            "eligibility": {"rating": "pass"},
            "exclusions": {"rating": "pass"},
        }
    )
    remote = _remote_mock()
    r = rank_pair(
        client=local,
        profile=profile,
        call=call,
        now=_NOW,
        remote_client=remote,
        escalation_min_score=45,
    )
    assert remote.chat.call_count == 0
    assert r.escalated is False
    assert r.escalation_attempted is False
    assert r.model.startswith("gemma4")


def test_rank_pair_escalates_on_eligibility_fail_high_topic():
    """eligibility=fail aggregates to 0 (<45) but topic argmax 4 ≥ 3 ⇒ escalate
    anyway (Codex R3 — the legalese failure mode)."""
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    local = _ollama_mock(
        content={
            "topic_fit": {"rating": 4, "rationale": "strong topic"},
            "eligibility": {"rating": "fail", "rationale": "maybe wrong"},
            "exclusions": {"rating": "pass"},
        }
    )
    remote = _remote_mock()
    r = rank_pair(
        client=local,
        profile=profile,
        call=call,
        now=_NOW,
        remote_client=remote,
        escalation_min_score=45,
    )
    assert remote.chat.call_count == 1
    assert r.escalated is True
    assert r.escalated_from_score == 0


def test_rank_pair_escalates_at_exact_threshold():
    """Local score exactly EQUAL to escalation_min_score escalates — pins the
    `>=` (not `>`) boundary semantics. Rating 4 → argmax score 80; threshold 80."""
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    local = _ollama_mock(
        content={
            "topic_fit": {"rating": 4, "rationale": "ok"},
            "eligibility": {"rating": "pass"},
            "exclusions": {"rating": "pass"},
        }
    )
    remote = _remote_mock()
    r = rank_pair(
        client=local,
        profile=profile,
        call=call,
        now=_NOW,
        remote_client=remote,
        escalation_min_score=80,
    )
    assert remote.chat.call_count == 1
    assert r.escalated is True
    assert r.escalated_from_score == 80


def test_rank_pair_remote_parse_error_keeps_local():
    """Sonnet answers but with unparseable content ⇒ ParseError is swallowed,
    local result survives with escalation_outcome='parse_error'."""
    from unittest.mock import MagicMock

    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    local = _ollama_mock(
        content={
            "topic_fit": {"rating": 4, "rationale": "ok"},
            "eligibility": {"rating": "pass"},
            "exclusions": {"rating": "pass"},
        }
    )
    remote = MagicMock()
    remote.model = "claude-sonnet-4-6"
    remote.chat.return_value = {"choices": [{"message": {"content": "not json"}}]}
    r = rank_pair(
        client=local,
        profile=profile,
        call=call,
        now=_NOW,
        remote_client=remote,
        escalation_min_score=45,
    )
    assert r.escalated is False
    assert r.score == 80  # local survived
    assert r.escalation_attempted is True
    assert r.escalation_outcome == "parse_error"


def test_rank_pair_no_escalation_on_eligibility_fail_low_topic():
    """eligibility=fail with topic argmax 2 (<3) does NOT trigger the Codex R3
    escalation path — score 0 (<45) and the topic condition misses too."""
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    local = _ollama_mock(
        content={
            "topic_fit": {"rating": 2, "rationale": "weak topic"},
            "eligibility": {"rating": "fail", "rationale": "company-only"},
            "exclusions": {"rating": "pass"},
        }
    )
    remote = _remote_mock()
    r = rank_pair(
        client=local,
        profile=profile,
        call=call,
        now=_NOW,
        remote_client=remote,
        escalation_min_score=45,
    )
    assert remote.chat.call_count == 0
    assert r.escalated is False
    assert r.escalation_attempted is False
    assert r.score == 0


def test_rank_pair_no_escalation_when_remote_disabled():
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    local = _ollama_mock(
        content={
            "topic_fit": {"rating": 5, "rationale": "ok"},
            "eligibility": {"rating": "pass"},
            "exclusions": {"rating": "pass"},
        }
    )
    r = rank_pair(client=local, profile=profile, call=call, now=_NOW, remote_client=None)
    assert r.escalated is False
    assert r.model.startswith("gemma4")


def test_rank_pair_remote_failure_keeps_local_records_attempt():
    """Sonnet raises LLMError ⇒ keep the local result, escalated=False,
    escalation_attempted=True, outcome='llm_error'. Job still succeeds."""
    from grant_lab.ranking.ollama_client import LLMError

    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    local = _ollama_mock(
        content={
            "topic_fit": {"rating": 4, "rationale": "ok"},
            "eligibility": {"rating": "pass"},
            "exclusions": {"rating": "pass"},
        }
    )
    remote = _remote_mock()
    remote.chat = lambda *a, **kw: (_ for _ in ()).throw(LLMError("boom"))
    r = rank_pair(
        client=local,
        profile=profile,
        call=call,
        now=_NOW,
        remote_client=remote,
        escalation_min_score=45,
    )
    assert r.escalated is False
    assert r.score == 80  # local survived
    assert r.model.startswith("gemma4")
    assert r.escalation_attempted is True
    assert r.escalation_outcome == "llm_error"


def test_rank_pair_uses_argmax_times_20():
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    client = _ollama_mock(
        content={
            "topic_fit": {"rating": 3, "rationale": "x"},
            "eligibility": {"rating": "pass"},
            "exclusions": {"rating": "pass"},
        },
        logprobs=None,
    )
    r = rank_pair(client=client, profile=profile, call=call, now=_NOW)
    # argmax 3 × 20 = 60
    assert r.score == 60
    assert r.reason is None


def test_rank_pair_scores_a_call_closing_tomorrow():
    """G12: deadline tomorrow is a real score, not a timeline zero."""
    from datetime import timedelta

    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(title="Pathology funding", summary="x", deadline=_NOW + timedelta(days=1))
    client = _ollama_mock(
        content={
            "topic_fit": {"rating": 4, "rationale": "ok"},
            "eligibility": {"rating": "pass"},
            "exclusions": {"rating": "pass"},
        }
    )
    r = rank_pair(client=client, profile=profile, call=call, now=_NOW)
    assert r.score == 80
    assert r.reason is None
    assert r.gates.timeline.status.value == "pass"


def test_rank_pair_expired_call_fails_even_with_must_include():
    """G12: must_include no longer rescues a closed call, and the LLM never runs."""
    from datetime import timedelta

    profile = _Profile(
        structured={"areas": [], "hard_exclusions": [], "must_include": ["pathology"]}
    )
    call = _Call(title="Pathology funding", summary="x", deadline=_NOW - timedelta(days=1))
    client = _ollama_mock(
        content={
            "topic_fit": {"rating": 5, "rationale": "x"},
            "eligibility": {"rating": "pass"},
            "exclusions": {"rating": "pass"},
        }
    )
    r = rank_pair(client=client, profile=profile, call=call, now=_NOW)
    assert r.score == 0
    assert r.reason == "timeline"
    assert r.parsed is None
    assert r.must_include_override is True


def test_rank_pair_must_include_match_requires_term_in_text():
    """Profile has must_include keywords but call text doesn't contain any —
    must_include_override stays False even though the call is expired
    (ranking spec §2/D8: nothing bypasses an expired timeline any more, so
    the expired deadline here only sets up the score=0 short-circuit; the
    override flag is still driven purely by text matching)."""
    from datetime import timedelta

    profile = _Profile(
        structured={
            "areas": [],
            "hard_exclusions": [],
            "must_include": ["pathology"],
        }
    )
    call = _Call(
        title="Unrelated call about EVs",
        summary="vehicle electrification",
        deadline=_NOW - timedelta(days=1),  # timeline FAIL (expired)
    )

    # LLM should NOT be called.
    def boom(req):
        raise AssertionError("LLM should not be called")

    bad_client = OllamaClient(
        base_url="http://x",
        model="m",
        transport=httpx.MockTransport(boom),
    )
    r = rank_pair(client=bad_client, profile=profile, call=call, now=_NOW)
    assert r.score == 0
    assert r.reason == "timeline"
    assert r.must_include_override is False


# ---------------------------------------------------------------------------
# Plan D Task 5 — embedding pre-gate integration
# ---------------------------------------------------------------------------


def _happy_path_v2_client():
    """Clean LLM response with logprobs that yield expected ~4.0 → score 80."""
    return _ollama_mock(
        content={
            "topic_fit": {"rating": 4, "rationale": "ok", "quoted_evidence": None},
            "eligibility": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
            "exclusions": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
        },
        logprobs=[
            {
                "token": "4",
                "logprob": -0.05,
                "top_logprobs": [
                    {"token": "4", "logprob": -0.05},
                    {"token": "5", "logprob": -3.0},
                    {"token": "3", "logprob": -3.5},
                ],
            },
        ],
    )


def _make_profile(*, embedding, embedding_model, structured=None, description="desc"):
    """Build a SimpleNamespace profile with all the attributes rank_pair +
    build_messages need."""
    from types import SimpleNamespace

    return SimpleNamespace(
        id=1,
        name="P",
        description=description,
        kind=SimpleNamespace(value="group"),
        structured=structured if structured is not None else {},
        budget_currency=None,
        embedding=embedding,
        embedding_model=embedding_model,
    )


def _make_call(
    *, embedding, embedding_model, title="T", summary="S", full_text="F", deadline_days=30
):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    return SimpleNamespace(
        id=1,
        title=title,
        summary=summary,
        full_text=full_text,
        programme=None,
        deadline=datetime.now(UTC) + timedelta(days=deadline_days),
        budget_min=None,
        budget_max=None,
        currency=None,
        structured={},
        keywords=[],
        embedding=embedding,
        embedding_model=embedding_model,
    )


def test_rank_pair_short_circuits_on_low_cosine_similarity():
    """Embedding gate FAIL: cosine 0.10 < 0.30 → score 0, reason 'embedding',
    LLM never called."""
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    profile = _make_profile(
        embedding=[1.0, 0.0, 0.0] + [0.0] * 1021,
        embedding_model="bge-m3",
        description="research desc",
    )
    call = _make_call(
        embedding=[0.1, 0.99, 0.0] + [0.0] * 1021,
        embedding_model="bge-m3",
        title="Off-topic call",
        summary="EV charging",
        full_text="x",
    )

    client = MagicMock()
    client.chat.side_effect = AssertionError("LLM should not be called for embedding-FAIL")

    result = rank_pair(client=client, profile=profile, call=call, now=datetime.now(UTC))
    assert result.score == 0
    assert result.reason == "embedding"
    assert result.embed_similarity is not None
    assert result.embed_similarity < 0.30


def test_rank_pair_persists_embed_similarity_on_clean_path():
    """Even on clean LLM-completion paths, embed_similarity is set on the result."""
    from datetime import UTC, datetime

    profile = _make_profile(
        embedding=[1.0] + [0.0] * 1023,
        embedding_model="bge-m3",
        description="cancer research",
    )
    call = _make_call(
        embedding=[0.95, 0.31] + [0.0] * 1022,  # cosine ~0.95
        embedding_model="bge-m3",
        title="On-topic call",
        summary="cancer biomarker",
        full_text="cancer",
    )

    client = _happy_path_v2_client()

    result = rank_pair(client=client, profile=profile, call=call, now=datetime.now(UTC))
    assert result.embed_similarity is not None
    assert result.embed_similarity > 0.9


def test_rank_pair_falls_through_to_llm_when_either_embedding_missing():
    """Either embedding NULL → cosine None → UNKNOWN gate → LLM runs."""
    from datetime import UTC, datetime

    profile = _make_profile(
        embedding=None,  # MISSING
        embedding_model=None,
    )
    call = _make_call(
        embedding=[0.5] * 1024,
        embedding_model="bge-m3",
    )

    client = _happy_path_v2_client()

    result = rank_pair(client=client, profile=profile, call=call, now=datetime.now(UTC))
    # LLM was called; score reflects the rubric path.
    assert result.score > 0
    assert result.reason is None
    # No similarity recorded.
    assert result.embed_similarity is None


def test_rank_pair_returns_none_similarity_on_model_mismatch(caplog):
    """Codex H2 regression: profile embedded under bge-m3-old, call under bge-m3 →
    cosine is meaningless → return None → UNKNOWN gate → LLM runs. WARN log emitted
    so operator runs the backfill CLI."""
    import logging
    from datetime import UTC, datetime

    profile = _make_profile(
        embedding=[1.0] + [0.0] * 1023,
        embedding_model="bge-m3-old",
        description="cancer",
    )
    call = _make_call(
        embedding=[1.0] + [0.0] * 1023,
        embedding_model="bge-m3",
        title="On-topic",
        summary="cancer",
        full_text="cancer",
    )
    call.id = 42

    client = _happy_path_v2_client()

    with caplog.at_level(logging.WARNING, logger="grant_lab.ranking.rank_pair"):
        result = rank_pair(client=client, profile=profile, call=call, now=datetime.now(UTC))

    # LLM ran (gate was UNKNOWN, not FAIL)
    assert result.score > 0
    assert result.reason is None
    # Similarity NOT recorded (we couldn't compute a meaningful one)
    assert result.embed_similarity is None
    # Warning emitted
    assert any(
        "embedding_model_mismatch" in (r.getMessage() or "")
        or "embedding_model_mismatch" in r.message
        for r in caplog.records
    )


def test_rank_pair_must_include_bypasses_embedding_gate():
    """A must_include keyword match bypasses the embedding gate, just like timeline."""
    from datetime import UTC, datetime

    profile = _make_profile(
        embedding=[1.0, 0.0, 0.0] + [0.0] * 1021,
        embedding_model="bge-m3",
        structured={"must_include": ["ferroptosis"]},
    )
    call = _make_call(
        embedding=[0.05, 1.0, 0.0] + [0.0] * 1021,  # cosine ~0.05 — would FAIL gate
        embedding_model="bge-m3",
        title="Ferroptosis study",
        summary="x",
        full_text="x",
    )

    client = _happy_path_v2_client()

    result = rank_pair(client=client, profile=profile, call=call, now=datetime.now(UTC))
    # Bypassed: LLM ran, score is from rubric, not 0.
    assert result.score > 0
    assert result.reason is None
    assert result.must_include_override is True
    # Similarity still recorded.
    assert result.embed_similarity is not None


def test_rank_pair_threshold_kwarg_actually_changes_the_gate_cutoff():
    """Threading a custom embedding_threshold into rank_pair changes the
    gate cutoff. Cosine ~0.4 PASSes the default 0.30 but FAILs at 0.50,
    proving the kwarg is honoured (the single source of truth — there is
    no module-level constant or lru_cache to silently shadow it)."""
    import math
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    # Build a profile/call pair with cosine ~0.4 (above 0.30, below 0.50).
    # cos(theta) for [1, 0, ...] vs [0.4, sqrt(1-0.16), 0, ...] = 0.4
    profile = _make_profile(
        embedding=[1.0] + [0.0] * 1023,
        embedding_model="bge-m3",
        description="x",
    )
    call = _make_call(
        embedding=[0.4, math.sqrt(1 - 0.16)] + [0.0] * 1022,
        embedding_model="bge-m3",
    )

    client = MagicMock()
    client.chat.side_effect = AssertionError(
        "LLM should not be called: threshold=0.5 must FAIL the embedding gate"
    )

    # Note: bypass the partial-wrapped `rank_pair` — we want to set the
    # threshold explicitly, not inherit the test-default of 0.30.
    result = _rank_pair(
        client=client,
        profile=profile,
        call=call,
        now=datetime.now(UTC),
        embedding_threshold=0.5,
    )
    assert result.score == 0
    assert result.reason == "embedding"
    assert result.embed_similarity is not None
    assert 0.30 < result.embed_similarity < 0.50


def test_call_matches_must_include_searches_full_text():
    """Codex Plan F regression: after Plan F moved Vinnova's substantive
    prose from `summary` into `full_text`, must_include keywords must
    match against the call's body text — not just title+summary+keywords.
    A term that lives only in `full_text` would otherwise be silently
    missed by both the timeline-bypass logic and the digest bucket."""
    from types import SimpleNamespace

    from grant_lab.ranking.rank_pair import _call_matches_must_include

    profile = SimpleNamespace(structured={"must_include": ["ferroptosis"]})
    call = SimpleNamespace(
        title="A study on cell death",
        summary="Brief abstract with no special terms.",
        full_text="Detailed paragraph mentioning ferroptosis as a key mechanism.",
        keywords=[],
    )
    assert _call_matches_must_include(profile, call) is True


def test_call_matches_must_include_word_boundary_no_substring():
    """B11 Defect 1: a short term like 'ai' must NOT match inside 'training'
    or 'brain' (bare-substring would have force-included an off-topic call into
    the paid LLM/escalation path)."""
    from types import SimpleNamespace

    from grant_lab.ranking.rank_pair import _call_matches_must_include

    profile = SimpleNamespace(structured={"must_include": ["pathology"]})
    # 'pathology' as a term, but the call only contains 'pathological' — a real
    # word-boundary mismatch is hard for a single term, so test the classic
    # short-term substring trap with a >=3-char term that is a substring of a
    # longer word.
    profile = SimpleNamespace(structured={"must_include": ["rna"]})
    call = SimpleNamespace(
        title="Brain training programme",  # contains 'rna' inside 'training'? no — but 'brain'? no
        summary="A study of learning and memory",
        full_text="The programme uses turnaround metrics",  # 'rna' inside 'turnaround'
        keywords=[],
    )
    assert _call_matches_must_include(profile, call) is False


def test_call_matches_must_include_standalone_token_case_insensitive():
    """B11 Defect 1: a standalone 'AI' token DOES match (case-insensitive)
    even though 'ai' is filtered as too short — but a >=3-char real token like
    'rna' matches as a whole word regardless of case."""
    from types import SimpleNamespace

    from grant_lab.ranking.rank_pair import _call_matches_must_include

    profile = SimpleNamespace(structured={"must_include": ["rna"]})
    call = SimpleNamespace(
        title="New RNA sequencing grant",  # standalone 'RNA' token
        summary="",
        full_text="",
        keywords=[],
    )
    assert _call_matches_must_include(profile, call) is True


def test_call_matches_must_include_short_term_ignored():
    """B11 Defect 1: a sub-minimum-length term ('ai', 'eu') is ignored entirely
    so it can never force-include a call — even a standalone 'AI' token."""
    from types import SimpleNamespace

    from grant_lab.ranking.rank_pair import _call_matches_must_include

    profile = SimpleNamespace(structured={"must_include": ["ai", "eu"]})
    call = SimpleNamespace(
        title="AI in the EU",  # standalone tokens, but both terms are < 3 chars
        summary="",
        full_text="",
        keywords=[],
    )
    assert _call_matches_must_include(profile, call) is False


def test_settings_embedding_pregate_threshold_reads_env_var(monkeypatch):
    """The Settings field that callers (worker → rank_pair) read is the
    single source of truth — it picks up EMBEDDING_PREGATE_THRESHOLD from
    the environment. (Replaces the old lru_cache-flush test.)"""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("EMBEDDING_PREGATE_THRESHOLD", "0.42")

    from grant_lab.config import Settings

    assert Settings().embedding_pregate_threshold == 0.42


# ---------------------------------------------------------------------------
# Phase 3 Task 17 — G-Eval expected value wired through rank_pair
# ---------------------------------------------------------------------------


def _logprobs_for(content: str, *, rating_char: str, top: list[dict]):
    """Char-tokenize content into an Ollama-shaped logprobs.content list,
    attaching `top` at the structural rating digit."""
    marker = '"rating":'
    pos = content.index(marker) + len(marker)
    while content[pos] == " ":
        pos += 1
    out = []
    for i, ch in enumerate(content):
        tok = {
            "token": ch,
            "bytes": list(ch.encode()),
            "logprob": -0.01,
            "top_logprobs": [{"token": ch, "logprob": -0.01, "bytes": list(ch.encode())}],
        }
        if i == pos and ch == rating_char:
            tok["top_logprobs"] = top
        out.append(tok)
    return {"content": out}


def test_rank_pair_populates_expected_value_from_logprobs():
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    # The _logprobs_for offsets are computed against this exact literal;
    # _ollama_mock re-serializes via json.dumps, whose default ", " / ": "
    # separators reproduce it byte-for-byte.
    content = (
        '{"topic_fit": {"rating": 4, "rationale": "x"}, '
        '"eligibility": {"rating": "pass"}, "exclusions": {"rating": "pass"}}'
    )
    assert json.dumps(json.loads(content)) == content
    top = [
        {"token": "4", "logprob": math.log(0.6), "bytes": [52]},
        {"token": "3", "logprob": math.log(0.4), "bytes": [51]},
    ]
    client = _ollama_mock(
        content=json.loads(content),
        logprobs=_logprobs_for(content, rating_char="4", top=top),
    )
    # logprobs_enabled=True is now REQUIRED: the default stopped asking for
    # logprobs (RANKING_LOGPROBS_ENABLED), and a transport mock would happily
    # return a block nobody requested, so without this the test would assert a
    # state production can no longer reach.
    r = rank_pair(client=client, profile=profile, call=call, now=_NOW, logprobs_enabled=True)
    # EV = 4*0.6 + 3*0.4 = 3.6 → round(3.6*20) = 72 (NOT the argmax 80)
    assert r.topic_fit_expected is not None
    assert abs(r.topic_fit_expected - 3.6) < 1e-6
    assert r.score == 72
    # raw_response must NOT carry the full logprobs block.
    assert "logprobs" not in r.raw_response.get("choices", [{}])[0]


# ---------------------------------------------------------------------------
# RANKING_LOGPROBS_ENABLED — the request must not ask for what nobody reads
# ---------------------------------------------------------------------------


class _RecordingClient:
    """Captures the kwargs rank_pair passes to `chat` and returns a canned
    envelope. Not a transport mock: the assertion is about the REQUEST shape,
    which an httpx-level mock would hide behind serialization."""

    model = "gemma4:e4b-it-q4_K_M"

    def __init__(self, content: dict, logprobs=None):
        body = {
            "model": self.model,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": json.dumps(content)}}
            ],
        }
        if logprobs is not None:
            body["choices"][0]["logprobs"] = {"content": logprobs}
        self._body = body
        self.calls: list[dict] = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self._body


_CLEAN = {
    "topic_fit": {"rating": 4, "rationale": "ok", "quoted_evidence": "T"},
    "eligibility": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
    "exclusions": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
}

# The alternatives attached to the rating digit: 60% on "4", 40% on "3".
_EV_TOP = [
    {"token": "4", "logprob": math.log(0.6), "bytes": [52]},
    {"token": "3", "logprob": math.log(0.4), "bytes": [51]},
]


def test_logprobs_are_not_requested_by_default():
    """Measured at ~6x wall clock (127s vs 21s per ranking) for under 3 points
    of score movement, so the default must not pay for them."""
    client = _RecordingClient(_CLEAN)
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))

    rank_pair(client=client, profile=profile, call=call, now=_NOW)

    assert len(client.calls) == 1
    assert "logprobs" not in client.calls[0]
    assert "top_logprobs" not in client.calls[0]


def test_logprobs_are_requested_when_the_setting_is_on():
    client = _RecordingClient(_CLEAN)
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))

    rank_pair(client=client, profile=profile, call=call, now=_NOW, logprobs_enabled=True)

    assert client.calls[0]["logprobs"] is True
    assert client.calls[0]["top_logprobs"] == 10


def test_with_logprobs_off_the_score_is_the_plain_rating():
    """Falling back to argmax is the SUPPORTED path — it is the one the remote
    Sonnet ranker already takes — so turning the setting off must degrade
    cleanly, not blank the score."""
    client = _RecordingClient(_CLEAN)
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))

    result = rank_pair(client=client, profile=profile, call=call, now=_NOW)

    assert result.topic_fit_expected is None
    assert result.score == 4 * 20


def test_with_logprobs_on_the_score_is_the_expected_value():
    """The other direction: on, the score must actually move off the multiple
    of 20 — otherwise the setting would be inert and this pair of tests would
    pass on code that ignores it entirely.

    The stream must CONTAIN the content: since the thinking-token fix, EV
    aligns the rating offset against the joined token stream and fails closed
    when it cannot find the content there."""
    client = _RecordingClient(
        _CLEAN,
        logprobs=_logprobs_for(json.dumps(_CLEAN), rating_char="4", top=_EV_TOP)["content"],
    )
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))

    result = rank_pair(client=client, profile=profile, call=call, now=_NOW, logprobs_enabled=True)

    assert result.topic_fit_expected is not None
    assert result.score != 4 * 20
    assert result.score == round((0.6 * 4 + 0.4 * 3) * 20)


# ---------------------------------------------------------------------------
# Ranking freshness Task 5 — escalation_wanted
# ---------------------------------------------------------------------------


def _content(topic, elig="pass"):
    return {
        "topic_fit": {"rating": topic, "rationale": "x"},
        "eligibility": {"rating": elig},
        "exclusions": {"rating": "pass"},
    }


def test_escalation_is_wanted_without_a_remote_client():
    """Spec D6: `escalation_wanted` is the local outcome, not remote availability.
    Without it the worker cannot tell a cap-withheld escalation from none."""
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    r = rank_pair(
        client=_ollama_mock(content=_content(4)),
        profile=profile,
        call=call,
        now=_NOW,
        remote_client=None,
        escalation_min_score=45,
    )
    assert r.score == 80
    assert r.escalation_wanted is True
    assert r.escalation_attempted is False


def test_escalation_is_wanted_for_an_eligibility_fail_with_strong_topic():
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    r = rank_pair(
        client=_ollama_mock(content=_content(3, "fail")),
        profile=profile,
        call=call,
        now=_NOW,
        remote_client=None,
        escalation_min_score=45,
    )
    assert r.score == 0
    assert r.escalation_wanted is True


def test_escalation_is_not_wanted_below_the_threshold():
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    r = rank_pair(
        client=_ollama_mock(content=_content(2)),
        profile=profile,
        call=call,
        now=_NOW,
        remote_client=None,
        escalation_min_score=45,
    )
    assert r.score == 40
    assert r.escalation_wanted is False


def test_a_successful_escalation_is_also_wanted():
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    r = rank_pair(
        client=_ollama_mock(content=_content(4)),
        profile=profile,
        call=call,
        now=_NOW,
        remote_client=_remote_mock(),
        escalation_min_score=45,
    )
    assert r.escalated is True
    assert r.escalation_wanted is True


def test_a_failed_escalation_is_still_wanted():
    from grant_lab.ranking.ollama_client import LLMError

    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    remote = _remote_mock()
    remote.chat.side_effect = LLMError("credit")
    r = rank_pair(
        client=_ollama_mock(content=_content(4)),
        profile=profile,
        call=call,
        now=_NOW,
        remote_client=remote,
        escalation_min_score=45,
    )
    assert r.escalation_outcome == "llm_error"
    assert r.escalation_wanted is True


def test_lowering_the_hard_fail_threshold_moves_a_pair_from_zero_to_scored():
    """G24, the reviewer's probe as a test. Same pair, same model output; only
    the hard-fail threshold moves (0.20 -> 0.10), and the score moves from a
    gate-fail 0 to an LLM score. No call or profile fingerprint input changed,
    which is why the scoring fingerprint exists (spec gap 3)."""
    import math

    p_vec = [1.0] + [0.0] * 1023
    c_vec = [0.15, math.sqrt(1 - 0.15**2)] + [0.0] * 1022  # cosine 0.15
    profile = _make_profile(embedding=p_vec, embedding_model="bge-m3")
    call = _make_call(embedding=c_vec, embedding_model="bge-m3", deadline_days=400)
    client = _ollama_mock(content=_content(4))
    strict = rank_pair(
        client=client, profile=profile, call=call, now=_NOW, embedding_hard_fail_threshold=0.20
    )
    loose = rank_pair(
        client=client, profile=profile, call=call, now=_NOW, embedding_hard_fail_threshold=0.10
    )
    assert (strict.score, strict.reason) == (0, "embedding")
    assert (loose.score, loose.reason) == (80, None)


def test_an_unusable_remote_reply_counts_as_a_parse_failure():
    """A refusal or truncation (UnusableReplyError) is both an LLMError and a
    ParseError. It recurs on the same inputs, so it must count toward gave_up
    as parse_error, not stay pending every night as llm_error (final review
    2026-10-05, finding 4)."""
    from grant_lab.ranking.anthropic_client import UnusableReplyError

    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    remote = _remote_mock()
    remote.chat.side_effect = UnusableReplyError("anthropic refused")
    r = rank_pair(
        client=_ollama_mock(content=_content(4)),
        profile=profile,
        call=call,
        now=_NOW,
        remote_client=remote,
        escalation_min_score=45,
    )
    assert r.escalation_outcome == "parse_error"
    assert r.escalated is False
