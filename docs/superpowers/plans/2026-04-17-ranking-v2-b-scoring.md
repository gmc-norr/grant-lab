# Ranking v2 — Plan B: Criterion-Rubric Scoring (`rank_pair_v2`)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Depends on:** Plan A (`2026-04-17-ranking-v2-a-schema.md`) must be merged first — schema, scheme-aware queue, scheme-aware read-paths, and the worker's `NotImplementedError`-for-v2 stub all live there.

**Goal:** Implement `rank_pair_v2` end-to-end: rule-based gates (timeline / budget / TRL with currency-aware comparison), LLM-judged criteria (topic_fit / eligibility / exclusions) with required quoted evidence, G-Eval logprob-weighted aggregation, sparse `call_ranking_criteria` writes, and the worker dispatch that replaces Plan A's `NotImplementedError` stub. Add minimal UI rendering of the criterion breakdown so users see the structured output. After this plan ships, profiles flipped to `ranking_scheme='v2'` get the new path end-to-end.

**Architecture:** New `ranking/criteria.py` (rule-based gates), `ranking/aggregate.py` (G-Eval + aggregation), `ranking/prompt.py` extended with `build_messages_v2`, `ranking/rank_pair.py` extended with `rank_pair_v2`. `OllamaClient` extended to request and return logprobs. Worker reads `job.scheme` (Plan A) and dispatches: `'v1'` → existing `rank_pair_v1`, `'v2'` → new `rank_pair_v2`. Worker writes sparse `CallRankingCriterion` rows for the criteria actually evaluated. Templates extended for criterion display. `prompt_version` bumps to `'v2.0'`.

**Tech Stack:** Python 3.12 · `httpx` (Ollama client extension) · existing `OllamaClient` · `pytest` + `testcontainers[postgres]` · Plans 1–5 + Ranking v2 Plan A

---

## Design decisions (fix-now; push back if you disagree)

- **Sparse criteria rows.** Per the spec, only the criteria actually evaluated are inserted into `call_ranking_criteria`. A pair that fails the timeline gate gets ONE row (`criterion='timeline', rating='fail'`), nothing else. The aggregate row's `aggregate_reason` is the canonical "why score=0" signal.
- **Logprob fallback is silent but counted.** When Ollama doesn't return logprobs for a particular call (model/version edge case), `aggregate.logprob_expected` falls back to argmax integer rating, increments a Prometheus-style counter, and logs at WARNING. Plan C surfaces the counter in `/admin`.
- **Criterion prompt is fixed; bump `prompt_version` to bump it.** v2.0 is the initial criterion prompt. Edits ship as v2.1, v2.2, etc. The constant lives in `ranking/prompt.py`.
- **`rank_pair_v2` returns a richer dataclass** than v1's `RankResult` — include the per-criterion breakdown so the worker can persist criteria rows without re-parsing. Name: `RankResultV2`. Includes the rule-based gate outputs (so the worker can write timeline/budget/TRL criteria rows when relevant).
- **Currency-aware budget gate is the only place currency is touched.** `profiles.budget_currency` is read by `criteria.budget_gate`; nowhere else in this plan reads or writes the field. Plan A's profile edit form already exposes the dropdown.
- **Quoted evidence is enforced at parse time, not at rendering.** If the LLM returns `quoted_evidence` for `topic_fit`, but the substring is not present in the call's title/summary/full_text, the parser logs a warning, sets `quoted_evidence=None`, and continues. We do NOT fail the ranking — quote-checking is a defense, not a gate.
- **Token cap for the prompt's call text is 4000 chars** (vs v1's 2000). Tests use shorter fixtures, but the constant `CALL_TEXT_LIMIT_V2 = 4000` lives at the top of `prompt.py`.
- **Templates additions are minimal and behind a flag.** The call detail page renders the criterion breakdown ONLY for v2 rows (detected via `ranking.scheme == 'v2'`). v1 rendering is untouched.

---

## File structure created or modified by this plan

```
call-monitor/
├── src/call_monitor/
│   ├── ranking/
│   │   ├── criteria.py                              # NEW: rule-based gates
│   │   ├── aggregate.py                             # NEW: G-Eval + aggregation
│   │   ├── prompt.py                                # extend: build_messages_v2
│   │   ├── rank_pair.py                             # extend: rank_pair_v2 + RankResultV2
│   │   ├── ollama_client.py                         # extend: logprobs param
│   │   └── worker.py                                # extend: dispatch + write criteria
│   ├── web/
│   │   ├── routes/
│   │   │   └── calls.py                             # call detail context: criterion rows
│   │   └── templates/
│   │       ├── call_detail.html                     # render v2 criterion breakdown
│   │       └── ../../digest/templates/
│   │           └── digest.html.jinja                # one-line criterion summary on v2 entries
└── tests/
    ├── test_ranking_criteria.py                     # NEW: gates
    ├── test_ranking_aggregate.py                    # NEW: G-Eval + aggregation
    ├── test_ranking_prompt_v2.py                    # NEW: prompt builder + parser
    ├── test_ranking_rank_pair_v2.py                 # NEW: orchestration with fake Ollama
    ├── test_ranking_ollama_logprobs.py              # NEW: client logprobs
    ├── test_ranking_worker_v2.py                    # NEW: dispatch + criteria rows written
    ├── test_web_call_detail_v2.py                   # NEW: criterion breakdown rendering
    └── test_digest_render_v2.py                     # NEW: one-line summary rendering
```

---

## Task 1: Rule-based gates (`criteria.py`)

**Files:**
- Create: `src/call_monitor/ranking/criteria.py`
- Create: `tests/test_ranking_criteria.py`

Defines `evaluate_gates(profile, call, *, now) → Gates` returning structured `(timeline, budget, trl)` gate outputs. Each gate is `pass | fail | unknown`.

- [ ] **Step 1: Failing test `tests/test_ranking_criteria.py`**

```python
from datetime import UTC, datetime, timedelta
from dataclasses import dataclass

import pytest

from call_monitor.ranking.criteria import GateStatus, Gates, evaluate_gates


@dataclass
class _Profile:
    structured: dict
    budget_currency: str | None = None


@dataclass
class _Call:
    deadline: datetime | None = None
    budget_min: int | None = None
    budget_max: int | None = None
    currency: str | None = None
    structured: dict | None = None  # not used by gates; kept for parallel shape


_NOW = datetime(2026, 4, 17, 12, 0, tzinfo=UTC)


def _profile(**structured) -> _Profile:
    return _Profile(structured={
        "budget_min": None, "budget_max": None,
        "trl_min": None, "trl_max": None,
        **structured,
    })


# --- Timeline gate ---

def test_timeline_passes_when_deadline_is_15_days_away():
    g = evaluate_gates(_profile(), _Call(deadline=_NOW + timedelta(days=15)), now=_NOW)
    assert g.timeline.status == GateStatus.PASS


def test_timeline_fails_when_deadline_is_13_days_away():
    g = evaluate_gates(_profile(), _Call(deadline=_NOW + timedelta(days=13)), now=_NOW)
    assert g.timeline.status == GateStatus.FAIL
    assert g.timeline.reason == "too_soon"


def test_timeline_fails_when_deadline_passed():
    g = evaluate_gates(_profile(), _Call(deadline=_NOW - timedelta(days=1)), now=_NOW)
    assert g.timeline.status == GateStatus.FAIL
    assert g.timeline.reason == "expired"


def test_timeline_passes_when_deadline_is_none():
    g = evaluate_gates(_profile(), _Call(deadline=None), now=_NOW)
    assert g.timeline.status == GateStatus.PASS


# --- Budget gate ---

def test_budget_overlap_with_matching_currency():
    g = evaluate_gates(
        _Profile(structured={"budget_min": 100_000, "budget_max": 1_000_000}, budget_currency="SEK"),
        _Call(budget_min=500_000, budget_max=2_000_000, currency="SEK"),
        now=_NOW,
    )
    assert g.budget.status == GateStatus.PASS


def test_budget_no_overlap_with_matching_currency():
    g = evaluate_gates(
        _Profile(structured={"budget_min": 100_000, "budget_max": 500_000}, budget_currency="SEK"),
        _Call(budget_min=1_000_000, budget_max=2_000_000, currency="SEK"),
        now=_NOW,
    )
    assert g.budget.status == GateStatus.FAIL


def test_budget_unknown_when_currencies_differ():
    """Different currencies → unknown, NOT fail. Spec M2 fix."""
    g = evaluate_gates(
        _Profile(structured={"budget_min": 100_000, "budget_max": 500_000}, budget_currency="SEK"),
        _Call(budget_min=10_000, budget_max=50_000, currency="EUR"),
        now=_NOW,
    )
    assert g.budget.status == GateStatus.UNKNOWN
    assert g.budget.reason == "currency_mismatch"


def test_budget_unknown_when_profile_has_no_currency():
    g = evaluate_gates(
        _Profile(structured={"budget_min": 100_000, "budget_max": 500_000}, budget_currency=None),
        _Call(budget_min=200_000, budget_max=300_000, currency="SEK"),
        now=_NOW,
    )
    assert g.budget.status == GateStatus.UNKNOWN


def test_budget_unknown_when_either_side_has_no_range():
    g = evaluate_gates(
        _Profile(structured={"budget_min": None, "budget_max": None}, budget_currency="SEK"),
        _Call(budget_min=200_000, budget_max=300_000, currency="SEK"),
        now=_NOW,
    )
    assert g.budget.status == GateStatus.UNKNOWN


# --- TRL gate ---

def test_trl_overlap_passes():
    g = evaluate_gates(
        _profile(trl_min=4, trl_max=8),
        _Call(structured={"trl_min": 6, "trl_max": 9}),
        now=_NOW,
    )
    assert g.trl.status == GateStatus.PASS


def test_trl_disjoint_fails():
    g = evaluate_gates(
        _profile(trl_min=1, trl_max=3),
        _Call(structured={"trl_min": 7, "trl_max": 9}),
        now=_NOW,
    )
    assert g.trl.status == GateStatus.FAIL


def test_trl_unknown_when_either_side_missing():
    g = evaluate_gates(
        _profile(trl_min=4, trl_max=8),
        _Call(structured=None),
        now=_NOW,
    )
    assert g.trl.status == GateStatus.UNKNOWN
```

- [ ] **Step 2: Run — expect ImportError**

- [ ] **Step 3: Implement `src/call_monitor/ranking/criteria.py`**

```python
"""Rule-based gates for ranking v2.

Three deterministic checks (timeline, budget, TRL) evaluated in Python
before any LLM call. A FAIL on any gate short-circuits the LLM call and
yields aggregate_score=0 with the gate name as aggregate_reason.

Gates return UNKNOWN (not FAIL) when input data is missing — source
adapters often don't populate budget/TRL, and treating null as fail
would reject valid calls.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

TIMELINE_MIN_DAYS = 14


class GateStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class GateResult:
    status: GateStatus
    reason: str | None = None  # 'too_soon' | 'expired' | 'currency_mismatch' | None


@dataclass(frozen=True)
class Gates:
    timeline: GateResult
    budget: GateResult
    trl: GateResult


def evaluate_gates(profile, call, *, now: datetime) -> Gates:
    """Evaluate the three rule-based gates for a (profile, call) pair.

    `profile` and `call` are duck-typed: `profile` needs `structured` (dict)
    and `budget_currency`; `call` needs `deadline`, `budget_min`, `budget_max`,
    `currency`, and `structured` (dict-or-None for TRL fields).
    """
    return Gates(
        timeline=_timeline(call.deadline, now),
        budget=_budget(profile, call),
        trl=_trl(profile, call),
    )


def _timeline(deadline: datetime | None, now: datetime) -> GateResult:
    if deadline is None:
        return GateResult(GateStatus.PASS)
    if deadline <= now:
        return GateResult(GateStatus.FAIL, reason="expired")
    if deadline < now + timedelta(days=TIMELINE_MIN_DAYS):
        return GateResult(GateStatus.FAIL, reason="too_soon")
    return GateResult(GateStatus.PASS)


def _budget(profile, call) -> GateResult:
    p = profile.structured or {}
    p_min, p_max = p.get("budget_min"), p.get("budget_max")
    c_min, c_max = call.budget_min, call.budget_max
    if p_min is None or p_max is None or c_min is None or c_max is None:
        return GateResult(GateStatus.UNKNOWN)
    p_currency = (profile.budget_currency or "").upper() or None
    c_currency = (call.currency or "").upper() or None
    if p_currency is None or c_currency is None:
        return GateResult(GateStatus.UNKNOWN)
    if p_currency != c_currency:
        return GateResult(GateStatus.UNKNOWN, reason="currency_mismatch")
    # Range overlap: max(p_min, c_min) <= min(p_max, c_max)
    if max(p_min, c_min) <= min(p_max, c_max):
        return GateResult(GateStatus.PASS)
    return GateResult(GateStatus.FAIL)


def _trl(profile, call) -> GateResult:
    p = profile.structured or {}
    p_min, p_max = p.get("trl_min"), p.get("trl_max")
    c = call.structured or {}
    c_min, c_max = c.get("trl_min"), c.get("trl_max")
    if any(v is None for v in (p_min, p_max, c_min, c_max)):
        return GateResult(GateStatus.UNKNOWN)
    if max(p_min, c_min) <= min(p_max, c_max):
        return GateResult(GateStatus.PASS)
    return GateResult(GateStatus.FAIL)
```

Note on the TRL gate: `Call.structured` doesn't currently exist on the model — TRL fields are nowhere on Call in v1. For v2 we'd need to either add TRL fields to NormalizedCall + Call schema, or accept that TRL will always be UNKNOWN until the source adapter pipeline (Pre-LLM text follow-up spec) extracts them. **For Plan B we leave the TRL gate scaffolded but it returns UNKNOWN for all current data.** Document this limitation; tests use a duck-typed `_Call` with `structured` to verify the logic works once data arrives.

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/test_ranking_criteria.py -v   # 12 passed
uv run ruff check .
git add src/call_monitor/ranking/criteria.py tests/test_ranking_criteria.py
git commit -m "feat: ranking v2 rule-based gates (timeline/budget/TRL)"
```

---

## Task 2: OllamaClient logprob support

**Files:**
- Modify: `src/call_monitor/ranking/ollama_client.py` (accept `logprobs=True, top_logprobs=N`)
- Create: `tests/test_ranking_ollama_logprobs.py`

- [ ] **Step 1: Failing test `tests/test_ranking_ollama_logprobs.py`**

```python
import httpx
import pytest

from call_monitor.ranking.ollama_client import OllamaClient


def _mock(payload):
    def handler(request):
        body = request.read().decode("utf-8")
        # Validate request shape
        import json as _json
        req = _json.loads(body)
        assert req.get("logprobs") is True
        assert req.get("top_logprobs") == 6
        return httpx.Response(200, json=payload)
    return httpx.MockTransport(handler)


def test_chat_with_logprobs_returns_logprob_payload():
    """When logprobs=True, the OpenAI-shape response includes choices[0].logprobs.content."""
    body = {
        "model": "gemma4:e4b-it-q4_K_M",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": '{"topic_fit": {"rating": 4}}'},
            "logprobs": {
                "content": [
                    {"token": "4", "logprob": -0.1, "top_logprobs": [
                        {"token": "4", "logprob": -0.1},
                        {"token": "3", "logprob": -1.5},
                        {"token": "5", "logprob": -2.0},
                    ]},
                ]
            },
        }],
    }
    client = OllamaClient(
        base_url="http://x", model="gemma4:e4b-it-q4_K_M",
        transport=_mock(body),
    )
    envelope = client.chat(
        messages=[{"role": "user", "content": "x"}],
        logprobs=True, top_logprobs=6,
    )
    assert envelope["choices"][0]["logprobs"]["content"][0]["token"] == "4"


def test_chat_without_logprobs_omits_param():
    """Default behavior — no logprobs param sent — keeps backward compatibility."""
    captured = {}
    def handler(request):
        import json as _json
        captured["body"] = _json.loads(request.read().decode("utf-8"))
        return httpx.Response(200, json={
            "model": "gemma4:e4b-it-q4_K_M",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        })
    client = OllamaClient(
        base_url="http://x", model="gemma4:e4b-it-q4_K_M",
        transport=httpx.MockTransport(handler),
    )
    client.chat(messages=[{"role": "user", "content": "x"}])
    assert "logprobs" not in captured["body"]
    assert "top_logprobs" not in captured["body"]
```

- [ ] **Step 2: Run — expect TypeError**

- [ ] **Step 3: Update `src/call_monitor/ranking/ollama_client.py`**

Find the `chat` method. It currently builds a JSON body without logprob params. Add optional kwargs:

```python
    def chat(
        self,
        *,
        messages: list[dict],
        response_format: dict | None = None,
        logprobs: bool = False,
        top_logprobs: int | None = None,
    ) -> dict:
        body = {
            "model": self._model,
            "messages": messages,
            "temperature": 0,
        }
        if response_format is not None:
            body["response_format"] = response_format
        if logprobs:
            body["logprobs"] = True
            if top_logprobs is not None:
                body["top_logprobs"] = top_logprobs
        # ... rest of the existing method (httpx POST, raise_for_status, json)
```

Read the existing method to preserve the rest of its structure (URL, error handling, timeout). Insert the `logprobs` / `top_logprobs` block into the body construction without touching the I/O.

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/test_ranking_ollama_logprobs.py -v   # 2 passed
uv run pytest -v   # all green (existing tests pass — logprobs is opt-in)
uv run ruff check .
git add src/call_monitor/ranking/ollama_client.py tests/test_ranking_ollama_logprobs.py
git commit -m "feat: OllamaClient.chat accepts logprobs + top_logprobs params"
```

---

## Task 3: G-Eval logprob weighting + aggregate function

**Files:**
- Create: `src/call_monitor/ranking/aggregate.py`
- Create: `tests/test_ranking_aggregate.py`

`aggregate.py` exposes:
- `logprob_expected(content_logprobs) → float | None` — Σ p_i × int_i over the top-k logprobs that parse as integers in {0..5}; None if no usable logprobs.
- `aggregate(gates, llm) → AggregateResult` — final 0-100 score + reason, per the spec's aggregation table.

- [ ] **Step 1: Failing test `tests/test_ranking_aggregate.py`**

```python
from dataclasses import dataclass

import pytest

from call_monitor.ranking.aggregate import (
    AggregateResult,
    LLMResult,
    aggregate,
    logprob_expected,
)
from call_monitor.ranking.criteria import GateResult, GateStatus, Gates


# --- logprob_expected ---

def test_logprob_expected_basic():
    """Top-k logprobs with all integers — softmax, weighted sum."""
    # token "4" with logprob -0.1, "3" with -1.5, "5" with -2.0
    # softmax([-0.1, -1.5, -2.0]) ≈ [0.74, 0.18, 0.08]
    # E = 4*0.74 + 3*0.18 + 5*0.08 ≈ 3.90
    content = [{"token": "4", "logprob": -0.1, "top_logprobs": [
        {"token": "4", "logprob": -0.1},
        {"token": "3", "logprob": -1.5},
        {"token": "5", "logprob": -2.0},
    ]}]
    e = logprob_expected(content)
    assert e is not None
    assert 3.7 < e < 4.1  # near 3.9


def test_logprob_expected_filters_non_integer_tokens():
    """Tokens like ' ' or 'four' are skipped; only integer tokens contribute."""
    content = [{"token": "4", "logprob": -0.1, "top_logprobs": [
        {"token": "4", "logprob": -0.1},
        {"token": " ",  "logprob": -1.5},   # skipped
        {"token": "five", "logprob": -1.6}, # skipped
        {"token": "3", "logprob": -2.0},
    ]}]
    e = logprob_expected(content)
    assert e is not None
    assert 3.5 < e < 4.0


def test_logprob_expected_returns_none_when_no_integer_tokens():
    """No usable signal → None; caller falls back to argmax."""
    content = [{"token": "x", "logprob": -0.1, "top_logprobs": [
        {"token": "x", "logprob": -0.1},
        {"token": "y", "logprob": -1.5},
    ]}]
    assert logprob_expected(content) is None


def test_logprob_expected_handles_empty_or_malformed():
    assert logprob_expected([]) is None
    assert logprob_expected(None) is None


def test_logprob_expected_clamps_to_0_5_range():
    """If somehow integers outside [0,5] appear (e.g. '6'), filter them out."""
    content = [{"token": "4", "logprob": -0.1, "top_logprobs": [
        {"token": "4", "logprob": -0.1},
        {"token": "6", "logprob": -1.5},  # out of range, skipped
        {"token": "9", "logprob": -2.0},  # out of range, skipped
    ]}]
    e = logprob_expected(content)
    assert e is not None
    assert e == pytest.approx(4.0, rel=0.01)


# --- aggregate function ---

def _gates_pass():
    return Gates(
        timeline=GateResult(GateStatus.PASS),
        budget=GateResult(GateStatus.PASS),
        trl=GateResult(GateStatus.PASS),
    )


def _llm_clean(topic_expected=4.0, eligibility="pass", exclusions="pass"):
    return LLMResult(
        topic_expected=topic_expected,
        topic_argmax=int(round(topic_expected)),
        eligibility=eligibility,
        exclusions=exclusions,
    )


def test_aggregate_clean_pass_uses_topic_expected_times_20():
    r = aggregate(_gates_pass(), _llm_clean(topic_expected=4.0))
    assert r.score == 80
    assert r.reason is None


def test_aggregate_timeline_fail_short_circuits():
    g = Gates(
        timeline=GateResult(GateStatus.FAIL, reason="too_soon"),
        budget=GateResult(GateStatus.PASS),
        trl=GateResult(GateStatus.PASS),
    )
    r = aggregate(g, _llm_clean(topic_expected=4.5))
    assert r.score == 0
    assert r.reason == "timeline"


def test_aggregate_budget_fail_short_circuits():
    g = Gates(
        timeline=GateResult(GateStatus.PASS),
        budget=GateResult(GateStatus.FAIL),
        trl=GateResult(GateStatus.PASS),
    )
    r = aggregate(g, _llm_clean(topic_expected=4.5))
    assert r.score == 0
    assert r.reason == "budget"


def test_aggregate_trl_fail_short_circuits():
    g = Gates(
        timeline=GateResult(GateStatus.PASS),
        budget=GateResult(GateStatus.PASS),
        trl=GateResult(GateStatus.FAIL),
    )
    r = aggregate(g, _llm_clean(topic_expected=4.5))
    assert r.score == 0
    assert r.reason == "trl"


def test_aggregate_eligibility_fail_short_circuits():
    r = aggregate(_gates_pass(), _llm_clean(eligibility="fail"))
    assert r.score == 0
    assert r.reason == "eligibility"


def test_aggregate_eligibility_unclear_caps_at_50():
    r = aggregate(_gates_pass(), _llm_clean(topic_expected=4.5, eligibility="unclear"))
    # 4.5 * 20 = 90, capped to 50.
    assert r.score == 50
    assert r.reason is None


def test_aggregate_exclusions_fail_caps_low():
    """exclusions=fail → min(10, topic_expected*2)."""
    r = aggregate(_gates_pass(), _llm_clean(topic_expected=4.0, exclusions="fail"))
    # min(10, 4.0 * 2) = 8
    assert r.score == 8
    assert r.reason == "exclusion"


def test_aggregate_exclusions_fail_caps_at_10_for_high_topic():
    r = aggregate(_gates_pass(), _llm_clean(topic_expected=5.0, exclusions="fail"))
    # min(10, 5.0 * 2) = 10
    assert r.score == 10
    assert r.reason == "exclusion"


def test_aggregate_unknown_gates_do_not_short_circuit():
    """UNKNOWN ≠ FAIL — gates that can't be evaluated don't penalize."""
    g = Gates(
        timeline=GateResult(GateStatus.PASS),
        budget=GateResult(GateStatus.UNKNOWN),
        trl=GateResult(GateStatus.UNKNOWN),
    )
    r = aggregate(g, _llm_clean(topic_expected=4.0))
    assert r.score == 80
    assert r.reason is None
```

- [ ] **Step 2: Run — expect ImportError**

- [ ] **Step 3: Implement `src/call_monitor/ranking/aggregate.py`**

```python
"""G-Eval logprob weighting + ranking v2 aggregation."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from call_monitor.ranking.criteria import GateStatus, Gates

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMResult:
    """The parsed LLM rubric output, after logprob expansion.

    `topic_expected` is the G-Eval expected value (float in [0, 5]).
    `topic_argmax` is the argmax integer rating used as fallback.
    `eligibility` is 'pass' | 'fail' | 'unclear'.
    `exclusions` is 'pass' | 'fail'.
    """
    topic_expected: float
    topic_argmax: int
    eligibility: str
    exclusions: str


@dataclass(frozen=True)
class AggregateResult:
    score: int  # 0-100
    reason: str | None  # 'timeline'|'budget'|'trl'|'eligibility'|'exclusion'|None


def aggregate(gates: Gates, llm: LLMResult) -> AggregateResult:
    """Combine gates + LLM into a final score per the v2 spec's aggregation table."""
    # Rule-based gates short-circuit.
    if gates.timeline.status == GateStatus.FAIL:
        return AggregateResult(0, "timeline")
    if gates.budget.status == GateStatus.FAIL:
        return AggregateResult(0, "budget")
    if gates.trl.status == GateStatus.FAIL:
        return AggregateResult(0, "trl")

    # LLM gates short-circuit next.
    if llm.eligibility == "fail":
        return AggregateResult(0, "eligibility")

    if llm.exclusions == "fail":
        # Spec: min(10, topic_expected * 2). Matches v1's "≤ 10" behavior.
        return AggregateResult(min(10, int(llm.topic_expected * 2)), "exclusion")

    # Clean path: topic-expected × 20.
    base = int(round(llm.topic_expected * 20))
    if llm.eligibility == "unclear":
        base = min(base, 50)
    return AggregateResult(base, None)


def logprob_expected(content_logprobs: list[dict] | None) -> float | None:
    """G-Eval expected value from top-k logprobs at the rating-token position.

    `content_logprobs` is the OpenAI-shape `choices[0].logprobs.content` list.
    The rating token is assumed to be the first content token.

    Returns the expected value (float in [0, 5]) or None if no integer tokens
    in [0, 5] appear in the top-k. Caller falls back to argmax on None.
    """
    if not content_logprobs:
        return None
    first = content_logprobs[0]
    top = first.get("top_logprobs") if isinstance(first, dict) else None
    if not top:
        return None

    pairs: list[tuple[int, float]] = []
    for entry in top:
        if not isinstance(entry, dict):
            continue
        tok = entry.get("token")
        lp = entry.get("logprob")
        if not isinstance(tok, str) or not tok.strip().isdigit() or not isinstance(lp, (int, float)):
            continue
        n = int(tok.strip())
        if not 0 <= n <= 5:
            continue
        pairs.append((n, float(lp)))

    if not pairs:
        _logger.warning(
            "ranking.logprob_no_integer_tokens",
            extra={"event": "logprob_no_integer_tokens",
                   "tokens": [e.get("token") for e in top if isinstance(e, dict)]},
        )
        return None

    # Softmax-normalize.
    max_lp = max(lp for _, lp in pairs)
    exps = [(n, math.exp(lp - max_lp)) for n, lp in pairs]
    z = sum(x for _, x in exps)
    return sum(n * (x / z) for n, x in exps)
```

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/test_ranking_aggregate.py -v   # 14 passed
uv run pytest -v
uv run ruff check .
git add src/call_monitor/ranking/aggregate.py tests/test_ranking_aggregate.py
git commit -m "feat: ranking v2 aggregation + G-Eval logprob expected value"
```

---

## Task 4: Prompt builder + parser (`prompt.py` extension)

**Files:**
- Modify: `src/call_monitor/ranking/prompt.py` (add `build_messages_v2`, `parse_v2_response`, `PROMPT_VERSION_V2`)
- Create: `tests/test_ranking_prompt_v2.py`

- [ ] **Step 1: Failing test `tests/test_ranking_prompt_v2.py`**

```python
import json
import pytest

from call_monitor.ranking.prompt import (
    PROMPT_VERSION_V2,
    ParsedV2,
    ParseV2Error,
    build_messages_v2,
    parse_v2_response,
)


def test_prompt_version_v2_constant():
    assert PROMPT_VERSION_V2 == "v2.0"


def test_build_messages_v2_includes_profile_and_call_blocks():
    profile = type("P", (), {
        "name": "Pathology",
        "kind": type("K", (), {"value": "group"})(),
        "description": "Digital pathology AI",
        "structured": {
            "areas": ["digital pathology", "AI"],
            "hard_exclusions": ["animal studies"],
        },
    })()
    call = type("C", (), {
        "title": "AI for medical imaging",
        "summary": "Open call for AI-driven medical imaging research.",
        "programme": "Vinnova",
        "deadline": None,
        "budget_min": None,
        "budget_max": None,
        "currency": None,
        "raw": {},
    })()
    msgs = build_messages_v2(profile=profile, call=call)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    sys = msgs[0]["content"]
    assert "Pathology" in sys
    assert "digital pathology" in sys.lower()
    assert "animal studies" in sys
    user = msgs[1]["content"]
    assert "AI for medical imaging" in user
    assert "AI-driven medical imaging" in user


def test_build_messages_v2_truncates_call_text_at_4000():
    long_summary = "x" * 6000
    profile = type("P", (), {
        "name": "P", "kind": type("K", (), {"value": "group"})(),
        "description": "", "structured": {"areas": [], "hard_exclusions": []},
    })()
    call = type("C", (), {
        "title": "T", "summary": long_summary, "programme": None,
        "deadline": None, "budget_min": None, "budget_max": None, "currency": None,
        "raw": {},
    })()
    msgs = build_messages_v2(profile=profile, call=call)
    assert len([c for c in msgs[1]["content"] if c == "x"]) <= 4000


def test_parse_v2_clean_response():
    content = json.dumps({
        "topic_fit": {"rating": 4, "rationale": "good", "quoted_evidence": "AI for medical"},
        "eligibility": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
        "exclusions": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
    })
    out = parse_v2_response(content)
    assert isinstance(out, ParsedV2)
    assert out.topic_fit_rating == 4
    assert out.topic_fit_rationale == "good"
    assert out.topic_fit_quoted == "AI for medical"
    assert out.eligibility_rating == "pass"
    assert out.exclusions_rating == "pass"


def test_parse_v2_invalid_json_raises():
    with pytest.raises(ParseV2Error):
        parse_v2_response("not json")


def test_parse_v2_missing_topic_fit_raises():
    content = json.dumps({"eligibility": {"rating": "pass"}, "exclusions": {"rating": "pass"}})
    with pytest.raises(ParseV2Error, match="topic_fit"):
        parse_v2_response(content)


def test_parse_v2_invalid_topic_rating_raises():
    content = json.dumps({
        "topic_fit": {"rating": 9, "rationale": "x"},
        "eligibility": {"rating": "pass"},
        "exclusions": {"rating": "pass"},
    })
    with pytest.raises(ParseV2Error, match="topic_fit.rating"):
        parse_v2_response(content)


def test_parse_v2_invalid_eligibility_rating_raises():
    content = json.dumps({
        "topic_fit": {"rating": 3},
        "eligibility": {"rating": "maybe"},
        "exclusions": {"rating": "pass"},
    })
    with pytest.raises(ParseV2Error, match="eligibility"):
        parse_v2_response(content)
```

- [ ] **Step 2: Run — expect ImportError**

- [ ] **Step 3: Extend `src/call_monitor/ranking/prompt.py`**

Append to the existing prompt.py (which already has v1 builder + PROMPT_VERSION='v1'):

```python
from __future__ import annotations

import json
from dataclasses import dataclass

PROMPT_VERSION_V2 = "v2.0"
CALL_TEXT_LIMIT_V2 = 4000


class ParseV2Error(Exception):
    """The LLM response didn't match the v2 schema."""


@dataclass(frozen=True)
class ParsedV2:
    topic_fit_rating: int
    topic_fit_rationale: str
    topic_fit_quoted: str | None
    eligibility_rating: str  # 'pass' | 'fail' | 'unclear'
    eligibility_rationale: str
    eligibility_quoted: str | None
    exclusions_rating: str   # 'pass' | 'fail'
    exclusions_rationale: str
    exclusions_quoted: str | None


_SYSTEM_TEMPLATE_V2 = """\
You are a research-funding analyst for an academic hospital.
Evaluate a funding call against this profile on three criteria.

PROFILE:
  Name: {name}
  Kind: {kind}
  Description: {description}
  Research areas: {areas}
  Hard exclusions: {hard_exclusions}

CRITERIA:
  1. topic_fit (integer 0-5): how well the call's scope aligns with
     the profile's research areas. 5 = direct match on a named area;
     3 = adjacent/applicable; 1 = tangential; 0 = unrelated.
  2. eligibility (pass | fail | unclear): whether a profile of this
     kind would be eligible to apply. fail = clearly ineligible based
     on a stated requirement. unclear = cannot determine from the text.
  3. exclusions (pass | fail): whether the call violates any of the
     profile's hard exclusions.

For topic_fit and for "fail" judgments on eligibility/exclusions,
quote the single sentence from the CALL that most supports your
judgment (verbatim, max 200 chars). If no such sentence exists for
eligibility or exclusions, set quoted_evidence to null.

Output JSON exactly matching this schema:
{{
  "topic_fit":   {{"rating": int, "rationale": str, "quoted_evidence": str}},
  "eligibility": {{"rating": "pass" | "fail" | "unclear",
                   "rationale": str, "quoted_evidence": str | null}},
  "exclusions":  {{"rating": "pass" | "fail",
                   "rationale": str, "quoted_evidence": str | null}}
}}
"""

_USER_TEMPLATE_V2 = """\
CALL:
  Title:     {title}
  Programme: {programme}
  Deadline:  {deadline}
  Budget:    {budget_min}-{budget_max} {currency}
  Text:      {text}
"""


def build_messages_v2(*, profile, call) -> list[dict]:
    """Build the v2 criterion-rubric prompt. Profile block first so the
    Ollama prefix cache is reused across calls scored against the same
    profile."""
    structured = profile.structured or {}
    sys = _SYSTEM_TEMPLATE_V2.format(
        name=profile.name,
        kind=profile.kind.value if hasattr(profile.kind, "value") else str(profile.kind),
        description=profile.description or "",
        areas=", ".join(structured.get("areas") or []) or "(none)",
        hard_exclusions=", ".join(structured.get("hard_exclusions") or []) or "(none)",
    )
    text_source = call.summary or ""
    if len(text_source) > CALL_TEXT_LIMIT_V2:
        text_source = text_source[:CALL_TEXT_LIMIT_V2]
    user = _USER_TEMPLATE_V2.format(
        title=call.title or "",
        programme=call.programme or "",
        deadline=call.deadline.isoformat() if call.deadline else "(none)",
        budget_min=call.budget_min if call.budget_min is not None else "?",
        budget_max=call.budget_max if call.budget_max is not None else "?",
        currency=call.currency or "",
        text=text_source or "(no text)",
    )
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": user},
    ]


def parse_v2_response(content: str) -> ParsedV2:
    """Parse the LLM's JSON criterion output. Raises ParseV2Error on shape
    violations."""
    try:
        data = json.loads(content)
    except ValueError as e:
        raise ParseV2Error(f"invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ParseV2Error("response is not a JSON object")

    tf = data.get("topic_fit")
    if not isinstance(tf, dict):
        raise ParseV2Error("topic_fit missing or not an object")
    rating = tf.get("rating")
    if isinstance(rating, bool) or not isinstance(rating, int) or not 0 <= rating <= 5:
        raise ParseV2Error(f"topic_fit.rating must be int 0-5; got {rating!r}")

    el = data.get("eligibility")
    if not isinstance(el, dict):
        raise ParseV2Error("eligibility missing or not an object")
    el_rating = el.get("rating")
    if el_rating not in {"pass", "fail", "unclear"}:
        raise ParseV2Error(f"eligibility.rating must be 'pass'|'fail'|'unclear'; got {el_rating!r}")

    ex = data.get("exclusions")
    if not isinstance(ex, dict):
        raise ParseV2Error("exclusions missing or not an object")
    ex_rating = ex.get("rating")
    if ex_rating not in {"pass", "fail"}:
        raise ParseV2Error(f"exclusions.rating must be 'pass'|'fail'; got {ex_rating!r}")

    return ParsedV2(
        topic_fit_rating=rating,
        topic_fit_rationale=str(tf.get("rationale") or ""),
        topic_fit_quoted=str(tf["quoted_evidence"]) if tf.get("quoted_evidence") else None,
        eligibility_rating=el_rating,
        eligibility_rationale=str(el.get("rationale") or ""),
        eligibility_quoted=str(el["quoted_evidence"]) if el.get("quoted_evidence") else None,
        exclusions_rating=ex_rating,
        exclusions_rationale=str(ex.get("rationale") or ""),
        exclusions_quoted=str(ex["quoted_evidence"]) if ex.get("quoted_evidence") else None,
    )
```

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/test_ranking_prompt_v2.py -v   # 7 passed
uv run pytest -v
uv run ruff check .
git add src/call_monitor/ranking/prompt.py tests/test_ranking_prompt_v2.py
git commit -m "feat: ranking v2 prompt builder + JSON parser"
```

---

## Task 5: `rank_pair_v2` orchestration

**Files:**
- Modify: `src/call_monitor/ranking/rank_pair.py` (add `RankResultV2`, `rank_pair_v2`)
- Create: `tests/test_ranking_rank_pair_v2.py`

`rank_pair_v2(*, client, profile, call, now) → RankResultV2` runs gates → calls Ollama with logprobs → parses → expands logprob → aggregates.

- [ ] **Step 1: Failing test `tests/test_ranking_rank_pair_v2.py`**

```python
import json
from datetime import UTC, datetime
from dataclasses import dataclass

import httpx
import pytest

from call_monitor.ranking.rank_pair import RankResultV2, rank_pair_v2
from call_monitor.ranking.ollama_client import OllamaClient


_NOW = datetime(2026, 4, 17, 12, 0, tzinfo=UTC)


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


def test_rank_pair_v2_clean_pass_with_logprobs():
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
    r = rank_pair_v2(client=client, profile=profile, call=call, now=_NOW)
    assert isinstance(r, RankResultV2)
    # 4.0 (essentially argmax with strong probability) × 20 = 80
    assert 75 <= r.score <= 85
    assert r.reason is None
    assert r.parsed.eligibility_rating == "pass"
    assert r.gates.timeline.status.value == "pass"


def test_rank_pair_v2_short_circuits_on_gate_fail():
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
    r = rank_pair_v2(client=client, profile=profile, call=call, now=_NOW)
    assert r.score == 0
    assert r.reason == "timeline"
    assert r.parsed is None  # no LLM call → no parsed response


def test_rank_pair_v2_eligibility_fail_zeros_score():
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    client = _ollama_mock(content={
        "topic_fit": {"rating": 4, "rationale": "x"},
        "eligibility": {"rating": "fail", "rationale": "company-only"},
        "exclusions": {"rating": "pass", "rationale": "x"},
    })
    r = rank_pair_v2(client=client, profile=profile, call=call, now=_NOW)
    assert r.score == 0
    assert r.reason == "eligibility"


def test_rank_pair_v2_logprob_unavailable_falls_back_to_argmax():
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    client = _ollama_mock(
        content={
            "topic_fit": {"rating": 3, "rationale": "x"},
            "eligibility": {"rating": "pass"},
            "exclusions": {"rating": "pass"},
        },
        logprobs=None,  # Ollama edge case: no logprobs
    )
    r = rank_pair_v2(client=client, profile=profile, call=call, now=_NOW)
    # argmax 3 × 20 = 60
    assert r.score == 60
    assert r.reason is None
    assert r.logprob_fallback is True
```

- [ ] **Step 2: Run — expect ImportError on `RankResultV2` / `rank_pair_v2`**

- [ ] **Step 3: Extend `src/call_monitor/ranking/rank_pair.py`**

Append to the existing rank_pair.py:

```python
from datetime import datetime
from dataclasses import dataclass

from call_monitor.ranking.aggregate import (
    AggregateResult,
    LLMResult,
    aggregate,
    logprob_expected,
)
from call_monitor.ranking.criteria import GateStatus, Gates, evaluate_gates
from call_monitor.ranking.ollama_client import LLMError, OllamaClient
from call_monitor.ranking.prompt import (
    PROMPT_VERSION_V2,
    ParsedV2,
    ParseV2Error,
    build_messages_v2,
    parse_v2_response,
)


@dataclass(frozen=True)
class RankResultV2:
    """Output of rank_pair_v2 — richer than v1's RankResult so the worker
    can write criteria rows without re-parsing."""
    score: int
    reason: str | None
    gates: Gates
    parsed: ParsedV2 | None  # None if a gate short-circuited before LLM call
    topic_expected: float | None  # G-Eval expected; None if logprobs unavailable
    logprob_fallback: bool         # True iff we fell back to argmax
    model: str
    prompt_version: str
    raw_response: dict             # full LLM envelope (for debugging) or {} on short-circuit


def rank_pair_v2(
    *,
    client: OllamaClient,
    profile,
    call,
    now: datetime,
) -> RankResultV2:
    """Run gates → LLM → parse → aggregate. Returns a structured RankResultV2.

    On rule-based-gate FAIL, short-circuits without an LLM call — UNLESS
    the call matches one of the profile's must_include keywords, in which
    case the gates are evaluated for diagnostic display but cannot
    short-circuit the pair (spec line 282: must-include is the user's
    explicit force-include and overrides the rule-based safety nets).
    The LLM still evaluates eligibility/exclusions/topic_fit normally.

    On LLM parse failure, raises ParseV2Error (caller decides retry /
    dead-letter). On Ollama failure, raises LLMError.
    """
    gates = evaluate_gates(profile, call, now=now)
    must_include_match = _call_matches_must_include(profile, call)
    if not must_include_match:
        if gates.timeline.status == GateStatus.FAIL:
            return _gate_fail_result(gates, "timeline")
        if gates.budget.status == GateStatus.FAIL:
            return _gate_fail_result(gates, "budget")
        if gates.trl.status == GateStatus.FAIL:
            return _gate_fail_result(gates, "trl")

    messages = build_messages_v2(profile=profile, call=call)
    envelope = client.chat(
        messages=messages,
        response_format={"type": "json_object"},
        logprobs=True,
        top_logprobs=6,
    )
    content = envelope["choices"][0]["message"]["content"]
    parsed = parse_v2_response(content)
    content_lp = (envelope["choices"][0].get("logprobs") or {}).get("content")
    expected = logprob_expected(content_lp)
    logprob_fallback = expected is None
    if expected is None:
        expected = float(parsed.topic_fit_rating)

    llm = LLMResult(
        topic_expected=expected,
        topic_argmax=parsed.topic_fit_rating,
        eligibility=parsed.eligibility_rating,
        exclusions=parsed.exclusions_rating,
    )
    agg: AggregateResult = aggregate(gates, llm)
    return RankResultV2(
        score=agg.score,
        reason=agg.reason,
        gates=gates,
        parsed=parsed,
        topic_expected=expected,
        logprob_fallback=logprob_fallback,
        model=client.model,
        prompt_version=PROMPT_VERSION_V2,
        raw_response=envelope,
    )


def _gate_fail_result(gates: Gates, reason: str) -> RankResultV2:
    return RankResultV2(
        score=0,
        reason=reason,
        gates=gates,
        parsed=None,
        topic_expected=None,
        logprob_fallback=False,
        model="",
        prompt_version=PROMPT_VERSION_V2,
        raw_response={},
    )


def _call_matches_must_include(profile, call) -> bool:
    """Spec line 282: must-include calls bypass the rule-based gates.

    Case-insensitive substring match against title + summary + keywords,
    same logic as the digest must-include bucket (assemble.py)."""
    structured = profile.structured or {}
    must_terms = [
        t.strip().lower()
        for t in (structured.get("must_include") or [])
        if t and t.strip()
    ]
    if not must_terms:
        return False
    haystack = " ".join(filter(None, [
        (call.title or "").lower(),
        (call.summary or "").lower(),
        " ".join((call.keywords or [])).lower() if hasattr(call, "keywords") else "",
    ]))
    return any(t in haystack for t in must_terms)
```

Add a regression test case in `tests/test_ranking_rank_pair_v2.py` for this behavior:

```python
def test_rank_pair_v2_must_include_bypasses_timeline_gate():
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
    r = rank_pair_v2(client=client, profile=profile, call=call, now=_NOW)
    # Bypassed timeline → got an LLM score, not 0.
    assert r.score >= 60
    assert r.reason is None
    # Gates were still evaluated (for diagnostic display).
    assert r.gates.timeline.status.value == "fail"


def test_rank_pair_v2_must_include_match_requires_term_in_text():
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
    import httpx
    bad_client = OllamaClient(
        base_url="http://x", model="m",
        transport=httpx.MockTransport(boom),
    )
    r = rank_pair_v2(client=profile and bad_client, profile=profile, call=call, now=_NOW)
    assert r.score == 0
    assert r.reason == "timeline"
```

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/test_ranking_rank_pair_v2.py -v   # 4 passed
uv run pytest -v
uv run ruff check .
git add src/call_monitor/ranking/rank_pair.py tests/test_ranking_rank_pair_v2.py
git commit -m "feat: rank_pair_v2 — gates + LLM + G-Eval orchestration"
```

---

## Task 6: Worker dispatches v2 + writes sparse criteria rows

**Files:**
- Modify: `src/call_monitor/ranking/worker.py` (replace Plan A's `NotImplementedError` with real v2 dispatch; write `CallRankingCriterion` rows)
- Create: `tests/test_ranking_worker_v2.py`

- [ ] **Step 1: Failing test `tests/test_ranking_worker_v2.py`**

```python
from datetime import UTC, datetime
import json
from unittest.mock import patch

import httpx
from sqlalchemy import select

from call_monitor.db.models import (
    Call,
    CallRanking,
    CallRankingCriterion,
    Profile,
    ProfileKind,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
)
from call_monitor.ranking.ollama_client import OllamaClient
from call_monitor.ranking.worker import drain_queue


def _seed(s, *, scheme="v2"):
    u = User(email=f"u-{scheme}@ki.se")
    s.add(u); s.flush()
    p = Profile(
        name=f"P-{scheme}", kind=ProfileKind.GROUP, description="",
        structured={"areas": ["AI"], "hard_exclusions": []},
        created_by=u.id, ranking_scheme=scheme,
    )
    s.add(p); s.flush()
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(
        source_id=src.id, external_id=f"E-{scheme}", url="https://x", title="AI call",
        summary="Open AI funding call.", deadline=datetime(2099, 12, 31, tzinfo=UTC),
        published_at=datetime.now(UTC),
    )
    s.add(c); s.flush()
    s.add(RankingJob(
        call_id=c.id, profile_id=p.id, scheme=scheme,
        status=RankingJobStatus.PENDING,
    ))
    s.flush()
    return c.id, p.id


def _ollama_v2_clean():
    body = {
        "model": "gemma4:e4b-it-q4_K_M",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": json.dumps({
                "topic_fit": {"rating": 4, "rationale": "AI match", "quoted_evidence": "AI"},
                "eligibility": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
                "exclusions": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
            })},
            "logprobs": {"content": [
                {"token": "4", "logprob": -0.1, "top_logprobs": [
                    {"token": "4", "logprob": -0.1},
                    {"token": "3", "logprob": -2.0},
                ]},
            ]},
        }],
    }
    def handler(request):
        return httpx.Response(200, json=body)
    return OllamaClient(
        base_url="http://x", model="gemma4:e4b-it-q4_K_M",
        transport=httpx.MockTransport(handler),
    )


def test_worker_v2_writes_call_ranking_with_aggregate_and_reason(pg_session_factory):
    with pg_session_factory() as s:
        cid, pid = _seed(s, scheme="v2")
        s.commit()

    client = _ollama_v2_clean()
    drain_queue(session_factory=pg_session_factory, client=client, batch_size=10)

    with pg_session_factory() as s:
        r = s.execute(select(CallRanking).where(CallRanking.scheme == "v2")).scalar_one()
    # 4 × 20 = 80 (give or take 1 from logprob expected ≈ 4.0)
    assert 75 <= r.aggregate_score <= 85
    assert r.aggregate_reason is None
    assert r.scheme == "v2"
    assert r.prompt_version == "v2.0"


def test_worker_v2_writes_sparse_criterion_rows_clean_path(pg_session_factory):
    """Clean pass — all 3 LLM criteria + 3 gate criteria get rows? Or only the LLM ones?
    Spec: only criteria 'actually evaluated' get rows. PASSes count as evaluated."""
    with pg_session_factory() as s:
        cid, pid = _seed(s, scheme="v2")
        s.commit()

    drain_queue(session_factory=pg_session_factory, client=_ollama_v2_clean(), batch_size=10)

    with pg_session_factory() as s:
        criteria = s.execute(select(CallRankingCriterion)).scalars().all()
    by_name = {c.criterion: c for c in criteria}
    # All 3 LLM criteria + 3 gates that ran are written.
    assert "topic_fit" in by_name
    assert by_name["topic_fit"].rating == "4"
    assert by_name["topic_fit"].rating_expected is not None
    assert "eligibility" in by_name
    assert by_name["eligibility"].rating == "pass"
    assert "exclusions" in by_name
    assert by_name["exclusions"].rating == "pass"
    assert "timeline" in by_name
    assert by_name["timeline"].rating == "pass"


def test_worker_v2_short_circuit_writes_only_failing_gate_row(pg_session_factory):
    """If timeline fails, only timeline criterion row is written. No LLM row."""
    with pg_session_factory() as s:
        u = User(email="sc@ki.se"); s.add(u); s.flush()
        p = Profile(
            name="SC", kind=ProfileKind.GROUP, description="",
            structured={"areas": [], "hard_exclusions": []},
            created_by=u.id, ranking_scheme="v2",
        )
        s.add(p); s.flush()
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        # Deadline tomorrow → timeline FAIL.
        from datetime import timedelta
        c = Call(
            source_id=src.id, external_id="SC", url="https://x", title="T",
            deadline=datetime.now(UTC) + timedelta(days=1),
            published_at=datetime.now(UTC),
        )
        s.add(c); s.flush()
        s.add(RankingJob(
            call_id=c.id, profile_id=p.id, scheme="v2",
            status=RankingJobStatus.PENDING,
        ))
        s.commit()

    # Client that would fail if called.
    def boom(request):
        raise AssertionError("LLM called for gate-failed pair")
    client = OllamaClient(
        base_url="http://x", model="m",
        transport=httpx.MockTransport(boom),
    )
    drain_queue(session_factory=pg_session_factory, client=client, batch_size=10)

    with pg_session_factory() as s:
        r = s.execute(select(CallRanking)).scalar_one()
        criteria = s.execute(select(CallRankingCriterion)).scalars().all()
    assert r.aggregate_score == 0
    assert r.aggregate_reason == "timeline"
    by_name = {c.criterion: c for c in criteria}
    assert set(by_name.keys()) == {"timeline"}
    assert by_name["timeline"].rating == "fail"
```

- [ ] **Step 2: Run — expect failure (worker still raises NotImplementedError on v2)**

- [ ] **Step 3: Update `src/call_monitor/ranking/worker.py`**

Find Plan A's `NotImplementedError` block. Replace it with real v2 dispatch.

Add imports at the top:

```python
from call_monitor.ranking.criteria import GateStatus
from call_monitor.ranking.rank_pair import RankResultV2, rank_pair_v2
from call_monitor.db.models import CallRankingCriterion
```

Inside the per-job loop, replace the `if job.scheme != "v1":` block with:

```python
        if job.scheme == "v2":
            try:
                v2_result = rank_pair_v2(
                    client=client,
                    profile=profile,
                    call=call,
                    now=datetime.now(UTC),
                )
            except (LLMError, ParseV2Error) as e:
                mark_failed(
                    session, job_id=job.id,
                    error=f"v2 ranking failed: {e}",
                    max_attempts=MAX_ATTEMPTS,
                )
                continue
            session.merge(CallRanking(
                call_id=job.call_id,
                profile_id=job.profile_id,
                scheme="v2",
                aggregate_score=v2_result.score,
                aggregate_reason=v2_result.reason,
                rationale=_v2_summary(v2_result),
                model=v2_result.model,
                prompt_version=v2_result.prompt_version,
                raw_response=v2_result.raw_response or {},
                ranked_at=datetime.now(UTC),
            ))
            _write_v2_criteria_rows(session, job, v2_result)
            mark_done(session, job_id=job.id)
            continue
        # ... existing v1 path follows
```

Add helpers at module scope:

```python
def _v2_summary(r: RankResultV2) -> str:
    """One-line summary for the rationale column (v2's free-form text is
    the per-criterion rationales, which live in CallRankingCriterion)."""
    if r.parsed is None:
        return f"Short-circuited: {r.reason}"
    return (
        f"Topic {r.parsed.topic_fit_rating}/5 · "
        f"Eligibility: {r.parsed.eligibility_rating} · "
        f"Exclusions: {r.parsed.exclusions_rating}"
    )


def _write_v2_criteria_rows(session, job, r: RankResultV2) -> None:
    """Write sparse CallRankingCriterion rows. Only the criteria actually
    evaluated up to (and including) the failing gate are inserted. Plan A's
    schema makes the table accept inserts; this function is the only writer."""
    now = datetime.now(UTC)

    # Gate rows — write up to and including the failing one (or all if no gate failed).
    gate_results = [
        ("timeline", r.gates.timeline),
        ("budget",   r.gates.budget),
        ("trl",      r.gates.trl),
    ]
    for name, gr in gate_results:
        # Use upsert to be re-rank-safe.
        session.merge(CallRankingCriterion(
            call_id=job.call_id, profile_id=job.profile_id, scheme="v2",
            criterion=name,
            rating=gr.status.value,
            rating_expected=None,
            rationale=gr.reason,  # 'too_soon' / 'expired' / 'currency_mismatch' / None
            quoted_evidence=None,
            ranked_at=now,
        ))
        if gr.status == GateStatus.FAIL:
            return  # stop — subsequent criteria not evaluated

    # LLM rows — only if we got that far (parsed is not None).
    if r.parsed is None:
        return
    session.merge(CallRankingCriterion(
        call_id=job.call_id, profile_id=job.profile_id, scheme="v2",
        criterion="topic_fit",
        rating=str(r.parsed.topic_fit_rating),
        rating_expected=r.topic_expected,
        rationale=r.parsed.topic_fit_rationale,
        quoted_evidence=r.parsed.topic_fit_quoted,
        ranked_at=now,
    ))
    session.merge(CallRankingCriterion(
        call_id=job.call_id, profile_id=job.profile_id, scheme="v2",
        criterion="eligibility",
        rating=r.parsed.eligibility_rating,
        rating_expected=None,
        rationale=r.parsed.eligibility_rationale,
        quoted_evidence=r.parsed.eligibility_quoted,
        ranked_at=now,
    ))
    session.merge(CallRankingCriterion(
        call_id=job.call_id, profile_id=job.profile_id, scheme="v2",
        criterion="exclusions",
        rating=r.parsed.exclusions_rating,
        rating_expected=None,
        rationale=r.parsed.exclusions_rationale,
        quoted_evidence=r.parsed.exclusions_quoted,
        ranked_at=now,
    ))
```

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/test_ranking_worker_v2.py -v   # 3 passed
uv run pytest -v
uv run ruff check .
git add src/call_monitor/ranking/worker.py tests/test_ranking_worker_v2.py
git commit -m "feat: worker runs rank_pair_v2 + writes sparse criteria rows"
```

---

## Task 7: Call detail page renders v2 criterion breakdown

**Files:**
- Modify: `src/call_monitor/web/routes/calls.py` (call detail context: load criteria rows for v2)
- Modify: `src/call_monitor/web/templates/call_detail.html` (v2 breakdown block)
- Create: `tests/test_web_call_detail_v2.py`

- [ ] **Step 1: Failing test `tests/test_web_call_detail_v2.py`**

```python
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import (
    Call,
    CallRanking,
    CallRankingCriterion,
    Profile,
    Source,
    User,
)
from call_monitor.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _logged_in(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="cd@ki.se"); s.add(u); s.commit(); s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_call_detail_renders_v2_criterion_breakdown(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    pid, _ = profile_factory(name="V2 detail")
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        p.ranking_scheme = "v2"
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id, external_id="DETAIL", url="https://x", title="DetailCall",
            published_at=datetime.now(UTC),
        )
        s.add(c); s.flush()
        s.add(CallRanking(
            call_id=c.id, profile_id=pid, scheme="v2",
            aggregate_score=82, aggregate_reason=None,
            rationale="Topic 4/5 · Eligibility: pass · Exclusions: pass",
            model="m", prompt_version="v2.0", ranked_at=datetime.now(UTC),
        ))
        s.add_all([
            CallRankingCriterion(
                call_id=c.id, profile_id=pid, scheme="v2",
                criterion="topic_fit", rating="4", rating_expected=4.1,
                rationale="strong AI match", quoted_evidence="AI funding call",
                ranked_at=datetime.now(UTC),
            ),
            CallRankingCriterion(
                call_id=c.id, profile_id=pid, scheme="v2",
                criterion="eligibility", rating="pass",
                rationale="open to research groups", quoted_evidence=None,
                ranked_at=datetime.now(UTC),
            ),
            CallRankingCriterion(
                call_id=c.id, profile_id=pid, scheme="v2",
                criterion="exclusions", rating="pass",
                rationale="no exclusions hit", quoted_evidence=None,
                ranked_at=datetime.now(UTC),
            ),
        ])
        s.commit()
        cid = c.id

    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get(f"/calls/{cid}?profile={pid}")
    assert r.status_code == 200
    # Aggregate is still primary
    assert "82" in r.text
    # Each criterion rating is rendered
    assert "topic" in r.text.lower()
    assert "4/5" in r.text or ">4<" in r.text
    assert "strong AI match" in r.text
    assert "AI funding call" in r.text  # quoted evidence
```

- [ ] **Step 2: Run — expect failure (template doesn't render breakdown)**

- [ ] **Step 3: Update `src/call_monitor/web/routes/calls.py`**

Find `call_detail` handler. The existing context provides `call` and the active `ranking` row. Add criterion rows when scheme is v2.

```python
@router.get("/calls/{call_id}", response_class=HTMLResponse)
def call_detail(
    request: Request,
    call_id: int,
    profile: int | None = None,
    user: User = Depends(get_current_user),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        call_obj = session.get(Call, call_id)
        if call_obj is None:
            raise HTTPException(status_code=404, detail="call not found")
        ranking = None
        criteria = []
        if profile is not None:
            p = session.get(Profile, profile)
            if p is not None and p.is_active:
                ranking = session.execute(
                    select(CallRanking).where(
                        CallRanking.call_id == call_id,
                        CallRanking.profile_id == profile,
                        CallRanking.scheme == p.ranking_scheme,
                    )
                ).scalar_one_or_none()
                if ranking is not None and ranking.scheme == "v2":
                    criteria = session.execute(
                        select(CallRankingCriterion).where(
                            CallRankingCriterion.call_id == call_id,
                            CallRankingCriterion.profile_id == profile,
                            CallRankingCriterion.scheme == "v2",
                        )
                    ).scalars().all()
        session.expunge_all()

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="call_detail.html",
        context={"call": call_obj, "ranking": ranking, "criteria": criteria, "user": user},
    )
```

The exact handler shape may differ — adapt to the existing structure.

- [ ] **Step 4: Update `src/call_monitor/web/templates/call_detail.html`**

After the existing aggregate display (where `ranking.score` / `ranking.aggregate_score` is rendered), add a v2 breakdown block:

```html
{% if ranking and ranking.scheme == 'v2' and criteria %}
  <h2 style="margin-top: 1.5em;">v2 criterion breakdown</h2>
  <dl>
    {% for c in criteria %}
      <dt>{{ c.criterion }}</dt>
      <dd>
        <strong>{% if c.criterion == 'topic_fit' %}{{ c.rating }}/5{% else %}{{ c.rating }}{% endif %}</strong>
        {% if c.rating_expected is not none and c.criterion == 'topic_fit' %}
          <small>(expected {{ '%.2f' % c.rating_expected }})</small>
        {% endif %}
        {% if c.rationale %}<div>{{ c.rationale }}</div>{% endif %}
        {% if c.quoted_evidence %}<blockquote style="margin: 0.3em 0 0 1em; color: #555; font-style: italic;">"{{ c.quoted_evidence }}"</blockquote>{% endif %}
      </dd>
    {% endfor %}
  </dl>
{% endif %}
```

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest tests/test_web_call_detail_v2.py -v
uv run pytest -v
uv run ruff check .
git add src/call_monitor/web/routes/calls.py src/call_monitor/web/templates/call_detail.html tests/test_web_call_detail_v2.py
git commit -m "feat: call detail renders v2 criterion breakdown"
```

---

## Task 8: Digest one-line criterion summary on v2 entries

**Files:**
- Modify: `src/call_monitor/digest/render.py` (already passes the rationale; for v2 entries the rationale is the one-line summary, which `_v2_summary` in the worker already produces)
- Modify: `src/call_monitor/digest/templates/digest.html.jinja` and `digest.txt.jinja` (no change required — entry.rationale renders as-is)
- Create: `tests/test_digest_render_v2.py`

The worker's `_v2_summary` (Task 6) already writes the one-line "Topic 4/5 · Eligibility: pass · Exclusions: pass" string into `CallRanking.rationale`. The digest assembler reads `CallRanking.rationale` into `DigestCallEntry.rationale`. Templates already render rationale next to score. So this task is verification + a regression test.

- [ ] **Step 1: Test `tests/test_digest_render_v2.py`**

```python
from datetime import UTC, datetime, timedelta

from call_monitor.digest.assemble import (
    DigestCallEntry,
    DigestPayload,
    DigestProfileSection,
)
from call_monitor.digest.render import render_html, render_text


def test_digest_html_shows_v2_one_line_summary_in_rationale_slot():
    """A v2 ranking entry's rationale is the criterion summary; templates
    render it next to the score without needing a v2-specific branch."""
    now = datetime(2026, 4, 17, tzinfo=UTC)
    entry = DigestCallEntry(
        call_id=1, title="V2 Call",
        summary="x",
        deadline=now + timedelta(days=10),
        score=80, rationale="Topic 4/5 · Eligibility: pass · Exclusions: pass",
        url="http://app/calls/1", bucket="top", also_in_profiles=[],
    )
    section = DigestProfileSection(profile_id=1, profile_name="P", entries=[entry])
    payload = DigestPayload(
        user_id=1, user_email="u@ki.se", sections=[section], total_calls=1,
    )
    html = render_html(payload, dashboard_url="http://app/")
    assert "Topic 4/5" in html
    assert "Eligibility: pass" in html


def test_digest_text_shows_v2_one_line_summary():
    now = datetime(2026, 4, 17, tzinfo=UTC)
    entry = DigestCallEntry(
        call_id=1, title="V2 Call",
        summary="x", deadline=None,
        score=80, rationale="Topic 5/5 · Eligibility: pass · Exclusions: pass",
        url="http://app/calls/1", bucket="top", also_in_profiles=[],
    )
    section = DigestProfileSection(profile_id=1, profile_name="P", entries=[entry])
    payload = DigestPayload(
        user_id=1, user_email="u@ki.se", sections=[section], total_calls=1,
    )
    text = render_text(payload, dashboard_url="http://app/")
    assert "Topic 5/5" in text
```

- [ ] **Step 2: Run — these should already pass since Task 6's worker writes the right rationale and templates already render it**

```bash
uv run pytest tests/test_digest_render_v2.py -v
```

If they fail, the existing template's rationale rendering needs the `e.rationale` reference — check `digest.html.jinja` and `digest.txt.jinja`. If the template strips or transforms the rationale, fix it.

- [ ] **Step 3: Commit**

```bash
git add tests/test_digest_render_v2.py
git commit -m "test: v2 one-line criterion summary survives digest rendering"
```

---

## Task 9: End-to-end test (v2 ranking against fake LLM)

**Files:**
- Create: `tests/test_ranking_v2_e2e.py`

Exercises the full path: enqueue v2 job → drain queue with fake Ollama → verify CallRanking + CallRankingCriterion rows + dashboard rendering.

- [ ] **Step 1: Test `tests/test_ranking_v2_e2e.py`**

```python
import json
from datetime import UTC, datetime
from unittest.mock import patch

import httpx
from sqlalchemy import select

from call_monitor.db.models import (
    Call,
    CallRanking,
    CallRankingCriterion,
    Profile,
    ProfileKind,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
)
from call_monitor.ranking.ollama_client import OllamaClient
from call_monitor.ranking.worker import drain_queue


def _ollama_v2_canned():
    body = {
        "model": "gemma4:e4b-it-q4_K_M",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": json.dumps({
                "topic_fit": {"rating": 5, "rationale": "perfect AI match",
                              "quoted_evidence": "AI for medical imaging"},
                "eligibility": {"rating": "pass", "rationale": "research groups eligible",
                                "quoted_evidence": None},
                "exclusions": {"rating": "pass", "rationale": "no exclusions",
                               "quoted_evidence": None},
            })},
            "logprobs": {"content": [
                {"token": "5", "logprob": -0.05, "top_logprobs": [
                    {"token": "5", "logprob": -0.05},
                    {"token": "4", "logprob": -3.0},
                ]},
            ]},
        }],
    }
    def handler(request):
        return httpx.Response(200, json=body)
    return OllamaClient(
        base_url="http://x", model="gemma4:e4b-it-q4_K_M",
        transport=httpx.MockTransport(handler),
    )


def test_e2e_v2_pipeline(pg_session_factory):
    """Enqueue a v2 job → drain → verify CallRanking + 4 criteria rows
    (timeline pass + topic_fit + eligibility + exclusions) + score 100."""
    with pg_session_factory() as s:
        u = User(email="e2e@ki.se"); s.add(u); s.flush()
        p = Profile(
            name="E2E", kind=ProfileKind.GROUP, description="",
            structured={
                "areas": ["AI for medical imaging"],
                "must_include": [], "hard_exclusions": [],
                "budget_min": None, "budget_max": None,
                "trl_min": None, "trl_max": None,
            },
            created_by=u.id, ranking_scheme="v2",
        )
        s.add(p); s.flush()
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id, external_id="E2E", url="https://x",
            title="AI for medical imaging — call",
            summary="Open call for AI-driven medical imaging research.",
            deadline=datetime(2099, 12, 31, tzinfo=UTC),
            published_at=datetime.now(UTC),
        )
        s.add(c); s.flush()
        s.add(RankingJob(
            call_id=c.id, profile_id=p.id, scheme="v2",
            status=RankingJobStatus.PENDING,
        ))
        s.commit()
        cid, pid = c.id, p.id

    drain_queue(session_factory=pg_session_factory, client=_ollama_v2_canned(), batch_size=10)

    with pg_session_factory() as s:
        ranking = s.execute(
            select(CallRanking).where(CallRanking.scheme == "v2")
        ).scalar_one()
        criteria = s.execute(select(CallRankingCriterion)).scalars().all()

    # Topic 5 (logprob expected ~5.0) × 20 = 100, no penalty.
    assert ranking.aggregate_score == 100
    assert ranking.aggregate_reason is None
    assert ranking.scheme == "v2"
    assert ranking.prompt_version == "v2.0"

    by_name = {c.criterion: c for c in criteria}
    assert set(by_name.keys()) >= {"topic_fit", "eligibility", "exclusions", "timeline"}
    assert by_name["topic_fit"].rating == "5"
    assert by_name["topic_fit"].quoted_evidence == "AI for medical imaging"
    assert by_name["eligibility"].rating == "pass"
    assert by_name["timeline"].rating == "pass"
```

- [ ] **Step 2: Run + commit**

```bash
uv run pytest tests/test_ranking_v2_e2e.py -v
uv run pytest -v
uv run ruff check .
git add tests/test_ranking_v2_e2e.py
git commit -m "test: v2 e2e — enqueue → drain → CallRanking + criteria rows"
```

---

## Verification at end of plan

1. `uv run ruff check .` → clean
2. `uv run pytest -v` → all green (~340+ tests after Plans A + B)
3. `DATABASE_URL=... uv run alembic check` → "No new upgrade operations detected" (no schema changes in Plan B)
4. Manual smoke (requires Ollama with `gemma4:e4b-it-q4_K_M`):
   - Flip an existing test profile to `ranking_scheme='v2'` via `/profiles/<id>/edit`
   - Trigger a backfill (POST `/admin/rerank` or wait for scheduler)
   - Watch `/admin` for queue depth; observe v2 jobs draining
   - Visit `/calls/<id>?profile=<id>` for a ranked call → see the criterion breakdown

## What Plan C picks up

- `/admin/ranking-compare` page that side-by-side renders v1 + v2 for a chosen profile
- Per-scheme queue depth + active-scheme display in `/admin`
- `setwise.py` (phase 2 borderline pass)
- Daily setwise APScheduler job
- Golden-set weekly eval reporting both schemes' Kendall's tau
- Profile-flip backfill trigger (when admin flips v1→v2, kick off `backfill_profile` automatically)
