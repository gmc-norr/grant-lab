# Ranking freshness — one completed ranking per input revision

**Status:** rev 2, unbuilt. Rev 2 closes three gaps an outside review found in rev 1
(§1.8); the measured evidence for each is below it.
**Base:** branch `chore/ranking-sonnet-5-cap-150` @ `0cb8a8a0` (the running app's code;
not yet on master).
**Depends on:** `2026-09-08-call-embedding-freshness` (plan merged as `060a2022`,
migration 0080, unbuilt). Build and deploy that first — §6.
**Origin:** the 2026-09-23 whole-project review (findings F1, F2, F3 and Astra #2, #3;
held in-session, not written to `reviews/`), the design session that followed it, and
an outside review of rev 1 on 2026-09-24.

**The goal in one line:** a (call, profile) pair is ranked once per *input revision*, and
that ranking is driven to completion — including its Sonnet second reading — with
bounded retries. Not "fewer nightly jobs": that is a consequence, not the contract.

---

## 1. The defects, measured

All figures were measured on 2026-09-24 against the live dev database
(`grant-lab-db-1`): 1 active profile, 475 open calls (448 `eu_portal`, 27 `vinnova`),
686 calls in total. "Open" means `deadline IS NULL OR deadline > now()`.

### 1.1 F1 — every fetched call is re-ranked every night

`bulk_enqueue_for_calls` enqueues every *touched* call × every active profile on every
ingest run, blocked only by an already-active job. "Touched" is every call the adapter
returned that has a row — `upsert_call` returns "was newly inserted", never "content
differed". Ranking jobs completed per day:

```
2026-09-17  485    2026-09-20  520    2026-09-23  450
2026-09-18  530    2026-09-21  548
2026-09-19  537    2026-09-22  478
```

With one profile and 475 open calls, that is the whole open feed, daily.

How much of it actually changed: **measured 2026-09-23 in the review session** by
re-fetching the EU feed and diffing against the stored rows — 447 of 474 byte-identical
on the ranked fields, and each of the other 27 had its deadline moved to the next round.
That run's raw output was not preserved. A reproducible snapshot was taken on
2026-09-24: `.superpowers/measurements/2026-09-24-open-call-content.csv` (475 rows:
`id`, md5 over the ranked fields listed in D2, `md5(raw)`, `fetched_at`). Re-run the
same query after the next 03:00 ingest and diff; that replaces the 2026-09-23 figure.

### 1.2 F2 — the same pairs are paid for on Sonnet again and again

Every re-rank that scores ≥ 45 locally re-escalates. `ranking_remote_attempts`, successful
attempts in the last 14 days, by the number of distinct Stockholm days the same pair was
escalated on:

```
days  pairs
1       8
2       3
3       1
4      15
5      41
6      61
7       1
```

All time: 883 successful attempts over 152 distinct pairs — 5.8 paid calls per pair.

### 1.3 F3 — reconcile and backfill look at publication date, not openness

`reconcile_missing_rankings` (`_RECONCILE_DAYS`) and `backfill_profile`
(`BACKFILL_DAYS`) both select `published_at >= now() - 90 days`. Of the 475 open calls,
**143 (30%) were published earlier** and are invisible to the 04:00 repair and to a new
profile's backfill. The admin page's `rerank_estimate` counts the same wrong set.
Today F1 hides this: ingest re-enqueues them nightly anyway.

### 1.4 Astra #2 — a ranking does not record which inputs it describes

`call_rankings` stores `profile_fingerprint` and `prompt_version`. Reconcile compares
the first and **never reads the second**. Nothing records the call side, and nothing
records the scoring settings (§1.8, gap 3).

### 1.5 Astra #3 — stale advice survives a call change

The worker keeps the prior `summary` / `applicant_notes` / `analysis_model` when a run
produces none, guarded only by `existing.profile_fingerprint == captured_fingerprint`.
A call whose text changed keeps Before-you-apply notes written about its old text.

### 1.6 Dead-letter churn on the ingest path

Reconcile and backfill skip a pair with a terminally-failed job in the last
`_DEAD_LETTER_SUPPRESS_DAYS` (7). **Ingest does not.** Last 7 days: 42 failed jobs over
**7 distinct pairs** — each re-enqueued nightly and dead-lettered again. The errors are
deterministic parse failures (`exclusions missing or not an object` ×36,
`eligibility missing or not an object` ×6), so each night burns `MAX_ATTEMPTS` (5)
Gemma runs per pair for nothing.

### 1.7 Why the 2026-09-08 spec's §6 stops being true

That spec left "ranking freshness against call text" out of scope *because* F1 re-ranks
a re-ingested call the same night. Removing F1 without the call fingerprint turns that
mitigated staleness into a permanent one. The two ship in order (§6).

### 1.8 Three gaps in rev 1 (outside review, 2026-09-24) — all confirmed

Rev 1 said "a pair is current when its fingerprints match". Each gap below is a state in
which the fingerprints match and the ranking is still not the one the system should
hold. F1 masks all three today by re-ranking everything nightly, so removing F1 exposes
them.

**Gap 1 (P1) — a withheld or failed escalation would become permanently "fresh".** When
the daily cap is reached, the worker passes `remote_client=None`, and `rank_pair` saves
the local result and marks the job done. When Sonnet fails (`LLMError` / `ParseError`),
the local result survives, also marked done. Neither the next day's cap reset nor
Sonnet's recovery changes a fingerprint. Measured:

```
open pairs that qualify for escalation, today
  local score >= 45, escalated          90
  local score >= 45, NOT escalated      14
  eligibility fail + topic >= 3, not escalated   2
llm_error outage:  2026-09-15 .. 2026-09-22, 841 attempts over 120 distinct pairs
```

Under rev 1, those 16 pairs and all 120 outage pairs would have kept a Gemma-only score
for good. That includes eligibility failures, which Sonnet exists to adjudicate
(`rank_pair`'s escalation condition, "Codex R3").

**Gap 2 (P2) — dead-letter suppression is keyed on the pair, not on what failed.** A
terminally-failed `ranking_jobs` row stores `call_id`, `profile_id`, `status`,
`attempts`, `last_error` and the timestamps — nothing about its inputs (columns listed
from `\d ranking_jobs`). So a corrected call, a revised prompt or a replaced model stays
suppressed for up to 7 days, possibly past the call's deadline. And `POST /admin/rerank`
goes through `backfill_profile`, which applies the same suppression, so even the manual
override cannot reach a suppressed pair.

**Gap 3 (P2) — scoring settings have no invalidation contract.** `criteria._embedding`
FAILs below `embedding_hard_fail_threshold` and hands the pair to the LLM between it and
`embedding_threshold`. Lowering `EMBEDDING_HARD_FAIL_THRESHOLD` from 0.20 to 0.10 turns a
pair at cosine 0.15 from a gate-fail 0 into an LLM-scored pair. The reviewer's probe
moved one pair from 0 to 80 with a mock model. Read from the code, not re-run here. No
rev 1 fingerprint input changes when a setting does. The same holds for
`EMBEDDING_PREGATE_THRESHOLD`, `RANKING_ESCALATION_MIN_SCORE`,
`RANKING_LOGPROBS_ENABLED`, and the two model ids.

---

## 2. The timeline gate cannot survive change-triggered re-ranking

`criteria._timeline` fails a call whose deadline is under `TIMELINE_MIN_DAYS` (14) away,
reason `too_soon`. That verdict depends on **the clock**, not on the call. Under
re-rank-on-change, a call ranked 20 days before its deadline is never revisited when it
crosses into the 14-day window. Today the nightly re-rank is what keeps the gate true.

It is also the gate doing the most damage: **68 of the 468 scored open pairs (15%)** are
at score 0 with `aggregate_reason = 'timeline'` (70 `timeline` criterion rows are
`fail/too_soon`; the other 2 are must-include overrides that were scored).

**Decision (design session, a product choice):** suitability and urgency are different
things. The gate fails **only on `expired`**. Urgency is shown where it is computed live:
`Deadline.tsx` marks ≤ 14 days as `soon` (red) in the browser. Closed calls are already
excluded downstream:

- digest: `or_(Call.deadline.is_(None), Call.deadline > now)` in every candidate query
  (`digest/assemble.py`)
- boards: `Call.deadline.is_not(None), Call.deadline > func.now()` (`board_candidate_parts`)
- dashboard: closed calls hidden in the inbox unless `show_closed`

The expired check stays as a guard, so a job for a call that closed before the worker
reached it spends no LLM time. **must_include no longer bypasses it.** The
embedding-gate bypass is unchanged.

---

## 3. The design

### D1. Schema — migration 0081 (`down_revision = "0080"`)

| Table | Column | Meaning |
|---|---|---|
| `call_rankings` | `call_fingerprint TEXT NULL` | D2 |
| `call_rankings` | `scoring_fingerprint TEXT NULL` | D3 |
| `call_rankings` | `escalation_state TEXT NULL`, `CHECK (escalation_state IN ('pending','gave_up'))` | D6; NULL = nothing outstanding |
| `call_rankings` | `escalation_failures SMALLINT NOT NULL DEFAULT 0` | D6; counts within one input revision |
| `ranking_jobs` | `profile_fingerprint`, `call_fingerprint`, `scoring_fingerprint`, all `TEXT NULL` | D7; the inputs a failed job failed on |

No grandfathering: a NULL `call_fingerprint` or `scoring_fingerprint` is stale, as 0075
decided for the cheap case. Cost of that first re-rank: 475 open pairs × 1 profile = 475
local rankings, the same as one ordinary night today (450–548 above). The Sonnet share
is bounded by `RANKING_REMOTE_DAILY_CAP` (150); the rest goes `pending` (D6) and drains
over the following days.

The same migration deletes `call_ranking_criteria` rows with `criterion = 'timeline'`
(D8). Closed pairs are never re-ranked and would otherwise keep a chip the frontend no
longer orders. `escalation_state` is a new status vocabulary, and
`docs/ARCHITECTURE.md` carries every vocabulary and CHECK, so regenerate it in the same
commit.

### D2. `call_rank_fingerprint(call)` — what the call contributed

Lives beside `call_embedding_fingerprint` in `profiles/fingerprint.py` and goes through
the existing `_digest` (shared `FINGERPRINT_VERSION`, currently `"v2"`). Payload:

| Part | Keys | Why |
|---|---|---|
| Typed content | `url`, `title`, `summary`, `full_text`, `deadline`, `budget_min`, `budget_max`, `currency`, `programme`, `keywords`, `published_at` | Every `NormalizedCall` field except `external_id` (identity) and `raw`. Wider than what the ranker reads today, so a new read of a typed column is already covered. |
| Raw projection | `trl_min`, `trl_max` from `call.structured` | The only raw keys the ranker reads (`criteria._trl`). |
| Call vector | `embedding_fingerprint`, `embedding_model` (the call's, from 0080) | The cosine gate reads the vector. Same reason `rank_fingerprint` carries both. |

**Why not hash `structured` whole**, as `rank_fingerprint` does for profiles:
`Call.structured` *is* `Call.raw` (the property returns `self.raw or {}`). Raw carries
source metadata that changes between fetches of an otherwise identical call: observed
in the 2026-09-23 review, and the 2026-09-24 snapshot records `md5(raw)` so the next
diff measures it. Hashing it whole would recreate F1. The raw projection is therefore
**the one enumeration this design cannot avoid**. It lives as a named constant,
`RANKED_RAW_KEYS`, and G5 pins it by *recording* which keys ranking reads. Today only 1
of the 475 open calls carries `trl_min`/`trl_max` at all.

### D3. `scoring_fingerprint(settings)` — what the scorer was configured with (gap 3)

A third, separate stamp, so that the admin page and the logs can say *which* side went
stale. Payload:

| Key | Why |
|---|---|
| `PROMPT_VERSION` | the prompt (moved here from rev 1's call payload) |
| `SCORING_VERSION` | a new constant for **code** changes to gates, `aggregate` or parsing, the one thing no setting can see (see below) |
| `embedding_threshold`, `embedding_hard_fail_threshold` | decide gate FAIL vs LLM (gap 3) |
| `escalation_min_score` | decides whether a pair wants a second reading |
| `logprobs_enabled` | changes the topic expected value, so the score |
| local model id (`client.model`), remote model id (`remote_client.model`) | a model swap re-ranks automatically |

Rev 1 left model ids out and made the admin button the lever. Rev 2 includes them. A
swap is exactly when every stored score stops describing the running scorer, and the
cost is bounded: one night of local rankings, and Sonnet capped per day.

**The chokepoint is `drain_queue`'s signature.** Every setting reaches ranking as a
keyword parameter of `drain_queue`, threaded from `scheduler.py`. G25 classifies every
parameter as either *in the payload* (the list above) or *no effect on a stored result*:
`session_factory`, `batch_size`, `tick_budget_seconds`, `remote_daily_cap` (it decides
*when* an escalation runs, which D6 handles), `_clock`. A new parameter that nobody
classified turns G25 red. Measured on the base: `drain_queue` has 11 keyword
parameters: 6 in the payload (`client`, `remote_client`, `embedding_threshold`,
`embedding_hard_fail_threshold`, `escalation_min_score`, `logprobs_enabled`) and the 5
above. No module on the ranking path (`rank_pair`, `criteria`, `aggregate`, `prompt`,
`worker`, `must_include`, `ev`, both clients) reads `Settings` or `get_settings`
directly (grep, 0 hits), so the signature really is the only way in. The enqueue paths compute the same fingerprint from `Settings`,
so D10 includes a parity gate.

**`SCORING_VERSION` is a convention, and the spec says so.** A change to
`criteria.py`, `aggregate.py` or `parse_response` that changes results must bump it. No
gate can see a semantic code change. The bump rule sits in the constant's comment, and
the admin button (D5) is the fallback. This slice's own semantic change (the timeline
gate) needs no bump, because every stamp starts NULL.

### D4. One freshness rule, one window, one module

A new `ranking/freshness.py` owns:

- the **open window**: `deadline IS NULL OR deadline > now()`, replacing both 90-day
  `published_at` cutoffs;
- **currency of a pair**: the existing profile arm (fingerprint match, or the bounded
  `legacy` arm, unchanged) **AND** `call_fingerprint` matches **AND**
  `scoring_fingerprint` matches **AND NOT** (`escalation_state = 'pending'` while a
  remote client is configured);
- **dead-letter suppression by input** (D7).

Its callers:

| Caller | Candidate calls | Profiles | Freshness | Suppression |
|---|---|---|---|---|
| ingest (`bulk_enqueue_for_calls`) | touched ∩ open | active | applied | applied |
| 04:00 reconcile | open | active | applied | applied |
| `backfill_profile` from `post_edit` / `bootstrap_worker` | open | the one profile | applied | applied |
| admin "Re-rank everything" | open | active | **bypassed** | **bypassed** |

Fingerprints are Python over row content, so they are computed in Python and passed to
one bulk `INSERT … SELECT`: call fingerprints as arrays
(`unnest(:call_ids, :call_fps)`), profile fingerprints as today's `VALUES` list, and the
scoring fingerprint as one scalar. The call select is column-limited and must not load
`Call.embedding`, the same rule the profile side follows. Measured load: 2.51 MB of text
across the 475 open calls (average 5,532 characters, maximum 36,399).

`enqueue_for_pair` has no production caller (only a docstring mentions it). Leave it
alone.

### D5. The admin button is the override

`POST /admin/rerank` ("Re-rank everything") re-ranks every open pair, ignoring both
freshness and suppression. It is the lever for an unversioned code change or a run
someone does not trust. The estimate becomes exactly `open calls × active profiles`, and
the label changes from "last {backfill_days} days" to "open calls". `backfill_profile`
gains an explicit `force` parameter. Only the admin endpoint passes `True`.

### D6. Escalation is tracked apart from freshness (gap 1)

Two questions, two answers. *Does this ranking describe the current inputs?* The three
fingerprints answer that. *Has its second reading completed?* `escalation_state`
answers that.

`rank_pair` reports a new `escalation_wanted`: the local outcome qualifies (score ≥
`escalation_min_score`, or eligibility `fail` with topic ≥ 3), whether or not a remote
client was available. After each ranking the worker sets:

| Situation | `escalation_state` | `escalation_failures` |
|---|---|---|
| not wanted, or escalated successfully | NULL | 0 |
| wanted, remote configured but **withheld by the daily cap** | `pending` | unchanged |
| wanted, remote failed with `llm_error` | `pending` | unchanged |
| wanted, remote failed with `parse_error` | `pending`, or `gave_up` at 3 | +1 |
| wanted, **no remote configured** (no API key) | NULL | 0 |

"Unchanged" and "+1" are **within one input revision**. The worker compares the
existing row's three stamps with the captured ones. If any differ, this is a new
revision, and failures start from 0 and `gave_up` clears.

Why the two failure classes differ: an `llm_error` is the *service* failing (the
2026-09-15..22 outage was 841 of them over 120 pairs). Counting it against a pair would
abandon every pair that happened to be pending during an outage. Its retries are
bounded instead by the daily cap (150 attempts, failures included, as the ledger already
counts them) and by the call's deadline, which ends the open window. A `parse_error` is
*this pair's* response failing, so it gets 3 tries per revision.

Why "no remote configured" is NULL rather than `pending`: a local-only deployment has
nothing outstanding. Adding a key later does not start a backlog of paid calls without
anyone deciding to. The admin button does that deliberately.

**Retry cadence is daily, with no new scheduler.** Jobs are created only by ingest,
reconcile, backfill and the admin button. A pending pair becomes due through D4, so it
is picked up by the 03:00/03:15 ingest or the 04:00 reconcile. That comes after the
Stockholm-midnight cap reset.

**A pending pair is re-ranked in full, Gemma included.** A remote-only path that reuses
the stored local result was considered and **deferred**. The local model runs on-prem
at temperature 0 (`OllamaClient` hardcodes it), so the re-run costs GPU time and no
money. A remote-only entry point would have to rebuild the gates and the local result
from stored rows: a second path into `rank_pair` for a saving of about 21 s per pending
pair (the 21 s is `RANKING_LOGPROBS_ENABLED`'s own measurement, logprobs off). Revisit
if GPU time becomes the bottleneck.

### D7. Dead-letter suppression by input (gap 2)

When the worker marks a job terminally failed, it stamps the job with the three captured
fingerprints (`ranking_jobs.*_fingerprint`). Suppression then reads:

> a failed job suppresses its pair for 7 days **only if** its three stamps equal the
> pair's current three fingerprints, **or** its stamps are NULL.

A NULL stamp covers failed rows written before 0081, and failures that happen before
the fingerprints are captured (the call or profile vanished). Both keep today's
pair-wide rule, which is time-bounded. A corrected call, a prompt bump or a model swap
changes a fingerprint and ends the suppression at once. The admin button bypasses
suppression outright (D5).

### D8. Timeline changes (§2)

- `criteria._timeline`: FAIL only when `deadline <= now` (`expired`). `TIMELINE_MIN_DAYS`
  and the `too_soon` branch go.
- `rank_pair` and `aggregate`: the timeline FAIL is unconditional. `must_include` still
  bypasses the embedding gate only.
- `_write_criteria_rows`: drop the `("timeline", …)` tuple. On an open call that row can
  only say "pass". `aggregate_reason = 'timeline'` stays as the expired guard's reason
  code.
- `_rank_summary`: drop the "must-include override; timeline" note.
- `call_detail_view`: drop "(minimum 14 day window)" from the timeline text.
- Frontend `CRITERIA` in `CriterionChips.tsx`: drop `"timeline"`.
- `tests/architecture/test_criterion_vocabulary.py`: the gate-tuple scan expects three
  gates. Whether the label map (`web/filters.py`) keeps a `timeline` label for the
  reason code is for the plan to settle against that gate's rules.

### D9. Stamping and preserving advice

`captured_call_fingerprint` and `captured_scoring_fingerprint` are taken where
`captured_fingerprint` is today, before `rank_pair`, for the reason the existing comment
gives. The advice is preserved only when **all three** existing stamps equal the
captured ones (Astra #3).

The worker does **not** defer on a lagging *call* vector, unlike the profile side. A
pair ranked against an old call vector is stamped with the old `embedding_fingerprint`.
The 03:30 tick then refreshes the vector, the fingerprint no longer matches, and the
04:00 reconcile re-ranks the pair. That self-heals at the cost of one extra ranking,
only when an embed fails.

### D10. Sonnet `temperature=0` — dropped

The design session asked for it to steady re-rank-to-re-rank noise. Claude Sonnet 5
rejects non-default sampling parameters with a 400 (Claude API migration guide,
"Migrating to Claude Sonnet 5": "non-default sampling parameters are rejected"). The
motivation also mostly goes away: a pair re-ranked once per input revision has no
nightly re-roll to be noisy. `AnthropicClient` is unchanged.

---

## 4. What changes for users

- Calls closing within 14 days get a real score instead of 0. The 68 open pairs above
  re-enter the ranking, and the red deadline carries the urgency.
- Open calls published more than 90 days ago are repaired and backfilled like any other.
- A pair held back by the daily cap or a Sonnet outage gets its second reading on a
  following day, instead of waiting for the next accidental nightly re-rank.
- Before-you-apply notes no longer outlive the call text they describe.
- A threshold or model change takes effect for every open pair, not just new ones.
- Steady state: nightly work is new calls, calls whose ranked fields changed, and
  pending escalations. `PREDICTED (not run)`: on the order of the 27/night measured
  2026-09-23; the snapshot diff in §1.1 replaces this.

---

## 5. What to measure after deploy

The contract is "one completed ranking per input revision", so measure that and not
just job counts. Each is one query, run daily for the first week:

| Measure | Expect |
|---|---|
| ranking jobs done per day | falls from ~500 to new + changed + pending |
| open pairs with `escalation_state = 'pending'` | drains to near 0 within days of deploy; rises only during a cap-hit day or an outage |
| open pairs with `escalation_state = 'gave_up'` | small; each one is worth reading |
| successful remote attempts per pair over 14 days | ≤ the pair's input revisions (normally 1), against 5.8 all-time today |
| failed jobs per day | the 7 deterministic pairs stop recurring nightly |

---

## 6. Deploy sequencing and the 2026-09-08 amendment

1. **Build and deploy `060a2022` (migration 0080).** Let one 03:30 embedding tick run.
   Check: `SELECT count(*) FROM calls WHERE embedding_fingerprint IS NULL AND (deadline IS NULL OR deadline > now())` returns 0.
   Its spec's §3 D4 baseline (58 calls) is stale: there are 686 calls now. Re-measure
   before relying on "seconds".
2. **Then deploy this slice (0081).** The first 03:00/03:15 ingest enqueues every open
   pair it touches (NULL = stale). The 04:00 reconcile enqueues the rest, including the
   143 older-published open calls. Escalations beyond the day's cap go `pending` and
   drain on the following days.

If step 2 ships before step 1's first tick, rankings are stamped with a NULL call
`embedding_fingerprint`, and every open pair re-ranks once more after the tick. That
costs one extra night and gives no wrong result, but ordering avoids it.

**Amendment:** the 2026-09-08 spec's §6 carries a dated note pointing here. Its
paragraph stays as written, as the record of what held then.

---

## 7. Gates

Every flip set is `PREDICTED (not run)` — the code does not exist. A gate is accepted
only with two lines of evidence: it passes on the new code, and it fails on something
it must reject (`measure-dont-predict` Rule 2).

**Freshness and window**

| # | Gate | What trivially passes it |
|---|---|---|
| G1 | An unchanged call re-ingested enqueues **no** job (F1) | an ingest that touched nothing: assert `touched_call_ids` non-empty |
| G2 | A call whose `summary` changed enqueues exactly its pairs | enqueue-everything (G1 rejects it) |
| G3 | A change to raw outside `RANKED_RAW_KEYS` enqueues nothing | a fingerprint that ignores raw entirely (G4 rejects it) |
| G4 | A change to `raw.trl_min` enqueues | — |
| G5 | Keys read from `call.structured` during `rank_pair` ⊆ `RANKED_RAW_KEYS`, recorded by a dict subclass; mutation: add a read of a new key in `_trl` → red | a fixture that never reaches `_trl`: assert the recorded set is non-empty |
| G6 | Reconcile enqueues an open call published 120 days ago (F3) | a fixture with only recent `published_at` |
| G7 | Reconcile skips a closed call | a fixture with only open calls |
| G13 | First run after migration: NULL `call_fingerprint` / `scoring_fingerprint` is stale | a fixture that seeds fingerprints |
| G15 | A changed call `embedding_fingerprint` makes the pair stale | a fixture whose vector never changes |

**Scoring settings (gap 3)**

| # | Gate | What trivially passes it |
|---|---|---|
| G8 | A `PROMPT_VERSION` change makes every open pair stale | stamping the literal instead of recomputing |
| G24 | Changing `embedding_hard_fail_threshold` (0.20 → 0.10) makes every open pair stale, and a pair at cosine 0.15 re-ranks from gate-fail to LLM-scored | a pair outside [0.10, 0.20): pin its cosine |
| G25 | Every `drain_queue` keyword parameter is in the scoring payload or on the explicit no-effect list; mutation: add a parameter → red | an empty classification list: assert both lists are non-empty and the union equals the signature |
| G26 | The scoring fingerprint the enqueue paths compute from `Settings` equals the one the worker stamps from its parameters | a test that builds both from one shared helper call and never compares production wiring |

**Escalation completion (gap 1)**

| # | Gate | What trivially passes it |
|---|---|---|
| G17 | Cap reached → the pair is saved `pending`. The next day, with cap left, the same unchanged pair is enqueued and escalated | a fixture where the cap never binds |
| G18 | Five consecutive `llm_error`s leave the pair `pending` with `escalation_failures = 0` | a single failure |
| G19 | Three `parse_error`s in one revision → `gave_up`, and the pair is no longer enqueued | a fixture that escalates successfully |
| G20 | A new revision (call text change) resets failures and clears `gave_up` | a fixture that never reached `gave_up` |
| G21 | No remote configured → a wanted escalation is saved NULL, not `pending`, and nothing is re-enqueued | a fixture with a remote client |

**Advice, suppression, admin, timeline**

| # | Gate | What trivially passes it |
|---|---|---|
| G9 | Call text changed, profile unchanged, run produces no analysis → prior analysis **not** kept (Astra #3) | a run that produced its own analysis: use a gate-fail re-rank |
| G10 | All three stamps match, run produces no analysis → prior analysis kept | never preserving (G9 alone passes on it) |
| G11 | Ingest does not re-enqueue a pair that failed 2 days ago on the **same** inputs | a fixture with no failed job |
| G22 | The same failed pair **is** re-enqueued once its call fingerprint changes (gap 2) | suppressing nothing (G11 rejects it) |
| G23 | Admin re-rank enqueues a suppressed pair and a fresh pair; estimate = open × active (gap 2, D5) | a fixture with only stale, unsuppressed pairs |
| G12 | Deadline in 5 days → timeline PASS, LLM runs, score not zeroed; deadline passed → FAIL **even with a must_include match** | a fixture whose must_include never matches |
| G16 | Migration 0081 deletes `timeline` criterion rows and nothing else | a database with no `timeline` rows: seed one, and seed one `budget` row that must survive |

G1/G2, G9/G10, G11/G22 and G17/G21 are pairs: each catches one failure direction the
other alone would miss.

---

## 8. Tests that pin the contract this slice reverses

Found by name (`grep`), **READ, not run**. Each must be seen to go red on the new code
before it is rewritten, which is also the cheapest proof that the change landed:

- `tests/test_ranking_criteria.py::test_timeline_fails_when_deadline_is_13_days_away`
- `tests/test_ranking_aggregate.py::test_aggregate_timeline_fail_ignored_with_must_include_override`
- `tests/test_ranking_rank_pair.py::test_rank_pair_must_include_bypasses_timeline_gate`
- `tests/test_ranking_worker.py::test_worker_must_include_with_failing_timeline_persists_llm_score`
- `tests/test_ranking_worker.py::test_worker_must_include_override_appears_in_summary`
  (asserts the timeline override note; check what else it covers before rewriting)
- `tests/architecture/test_criterion_vocabulary.py::test_the_gate_tuple_scan_finds_the_four_gates`
- `frontend/src/components/CriterionChips.test.tsx`: the `timeline` / `too_soon` chip case

`tests/test_ranking_aggregate.py` also uses `too_soon` as a fixture reason in its
short-circuit test. That test survives with `expired`.

---

## 9. Out of scope

- **A remote-only escalation path** that reuses the stored local result (D6, deferred
  with its reason).
- **A UI marker for `pending` / `gave_up`.** The state now exists for one. See the
  memory note on the escalation cap as a silent quality cliff.
- **A circuit breaker for a Sonnet outage.** During one, the daily cap still bounds
  failed attempts, as today.
- **Sonnet 5 request settings** beyond D10: adaptive thinking is on by default on
  Sonnet 5, and the effort level is unset. Both affect cost and variance, and are a
  separate decision.
- **The 7 dead-lettered pairs themselves.** Gemma omits a required field for them. This
  slice stops the nightly churn, not the parse failure.
- **The weekly digest never having sent** (0 `digest_runs`). A separate investigation.
- A general event system, a separate service, or periodic ranking expiry. The existing
  queue plus the daily reconcile is enough.
- `events.ts`, the corridor-board CSS at non-1080p resolutions, the scoring rubric.

---

## 10. Baseline

Not measured for this document. Measure on the build branch **after 0080 has landed**,
and do not copy a number from here or from the 2026-09-08 spec:

```
backend:  <measure>  (the "1 skipped" stays 1)
frontend: <measure>
latest migration: 0080  ->  this slice adds 0081
```
