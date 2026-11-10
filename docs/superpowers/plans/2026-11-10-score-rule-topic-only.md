# Score Rule: Eligibility Stops Changing the Score — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `aggregate()` scores a call as `topic × 20` whatever the eligibility rating says; eligibility shows only as its chip.

**Architecture:** One rule change in `ranking/aggregate.py`, a `SCORING_VERSION` bump so the app re-ranks every open pair once, and two user-facing texts that describe the old cap. No schema change, no migration, no prompt change.

**Tech Stack:** Python 3.12, pytest; React 19 + vitest.

**Spec:** `docs/superpowers/specs/2026-11-10-score-rule-topic-only-design.md`

## Global Constraints

- Worktree: `/home/parlar_ai/dev/grant-lab/.worktrees/score-rule`, branch `feat/score-rule-topic-only`. Start every shell command with `cd /home/parlar_ai/dev/grant-lab/.worktrees/score-rule &&`.
- Do not change: the gates, the `must_include` bypass, the exclusions rule, `PROMPT_VERSION`, the escalation condition in `rank_pair` (its code; Task 1 extends its docstring), any threshold (`DASHBOARD_HIGH_SCORE_THRESHOLD`, `DIGEST_TOP_THRESHOLD`, `DIGEST_BORDERLINE_LOWER`, `RANKING_ESCALATION_MIN_SCORE`).
- Backend suite: `uv run pytest -n 16 --dist loadfile -q`. Never `-n auto`. Expect `0 skipped`, `0 xfailed`.
- Measured base at `c73b2bcf`: **2487 tests** (with the rule changed crudely and nothing else: 7 failed, 2480 passed). With the whole of Task 1 applied in a dry run: **2547 passed**. Carry deltas against these; re-measure, never adjust a number to close the arithmetic.
- Never `ruff format` a directory; format only the files you touched.
- No model calls from tests. Do not touch the live app, the live DB, or the main tree.
- Commit titles name the change. Commits end with `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`.

---

### Task 1: The rule, its contract tests, and the version bump

**Files:**
- Modify: `src/grant_lab/ranking/aggregate.py`
- Modify: `src/grant_lab/ranking/freshness.py` (`SCORING_VERSION`)
- Modify: `src/grant_lab/ranking/rank_pair.py` (one docstring only)
- Modify: `src/grant_lab/digest/assemble.py` (one comment only)
- Modify: `tests/test_ranking_aggregate.py`
- Modify: `tests/test_ranking_rank_pair.py`
- Modify: `tests/test_ranking_freshness_enqueue.py`
- Create: `tests/test_ranking_score_rule_labelled_set.py`

**Every expected result in this task was measured**, on 2026-11-10 at `12a0742c`: the test edits, the rule and the bump were applied in this worktree, run, mutated, and reverted (`git status` clean afterwards). Steps 1-4 write the tests; Step 5 watches them fail; Step 6 changes the code.

- [ ] **Step 1: Write the failing contract tests in `tests/test_ranking_aggregate.py`**

Add `import pytest` at the top. Delete `test_aggregate_eligibility_fail_short_circuits`, `test_aggregate_eligibility_unclear_caps_at_50` and `test_aggregate_expected_value_respects_unclear_cap`. Add:

```python
# --- eligibility never moves the score (spec 2026-11-10) ---


@pytest.mark.parametrize("topic", range(6))
@pytest.mark.parametrize("eligibility", ["pass", "unclear", "fail"])
def test_aggregate_eligibility_never_moves_the_score(topic, eligibility):
    r = aggregate(_gates_pass(), _llm_clean(topic_argmax=topic, eligibility=eligibility))
    assert (r.score, r.reason) == (topic * 20, None)


@pytest.mark.parametrize("eligibility", ["pass", "unclear", "fail"])
def test_aggregate_exclusions_fail_caps_low_whatever_the_eligibility(eligibility):
    llm = _llm_clean(topic_argmax=4, eligibility=eligibility, exclusions="fail")
    r = aggregate(_gates_pass(), llm)
    assert (r.score, r.reason) == (8, "exclusion")


def test_aggregate_expected_value_ignores_eligibility():
    llm = LLMResult(topic_argmax=5, eligibility="unclear", exclusions="pass", topic_expected=4.8)
    assert aggregate(_gates_pass(), llm).score == 96
```

- [ ] **Step 2: Write the whole-output test, `tests/test_ranking_score_rule_labelled_set.py`**

```python
"""The score rule, run over a whole labelled set (spec 2026-11-10).

Each row is [n, topic_fit, eligibility, exclusions, label]: Sonnet's ratings for
profile 1 on 30 open calls (trial of 2026-11-10), and whether the call was wanted.

THE LABELS ARE CLAUDE'S DRAFTS, read and accepted unchanged by the user. They are
not blind human labels, and only 6 of the 30 are wanted. This pins the rule
against the one labelled set that exists; it does not prove the rule is right.
"""

from grant_lab.ranking.aggregate import LLMResult, aggregate
from grant_lab.ranking.criteria import GateResult, Gates, GateStatus

ROWS = [
    [1, 1, "fail", "pass", "no"],
    [2, 0, "unclear", "pass", "no"],
    [3, 0, "fail", "pass", "no"],
    [4, 0, "unclear", "pass", "no"],
    [5, 0, "fail", "pass", "no"],
    [6, 0, "unclear", "pass", "no"],
    [7, 1, "unclear", "pass", "no"],
    [8, 0, "unclear", "pass", "no"],
    [9, 1, "unclear", "pass", "maybe"],
    [10, 0, "unclear", "pass", "no"],
    [11, 1, "unclear", "pass", "no"],
    [12, 0, "unclear", "pass", "no"],
    [13, 0, "unclear", "pass", "no"],
    [14, 0, "fail", "pass", "no"],
    [15, 0, "unclear", "pass", "no"],
    [16, 1, "unclear", "pass", "no"],
    [17, 3, "unclear", "pass", "good"],
    [18, 3, "unclear", "pass", "maybe"],
    [19, 0, "unclear", "pass", "no"],
    [20, 0, "unclear", "pass", "no"],
    [21, 0, "fail", "pass", "no"],
    [22, 3, "pass", "pass", "maybe"],
    [23, 1, "unclear", "pass", "no"],
    [24, 1, "fail", "pass", "no"],
    [25, 1, "unclear", "pass", "no"],
    [26, 2, "fail", "pass", "maybe"],
    [27, 0, "unclear", "pass", "no"],
    [28, 2, "fail", "pass", "no"],
    [29, 2, "unclear", "pass", "no"],
    [30, 4, "fail", "pass", "maybe"],
]

_GATES = Gates(
    timeline=GateResult(GateStatus.PASS),
    budget=GateResult(GateStatus.PASS),
    trl=GateResult(GateStatus.PASS),
    embedding=GateResult(GateStatus.UNKNOWN),
)


def _auc(scores, wanted):
    """Chance that a wanted call outscores an unwanted one; a tie counts half."""
    pos = [s for s, w in zip(scores, wanted, strict=True) if w]
    neg = [s for s, w in zip(scores, wanted, strict=True) if not w]
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def test_the_labelled_set_is_the_one_the_trial_measured():
    wanted = [row[4] != "no" for row in ROWS]
    assert (len(ROWS), sum(wanted)) == (30, 6)


def test_wanted_calls_outscore_unwanted_ones_on_the_labelled_set():
    scores = [
        aggregate(_GATES, LLMResult(topic_argmax=t, eligibility=e, exclusions=x)).score
        for _, t, e, x, _ in ROWS
    ]
    wanted = [row[4] != "no" for row in ROWS]
    # Measured: 0.955 with this rule, 0.767 with the rule it replaced.
    assert _auc(scores, wanted) >= 0.9
```

- [ ] **Step 3: Update and extend the pair-ranker tests in `tests/test_ranking_rank_pair.py`**

Add `import pytest` directly after `import httpx` (the file does not import it today).

Four existing tests assert the old score. The escalation behaviour they check does not change.

1. Rename `test_rank_pair_eligibility_fail_zeros_score` to `test_rank_pair_eligibility_fail_keeps_the_topic_score`; its last two lines become:

```python
    assert r.score == 80
    assert r.reason is None
```

2. `test_rank_pair_escalates_on_eligibility_fail_high_topic`: docstring becomes
`"""eligibility=fail with topic argmax 4 escalates: the local score is 80 (>= 45)."""` and the last line becomes `assert r.escalated_from_score == 80`.

3. `test_rank_pair_no_escalation_on_eligibility_fail_low_topic`: docstring becomes
`"""eligibility=fail with topic argmax 2 does not escalate: score 40 (<45), and the topic condition (>= 3) misses too."""` and the last line becomes `assert r.score == 40`.

4. `test_escalation_is_wanted_for_an_eligibility_fail_with_strong_topic`: after the rule change a topic-3 call scores 60, so the score clause alone would satisfy this test and the eligibility clause would be untested. Make exclusions fail so only the eligibility clause can want the escalation. Replace the test with:

```python
def test_escalation_is_wanted_for_an_eligibility_fail_with_strong_topic():
    """Exclusions fail holds the score at 6 (<45), so only the
    eligibility-fail-with-topic>=3 clause can want this escalation."""
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    content = _content(3, "fail")
    content["exclusions"] = {"rating": "fail"}
    r = rank_pair(
        client=_ollama_mock(content=content),
        profile=profile,
        call=call,
        now=_NOW,
        remote_client=None,
        escalation_min_score=45,
    )
    assert r.score == 6
    assert r.escalation_wanted is True
```

Then add these three tests directly after it. The first pins what did NOT change (it passes against the old rule and the new one). The other two pin what DID change, under settings the live app does not use (review 2026-11-10, finding 1):

```python
@pytest.mark.parametrize("topic", range(6))
@pytest.mark.parametrize("eligibility", ["pass", "unclear", "fail"])
@pytest.mark.parametrize("exclusions", ["pass", "fail"])
def test_the_default_escalation_set_did_not_move_with_the_topic_only_rule(
    topic, eligibility, exclusions
):
    """Spec 2026-11-10: at the default threshold (45) with integer ratings
    (logprobs off), the pairs that want a second reading are the same under the
    topic-only rule as under the rule it replaced -- topic >= 3, unless
    exclusions failed and eligibility did not. This passes against BOTH rules;
    the two tests below pin the settings where the set DID move."""
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    content = _content(topic, eligibility)
    content["exclusions"] = {"rating": exclusions}
    r = rank_pair(
        client=_ollama_mock(content=content),
        profile=profile,
        call=call,
        now=_NOW,
        remote_client=None,
        escalation_min_score=45,
    )
    assert r.escalation_wanted is (topic >= 3 and (exclusions == "pass" or eligibility == "fail"))


def test_an_expected_value_over_the_threshold_now_escalates_an_eligibility_fail():
    """CHANGED by the topic-only rule (spec 2026-11-10), with logprobs ON only.
    Argmax 2, expected value 2.4, eligibility fail: the old rule scored it 0 and
    the argmax (< 3) missed the eligibility clause, so it stayed local. It now
    scores round(2.4 * 20) = 48 (>= 45) and wants the second reading."""
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    content = (
        '{"topic_fit": {"rating": 2, "rationale": "x"}, '
        '"eligibility": {"rating": "fail"}, "exclusions": {"rating": "pass"}}'
    )
    assert json.dumps(json.loads(content)) == content
    top = [
        {"token": "2", "logprob": math.log(0.6), "bytes": [50]},
        {"token": "3", "logprob": math.log(0.4), "bytes": [51]},
    ]
    r = rank_pair(
        client=_ollama_mock(
            content=json.loads(content),
            logprobs=_logprobs_for(content, rating_char="2", top=top),
        ),
        profile=profile,
        call=call,
        now=_NOW,
        remote_client=None,
        escalation_min_score=45,
        logprobs_enabled=True,
    )
    assert r.topic_fit_expected is not None
    assert abs(r.topic_fit_expected - 2.4) < 1e-6
    assert r.score == 48
    assert r.escalation_wanted is True


def test_a_threshold_of_60_now_escalates_topic_3_with_unclear_eligibility():
    """CHANGED by the topic-only rule (spec 2026-11-10), with a NON-default
    threshold only. The old cap held topic 3 / eligibility unclear at 50 (< 60),
    so it stayed local; it now scores 60 and wants the second reading."""
    profile = _Profile(structured={"areas": [], "hard_exclusions": []})
    call = _Call(deadline=_NOW.replace(year=2099))
    r = rank_pair(
        client=_ollama_mock(content=_content(3, "unclear")),
        profile=profile,
        call=call,
        now=_NOW,
        remote_client=None,
        escalation_min_score=60,
    )
    assert r.score == 60
    assert r.escalation_wanted is True
```

- [ ] **Step 4: Write the staleness test in `tests/test_ranking_freshness_enqueue.py`**

Add directly after `test_a_prompt_version_bump_is_stale`:

```python
def test_a_ranking_stored_under_scoring_version_1_is_stale_under_the_shipped_version(
    session, monkeypatch
):
    """Spec 2026-11-10, contract 4: aggregate() stopped reading eligibility, so a
    score stored under SCORING_VERSION "1" must be re-ranked once. The first
    assert is the control -- the pair is fresh under the version that stamped it,
    so only the version can be what makes it stale. Before the bump the shipped
    version IS "1" and the second assert fails."""
    ctx = ctx_for()
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    with monkeypatch.context() as stored_under:
        stored_under.setattr(F, "SCORING_VERSION", "1")
        _stamp(session, c, p, ctx)
        assert enqueue_stale_pairs(session, ctx=ctx) == 0
    assert enqueue_stale_pairs(session, ctx=ctx) == 1
    assert _pending(session) == [(c.id, p.id)]
```

- [ ] **Step 5: Run the four files and watch them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_ranking_aggregate.py tests/test_ranking_score_rule_labelled_set.py tests/test_ranking_rank_pair.py tests/test_ranking_freshness_enqueue.py -q -p no:cacheprovider 2>&1 | grep -E "^(FAILED|ERROR)|passed|failed|error" | sed 's/ - .*//'`

Expected (measured): **`19 failed, 132 passed`**, every one FAILED, none ERROR:

- `test_ranking_aggregate.py` (11): `test_aggregate_eligibility_never_moves_the_score` for `[unclear-3]`, `[unclear-4]`, `[unclear-5]` and `[fail-0]` to `[fail-5]`; `test_aggregate_exclusions_fail_caps_low_whatever_the_eligibility[fail]`; `test_aggregate_expected_value_ignores_eligibility`.
- `test_ranking_score_rule_labelled_set.py` (1): `test_wanted_calls_outscore_unwanted_ones_on_the_labelled_set`.
- `test_ranking_rank_pair.py` (6): the four edited tests, `test_an_expected_value_over_the_threshold_now_escalates_an_eligibility_fail` and `test_a_threshold_of_60_now_escalates_topic_3_with_unclear_eligibility`. All 36 cases of `test_the_default_escalation_set_did_not_move_with_the_topic_only_rule` PASS here: that is the evidence the default set is the same under the old rule.
- `test_ranking_freshness_enqueue.py` (1): `test_a_ranking_stored_under_scoring_version_1_is_stale_under_the_shipped_version`.

- [ ] **Step 6: Change the rule in `src/grant_lab/ranking/aggregate.py`, and bump the scoring version**

Replace the `LLMResult` docstring's eligibility line and the tail of `aggregate()`.

In `LLMResult`'s docstring, replace

```python
    `eligibility` is 'pass' | 'fail' | 'unclear'.
```

with

```python
    `eligibility` is 'pass' | 'fail' | 'unclear'. `aggregate` does not read it:
    it is carried for the criterion row and the escalation rule (spec 2026-11-10).
```

In `AggregateResult`, replace the comment

```python
    # 'timeline'|'budget'|'trl'|'embedding'|'eligibility'|'exclusion'|None
```

with

```python
    # 'timeline'|'budget'|'trl'|'embedding'|'exclusion'|None
```

In `aggregate()`, replace everything from `    # LLM gates short-circuit next.` to the end of the function with:

```python
    # Eligibility is deliberately NOT read here (spec 2026-11-10). The model's
    # eligibility rating was 'unclear' or 'fail' on every open pair measured, so
    # zeroing on 'fail' and capping 'unclear' at 50 flattened the top of the feed
    # and sank wanted calls. It shows as its own chip instead.

    # Effective topic: use EV when available, fall back to argmax.
    topic = llm.topic_expected if llm.topic_expected is not None else float(llm.topic_argmax)

    if llm.exclusions == "fail":
        # Spec: min(10, topic * 2). Matches v1's "≤ 10" behavior.
        return AggregateResult(min(10, round(topic * 2)), "exclusion")

    return AggregateResult(round(topic * 20), None)
```

In `src/grant_lab/ranking/freshness.py` change `SCORING_VERSION = "1"` to:

```python
# "2" (2026-11-10): aggregate() stopped reading eligibility.
SCORING_VERSION = "2"
```

- [ ] **Step 7: Run the four files again**

Run the Step 5 command.
Expected (measured): **`151 passed`**.

- [ ] **Step 8: Prove the new tests bite (two mutations, each restored with `cp` and checked with `cmp`)**

```bash
cd /home/parlar_ai/dev/grant-lab/.worktrees/score-rule && cp src/grant_lab/ranking/rank_pair.py /tmp/rank_pair.orig.py \
&& python3 - <<'EOF'
p = "src/grant_lab/ranking/rank_pair.py"
s = open(p).read()
a = 'local_parsed.eligibility_rating == "fail" and local_parsed.topic_fit_rating >= 3'
assert s.count(a) == 1
open(p, "w").write(s.replace(a, "False"))
EOF
PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_ranking_rank_pair.py -q -p no:cacheprovider 2>&1 | grep -E "^FAILED|passed|failed" | sed 's/ - .*//'
cp /tmp/rank_pair.orig.py src/grant_lab/ranking/rank_pair.py && cmp /tmp/rank_pair.orig.py src/grant_lab/ranking/rank_pair.py && echo RESTORED
```

Expected (measured): `4 failed, 78 passed` — `test_escalation_is_wanted_for_an_eligibility_fail_with_strong_topic` and `test_the_default_escalation_set_did_not_move_with_the_topic_only_rule` for `[fail-fail-3]`, `[fail-fail-4]`, `[fail-fail-5]` — then `RESTORED`.

```bash
cd /home/parlar_ai/dev/grant-lab/.worktrees/score-rule && cp src/grant_lab/ranking/freshness.py /tmp/freshness.orig.py \
&& python3 - <<'EOF'
p = "src/grant_lab/ranking/freshness.py"
s = open(p).read()
a = '                "scoring_version": SCORING_VERSION,\n'
assert s.count(a) == 1
open(p, "w").write(s.replace(a, ""))
EOF
PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_ranking_freshness_enqueue.py tests/test_ranking_scoring_config.py -q -p no:cacheprovider 2>&1 | grep -E "^FAILED|passed|failed" | sed 's/ - .*//'
cp /tmp/freshness.orig.py src/grant_lab/ranking/freshness.py && cmp /tmp/freshness.orig.py src/grant_lab/ranking/freshness.py && echo RESTORED
```

Expected (measured): `2 failed, 50 passed` — `test_a_ranking_stored_under_scoring_version_1_is_stale_under_the_shipped_version` and `test_a_scoring_version_bump_changes_it` — then `RESTORED`.

- [ ] **Step 9: Fix the comments that describe the old rule**

`src/grant_lab/ranking/rank_pair.py` — in the `rank_pair` docstring's escalation paragraph, directly after "(Codex R3: legalese eligibility false-fails on topically strong calls)", add:

```
      Since 2026-11-10 eligibility no longer lowers the score, so a score can
      only be higher than before and the pairs escalated can only grow. At the
      default threshold (45) with integer ratings they are unchanged, and the
      second clause decides only a call whose exclusions failed. Two settings
      escalate MORE than before: with logprobs on, an eligibility fail whose
      argmax is <= 2 but whose expected topic clears the threshold (2.4 -> 48);
      and with a threshold above 50, an eligibility-unclear call whose topic
      score reaches it (the cap used to hold it at 50).
```

Fit the paragraph to the docstring's existing indentation and wrapping; keep the "(Anthropic Sonnet, enable_analysis=True)" sentence that follows intact.

`src/grant_lab/digest/assemble.py` — in the comment above `DIGEST_BORDERLINE_LOWER`, replace

```
# values a ranking can take are 0/20/40/50/60/80/100 (50 is the
# eligibility=unclear cap). Nothing lies between 40 and 45, so the former
```

with

```
# values a ranking could take were 0/20/40/50/60/80/100 (50 was the
# eligibility=unclear cap, removed 2026-11-10). Nothing lay between 40 and 45, so the former
```

- [ ] **Step 10: Full backend suite, types, lint, architecture map**

```bash
cd /home/parlar_ai/dev/grant-lab/.worktrees/score-rule && uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider 2>&1 | tail -3
uv run mypy 2>&1 | tail -1
uv run ruff check src tests && uv run ruff format --check src/grant_lab/ranking/aggregate.py src/grant_lab/ranking/freshness.py src/grant_lab/ranking/rank_pair.py src/grant_lab/digest/assemble.py tests/test_ranking_aggregate.py tests/test_ranking_rank_pair.py tests/test_ranking_freshness_enqueue.py tests/test_ranking_score_rule_labelled_set.py
uv run python scripts/gen_architecture.py && git status --short docs/ARCHITECTURE.md
```

Expected: **`2547 passed`** (measured with the rule, the bump and every test of this task applied, before the Step 9 comments; 2487 at the base, so +60), 0 failed, 0 skipped, 0 xfailed. mypy: 0 errors (PREDICTED, not run in the dry run). If `docs/ARCHITECTURE.md` changed, include it in the commit.

- [ ] **Step 11: Commit**

```bash
cd /home/parlar_ai/dev/grant-lab/.worktrees/score-rule && git add src/grant_lab/ranking/aggregate.py src/grant_lab/ranking/freshness.py src/grant_lab/ranking/rank_pair.py src/grant_lab/digest/assemble.py tests/test_ranking_aggregate.py tests/test_ranking_rank_pair.py tests/test_ranking_freshness_enqueue.py tests/test_ranking_score_rule_labelled_set.py docs/ARCHITECTURE.md \
&& git commit -m "feat(ranking): eligibility no longer zeroes or caps the score

Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>"
```

---

### Task 2: The two texts that describe the cap

**Files:**
- Modify: `frontend/src/routes/EligibilityWarning.tsx`
- Modify: `frontend/src/routes/StructuredFields.tsx`
- Modify: `frontend/src/routes/ProfileNew.tsx` (comment)
- Modify: `frontend/src/routes/EligibilityWarning.test.tsx`, `ProfileEdit.test.tsx`, `ProfileDetail.test.tsx`, `ProfileNewMatch.test.tsx` (same directory)
- Modify: `frontend/e2e/match.spec.ts` (comment)

**Interfaces:** none. `data-field="eligibility-empty"`, the component names and the "all three facts empty" rule do not change.

New warning text, exact:

```
Eligibility is not filled in, so calls can't be checked for eligibility. Fill in organisation type, country and applicant types.
```

New hint text, exact:

```
Used to check eligibility for each call. If left blank, eligibility can't be checked.
```

- [ ] **Step 1: Install frontend packages** (a fresh worktree has no `node_modules`)

Run: `pnpm -C frontend install --frozen-lockfile`

- [ ] **Step 2: Change the three test constants first**

In `EligibilityWarning.test.tsx`, `ProfileEdit.test.tsx` and `ProfileDetail.test.tsx`, the string starting `"Eligibility is not filled in. Calls can't be judged eligible, so no call can score above 50` is replaced, whole, by the new warning text above. Find all three with:
`grep -rn "no call can score above 50" frontend/src` (expected: those three test files plus `EligibilityWarning.tsx`).

Run: `pnpm -C frontend exec vitest run src/routes/EligibilityWarning.test.tsx src/routes/ProfileEdit.test.tsx src/routes/ProfileDetail.test.tsx`
Expected: FAIL — the tests that look up the warning by its text do not find it.

- [ ] **Step 3: Change the component text**

`EligibilityWarning.tsx`: set `WARNING` to the new warning text. Replace the doc comment above `eligibilityFactsAreEmpty` (from `/** True when none of the three facts carries a value.` to its closing `*/`) with:

```tsx
/** True when none of the three facts carries a value.
 *
 *  With nothing but "(none specified)" in the PROFILE block the model answers
 *  eligibility "unclear" for every call. Until 2026-11-10 `aggregate`
 *  (ranking/aggregate.py) capped such a call at 50, which emptied "top matches",
 *  the digest's top bucket and the boards; that cap is gone (spec
 *  2026-11-10-score-rule-topic-only-design.md), and eligibility no longer moves
 *  the score. What an unfilled profile still costs is the eligibility chip:
 *  every call shows "?" instead of a judgement. */
```

In the doc comment above `ProfileEligibilityWarning`, replace `Without it the cap was explained on the profile page alone, while
 *  the places it bites showed "50 / 100" on every top card and an empty board
 *  (UX pass 2026-10-31).` with `Without it the missing facts were explained on the profile page alone (UX pass 2026-10-31).`

In the `EligibilityFacts` doc comment, replace `is keyed on what
 *  decides the cap.` with `is keyed on what
 *  the model reads.`

`StructuredFields.tsx`: replace `Used to check eligibility for each call. If left blank, match scores are capped at 50.` with the new hint text.

- [ ] **Step 4: Fix the comments that still describe the cap**

Run `grep -rn "capped at 50\|above 50\|cap explanation" frontend/src frontend/e2e` and reword each hit so it states what is true now. Known hits (measured at `c73b2bcf`): `ProfileNew.tsx` ("they were skipped and every score capped at 50" → "they were skipped and no call could be checked for eligibility"), the same sentence in `ProfileNewMatch.test.tsx` and `frontend/e2e/match.spec.ts`, the `// --- H3: eligibility facts empty => every call is capped at 50` banners and their paragraphs in `ProfileEdit.test.tsx` and `ProfileDetail.test.tsx`, and in `EligibilityWarning.test.tsx` the test name `renders the cap explanation verbatim, as an alert, when the facts are empty` (→ `renders the explanation verbatim, as an alert, when the facts are empty`) with its comment about the "above 50" clause. Comments and one test name only: do not add or remove an `expect(` in `match.spec.ts` (its count is pinned by `tests/architecture`).

Then: `grep -rn "capped at 50\|above 50" frontend/src frontend/e2e` → expected: no output.

- [ ] **Step 5: Frontend checks**

```bash
cd /home/parlar_ai/dev/grant-lab/.worktrees/score-rule && pnpm -C frontend exec vitest run 2>&1 | tail -6
pnpm -C frontend run typecheck 2>&1 | tail -2
pnpm -C frontend run lint 2>&1 | tail -2
```

Expected: all pass; the test count is unchanged from before Step 2 (measure it before you start if you want the number). If `typecheck` or `lint` is not a script in `frontend/package.json`, use the names CI uses in `.github/workflows/ci.yml`.

- [ ] **Step 6: Backend suite again** (a frontend-only diff can redden `tests/architecture`)

Run: `uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider 2>&1 | tail -3`
Expected: same total as Task 1 Step 10, 0 failed.

- [ ] **Step 7: Commit**

```bash
cd /home/parlar_ai/dev/grant-lab/.worktrees/score-rule && git add frontend/src frontend/e2e && git status --short \
&& git commit -m "fix(profiles): the empty-eligibility warning no longer claims a score cap at 50

Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>"
```

---

## After the plan (on the user's word only)

1. Push the branch, open a PR, wait for CI.
2. Before deploying: confirm a local Gemma ranking job completes (the GPU driver was out of step on 2026-11-06).
3. Deploy with the usual recipe. The `SCORING_VERSION` bump re-ranks every open pair on the next ingest or the 04:00 reconcile; scores stay old until then.
4. After the re-rank: count profile 1's open calls at ≥ 60 and ≥ 70 (the spec predicts about 9 and 2 from the old ratings; a re-rank changes ratings, so treat that as an order of magnitude).
