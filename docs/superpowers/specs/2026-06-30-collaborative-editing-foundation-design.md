# Collaborative Proposal Editing — Foundation (Real-time WYSIWYG)

- **Date:** 2026-06-30
- **Status:** Design — pending review
- **Scope:** Sub-project #1 of a larger program (see [Roadmap](#out-of-scope--roadmap)). This spec covers **only** the collaborative WYSIWYG editing foundation.

## Background & motivation

Today the proposal-assist **cockpit** edits a draft one **section at a time**: the full-document view is read-only, only one section is editable at once, and there are no structural operations (add / move / split / merge are "chat-only"). This surfaced as a concrete usability complaint — switching to the "Section" view always opens the *top* section rather than the one the user is working in, because the Section⇄Full toggle deliberately never crosses the sidebar boundary and there is no cursor→section mapping.

Investigating that complaint opened a larger design question: **how should working on a proposal be designed?** Two needs emerged:

1. **Edit the whole proposal freely** (not section-locked), including moving text across section boundaries, and adding/removing sections.
2. **True multi-user editing** — the project is heading toward multiple people working on the same proposal, and a section-locked, single-author, optimistic-concurrency model does not get there.

A key reframe from tracing the backend: the system is **already "document is the source of truth"** — each artifact is a single markdown file in a per-workspace git repo, and "sections" are *derived on the fly* by splitting on `##` (H2) headings (`split_sections`, byte-identical round-trip). The rigidity is in the **edit UX** and in a few subsystems that key off the fragile **section *index***, not in storage. That makes the foundation a UX + real-time + reference-stability effort rather than a storage rewrite.

Usability requirement: non-developer users (clinicians, researchers) must **not** have to write raw markdown. The editing experience must be WYSIWYG; markdown stays under the hood as the storage/interchange format.

## Goals

- Replace the section-locked CodeMirror editor with a **collaborative, WYSIWYG editor over the whole artifact** (draft, and spec).
- **Real-time multi-user co-editing**: owner + members edit the draft simultaneously, free-for-all, everyone an equal editor.
- Keep **markdown-in-git as the durable source of truth** for everything the backend pipeline reads (agent, citation audit, evaluator, coherence, activity feed).
- Preserve existing behavior with **no functional regression** — including atomic proposal accept, citation *creation* (select-to-cite), provenance, and version history. (Citations *render* read-only in #1; creation is preserved — see [Citations in #1](#citations-in-1).)
- Be **branch-compatible by construction** so later sub-projects (#4 agent-against-live-doc, #5 branching) build on this without rework.

## Non-goals (explicitly out of this sub-project)

- Structure operations as editor gestures (add/split/merge/reorder) and **stable section identity** — that is #3.
- **Interactive citation *nodes*** (click-to-peek, drag, node-anchored offsets) — #1 keeps citation *creation* and read-only pill rendering, but full node-ification is #2.
- **Autonomous server-side writes** (agent editing the live doc with *no human present*), **richer proposal merge**, and **branch/merge** — #4 and #5. (Note: #1 *does* include a **constrained, human-initiated** server-write — atomic proposal accept into the live room; see below.)
- **Offline editing** — online-first for v1 (the architecture leaves the door open via Yjs).
- Horizontal scaling of the sync layer — single-instance is acceptable for now (see [Risks](#risks--spikes)).

## Decisions (and why)

| Decision | Choice | Rationale |
|---|---|---|
| Real-time engine | **Yjs** (CRDT) | De-facto standard, most mature, best editor bindings; binds to our stack via `y-prosemirror`. |
| Editor | **Tiptap** (on ProseMirror) | WYSIWYG that serializes to markdown; headless → full control for our **custom citation node** and section behavior (which BlockNote constrains). Battle-tested ProseMirror lineage. |
| Storage format | **Markdown in git** (unchanged) | Keeps the entire existing pipeline working; users never see markdown. |
| Sync server | **`pycrdt` + `pycrdt-websocket` in FastAPI** | Same CRDT stack as Jupyter real-time collaboration; keeps the backend **pure Python** (no Node sidecar). |
| Source of truth | **Server-authoritative (Approach 1)** | Yjs update-log canonical for the *live* doc; git is a checkpoint stream derived from it server-side. Only model that cleanly supports the #4/#5 roadmap and client-independent checkpoints. |
| Serialization locus | **Pure Python, server-side** | `pycrdt` XmlFragment → ProseMirror JSON (port of `yXmlFragmentToProsemirrorJSON`) → `prosemirror-py` → markdown. No second runtime. |
| Persistence/provenance | **Hybrid** | Continuous Yjs update-log persistence (durability/recovery) + **git commits at meaningful checkpoints** (idle/blur debounce + explicit "Save a version"), authorship derived from the **server-side authenticated-actor log** (not client awareness). |
| Collaboration scope | Draft: owner + members, free-for-all equal editors. Setup phase: owner-only. Spec: co-editable, **owner can lock** (extends `spec_locked_at`). | Per product decision. |
| Agent/proposal in #1 | Agent **proposes**; **accept is applied server-side, atomically** via the room-aware write kernel. A **constrained** server-write lands in #1; autonomous (no-human) edits + richer merge are #4. | Preserves today's atomic accept contract (`accepted` ⇒ `resulting_commit_sha` set and room/git agree) and keeps the live room canonical. |

## Architecture

### Components

1. **Tiptap editor (frontend, React 19 + Mantine).** A constrained schema (see below). Mounts per `(workspace, artifact)`. Replaces the CodeMirror section editor and the Section/Full toggle. The **Outline sidebar remains** as navigation — clicking a heading scrolls to it in the single document (this resolves the original "opens the top section" bug as a side effect).
2. **Yjs document + `y-prosemirror` binding (frontend).** One `Y.Doc` per artifact; the ProseMirror doc lives in the `'prosemirror'` `Y.XmlFragment`. `y-prosemirror`'s `ySyncPlugin`, `yCursorPlugin` (remote cursors / presence), and `yUndoPlugin` (per-user undo).
3. **Sync server (`pycrdt-websocket` in FastAPI).** A websocket endpoint hosting one **room** per `(workspace, artifact)`. Authenticates the connection and enforces **write permission, re-evaluated on mutable state** (see [Collaboration & permissions](#collaboration--permissions)). Persists the Yjs update-log to Postgres (`pycrdt`-store style), tagging each update with its authenticated `user_id`, for durability, late-joiners, and authorship.
4. **Serialization bridge (Python), bidirectional.**
   - *Checkpoint direction:* room's `'prosemirror'` XmlFragment → ProseMirror JSON → markdown via `prosemirror-py` + a schema-matched markdown serializer.
   - *Hydration direction:* git HEAD markdown → ProseMirror JSON → Yjs XmlFragment, to populate an empty room.
   - Both directions run **server-side**, so room lifecycle and checkpoints never depend on a connected client.
5. **Checkpoint/commit layer (Python).** Debounced + explicit checkpoints serialize the live doc to markdown and commit to git via the existing `write_and_commit_files`, writing `ArtifactVersion` + `WorkspaceEvent` rows with authorship derived from the per-update actor log.
6. **Room-aware write kernel (Python).** The **single path** for *every* server operation that mutates artifact markdown while Yjs is canonical (proposal accept, citation insertion, spec lock). A bounded extension of the bridge's hydration direction; its *autonomous* / richer-merge uses are deferred to #4.

### Invariant: all markdown writes go through the kernel

> **While Yjs is canonical, no server operation reads or writes artifact markdown via the old git-first path** (`save_section`, `human_cite`, the spec-lock audit). They all route through the room-aware write kernel, whose sequence is:
>
> 1. Acquire any **operation-specific row lock** (e.g., `ProposedChange` `FOR UPDATE` + status gate for accept).
> 2. Acquire the **room lock** (serialize live-doc mutation).
> 3. **Serialize the live room** to get the authoritative current document/sections — *the validation source is the live room, not git.*
> 4. **Validate against live state** with today's semantics: staleness (compare the caller's `base_section_sha` to the **live** section SHA directly — not gated on `head != base_commit_sha`, which is unsafe when Yjs leads git), spec-lock, and the H2 structure guard.
> 5. **Apply** the change to the live room as an **authenticated** Yjs update.
> 6. **Checkpoint → commit** the doc (plus any sidecar, e.g. `citations.json`) to git, yielding `resulting_commit_sha`.
> 7. Update DB provenance/status rows referencing that checkpoint.
>
> This is the recurring theme of review: every artifact-markdown write must be room-aware. The kernel reuses today's *validation semantics and commit/provenance helpers*, but sources current state from the live room.

### Source of truth & data flow

- **Live document:** the **persisted Yjs update-log is canonical** while editing. On the first open of an artifact (no Yjs state yet), the **server hydrates the room from git HEAD markdown** (markdown → ProseMirror → Yjs, via the bridge). Thereafter the Yjs store is the recovery source; clients never lose live work to a stale git snapshot.
- **Git** is a **checkpoint / version / provenance stream derived from the live doc** at meaningful boundaries, and is **what every backend reader consumes** (agent, citation audit, evaluator, coherence, activity feed). Between checkpoints git may lag the live doc; that is expected and safe.
- **Checkpoint triggers:** (a) debounced idle (no edits for N seconds) / on blur, (b) explicit "Save a version", (c) last client leaving the room. Each checkpoint:
  1. Serialize live doc → markdown.
  2. If changed vs git HEAD, commit (one `ArtifactVersion` per checkpoint), message includes contributing authors.
  3. Emit a `WorkspaceEvent` (extends today's `user_edit`) carrying the author set and byte delta; the **activity feed** reads this.
  4. Run the existing draft Tier-1 validation / Tier-2 enqueue against the committed snapshot (unchanged downstream).
- **Authorship (server-authoritative):** the sync server tags **every persisted Yjs update** with the **authenticated `user_id`** of the connection that sent it (recorded in the update-log, *not* taken from client awareness, which is ephemeral and spoofable). Checkpoint authorship is derived from this server-side log; a checkpoint spanning several users' updates is recorded as **co-authored**. Awareness is used only for live presence/cursors.

### Editor schema & markdown round-trip

The Tiptap schema is **deliberately constrained** to what proposals need, so markdown round-trips deterministically:

- **Block:** H2 (= section boundary) and H3 headings, paragraphs, bullet/ordered lists, blockquote, code block, horizontal rule, images (existing attachment support).
- **Inline/marks:** bold, italic, strikethrough, inline code, links, hard break.
- **Custom node:** **citation** — rendered as a read-only pill in #1 (creation is still possible via select-to-cite; see [Citations in #1](#citations-in-1)); serialized back to its existing markdown marker form on disk (so the citation/claims pipeline is untouched). Becomes a fully interactive, node-anchored citation in #2.

Round-trip contract:

- The serializer produces **normalized markdown** and is the single canonical writer. Exotic/arbitrary markdown constructs outside the schema are disallowed (the editor cannot create them).
- The existing `split_sections` invariant (`"".join(section.text) == doc`, `##` boundaries) **must hold on the serializer's output**. This is validated by property tests (see [Testing](#testing-strategy)) and protects #3 and #5.

**Section-structure policy for #1 (decided):** the **set and order of H2 section boundaries is fixed** in #1. Within that fixed skeleton, editing is fully free — prose, H3 subheadings, and **moving text *between existing sections*** (cut/paste) all work. What is **not** allowed in #1 is **adding, deleting, or reordering H2 sections themselves** — that is #3, where stable section identity removes the `section_index` fragility (`ProposedChange.section_index`, evaluator/uncited deep-links). The editor enforces this (a ProseMirror constraint blocks creating/removing/reordering H2 nodes; H2 text may be *renamed*), mirroring today's `SectionStructureError` guard; the checkpoint serializer asserts the invariant as defense-in-depth. This keeps `section_index` stable for #1 and is consistent with the [non-goals](#non-goals-explicitly-out-of-this-sub-project).

### Collaboration & permissions

- **Websocket auth:** authenticate via the existing session cookie; authorize workspace membership.
- **Write permission, enforced server-side and re-evaluated on state change** (not fixed at connect time, because it depends on mutable state — `current_phase` and `spec_locked_at`):
  - **Draft:** owner + members may write during the `active` phase. During `setup`, owner-only.
  - **Spec:** owner + members may write **unless `spec_locked_at` is set**, in which case owner-only.
  - The server rejects (neither persists nor broadcasts) any update from a connection that currently lacks write permission.
  - When permission is **revoked mid-session** (phase change, spec lock, membership removal), already-open sockets are **downgraded to read-only** (and notified).
  - Non-writers get a **read-only live view** (they still see edits and presence).
- **Spec lock is room-aware** (goes through the write kernel, not the git-first audit): acquire the room lock → **downgrade spec writers to read-only** → **checkpoint the live spec** → set `spec_locked_at` and write the `SpecLockEvent` **against that checkpoint**. (Today's audit pins git HEAD, which would record a *stale* spec when Yjs leads git.)
- **Presence & remote cursors** via Yjs awareness (`yCursorPlugin`): show who is editing and where.

> **Test requirement:** updates sent from a socket *after* a spec lock / phase downgrade are proven to be **neither persisted nor broadcast**, and the downgraded client is moved to read-only.

### Agent / proposal interaction in #1

- Agent **reads** committed snapshots and **proposes** as today (proposals generated against the latest commit).
- **Accepting a proposal is a server-side, atomic operation** through the [room-aware write kernel](#invariant-all-markdown-writes-go-through-the-kernel), preserving today's contract:
  1. Lock the `ProposedChange` row **`FOR UPDATE`** and gate on `status == pending` (serializes double-clicks / two-member races, exactly as today's `_load_scoped`).
  2. Under the room lock, **validate against the serialized live room**: compare the proposal's `base_section_sha` to the **live** section SHA → mismatch marks `stale`; honor **spec-lock** (`SpecLockedError` → 409, stays pending) and the H2 structure guard.
  3. Apply the section-scoped replacement to the live room as an authenticated update, attributed to the approver.
  4. Immediate **checkpoint → commit**; mark `ProposedChange.accepted` + `resulting_commit_sha`.
- **Contract:** `accepted` ⇒ `resulting_commit_sha` is set **and room/git agree** (a **no-op** accept still resolves to `accepted` at the current checkpoint with no new commit, matching today's `changed=False` short-circuit). The accepted edit can never be clobbered by a later checkpoint, because it *is* a checkpoint of the live room.
- **Why server-side, not client-side:** the live room is canonical, so an accept that touched git but not the room would be **overwritten by the next checkpoint**, and "accepted" must not be recorded before a commit exists. (This overturns an earlier client-side-accept idea after review.)

### Citations in #1

- Citations **render as read-only pills**; clicking does not yet open the node-anchored peek (that is #2).
- **Citation *creation* is preserved (no regression), but routed through the room-aware write kernel** — *not* the old git-first `human_cite` path (which would reject valid uncheckpointed text, or commit a marker the next checkpoint overwrites). Inserting a citation: room lock → validate the anchor against the **serialized live doc** → apply the marker to the **live room** → checkpoint commits the doc **and** `citations.json` together. The select-to-cite → `CitationComposer` UX is unchanged; the server endpoint moves onto the kernel. Citation creation stays **owner-gated** as today (revisit if members should cite).
- The current **(section, code-point offset)** anchor model is retained as **acknowledged interim**: #2 replaces it with node-anchored citations that move with their text. A consequence in #1: moving *cited* text across section boundaries can leave the recorded `section_index` lagging until #2 — the marker itself survives in the doc, so the audit pipeline is unaffected.

## Reconciliation with existing systems

- **Section model:** unchanged conceptually — sections are still derived by `split_sections` from the committed markdown. Backend readers are unaffected.
- **Per-section save endpoint** (`POST /workspaces/{id}/sections/{index}`): **no longer the human edit path** (humans edit via the live room). It remains the writer for the **old (flag-off) editor** during transition. Proposal-accept and citation insertion no longer route through the git-first HTTP paths for new-editor workspaces — they use the room-aware write kernel. The kernel **reuses the commit/provenance helpers** (`write_and_commit_files`, `ArtifactVersion`, `WorkspaceEvent`, `SpecLockEvent`) and the **validation *semantics*** (staleness, spec-lock, structure guard), but the staleness check is **re-implemented against the serialized live room** rather than `save.py`'s git-read + `head != base_commit_sha` gate (which is unsafe when Yjs leads git). Endpoint retirement is tracked with #3.
- **Provenance / version history:** preserved at checkpoint granularity (coarser than per-keystroke, by design) via `ArtifactVersion` + `WorkspaceEvent`.
- **Citation audit, evaluator, coherence, uncited-claims:** unchanged — all read committed markdown snapshots.

## Rollout, schema & dependencies

- **No content migration** of artifacts — markdown already lives in git; the first open of an artifact hydrates the Yjs room from git HEAD. **But there is real schema and dependency work:**
  - **New Alembic migration** for the durable Yjs update-log store (rooms/updates table keyed by `(workspace, artifact)`, with the authenticated-actor metadata per update from [finding above](#collaboration--permissions)).
  - **New backend deps:** `pycrdt`, `pycrdt-websocket`, `prosemirror-py` (+ a markdown serializer); `markdown-it-py` is already present for the reverse parse.
  - **New frontend deps:** `@tiptap/*`, `yjs`, `y-prosemirror` — added to `pnpm` lockfile (note: `pnpm add` is subject to the 7-day `minimumReleaseAge` cooldown; use the documented `--config.minimumReleaseAge=0` workaround for the install).
- **Feature flag with strict, one-way semantics (no source-of-truth fork):** the new editor is gated per artifact. **Once an artifact has live Yjs state, the flag is sticky-forward for that artifact** — it does **not** fall back to the git-reading old editor, because uncheckpointed live edits would otherwise be stranded or overwritten by a stale git reopen. Any fallback (e.g., emergency disable) is only permitted after a **successful checkpoint + room drain**, so git is guaranteed current first.
- **Do not delete the old editor** until the bridge spike passes and the flag's safety semantics are proven; both editors coexist behind the flag during transition.

## Testing strategy

- **Serialization round-trip (the critical one):** property tests — `markdown → PM → Yjs XmlFragment → PM → markdown` is **stable** for the constrained schema, and the `split_sections` invariant holds on output. Seeded with real proposal fixtures.
- **`pycrdt`↔ProseMirror bridge:** unit tests for XmlFragment → PM JSON against fixtures produced by the JS `yXmlFragmentToProsemirrorJSON` (golden files), guarding fidelity.
- **Sync server:** room hydration-from-git, persistence/recovery across restart, per-connection write-permission enforcement (draft phase gating, spec lock), late-joiner sync. Integration test with **two `pycrdt` clients** converging through a server room.
- **Checkpoint/commit:** debounce + explicit + last-client-leaving triggers each produce the expected `ArtifactVersion`/`WorkspaceEvent`, with correct (co-)authorship derived from the server-side actor log; downstream Tier-1/Tier-2 still fire.
- **Room-aware write kernel — validation source is the live room (the key regression guard):** a section edited *live but not yet checkpointed* (so git HEAD still equals the proposal's `base_commit_sha`) must make accept/cite detect **staleness against the live section SHA** and refuse to clobber — proving validation does *not* use `save.py`'s git-read `head != base_commit_sha` gate.
- **Atomic server-side accept:** accept applies to the live room **and** commits in one operation — `accepted` ⇒ `resulting_commit_sha` set and room/git agree; spec-lock → 409 + stays pending; stale base SHA → `stale`; **no-op** proposal → `accepted` at current checkpoint, no new commit. Mirrors and reuses the existing `test_accepting.py` contract.
- **Accept race:** two concurrent accepts of the same proposal — the `FOR UPDATE` row lock + status gate makes the second a clean rejection/no-op (no double apply).
- **Citation insertion via the kernel:** the marker lands in the live room and a single checkpoint commits the doc **and** `citations.json` together; valid uncheckpointed live text is *not* rejected.
- **Spec lock is room-aware:** locking checkpoints the live spec first, so `SpecLockEvent.commit_sha` references that checkpoint (not a stale git HEAD), and spec writers are downgraded.
- **H2 structure enforcement:** the editor blocks add/delete/reorder of H2 sections (rename allowed); the checkpoint serializer asserts the `split_sections` invariant.
- **Authorship integrity:** a spoofed-awareness client cannot mis-attribute a checkpoint; authorship comes from the authenticated per-update actor, not awareness.
- **Frontend (Vitest; `tsc` is the type gate, no separate TS linter):** editor mounts and renders markdown; read-only enforcement for non-writers; presence; **select-to-cite over the Tiptap doc** creates a citation (selection → section/offset mapping → `POST /citations`).
- Cockpit `/api` tests live under `tests/proposal_assist/`; whole-suite runs may flake on testcontainers PG under load — verify flagged tests via grouped dir runs.

## Risks & spikes

- **`pycrdt`↔ProseMirror bridge fidelity (primary risk).** Mitigation: an explicit **spike first** in the implementation plan — prove faithful round-trip **in both directions** (markdown→PM→Yjs hydration *and* Yjs→PM→markdown checkpoint) for the constrained schema, with golden files vs the JS reference, before building the checkpoint/hydration layers on it. The reverse parse (markdown→PM) likely uses `markdown-it-py` feeding a ProseMirror parser; the spike confirms feasibility.
- **Markdown round-trip normalization.** Mitigation: constrain the schema; accept normalized markdown as canonical; assert the `split_sections` invariant in tests.
- **Single-instance room affinity.** All clients of a given room must reach the same process holding it. Acceptable today (single-instance is already a hard constraint for the scheduler). Future horizontal scaling needs sticky rooms or `y-redis`; noted, not solved here.
- **Server-side serialization cost** on the on-prem box. Checkpoints are debounced and infrequent; serialization is cheap relative to LLM work. Low risk.

## Out of scope / Roadmap

This foundation is **#1** of a program. Later sub-projects each get their own spec → plan:

- **#2 — Citations as a first-class node.** Migrate select-to-cite / peek / composer onto the citation node; retire offset-based anchoring (a node moves with its text — solving the original anchoring problem).
- **#3 — Structure operations + stable section identity.** Add/split/merge/reorder as native editor gestures; give sections **stable IDs** (identity ≠ position) and fix the `section_index` fragility in `ProposedChange` and evaluator/uncited-claims deep-links; retire the dormant per-section save endpoint.
- **#4 — Autonomous agent writes against the live doc.** Extends #1's constrained (human-initiated) server-write into **autonomous** edits (agent as a bot client editing with no human present) and **richer proposal merge** beyond section-scoped replacement.
- **#5 — Branching & reconciliation.** Fork the draft into a git **branch** ("draft variant"), edit in isolation, reconcile via a git 3-way merge surfaced as a **PR-style review**. Rides the **git** layer (not the CRDT); shares merge-into-live-doc machinery with #4. The foundation's "git as durable checkpoint truth + clean deterministic markdown serialization" is the enabling constraint.

**Division of labor to keep in mind throughout:** the **CRDT (Yjs)** handles real-time *micro-convergence*; **git** handles intentional *macro-divergence* (versions, branches, merges).
