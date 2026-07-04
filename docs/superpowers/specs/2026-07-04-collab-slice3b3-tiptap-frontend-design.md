# Collab Slice 3b-3 — Tiptap WYSIWYG editor + Yjs WebSocket provider (frontend)

**Date:** 2026-07-04
**Status:** Design (approved with amendments; pre-plan)
**Depends on:** Slice 3b-1 server hardening (MERGED `e4d5442`) and the whole collab foundation (Slices 0–2c, 3a). See `[[project_collab_editing_foundation]]`.

## 1. What this slice is (and is not)

3b-3 is the **first frontend runtime** for the collaborative editor: a schema-locked Tiptap WYSIWYG surface in the cockpit that, when the collab flag is enabled for an artifact, binds a Yjs `Y.Doc` to the hand-rolled backend WebSocket sync server and gives live, multi-client editing. Flag-off keeps the existing CodeMirror-6 section editor unchanged.

**In scope:**
- A Tiptap React editor whose ProseMirror schema matches `frontend/scripts/collab-schema.mjs` exactly, guarded by a schema-equality test.
- A **custom thin Yjs provider** over the existing WS route `/api/ws/workspaces/{id}/{artifact}` (no `y-websocket`).
- Conditional mount by a new additive `collab_enabled` read-model field; flag-off path (CM6) preserved byte-for-byte.
- Read-only rendering of citations (inline-atom NodeView pill).
- "Save a version" wired to the existing checkpoint endpoint, gated (socket settled) to *shrink* the window against an in-flight update — not a race-free guarantee (see §7.2; a true fence is a follow-up).
- A first-task **handshake de-risk** (Slice-0 style GO/NO-GO), a schema-equality test, a provider unit test, and one Playwright round-trip e2e.

**Explicitly NOT in scope (deferred):**
- **Presence / cursors / awareness → Slice 3b-4.** The provider sends no awareness frames; the server already ignores them.
- **Flipping the flag ON in prod → Slice 3b-5.** At the end of 3b-3 the flag remains OFF by default; the editor is exercised only via dev/e2e enabling the per-artifact flag on a seeded workspace.
- Rich editing UX: formatting toolbar, bubble menu, slash commands, interactive citation **insert** in the WYSIWYG surface (citation insert stays on the existing CM6/REST flow).
- Live-as-you-type Outline (see §7); a true persist-ack fence for Save-a-version (see §7); active-session update-tail bounding (foundation roadmap, lands around the flip).

## 2. Constraints & invariants (hard gates)

1. **Flag stays OFF in prod.** Every new runtime behavior is reachable only when `is_collab_enabled(workspace, artifact)` is true, which prod never sets. Flag-off parity is a HARD gate.
2. **CM6 flag-off path is byte-for-byte unchanged.** `ArtifactSectionEditor` / `CodeMirrorEditor` / `useDraft` / `useSaveSection` / the REST section-save flow are untouched on the flag-off path. `can_edit` keeps its current owner-oriented semantics (do not repurpose it — the CM6 path reads it).
3. **Backend changes are additive & read-only.** The only backend edit is adding `collab_enabled` + `can_write` fields to the `DraftOut` / `SpecOut` response models (derived from `resolve_access` / `is_collab_enabled`). NO migration, NO new write endpoint, NO schema change. The checkpoint and WS routes already exist.
4. **Single-instance Yjs.** Exactly one `yjs` and one `y-prosemirror` instance in the bundle (dedupe/pin), matching the versions the write-path harness already validated (`yjs 13.6.31`, `y-prosemirror 1.3.7`). A duplicated yjs silently breaks CRDT identity.
5. **Schema identity is load-bearing.** The Tiptap-generated schema MUST equal `collab-schema.mjs` node-for-node / mark-for-mark / attr-for-attr, or the y-prosemirror ↔ pycrdt bridge desyncs from the server's markdown serializer. A CI vitest enforces this.
6. Tests: backend runs with `EMBEDDING_MODEL=bge-m3`, grouped-dir (never whole-suite). CI runs `ruff check .` (not format). Frontend gate is `tsc` (no TS linter) + `vitest` + Playwright.

## 3. Architecture overview

### 3.1 Conditional mount
`DraftPanel` / `SpecPanel` read the new `collab_enabled` field from the draft/spec query:
- `collab_enabled === true` → render `<CollabEditor artifact="draft"|"spec" workspaceId can_write=… citations=…/>`.
- `collab_enabled === false` (prod default) → the current `ArtifactSectionEditor` path, unchanged.

The choice is made at the panel level so the two editors never coexist for one artifact and the CM6 path is completely dormant when collab is on (and vice versa).

### 3.2 New frontend modules (`frontend/src/collab/`)
| File | Responsibility |
|---|---|
| `CollabEditor.tsx` | Tiptap React editor component; owns the `useEditor` instance, wires the Collaboration extension to the Y.Doc, sets `editable` from `can_write`, publishes cursor/status to `editorStore`, renders the "Save a version" control. |
| `collabProvider.ts` | The thin Yjs↔WebSocket provider (see §4). Framing via `y-protocols/sync` + `lib0`. Emits `status` events; owns reconnect. |
| `useCollabDoc.ts` | React hook: constructs the `Y.Doc` + provider for a `(workspaceId, artifact)`, tears them down on unmount / artifact switch, exposes `{ doc, provider, status }`. |
| `schema.ts` | Tiptap `Node`/`Mark` extensions (snake_case names) matching `collab-schema.mjs`; the `citation` inline-atom extension. |
| `CitationNode.tsx` | Read-only React NodeView for the citation pill. |
| `collabSchema.equality.test.ts` | Vitest asserting the Tiptap schema ≡ `collab-schema.mjs`. |
| `collabProvider.test.ts` | Vitest for the provider against a fake socket. |

Code-split so non-collab users don't pull Tiptap/yjs (mirror the existing `lazy()` on `Cockpit`).

### 3.3 Dependencies
- **Add (runtime):** `@tiptap/react`, `@tiptap/core`, `@tiptap/pm`, `@tiptap/extension-collaboration`, `y-protocols`, `lib0`.
- **Promote dev→runtime + pin:** `yjs`, `y-prosemirror` (already present as devDeps for the golden/write-path tooling; the runtime editor must use the **same** pinned versions).
- **Do NOT add** `y-websocket` (custom provider) or `@tiptap/extension-collaboration-cursor` (that's 3b-4).
- pnpm `add` for the fresh `@tiptap/*` packages will hit the 7-day `minimumReleaseAge` cooldown → use the ephemeral `--config.minimumReleaseAge=0` for the add; the committed lockfile install is unaffected. Verify integrity against the registry.

## 4. Transport — custom thin provider

`collabProvider.ts` opens a raw `WebSocket` to `wss?://<host>/api/ws/workspaces/{workspaceId}/{artifact}` and speaks exactly the three sync frames the server implements (`protocol.py`): `SYNC_STEP1`, `SYNC_STEP2`, `SYNC_UPDATE`. Message framing uses `y-protocols/sync` (`writeSyncStep1/2`, `writeUpdate`, `readSyncMessage`) + `lib0/encoding|decoding` so bytes are wire-identical to what pycrdt's `_sync` codec expects. AWARENESS (message type 1) is never sent (3b-4).

The provider MUST explicitly implement all of the following (these are the amendments called out as required before implementation):

1. **Outer message-type framing.**
   The backend classifies frames as `[YMessageType.SYNC (0)] [YSyncMessageType subtype] [varuint len] [payload]` (`protocol.py`). `y-protocols/sync`'s `writeSyncStep1/2` / `writeUpdate` emit only the **inner** sync sub-message (`[subtype][len][payload]`). So every outbound frame MUST prepend the outer `writeVarUint(encoder, YMessageType.SYNC = 0)` byte before the sync sub-message; and every inbound frame MUST read the leading varuint message-type, confirm it is SYNC (0) — parking/ignoring AWARENESS (1), which the server never sends but a defensive reader tolerates — and only then `readSyncMessage` on the remainder. This mirrors the server's `create_sync_message`/`create_update_message` (which include the outer SYNC byte) and `handle_sync_message(message[1:])` / `extract_update` (which strip it).

2. **Bidirectional initial sync (server-first STEP1 + client STEP1 on open).**
   The server sends its own `SYNC_STEP1` right after `accept()` (`collab_ws.py` → `room.initial_message()`). The provider must:
   - On socket open, **send its own `SYNC_STEP1`** (client state vector) so the server replies with a `SYNC_STEP2` carrying the whole server document — without this the client uploads its empty state but never downloads the doc (blank editor).
   - On receiving the server's `SYNC_STEP1`, a **writer** replies with `SYNC_STEP2` (its diff — near-empty for a fresh client). A **read-only client (`canWrite=false`) suppresses the STEP2 reply entirely** (it has nothing the server will accept) and never sends `SYNC_UPDATE` — so a non-writer emits **no** write frames at all. Reading still works because the client's *own* STEP1 → server's STEP2 answer is on the ungated read path. The provider therefore takes a `canWrite` flag (from the read model's `can_write`, §6) and gates all outbound write frames (STEP2 reply + updates) on it.

3. **Origin tagging (no echo loop).**
   Apply every inbound update with the provider object as the transaction origin: `Y.applyUpdate(doc, update, provider)`. The local update handler `doc.on('update', (u, origin) => …)` MUST forward to the socket **only when `origin !== provider`** — otherwise remote updates get re-broadcast to the server as if local, producing an echo loop.

4. **Close-code-aware reconnect.**
   | Close code | Meaning (server) | Reconnect? |
   |---|---|---|
   | 1008 policy violation | bad origin / auth fail / non-member / **collab-not-enabled** / mid-session permission revoke | **NO** — permanent policy rejection; reconnecting loops forever. Surface a terminal "disconnected" status. |
   | 1009 message too big | frame exceeded 1 MB | **NO** — resending the same frame repeats the failure. |
   | 1011 server error | poison/evict recovery | **YES** — reconnect (with backoff) rebuilds cleanly from the durable log; the CRDT client re-offers its update. |
   | 1006 / network / unclean | transport drop | **YES** — reconnect with exponential backoff (cap ~30s), re-run the STEP1 handshake on reopen (resync). |

5. **Vite WS proxy.** Dev proxy currently has `ws:false` (`frontend/vite.config.ts`). Add a WebSocket-enabled proxy for `/api/ws` → `ws://localhost:8000` (a dedicated `"/api/ws"` entry with `ws:true`, or flip `ws:true` on the catch-all — the dedicated entry is lower-risk since it won't disturb Vite's own HMR socket).

Provider surface (indicative): `new CollabProvider({ url, doc, canWrite, WebSocketImpl? })` → `.status` ("connecting"|"connected"|"offline"|"closed-policy"), `.on('status', cb)`, `.bufferedAmount`/`.isSettled()` (for the Save-a-version gate, §7), `.destroy()`. `WebSocketImpl` defaults to the global `WebSocket` and is **injectable** — the Node handshake probe (§8, Task 1) and the provider unit test pass in Node's `WebSocket` (Node ≥20) / the `ws` package / a fake socket, so both exercise the real provider.

## 5. Editor & schema

### 5.1 Schema (the correctness core)
`collab/schema.ts` defines Tiptap `Node`/`Mark` extensions whose **`name`** fields are pinned to the snake_case names in `collab-schema.mjs`:
- Nodes: `doc`, `paragraph`, `heading` (levels 1–6), `bullet_list`, `ordered_list`, `list_item` (content `paragraph block*`), `blockquote`, `code_block`, `horizontal_rule`, `image` (`src`/`alt`/`title`), `hard_break`, `text`, and the custom `citation` inline-atom (`citekey`, `ev_id`).
- Marks: `strong`, `em`, `strikethrough`, `code`, `link` (`href`/`title`).
- Attrs, content expressions (`doc: block+`, `list_item: paragraph block*`, etc.), `group`, `inline`/`atom` flags must all match.

We do **not** rely on StarterKit's default names (`bold`/`italic`/`strike`/`bulletList`…). Either configure/rename StarterKit nodes or define individual extensions; the equality test is the backstop.

**`@tiptap/extension-collaboration`** binds the editor to `{ document: ydoc, field: 'prosemirror' }` (the XmlFragment name the server uses). Tiptap's native history is disabled; y-undo (bundled by the Collaboration extension) provides undo/redo.

### 5.2 Schema-equality test (drift guard)
`collabSchema.equality.test.ts` builds the ProseMirror `Schema` that Tiptap generates from the extensions and asserts structural equality against the `schema` exported by `collab-schema.mjs`: identical node-name set, mark-name set, per-node `content`/`group`/`inline`/`atom`, per-node/mark attr names + defaults. Any drift fails CI. This is what makes the "third schema twin" safe. The comparison is over **structural fields** (string names, content expressions, attr keys/defaults), not object identity — so it is robust even though the Tiptap side is built via `@tiptap/pm`'s `prosemirror-model` while `collab-schema.mjs` uses the standalone `prosemirror-model` devDep. At runtime the editor only ever uses `@tiptap/pm`'s copy (`collab-schema.mjs` is test-only tooling), so runtime schema construction is single-instance.

### 5.3 Citations (read-only)
`CitationNode.tsx` is a read-only React NodeView rendering the existing citation-pill visual (reuse current pill styling). It does not open the citation composer or support insert in this slice — interactive citation insert remains on the CM6/REST path (deferred).

### 5.4 Editability
`CollabEditor` sets `editable` from the new **`can_write`** field (from `resolve_access`), NOT `can_edit`. A non-writer gets a read-only editor (no local-only edits the server would silently drop). If a mid-session revoke closes the socket 1008, the provider surfaces a terminal disconnected status and the editor drops to read-only.

## 6. Backend read-model additions (additive)

Add two fields to `DraftOut` and `SpecOut`:
- `collab_enabled: bool` — from `collab_state.is_collab_enabled(session, workspace_id, artifact)`.
- `can_write: bool` — from `resolve_access(session, user, workspace_id, artifact)` (the resolver the WS + checkpoint endpoint already use — single source of truth for write permission).

`get_draft` / `get_spec` already open a session and `require_member`; they additionally call `resolve_access` (returns `(is_member, can_write)`) and `is_collab_enabled`, and populate the two new fields. `can_edit` is left exactly as-is (owner-oriented) so the CM6 path is unaffected. Regenerate `frontend/src/api/schema.gen.ts` (with `EMBEDDING_MODEL=bge-m3` to avoid the mxbai warning that breaks openapi-typescript).

Rationale for `can_write` (the amendment): the current read models are owner-oriented (`can_edit = owner` for draft; `owner && !locked` for spec), but `resolve_access` grants write to any **active member** on an active draft / unlocked spec. Keying the collab editor's editability off `can_edit` would wrongly make active members read-only. `can_write` from the same resolver keeps the HTTP read model and the WS write gate consistent.

## 7. Save / version UX, status, Outline

### 7.1 "Save a version" = checkpoint
On the collab path there is no per-section REST save — edits persist continuously through the WS, and checkpoints are server-side (last-client-leaving is already wired). The Draft/Spec panel's save affordance becomes **"Save a version"** → `POST /api/workspaces/{id}/collab/{artifact}/checkpoint` (exists, currently uncalled). Response `{committed, commit_sha, authors}`; `503` (busy/commit-failed) is retriable, `403` read-only, `409` collab-off.

### 7.2 The save-version race and the settled-gate (amendment)
The checkpoint endpoint checkpoints the **durable log**, not the live browser buffer, so a click immediately after a keystroke can checkpoint one update behind. Minimal-slice mitigation: **disable the "Save a version" control unless the provider is settled** — socket `readyState===OPEN` **and** `bufferedAmount===0` **and** a short idle-debounce (≈300–400 ms) since the last local update. This reduces (does not eliminate) the window; a residual sub-RTT gap remains because the server persists asynchronously after the frame leaves the client.
**Follow-up (documented, not in this slice):** a true fence — a lightweight server ack of the last-persisted update id so the client can wait for `acked >= last-sent` before checkpointing. Pairs naturally with the 3b-4 awareness channel.

### 7.3 editorStore / StatusBar
`editorStore` is repurposed on the collab path:
- `setSave` carries a **connection/sync status** on this path. The current `SaveState` union is `idle|saving|saved|dirty|error` (`editorStore.ts`) with no connection states, and `StatusBar` (`StatusBar.tsx`) has no labels for connection status — so this path requires **additively widening `SaveState`** with collab states (e.g. `collab-connecting | collab-synced | collab-offline | collab-readonly`), mapped from the provider `.status` (§4) + `can_write`, plus matching labels in `StatusBar`. The widening is additive: the CM6 flag-off path still emits only the original five, so its StatusBar rendering is byte-for-byte unchanged (parity).
- `setCursor` from the Tiptap selection (Ln/Col in the StatusBar).
- `setDirty` is inert (a CRDT has no per-section dirty state); the Outline's dirty markers simply don't show on this path.
- `resetEditorStore` on unmount as today.
The Outline component is untouched; `StatusBar` gains labels for the new collab states (additive — CM6 rendering unchanged). Only what the collab editor publishes changes.

### 7.4 Outline staleness (accepted for this slice)
The sidebar Outline reads headings from the React Query `useDraft` cache (`CockpitSidebar.tsx`), which is stale vs the live Y.Doc. Minimal boundary: **refetch the draft/spec query on checkpoint / version-save** so the Outline updates at version boundaries. Live-as-you-type Outline is a follow-up (a tiny Tiptap-derived outline store feeding the sidebar).

## 8. Testing & de-risk

1. **Task 1 — handshake de-risk (GO/NO-GO gate, before building the editor).** A **Node-ws probe against a real uvicorn**, importing the **real provider** with an injectable `WebSocket` implementation (Node ≥20 global `WebSocket` or the `ws` package) — so the probe exercises the actual provider code, not a throwaway client. A Python seed helper/fixture (pytest fixture or a small script) creates an active workspace, commits a draft, enables collab for `draft`, and prints the `workspace_id` + a valid session cookie. The Node probe connects with that `Cookie` header + a matching `Origin`, then verifies: (i) the bidirectional handshake completes; (ii) a sent update **persists** (survives a fresh connection replaying the durable log); (iii) a **second concurrent client converges**. If NO-GO, stop and fix transport — do not paper over it. (Mirrors the Slice-0 spike discipline.) Playwright is reserved for the later browser/UI/proxy e2e (item 4).
2. **Schema-equality vitest** (§5.2) — the drift guard.
3. **Provider unit test** (injectable fake socket): outbound frames carry the outer SYNC byte; sends its own STEP1 on open; a **writer** replies STEP2 to the server's STEP1 and a local edit → one `SYNC_UPDATE` frame; inbound update applied with the provider origin and **not** re-sent; a **read-only provider (`canWrite=false`)** sends its own STEP1 but **no STEP2 reply and no `SYNC_UPDATE`**; `1008`/`1009` → no reconnect; `1006`/`1011` → reconnect scheduled.
4. **One Playwright e2e:** two browser contexts on a flag-on workspace → type in A, assert B converges, reload preserves content, "Save a version" returns a commit and the Outline refetches.
5. **Parity:** CM6 flag-off tests (`test_accepting.py`, `test_api_spec_lock.py`, member/rubric/soft-delete) stay green unchanged; `get_draft`/`get_spec` existing tests still pass with the two additive fields.

## 9. Process

- Worktree isolation (`.worktrees/collab-slice3b3`), detached subagent-driven-development, **stop-before-merge** (human verifies + merges).
- Explicit-path `git add` only; never touch master/main from the worktree.
- Backend tests: `EMBEDDING_MODEL=bge-m3`, grouped-dir, `-p no:cacheprovider`.
- Frontend: `pnpm install --frozen-lockfile` for the harness; `tsc` + `vitest run` + Playwright are the gates; `--config.minimumReleaseAge=0` only for the fresh `@tiptap/*` adds.
- No `/code-review` / `/ultrareview` slash reviews in the detached run (user-billed); use a dispatched code-reviewer subagent for the final review.

## 10. Deferred / follow-ups

- Presence / cursors / awareness — **Slice 3b-4** (extends both server and this provider).
- Flip the flag ON in prod — **Slice 3b-5** (CM6→Tiptap cutover; `save_section` git-canonical switchover + Tier-1/2 post-checkpoint re-fire land around here).
- Rich UX: toolbar, bubble menu, slash commands, interactive citation insert in WYSIWYG.
- Live-as-you-type Outline (Tiptap-derived outline store).
- True persist-ack fence for Save-a-version (§7.2).
- Active-session update-tail bounding (high-volume Tiptap updates).

## 11. Open risks

- **Handshake compatibility** is the top unknown — mitigated by making it Task 1's explicit GO/NO-GO gate.
- **Schema drift** — mitigated by the equality test.
- **Duplicate yjs** — mitigated by pinning + dedupe verification.
- **CSP (decision: prod CSP stays out of this slice).** Dev CSP already allows `ws:` in `SPA_MODE=dev`, so the dev/e2e WS connection works here. Prod CSP is deliberately **not** touched — prod currently has tests asserting **no** `ws:` in the CSP, and the flag is OFF so no prod WS occurs. When 3b-5 flips the flag it will add a **narrow same-origin WS allowance** (not a broad `ws:`/`wss:` scheme wildcard). Action for this slice: only verify the dev CSP doesn't block the Task-1 probe / e2e.
