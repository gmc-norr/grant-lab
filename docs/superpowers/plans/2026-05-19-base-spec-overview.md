# Base Proposal-Assist Spec — Plan Decomposition Overview

**Spec:** [docs/superpowers/specs/2026-04-18-call-manager-proposal-assist-design.md](../specs/2026-04-18-call-manager-proposal-assist-design.md)

This is the **index** for the base proposal-assist spec implementation. The spec is large (442 lines, ~7 independent subsystems), so it's decomposed into 8 plans that each ship working software on their own. Each plan has its own file under `docs/superpowers/plans/2026-05-19-base-spec-<N>-<topic>.md`.

The plans are designed to be executable in roughly the order shown, with explicit dependencies called out. Some can run in parallel once their prerequisites land.

---

## Plan dependency graph

```
B1 (schema foundation + workspace creation)
  ├─ unblocks: Slice A Part 2 (already-merged Slice A foundation can extend)
  ├─ unblocks: B3, B4, B5, B6
  └─ independent of: B2

B2 (rename call-monitor → call-manager)
  └─ independent of all other plans; can run any time
     (the longer this is deferred, the more imports it eventually touches)

B3 (Agent SDK adapter + per-workspace drafting subprocess)
  ├─ requires: B1
  └─ unblocks: B4

B4 (turn queue + chat machinery + compaction)
  ├─ requires: B1, B3
  └─ unblocks: B5, B6

B5 (tool surface for the agent)
  ├─ requires: B1, B3, B4
  └─ unblocks: B6

B6 (workspace web UX)
  ├─ requires: B1, B3, B4, B5
  └─ unblocks: end-user workflow

B7 (figures + attachments + vision)
  ├─ requires: B1, B4 (turn machinery for tool-use rounds)
  ├─ requires: B5 for the agent-tool half (look_at_figure / read_preliminary_data
  │            register through the same tool-registration surface B5 defines)
  └─ upload-and-describe half (REST upload + attachment_jobs drain) is orthogonal
     to B5 and can run in parallel with B5/B6

B8 (disclosure + kill switch + spend guardrails)
  ├─ requires: B1, B4 (for the per-turn token banner)
  └─ should land before any production rollout
```

The shortest path to "an end-user can chat with the agent to draft a proposal" is **B1 → B3 → B4 → B5 → B6**, with **B7** and **B8** landing in parallel with B5/B6.

---

## Plan summaries

### B1 — Schema foundation + workspace creation

**Goal:** Land all 9 base-spec tables (`workspaces`, `workspace_viewers`, `workspace_events`, `chat_turns`, `turn_jobs`, `chat_summaries`, `artifact_versions`, `attachments`, `attachment_jobs`) with SQLAlchemy models, plus a `create_workspace()` service function that inserts a workspace row and writes a `created` audit event.

**End state:** A test (or CLI) can call `create_workspace(session, owner_id=..., call_id=..., profile_id=...)`, get a Workspace back, and see a `workspace_events(kind='created')` row written. **Slice A Part 2 can now start** — its imports work, its FKs to `workspaces` resolve. Phase 0 actually draining requires Slice A Part 2's own additional migrations (phased columns ALTER, `workspace_setup_jobs` table, Phase 0 worker code).

**Scope (in):** 9 migrations, 9 models, `create_workspace()` + tests.

**Scope (out, deliberately):**
- `workspace_setup_jobs` table + model — owned by Slice A Part 2 (its Task 11).
- Phased-workflow columns on `workspaces` (`current_phase`, `call_body_snapshot`, `rubric_criteria`, etc.) — owned by Slice A Part 2 as an ALTER TABLE (its Task 9).
- Phase 0 enqueue from `create_workspace()` — Slice A Part 2 either extends `create_workspace()` or adds a separate `enqueue_phase0()` helper after its `workspace_setup_jobs` migration lands.
- Web endpoints (B6), chat (B4), agent (B3), tools (B5), figures (B7).

**Estimated tasks:** ~11.

**File:** `docs/superpowers/plans/2026-05-19-base-spec-1-schema-foundation.md` (this is the first plan written in full).

---

### B2 — Rename `call-monitor` → `call-manager`

**Goal:** Rename the Python package, the database, the env vars, every import, and update operational scripts.

**End state:** Codebase, DB, and operational tooling consistently use `call_manager` / `call-manager`. CI green. Migration scripts published.

**Scope (in):**
- Rename `src/call_monitor/` → `src/call_manager/`.
- Update every `from call_monitor.*` import (~100+ files).
- `ALTER DATABASE call_monitor RENAME TO call_manager` migration script.
- Update env vars (`DATABASE_URL` connection string).
- Update Alembic env, scripts, Dockerfile, README, CLAUDE.md.
- Pre-cutover compatibility shim (an alias package `call_monitor` that re-exports `call_manager` for one release, if needed).
- Test suite passes against the renamed package.

**Scope (out):** Behavioral changes (this is pure refactor).

**Estimated tasks:** ~15–20.

**Dependencies:** None — can run any time. The longer it's deferred, the more files it touches. Recommend running it after B1 lands and before B6 starts (web routes embed package paths in many places).

**File:** Not yet written.

---

### B3 — Agent SDK adapter + per-workspace drafting subprocess

**Goal:** Implement the `DraftingAgent` adapter that wraps the Claude Agent SDK in a per-workspace subprocess, with idle-timeout teardown and resume-from-history.

**End state:** Given a workspace and an SDK turn-handler, the controller can launch a subprocess, send a turn, receive streamed responses (assistant + tool blocks), and tear down after `WORKSPACE_IDLE_TIMEOUT_SECONDS` of inactivity. Subprocess crash → orphaned `turn_jobs` row swept to `failed`.

**Scope (in):**
- `DraftingAgent` adapter class (interface: `launch(workspace_id) -> handle`, `send_turn(handle, user_msg)`, `terminate(handle)`, async stream).
- Subprocess registry (in-memory map: workspace_id → handle, with idle-timer).
- Resume-from-history: subprocess startup reads latest `chat_summaries` + un-summarized `chat_turns` and replays into SDK state.
- Per-workspace `pg_advisory_lock` held by the subprocess.
- Orphan sweep on subprocess launch.
- Tests against a mocked SDK server (stdio-compatible fake).

**Scope (out):** Turn queueing (B4), tool implementations (B5), chat UI (B6).

**Estimated tasks:** ~20–25.

**Dependencies:** B1 (needs `workspaces`, `chat_turns`, `chat_summaries`, `turn_jobs`).

**File:** Not yet written.

---

### B4 — Turn queue + chat machinery + compaction

**Goal:** Implement the turn submission flow, FIFO drain, single-write enforcement, and bounded-replay compaction.

**End state:** A user can POST a turn → atomic insert into `chat_turns` (role=user) + `turn_jobs` (status=queued). The subprocess drains one queued turn at a time per workspace, transitions through `running → completed/failed`. After 100 turns or 200K tokens, a background job summarizes and writes `chat_summaries`. Per-turn token telemetry recorded. Daily per-workspace token cap enforced (workspace pauses at midnight Europe/Stockholm).

**Scope (in):**
- POST /workspaces/<id>/turns endpoint (insert + enqueue in one tx).
- Turn drain logic in the subprocess (claim oldest queued, transition, generate response, write assistant/tool turns, transition to terminal state).
- Compaction job (cron-style, in APScheduler).
- Token telemetry (`chat_turns.tokens_in`, `tokens_out`).
- **Daily per-workspace token cap — enforcement only**: pre-enqueue check that rejects new turns once the day's cap is reached. The "you've hit the cap, paused until midnight" UX surfacing (banner, admin override, status display) is **B8's** responsibility, not B4's.
- Tests for queueing, drain, compaction, and the enforcement gate.

**Scope (out):** Tool implementations (B5), chat UI (B6), daily-cap UX surfacing (B8).

**Estimated tasks:** ~20–25.

**Dependencies:** B1, B3.

**File:** Not yet written.

---

### B5 — Tool surface for the agent

**Goal:** Implement the custom tools the drafting agent invokes: artifact editing (`edit_proposal`, `replace_proposal`), version history (`get_artifact_history`, `read_artifact_at`, `diff_artifact`), and context retrieval (`get_call_details`, `get_profile`).

**End state:** The SDK can invoke each tool by name during a turn. Tool calls auto-commit artifact changes to the workspace's git repo and write `artifact_versions` rows. Context tools query the existing `calls` / `profiles` tables.

**Scope (in):**
- Tool definitions (JSON schema for the SDK).
- Tool registration surface (a small registry that B7 can also plug into for figure/attachment tools).
- Tool handlers in the subprocess.
- Git operations via `subprocess` (matches Slice A's Phase 0 pattern).
- Atomic "commit-and-write-row" for artifact edits.
- Tests for each tool against a real git repo + mocked SDK call site.

**Scope (out — deliberately dropped because the phased workflow doesn't use them):**
- **Branching** (`create_branch`, `switch_branch`, `list_branches`) — base spec line 217–218, dropped by phased spec line 33.
- **Past-grant tools** (`list_past_grants`, `read_past_grant`) — base spec line 224, dropped by phased spec line 35.
- **`search_pubmed`** — base spec line 224, dropped by phased spec line 32.
- **Figure/attachment tools** (`describe_figure`, `look_at_figure`, `read_preliminary_data`) — owned by B7, but they register through B5's tool surface (see dependency graph above).
- Chat UI (B6).

**Estimated tasks:** ~25–30.

**Dependencies:** B1, B3, B4.

**File:** Not yet written.

---

### B6 — Workspace web UX

**Goal:** Build the web UI for workspaces: list, create, view (chat + artifact panes), settings (viewer management, export), and the "Start drafting workspace" button on `/calls/<id>`.

**End state:** A logged-in user can create a workspace from a call ranking, chat with the agent, see the artifact rendered as markdown (raw toggle, version selector, diff view), add a viewer, export the proposal as Word/PDF/markdown, and see disclosure banner + per-turn token banner.

**Scope (in):**
- `/workspaces` (list, filter by status)
- `/workspaces/new` (form: title, optional call_id, optional profile_id, deadline)
- `/workspaces/<id>` (left: chat + transcript via HTMX; right: artifact pane)
- `/workspaces/<id>/settings` (viewers, archive)
- `/workspaces/<id>/export` (Word/PDF/markdown formats)
- "Start drafting workspace" button on `/calls/<id>` (pre-fills call + profile)
- Disclosure modal + per-user, per-workspace ack
- Tests for each page + workflow

**Scope (out):** Agent SDK (B3), turn queueing (B4), tool plumbing (B5), figures (B7), kill switch backend (B8).

**Estimated tasks:** ~25–30.

**Dependencies:** B1, B3, B4, B5.

**File:** Not yet written.

---

### B7 — Figures + attachments + vision

**Goal:** Implement attachment upload, vision-on-ingest (`describe_figure` job), and the agent-facing attachment tools (`look_at_figure`, `read_preliminary_data`).

**End state:** A user uploads a figure → file saved to `var/workspaces/<id>/attachments/` → `attachment_jobs(kind='describe_figure')` enqueued → vision call populates `attachments.description`. Agent calls `look_at_figure(id)` and the next turn carries the image bytes; agent calls `read_preliminary_data(id)` and gets extracted text.

**Scope (in):**
- Attachment upload endpoint + multipart handler.
- Disk storage layout under `var/workspaces/<id>/attachments/`.
- `attachment_jobs` drain (APScheduler tick) calling the vision API.
- `look_at_figure` and `read_preliminary_data` tool handlers (must produce attachment-reference tool-result blocks, NOT inflate bytes into `chat_turns.content` per spec line 193).
- PDF/CSV text extraction for `read_preliminary_data`.
- Tests.

**Scope (out):** Past-grant ingestion (deferred per spec).

**Estimated tasks:** ~20–25.

**Dependencies:** B1, B4 (turn machinery for the tool-result attachment-reference blocks), and **B5 for the agent-tool handlers** (`look_at_figure`, `read_preliminary_data`) which register through B5's tool surface. The upload/storage half + `describe_figure` background job are parallel-safe with B5.

**File:** Not yet written.

---

### B8 — Disclosure + kill switch + spend guardrails

**Goal:** Implement the compliance surface: disclosure banner + ack, kill switch (admin-flippable global flag), per-turn token banner, daily per-workspace token cap, audit trail completeness.

**End state:** A new user opening a workspace sees the disclosure modal (records `workspace_events(kind='disclosure_acknowledged')`). Admin can flip a kill switch → all workspaces become read-only (no new turns accepted). Per-turn the chat UI shows estimated input/output tokens and cumulative daily usage. When the daily cap is hit, the workspace pauses gracefully with a "resumes in N hours" message.

**Scope (in):**
- Disclosure modal + Jinja partial + ack endpoint.
- Kill switch flag (settings table or env-flagged — decided in B8's plan).
- Per-turn token banner (chat UI fragment).
- **Daily per-workspace token cap — surfacing only** (banner, admin override UI, "resumes in N hours" status). The enforcement gate that actually blocks new turns lives in B4's enqueue path.
- Admin view (`/admin`) showing per-workspace token usage + kill-switch toggle.
- Tests for each.

**Scope (out):** Per-user monthly cap (deferred per phased spec line 351).

**Estimated tasks:** ~15–20.

**Dependencies:** B1, B4.

**File:** Not yet written.

---

## Total estimate

~165–200 tasks across 8 plans. Each plan independently shippable.

Critical path from current state to "end user can chat with agent": **B1 → B3 → B4 → B5 → B6** (~110–135 tasks total).

Parallel tracks once B1 lands: **B2** (rename, any time) and **B7/B8** (after B4).

Slice A Part 2 unblocks the moment B1 lands.
