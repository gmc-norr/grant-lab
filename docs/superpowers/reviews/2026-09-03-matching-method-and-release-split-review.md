# Matching method review, and the two-release split

Date: 2026-09-03
Scope: the call↔profile ranking pipeline, and the decision to ship matching before
proposal assist.
Companion spec: `docs/superpowers/specs/2026-09-03-call-match-release-1-design.md`
(commit `128a63d7`).

---

## The headline

**The ranking pipeline is well designed. It is also almost entirely unmeasured.**

`data/golden_set_last.json` reads:

```json
{"n_fixture": 0, "n_compared": 0, "tau": null}
```

The non-zero rows in `data/golden_set_history.jsonl` are test-suite artifacts — four
entries 400 ms apart, `n_fixture` of 1, 3 and 4, `tau` of 1.0 over three points.

The evaluation harness is complete: Kendall's tau (`ranking/eval.py`), golden-set CRUD
with candidate mining (`ranking/golden_set.py`), an admin page, a weekly job. There is
nothing in the fixture.

The code already knows. `config.py:153-156`:

> *"Defaults to 0.20 so any pair whose BGE-M3 cosine is in [0.20, 0.30) reaches the LLM
> instead of being silently zeroed. Tighten (raise toward the pregate) **once the golden
> set has enough examples** to calibrate the cutoff against observed false negatives."*

A conservative threshold was deliberately chosen pending a calibration that never
happened. That single fact makes most tuning questions unanswerable — including "is
Gemma good enough", "does Sonnet help", and "would model X be better".

---

## What the pipeline actually does

Five stages per (call, profile) pair, in `ranking/rank_pair.py`:

1. **Deterministic gates** — timeline (≥14 days), budget, TRL. Pure Python, free.
   A FAIL short-circuits to score 0 with no LLM call.
2. **Embedding pre-gate** — BGE-M3 cosine, two bands
   (`config.py:148`, `config.py:157`): `< 0.20` hard fail; `0.20–0.30` UNKNOWN, the LLM
   adjudicates; `≥ 0.30` pass. A `must_include` keyword match bypasses both the timeline
   and embedding gates.
3. **Local Gemma 4 E4B Q4_K_M** — rubric producing topic_fit 0-5, eligibility, and
   exclusions, with quoted evidence verified against the call body.
4. **G-Eval expected value** (`ranking/ev.py`) — recovers a continuous score from the
   6-point Likert by taking the expected value over the rating token's logprobs, with
   deterministic JSON-offset binding so rationale prose digits are never weighted.
5. **Selective escalation to Anthropic** — when local score ≥ `RANKING_ESCALATION_MIN_SCORE`
   (45), **or** eligibility=fail with topic_fit ≥ 3. The remote result replaces the local
   one. Capped at `RANKING_REMOTE_DAILY_CAP` (100) per day.

Gates-before-LLM is correct and should stay. A call closing in three days is a date
comparison, not a matching problem.

---

## Findings

### F1 — The golden set is empty (blocking)

See the headline. Every other question in this document is downstream of it.

Labelling 100–200 pairs is roughly a day of domain-expert time, and `golden_set.py`
already ships candidate mining (`_CANDIDATE_LIMIT = 20`).

### F2 — False negatives are invisible, and they are the expensive error

A false positive costs a researcher five seconds of reading. A false negative is a
missed grant, and nothing in the system reports that it happened.

The escalation rescues exactly one false-negative mode — eligibility legalese with a
strong topic (added by a prior Codex review, R3). It does not rescue the general case:
a pair Gemma simply rated low never gets a second look. And a pair below cosine 0.20 is
killed *before any LLM sees it*.

Cheap mitigation: escalate a random ~5% of the low band. It costs almost nothing under
the daily cap and it measures the false-negative rate, which is currently unknown.

### F3 — Cost is not the problem it appears to be

`RANKING_REMOTE_DAILY_CAP = 100` is a hard ceiling, and `CALL_TEXT_LIMIT = 16000` is
sixteen thousand *characters* (~4k tokens), not tokens. At 6k in / 1k out per pair:

| Model | Max cost/month at the cap |
| --- | --- |
| Claude Sonnet 4.6 — **currently configured** | $99 |
| Kimi K3 | $99 |
| Claude Sonnet 5 | $66 |
| Qwen3.8 Max | $54 |
| GLM-5.3 | $38 |
| Claude Haiku 4.5 | $33 |
| DeepSeek V4 Pro | $10 |
| DeepSeek V4 Flash | $3 |

The entire spread is $3 to $99 a month. Switching to the cheapest option saves about
twenty minutes of a researcher's salary, once.

**The cap is doing all the work in that conclusion.** If it is ever raised for a
backfill, redo the arithmetic.

Pricing above is from third-party aggregators, which were caught two releases stale
during this review (they listed GLM-5.2 and Qwen3.7 Max; GLM-5.3 shipped 2026-08-18 at
an unchanged $1.40/$4.40, and Qwen3.8-Max shipped 2026-08-03 at $2.00/$6.00 — *up* from
3.7 Max). Verify against vendor pricing pages before committing.

### F4 — A free win in the remote tier

`remote_rank_model` defaults to `claude-sonnet-4-6` (`config.py:691`) at $3/$15 per MTok.
`claude-sonnet-5` is $2/$10 — a newer model at 33% lower cost. Strictly dominant:

```
REMOTE_RANK_MODEL=claude-sonnet-5
```

### F5 — Whether the remote tier earns its keep is measurable *today*

This does not need the golden set. `call_rankings` persists `escalated`,
`escalated_from_score` and the final `aggregate_score` (`db/models.py:387-389`):

```sql
SELECT count(*) AS escalations,
       round(avg(abs(aggregate_score - escalated_from_score)), 1) AS avg_shift,
       count(*) FILTER (WHERE abs(aggregate_score - escalated_from_score) >= 15) AS big_flips
FROM call_rankings
WHERE escalated AND escalated_from_score IS NOT NULL;
```

If the remote model largely agrees with Gemma, the tier is not earning anything and can
be removed. If it flips scores often, it is the most valuable $99 in the project.

### F6 — Effort is aimed at the wrong tier

Gemma runs on **every pair that clears the gates**. The remote model runs on **at most
100 per day**. Quality work belongs in the local tier, where the binding constraint is
11 GB of VRAM on the RTX 2080 Ti — not price. Kimi K3 is open-weight but 2.8T
parameters; it is not a candidate for this hardware.

### F7 — Two methods have moved since the project started

**Cross-encoder rerankers got small and good.** jina-reranker-v3 is 0.6B and scores 61.94
on BEIR against bge-reranker-v2-m3's 56.51, beating Qwen3-Reranker at 4B. The current
embedding gate is a *bi-encoder* cosine against a threshold — profile and call embedded
separately, then compared. A cross-encoder reads the pair together, which is precisely
the judgement the gate is making. Slotting one between the cosine gate and Gemma is the
cheapest available experiment, and it is the same shape as the already-recorded v2 hybrid
proposal.

**Pointwise Likert is out of favour.** The 2026 consensus favours pairwise comparison with
order alternation for nuanced criteria, and rubric-based judging over Likert baselines.
The G-Eval logprob trick was a good way to *rescue* a Likert scale; the field moved
toward not using one. Mitigating factor: `call_ranking_criteria` is already rubric-shaped
(topic_fit / eligibility / exclusions / timeline / budget / trl). Only `topic_fit` is a
Likert.

---

## The release split

Decided in the same session; full design in the companion spec. Summary:

- Two releases. **Call Match** (collection, ranking, triage, digest, corridor board)
  ships first; the proposal-assist cockpit follows.
- **One repo, two routers, a build flag.** The cockpit routes are already `lazy()`, so
  omitting them from `router.simple.tsx` means Vite never emits those chunks — the code
  is absent from the release-1 bundle, not merely unreachable. A separate frontend
  project was considered and rejected: it would rebuild auth, session and the API client,
  then duplicate every shared fix forever.
- **Peer survey.** Iowa, Auburn, Pitt, Trinity Dublin, MUSC, Oregon and UNO all run a
  weekly funding newsletter, several in discipline-specific variants. No prior art was
  found for funding calls on digital signage. The digest is therefore the proven channel
  and already exists; the corridor board is a cheap experiment, not the foundation.
- **The board is admin-approved.** The ranker proposes; an admin ticks what goes up.
  A corridor screen is institutional voice, and one bad rank on a hospital wall is a
  public incident rather than a bug report.
- `call_ranking_criteria` already carries per-criterion ratings **and text quoted from
  the call**, so "why does this match" can show the funder's own sentence rather than a
  score. Better than the score badge it replaces.

---

## Recommended order

1. `REMOTE_RANK_MODEL=claude-sonnet-5` — free, newer, cheaper (F4).
2. Run the escalation-shift query (F5). Decides whether the remote tier should exist.
3. Label 100–200 golden-set pairs (F1). Unlocks everything else.
4. Sample ~5% of the low band into escalation (F2). Measures the expensive error.
5. Trial a cross-encoder reranker against the golden set (F7). Not before step 3.
6. Leave the hosted-model choice alone (F3). The saving does not justify a data-residency
   review with hospital IT.

## Product risk

Release 1 hands a customer a ranked list whose miss rate nobody has measured. Step 3 is
not cleanup; it is part of shipping.
