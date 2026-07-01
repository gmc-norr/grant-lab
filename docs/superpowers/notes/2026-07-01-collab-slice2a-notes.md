# Collab Slice 2a — git↔room bridge + checkpoint layer: Implementation Notes

**Date:** 2026-07-01
**Branch:** collab-slice2a
**Tasks:** 1–6 (all merged onto the branch)

---

## What shipped

- **WS route now flag-gated at connect.** The `/api/ws/workspaces/{id}/{artifact}` route reads `state.is_collab_enabled` inside the connect-time `_access` check (run in a threadpool alongside `resolve_access`) and closes with `1008` (RFC 6455 policy violation) **before `accept()`** when the artifact's collab flag is off. Flag-off is fully dormant: no socket is accepted, nothing is persisted, no room is created. The six Slice-1 connecting tests now `enable_collab` in setup.

- **`collab_artifact_state` flag + checkpoint watermark** (migration 0060). One row per `(workspace_id, artifact)` carries the off-by-default `enabled` flag and the checkpoint watermark (last serialized `yjs_updates.id` + its `commit_sha`). `state.py` owns `is_collab_enabled` / `enable_collab` / `get_checkpoint_watermark` / `advance_checkpoint`.

- **git→room hydration — seed as update #1.** On the first open of a collab-enabled artifact whose Yjs store is empty, `hydrate.load_or_hydrate` reads HEAD markdown, converts it (`markdown_to_pm` → `pm_json_to_ydoc`), and persists that seed as the first `yjs_updates` row (`actor_id=None`). The store is canonical thereafter, so subsequent opens replay from the log, not from git. If the artifact file has never been committed (no HEAD to seed), the room stays Slice-1-empty rather than persisting a synthetic seed.

- **`store.load_rows` — atomic checkpoint snapshot.** A single ordered `SELECT (id, actor_id, update_bytes)` from which the checkpoint derives the doc, `max_id`, and author set together — closing the load-doc-then-read-max TOCTOU that a separate `max_update_id` read would open.

- **Checkpoint core (room→git).** `checkpoint.checkpoint_artifact` self-loads the atomic snapshot, and when `max_id > watermark`:
  - **Atomic snapshot** — doc, watermark comparison, and co-authors all derive from the one `load_rows` result.
  - **Commit mutex** — acquires `acquire_commit_mutex` before any git I/O so a checkpoint serializes against the still-live git-first paths (`save_section`, `human_cite`, agent tools). The mutex is released at the caller's COMMIT/ROLLBACK; `checkpoint_artifact` never commits its own session.
  - **Provenance** — on a content-changing commit it records an `ArtifactVersion` (`created_by = "user"` if any human author else `"system"`) and a `collab_checkpoint` `WorkspaceEvent`.
  - **Co-authorship via watermark** — authors = the distinct non-null `actor_id`s of rows *after* the watermark; a single author tags the event's `actor_id`, multiple authors leave it null and list all in the payload. The git commit message carries `authors:<ids>` (or `system`).
  - Advances the watermark to `max_id` regardless, so an empty (no-op) checkpoint still moves the watermark forward.

- **Checkpoint triggers — last-leave + explicit.**
  - *Last-leave:* `MANAGER.release(..., on_evict=_checkpoint_on_evict)` checkpoints when the last client of a room disconnects.
  - *Explicit:* the `web/collab_checkpoint.py` endpoint checkpoints on demand; `CommitMutexTimeout` → **503** (busy), matching the drafting-busy convention.

- **Node write-path GO.** The deferred Slice-0 cross-runtime direction (Node reads Python-produced bytes) is now validated: a `pycrdt.Doc` built by `pm_json_to_ydoc` and serialized to bytes is read back **identically** by a real browser `y-prosemirror` for all 16 corpus fixtures. See "Write-path validation" below. This is the GO for the Python producer.

---

## Write-path validation (Task 6)

- `pm_json_to_ydoc(doc, *, client_id=None)` gained an additive `client_id` param (forwarded to `pycrdt.Doc`). Default `None` preserves current behavior; existing callers/tests are unaffected.
- Committed artifact `tests/proposal_assist/collab/goldens/python_writepath.json` — a JSON array of `{name, pm_json, update_b64}`, one per fixture, produced by Python (`base64(pm_json_to_ydoc(golden_pm, client_id=1).get_update())`) and regenerable only under `REGEN_WRITEPATH=1`.
- Python producer test `test_collab_writepath.py` validates the committed artifact **in place** (corpus parity, pm_json parity with the goldens, producer round-trip, and that the committed bytes reconstruct the pm_json) — it does NOT regenerate-and-assert-byte-equality.
- Node verifier `frontend/scripts/verify_python_writepath.mjs` (+ its vitest wrapper `frontend/src/writepath.test.ts`) applies the committed bytes into a `Y.Doc` and `deepStrictEqual`s `yDocToProsemirrorJSON(doc)` to `pm_json` for every fixture — order-insensitive, matching the Slice-0 `gen_collab_goldens.mjs` canonicalization. **Result: 16/16 identical — GO.**

---

## Safety invariant — the flag MUST stay OFF in prod until 2b

The `collab_artifact_state.enabled` flag is **off by default and must stay off in production until Slice 2b lands the write kernel.** 2a is a *substrate*, verified only by tests that opt in via `enable_collab`. Enabling it in prod before 2b would let the still-live git-first write paths (`save_section` / `human_cite` / spec-lock) fight checkpoints over the same git blobs.

## 2a checkpoint is provenance-only — the full checkpoint contract is deferred

The Slice-2a checkpoint records **provenance only** (ArtifactVersion + WorkspaceEvent + co-authorship). The design's checkpoint contract also calls for **Tier-1 citation validation** and **Tier-2 claim enqueue** (design line 86) to re-fire on checkpoint; **those are NOT run in 2a.** A whole-doc checkpoint lacks the touched-section identity those tiers need. They move to **2b's write kernel**, which owns the mutating paths and the per-section touch identity. So 2a **does not yet satisfy the full checkpoint contract** — this is an intentional, documented boundary, not an omission.

---

## Deferred items (named, with rationale)

- **Write kernel + accept/cite/spec-lock routing + git-canonical switchover + sticky-forward enforcement → 2b.** 2a keeps the git-first paths live; 2b routes the mutating paths (accept a proposal, add a citation, spec-lock) through the collab kernel, flips git-canonical to store-canonical, and enforces sticky-forward. The Tier-1/Tier-2 post-checkpoint re-fire (above) lands with this kernel.
- **Mid-session permission downgrade / notify → 2c.** A live socket's cached `can_write` is stale if the owner changes a role or the phase transitions mid-session; a server-push re-check is 2c.
- **Debounced-idle checkpoint trigger → follow-up.** 2a checkpoints on last-leave + explicit only; a debounced idle-timer checkpoint (so a long-lived room with a lingering client still flushes to git) is a follow-up.
- **Yjs update-log compaction / squash → follow-up.** `yjs_updates` is append-only; cold-start hydration cost grows with the log. A periodic squash-to-snapshot job is deferred until the table shows measurable growth (carried over from Slice 1).

---

## Caveats

- **Raw HTML is dropped on hydration.** `markdown_to_pm` maps HTML-in-markdown to the collab schema, which has no raw-HTML node; embedded HTML in a HEAD markdown blob is dropped when seeding the room. Acceptable for our proposal markdown (no raw HTML in practice), but noted so a future raw-HTML artifact does not silently lose content on first collab open.

---

## Implementation deviations folded in (for the human)

- **Finding B — last-leave checkpoint hardened to best-effort.** The `_checkpoint_on_evict` handler broadened its exception handling to **log-and-skip on ANY exception** (not just `CommitMutexTimeout`): a teardown-time checkpoint must never let a failure escape the eviction path. The durable Yjs log is unchanged, so the next last-leave (or explicit checkpoint) retries cleanly. The **explicit endpoint keeps its 503** on `CommitMutexTimeout` (the caller can retry). The six retrofitted Slice-1 connecting tests stay repo-free (they don't touch git).

- **Finding D — `markdown_to_pm("")` crashes; hydrate guards it.** An empty / whitespace-only HEAD markdown has no valid ProseMirror `doc` (PM `doc` requires `block+`), and `markdown_to_pm("")` raises. Hydration guards both edges: a **missing artifact file → `[]`** (stay Slice-1-empty, no synthetic seed) and a **blank file → seed a single empty paragraph** (`_EMPTY_DOC`, the canonical empty document, which round-trips to lone-newline markdown on checkpoint). Both branches are covered (`test_hydrates_empty_paragraph_when_head_is_blank`, `test_no_hydrate_when_enabled_but_artifact_file_missing`).

- **Finding E — pycrdt `get_update()` is non-deterministic even with a fixed `client_id`.** Two `pm_json_to_ydoc(pm, client_id=1)` builds produce **different bytes**, so the plan's Step-1 byte-stability unit assertion was **dropped** (the plan anticipated this fallback). The write-path artifact is therefore generated once and committed, and validated **structurally** (corpus parity + pm_json parity + producer round-trip + committed-bytes-reconstruct-pm) — **not** by byte-equality. This keeps git clean (no perpetual-dirty regeneration) and the artifact regenerable only under `REGEN_WRITEPATH=1`. The Node verifier likewise compares with order-insensitive `deepStrictEqual` (one fixture's link-mark attrs differ only in key order between the two runtimes — a benign, non-semantic difference).
