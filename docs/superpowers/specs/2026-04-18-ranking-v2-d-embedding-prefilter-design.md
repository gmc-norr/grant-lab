# Ranking v2 — Plan D: Embedding Pre-filter (Design)

> **Status:** design draft, 2026-04-18. Plans A/B/C are merged to `master` but nothing is in production yet. This spec proposes integrating embeddings as a first-class part of the v2 ranking pipeline — *not* a bolt-on retrofit.

> **Companion spec:** [2026-04-17-ranking-v2-criterion-design.md](./2026-04-17-ranking-v2-criterion-design.md) — current v2 design (rule-based gates + LLM rubric + setwise). This spec extends that pipeline with a 4th gate.

## 1. Problem statement

The current v2 pipeline runs the full Gemma 4 LLM rubric on every `(call × profile)` pair that passes the three rule-based gates (timeline, budget, TRL). On the 2080 Ti at Q4_K_M, each rubric call is ~10 s. At steady state for 15 profiles × ~35 new calls/week that's ~90 min of GPU time per week — manageable. But:

- A profile flip (`v1 → v2` via `/profiles/<id>/edit`) triggers a 90-day backfill: 15 profiles × ~450 calls = ~6,750 LLM calls = ~19 hours of GPU time.
- Vinnova's source mix is heavily skewed to topics the hospital does not care about (EV charging, manufacturing, industrial process automation). For a hospital research profile, 60–80% of incoming Vinnova calls are obviously off-topic.
- LLM judgment on obviously-off-topic content is wasted compute *and* introduces noise: Gemma at Q4 sometimes scores genuine off-topic calls in the 30–45 range rather than the floor 0–10, polluting the borderline band.

A cheap semantic-similarity pre-filter solves all three.

## 2. Design intent (one-line)

**Compute one BGE-M3 embedding per call (on ingest) and one per profile (on description change). At rank time, cosine-gate the LLM call: if `cosine(call, profile) < threshold`, short-circuit to `score = 0, reason = "topic_dissimilar"`. The embedding gate becomes the 4th rule-based gate alongside timeline/budget/TRL.**

The reranker (BGE-reranker-v2-m3) is a separate, optional precision step documented in §9 but not required for Plan D's first deliverable.

## 3. Architecture decisions (FIX-NOW; push back if you disagree)

- **Embedding model: BGE-M3.** 1024-dim, multilingual (matters because Vinnova content is partly Swedish), strong general-purpose semantic similarity. Available via Ollama (`ollama pull bge-m3`) — reuses our existing infrastructure, no new model server. SPECTER2/SciNCL (the v2 spec's original suggestion) are scientific-paper-specialized and English-only; BGE-M3's general+multilingual coverage is the better fit for funding-call vs research-profile matching.
- **Serving via Ollama**, not in-process `sentence-transformers`. Reuses the existing `OllamaClient` HTTP path (extend it with an `embed()` method). Keeps GPU model management in one place. No new container or process supervision.
- **Storage: `pgvector` extension.** Use the native `vector(1024)` type. Cheaper than `bytea` for cosine, and future-proofs Phase 2 reranker shortlisting via SQL `ORDER BY embedding <=> profile_embedding LIMIT N`. Needs `CREATE EXTENSION vector;` in a migration; testcontainers needs the `pgvector/pgvector:pg16` image.
- **Embedding gate runs LAST among rule-based gates.** Timeline/budget/TRL are pure-Python checks taking microseconds; the embedding gate requires a vector pull from Postgres + a cosine. Run cheap checks first so an early FAIL avoids the cosine entirely. (No LLM call is involved at gate-evaluation time — the embedding pre-fetch is the only cost.)
- **Threshold: configurable, default `EMBEDDING_PREGATE_THRESHOLD = 0.30`.** Empirical BGE-M3 cosines for clearly off-topic pairs sit around 0.15–0.25; on-topic pairs sit at 0.40+. 0.30 is a defensive (low) cutoff — it should fire only on obviously-irrelevant content. Validation comes after we have production data; until then, the goal is *no false-FAILs*, accepting that some clear-FAILs slip through to the LLM.
- **`must_include` bypass applies to the embedding gate**, the same way it bypasses the timeline gate (per the existing v2 spec line 388 logic). A user's explicit force-include keyword must not be silently overridden by a low cosine.
- **Embedding refresh policy:**
  - On call ingest → orchestrator schedules an embedding compute for the new call (batched per ingest tick).
  - On profile create / edit → handler synchronously recomputes the profile embedding (description-only; structured rubric is not embedded).
  - One-time backfill on Plan D deploy → Alembic data migration computes embeddings for all existing rows.
  - Daily 03:30 cron tick → scans for null embeddings and computes them (recovery from missed ingests, model downtime, etc.).
- **`embed_similarity` is stored on every CallRanking row, even when the gate didn't trigger.** Surface it in admin (call detail page + ranking-compare table). Cheap to write, valuable for tuning the threshold and for spotting LLM/embedding disagreements (high cosine + low LLM topic_fit, or vice versa) as a miss-detection signal.
- **No new ranking scheme.** Embeddings are an extension of v2, not v3. Same `ranking_scheme = 'v2'` value; same `prompt_version = 'v2.0'` (since the LLM prompt is unchanged). The pipeline behavior changes; the schema's scheme identifier doesn't.

## 4. Schema changes

```sql
-- Migration: 0008_embeddings.py
CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE calls           ADD COLUMN embedding vector(1024);
ALTER TABLE profiles        ADD COLUMN embedding vector(1024);
ALTER TABLE call_rankings   ADD COLUMN embed_similarity double precision;

-- Helpful for the daily refresh-missing tick.
CREATE INDEX ix_calls_embedding_null    ON calls    (id) WHERE embedding IS NULL;
CREATE INDEX ix_profiles_embedding_null ON profiles (id) WHERE embedding IS NULL;
```

No reranker columns (Phase 2). No criterion-level embeddings (out of scope, §10).

## 5. Pipeline integration

### 5.1 `criteria.py` — add 4th gate

```python
class GateStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"

@dataclass(frozen=True)
class Gates:
    timeline: GateResult
    budget:   GateResult
    trl:      GateResult
    embedding: GateResult     # NEW
```

`evaluate_gates(profile, call, *, now)` gets a new optional `embed_similarity: float | None` parameter:

```python
def evaluate_gates(profile, call, *, now, embed_similarity=None) -> Gates:
    return Gates(
        timeline=_timeline(call.deadline, now),
        budget=_budget(profile, call),
        trl=_trl(profile, call),
        embedding=_embedding(embed_similarity),
    )

def _embedding(sim: float | None) -> GateResult:
    if sim is None:
        return GateResult(GateStatus.UNKNOWN)        # missing → fall through to LLM
    if sim < EMBEDDING_PREGATE_THRESHOLD:
        return GateResult(GateStatus.FAIL, reason="topic_dissimilar")
    return GateResult(GateStatus.PASS)
```

### 5.2 `aggregate.py` — add gate short-circuit

```python
def aggregate(gates, llm, *, must_include_override=False) -> AggregateResult:
    if not must_include_override and gates.timeline.status == GateStatus.FAIL:
        return AggregateResult(0, "timeline")
    if gates.budget.status == GateStatus.FAIL:
        return AggregateResult(0, "budget")
    if gates.trl.status == GateStatus.FAIL:
        return AggregateResult(0, "trl")
    if not must_include_override and gates.embedding.status == GateStatus.FAIL:
        return AggregateResult(0, "embedding")        # NEW
    # ... existing LLM-gate logic unchanged
```

### 5.3 `rank_pair_v2` — fetch similarity, store it, skip LLM on FAIL

```python
def rank_pair_v2(*, client, profile, call, now) -> RankResultV2:
    sim = _cosine_or_none(profile.embedding, call.embedding)
    must_include_match = _call_matches_must_include(profile, call)
    gates = evaluate_gates(profile, call, now=now, embed_similarity=sim)

    # Hard-fail short-circuits (budget/TRL unconditional, others conditional)
    for gate_name in ("budget", "trl"):
        if getattr(gates, gate_name).status == GateStatus.FAIL:
            return _gate_fail_result(gates, gate_name, must_include_match,
                                      embed_similarity=sim)
    if gates.timeline.status == GateStatus.FAIL and not must_include_match:
        return _gate_fail_result(gates, "timeline", must_include_match,
                                  embed_similarity=sim)
    if gates.embedding.status == GateStatus.FAIL and not must_include_match:
        return _gate_fail_result(gates, "embedding", must_include_match,
                                  embed_similarity=sim)
    # ... LLM call as today, with sim threaded through to result
```

`_cosine_or_none` returns None if either embedding is missing (graceful degradation). `embed_similarity` is written to the `CallRanking` row in *all* paths — pre-LLM short-circuits, gate-bypass-via-must-include, and clean LLM completion — for diagnostic surfacing.

### 5.4 `worker.py` — surface gate reason

The `_v2_summary` rationale builder gets one new branch:

```python
if r.aggregate_reason == "embedding":
    sim_str = f"{r.embed_similarity:.3f}" if r.embed_similarity is not None else "n/a"
    return f"FAIL: topic_dissimilar (cosine {sim_str} < {EMBEDDING_PREGATE_THRESHOLD})"
```

### 5.5 New module: `src/call_monitor/embeddings/`

```
src/call_monitor/embeddings/
├── __init__.py
├── client.py        # OllamaEmbedClient — POST /api/embed wrapper
├── refresh.py       # refresh_call_embedding(), refresh_profile_embedding(),
│                    #   refresh_missing_embeddings_tick()
└── similarity.py    # cosine(a, b) -> float; helper for None-handling
```

`OllamaEmbedClient` shares the `OllamaClient`'s base URL, transport, and timeout settings — extends it via composition so tests can mock with `httpx.MockTransport` the same way.

### 5.6 Scheduler

Add to `build_scheduler`:

```python
sched.add_job(
    _embedding_refresh_tick,
    trigger=CronTrigger(hour=3, minute=30),       # before setwise:daily at 04:00
    id="embeddings:refresh-missing",
    name="Daily backfill of missing embeddings",
    replace_existing=True, max_instances=1, coalesce=True,
)
```

Also: extend the existing `vinnova:hourly` and `eu:hourly` orchestrator hooks to enqueue per-call embedding computes immediately after upsert.

## 6. Embedding refresh — operational details

### 6.1 What gets embedded

- **Call**: `title + "\n\n" + (summary or "") + "\n\n" + (full_text[:8000] or "")`. Truncate full_text to 8000 chars (BGE-M3 max input is 8192 tokens; 8000 chars is a safe under-estimate).
- **Profile**: `description` only. The structured rubric (`areas`, `must_include`, `hard_exclusions`) is NOT included in the embedding — those fields are exact-keyword mechanisms, not semantic ones, and embedding them would dilute the description's signal.

### 6.2 When it happens

| Trigger | Path | Sync/async |
|---|---|---|
| Call ingested via `vinnova:hourly` or `eu:hourly` | Orchestrator calls `refresh_call_embedding(call_id)` per new call | Synchronous within the ingest tick |
| Profile created or `description` field edited | `profile_create_submit` / `profile_edit_submit` calls `refresh_profile_embedding(profile_id)` | Synchronous within the request handler |
| Daily 03:30 tick | `refresh_missing_embeddings_tick`: `SELECT id FROM calls WHERE embedding IS NULL LIMIT 200`; same for profiles | Sync within the tick |
| One-time on Plan D deploy | Alembic data migration scans both tables and embeds in batches of 50 | Migration runtime |

### 6.3 Failure mode — Ollama down or model missing

`refresh_*_embedding` raises; caller (orchestrator, handler, tick) catches and logs `embedding_compute_failed`. The `embedding` column stays NULL. Next-day refresh tick catches the NULL and retries. The pre-gate sees NULL and returns UNKNOWN — falls through to LLM. **Embeddings are best-effort: if the service is down, ranking still works, just without the LLM-savings benefit.**

## 7. Threshold rationale

`EMBEDDING_PREGATE_THRESHOLD = 0.30` (BGE-M3 cosine).

Empirically, BGE-M3 cosines for our domain tend to fall as:

| Pair type | Cosine range |
|---|---|
| Clearly off-topic (e.g. "EV charging infrastructure" vs hospital pathology profile) | 0.10 – 0.22 |
| Marginally relevant (e.g. "AI in industry" vs hospital AI profile) | 0.25 – 0.35 |
| On-topic (e.g. "Cancer biomarker discovery" vs cancer research profile) | 0.40 – 0.55 |
| Direct match | 0.55 – 0.80 |

A 0.30 cutoff means: pairs at 0.25 (marginal) still get the LLM — they're the borderline cases where LLM judgment matters most. Pairs at 0.20 or below get auto-zeroed — they're the ones where the LLM was wasting compute anyway.

The threshold is configurable via env var so a deployment can tighten it (e.g. to 0.35) once we have the joint distribution of `(embed_similarity, llm_topic_fit)` from production.

## 8. Performance expectations

| Metric | Today (v2 LLM-only) | After Plan D |
|---|---|---|
| Per-call embedding compute | n/a | ~50 ms (BGE-M3 on 2080 Ti via Ollama) |
| Per-rank pre-gate cost | 0 | ~5 ms (one DB SELECT for the call's embedding + cosine in Python) |
| LLM calls per profile flip backfill (15 profiles, 450 calls/profile) | 6,750 | ~2,000 – 3,000 (estimate: 60–70% of pairs filtered by 0.30 cutoff) |
| Wall time per backfill | ~19 hours | ~6–8 hours |
| Steady-state weekly LLM time | ~90 min | ~30–40 min |
| Total embedding compute (90-day backfill) | n/a | ~22 min (one-time + per-ingest) |

These are estimates; real numbers come from running Plan D against the historical Vinnova corpus.

## 9. Phase 2 (optional): BGE-reranker-v2-m3 shortlist

If Phase 1 cosine pre-gate doesn't reduce LLM volume enough on big backfills (it should, but if e.g. someone configures the threshold conservatively at 0.20 it might not), add a cross-encoder shortlisting step:

1. After cosine pre-gate passes a candidate set (cosine ≥ threshold), select per-profile **top-50 by cosine score**.
2. Run BGE-reranker-v2-m3 on those 50 candidates as `(profile.description, call_text)` pairs. Reranker returns a relevance score per pair. Cost: ~100 ms per pair on the 2080 Ti, ~5 sec per profile per shortlist.
3. LLM rubric runs on **top-15 by reranker score** only. Lower-ranked pairs in [16, 50] get an `aggregate_score` derived from the normalized reranker score (mapped to [10, 55]) so they appear in calls index but never reach top/borderline bands.

Phase 2 is design-only here. Implement if Phase 1 numbers don't hit the targets in §8.

## 10. Out of scope (deferred to follow-up specs)

- **SPECTER2 + SciNCL ensemble** (the original v2 spec's choice). BGE-M3 is simpler and good enough; switch is one model-config change if needed.
- **Per-criterion embeddings** (separate embeddings for `areas`, `hard_exclusions`, `must_include` items individually). Adds complexity for marginal value at our scale.
- **Embedding-similarity surfacing in `/admin/ranking-compare`** as a dedicated column. Plan D writes the value to the column; surfacing in the compare table can be a one-line follow-up plan.
- **Cross-language embedding fine-tune** (e.g. for Swedish-medical jargon). BGE-M3's stock multilingual training is adequate for v1.
- **Profile-level embedding cache invalidation on rubric edits.** Rubric structure (areas/must_include/hard_exclusions) doesn't change the embedding (we only embed description), so no cache invalidation needed for rubric edits.

## 11. Decision log

| Question | Decision | Why |
|---|---|---|
| One model or ensemble? | One: BGE-M3 | Simpler operational footprint; ensembles add infra without proportional quality at this scale |
| Where to serve? | Existing Ollama instance | No new model server; BGE-M3 supported by Ollama natively |
| Storage type? | `pgvector vector(1024)` | Native cosine, future-proofs Phase 2 |
| Sync vs async refresh? | Sync within the trigger | Embedding compute is cheap (~50 ms); not worth a queue for v1 |
| What text to embed for calls? | title + summary + full_text[:8000] | Full context within model input limits |
| What text to embed for profiles? | description only | Structured rubric is keyword-exact, not semantic |
| Default threshold? | 0.30 | Defensive — biased toward keeping borderline calls in the LLM path |
| Reranker now? | No, Phase 2 | Cosine pre-gate alone should hit targets; add only if needed |
| New ranking_scheme value? | No, still v2 | Behavior change, not output-shape change |

## 12. Open questions for the implementation plan

- **Migration path for in-flight v2 rankings.** Existing `call_rankings` rows have `embed_similarity = NULL`. Should the deploy migration backfill them by re-running the gate (read existing aggregate_score, decide whether to invalidate)? Or accept that historical rows have no similarity recorded? — Lean toward "leave historical NULL; new ranks have the column populated".
- **What if an admin adds a new profile when 1000 calls already exist?** Profile-create handler currently doesn't trigger backfill (Plan A's `backfill_profile` runs only on edits to active profiles). Plan D may or may not need to add per-call cosine-gate evaluation as part of the new-profile setup — depends on whether we want immediate borderline-band population.
- **Versioning the embedding model.** If we later switch from BGE-M3 to a different model, all stored embeddings become incomparable. Add an `embedding_model` column (varchar) on both `calls` and `profiles`? — Probably yes; cheap to add now, expensive later.
