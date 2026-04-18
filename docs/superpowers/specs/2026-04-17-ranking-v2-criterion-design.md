# Call Monitor — Ranking v2: Criterion-Level Rubric Scoring

**Date:** 2026-04-17
**Status:** Draft for review
**Supersedes (in part):** Ranking section of `2026-04-14-call-monitor-design.md`
**Relationship to existing work:** Ranking v1 is implemented and shipped (plan `2026-04-17-ranking.md`). This spec defines v2 as an additive, dual-scheme evolution, not a rewrite.

## Purpose

Ranking v1 produces a monolithic 0–100 score plus a 2–3-sentence rationale per `(call, profile)` pair. It works, and the surrounding defenses (`must_include`, borderline band, golden set, dashboard-as-truth) cover its main failure modes. But three structural weaknesses remain:

1. **Calibration noise.** LLM pointwise absolute scoring is notoriously poorly calibrated. A genuinely borderline call can surface as 48 one run and 62 the next (Liu et al., EMNLP 2023).
2. **Opaque failure modes.** A score of 45 can mean "wrong topic," "right topic but wrong budget," "right topic but ineligible," or "fine but uninteresting." The user cannot act differently without re-reading the call.
3. **Ungrounded rationales.** The LLM can produce plausible-sounding rationales unsupported by the call text. `raw_response` catches the worst cases but requires admin intervention.

v2 addresses these by adopting a **criterion-level rubric** with **grounded (quoted) evidence**, a pattern drawn from TrialGPT (Jin et al., *Nature Communications* 2024, DOI 10.1038/s41467-024-53081-z), which applied the same structure to clinical-trial-to-patient matching with a 43.8% improvement over the strongest prior ranker.

## Summary of changes

- **Criterion-level scoring.** Each `(call, profile)` pair yields per-criterion judgments across six criteria.
- **Rule-based gates.** Deterministic criteria (budget, TRL, timeline) evaluated in Python before any LLM call. Hard-fails short-circuit scoring.
- **LLM rubric.** Three criteria (topic, eligibility, exclusions) judged by the LLM with required quoted evidence.
- **Logprob-weighted aggregation** (G-Eval; Liu et al., arXiv:2303.16634). The topic-fit Likert rating is converted to an expected value over the top-k logprobs rather than argmax — large calibration gain, one-call overhead.
- **Setwise prompting** for borderline calls (phase 2). Groups of 4 compared at a time, refines ordering where pointwise absolute scoring is weakest.
- **Dual-scheme rollout.** v1 and v2 rows coexist for ~4 weeks. Per-profile flag controls the active scheme.
- **Model stays configurable.** Gemma 4 E4B Q8 remains primary. Prometheus 2 (arXiv:2405.01535) documented as an Apache-2.0 swap-in; not adopted now.

## Scope

**In scope:**
- New prompt shape and JSON schema with structured-output enforcement
- `ranking/criteria.py` — rule-based gate evaluation (budget / TRL / timeline)
- `ranking/prompt.py` — criterion-level prompt builder (extends v1)
- `ranking/aggregate.py` — G-Eval logprob weighting + aggregation rules
- `ranking/rank_pair.py` — `rank_pair_v2` alongside existing `rank_pair_v1`
- `ranking/setwise.py` — borderline-band setwise comparison (phase 2)
- Data-model migration (new column on `call_rankings`, new `call_ranking_criteria` table, `ranking_scheme` on `profiles`)
- Worker dispatch by profile scheme
- Golden-set expansion: per-criterion expected bands
- Admin-panel additions: per-scheme queue depth, per-profile scheme flag, side-by-side compare view
- 4-week phased rollout plan

**Out of scope (follow-up specs, listed in "Follow-ups"):**
- Embedding second-opinion (SPECTER2 + SciNCL)
- Pre-LLM text pipeline (PDF / section segmentation)
- Profile drift detection
- User feedback loop with per-criterion granularity

## Architecture

No new top-level module. All changes live inside `ranking/`.

```
ranking/
├── prompt.py         # extended: v2 prompt builder
├── criteria.py       # NEW: rule-based gate evaluation
├── aggregate.py      # NEW: logprob weighting + aggregation rules
├── rank_pair.py      # extended: rank_pair_v2 alongside v1
├── setwise.py        # NEW (phase 2): borderline setwise pass
├── worker.py         # extended: dispatches by profile.ranking_scheme
├── backfill.py       # unchanged
├── queue.py          # unchanged
└── ollama_client.py  # extended: logprobs support
```

`sources/`, `ingestion/`, `digest/`, `web/` are unchanged except for the call-detail template, which renders the criterion breakdown.

## The six criteria

### Rule-based gates (deterministic, no LLM)

Evaluated in Python. Hard-fail short-circuits the LLM call — cheap and predictable.

| Criterion | Rule | Output on fail |
|-----------|------|----------------|
| **Timeline** | `call.deadline` is null or ≥ `now() + 14 days`. | Aggregate = 0, reason `timeline`. |
| **Budget fit** | If both `profile.budget_min/max` and `call.budget_min/max` present **AND** `profile.budget_currency == call.currency` → require range overlap. Null on either side, or currency mismatch → `unknown` (no penalty). | Aggregate = 0, reason `budget`. |
| **TRL fit** | Same logic on `trl_min/max`. | Aggregate = 0, reason `trl`. |

Design note: gates return `unknown`, never `fail`, when inputs are missing. Source adapters often don't populate budget/TRL — treating null as fail would reject valid calls.

**Currency semantics for the budget gate.** Adds a new `profiles.budget_currency` column (text, nullable, ISO 4217). The numeric range overlap is computed only when *both* sides have a currency and they match. Currency mismatch is treated as `unknown`, not `fail` — silently zeroing a EUR call against a SEK-entered profile would be a bad failure mode. FX conversion is deferred to a follow-up; v2's gate is intentionally conservative. Existing profiles default to `NULL` (unknown semantics, no penalty). The profile edit form gains a currency selector (SEK / EUR / USD / null).

### LLM-judged criteria (rubric)

One LLM call per pair. Each criterion yields `{rating, rationale, quoted_evidence}`.

| Criterion | Rating type | Effect on aggregate |
|-----------|-------------|---------------------|
| **Topic fit** | Integer 0–5 Likert | Primary signal. Logprob-weighted expected value → ×20 = base score. |
| **Eligibility** | `pass` \| `fail` \| `unclear` | `fail` → aggregate = 0. `unclear` → aggregate capped at 50. |
| **Exclusions** | `pass` \| `fail` | `fail` → aggregate = min(10, topic_expected × 2). Matches v1's "hard exclusions → ≤ 10" behavior. |

For each LLM criterion, `quoted_evidence` must be a verbatim span from the call (≤ 200 chars). For `eligibility` and `exclusions`, `quoted_evidence` may be null if no relevant sentence exists.

## Prompt shape

Single inference call per pair. Profile block first so the prefix cache is reused. Output enforced by Gemma 4's structured-output mode.

```
SYSTEM (cached per profile):
  You are a research-funding analyst for [hospital name].
  Evaluate a funding call against this profile on three criteria.

  PROFILE:
    Name: <profile.name>
    Kind: <org | group | user>
    Description: <profile.description>
    Research areas: <areas>
    Hard exclusions: <hard_exclusions>

  CRITERIA:
    1. topic_fit (integer 0–5): how well the call's scope aligns with
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

  Output JSON:
  {
    "topic_fit":   {"rating": int,      "rationale": str, "quoted_evidence": str},
    "eligibility": {"rating": "pass" | "fail" | "unclear",
                    "rationale": str, "quoted_evidence": str | null},
    "exclusions":  {"rating": "pass" | "fail",
                    "rationale": str, "quoted_evidence": str | null}
  }

USER (per call):
  CALL:
    Title:     <title>
    Programme: <programme>
    Deadline:  <deadline>
    Budget:    <budget_min>-<budget_max>
    Text:      <first 4000 chars of full_text or summary>
```

v1's truncation was 2000 chars; v2 uses 4000. On a warm prefix cache, Gemma Q8 on the 2080 Ti handles the extra tokens in well under a second. More text = more eligibility information captured. A later spec (pre-LLM text pipeline) will target the 4000 chars more intelligently.

## Aggregation

```python
def aggregate(gates, llm) -> tuple[int, str | None]:
    # Rule-based gates short-circuit.
    if gates.timeline.status in {"too_soon", "expired"}:
        return 0, "timeline"
    if gates.budget.status == "fail":
        return 0, "budget"
    if gates.trl.status == "fail":
        return 0, "trl"

    # LLM gates short-circuit next.
    if llm.eligibility.rating == "fail":
        return 0, "eligibility"

    topic_expected = logprob_expected(llm.topic_fit_logprobs)  # float in [0, 5]

    if llm.exclusions.rating == "fail":
        return min(10, int(topic_expected * 2)), "exclusion"

    base = int(round(topic_expected * 20))  # 0–5 → 0–100
    if llm.eligibility.rating == "unclear":
        base = min(base, 50)

    return base, None
```

Reasons are stored alongside the aggregate so the UI can show *why* a score is what it is (e.g., a "budget mismatch" badge next to a zero score is far more actionable than just a zero).

### G-Eval logprob weighting (detail)

For the `topic_fit.rating` token position, retrieve the top-6 logprobs. Filter to tokens that parse as integers in `{0, 1, 2, 3, 4, 5}`. Softmax-normalize their probabilities. Compute `E[rating] = Σ p_i × int_i`. That expected value — a float in [0, 5] — drives the aggregate. Falls back to argmax integer rating if logprobs are unavailable (logged).

## Setwise prompting for borderline (phase 2)

The borderline band (aggregate ∈ [45, 60]) is where ordering matters most and pointwise is weakest. Setwise ranking is dramatically more consistent than pointwise (Zhuang et al., SIGIR 2024, "A Setwise Approach for Effective and Highly Efficient Zero-shot Ranking with LLMs").

Algorithm (runs once per profile per ranking window, e.g., daily):

1. Gather all calls in `[45, 60]` aggregate for the profile (cap at 12).
2. Partition into groups of 4 (drop the last group if < 4).
3. For each group, prompt the LLM: *"Given the profile above, rank these 4 calls from most to least relevant. Output a permutation of their IDs."*
4. Adjust each call's aggregate within its group by a fixed offset based on within-group rank: `{1: +5, 2: +2, 3: -2, 4: -5}`.
5. Store the setwise adjustment on `call_rankings.setwise_offset` (new column); final displayed score = `aggregate_score + setwise_offset`, clamped to [0, 100].

Cost: ~3× inference on ~10% of pairs. At v1 volume (~750 rankings/week), adds roughly a hundred extra LLM calls per week — trivial on local GPU.

Skipped when fewer than 4 borderline calls exist for the profile.

## Model

**Primary: Gemma 4 E4B Q8** via Ollama, unchanged from v1. Structured-output mode supports the v2 schema cleanly. Logprobs are exposed via Ollama's OpenAI-compatible `/v1/chat/completions` with `logprobs=true, top_logprobs=6`.

**Documented alternative: Prometheus 2 (7B or 8x7B).** Apache-2.0, specifically trained to follow user-supplied rubrics and output both absolute scores and pairwise judgments (Kim et al., EMNLP 2024, arXiv:2405.01535). Fits the 2080 Ti at Q8. Swap is a config change (`OLLAMA_JUDGE_MODEL=prometheus-7b-v2.0`) + golden-set re-evaluation. Not adopted in v2 — Gemma's structured output has been adequate and we have no evidence of rubric-adherence failure at v1 scale.

The `rank_pair_v2` function remains model-agnostic: input = prompt + schema, output = parsed JSON. Any model supporting structured output plugs in.

## Data model

### `call_rankings` — expanded

```
call_rankings
  call_id, profile_id, scheme ('v1' | 'v2'),
  aggregate_score (int 0–100),
  aggregate_reason (text, null),          -- 'timeline' | 'budget' | 'trl' | 'eligibility' | 'exclusion' | null
  setwise_offset (int, default 0),        -- v2 phase 2
  rationale (text),                       -- v1: full free-text; v2: short summary line
  model (text),
  prompt_version (text),
  raw_response (jsonb),
  sample_count (int, default 1),
  ranked_at (timestamptz),
  PRIMARY KEY (call_id, profile_id, scheme)
```

`scheme` joins the PK so v1 and v2 rows coexist.

**Index changes.** Drop `ix_call_rankings_profile_score (profile_id, score DESC NULLS LAST)`. Replace with `ix_call_rankings_profile_scheme_score (profile_id, scheme, aggregate_score DESC NULLS LAST)`. The leading `(profile_id, scheme)` matches the new dominant access pattern (`WHERE profile_id=? AND scheme=? ORDER BY aggregate_score DESC`); without scheme in the index key, dual-scheme storage would force the planner to scan both schemes for every dashboard or digest query.

**Read-path changes.** Every query that reads `call_rankings` must filter by `scheme`, otherwise it duplicates or arbitrarily mixes v1/v2 rows. The affected sites:

- `web/routes/calls.py` (dashboard ranked feed) — filter to `profile.ranking_scheme`
- `digest/assemble.py` (top + borderline buckets, also-in-profiles dedup) — filter to `profile.ranking_scheme`
- `web/routes/admin.py` ranking-compare view (week 2+) — explicitly selects both schemes for side-by-side rendering
- `ranking/backfill.py` — already insert-only via `ON CONFLICT`; with the new PK including scheme, INSERT...SELECT remains conflict-free across schemes.

### `call_ranking_criteria` — new (v2 only)

```
call_ranking_criteria
  call_id, profile_id, scheme ('v2'),
  criterion (text),                  -- 'topic_fit' | 'eligibility' | 'exclusions' | 'timeline' | 'budget' | 'trl'
  rating (text),                     -- "5", "pass", "fail", "unclear", etc. Canonical string.
  rating_expected (float, null),     -- G-Eval expected value for Likert; null for pass/fail
  rationale (text, null),            -- null for rule-based gates
  quoted_evidence (text, null),
  ranked_at (timestamptz),
  PRIMARY KEY (call_id, profile_id, scheme, criterion)
```

One row per pair per criterion *that was actually evaluated*. **Sparse semantics** — when a rule-based gate short-circuits the pair, only the rows for the gates evaluated up to and including the failing gate are inserted; subsequent gates and all LLM criteria are NOT inserted. The aggregate row's `aggregate_reason` is the canonical "why score=0" signal; criterion absence is intentional, not load-bearing. (Dense alternative — insert all 6 rows with `rating='not_evaluated'` for skipped ones — was rejected for storage cost; can be added later if per-criterion analytics need explicit "not evaluated" presence rather than absence.)

Enables per-criterion golden-set evaluation, per-criterion feedback (follow-up spec), and per-criterion badges in the UI.

### `profiles` — new columns

```
profiles
  ...
  ranking_scheme ('v1' | 'v2') DEFAULT 'v1' NOT NULL,
  budget_currency text NULL                          -- ISO 4217; see budget gate
```

`ranking_scheme` is admin-flippable. New profiles default to `'v2'` after rollout week 4. `budget_currency` defaults to NULL on existing profiles (the gate treats null currency as `unknown`, no penalty).

### `ranking_jobs` — extended (scheme is part of identity)

```
ranking_jobs
  id, call_id, profile_id, scheme ('v1' | 'v2'),
  status, attempts, last_error, created_at, updated_at
  PRIMARY KEY (id)
  UNIQUE INDEX (call_id, profile_id, scheme) WHERE status IN ('pending', 'running')
```

The queue is **scheme-aware**, not scheme-agnostic. Two reasons:

1. **Compare mode (week 2)** requires running BOTH schemes against the same pair simultaneously. With the v1 schema's `(call_id, profile_id)` partial unique index, only one active job per pair can exist — making "queue both" structurally impossible.
2. **Profile flip mid-flight is a silent miscarriage of intent.** A job queued at 10:00 with the profile on `v1`, then the admin flips to `v2` at 10:02, then the worker claims the job at 10:05 — the v1-spec'd job runs under v2 with no audit trail. Storing scheme on the job freezes intent at enqueue time.

The worker reads `scheme` from the job (NOT from the profile) and dispatches to `rank_pair_v1` or `rank_pair_v2`. Backfill and the orchestrator's per-call enqueue both pass the profile's *current* `ranking_scheme` at enqueue time. The compare-mode admin trigger (week 2) explicitly enqueues two jobs per pair, one per scheme.

## Migration

Single Alembic revision (`xxxx_ranking_v2_schema`):

1. Add `call_rankings.scheme` (text) with default `'v1'` on existing rows; backfill existing rows to `'v1'`.
2. Add `call_rankings.aggregate_reason`, `call_rankings.setwise_offset`.
3. Drop current PK on `call_rankings`; add new PK `(call_id, profile_id, scheme)`.
4. Drop index `ix_call_rankings_profile_score`; create `ix_call_rankings_profile_scheme_score (profile_id, scheme, aggregate_score DESC NULLS LAST)`.
5. Create `call_ranking_criteria`.
6. Add `profiles.ranking_scheme` default `'v1'`.
7. Add `profiles.budget_currency` (text, nullable).
8. Add `ranking_jobs.scheme` (text) with default `'v1'` on existing rows; backfill to `'v1'`.
9. Drop the partial unique index on `ranking_jobs (call_id, profile_id) WHERE status IN ('pending','running')`; create the scheme-aware replacement on `(call_id, profile_id, scheme)`.

No row-level backfill of *rankings* at migration time. When a profile is flipped to `v2`, the existing 90-day backfill flow re-runs (now enqueueing scheme='v2' jobs); v1 rows stay in place under the dual-scheme PK.

### Down-migration — reversible *with documented data loss*

Reversing the migration after dual-scheme rows have been written requires destroying one scheme's rows. The down-migration is intentionally explicit about this:

1. `DELETE FROM call_ranking_criteria` (whole table dropped next anyway)
2. `DROP TABLE call_ranking_criteria`
3. `DELETE FROM call_rankings WHERE scheme = 'v2'` — **point of no return for v2 ranking data**
4. `DELETE FROM ranking_jobs WHERE scheme = 'v2'`
5. Drop the new scheme-aware partial unique index on `ranking_jobs`; restore the old `(call_id, profile_id) WHERE status IN ('pending','running')`.
6. `ALTER TABLE ranking_jobs DROP COLUMN scheme`.
7. Drop the new index on `call_rankings`; restore `ix_call_rankings_profile_score`.
8. Drop the new PK on `call_rankings`; restore `(call_id, profile_id)`.
9. `ALTER TABLE call_rankings DROP COLUMN scheme, aggregate_reason, setwise_offset`.
10. `ALTER TABLE profiles DROP COLUMN ranking_scheme, budget_currency`.

v1 rows survive the round trip intact. v2 rows are dropped at step 3. Admins must snapshot `call_rankings` (and `call_ranking_criteria`) before running the down-migration if v2 results need to be preserved for post-mortem.

## Rollout plan

**Week 1 — ship code and schema.** Deploy v2 code + migration. All profiles stay on `v1`. The `rank_pair_v2` path exists but is unused in production.

**Week 2 — admin side-by-side compare.** New `/admin/ranking-compare` page. Admin picks a profile; page forces re-ranking of the last 30 days under both schemes (writes v1 and v2 rows), shows them in a side-by-side table ordered by each scheme's top. Golden-set eval runs weekly on both schemes; report both correlations. Phase 2 (setwise) still dark.

**Week 3 — first v2 profiles.** If v2 golden-set correlation ≥ v1 and admin review of side-by-side is favorable, flip 1–2 profiles to `v2` (via `profiles.ranking_scheme`). User feeds + digests now use v2 for those profiles. Monitor `user_call_dismissals` ratios.

**Week 4 — flip remainder, enable setwise.** If no regressions, flip all remaining profiles to `v2`, enable the setwise daily pass. v1 rows retained for audit indefinitely; removal deferred to a cleanup spec.

**Week 5+ — deprecate v1 path.** `rank_pair_v1` retained but no longer invoked. Removal spec follows after ≥ 3 months of v2-in-production.

## How v2 interacts with existing defenses

- **`must_include` keyword safety net:** unchanged. Applied after v2 aggregation as a force-include override. Must-include calls skip the `timeline` gate too (a user explicitly asked for this topic; let them see it even if the deadline is tight).
- **Borderline section in digest:** unchanged thresholds (45–60). Digest rendering now shows a one-line criterion summary ("Topic 3/5 · Budget unclear · Eligible") — far faster triage signal than v1's free-text rationale.
- **Golden-set drift detection:** expanded. Triples gain per-criterion expected bands. Weekly eval alerts on any criterion drift > 1 Likert point on any pair, not only aggregate drift.
- **`raw_response`:** unchanged — stores full LLM JSON. Now structurally richer, making post-hoc debugging easier.
- **`prompt_version`:** bumps from `"v1"` to `"v2.0"` on ship. Minor version bumps track prompt edits.
- **Dead-letter queue:** new reason `schema_validation_failed_v2` for v2 parse failures; v1 errors keep their existing reason. Distinguished in admin panel.

## Testing

### Unit

- `ranking/criteria.py` — gate evaluation against synthetic budget/TRL/timeline combinations, including all null-combinations → `unknown`.
- `ranking/aggregate.py` — logprob weighting on canned logprob fixtures (no LLM call); all aggregate branches (timeline fail, budget fail, trl fail, eligibility fail, eligibility unclear, exclusions fail, clean pass).
- `ranking/setwise.py` — ordering given canned LLM permutations; skip-when-<4 behavior; offset clamping.

### Integration

- `rank_pair_v2` against a fake Ollama returning canned criterion-level JSON + logprobs.
- Full ingest → rank-v2 → browse flow (seeded profile + fake LLM + Postgres testcontainer).
- Dual-scheme coexistence: same pair scored under v1 and v2 yields two distinct rows.
- Profile scheme flip: `v1` → `v2` triggers backfill; old v1 rows persist.

### Golden set

- Expand the existing ~20 triples with per-criterion expected bands (`topic_fit ± 1`, `eligibility` exact, `exclusions` exact). One-time labeling cost, ~2 hours.
- Weekly eval reports:
  - Aggregate correlation (Kendall's tau) between v1 and v2 scores.
  - Per-criterion hit rate within expected band.
  - Drift alerts per criterion.

### A/B signal (optional, not blocking)

During week 3, hand-label ~30 calls across 3 profiles with "would you act on this?" (yes/no/maybe). Compare v1 vs v2 ordering via Kendall's tau against human labels. Informs future tuning.

## Observability

- `/admin` adds: per-scheme queue depth, per-profile active scheme, weekly golden-set correlation per scheme, counts of hard-fails by reason.
- `/admin/ranking-compare` (week 2+) — side-by-side v1/v2 rankings for a selected profile.
- Per-criterion latency + token counts logged to stdout as structured JSON.

## Risks

1. **Logprob availability in Ollama.** `top_logprobs` support has edge cases on some model/version combinations. *Mitigation:* fall back to argmax integer rating; increment a `logprob_fallback` counter; expose the counter in the admin panel.
2. **Prompt length growth.** ~20% longer than v1. Gemma Q8 on a 2080 Ti with warm prefix cache remains sub-second steady-state; cold starts are ~3–5 s. Acceptable.
3. **Structured-output parse failures.** Rarer than free-form, but still possible. *Mitigation:* one retry with a tightened schema-reminder prompt; dead-letter on second failure (existing v1 machinery).
4. **Setwise instability at group boundary.** With exactly 4 borderline calls, no tournament dimension exists. *Mitigation:* already handled — skip when < 4.
5. **Golden set re-labeling cost.** ~2 hours one-time. Tracked as a rollout-week-1 admin task.
6. **UX change on call detail.** Users accustomed to a single score now see a criterion breakdown. *Mitigation:* aggregate remains the primary displayed number; criterion breakdown appears below the fold on call detail, collapsed by default on the feed.
7. **Eligibility "unclear" cap at 50.** Some profiles might want stricter (cap 40) or more permissive (no cap). *Mitigation:* fixed at 50 in v2; make per-profile if evidence emerges.

## Open questions

1. **Eligibility "unclear" cap — per-profile or global?** v2 starts global (50); revisit if profiles diverge.
2. **Setwise model vs pointwise model.** Currently the same (Gemma). Prometheus 2 would plausibly improve setwise specifically — revisit if setwise ordering underwhelms.
3. **v1 row retention.** Indefinite for now. A cleanup spec after ≥ 3 months of v2 default can prune.
4. **`prompt_version` granularity.** Currently bumps to `"v2.0"`; criterion-level prompt tweaks will use `"v2.1"`, `"v2.2"`, etc. Confirm this cadence is usable by the golden-set drift check.

## Follow-up specs (not in v2 scope)

- **Embedding second-opinion.** Profile and call embeddings (SPECTER2 + SciNCL ensemble, equal-weighted; this is OpenReview's production pattern). Cosine similarity stored on `call_rankings.embed_similarity` (new column). Surface LLM/embedding disagreements in admin as a miss-detection signal. Uses v2's schema without modification.
- **Embedding pre-gate.** Once `embed_similarity` is being computed (per the second-opinion follow-up), promote it to a 4th rule-based gate: if `cosine(call, profile.description) < threshold` short-circuit to `score=0, reason='topic_dissimilar'` before the LLM call. Threshold tuned from production data — typical 0.25–0.35 range for SPECTER2. Cuts LLM call volume on weeks dominated by off-topic source calls (e.g., Vinnova EV/manufacturing calls against hospital profiles). Like the timeline gate (per "How v2 interacts with existing defenses"), the embedding pre-gate is bypassed by `must_include` keyword matches — a user's explicit force-include must not be silently overridden by a low cosine. Depends on the second-opinion spec landing first; threshold cannot be set without v2 production data showing the joint distribution of `embed_similarity` and `topic_fit=0` outcomes.
- **Pre-LLM text pipeline.** PDF/HTML extraction, boilerplate removal, section segmentation (scope / eligibility / budget / deadline). Produces a better 4000-char window for v2's prompt.
- **Profile drift detection.** Quarterly PubMed re-bootstrap; diff against saved `description`; admin "profile may be stale" flag.
- **User feedback loop with per-criterion granularity.** "Mark relevant / not relevant" + optional per-criterion "this was wrong: topic / eligibility / budget / other." v2's schema makes this natural; without it, feedback is coarse.

## References

- Jin, Wang, et al. "Matching patients to clinical trials with large language models." *Nature Communications* (2024). DOI: [10.1038/s41467-024-53081-z](https://doi.org/10.1038/s41467-024-53081-z)
- Liu, Iter, Xu, et al. "G-Eval: NLG Evaluation using GPT-4 with Better Human Alignment." arXiv:[2303.16634](https://arxiv.org/abs/2303.16634), EMNLP 2023.
- Kim, Suk, Longpre, et al. "Prometheus 2: An Open Source Language Model Specialized in Evaluating Other Language Models." arXiv:[2405.01535](https://arxiv.org/abs/2405.01535), EMNLP 2024.
- Zhuang, Liu, Zuccon, Mackie. "A Setwise Approach for Effective and Highly Efficient Zero-shot Ranking with LLMs." SIGIR 2024.
- Sun, Yan, Zhao, et al. "Is ChatGPT Good at Search? Investigating Large Language Models as Re-Ranking Agents." arXiv:[2304.09542](https://arxiv.org/abs/2304.09542), EMNLP 2023 (RankGPT).
- Cohan, Feldman, Beltagy, et al. "SPECTER: Document-level Representation Learning using Citation-informed Transformers." ACL 2020 / SPECTER2 ([AllenAI, 2023](https://allenai.org/blog/specter2-adapting-scientific-document-embeddings-to-multiple-fields-and-task-formats-c95686c06567)).
- Zhu et al. "A novel NIH research grant recommender using BERT." *PLOS ONE* 2023. [Link](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0278636).
