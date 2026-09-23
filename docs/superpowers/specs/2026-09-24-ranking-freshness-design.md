# Ranking freshness — re-rank a pair when its inputs change, not every night

**Status:** rev 1, unbuilt.
**Base:** branch `chore/ranking-sonnet-5-cap-150` @ `0cb8a8a0` (the running app's code;
not yet on master).
**Depends on:** `2026-09-08-call-embedding-freshness` (plan merged as `060a2022`,
migration 0080, unbuilt). Build and deploy that first — §5.
**Origin:** the 2026-09-23 whole-project review (findings F1, F2, F3 and Astra #2, #3;
held in-session, not written to `reviews/`) and the design session that followed it.

---

## 1. The defects, measured

All figures below were measured on 2026-09-24 against the live dev database
(`grant-lab-db-1`), 1 active profile, 475 open calls (448 `eu_portal`, 27 `vinnova`),
686 calls in total. "Open" means `deadline IS NULL OR deadline > now()`.

### F1 — every fetched call is re-ranked every night

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

### F2 — the same pairs are paid for on Sonnet again and again

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

### F3 — reconcile and backfill look at publication date, not openness

`reconcile_missing_rankings` (`_RECONCILE_DAYS`) and `backfill_profile`
(`BACKFILL_DAYS`) both select `published_at >= now() - 90 days`. Of the 475 open calls,
**143 (30%) were published earlier** and are invisible to the 04:00 repair and to a new
profile's backfill. The admin page's `rerank_estimate` counts the same wrong set.
Today F1 hides this: ingest re-enqueues them nightly anyway. Remove F1 without fixing F3
and a new profile never sees 30% of the open calls.

### Astra #2 — a ranking does not record which call content it describes

`call_rankings` stores `profile_fingerprint` and `prompt_version`. Reconcile compares
the first and **never reads the second**. Nothing records the call side. Today F1
re-ranks everything nightly, so a changed call is re-ranked by accident.

### Astra #3 — stale advice survives a call change

The worker keeps the prior `summary` / `applicant_notes` / `analysis_model` when a run
produces none (a gate-fail re-rank, or a local-only run), guarded only by
`existing.profile_fingerprint == captured_fingerprint`. A call whose text changed
keeps Before-you-apply notes written about its old text, shown as current.

### Dead-letter churn on the ingest path

Reconcile and backfill skip a pair with a terminally-failed job in the last
`_DEAD_LETTER_SUPPRESS_DAYS` (7). **Ingest does not.** Last 7 days: 42 failed jobs over
**7 distinct pairs**, i.e. each re-enqueued nightly and dead-lettered again. The errors
are deterministic parse failures (`exclusions missing or not an object` ×36,
`eligibility missing or not an object` ×6), so each night burns
`MAX_ATTEMPTS` (5) Gemma runs per pair for nothing.

### Why the 2026-09-08 spec's §6 stops being true

That spec left "ranking freshness against call text" out of scope *because* F1 re-ranks
a re-ingested call the same night. Removing F1 without the call fingerprint below turns
that mitigated staleness into a permanent one. The two changes ship together (§5 carries
the §6 amendment).

---

## 2. The timeline gate cannot survive change-triggered re-ranking

`criteria._timeline` fails a call whose deadline is under `TIMELINE_MIN_DAYS` (14) away,
reason `too_soon`. That verdict depends on **the clock**, not on the call. Under
re-rank-on-change, a call ranked 20 days before its deadline is never revisited when it
crosses into the 14-day window, so the gate would silently stop applying. Today the
nightly re-rank is what keeps it true.

It is also the gate doing the most damage: **68 of the 468 scored open pairs (15%)** are
at score 0 with `aggregate_reason = 'timeline'` (70 `timeline` criterion rows are
`fail/too_soon`; the other 2 are must-include overrides). A call closing in 10 days is
scored 0 and dropped from the email — the moment it is most urgent.

**Decision (design session):** the gate fails **only on `expired`**. The warning moves
to where it is computed live: the dashboard already turns a deadline red at 14 days or
less in the browser. Closed calls are already excluded downstream:

- digest: `or_(Call.deadline.is_(None), Call.deadline > now)` in every candidate query
  (`digest/assemble.py`)
- boards: `Call.deadline.is_not(None), Call.deadline > func.now()` (`board_candidate_parts`)
- dashboard: closed calls hidden in the inbox unless `show_closed`

The expired check stays as a guard — a job enqueued for a call that closes before the
worker reaches it spends no LLM time — and **must_include no longer bypasses it**. There
is nothing to rescue on a closed call. The embedding-gate bypass is unchanged.

---

## 3. The design

**D1. `call_rankings.call_fingerprint TEXT NULL`, migration 0081**
(`down_revision = "0080"`). No grandfathering: NULL means stale, as 0075 decided for the
cheap case. Cost of that first re-rank: 475 open pairs × 1 profile = 475 local rankings,
the same as one ordinary night today (450–548 above). The Sonnet share is bounded by
`RANKING_REMOTE_DAILY_CAP` (150).

The same migration deletes `call_ranking_criteria` rows with `criterion = 'timeline'`
(D6). Open pairs would lose them on their first re-rank anyway; closed pairs are never
re-ranked, and would otherwise keep a chip the frontend no longer orders.

**D2. `call_rank_fingerprint(call)`** beside `call_embedding_fingerprint` in
`profiles/fingerprint.py`, through the existing `_digest` (shared `FINGERPRINT_VERSION`,
currently `"v2"`). Payload:

| Part | Keys | Why |
|---|---|---|
| Typed content | `url`, `title`, `summary`, `full_text`, `deadline`, `budget_min`, `budget_max`, `currency`, `programme`, `keywords`, `published_at` | Every `NormalizedCall` field except `external_id` (identity) and `raw`. Wider than what the ranker reads today, so a new read of a typed column is covered in advance. |
| Raw projection | `trl_min`, `trl_max` from `call.structured` | The only raw keys the ranker reads (`criteria._trl`). |
| Call vector | `embedding_fingerprint`, `embedding_model` (the call's, from 0080) | The cosine gate reads the vector. Mirrors `rank_fingerprint`'s reason for carrying both. |
| Prompt | `PROMPT_VERSION` | Stored on every ranking and compared nowhere (Astra #2). |

**Why not hash `structured` whole**, as `rank_fingerprint` does for profiles:
`Call.structured` *is* `Call.raw` (the property returns `self.raw or {}`), and raw
carries source metadata that changes between fetches of an otherwise identical call
(observed in the 2026-09-23 review; the 2026-09-24 snapshot records `md5(raw)` so the
next diff measures it). Hashing it whole would recreate F1 exactly. The raw projection
is therefore **the one enumeration this design cannot avoid**. It lives as a named
constant (`RANKED_RAW_KEYS`), and G5 pins it by recording which keys ranking actually
reads, rather than trusting a list to stay in step with `criteria.py`.

Today only 1 of the 475 open calls carries `trl_min`/`trl_max` at all. The projection is
there for correctness, not volume.

**Not in the payload, on purpose:** `GEMMA_MODEL` / `REMOTE_RANK_MODEL`. A model swap
does not re-rank automatically. The admin button (D4) is the lever, and the operator
sees the count before pressing it.

**D3. One staleness rule, one window, one module.** A new `ranking/freshness.py` owns:

- the **open window**: `deadline IS NULL OR deadline > now()`, replacing both 90-day
  `published_at` cutoffs (`_RECONCILE_DAYS`, `BACKFILL_DAYS`);
- **currency of a pair**: the existing profile arm (fingerprint match, or the bounded
  `legacy` arm, unchanged) **AND** `cr.call_fingerprint =` the recomputed call
  fingerprint;
- **dead-letter suppression**: the existing 7-day rule, now applied on every path.

Its callers:

| Caller | Candidate calls | Profiles | Stale only? |
|---|---|---|---|
| ingest (`bulk_enqueue_for_calls`) | touched ∩ open | active | yes |
| 04:00 reconcile | open | active | yes |
| `backfill_profile` from `post_edit` / `bootstrap_worker` | open | the one profile | yes |
| admin "Re-rank everything" | open | active | **no** (D4) |

Fingerprints are Python over row content, so they are computed in Python and passed to
one bulk `INSERT … SELECT` as arrays (`unnest(:call_ids, :call_fps)`). The profile side
keeps today's `VALUES` list. The call select is column-limited and must not load
`Call.embedding`, the same rule the profile side follows. Measured load: 2.51 MB of text
across the 475 open calls (average 5,532 characters, maximum 36,399).

`enqueue_for_pair` has no production caller (only a docstring mentions it). Leave it
alone; it is outside the window rule.

**D4. The admin button stays a forced re-rank.** `POST /admin/rerank` is labelled "Re-rank
everything" and is the only way to re-rank after a model swap or an unversioned prompt
tweak. Making it stale-only would remove that. It gets the open window (fixing F3 for
it) and **ignores freshness**. The estimate becomes exactly `open calls × active
profiles`, and the label changes from "last {backfill_days} days" to "open calls".

**D5. The worker stamps both fingerprints and preserves advice only when both match.**
`captured_call_fingerprint = call_rank_fingerprint(call)` is taken where
`captured_fingerprint` is today, before `rank_pair`, for the reason the existing comment
gives. The preservation guard becomes
`existing.profile_fingerprint == captured_fingerprint and existing.call_fingerprint == captured_call_fingerprint`
(Astra #3).

The worker does **not** defer on a lagging *call* vector, unlike the profile side. A pair
ranked against an old call vector is stamped with the old `embedding_fingerprint`. The
03:30 embedding tick then refreshes the vector, the fingerprint no longer matches, and
the 04:00 reconcile re-ranks the pair. That self-heals at the cost of one extra ranking,
and it arises only when an embed fails, which is rare. A deferral mechanism is not worth
that.

**D6. Timeline changes (§2):**

- `criteria._timeline`: FAIL only when `deadline <= now` (`expired`). `TIMELINE_MIN_DAYS`
  and the `too_soon` branch are removed.
- `rank_pair` and `aggregate`: the timeline FAIL is unconditional. `must_include` still
  bypasses the embedding gate only.
- `_write_criteria_rows`: drop the `("timeline", …)` tuple. A timeline-only criterion
  row on an open call can only say "pass". `aggregate_reason = 'timeline'` stays as the
  expired guard's reason code.
- `_rank_summary`: drop the "must-include override; timeline" note.
- `call_detail_view`: drop "(minimum 14 day window)" from the timeline text.
- Frontend `CRITERIA` in `CriterionChips.tsx`: drop `"timeline"`.
- `tests/architecture/test_criterion_vocabulary.py`: the gate-tuple scan expects three
  gates. Whether the label map (`web/filters.py`) keeps a `timeline` label for the
  reason code is for the plan to decide against that gate's rules.

**D7. Sonnet `temperature=0` — dropped from the design.** The design session asked for
it to steady re-rank-to-re-rank noise. Claude Sonnet 5 rejects non-default sampling
parameters with a 400 (Claude API migration guide, "Migrating to Claude Sonnet 5":
"non-default sampling parameters are rejected"). The motivation also mostly goes away
here: once a pair is re-ranked only when its inputs change, there is no nightly re-roll
to be noisy. `AnthropicClient` stays as it is.

---

## 4. What changes for users

- Calls closing within 14 days get a real score instead of 0. The 68 open pairs above
  re-enter the ranking, and the red deadline carries the urgency.
- Open calls published more than 90 days ago are repaired and backfilled like any other.
- Before-you-apply notes no longer outlive the call text they describe.
- Steady state: nightly work is new calls plus calls whose ranked fields changed
  (`PREDICTED (not run)`: on the order of the 27/night measured 2026-09-23; the snapshot
  diff in §1 replaces this). Sonnet repeats (F2) stop.

---

## 5. Deploy sequencing and the 2026-09-08 amendment

1. **Build and deploy `060a2022` (migration 0080).** Let one 03:30 embedding tick run.
   Check: `SELECT count(*) FROM calls WHERE embedding_fingerprint IS NULL AND (deadline IS NULL OR deadline > now())` returns 0.
   Its spec's §3 D4 baseline (58 calls) is stale: there are 686 calls now. Re-measure
   before relying on "seconds".
2. **Then deploy this slice (0081).** The first 03:00/03:15 ingest enqueues every open
   pair it touches (NULL = stale). The 04:00 reconcile enqueues the rest, including the
   143 older-published open calls.

If step 2 ships before step 1's first tick, rankings are stamped with a NULL call
`embedding_fingerprint`, and every open pair re-ranks once more after the tick. That
costs one extra night and gives no wrong result, but ordering avoids it.

**Amendment:** the 2026-09-08 spec's §6 first paragraph gets a dated note pointing here.
The paragraph itself stays as written, as the record of what was true then.

---

## 6. Gates

Every flip set is `PREDICTED (not run)` — the code does not exist. A gate is accepted
only with two lines of evidence: it passes on the new code, and it fails on something
it must reject (`measure-dont-predict` Rule 2).

| # | Gate | What trivially passes it |
|---|---|---|
| G1 | An unchanged call re-ingested enqueues **no** job (F1) | an ingest that touched nothing: assert `touched_call_ids` non-empty |
| G2 | A call whose `summary` changed enqueues exactly its pairs | enqueue-everything (G1 rejects it) |
| G3 | A change to raw outside `RANKED_RAW_KEYS` enqueues nothing | a fingerprint that ignores raw entirely (G4 rejects it) |
| G4 | A change to `raw.trl_min` enqueues | — |
| G5 | Keys read from `call.structured` during `rank_pair` ⊆ `RANKED_RAW_KEYS`, recorded by a dict subclass; mutation: add a read of a new key in `_trl` → red | a fixture that never reaches `_trl`: assert the recorded set is non-empty |
| G6 | Reconcile enqueues an open call published 120 days ago (F3) | a fixture with only recent `published_at` |
| G7 | Reconcile skips a closed call | a fixture with only open calls |
| G8 | A `PROMPT_VERSION` change makes every open pair stale | stamping the literal instead of recomputing |
| G9 | Call text changed, profile unchanged, run produces no analysis → prior analysis **not** kept (Astra #3) | a run that produced its own analysis: use a gate-fail re-rank |
| G10 | Both fingerprints match, run produces no analysis → prior analysis kept | never preserving (G9 alone passes on it) |
| G11 | Ingest does not re-enqueue a pair dead-lettered 2 days ago | a fixture with no failed job |
| G12 | Deadline in 5 days → timeline PASS, LLM runs, score not zeroed; deadline passed → FAIL **even with a must_include match** | a fixture whose must_include never matches |
| G13 | First run after migration: NULL `call_fingerprint` is stale | a fixture that seeds fingerprints |
| G14 | Admin re-rank enqueues a **fresh** pair; estimate = open × active | a fixture with only stale pairs |
| G15 | A changed call `embedding_fingerprint` makes the pair stale | a fixture whose vector never changes |
| G16 | Migration 0081 deletes `timeline` criterion rows and nothing else | a database with no `timeline` rows: seed one, and seed one `budget` row that must survive |

G1 and G2 catch the two failure directions of the enqueue rule. G9 and G10 do the same
for preservation. G12 reverses the contract that the tests in §7 pin.

---

## 7. Tests that pin the contract this slice reverses

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

## 8. Out of scope

- **Model swaps re-ranking automatically** (D2). The admin button covers it.
- **Sonnet 5 request settings** beyond D7: adaptive thinking is on by default on
  Sonnet 5, and the effort level is unset. Both affect cost and variance, and are a
  separate decision.
- **The 7 dead-lettered pairs themselves.** Gemma omits a required field for them.
  This slice stops the nightly churn, not the parse failure.
- **The weekly digest never having sent** (0 `digest_runs`). Separate investigation.
- `events.ts`, the corridor-board CSS at non-1080p resolutions, the scoring rubric.

---

## 9. Baseline

Not measured for this document. Measure on the build branch **after 0080 has landed**,
and do not copy a number from here or from the 2026-09-08 spec:

```
backend:  <measure>  (the "1 skipped" stays 1)
frontend: <measure>
latest migration: 0080  ->  this slice adds 0081
```
