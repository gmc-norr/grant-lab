# Score rule: eligibility stops changing the score

Date: 2026-11-10. Status: design, not built.

## The job

A researcher opens the feed to find the calls worth reading. The feed is sorted
by `aggregate_score`. The job is done when the calls the researcher wants sit
above the calls they do not want.

## What really happens today

`ranking/aggregate.py` turns the model's three ratings into one number:

- score = `topic_fit` (0–5) × 20
- eligibility `fail` → score 0, reason `eligibility`
- eligibility `unclear` → score capped at 50
- exclusions `fail` → at most 10

The eligibility rating is the weakest of the three. On profile 1's open calls
(measured 2026-11-10, 413 calls) Sonnet rated eligibility `unclear` 45 times,
`fail` 18 times and `pass` 0 times. So every strong call is capped or zeroed:
no open call scores above 50, eight tie at 50, and nothing reaches the "top
match" line (70) or the digest's top line (60).

## Evidence

A 30-call trial (`~/grant-lab-labels/trial.py`, replies saved in
`trial-cache.json`). Sonnet rated every call with today's prompt. The measure is
AUC: the chance that a wanted call scores above an unwanted one (1.0 perfect,
0.5 coin flip). 6 calls were wanted, 24 were not.

| Rule | AUC |
|---|---|
| Today | 0.767 |
| Topic only (this design) | 0.955 |
| `fail` takes one step (−20) off | 0.944 |
| `fail` halves the score | 0.941 |

Today's rule zeroed 2 of the 6 wanted calls (labels 26 and 30, eligibility
`fail`) and capped 2 more from 60 to 50 (labels 17 and 18, eligibility
`unclear`). The other two (9 and 22) score the same under both rules.

**Limits of this evidence.** The labels are Claude's drafts, which the user read
and accepted unchanged; they are not blind human labels. There are only 6 wanted
calls, so one swapped pair moves AUC by 0.007. The gap between today's rule and
the other three is large; the gaps among the other three are noise.

## The rule

After the gates, `aggregate()` returns:

- exclusions `fail` → `min(10, round(topic × 2))`, reason `exclusion` (unchanged)
- otherwise → `round(topic × 20)`, reason `None`

Eligibility is not read by `aggregate()` any more. It never sets the score and
never sets the reason.

Unchanged: the timeline, budget, TRL and embedding gates; the `must_include`
bypass; the exclusions rule; the prompt (`PROMPT_VERSION` stays `v5.0`); the
expected-value option (`topic_expected`).

### Decision: "not eligible" keeps its full score

The user chose this over halving the score. The eligibility rating is wrong too
often to let it sink a call, and the two rules measured the same.

## How the user sees eligibility

On a call: no change. The feed card (`TriageCard`) and the call page
(`MatchCallOverview`) already render `CriterionChips`, which shows an
eligibility chip: ✓ pass, ✗ fail, ? unclear, with the model's reason behind it.
That chip becomes the only place eligibility shows on a call.

Two texts change, because they describe the cap:

- `EligibilityWarning` (shown on the profile pages, the feed and a board when a
  profile has no eligibility facts) says "no call can score above 50 and none
  will reach 'top matches' or the board". New text: "Eligibility is not filled
  in, so calls can't be checked for eligibility. Fill in organisation type,
  country and applicant types." The alert stays where it is and keeps its rule
  (all three facts empty).
- The hint in `StructuredFields`, "If left blank, match scores are capped at
  50.", becomes "If left blank, eligibility can't be checked."

## This reverses an earlier decision

The UX pass of 2026-10-31 kept the cap and added the warning instead; the
comment in `EligibilityWarning.tsx` records it: "The cap rule itself is NOT
changed (recorded human decision)". This design changes the cap rule. The
evidence is the trial above, which did not exist then.

## What else moves

- **Escalation to Sonnet: same pairs as today, at the live settings.** The
  rule in `rank_pair` is "local score ≥ threshold, or eligibility `fail` with
  topic ≥ 3", and its code does not change. At the default threshold (45) with
  integer ratings (logprobs off), the pairs that want a second reading are
  "topic ≥ 3, unless exclusions failed and eligibility did not" under both the
  old rule and the new one. Measured: all 36 combinations of topic 0–5 ×
  eligibility × exclusions give the same answer against both rules. The live
  app runs on these defaults (neither `RANKING_ESCALATION_MIN_SCORE` nor
  `RANKING_LOGPROBS_ENABLED` is set in its `.env`, checked 2026-11-10).
- **Escalation grows under two non-default settings.** Removing the zero and
  the cap can only raise a score, so the escalated set can only grow. It does
  grow in two cases, both reproduced:
  - *Logprobs on:* eligibility `fail` with topic argmax ≤ 2 and an expected
    topic that clears the threshold. Argmax 2, expected 2.4: the old rule
    scored 0 and kept it local; it now scores 48 and goes to Sonnet.
  - *Threshold above 50:* eligibility `unclear` with a topic score that reaches
    the threshold. At threshold 60, topic 3 / `unclear` was capped at 50 and
    stayed local; it now scores 60 and goes to Sonnet.

  Both are accepted: such a pair now carries a score the threshold was set to
  catch. Anyone who turns logprobs on or raises the threshold should expect
  more Sonnet calls than the old rule gave at the same setting.
- **Thresholds start to bite.** With today's stored ratings the new rule gives
  2 calls ≥ 70 and 9 calls ≥ 60, against 0 and 0 today. So the "top match"
  highlight, the digest's top section (≥ 60) and board `min_score` filters begin
  to select calls. The thresholds themselves do not change.
- **`aggregate_reason = 'eligibility'`** is no longer written. The criterion
  name `eligibility` stays in the vocabulary, because the criterion row is
  still written.

## Old scores

Bump `SCORING_VERSION` from `"1"` to `"2"` in `ranking/freshness.py`. That is
the existing convention for a change to `aggregate.py`: every open pair turns
stale and is re-ranked once, on the next ingest or the 04:00 reconcile.

Cost: about 413 local Gemma runs and a Sonnet run for each pair with topic ≥ 3
(about 10–20 pairs; under $1, estimated). Until the re-rank finishes, rows keep
their old score.

Rival, not chosen: a data migration that recomputes each score from the stored
criteria rows. It is instant and free, but it leaves `SCORING_VERSION` alone
when the convention says to bump it, and a bump would re-rank everything anyway.

## How failures show

- A call the group truly cannot apply for can now sit high. The ✗ eligibility
  chip on the card says why. Today's data has 1 such call with topic ≥ 3.
- If the local GPU cannot run Gemma, the re-rank stalls and old scores stay.
  `nvidia-smi` reported a driver/library mismatch on 2026-11-06. The user reported the driver working again on 2026-11-10 and
  `nvidia-smi` answers; that a Gemma ranking job completes is still unchecked.
  **Check it before deploying.**

## The contract, as tests

1. **Eligibility never moves the score.** For every topic 0–5 and every
   eligibility value, with gates passing and exclusions `pass`,
   `aggregate()` returns `topic × 20` and reason `None`. This test must fail
   against today's `aggregate()`.
2. **Exclusions still cap.** Exclusions `fail` gives at most 10, reason
   `exclusion`, for every eligibility value.
3. **Whole-output check.** A fixture of the trial's 30 rows (topic, eligibility,
   exclusions, label; marked in the file as Claude-drafted labels) must give
   AUC ≥ 0.9 through `aggregate()`. Against today's rule it gives 0.767, so the
   test can go red.
4. **Staleness.** A ranking stamped under scoring version 1, and fresh under
   that version, is enqueued by `enqueue_stale_pairs` under the version the
   code ships with. An integration test, not a check of the constant; it must
   fail before the bump.
5. **Escalation.** At threshold 45 with integer ratings, `escalation_wanted`
   is "topic ≥ 3, unless exclusions failed and eligibility did not" for all 36
   combinations; this test must pass against the old rule too. The two cases
   where escalation grows (logprobs on; threshold 60) are each pinned by a
   test that fails against the old rule.

Existing tests that pin the old rule (score 0 / reason `eligibility`, the cap
at 50) change with it. `docs/ARCHITECTURE.md` is regenerated in the same commit
if the generator's output moves.

## Not in this change

- Reading each call once into facts (tied with pair-by-pair in the trial; it
  pays only with several profiles).
- A finer topic scale, or anchors for ratings 2 and 4. Ties at 60 are still
  broken by embedding similarity.
- Replacing Gemma.
- Blind human labels. They remain the missing yardstick.
