# Collab Slice 3b-1 — Server Hardening + Write-Path Validation (Design)

**Status:** approved (brainstorm 2026-07-03), pending implementation plan.

## Context

Real-time collaborative WYSIWYG proposal editing (server-authoritative Yjs + Tiptap,
markdown-in-git). Foundation slices 0–2c and **Slice 3a backend hardening** are merged
to master (`aeeb2b9`). 3a closed all C1/C2/C3 flag-flip prerequisites (canonical-projection
staleness normalization, section-scoped Yjs replace, the 2c reconciliation-robustness
cluster).

"Slice 3b" (Tiptap frontend + presence + flip the flag ON) is too large for one slice and
is decomposed into:

- **3b-1 (this spec)** — remaining *server-side* hardening + write-path validation. Flag OFF, headless.
- **3b-2** — folded into 3b-1 (the write-path validation, Component 4 below).
- **3b-3** — Tiptap editor + Yjs WebSocket provider (greenfield frontend, flag OFF in prod).
- **3b-4** — awareness / presence / cursors.
- **3b-5** — flag flip / production cutover (CM6 → Tiptap switchover, sticky-forward, flip ON).

3b-1 exists so the frontend (3b-3) and the flip (3b-5) build on a proven server: the log
cannot grow unbounded, the section-map guard cannot be fooled by a compensating divergence,
eviction cannot strand an in-flight op on a zombie room, and Python-generated Yjs updates are
proven to apply correctly in a *real* `y-prosemirror` client.

## Goal

Close four known server-side flip prerequisites, entirely server-side (+ one dev-only Node
harness), so 3b-3/3b-5 can proceed safely.

## Global Constraints

- **The collab flag STAYS OFF in prod.** Every new behavior is reachable only when
  `is_collab_enabled(...)` is true, which prod never sets.
- **Flag-off parity is a hard gate.** The flag-off accept / staleness / spec-lock / citation
  paths and their suites (`tests/proposal_assist/proposals/test_accepting.py`,
  `tests/test_api_spec_lock.py`, member/rubric/soft-delete) MUST stay green **unchanged**.
- **No migration.** Component 1 reuses `yjs_updates` + the existing `last_checkpoint_update_id`
  watermark; Components 2/3 are logic-only; Component 4 is a dev harness.
- **No frontend runtime.** The Tiptap editor is 3b-3. Component 4's Node harness is a
  build/validation script, not shipped UI.
- Run pytest with `EMBEDDING_MODEL=bge-m3`; verify via grouped-dir runs
  (`tests/proposal_assist/collab/`, `tests/proposal_assist/`, `tests/test_api_spec_lock.py`),
  never the whole suite (testcontainers PG flake under load); `-p no:cacheprovider`.
- CI runs `ruff check .` (NOT `ruff format`). Keep imports sorted (I001), avoid nested-`with`
  (SIM117), match each file's existing style.
- Collab tests live under `tests/proposal_assist/collab/`.

---

## Component 1 — yjs-log compaction (compact-on-checkpoint, squash-in-place)

**Problem.** The durable Yjs update log (`yjs_updates`, mig 0059) is append-only and never
compacted. Every hydration (`load_or_hydrate` → `build_doc`) and every read-projection
(`canonical_artifact_markdown` → `store.load_updates` → `build_doc`) replays the **entire**
history. The proposals-list GET (`web/proposals.py:63` → `refresh_staleness` →
`canonical_artifact_markdown` per collab-on artifact) therefore replays the full all-time log
on every load — the unbounded-replay Important the 3a final review deferred.

**Design.** Compact **on checkpoint**, **squash-in-place by reusing the lowest merged-range id**,
**no schema**.

> **Replay order is load-bearing (Codex High, empirically verified).** pycrdt `apply_update` is
> NOT order-independent for a full-state merged update vs the incremental tail: **merged → tail
> converges, but tail → merged does not** (a tail update whose dependency structs live only in the
> merged full-state, applied first to a fresh `Doc`, does not reliably integrate once the merged
> update arrives). Therefore compaction MUST guarantee the merged row replays **before** any tail
> row. A naive "insert the merged update as a new (higher-id) row" breaks this: a client append
> that commits between the snapshot and the insert can take an id **lower** than the merged row,
> so `load_updates` (ordered by `id`) yields `[tail, merged]` → non-convergence.

`checkpoint_artifact` (`collab/checkpoint.py`) already, under the commit mutex, takes an atomic
`store.load_rows` snapshot (returning `(id, actor_id, bytes)` up to a fixed `max_id`), derives the
merged doc + authors, writes the git commit, and advances `last_checkpoint_update_id` to `max_id`.
Extend it so that in the **same transaction** it also compacts, guaranteeing merged-first order by
**id reuse** (never a new high id):

1. `min_id` = the lowest id in the snapshot. **Overwrite** the `update` bytes of the `min_id` row
   with the merged `doc.get_update()` (full state) — this row keeps its id, which is `<=` every
   snapshotted id and strictly `<` every tail row (tail rows have `id > max_id >= min_id`), so the
   merged full-state always sorts first.
2. `DELETE` the OTHER snapshotted rows — scoped to the **snapshotted id set** (`id IN (snapshot ids)
   AND id != min_id`), NOT a blanket `id <= max_id`. A row that commits with an id `<= max_id`
   after the snapshot but was not merged must survive (it replays after `min_id`, on top of the
   merged state its deps are already in — convergent).

Add a `store.compact_snapshot(session, *, workspace_id, artifact, min_id, merged_update, delete_ids)
-> None` helper (overwrite `min_id`'s bytes + delete `delete_ids`), called by `checkpoint_artifact`
inside its existing txn/mutex. No separate commit. `load_updates` and `build_doc` are unchanged (id
order now provably yields merged-first).

**Correctness invariants** (the load-bearing claims — Component-1 is the top Codex-review target):

- **Merged-first replay (the fix).** The merged row reuses `min_id`, so `load_updates` (ORDER BY id)
  returns `[merged(min_id), …surviving rows > min_id]` — always merged → tail. A test asserts
  pre/post-compaction doc equality **and** explicitly exercises the concurrent-append shape (a tail
  row committed during the checkpoint window keeps `id > max_id > min_id`, replays after merged,
  converges).
- **No clobber of an unmerged row.** Delete is scoped to the snapshotted id set (minus `min_id`),
  never blanket `<= max_id`, so a late sub-`max_id` commit is not lost.
- **Watermark alignment.** The compaction floor is exactly `last_checkpoint_update_id` (== the
  snapshot `max_id`). No new column. (`min_id`'s row survives as the merged row; the watermark stays
  an integer high-water mark, not an FK.)
- **No-op guard.** If the snapshot is `<= 1` row (nothing to collapse), skip (do not rewrite a
  single row, never persist a `b""`).

**Trigger.** Checkpoint-only (last-leave + explicit save) — the decision this slice makes.
An active session that never checkpoints keeps a growing tail; bounding that is **deferred to
3b-3** (where Tiptap makes updates high-volume; in 3b-1 the only writers are coarse kernel
accepts — one update each). Idle rooms compact to ~1 row via the last-leave checkpoint.

**Files.** `collab/checkpoint.py` (call the compaction inside the existing txn), `collab/store.py`
(new `compact_snapshot` overwrite+delete helper). No change to `load_updates`/`build_doc`/`canonical.py`
(they benefit transparently).

---

## Component 2 — section-map line-attribution (harden the C2 guard)

**Problem.** 3a's `apply_pm_section_to_fragment` (`yjs_bridge.py`) maps `section_index` to a
top-level block range and guards against a boundary falling *inside* a multi-line block by
comparing **counts**: `len(starts) != len(split_sections(pm_to_markdown(pm_json)))`. As 3a's own
comment notes, a **compensating double-divergence** — one phantom block-boundary AND one hidden
real line-boundary — nets to the same count and slips the check, splicing the wrong section.

**Design.** Replace the count check with **positional attribution**. `split_sections(md)` yields
the line-scan sections (each with `.text`/`.heading`). For each `k`, verify that the markdown
serialization of the block range `[starts[k], starts[k+1])` equals `split_sections(...)[k].text`
(exact text equality; if that proves brittle to trailing-newline normalization, compare on the
canonical `split_sections` of each side). Any per-section mismatch → raise
`SectionMapDivergenceError` (unchanged class — already a `SectionStructureError` subclass in
`_CLEAN_ACCEPT_ERRORS` → non-evicting 400, pc stays pending). A hidden real boundary inside range
`k` makes that range serialize to two line-sections' worth of text ≠ the single expected one, so
it is caught.

**Cost.** O(n) serialization per apply (the block-map pre-pass already serializes each block once;
this adds a per-range comparison). Immaterial at expected block counts.

**Files.** `collab/yjs_bridge.py` (`apply_pm_section_to_fragment`; `SectionMapDivergenceError`
unchanged).

---

## Component 3 — evict-mid-op zombie window (make eviction ops-safe)

**Problem.** `RoomManager.evict(key)` (`collab/room.py`) pops `_rooms[key]` **unconditionally**,
ignoring `room.ops`. If a server `operate()` is in flight (holding the yielded room, `ops > 0`)
when `evict()` runs, the op keeps mutating the now-detached room while a fresh connect builds a
*second* room for the same key — divergence / lost writes. (`operate`/`release`/`drop_client` all
respect the H1 `ops` refcount; only `evict` does not, because its job is immediate poison
burn-down.)

**Design.** **Tombstone + recheck at every concrete site** (chosen over making `evict`
defer-while-ops, which conflicts with evict's immediate-burn-down purpose):

- Add `Room.evicted: bool = False`.
- `evict()` pops `_rooms[key]` (as now, so new connects build fresh) AND sets `room.evicted = True`,
  then closes client sockets.
- The recheck must appear at **each** concrete persist/broadcast site (Codex Medium — a single
  "after `room.lock`" check is not enough because the two broadcasts happen *after* the lock is
  released):
  1. **`Room.handle`** (`room.py`) — check `room.evicted` under `room.lock`, immediately before
     `self.doc.apply_update` / `persist(...)`; if evicted, drop (no apply, no persist, no broadcast).
  2. **kernel accept `_body`** — check before `store.append_update` (under `room.lock`, in the
     threadpool op); if evicted, abort the accept (raise a clean, non-evicting error so the pc stays
     pending — the room is being rebuilt from the log anyway).
  3. **Both post-lock broadcasts** — `room.py:155` (`self._broadcast`) and `kernel_accept.py:93`
     (`room.broadcast`) run *after* `room.lock` is released; each must skip the broadcast when
     `room.evicted` (the sockets are already closed by `evict`; the durable update, if any, is
     safely committed and a fresh connect rebuilds from the log).

**Lock/ordering note.** `evicted` is a plain bool set under `manager._lock` in `evict` and read at
the sites above. The backend is a single-instance asyncio event loop, so there are no torn reads;
the requirement is that each recheck is the **last** thing before its persist/broadcast (no `await`
between the recheck and the action for the under-lock sites). This is the concurrency-sensitive
component; the plan MUST enumerate every persist/broadcast site (the three above are the known set —
the plan re-greps to confirm none are missed) and the plan review (Codex) should scrutinize it.

**Files.** `collab/room.py` (`Room.evicted`, `evict`, rechecks in `handle` + its broadcast),
`proposals/kernel_accept.py` and/or `collab/kernel.py` (recheck before `append_update` + before the
post-lock broadcast).

---

## Component 4 — write-path cross-runtime validation (extend the existing harness)

**Problem.** A real Node/`y-prosemirror` verifier for Python-produced docs **already exists**
(`frontend/scripts/verify_python_writepath.mjs` + `tests/proposal_assist/collab/test_collab_writepath.py`,
REGEN-gated via `REGEN_WRITEPATH=1`, golden `tests/proposal_assist/collab/goldens/python_writepath.json`).
But it only validates **full-state** docs from `pm_json_to_ydoc(...).get_update()`. It does NOT
cover the **incremental** updates from `apply_pm_section_to_fragment` — the section-scoped replaces,
block-count grow/shrink, and especially the `blocks=[]` **delete-all-blocks** clear, which the flag
flip will make real clients depend on. (Codex Low/Med: don't build a parallel harness — extend this one.)

**Design.** Extend the existing pair (same REGEN-gated golden pattern; Node stays out of the pytest CI):

- **Python producer** (`test_collab_writepath.py`): add an incremental corpus. For each case
  (section replace, block-count grow, block-count shrink, `blocks=[]` clear, preamble clear), emit a
  fixture with `{base_update (full-state of the pre-edit doc), incremental_update
  (apply_pm_section_to_fragment output, base64), expected_pm_json}` into the golden artifact
  (extend `python_writepath.json` or a sibling `python_writepath_incremental.json`), regenerated only
  under `REGEN_WRITEPATH=1`. Keep the existing full-state corpus unchanged.
- **Node verifier** (`verify_python_writepath.mjs`): for each incremental fixture, apply
  `base_update` then `incremental_update` into a fresh `Y.Doc`, read back with
  `yDocToProsemirrorJSON(doc, "prosemirror")`, and `deepStrictEqual` to `expected_pm_json` (reusing
  the file's existing normalize + exported per-fixture comparator).
- Run as this slice's validation gate; commit the extended harness + a findings note
  (`docs/superpowers/notes/2026-07-03-collab-slice3b1-writepath-findings.md`). If it surfaces a real
  interop bug (e.g. `blocks=[]` producing an update a real client mishandles), fix it in the Python
  write path before the slice closes.

**Files.** `tests/proposal_assist/collab/test_collab_writepath.py` (incremental producer),
`frontend/scripts/verify_python_writepath.mjs` (incremental verify), the golden artifact, findings
note. Reuses the existing `collab-schema.mjs`/normalize; `yjs`/`y-prosemirror` already in
`frontend/package.json`.

---

## Testing

- **Compaction:** append N updates → checkpoint → assert the log collapsed to `[merged(min_id) +
  surviving tail]`, the merged row reuses `min_id` (sorts first), and `build_doc(load_updates())` /
  `canonical_artifact_markdown` yields a doc **identical** pre/post compaction. **Ordering
  regression:** explicitly assert convergence in the concurrent-append shape — a tail row committed
  during the checkpoint window (id `> max_id`) replays after the merged row and converges (this is
  the exact `[tail, merged]` non-convergence Codex found; the id-reuse design must make it
  `[merged, tail]`). Also: a late sub-`max_id` unmerged row is NOT deleted; idempotent no-op when the
  log is already `<= 1` row; the git checkpoint content is unchanged by compaction.
- **Line-attribution:** a **compensating double-divergence** fixture (one phantom block-boundary +
  one hidden real line-boundary → equal counts, misaligned positions) now raises
  `SectionMapDivergenceError`; the 3a `hard_break + "## …"` phantom case still raises; every
  non-divergent doc (preamble / no-preamble / `##NoSpace` / clear-to-empty) still applies without a
  spurious raise.
- **Zombie-window:** start an `operate()` (ops=1), call `evict()` concurrently, assert the in-flight
  op does NOT persist to the detached room (tombstone recheck aborts) and a fresh connect gets a
  clean room rebuilt from the log; existing evict/operate/release semantics unchanged for the
  no-race path.
- **Write-path:** the extended `test_collab_writepath.py` producer + `verify_python_writepath.mjs`
  verifier pass for the new **incremental** fixtures (section replace, grow, shrink, `blocks=[]`
  clear, preamble clear) alongside the unchanged full-state corpus; findings note records the result.
- **Parity:** the flag-off suites stay green unchanged; grouped-dir sweep + `ruff check .` clean; no
  migration touched.

## Open risks / for plan + Codex review

1. **Compaction delete-set + isolation** (Component 1) — the ordering bug is resolved by id reuse
   (merged row keeps `min_id`, always sorts first; verified against Codex's tail-before-merged
   non-convergence). The residual to pin in the plan: the delete must be scoped to the snapshotted id
   set (minus `min_id`), NOT blanket `<= max_id`, and the snapshot→overwrite→delete must be one
   consistent transaction so a concurrent append is never half-seen. Include the explicit
   concurrent-append convergence test.
2. **Compaction vs a straddling reader** — a `canonical_artifact_markdown` replay that races the
   checkpoint txn: MVCC means the reader sees either the pre- or post-compaction row set atomically;
   confirm the overwrite+delete is one txn and the reader uses one snapshot.
3. **Zombie-window persist-site enumeration** (Component 3) — the three known sites are `Room.handle`
   (before apply/persist), the kernel `_body` (before `append_update`), and both post-lock broadcasts
   (`room.py:155`, `kernel_accept.py:93`). The plan must re-grep to confirm that set is complete; a
   missed site reopens the window.
4. **Line-attribution text-equality brittleness** (Component 2) — trailing-newline / normalization
   differences between a block-range serialization and `split_sections[k].text`; the plan should pin
   the exact comparison (raw text vs re-`split_sections` of each side).
5. **Active-session tail** — explicitly **deferred to 3b-3** (documented, not a 3b-1 gap).
