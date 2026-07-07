# Collab Slice 3b-5d·D3 — Post-Checkpoint Tier-2 Prompt Coverage (Design)

**Date:** 2026-07-07
**Slice:** 3b-5d·D3 (fourth sub-slice of the flip family; follows D0 e2e de-risk, D1a toolbar robustness, D1b stale-snapshot 409, D2 awareness relay-spoof filter).
**Status:** Design approved; ready for implementation plan.
**Flip prerequisites remaining after D3:** D4 (THE FLIP, LAST).

---

## 1. Goal & scope

When a collab checkpoint advances a workspace's **draft** git HEAD, promptly enqueue the same per-section **Tier-2 claim-analysis** coverage that the legacy `save_section` path gave every git commit (its step 10) — so the Claims tab and any claim-driven signals reflect the newly-checkpointed draft **without waiting up to a full reconcile interval**. Enqueue only; no LLM runs at checkpoint time (the existing drain tick does the dollar-managed analysis).

**Deliberate exclusions (confirmed with the user):**
- **Tier-1** (the deterministic citation audit, `editing/save.py::_tier1_issues`). It is **pure and transient** — it computes broken-citation + bare-marker issues and returns them in the save response; it persists nothing. The Problems panel (`problems/aggregate.py::assemble_problems`) independently re-derives the same signals deterministically from git HEAD at read time, so a checkpoint's current citation issues already surface on the next Problems poll. **There is no stateful Tier-1 result to refresh → no code.**
- **Manual LLM families** — coherence (`coherence_jobs`), evaluator/scorecard (`evaluator_jobs`), uncited-claims (`uncited_claims_jobs`), review/mock-panel (`review_runs`). All are manual-only (member-gated POST buttons), read git HEAD, and already compute **staleness at read time** by comparing a pinned `evaluated_*_sha` to the live content. They stay manual: the panels show "stale" and the user re-runs when ready. Auto-firing them per checkpoint would breach their deliberate daily/dollar caps and change their manual-by-design intent. Easy to revisit post-flip.

### Why this is the whole job

Tier-2 claim analysis **already self-heals**: `analysis/reconcile.py::reconcile_analysis_coverage` (an APScheduler tick) reads working-tree `proposal.md` = git HEAD, splits sections, and enqueues a claim-analysis job for every HEAD section sha with no queued/running/done job (content-keyed idempotent). So a collab checkpoint's new sections **will** get coverage eventually. The **only** gap versus the save path is **promptness**: `save_section` enqueues immediately at commit; a collab draft would otherwise wait up to one reconcile interval. D3 closes exactly that latency gap — nothing else.

## 2. Existing landscape (verified by exploration)

- **The checkpoint funnel** — `collab/checkpoint.py::checkpoint_artifact(session, *, workspace_id, artifact, workspaces_dir, extra_files=None, record_version=True) -> CheckpointResult`. Single funnel for **all 7 checkpoint triggers** (2 kernel accepts, last-leave/revoke evict, periodic tail-scan, spec lock, spec unlock, explicit save-version endpoint). Every trigger runs on a **worker thread** (Starlette threadpool, sync route, or the APScheduler scan thread) with an **open sync `Session` the caller commits** — `checkpoint_artifact` never commits. At `checkpoint.py:118` it computes `changed = commit_sha != head`; the freshly-serialized markdown `md` (`checkpoint.py:109`) is byte-identical to the new git HEAD (`write_and_commit_files` wrote it); `commit_sha`, `authors`, `record_version` are all in scope. The provenance block (`checkpoint.py:119-135`) already emits `ArtifactVersion` + `WorkspaceEvent` when `changed and record_version`.
- **The enqueue primitive** — `analysis/queue.py::enqueue_section_job(session, *, workspace_id, section_index, section_heading, section_sha, commit_sha) -> bool`. `ON CONFLICT DO NOTHING` on the partial unique index `uq_claim_analysis_jobs_ws_sha_queued (workspace_id, section_sha) WHERE status='queued'`; returns whether a row now exists as a query (so the boolean is reliable regardless of the conflict outcome). This is what `save_section` step 10 (`editing/save.py:302`) and reconcile both call.
- **The reconcile safety net** — `analysis/reconcile.py::reconcile_analysis_coverage(*, session_factory, workspaces_dir) -> int`. Per live workspace: read working-tree `proposal.md`, read HEAD, query covered shas (`_COVERED_STATUSES = ("queued","running","done")`), enqueue each uncovered non-empty section (dedup by sha), one commit at the end. Registered on APScheduler as `_analysis_reconcile_tick`.
- **No analysis today from the checkpoint** — `checkpoint_artifact` enqueues nothing. That empty seam is what D3 fills.

## 3. Architecture — Approach 1 (in-funnel prompt coverage via a shared helper)

One new shared function, called from two places (the checkpoint and reconcile), so there is a single source of truth for "which HEAD sections of this workspace need a claim-analysis job."

### 3.1 Component A — `enqueue_uncovered_sections` (new, `analysis/queue.py`)

```python
def enqueue_uncovered_sections(
    session: Session, *, workspace_id: uuid.UUID, doc: str, commit_sha: str
) -> int:
    """Enqueue a claim-analysis job for every non-empty section of *doc* whose
    content sha has no queued/running/done job. Content-keyed idempotent; dedups
    duplicate-content sections; reads nothing (caller supplies the markdown) and
    does NOT commit (caller owns the transaction). Returns the number inserted."""
```

Body is exactly reconcile's current inner loop, extracted verbatim:
1. `covered = {shas of ClaimAnalysisJob for this workspace with status in _COVERED_STATUSES}` (one query).
2. For each `sec in split_sections(doc)`: skip `not sec.text.strip()`; skip `sec.sha in covered`; else `enqueue_section_job(session, workspace_id=workspace_id, section_index=sec.index, section_heading=sec.heading, section_sha=sec.sha, commit_sha=commit_sha)` and increment on a truthy return; then `covered.add(sec.sha)` (dedup identical sections).
3. Return the insert count.

The `_COVERED_STATUSES = ("queued", "running", "done")` constant **moves** from `reconcile.py` into `queue.py` alongside the helper (its new home, since the coverage loop now lives here). After the refactor `reconcile.py` no longer references it directly, so no import is needed there — one definition, no drift.

### 3.2 Component B — the checkpoint hook (`collab/checkpoint.py`)

Immediately after the provenance block (`checkpoint.py:135`), staged on the same session, before `advance_checkpoint`:

```python
if changed and artifact == "draft":
    # Prompt Tier-2 coverage for the just-committed draft (3b-5d.D3): mirror
    # save_section step 10, funnelled. `md` is the committed HEAD content;
    # idempotent, so covered sections are free no-ops. Gated on `changed`
    # (not record_version) so the silent tail-scan checkpoints are covered too.
    enqueue_uncovered_sections(
        session, workspace_id=workspace_id, doc=md, commit_sha=commit_sha
    )
```

- **`changed` gate, not `record_version`:** any real git advance triggers coverage, including the periodic tail-scan (`checkpoint_active_tails` passes `record_version=False`). The no-op/seed early-returns (`checkpoint.py:87-106`) never reach here (no new commit).
- **`artifact == "draft"` guard:** spec checkpoints get no claim analysis (mirrors `save_section` — spec §3 has no citation validation / claim analysis).
- **No re-read:** uses `md` already in hand.

### 3.3 Component C — reconcile refactor (`analysis/reconcile.py`)

`reconcile_analysis_coverage` keeps its per-workspace file-read + HEAD-read, then delegates its inner loop:

```python
inserted += enqueue_uncovered_sections(
    session, workspace_id=ws_id, doc=doc, commit_sha=head
)
```

One session, one commit at the end — unchanged. This is a **pure, behavior-preserving extraction** (same covered predicate, same skip-empty, same dedup, same enqueue, same per-workspace `covered` query).

## 4. Data flow

```
checkpoint trigger (any of 7, worker thread)
  └─ checkpoint_artifact(session, ...)
       write_and_commit_files → git HEAD advances; md == new HEAD
       changed = commit_sha != head
       [changed and record_version] → ArtifactVersion + WorkspaceEvent   (existing)
       [changed and artifact=="draft"] → enqueue_uncovered_sections(session, md, commit_sha)   (NEW)
       advance_checkpoint; compact prefix
     caller: session.commit()   ← the enqueue commits atomically here
  (later) _analysis_drain_tick claims queued jobs → LLM classify (existing, dollar-managed)

reconcile tick (APScheduler)  ← unchanged safety net
  └─ reconcile_analysis_coverage → per ws: enqueue_uncovered_sections(session, doc=HEAD file, head)
```

## 5. Error handling & threading

- **Thread-safe by construction.** `enqueue_uncovered_sections` is pure DB DML on the supplied sync `Session` — no LLM, git, file IO, or asyncio, and it never touches the room `MANAGER`. Safe on the Starlette threadpool (checkpoint call sites 1–3, 5–7) and the APScheduler scan thread (site 4).
- **No try/except inside `checkpoint_artifact`.** Like the existing `ArtifactVersion`/`WorkspaceEvent` writes, the enqueue participates in the checkpoint transaction and fails-or-commits together with them; a bare `INSERT … ON CONFLICT` only fails under a broader DB fault that would already fail the whole commit. This matches `save_section` step 10 (no wrapper there either).
- **Git-ahead-of-DB window is pre-existing and self-healing.** The enqueue runs after `write_and_commit_files`, so on a caller rollback git is ahead of a DB with no coverage rows — the **same accepted window** the provenance writes already have. Reconcile enqueues the missing coverage on its next tick. The scan path additionally isolates + rolls back + logs per artifact (`checkpoint.py:191-201`), so a scan-path enqueue failure is contained.

## 6. Testing

**Helper unit** (`tests/proposal_assist/analysis/` alongside the queue/reconcile tests):
- enqueues one job per uncovered non-empty section; **skips empty** (blank preamble); **dedups** duplicate-content sections (one job for two identical shas); **skips covered** (a section with an existing queued/running/done job); returns the insert count; **does not commit** (rows visible in-transaction, gone after `session.rollback()`); **idempotent** (a second identical call returns 0).

**Reconcile regression:** the existing `reconcile_analysis_coverage` tests pass unchanged — proves the extraction is behavior-preserving (the flag-off-sensitive piece).

**Checkpoint integration** (`tests/proposal_assist/collab/`):
- a **DRAFT** checkpoint that changes git enqueues coverage for the new sections — `ClaimAnalysisJob` rows exist for the new shas, pinned to the checkpoint `commit_sha`;
- a **SPEC** checkpoint enqueues nothing;
- a **no-op/seed** checkpoint (`max_id <= watermark`, or the unauthored-seed early-return) enqueues nothing;
- a **repeat** checkpoint with no content change enqueues nothing (`changed=False`);
- the **periodic scan path** (`record_version=False`, via `checkpoint_active_tails` or a direct `record_version=False` call) also enqueues coverage — proving the `changed`-not-`record_version` gate.

**Acceptance gates:** grouped backend sweep incl. `tests/test_api_spec.py` (`EMBEDDING_MODEL=bge-m3`, `-p no:cacheprovider`, never whole-suite) · `ruff check .` · frontend `pnpm exec tsc --noEmit` + `pnpm exec vitest run src/collab` (regression only — no frontend change) · two-browser e2e (a–e) via `scripts/collab_e2e_run.sh` as a checkpoint-path regression gate (scenario (a) exercises a real checkpoint).

## 7. Global constraints (verbatim)

- Feature flag **stays OFF** in prod until the explicit 3b-5d flip (D4). All checkpoint paths are collab-gated → the checkpoint hook is prod-inert until D4. The reconcile refactor runs in prod today (the always-on Tier-2 safety net) and **must be a pure, behavior-preserving extraction** — this is D3's flag-off parity crux.
- **No migration, no schema change** — reuses `ClaimAnalysisJob` + `enqueue_section_job`. Backend-only; frontend untouched.
- No new locks; no lock-ordering changes (the enqueue runs inside the existing checkpoint transaction/commit-mutex, which already serializes the read→commit→advance sequence).
- Explicit-path `git add` only; merges are **local `--no-ff` only**, not pushed without an explicit ask; do NOT invoke `/code-review` / `/ultrareview`; Codex findings are authoritative-but-verify-against-code.
- Commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

## 8. Key references

- `src/call_manager/proposal_assist/collab/checkpoint.py:54-162` — the checkpoint funnel; hook goes after the provenance block (`:135`), `changed` at `:118`, `md` at `:109`.
- `src/call_manager/proposal_assist/analysis/queue.py:29` — `enqueue_section_job` (the idempotent primitive); new `enqueue_uncovered_sections` lands here.
- `src/call_manager/proposal_assist/analysis/reconcile.py:28-85` — `reconcile_analysis_coverage` (the loop to extract from; the always-on safety net).
- `src/call_manager/proposal_assist/editing/save.py:296-309` — the save-path Tier-1/Tier-2 hooks this mirrors (step 10 = `enqueue_section_job`).
- `src/call_manager/proposal_assist/problems/aggregate.py:65` — `assemble_problems` (why Tier-1 needs no re-fire: it re-derives citation issues at read time).
