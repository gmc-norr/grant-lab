# Score Rule: Eligibility Stops Changing the Score — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `aggregate()` scores a call as `topic × 20` whatever the eligibility rating says; eligibility shows only as its chip.

**Architecture:** One rule change in `ranking/aggregate.py`, a `SCORING_VERSION` bump so the app re-ranks every open pair once, and two user-facing texts that describe the old cap. No schema change, no migration, no prompt change.

**Tech Stack:** Python 3.12, pytest; React 19 + vitest.

**Spec:** `docs/superpowers/specs/2026-11-10-score-rule-topic-only-design.md`

## Global Constraints

- Worktree: `/home/parlar_ai/dev/grant-lab/.worktrees/score-rule`, branch `feat/score-rule-topic-only`. Start every shell command with `cd /home/parlar_ai/dev/grant-lab/.worktrees/score-rule &&`.
- Do not change: the gates, the `must_include` bypass, the exclusions rule, `PROMPT_VERSION`, the escalation condition in `rank_pair`, any threshold (`DASHBOARD_HIGH_SCORE_THRESHOLD`, `DIGEST_TOP_THRESHOLD`, `DIGEST_BORDERLINE_LOWER`, `RANKING_ESCALATION_MIN_SCORE`).
- Backend suite: `uv run pytest -n 16 --dist loadfile -q`. Never `-n auto`. Expect `0 skipped`, `0 xfailed`.
- Measured base at `c73b2bcf`: **2487 tests** (with the rule changed crudely and nothing else: 7 failed, 2480 passed). The 7 are named in Task 1. Carry deltas against this; re-measure, never adjust a number to close the arithmetic.
- Never `ruff format` a directory; format only the files you touched.
- No model calls from tests. Do not touch the live app, the live DB, or the main tree.
- Commit titles name the change. Commits end with `Co-Authored-By: Claude <noreply@anthropic.com>`.

---

### Task 1: The rule, its contract tests, and the version bump

**Files:**
- Modify: `src/grant_lab/ranking/aggregate.py`
- Modify: `src/grant_lab/ranking/freshness.py` (`SCORING_VERSION`)
- Modify: `src/grant_lab/ranking/rank_pair.py` (two comments only)
- Modify: `src/grant_lab/digest/assemble.py` (one comment only)
- Modify: `tests/test_ranking_aggregate.py`
- Modify: `tests/test_ranking_rank_pair.py`
- Modify: `tests/test_ranking_scoring_config.py`
- Create: `tests/test_ranking_score_rule_labelled_set.py`

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

- [ ] **Step 3: Run both files and watch them fail**

Run: `uv run pytest tests/test_ranking_aggregate.py tests/test_ranking_score_rule_labelled_set.py -q -p no:cacheprovider`

Expected: FAILED, not ERROR. In `test_ranking_aggregate.py`: the `eligibility_never_moves_the_score` cases with `fail` (topic ≥ 1) and `unclear` (topic ≥ 3), the `exclusions_fail..._whatever_the_eligibility[fail]` case (today returns `(0, "eligibility")`), and `expected_value_ignores_eligibility` (today 50). In the labelled-set file: `test_wanted_calls_outscore...` fails (today 0.767); `test_the_labelled_set_is...` passes.

- [ ] **Step 4: Change the rule in `src/grant_lab/ranking/aggregate.py`**

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

- [ ] **Step 5: Run the two files again**

Run: `uv run pytest tests/test_ranking_aggregate.py tests/test_ranking_score_rule_labelled_set.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 6: Update the four pair-ranker tests in `tests/test_ranking_rank_pair.py`**

They assert the old score. The escalation behaviour they check does not change.

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

Run: `uv run pytest tests/test_ranking_rank_pair.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 7: Prove test 4 bites (mutation, restored with `cp`)**

```bash
cd /home/parlar_ai/dev/grant-lab/.worktrees/score-rule && cp src/grant_lab/ranking/rank_pair.py /tmp/rank_pair.orig.py \
&& python3 - <<'EOF'
p = "src/grant_lab/ranking/rank_pair.py"
s = open(p).read()
a = 'local_parsed.eligibility_rating == "fail" and local_parsed.topic_fit_rating >= 3'
assert s.count(a) == 1
open(p, "w").write(s.replace(a, "False"))
EOF
PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_ranking_rank_pair.py -q -p no:cacheprovider 2>&1 | grep -E "^FAILED|passed|failed"
cp /tmp/rank_pair.orig.py src/grant_lab/ranking/rank_pair.py && cmp /tmp/rank_pair.orig.py src/grant_lab/ranking/rank_pair.py && echo RESTORED
```

Expected: `FAILED ...::test_escalation_is_wanted_for_an_eligibility_fail_with_strong_topic` (record any other test that also fails), then `RESTORED`.

- [ ] **Step 8: Bump the scoring version**

In `src/grant_lab/ranking/freshness.py` change `SCORING_VERSION = "1"` to:

```python
# "2" (2026-11-10): aggregate() stopped reading eligibility.
SCORING_VERSION = "2"
```

In `tests/test_ranking_scoring_config.py`, after `test_a_scoring_version_bump_changes_it`, add:

```python
def test_the_scoring_version_is_the_one_the_topic_only_rule_shipped_with():
    """aggregate() stopped reading eligibility at version 2 (spec 2026-11-10).
    A revert to "1" would leave every stored score looking fresh under a rule
    that no longer produced it."""
    assert F.SCORING_VERSION == "2"
```

Run: `uv run pytest tests/test_ranking_scoring_config.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 9: Fix the comments that describe the old rule**

`src/grant_lab/ranking/rank_pair.py` — in the `escalation_wanted` field comment and in the `rank_pair` docstring the clause "eligibility 'fail' with topic >= 3" stays true; add one sentence to the docstring's escalation paragraph, after "(Codex R3: legalese eligibility false-fails on topically strong calls)":

```
      Since 2026-11-10 eligibility no longer lowers the score, so the second
      clause only decides a call whose exclusions failed.
```

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
uv run ruff check src tests && uv run ruff format --check src/grant_lab/ranking/aggregate.py src/grant_lab/ranking/freshness.py src/grant_lab/ranking/rank_pair.py src/grant_lab/digest/assemble.py tests/test_ranking_aggregate.py tests/test_ranking_rank_pair.py tests/test_ranking_scoring_config.py tests/test_ranking_score_rule_labelled_set.py
uv run python scripts/gen_architecture.py && git status --short docs/ARCHITECTURE.md
```

Expected: 0 failed, 0 skipped, 0 xfailed. PREDICTED (not run) total: 2487 − 3 deleted + 18 + 3 + 1 (aggregate) + 2 (labelled set) + 1 (version) = **2509**; record the measured number. mypy: 0 errors. If `docs/ARCHITECTURE.md` changed, include it in the commit.

- [ ] **Step 11: Commit**

```bash
cd /home/parlar_ai/dev/grant-lab/.worktrees/score-rule && git add src/grant_lab/ranking/aggregate.py src/grant_lab/ranking/freshness.py src/grant_lab/ranking/rank_pair.py src/grant_lab/digest/assemble.py tests/test_ranking_aggregate.py tests/test_ranking_rank_pair.py tests/test_ranking_scoring_config.py tests/test_ranking_score_rule_labelled_set.py docs/ARCHITECTURE.md \
&& git commit -m "feat(ranking): eligibility no longer zeroes or caps the score

Co-Authored-By: Claude <noreply@anthropic.com>"
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

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## After the plan (on the user's word only)

1. Push the branch, open a PR, wait for CI.
2. Before deploying: confirm a local Gemma ranking job completes (the GPU driver was out of step on 2026-11-06).
3. Deploy with the usual recipe. The `SCORING_VERSION` bump re-ranks every open pair on the next ingest or the 04:00 reconcile; scores stay old until then.
4. After the re-rank: count profile 1's open calls at ≥ 60 and ≥ 70 (the spec predicts about 9 and 2 from the old ratings; a re-rank changes ratings, so treat that as an order of magnitude).
