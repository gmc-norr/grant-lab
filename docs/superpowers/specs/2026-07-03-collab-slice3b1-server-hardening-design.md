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

**Design.** Compact **on checkpoint**, **squash-in-place**, **no schema**.

`checkpoint_artifact` (`collab/checkpoint.py`) already, under the commit mutex, takes an atomic
`store.load_rows` snapshot up to a fixed `max_id`, derives the merged doc + authors, writes the
git commit, and advances `last_checkpoint_update_id` to `max_id`. Extend it so that in the
**same transaction** it also:

1. writes the merged `doc.get_update()` as ONE new `yjs_updates` row (system-authored,
   `actor_id = NULL`), and
2. `DELETE`s all `yjs_updates` rows for this `(workspace_id, artifact)` with `id <= max_id`
   (the rows just merged).

Add a `store.compact_through(session, *, workspace_id, artifact, max_id, merged_update) -> None`
helper (append merged row + delete `<= max_id`), called by `checkpoint_artifact` inside its
existing txn/mutex. No separate commit.

**Correctness invariants** (the load-bearing claims — Component-1 is the top Codex-review target):

- **Replay convergence.** Yjs updates are a CRDT: applying the full set of updates to a fresh
  `Doc` converges regardless of order, and a merged update encodes the same operations as the
  rows it merged. So after compaction `build_doc(load_updates())` — which applies
  `[merged, …tail]` (or `[…tail, merged]` by id order) — is state-equivalent to the pre-compaction
  `build_doc` over `[1..max_id, …tail]`. The merged row taking a *new* (higher) id than the tail
  is therefore safe (order-independence). A test asserts pre/post-compaction doc equality
  explicitly rather than trusting this.
- **No append race.** The snapshot fixes `max_id` under the commit mutex; concurrent client
  appends land with `id > max_id` and are neither merged nor deleted. Delete is scoped
  `id <= max_id`.
- **Watermark alignment.** The compaction floor is exactly `last_checkpoint_update_id` (== the
  snapshot `max_id`). No new column.
- **No-op guard.** If the snapshot is `<= 1` row (nothing to collapse), skip the merge+delete
  (do not rewrite a single row into another single row, and never persist a `b""`).

**Trigger.** Checkpoint-only (last-leave + explicit save) — the decision this slice makes.
An active session that never checkpoints keeps a growing tail; bounding that is **deferred to
3b-3** (where Tiptap makes updates high-volume; in 3b-1 the only writers are coarse kernel
accepts — one update each). Idle rooms compact to ~1 row via the last-leave checkpoint.

**Files.** `collab/checkpoint.py` (call the compaction inside the existing txn), `collab/store.py`
(new `compact_through` + delete helper). No change to `load_updates`/`build_doc`/`canonical.py`
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

**Design.** **Tombstone + recheck** (chosen over making `evict` defer-while-ops, which conflicts
with evict's immediate-burn-down purpose):

- Add `Room.evicted: bool = False`.
- `evict()` pops `_rooms[key]` (as now, so new connects build fresh) AND sets `room.evicted = True`,
  then closes client sockets.
- Every server op that mutates/persists (the kernel accept path via `operate`, and `Room.handle`
  for client updates), **after acquiring `room.lock`**, checks `room.evicted` (equivalently
  `MANAGER.get(key) is not room`) and aborts its persist/broadcast if the room was evicted out from
  under it. A detached room accepts no further writes.

**Lock/ordering note.** The recheck happens under `room.lock` (already held for the mutation), so
it observes a consistent tombstone. `evicted` is set under `manager._lock` in `evict`; the op reads
it under `room.lock` — a plain bool flip/read, no new lock cycle. This is the concurrency-sensitive
component; the plan must enumerate every persist site and the plan review (Codex) should scrutinize
it specifically.

**Files.** `collab/room.py` (`Room.evicted`, `evict`, the recheck in the op/persist sites),
`collab/kernel.py` and/or `proposals/kernel_accept.py` (recheck after `room.lock` before persist).

---

## Component 4 — write-path cross-runtime validation (committed gated Node harness)

**Problem.** The Python write path (`pm_json_to_ydoc`, `apply_pm_section_to_fragment`) has only
been validated **structurally** via pycrdt round-trip (Slice 0/2a) — never against a **real**
browser-grade `y-prosemirror` client. The section-scoped replaces, block-count grow/shrink, and
especially the `blocks=[]` **delete-all-blocks** clear could in principle produce an update a real
`y-prosemirror` client mishandles. The flag flip makes real clients depend on this invariant.

**Design.** A committed, **env-gated** Node harness (matches the existing REGEN-gated goldens
pattern; Node is not in the pytest CI):

- Python side (a test/script under `tests/proposal_assist/collab/` or `frontend/scripts/`) emits
  a fixture bundle: for each case (section replace, grow, shrink, `blocks=[]` clear, preamble clear)
  the base doc + the Python-generated Yjs update (base64) + the expected ProseMirror JSON.
- `frontend/scripts/validate-writepath.mjs` (Node): builds a real `yjs` `Doc`, binds it via
  `y-prosemirror` (using the shared `collab-schema.mjs` twin), applies each Python-generated update,
  and asserts the resulting PM doc equals the expected JSON.
- Gated behind `VALIDATE_WRITEPATH=1` (or an npm script), **not** wired into pytest CI.
- Run once as this slice's validation gate; commit the harness + a findings note
  (`docs/superpowers/notes/2026-07-03-collab-slice3b1-writepath-findings.md`). If it surfaces a real
  interop bug, fix it in the Python write path before the slice closes.

**Files.** `frontend/scripts/validate-writepath.mjs` (+ any small fixture-emitter on the Python
side), findings note. Reuses `frontend/scripts/collab-schema.mjs`; `yjs`/`y-prosemirror` already in
`frontend/package.json`.

---

## Testing

- **Compaction:** append N updates → checkpoint → assert the log collapsed to `[1 merged + tail]`,
  and `build_doc(load_updates())` / `canonical_artifact_markdown` yields a doc **identical** pre/post
  compaction; concurrent-append-during-explicit-save (a row with `id > max_id`) survives; idempotent
  no-op when the log is already `<= 1` row; the git checkpoint content is unchanged by compaction.
- **Line-attribution:** a **compensating double-divergence** fixture (one phantom block-boundary +
  one hidden real line-boundary → equal counts, misaligned positions) now raises
  `SectionMapDivergenceError`; the 3a `hard_break + "## …"` phantom case still raises; every
  non-divergent doc (preamble / no-preamble / `##NoSpace` / clear-to-empty) still applies without a
  spurious raise.
- **Zombie-window:** start an `operate()` (ops=1), call `evict()` concurrently, assert the in-flight
  op does NOT persist to the detached room (tombstone recheck aborts) and a fresh connect gets a
  clean room rebuilt from the log; existing evict/operate/release semantics unchanged for the
  no-race path.
- **Write-path:** the gated Node harness passes for all fixtures (incl. `blocks=[]` clear); findings
  note records the result.
- **Parity:** the flag-off suites stay green unchanged; grouped-dir sweep + `ruff check .` clean; no
  migration touched.

## Open risks / for plan + Codex review

1. **Compaction convergence + ordering** (Component 1) — the merged row taking a higher id than the
   tail relies on Yjs order-independence; the plan must include an explicit pre/post doc-equality
   test, and Codex should confirm no path assumes strict chronological replay.
2. **Compaction vs a straddling reader** — a `canonical_artifact_markdown` replay that races the
   checkpoint txn: MVCC means the reader sees either the pre- or post-compaction row set atomically;
   confirm the delete+insert is one txn and the reader uses one snapshot.
3. **Zombie-window persist-site enumeration** (Component 3) — the plan must list every site that
   persists/broadcasts after `room.lock` and add the `evicted` recheck to each; a missed site
   reopens the window.
4. **Line-attribution text-equality brittleness** (Component 2) — trailing-newline / normalization
   differences between a block-range serialization and `split_sections[k].text`; the plan should pin
   the exact comparison (raw text vs re-`split_sections` of each side).
5. **Active-session tail** — explicitly **deferred to 3b-3** (documented, not a 3b-1 gap).
