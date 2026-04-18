import json
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial

import httpx

from call_monitor.ranking.ollama_client import OllamaClient
from call_monitor.ranking.rank_pair import RankResult
from call_monitor.ranking.rank_pair import rank_pair as _rank_pair

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


def _ollama_mock(*, content: dict, logprobs: list[dict] | None = None):
    """Build a fake Ollama returning the given content + logprobs."""
    body = {
        "model": "gemma4:e4b-it-q4_K_M",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": json.dumps(content)},
        }],
    }
    if logprobs is not None:
        body["choices"][0]["logprobs"] = {"content": logprobs}
    def handler(request):
        return httpx.Response(200, json=body)
    return OllamaClient(
        base_url="http://x", model="gemma4:e4b-it-q4_K_M",
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
            {"token": "4", "logprob": -0.05, "top_logprobs": [
                {"token": "4", "logprob": -0.05},
                {"token": "5", "logprob": -3.0},
                {"token": "3", "logprob": -3.5},
            ]},
        ],
    )
    r = rank_pair(client=client, profile=profile, call=call, now=_NOW)
    assert isinstance(r, RankResult)
    # 4.0 (essentially argmax with strong probability) × 20 = 80
    assert 75 <= r.score <= 85
    assert r.reason is None
    assert r.parsed.eligibility_rating == "pass"
    assert r.gates.timeline.status.value == "pass"


def test_rank_pair_short_circuits_on_gate_fail():
    """Timeline gate fails (deadline tomorrow) — no LLM call."""
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    from datetime import timedelta
    call = _Call(deadline=_NOW + timedelta(days=1))
    # Build a client that asserts it's NEVER called.
    def handler(request):
        raise AssertionError("LLM should not be called when gate fails")
    client = OllamaClient(
        base_url="http://x", model="gemma4:e4b-it-q4_K_M",
        transport=httpx.MockTransport(handler),
    )
    r = rank_pair(client=client, profile=profile, call=call, now=_NOW)
    assert r.score == 0
    assert r.reason == "timeline"
    assert r.parsed is None  # no LLM call → no parsed response


def test_rank_pair_eligibility_fail_zeros_score():
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    client = _ollama_mock(content={
        "topic_fit": {"rating": 4, "rationale": "x"},
        "eligibility": {"rating": "fail", "rationale": "company-only"},
        "exclusions": {"rating": "pass", "rationale": "x"},
    })
    r = rank_pair(client=client, profile=profile, call=call, now=_NOW)
    assert r.score == 0
    assert r.reason == "eligibility"


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


def test_rank_pair_must_include_bypasses_timeline_gate():
    """A call matching a must_include keyword still gets LLM-scored even when
    the timeline gate would have FAILed (deadline tomorrow)."""
    from datetime import timedelta
    profile = _Profile(structured={
        "areas": [], "hard_exclusions": [],
        "must_include": ["pathology"],
    })
    call = _Call(
        title="Pathology funding announcement",
        summary="x",
        deadline=_NOW + timedelta(days=1),  # would fail timeline gate
    )
    client = _ollama_mock(
        content={
            "topic_fit": {"rating": 4, "rationale": "ok"},
            "eligibility": {"rating": "pass"},
            "exclusions": {"rating": "pass"},
        },
        logprobs=[
            {"token": "4", "logprob": -0.1, "top_logprobs": [
                {"token": "4", "logprob": -0.1},
                {"token": "3", "logprob": -2.5},
            ]},
        ],
    )
    r = rank_pair(client=client, profile=profile, call=call, now=_NOW)
    # Bypassed timeline → got an LLM score, not 0.
    assert r.score >= 60
    assert r.reason is None
    # Gates were still evaluated (for diagnostic display).
    assert r.gates.timeline.status.value == "fail"
    # The override flag is set so the worker's criteria-row writer knows to
    # continue past the timeline-fail row instead of bailing.
    assert r.must_include_override is True


def test_rank_pair_must_include_match_requires_term_in_text():
    """Profile has must_include keywords but call text doesn't contain any —
    behaves like a normal pair (timeline FAIL → score=0)."""
    from datetime import timedelta
    profile = _Profile(structured={
        "areas": [], "hard_exclusions": [],
        "must_include": ["pathology"],
    })
    call = _Call(
        title="Unrelated call about EVs",
        summary="vehicle electrification",
        deadline=_NOW + timedelta(days=1),  # timeline FAIL
    )
    # LLM should NOT be called.
    def boom(req):
        raise AssertionError("LLM should not be called")
    bad_client = OllamaClient(
        base_url="http://x", model="m",
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
            {"token": "4", "logprob": -0.05, "top_logprobs": [
                {"token": "4", "logprob": -0.05},
                {"token": "5", "logprob": -3.0},
                {"token": "3", "logprob": -3.5},
            ]},
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


def _make_call(*, embedding, embedding_model, title="T", summary="S", full_text="F",
               deadline_days=30):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    return SimpleNamespace(
        id=1,
        title=title,
        summary=summary,
        full_text=full_text,
        programme=None,
        deadline=datetime.now(UTC) + timedelta(days=deadline_days),
        budget_min=None, budget_max=None, currency=None,
        structured={}, keywords=[],
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

    with caplog.at_level(logging.WARNING, logger="call_monitor.ranking.rank_pair"):
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
        client=client, profile=profile, call=call, now=datetime.now(UTC),
        embedding_threshold=0.5,
    )
    assert result.score == 0
    assert result.reason == "embedding"
    assert result.embed_similarity is not None
    assert 0.30 < result.embed_similarity < 0.50


def test_settings_embedding_pregate_threshold_reads_env_var(monkeypatch):
    """The Settings field that callers (worker → rank_pair) read is the
    single source of truth — it picks up EMBEDDING_PREGATE_THRESHOLD from
    the environment. (Replaces the old lru_cache-flush test.)"""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("EMBEDDING_PREGATE_THRESHOLD", "0.42")

    from call_monitor.config import Settings

    assert Settings().embedding_pregate_threshold == 0.42
