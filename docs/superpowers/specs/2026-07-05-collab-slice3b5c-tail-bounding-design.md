# Collab Slice 3b-5c — Active-Session Tail-Bounding (Design)

**Date:** 2026-07-05
**Status:** Design — pending user review
**Builds on:** 3b-1 checkpoint/compaction (`e4d5442`), 3b-5b presence (`205f561`)
**Feature flag:** collaborative editing STAYS OFF in prod. 3b-5c does **not** flip it (that is 3b-5d). Because no collab sessions exist in prod with the flag off, the periodic job's scan finds nothing and is a prod no-op until the flip.

---

## 1. Context & Problem

The collab durable truth is the append-only `YjsUpdate` log (one row per persisted Yjs update, per `(workspace_id, artifact)`). A **checkpoint** (`checkpoint.py::checkpoint_artifact`) serializes the log's merged doc to markdown, commits it to git, advances the `last_checkpoint_update_id` watermark, and **compacts** the checkpointed prefix into one merged full-state row (Slice 3b-1). This bounds the replay/hydration cost and storage.

Today a checkpoint fires **only on room eviction** (last client leaves → `RoomManager.release`/`operate` `on_evict`). A **never-ending active session** (clients stay connected for hours) therefore **never checkpoints**: the log tail grows without bound → cold-start hydration (`load_updates` → replay every row) slows, and storage grows. Sessions that crash without a clean eviction leave an orphaned uncompacted tail too.

**3b-5c adds a periodic checkpoint of active/growing tails so the log tail stays bounded regardless of session length.** This is the roadmap's "active-session update-tail bounding" item, scoped as its own slice (the CM6→Tiptap cutover, prod CSP `wss`, and Tier-1/2 post-checkpoint re-fire remain in 3b-5d).

## 2. Architecture-Determining Constraint (and why it simplifies things)

The app's scheduler is a thread-based **`BackgroundScheduler`** ([scheduler.py:147](../../../src/call_manager/scheduler.py#L147)), not an asyncio-loop scheduler. A scheduler job runs in a background **thread** and must **not** touch the event-loop-affine collab `MANAGER` (its `asyncio.Lock`s, rooms, and live `pycrdt.Doc`s).

This is not a limitation here — it is a clean fit. `checkpoint_artifact` **never reads the live room**: it rebuilds its own doc from the **log rows** under the per-workspace commit mutex, entirely as synchronous DB + git work. So a periodic checkpoint is a pure **DB+git** operation, ideal for a background thread, and it also bounds the tail of sessions that crashed without eviction (which a room-driven trigger could not reach).

## 3. Design

### 3.1 Periodic scan job

A new `BackgroundScheduler` interval job (registered alongside the ingest/rank jobs) runs every `COLLAB_CHECKPOINT_SCAN_SECONDS` (default **60s**). Each tick:

1. **Scan** for `(workspace_id, artifact)` pairs whose unwatermarked tail is at least `COLLAB_CHECKPOINT_MIN_TAIL` rows (default **200**): `MAX(YjsUpdate.id)` grouped per `(workspace_id, artifact)`, minus `COALESCE(CollabArtifactState.last_checkpoint_update_id, 0)`, `>= min_tail`. One cheap indexed query in its own short-lived session.
2. **Checkpoint each candidate** in **its own session/transaction**, calling `checkpoint_artifact(..., record_version=False)` then `session.commit()`. Per-artifact isolation: one artifact's git error or rollback never affects the others. Errors are logged and swallowed (best-effort maintenance job).

The scan threshold means idle-but-connected rooms (tail below `min_tail`) are left alone, and lightly-edited artifacts are never pointlessly rewritten; only genuinely growing tails are bounded. The tail is bounded to roughly `min_tail + (write-rate × scan-interval)` — a few hundred rows for this low-write-rate internal tool.

Job registration: `id="collab:checkpoint_tails"`, `trigger=IntervalTrigger(seconds=…)`, `max_instances=1`, `coalesce=True` (a long scan never overlaps or piles up its own next run), a small `misfire_grace_time`. Setting `COLLAB_CHECKPOINT_SCAN_SECONDS=0` disables registration (an explicit off switch).

### 3.2 Quiet checkpoints (`record_version` parameter)

`checkpoint_artifact` gains `record_version: bool = True`. When `False` (the periodic path), it still commits to git, advances the watermark, and compacts — but **skips** adding the `ArtifactVersion` and `collab_checkpoint` `WorkspaceEvent` rows. This keeps the version-history list and Activity feed at today's granularity (a long session does not accrue a version/event per minute); the eviction/manual checkpoint keeps `record_version=True`. Existing callers are unaffected (default `True`).

**Deliberate, documented consequence:** if a quiet checkpoint captures a session's *entire* tail and no edits follow before eviction, the eviction checkpoint is a true no-op (`max_id ≤ watermark`) and records **no** `ArtifactVersion` for that session — the content is still durable in git (the quiet commit) and the draft is current; only the version-list restore-point row is absent. This is the accepted cost of "quiet" and is uncommon (it needs a quiet checkpoint to land with zero subsequent edits). Recorded here so 3b-5d does not treat it as a regression.

### 3.3 Concurrency & correctness

- **Scan-without-mutex is only a candidate filter.** A concurrent append/checkpoint can shift the true tail between the scan and the checkpoint; the checkpoint re-reads the log under the mutex and no-ops if already caught up (`max_id ≤ watermark`). False positive → cheap no-op; false negative → caught next tick.
- **Checkpoint concurrent with live writers is safe** — the same guarantee the eviction checkpoint already relies on. `checkpoint_artifact` snapshots `rows` up to `max_id`; new writes land at ids `> max_id`; the watermark advances to `max_id`; `compact_snapshot` deletes the merged prefix **by explicit id** (never `id <= max_id`), so a row committing after the snapshot is never clobbered. The live room's in-memory doc is untouched (the room hydrates from the log only at cold start), so compaction mid-session is invisible to the running editor; on the next cold start `merged_row + tail` replays to the identical doc.
- **Serialization:** every checkpoint (periodic, eviction, and the live git-first paths) acquires the per-workspace commit mutex, so they can never interleave. A periodic checkpoint racing an eviction checkpoint of the same artifact → one wins, the other no-ops.
- **Single-instance:** the scheduler is already single-instance-gated (`main.py` `scheduler_lock`); even a stray second runner would be serialized by the commit mutex and no-op. No new multi-instance exposure.

## 4. Components / File Structure

- `src/call_manager/proposal_assist/collab/store.py` — ADD `find_checkpoint_candidates(session, *, min_tail) -> list[tuple[uuid.UUID, str]]` (the scan query). Responsibility: log persistence + queries.
- `src/call_manager/proposal_assist/collab/checkpoint.py` — MODIFY `checkpoint_artifact(...)` to accept `record_version: bool = True`; gate the `ArtifactVersion` + `WorkspaceEvent` adds on it. ADD `checkpoint_active_tails(session_factory, *, workspaces_dir, min_tail) -> int` (the job body: scan → per-artifact checkpoint+commit, error-isolated; returns count checkpointed for logging/tests). Responsibility: room→git checkpoint.
- `src/call_manager/scheduler.py` — MODIFY `build_scheduler` to register the interval job when `COLLAB_CHECKPOINT_SCAN_SECONDS > 0`. Responsibility: periodic job wiring.
- Settings (`config`/`settings`) — ADD `collab_checkpoint_scan_seconds: int = 60` and `collab_checkpoint_min_tail: int = 200` (env-tunable; app config, **not** DB schema).
- Tests:
  - `tests/proposal_assist/collab/test_store.py` (or the store test module) — `find_checkpoint_candidates` returns only pairs with tail ≥ threshold; excludes fully-checkpointed and below-threshold pairs.
  - `tests/proposal_assist/collab/test_checkpoint.py` — `record_version=False` commits + advances watermark + compacts but adds **no** `ArtifactVersion`/`WorkspaceEvent`; `record_version=True` (default) still records them (regression pin).
  - `tests/proposal_assist/collab/test_checkpoint_scan.py` (new) — `checkpoint_active_tails` checkpoints an over-threshold artifact (git commit made, tail compacted, no version rows), leaves an under-threshold one untouched, and isolates a failing artifact from a healthy one.
  - `tests/test_scheduler.py` — the job registers with the right id/interval when enabled and is absent when `COLLAB_CHECKPOINT_SCAN_SECONDS=0`.

## 5. Global Constraints

- Feature flag STAYS OFF; add no `enable_collab` source caller. Prod-inert (empty scan).
- **No DB migration, no schema change.** Reuses `YjsUpdate` + `CollabArtifactState`; the new settings are app config.
- Backend-only; no frontend, no HTTP endpoint. Changes confined to `store.py`, `checkpoint.py`, `scheduler.py`, and settings.
- Python 3.12 / `uv` / `ruff` / `pytest` (+ testcontainers). Run grouped-dir with `EMBEDDING_MODEL=bge-m3`, `-p no:cacheprovider`, ALWAYS incl. `tests/test_api_spec.py` and `tests/test_scheduler.py`. Never the whole suite. CI gate is `ruff check .`.
- Explicit-path `git add`. Detached SDD, stop-before-merge, local merge only, not pushed.

## 6. Non-Goals

The flag flip, CM6→Tiptap cutover, prod CSP `wss` allowance, Tier-1/2 (coherence/evaluator) post-checkpoint re-fire, the 3b-5a flag-on deferrals (C#3 stale-snapshot), and the 3b-5b relay-spoof payload-filter — all belong to 3b-5d. 3b-5c is only the periodic tail-bound.

## 7. Risks & Rejected Alternatives

- **Room-driven write-path counter (rejected):** trigger a checkpoint from `room.handle` after N updates. Rejected because it adds logic to the hot write path and cannot run in the background thread; the DB-driven scan is decoupled, needs no hot-path change, and also covers crashed sessions. A pure count-based hard bound is unnecessary given the low write rate — the scan's `min_tail` threshold already bounds the tail well.
- **Compaction without a git commit (rejected):** decoupling log-compaction from the git checkpoint would need a second watermark and duplicate machinery; reusing `checkpoint_artifact` (commit+compact atomic under one mutex) is simpler and proven.
- **Quiet-checkpoint version gap** (see §3.2): accepted, documented; only manifests flag-on and is prod-inert now.
- **Scan cost:** one indexed `GROUP BY` per minute; negligible, and no-op when there are no growing tails (i.e., always, in prod with the flag off).

## 8. Deliverables Summary

| Item | Kind | Files |
|------|------|-------|
| Scan query | backend + test | `store.py`, `test_store.py` |
| `record_version` param | backend + test | `checkpoint.py`, `test_checkpoint.py` |
| Periodic job body | backend + test | `checkpoint.py`, `test_checkpoint_scan.py` |
| Scheduler registration | backend + test | `scheduler.py`, settings, `test_scheduler.py` |
