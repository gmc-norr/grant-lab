# Cockpit Cutover — Slice B: Interactive Agent Chat (right pane) — Design Spec

**Date:** 2026-06-24
**Status:** Design approved (pending written-spec review)
**Sub-project:** Cockpit cutover (the first of five in the full-React migration)

---

## 1. Migration context & roadmap

The web app is mid-migration from Jinja2/HTMX to a React SPA. The agreed end state is **one React application**: FastAPI becomes a pure JSON API (`/api/*`) + auth redirect endpoints (OAuth callback, no templates) + a static React bundle; Jinja2/HTMX/Pico/`app.css`/`base.html` are deleted. (The email-digest Jinja under `digest/templates/` renders email, not pages — it stays.)

**Strategy — strangler-fig, route-by-route** (not big-bang, not SSR/Next — this is an internal authed tool, and the prior architecture decision is "no Node SSR"). Each surface: add typed `/api` endpoints → build the React route under the existing AppShell → flip nav → **delete the Jinja template**. The app stays fully working at every step.

**Sub-projects** (each its own spec → plan → build): **(1) Cockpit cutover**, (2) Calls detail, (3) Profile/account, (4) Admin, (5) Auth + Jinja teardown (last, riskiest). Auth's OAuth *flow* stays server-side (redirects, no pages); only the login/register/status *pages* become React. `/api/me` already makes the SPA auth-aware.

**Cockpit-cutover slices** (this sub-project): **A** front door (workspace list + create), **B** *agent chat (this spec)*, **C** reference management UI, **D** settings + collaborators, **E** rubric-review gate, **F** citation audit + export, **G** cutover + Jinja teardown. Both UIs coexist until **G**.

### Why chat is Slice B (first)

The parity audit found the cockpit is already *ahead* of the Jinja workspace on the structured review layer (spec, scorecard, problems, coherence, persona panel, activity) and at parity on the editor + citations — but it is missing the **entire workspace lifecycle** and, critically, the **interactive drafting chat**. The Jinja workspace drafts via an SSE chat; the cockpit has only the background propose→accept inbox. Chat is the highest-value, highest-uncertainty piece, so it is tackled first to de-risk early, and because it is what makes the cockpit a complete-enough destination for Slice A's front door to route into.

**The agent model is already settled by the existing architecture** (this is the key finding): the Jinja chat turn engine (`turns/processor.py` → `drafting/WorkspaceController.send_turn` → `drafting/claude_adapter`) already drives the **propose-only** MCP toolset (`tools/registry.py`: `propose_edit` / `propose_spec_edit` / `propose_citation`, each returning a `proposed_change_id` with status `pending`). A user turn produces **proposals**, not direct writes, and those proposals already land in the cockpit's existing Inbox (SP2b-3b). **Chat is the missing input end of a loop whose output end already exists.** Bringing it into the cockpit is therefore a JSON/SSE-API + React-UI job — not an agent redesign.

---

## 2. Goal & scope

**Goal:** Add an interactive, SSE-streamed agent chat to the React cockpit as a persistent, collapsible **right pane** (the VS Code secondary-sidebar position), reusing the existing turn engine. A user message runs the propose-only agent; proposals stream into the existing bottom Inbox; the draft updates on accept.

**In scope (v1):**
- A right-pane chat region in the AppShell (collapsible), rendering the workspace's chat history + a composer.
- JSON turn-history read, JSON turn submission, and an **SSE turn stream**, all under `/api` (additive; reusing `submit_user_turn` / the turn processor / `chat_turns` / `turn_jobs`).
- Turn composer = **text + reference inclusions** (already-ingested references; the cockpit already lists them).
- The cloud-LLM **disclosure gate** (one-time acknowledgment), ported as JSON.
- Read-only history for viewers (no composer); owner-only sending.
- Inline surfacing of the engine's existing guard states: token-cap (429), drafting-disabled (503), setup-phase (409), one-running-turn-per-workspace.
- On turn completion, invalidate the proposals/draft/turns queries so the Inbox + editor reflect the new proposals.

**Out of scope / deferred:**
- **Image attachments** (upload/pick) → **Slice C** (Files/References UI); the composer shows a disabled, labeled image affordance.
- Workspace **list/create** → **Slice A**; chat is reached at `/cockpit/:id` for an existing workspace.
- **Rubric-review / Phase-0 gate** → **Slice E**. v1 chat targets **active** workspaces; a `setup`-phase workspace shows a "finish setup in the legacy workspace" gate state rather than a composer (see §9). This is the one cross-slice dependency.
- Jinja `/workspaces/{id}` retirement → **Slice G**.

---

## 3. Architecture overview

```
User types ─▶ POST /api/workspaces/{id}/turns (JSON: text + reference_ids)
                │  reuses submit_user_turn(): ChatTurn(role=user) + TurnJob(queued)
                ▼
         { job_id, user_turn }           drain scheduler → turns/processor.py
                │                                │  WorkspaceController.send_turn
                ▼                                ▼  (propose-only MCP tools)
   EventSource: GET .../turns/{job_id}/stream   chat_turns rows (assistant/tool) +
                │  text/event-stream             proposed_change rows (pending)
                ▼
   React right pane appends streamed turns; on `done`:
     invalidate ["turns", id], ["proposed-changes", id, *], ["draft", id]
                │
                ▼  proposals already render in the existing bottom Inbox (SP2b-3b)
```

The **right pane is a shell region**, consistent with Phase 3a's sidebar: `AppShell` renders it from the active route's `handle.RightPane` (a sibling of the `<Outlet>`, not a child — so it can't receive props from the cockpit; it reads the workspace id from the URL via `cockpitIdFromPath`/`useParams`, history from the React Query cache, exactly the global-channel discipline already in use). No schema change: `chat_turns`, `turn_jobs`, `workspace_events` already exist.

---

## 4. Backend API (additive, under `/api`)

All member/owner-gated via the existing `require_member` / `require_owner` (`proposal_assist.web._perms`). New handlers live in a new `src/call_manager/web/api/turns.py` router (mounted under `/api`), delegating to the **existing** `turns/`, `drafting/`, and disclosure logic — no business-logic duplication.

**4.1 `GET /api/workspaces/{id}/turns`** (member) → the chat read-model:
```jsonc
{
  "turns": [ { "id": "uuid", "role": "user|assistant|tool|system",
               "content": <JSONB blocks, see §5>, "created_at": "iso", "position": 12 } ],
  "can_send": true,            // owner AND phase=active AND drafting_enabled
  "needs_disclosure": false,   // TRUE when this user has no prior disclosure_acknowledged (false here = already acked)
  "phase": "active",           // "setup" | "active"
  "running_job_id": null       // a queued/running TurnJob for this ws, else null (resume the stream)
}
```
`running_job_id` lets a reloading client re-attach its `EventSource` to an in-flight turn.

**4.2 `POST /api/workspaces/{id}/turns`** (owner) — JSON body `{ "content": "text", "reference_ids": ["uuid", ...] }`. Mirrors the existing turns-route gates **in the canonical order** (returning JSON errors via the standard `/api` envelope). The order is the source of truth in `web/_cloud_gate.py` — **owner → setup → drafting → disclosure → caps → in-flight** — and must not be reordered (e.g. a setup-phase workspace with no ack must surface the *setup* gate, not disclosure):
- not owner → **403**;
- `phase == "setup"` → **409** (`code: "workspace_setup"`); drafting feature-flag off → **503**; `needs_disclosure` (no prior ack) → **403** (`code: "disclosure_required"`) — these three reuse the shared **`gate_cloud_llm()`** (`proposal_assist/web/_cloud_gate.py`; canonical setup → drafting → disclosure), rather than a hand-ordered copy;
- daily token cap (`TokenCapExceeded` / `GlobalTokenCapExceeded`) → **429**;
- a turn already `queued`/`running` → **409** (`code: "turn_in_flight"`).
- Success → **200** `{ "job_id": "uuid", "user_turn": { ...ChatTurn } }`. Reuses the existing user-content assembly (`attachments.resolver.resolve_user_content`, references-only in v1 — no image branch) + `submit_user_turn()` (creates the `user` ChatTurn with the assembled content blocks + the queued TurnJob).

**4.3 `GET /api/workspaces/{id}/turns/{job_id}/stream?since=<position>`** (member) → `StreamingResponse(media_type="text/event-stream")`, the JSON analogue of the Jinja `_stream_turn_generator`. Resume contract (driven by how `useEventStream` reconnects — see §6): the endpoint honors a `since=<position>` query param and starts polling `chat_turns` **after** that position (defaulting to the connect-time max position when absent), and **emits an SSE `id:` line carrying `ChatTurn.position` on every `turn` event** so the client captures a cursor in `ev.lastEventId` and can resume exactly after the last delivered row on reconnect. One event per new row; a terminal `done` event when the `TurnJob` reaches `completed|failed|cancelled`:
```
id: 42                event: done
event: turn           data: {"status":"completed"}   // or {"status":"failed","error":"..."}
data: {<ChatTurn>}
```
Because `position` is a workspace-global sequence, `since` resumes deterministically regardless of job boundaries. Same owner-or-viewer access and client-disconnect detection as today. (SSE works through the dev proxy and prod static-serve; the `/api/events/ping` endpoint already proves the pattern in this app.)

**4.4 `POST /api/workspaces/{id}/disclosure`** (member) → records the idempotent `WorkspaceEvent(kind="disclosure_acknowledged")` for this user; returns `{ "acknowledged": true }`. (The reference picker reuses the existing `GET /api/workspaces/{id}/references`.)

---

## 5. Turn content model

`ChatTurn.content` is JSONB. v1 renders the block shapes the engine already produces:
- **user**: `{ "type": "text", "text": "..." }`, optionally a list including `{ "type": "reference", "reference_id": "uuid" }` blocks (rendered as a reference chip). The chip's label is resolved **client-side** against the `useReferences(id)` cache the cockpit already loads (no `reference_map` is added to the read-model or stream — avoiding the earlier inconsistency); a reference missing from that cache (e.g. since-tombstoned) falls back to a muted "reference" chip. *Image blocks are parsed but not producible in v1 (no upload UI).*
- **assistant**: `{ "type": "text", "text": "..." }` (the streamed `AssistantTextEvent`), and the terminal **outcome** `{ "type": "turn_outcome", "text": "Proposed edit → Methods" }` rendered as the teal outcome chip that deep-links to the bottom Inbox tab.
- **tool**: `tool_use` / `tool_result` rows (`ToolUseEvent` / `ToolResultEvent`) rendered as one compact, low-emphasis "⚙ tool_name" line (collapsible group), so the transcript reads as conversation, not a trace.

The renderer is a typed dispatch (`TurnItem`) keyed on `role` + block `type`, defaulting unknown blocks to a muted fallback (never throws — mirrors the never-500 discipline of the Problems/sidebar panels).

---

## 6. SSE streaming (frontend)

Reuse the existing `useEventStream(url, handlers, opts)` (`frontend/src/api/events.ts`) — EventSource with `withCredentials`, error classification via `/api/me`. The chat opens the stream when a submit returns a `job_id` **or** when the history read returns a non-null `running_job_id` (reload re-attach). Handlers: `turn` → append the parsed `ChatTurn` to the live transcript (deduped by `position`); `done` → close the stream and `invalidate(["turns", id])`, `(["proposed-changes", id, "draft"])`, `(["proposed-changes", id, "spec"])`, `(["draft", id])` so the Inbox + editor refresh. A `failed` done-status renders the partial transcript plus an inline "turn failed" notice (mid-stream crashes leave partial `assistant`/`tool` rows — rendered coherently, not hidden).

*Note:* `useEventStream` hardening was a deferred SPA-foundation item; this slice is its first real consumer, so the plan includes a hardening pass as part of Task scope. Two concrete items: (a) **reset the `lastId` cursor when `url` changes** — today the ref persists across a `url` (i.e. `job_id`) change, leaking a stale `since=` into the next job's stream; key/reset the cursor per `job_id`. (b) Confirm reconnect/backoff + unmount cleanup behave under a long-lived turn stream. The resume itself depends on the server emitting `id:` lines (§4.3).

---

## 7. Frontend components

Under `frontend/src/cockpit/chat/`:
- **`CockpitChat`** — the `handle.RightPane` region. Reads `id` from the URL, calls `useTurns(id)` (RQ) + the disclosure/can_send flags; renders header (with collapse toggle), `TurnList`, and exactly one footer in this **precedence order** (mirroring the backend gate order in §4.2 so the UI and the would-be 4xx agree): viewer (not owner) → read-only note; else `phase === "setup"` → setup gate; else drafting-disabled → disabled note; else `needs_disclosure` → `DisclosureGate`; else → `Composer`.
- **`TurnList` / `TurnItem`** — the typed transcript renderer (§5); auto-scrolls on new turns.
- **`Composer`** — textarea + `ReferencePicker` (multi-select over `useReferences`, `text_status === "done"` only) + Send; disabled while a turn runs or `!can_send`; a disabled, labeled "📎 Image — Files slice" affordance. Surfaces 429/409/503 inline.
- **`DisclosureGate`** — the one-time acknowledgment; `POST /disclosure` → invalidate `["turns", id]`.
- **AppShell** gains a right-pane region (mirrors the Phase-3a `handle.Sidebar` wiring): renders `handle.RightPane` when the active route defines it; collapse state persisted (localStorage, like a VS Code panel). The cockpit route gains `handle.RightPane = CockpitChat`.

OpenAPI types regenerate via `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`; new hooks in `frontend/src/api/turns.ts` (`useTurns`, `useSubmitTurn`, `useAckDisclosure`), all `enabled: !!id`.

---

## 8. Error & edge handling

| State | Backend | Frontend |
|---|---|---|
| Viewer (not owner) | history 200; submit 403 | history shown; **no composer** ("read-only") |
| `setup` phase | submit 409 `workspace_setup` | setup gate: "Finish setup in the legacy workspace" + link (until Slice E) |
| Drafting flag off | submit 503 | inline "drafting is currently disabled" |
| Disclosure not acknowledged | submit 403 `disclosure_required`; flag in read-model | `DisclosureGate` instead of composer |
| Daily token cap | submit 429 | inline "daily limit reached" |
| Turn already running | submit 409 `turn_in_flight`; one-running partial index | composer disabled + "agent is working…"; stream attached |
| Mid-stream crash | orphan sweep → job `failed`; partial rows kept | partial transcript + "turn failed" notice |

---

## 9. Data model, security, testing

**Data model:** **No migration.** Reuses `chat_turns`, `turn_jobs`, `workspace_events`. Alembic head unchanged.

**Security:** owner-only send / viewer read (existing perms); per-user, per-workspace cloud-LLM disclosure hard-gate preserved (the spec'd compliance control); IDOR via `require_member`/`require_owner`; the agent remains structurally propose-only (no new tools).

**Testing:**
- *Backend* (`tests/test_api_turns.py`, testcontainers, **targeted** ids): history read-model (owner/viewer/non-member 403); submit happy-path (queues a TurnJob, echoes the user turn); each gate (disclosure 403, setup 409, cap 429, flag 503, in-flight 409); SSE generator emits a `turn` then `done` for a seeded completed job.
- *Frontend* (vitest): transcript renders each block type; streaming via a `FakeES` EventSource stub appends turns and invalidates on `done`; disclosure flow; viewer read-only (no composer); composer disabled while running; reference picker filters to `done`. `resetEditorStore()`/RQ hygiene per existing patterns.
- *Visual:* right-pane chat screenshot via the mocked-`/api` Playwright recipe (`reference_spa_visual_check`).

---

## 10. Open questions / risks

1. **Setup-phase dependency on Slice E — RESOLVED (interim accepted).** v1 chat is fully usable on **active** workspaces (the common case for existing ones). The explicit interim contract: a `setup`-phase workspace returns **409 `workspace_setup`** on submit, and the right pane shows a **clear setup gate** ("Finish setup — review the rubric in the legacy workspace" + link) instead of a composer; React-created workspaces (Slice A) do **not** become chat-enabled until the rubric gate (Slice E) flips them `active`. Since Slice A (create) isn't built yet, the full new-workspace→chat flow lands with A+E; this slice neither builds nor blocks on them.
2. **SSE lifecycle.** `useEventStream` is unproven under a long-lived turn stream; the plan hardens reconnect/cleanup. Risk is low (the engine is poll-based server-side) but called out.
3. **Session resume.** The engine persists the SDK `session_id` per workspace for native resume; the cockpit chat inherits this transparently (no new work), noted for awareness.
4. **Spike first.** Per the de-risking plan, Task 1 is a thin end-to-end spike (one text turn → streamed → proposal in the inbox against a seeded active workspace) before the full build.
