# Slice 2b-2: Citation-Insert Accept + Spec-Lock Through the Write Kernel — Notes

**Date:** 2026-07-02
**Branch:** collab-slice2b2
**Feature:** Route the two remaining write paths that still bypassed the room-aware kernel for a **collab-enabled** artifact through the kernel invariant — (a) a `citation_insert` **accept** applies the citation to the LIVE Yjs room and co-commits the evidence-ledger row atomically via the checkpoint; (b) the **spec-lock operation** checkpoints the live room (from the durable log) under the commit mutex and records the fresh checkpoint SHA. The flag-off (git-first) paths stay **byte-for-byte unchanged**; the collab flag stays **OFF in prod** (all new behavior is test-only until Slice 3).

---

## What Shipped

### 1. Shared citation core: `compute_citation_insert` + `_write_citekey_cache` (`tools/citations.py`)

`_insert_citation_locked` was split so the git-first tool path and the collab kernel path share the exact same verify/mint/idempotency/marker logic:

- **`compute_citation_insert(session, *, workspace_id, proposal_text, ledger, ref_uuid, anchor, chunk_index, evidence_quote, stance, confidence) -> dict`** — steps 1–12 of the old function. It is "pure" in the sense that matters: **no commit mutex, no git commit, no disk read/write, and no `ref.citekey` mutation**. It *does* read `Reference`/`ReferenceChunk` and resolve the citekey via the session. Returns `{"new_proposal_text", "new_ledger", "ev_id", "citekey", "ref"}` on success (the session-attached `Reference` is returned so the caller can write the citekey cache post-commit); `{"idempotent": True, "ev_id", "citekey"}` on an idempotent no-op (no `ref`, no cache write); `{"error": "<message>"}` on any verify failure (reference not found / tombstoned / chunk not found / quote not found / ambiguous / anchor not found / ambiguous — exact message strings preserved). It never returns the busy or git-commit-failed sentinels.
- **`_write_citekey_cache(session, ref, citekey, *, owns_txn)`** — today's step-14 `references.citekey` cache write, relocated verbatim. `owns_txn=True` commits (tool path); `owns_txn=False` uses a `begin_nested()` SAVEPOINT + `flush()` so a racing `IntegrityError` cannot poison the caller's accept/kernel transaction. Best-effort: it logs and never raises out (the git ledger is canonical; reconciliation backfills).
- **`_insert_citation_locked`** is now a thin git-first wrapper: `acquire_commit_mutex` → `_read_proposal_and_ledger` → `compute_citation_insert` → `write_and_commit_files` → `_write_citekey_cache(owns_txn=owns_txn)`. Behavior is identical to before (`test_accepting.py` unchanged, including the commit-spy `test_accept_citation_atomic_when_citekey_cache_branch_runs`).

### 2. `checkpoint_artifact(extra_files=…)` — atomic ledger co-commit (`collab/checkpoint.py`)

Additive `extra_files: dict[str, str] | None = None` param. On the committing path only, the files dict becomes `{fname: md, **extra_files}` and goes through the **single** `write_and_commit_files` call — so `proposal.md` (rebuilt from the room) and `citations.json` (the new ledger) land in ONE git commit; a git failure rolls back both. The no-op branches are untouched (a citation accept always stages a real update first, so it never no-ops).

### 3. `kernel_accept_citation` — apply the citation to the live room + co-commit the ledger (`collab/kernel.py`)

Sibling to `kernel_accept_prose`. Flow: `_load_scoped` (FOR UPDATE) → staleness vs the **LIVE section SHA** (never git) → `acquire_commit_mutex` (before minting/reading the ledger, H3 pattern) → read `citations.json` via `load_ledger` (**wrapped in `try/except ValueError: return _stale()`** — a corrupt ledger maps to terminal stale, mirroring git-first, instead of escaping to the evict arm) → `compute_citation_insert` on the room-serialized whole-doc markdown + disk ledger → apply the marker-bearing whole-doc via `apply_pm_json_to_fragment` → `store.append_update(commit=False)` → `checkpoint_artifact(extra_files={CITATIONS_FILENAME: dump_ledger(new_ledger)})` → `_write_citekey_cache(owns_txn=False)` → status flip. **One transaction** (the route's `_body` issues the single `session.commit()`; the kernel only flushes). Verify failures → `stale` (`evict=False`); idempotent → `accepted` at HEAD; success → `KernelAcceptResult(..., evict=True)`.

### 4. Dispatch accept by kind; `CitationKernelDeferred` deleted end-to-end (`proposals/kernel_accept.py`, `proposals/accepting.py`, `web/proposals.py`)

`accept_via_kernel`'s sync `_body` now dispatches by `pc.kind`: `citation_insert` → `kernel_accept_citation`, `prose_edit` → `kernel_accept_prose`, else → **`UnsupportedKernelKind(kind)`** (a new non-evicting error, raised at dispatch BEFORE any room mutation; in `_CLEAN_ACCEPT_ERRORS`, mapped by the route to **422**). The `_load`/`operate`/`room.lock`/broadcast/evict scaffold is unchanged. `CitationKernelDeferred` (the class, its `_CLEAN_ACCEPT_ERRORS` entry, the `pc.kind != "prose_edit"` gate, and the route's 409 branch) is **fully deleted** — `grep -rn CitationKernelDeferred src/ tests/` is zero.

The DB-allowed kinds are `prose_edit`, `citation_insert`, `citation_remove`, `citation_update`; only the first two have producers. So `citation_remove`/`citation_update` (and any unknown kind) hit the `UnsupportedKernelKind → 422` guard rather than falling into `kernel_accept_prose`'s `payload["new_text"]`.

### 5. Spec-lock operation for a collab spec — checkpoint the durable log + record the fresh SHA (`web/api/workspaces.py`)

`post_lock_spec` / `post_unlock_spec` branch on `is_collab_enabled(…, artifact="spec")`. Flag-on: under the commit mutex, `checkpoint_artifact(artifact="spec")` rebuilds the doc from the **durable `yjs_updates` log** (not an in-memory room, so the endpoint stays synchronous — no `MANAGER`/`operate`/`room.lock`/`evict`), and the fresh `cp.commit_sha` is stamped on `SpecLockEvent.artifact_commit_sha`. The empty-check reads disk **after** the checkpoint (post-checkpoint disk == log). Flag-off runs the current body verbatim.

> **This is NOT a room freeze (narrowed invariant, Codex High).** Setting `spec_locked_at` + stamping a checkpoint SHA does not stop a connected WS client from writing (`can_write` is bound at handshake; `room.py` gates on that connect-time value) and the checkpoint reads the durable log, which can lag an update applied to `room.doc` but not yet committed. Both are unreachable in 2b-2 (flag OFF; tests seed the log directly, no live sockets). Live-writer blocking on lock — connect-time `can_write` refresh + `evict()`/disconnect of connected clients — is a **Slice-3** concern (bundled with the already-deferred "`evict()` doesn't disconnect").

---

## Invariants Proven By Tests

- Citation accept staleness vs the **live** section SHA (not git); verify-fail → stale; anchor-moved → stale (global anchor uniqueness on the whole room markdown); idempotent no-op (no new `ArtifactVersion`, `resulting_commit_sha == HEAD`); **corrupt `citations.json` → stale** (room NOT evicted, git unchanged); **citekey cache written on accept** and the whole accept lands in ONE transaction (proven by reading through a *second* connection before the caller commits).
- Marker + ledger row co-commit **atomically** (both in the same HEAD commit; ledger row `ev_id` matches the on-disk marker); exactly one `ArtifactVersion` per committing checkpoint.
- Dispatch: collab `citation_insert` accepts via HTTP (200, marker in checkpointed `proposal.md`, ledger row in HEAD); flag-off citation accept still runs the git-first path; **unsupported kind → 422** (status unchanged `pending`, room NOT evicted, git unchanged).
- Spec-lock: collab lock stamps the **fresh** checkpoint SHA (differs from the prior HEAD; disk matches room content); empty-check reads the room not stale disk; unlock stamps a fresh SHA too; **empty-SHA (unborn repo) → 404** on both lock and unlock; **flag-off parity** (`test_api_spec_lock.py` green unchanged).

**Flag still OFF in prod.** Every new path is guarded by `is_collab_enabled` and exercised only by tests that call `enable_collab`. The prod flag-flip is Slice 3.

---

## Sequencing Deviation From the Plan (controller decision)

The plan assigned the deletion of `CitationKernelDeferred` (class + gate) to **Task 3**, with the two importing modules cleaned in **Task 4**. Deleting the class in Task 3 alone would have left `proposals/kernel_accept.py` and `web/proposals.py` importing a missing symbol, breaking the tree under Task 3's grouped-dir verification (the existing `test_collab_kernel_accept.py` imports `_CLEAN_ACCEPT_ERRORS` from `kernel_accept.py`). So Task 3 was kept **purely additive** (`kernel_accept_citation` + tests, class/gate untouched) and the **entire end-to-end deletion was consolidated into Task 4** (its stated title). The plan's end state is unchanged; only the task boundary moved.

---

## Deferrals (Slice-3 residuals)

- **Spec-lock does NOT freeze a live room** — connect-time `can_write` refresh + `evict()`/disconnect of connected clients on lock is Slice-3 (bundled with "`evict()` doesn't disconnect").
- The direct git-first citation tools (`remove_citation` / `update_citation` / `rename_citekey`) commit immediately and never create a `ProposedChange` — they still bypass the room (Slice-3 write-kernel residual).
- The `citation_remove` / `citation_update` proposal kinds have no producers; the kernel deliberately rejects them (`UnsupportedKernelKind → 422`) rather than building unused paths.
- Slice-3 flag-flip prerequisites carried over from 2b-1: staleness normalized-live-SHA vs raw-git-base-SHA (room-aware propose or hydrate-normalize); whole-fragment replace loses concurrent edits; `evict()` doesn't disconnect clients; `save_section` git-canonical switchover + Tier-1/2 post-checkpoint re-fire.

---

## Verification

- `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/ tests/proposal_assist/proposals/ tests/test_api_spec_lock.py tests/test_api_spec.py -q` → **172 passed**.
- `uv run ruff check .` → clean.
- `cd frontend && EMBEDDING_MODEL=bge-m3 pnpm gen:api` → **no drift** in `frontend/src/api/schema.gen.ts` (no route shape changed — accept route is body-only; spec-lock endpoints keep the same path/response).

No new migration (reuses `yjs_updates`, `collab_artifact_state`, `proposed_changes`, `references`, `reference_chunks`, `spec_lock_events`, and the on-disk `citations.json` ledger). No new deps.
