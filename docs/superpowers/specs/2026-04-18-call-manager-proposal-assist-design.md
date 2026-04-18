# Call Manager — Proposal-Assist (Future-Phase Design Spec)

**Date:** 2026-04-18
**Status:** Brainstormed design, pending engineering review
**Audience:** Engineers and stakeholders at the hospital
**Supersedes for this phase:** the "Application tracking" (medium-term) and "Application-prep helpers" (longer-term) items in [2026-04-14-call-monitor-design.md](2026-04-14-call-monitor-design.md)'s Roadmap.

## Purpose

Add a **drafting workspace** to the tool: a chat-driven, iterative proposal-writing surface that uses a frontier LLM (Claude Opus 4.7) via the Claude Agent SDK, with the call's requirements, the group's profile, preliminary data, and figures available to the agent.

This is **future-phase** work — not part of ranking v2. It is designed, not scheduled. Implementation begins only after ranking v2 stabilizes.

The output of this phase is (a) a drafting module that subsumes the roadmap's "application tracking" item, (b) a rename from `call-monitor` to `call-manager`, and (c) an architecture that keeps ranking on-prem Gemma while isolating the cloud frontier-model use to the drafting surface.

## Rename: `call-monitor` → `call-manager`

The v1 name describes what the tool *watched*. After this phase the tool also *acts* — it helps draft the outputs the monitoring was for. "Manager" captures both roles. The rename applies to the Python package, the repo, the deployment service, the Postgres database, and user-facing copy. See Deployment for the cutover plan.

## Scope (this phase)

**In scope:**

- Drafting workspaces (chat + proposal artifact + attachments + tracking state).
- Claude Agent SDK-backed agent, Opus 4.7 as the default model.
- Vision-on-ingest + re-vision-on-demand for figures.
- Shared-read, single-write collaboration.
- Subsumes the medium-term "application tracking" roadmap item (status, deadline, files) into the workspace entity.
- Rename `call-monitor` → `call-manager`, coordinated one-shot cutover.

**Explicitly out of scope for this phase:**

- Multi-author live editing (Google-Docs-style).
- Real-time presence indicators.
- Automated submission to funders — the proposal is exported, humans submit.
- Proposal-quality scoring / "will this win?" estimates.
- Running the drafting agent against local frontier models (Opus-only; interface stays swap-ready).
- Per-workspace model overrides or per-user API keys.
- Templated section scaffolding per funder (Horizon Europe-specific layouts, etc.).
- Pushing workspace git repos to external remotes.

## Architecture

### New module

`drafting/` — peer to the existing `sources/`, `ingestion/`, `ranking/`, `profiles/`, `digest/`, `web/`, `db/`.

### Execution model

The drafting agent runs via the **Claude Agent SDK** in a per-workspace subprocess, launched on-demand when a user opens a workspace chat and torn down after an idle timeout (`WORKSPACE_IDLE_TIMEOUT_SECONDS`, default 600).

Each subprocess has:

- A per-workspace working directory on disk, e.g., `var/workspaces/<uuid>/`, containing:
  - `proposal.md` — the artifact (a git-tracked working copy).
  - `attachments/` — uploaded figures, preliminary data, past-grant files.
  - `figure_descriptions/` — text descriptions produced by `describe_figure`.
  - `.git/` — a git repo tracking `proposal.md` and other workspace files (see Artifact versioning).
- A bound custom tool set (see Tools).
- The `ANTHROPIC_API_KEY` from env, never exposed to the browser.

The FastAPI web layer speaks to the subprocess over stdio (the SDK's native transport), streams agent output back to the browser via SSE, and persists turns + artifact pointers to Postgres. The subprocess is disposable — the state of record is the Postgres rows + the workspace working directory, not the live process.

### Why this shape

- Matches the SDK's assumptions (local filesystem, one agent loop per "session").
- Keeps sensitive operations server-side (API key, tool execution, file system).
- Lets the web layer stay stateless and multi-request.
- Idle-timeout + on-demand launch bounds concurrent cost — 10 idle workspaces is zero subprocesses.

### New runtime dependencies

- `claude-agent-sdk` (pinned version)
- `anthropic` (for one-shot vision calls outside the SDK loop)
- A markdown-to-Word export path (`pypandoc` or `python-docx`, decided at implementation)
- `git` (system binary, already standard on Linux)

### Single-instance constraint preserved

The existing hard constraint (APScheduler needs single-instance) is unchanged. Drafting subprocesses are local to the web instance that launched them. Under single-instance this is trivially safe. If multi-instance becomes necessary later, workspace-to-instance affinity via a `workspaces.served_by_instance` column + advisory lock handles it; out of scope now.

### SDK version churn risk

The Claude Agent SDK is young; breaking changes are likely. All SDK interaction is wrapped behind an internal `DraftingAgent` adapter class so an SDK upgrade affects one file. `DraftingAgent` exposes: `launch(workspace_id) -> handle`, `send_turn(handle, user_msg)`, `terminate(handle)`, and an async stream interface.

## Data model

Added to the existing seven core + three auxiliary tables:

```
workspaces
  id (uuid)
  title (text)
  owner_id (FK users(id))
  call_id (FK calls(id), nullable)           -- optional link to a ranked call
  profile_id (FK profiles(id), nullable)     -- optional; seeds context
  status (text)                              -- 'drafting' | 'submitted' | 'awarded' | 'rejected' | 'abandoned'
  deadline (timestamptz, nullable)           -- copied from call if linked, editable
  created_at, updated_at (timestamptz)
  soft_deleted_at (timestamptz, nullable)

workspace_viewers
  workspace_id (FK workspaces(id))
  user_id (FK users(id))
  added_at (timestamptz)
  PRIMARY KEY(workspace_id, user_id)

chat_turns
  id (uuid)
  workspace_id (FK workspaces(id))
  role (text)                                -- 'user' | 'assistant' | 'tool'
  content (jsonb)                            -- SDK message payload. Attachment bytes and
                                             --   preliminary-data text are stored by reference only
                                             --   (see "Attachment references" design note).
  tokens_in (int, nullable)
  tokens_out (int, nullable)
  model (text, nullable)                     -- e.g., 'claude-opus-4-7'
  created_at (timestamptz)
  INDEX on (workspace_id, created_at)

turn_jobs
  id (uuid)
  workspace_id (FK workspaces(id))
  user_turn_id (FK chat_turns(id), nullable) -- populated once the user message row is written
  status (text)                              -- 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  error (text, nullable)                     -- populated on status='failed'
  queued_at (timestamptz)
  started_at (timestamptz, nullable)
  completed_at (timestamptz, nullable)
  INDEX on (workspace_id, queued_at)
  UNIQUE INDEX on (workspace_id) WHERE status = 'running'
    -- enforces "one running turn per workspace" at the DB level; the subprocess also
    -- holds pg_advisory_lock(hashtext('workspace:' || workspace_id::text)).

chat_summaries
  id (uuid)
  workspace_id (FK workspaces(id))
  summary (text)                             -- compaction output: bounded-size summary
                                             --   of all turns up to and including summarized_through
  summarized_through (FK chat_turns(id))     -- last turn included in this summary
  tokens (int)                               -- summary size, for budgeting
  model (text)                               -- model used to produce the summary
  created_at (timestamptz)
  INDEX on (workspace_id, created_at DESC)

attachment_jobs
  id (uuid)
  workspace_id (FK workspaces(id))
  attachment_id (FK attachments(id))
  kind (text)                                -- 'describe_figure' for v1 (room to extend)
  status (text)                              -- 'queued' | 'running' | 'completed' | 'failed'
  attempts (int, default 0)
  last_error (text, nullable)
  queued_at, updated_at (timestamptz)
  INDEX on (status, updated_at)
  UNIQUE INDEX on (attachment_id, kind) WHERE status IN ('queued', 'running')

artifact_versions
  id (uuid)
  workspace_id (FK workspaces(id))
  commit_sha (text)                          -- pointer into the workspace's git repo
  triggering_turn_id (FK chat_turns(id), nullable)
  created_by (text)                          -- 'agent' | 'user' | 'system'
  message (text)                             -- commit message (also stored in git)
  created_at (timestamptz)
  INDEX on (workspace_id, created_at DESC)

attachments
  id (uuid)
  workspace_id (FK workspaces(id))
  kind (text)                                -- 'figure' | 'preliminary_data' | 'past_grant' | 'other'
  filename (text)
  storage_path (text)                        -- relative to var/workspaces/<id>/attachments/
  mime_type (text)
  description (text, nullable)               -- populated asynchronously by the describe_figure job
                                             --   (see attachment_jobs); null until the job completes
  uploaded_by (FK users(id))
  uploaded_at (timestamptz)

workspace_events
  id (uuid)
  workspace_id (FK workspaces(id))
  kind (text)                                -- 'created' | 'status_changed' | 'exported' |
                                             --   'cloud_turn_sent' | 'disclosure_acknowledged' | ...
  payload (jsonb)
  actor_id (FK users(id), nullable)
  created_at (timestamptz)
```

**Design notes:**

- **`chat_turns.content` is jsonb** storing the SDK's native message shape (tool-use and tool-result blocks included). This makes resuming a subprocess from history a replay rather than a reconstruction.
- **Attachment references, not bytes.** Tool-result blocks that carry attachment content (`look_at_figure`, `read_preliminary_data`) store a reference — `{"attachment_id": "<uuid>", "kind": "figure" | "preliminary_data"}` — not inflated bytes or text. The subprocess re-inflates from disk on replay. This is a hard rule: no attachment bytes, image base64, or preliminary-data text may appear in `chat_turns.content`. Violations bloat Postgres, duplicate the "files on disk, metadata in DB" rule, and blow up the backup footprint.
- **Turn lifecycle lives in `turn_jobs`, not `chat_turns`.** A user sends a message → one row in `chat_turns` (role='user') + one row in `turn_jobs` (`status='queued'`), committed in the same DB transaction. The subprocess picks the oldest queued job for its workspace, transitions it to `running`, generates the agent response(s) (which append assistant/tool rows to `chat_turns`), and transitions the job to `completed` or `failed`. Assistant/tool rows never get their own `turn_jobs` row — they are results, not requests.
- **Workspace lock.** The subprocess holds `pg_advisory_lock(hashtext('workspace:' || workspace_id::text))` for its lifetime. The unique partial index on `turn_jobs.workspace_id WHERE status='running'` is the database-level safety net. A second subprocess racing to run the same workspace blocks on the advisory lock.
- **Orphan sweep on workspace open.** When a subprocess launches (or `/admin` runs "recover workspace"), a single SQL pass transitions any `running` `turn_jobs` for that workspace to `failed` with `error='orphaned'`. This cleans up after crashes and redeploys.
- **Compaction (bounded replay).** When a workspace's active-conversation size exceeds a threshold (e.g., 100 turns or 200K input tokens since the latest summary), a background job calls the summarizer model on the un-summarized turns and writes a `chat_summaries` row. Subprocess resume = `latest chat_summaries row + all chat_turns after summarized_through`. This caps cold-start cost and context size at a known ceiling regardless of workspace age. Prompt caching further reduces resume cost when the summary+turns prefix is stable.
- **`artifact_versions` is pointers-only** — content history lives in git. The DB stores just enough to join commits to turns and timestamps.
- **`workspace_events`** is the audit trail. `cloud_turn_sent` specifically enables compliance to trace how many turns left the hospital network, when, by whom, for which workspace — without duplicating content.
- **Files on disk, metadata in Postgres.** Proposal markdown lives in the workspace git repo on disk. Attachment bytes live on disk; rows are metadata + description.
- **Soft delete on `workspaces`.** Consistent with the existing `profiles.is_active` pattern. Hard delete is SQL-only.
- **No separate "tracked applications" table.** A workspace *is* the tracked application, whether or not it has a `call_id`. A workspace with no linked call is valid (speculative drafts, non-monitored sources, internal funding lines).

## Tools exposed to the agent

A small, purposeful tool set — not the Claude Code toolbox. Custom tools only; no `bash`, no arbitrary `write_file`, no generic `web_fetch`.

**Artifact-editing tools:**
- `read_proposal()` — returns current `proposal.md`.
- `edit_proposal(edits: list[{old_string, new_string}])` — surgical string-replace edits; auto-commits.
- `replace_proposal(content)` — full overwrite; auto-commits. For large structural changes.

**Version-history tools (git-backed):**
- `get_artifact_history(limit=20)` — recent commits with messages, shas, timestamps.
- `read_artifact_at(commit_sha)` — older version of the proposal.
- `diff_artifact(commit_a, commit_b)` — unified diff.
- `create_branch(name)` — start an alternative draft.
- `switch_branch(name)` / `list_branches()`.

**Context-retrieval tools:**
- `get_call_details()` — full call text, programme, deadline, budget, link; empty if no linked call.
- `get_profile()` — description, structured fields, `must_include` / `hard_exclusions`; empty if none selected.
- `list_past_grants()` / `read_past_grant(id)` — grants pasted into the linked profile.
- `search_pubmed(query, limit=10)` — reuses the existing E-utilities client from profile bootstrap.

**Attachment tools:**
- `list_attachments()` — metadata + descriptions.
- `describe_figure(attachment_id)` — one-shot vision call; populates `attachments.description` if null.
- `look_at_figure(attachment_id)` — attaches the image to the *next* turn so the agent can see pixels directly.
- `read_preliminary_data(attachment_id)` — reads CSV / PDF / text into the conversation.

Tools are the security boundary. Everything the agent can do is in this list; anything outside requires new code + review.

## Artifact versioning (git-backed)

Each workspace's working directory is a git repo initialized at workspace creation.

**Commit cadence.** Every successful `edit_proposal` / `replace_proposal` tool call auto-commits with a message like `agent: <first 80 chars of triggering user turn> [turn=<id>]`. User-initiated save points become tagged commits (e.g., `user-save/2026-06-12-draft-for-review`).

**Branching.** `create_branch` / `switch_branch` / `list_branches` expose git branches to the agent and user. The chat stream itself stays linear (one conversation per workspace); branches live in the artifact only. A branch switch is recorded as a system/tool message in the chat so the transcript narrates the story.

**Export.** "Export as Word / PDF / markdown" exports the current branch HEAD. Exporting a specific version exports at a specific commit.

**External sync (deferred).** One repo per workspace means "push this proposal to my own git remote" is a future feature, not a redesign. Out of scope for this phase.

**DB ↔ git consistency.** The filesystem is outside Postgres's transaction boundary, so perfect atomicity isn't achievable. The design accepts this and defines an explicit reconciliation policy instead.

Write order:

1. Git commit in the workspace repo (gets commit sha `X`).
2. DB transaction: insert the triggering `chat_turns` row (if applicable) + `artifact_versions` row with `commit_sha=X` + mark the corresponding `turn_jobs` row `completed`. Commit.

If step 1 fails: no state change anywhere.
If step 2 fails: git has commit `X`, DB does not know about it. This is the drift case.

**Reconciliation is forward-only and never destructive.** Git commits are never undone by reconciliation — they are the user's and agent's work product, and `git reset` is not a bulk recovery tool. Instead:

- **Orphan commit (sha in git, no `artifact_versions` row):** backfill by inserting an `artifact_versions` row reading message and timestamp from `git log`. `triggering_turn_id` is left null; `created_by='reconciled'`. A `workspace_events` row (`kind='git_drift_reconciled'`) records what was backfilled.
- **Orphan row (sha in `artifact_versions`, no git commit):** impossible under the write order above (the git commit happens first), so if it occurs it signals a deeper bug — the row is marked with `created_by='unrecoverable'` and an admin alert fires. The row is not deleted, to preserve audit history.

Triggers:

- **On workspace open:** the workspace-reopen code runs a fast reconciliation pass before the subprocess launches.
- **On-demand:** `/admin` "verify and reconcile workspace" button runs the same pass against one or all workspaces.

Backups: `var/workspaces/` is in the backup set alongside Postgres. A cross-consistent restore requires snapshots taken at the same point — operationally, restore Postgres first, then the working directory, then run reconciliation.

**Commit overhead.** ~20–50 ms per commit on SSD — negligible vs turn latency dominated by Opus (seconds).

## Figures and preliminary data

**Hybrid vision: describe-on-ingest, re-vision on demand.**

On upload of an image attachment, the server writes the `attachments` row and enqueues a `describe_figure` job in `attachment_jobs`. The upload request returns as soon as the row is written — upload success does **not** depend on Anthropic being reachable. The attachment appears in the UI with a "description pending" badge.

A background worker (same process, separate async task) drains `attachment_jobs`:

- On success → `attachments.description` is populated, a system/tool message is appended to the workspace chat ("Figure figure-1.png described: …"), and the badge clears.
- On transient failure (HTTP 5xx, timeout) → exponential backoff retry (e.g., 30 s, 2 min, 10 min), up to 5 attempts.
- After 5 failures → job `status='failed'`, `attachments.description` remains null, and the UI shows a "description failed — retry" button that enqueues a fresh job.

The one-shot vision call is model-configurable (likely Claude Haiku 4.5 for cost — confirmed at implementation) and does not require the agent subprocess; it is a direct Anthropic SDK call from the worker.

Subsequent turns carry only the text description. When the agent needs to double-check a visual claim, it calls `look_at_figure(attachment_id)` to re-attach the image to the next turn. `read_preliminary_data(attachment_id)` similarly pulls CSV/PDF text into the conversation on demand. Both tool results are stored in `chat_turns.content` as references, not inflated bytes (see Data model — Attachment references).

This matches the Q3-C decision: description by default, re-vision on demand. It avoids paying vision token cost on every turn, keeps the re-check path available, and decouples upload UX from Anthropic availability.

## Workspace UX

### New pages

- `/workspaces` — list of workspaces visible to the user (owned or viewer-added). Filter by status. Columns: title, linked call (if any), status, deadline, last activity, owner.
- `/workspaces/new` — create: title, optional call link (searchable against `calls`), optional profile selection, deadline. Owner = current user.
- `/workspaces/<id>` — the main two-pane view:
  - **Left:** chat (message history, composer, attachment button, "cloud data" banner, model info).
  - **Right:** proposal artifact (rendered markdown by default; raw markdown on toggle). Version selector, branch indicator, diff view.
- `/workspaces/<id>/attachments` — attachment manager (upload, preview, delete, re-run `describe_figure`).
- `/workspaces/<id>/settings` — title, status, deadline, linked call/profile, viewers, delete.
- `/workspaces/<id>/export` — export current branch HEAD as Word / PDF / markdown.
- `/calls/<id>` gets a **new button:** "Start drafting workspace" (pre-fills the linked call and the profile that scored it highest for the current user).

### Chat mechanics

- Attachments upload per-turn or per-workspace. Image uploads return immediately; `describe_figure` runs asynchronously in the background (see Figures). The description renders inline in the chat as a system/tool message once the job completes.
- Long agent turns stream via SSE. The artifact pane refreshes on each `edit_proposal` / `replace_proposal` completion.
- A small "branch: main" indicator in the artifact pane is the source of truth for the current branch.
- User can send a turn at any time. Each submission writes a `chat_turns` row + a `turn_jobs` row (`status='queued'`) in one DB transaction. The agent subprocess drains queued jobs in FIFO order — one `running` job per workspace at a time, enforced by a unique partial index on `turn_jobs` and a workspace-level `pg_advisory_lock` held by the subprocess. A queued-but-not-running turn is visible in the UI (position indicator) and is cancellable by the user before it starts. Multi-tab submissions land as ordered queue entries; they do not race or duplicate.
- On reconnect or page refresh during a running turn, the UI re-subscribes to the stream via a per-`turn_jobs.id` resume token. If the subprocess crashed, the orphan-sweep on workspace open transitions the orphaned `running` job to `failed` with `error='orphaned'`, and the UI surfaces it with a "retry" action that enqueues a fresh turn.

### Collaboration (shared-read, single-write)

- Viewers see the chat transcript and artifact read-only. They cannot send turns, edit the artifact, or upload attachments.
- The workspace settings page is where the owner adds viewers (by existing-user email).
- No real-time "who's here" presence indication.

### Admin surfacing

`/admin` adds a "Drafting" section: active workspaces (count), active subprocesses (count, with idle-eviction indicator), total cloud tokens used this week, workspaces with failed agent turns, and the global `DRAFTING_ENABLED` toggle.

## Compliance, disclosure, and kill switch

### User-facing disclosure

First time a user opens a workspace chat (i.e., the workspace has zero `chat_turns`), a modal:

> **This workspace sends data to Anthropic's API.**
>
> Opus 4.7 runs on Anthropic's servers, not on hospital hardware. When you send a message, the text and any attached figures or files leave the hospital network. Anthropic does not train on this data under our enterprise agreement and retains it only for abuse monitoring.
>
> **Do not paste patient identifiers, raw patient data, or information covered by consent restrictions** into this workspace. Research ideas, preliminary results, citation context, and collaborator names at the level normally included in a grant proposal are appropriate.
>
> ☐ I understand. Don't show this again for this workspace.

Acknowledgment records `workspace_events.kind = 'disclosure_acknowledged'` with user id and timestamp. Per-user, per-workspace. A viewer who joins later sees their own first-view modal.

### Per-turn banner

A small persistent banner above the chat composer: "Turns in this workspace are sent to Anthropic's Opus 4.7." Non-dismissable.

### Audit trail

`workspace_events.kind = 'cloud_turn_sent'` rows give compliance an after-the-fact audit trail. Payload records the turn id and token counts — never the turn content (the content is in `chat_turns`).

### Global kill switch

`DRAFTING_ENABLED` env var defines the startup value; an admin-UI toggle can change it at runtime. The env var is re-read on every restart and overwrites any runtime change — so setting `DRAFTING_ENABLED=false` in env and deploying is the durable way to disable drafting; the UI toggle is the faster way. When disabled:

- New workspace creation is blocked.
- Existing workspaces are read-only — chat/artifact history viewable, no new turns accepted, no artifact edits.
- The ranking side is unaffected.

This is the "legal says no" escape hatch. Shipping without it and rebuilding it later is more expensive.

### Rate / spend guardrails

- Per-workspace daily token ceiling (default 500K input + 100K output; overridable in admin). Exceeding returns a chat error and surfaces in admin.
- Global daily ceiling across all workspaces, same mechanism.
- Cost overruns show up in the admin drafting panel, not as silent bill shock.

### API key management

`ANTHROPIC_API_KEY` added to env. Rotation is a deploy-level concern. No per-user keys in this phase.

## Deployment and the rename migration

### New env vars

- `ANTHROPIC_API_KEY` — required.
- `DRAFTING_ENABLED` — default `true`; kill switch.
- `WORKSPACES_DIR` — default `var/workspaces/`; must be writable, backed up, excluded from the docker image.
- `DRAFTING_MODEL` — default `claude-opus-4-7`; one place to swap for Sonnet.
- `WORKSPACE_IDLE_TIMEOUT_SECONDS` — default 600.
- `WORKSPACE_DAILY_TOKEN_LIMIT_IN` / `_OUT` — defaults 500K / 100K.

### Rename cutover (`call-monitor` → `call-manager`)

The rename requires downtime because `ALTER DATABASE ... RENAME` requires zero open connections. Under the single-instance deploy, this is a scheduled maintenance window (announce ≥24 h in advance; the actual rename is seconds).

**Pre-merge changes (no downtime):**

1. `src/call_monitor/` → `src/call_manager/`. Systematic import-path rename; module file names stay identical. Tests pass on the new name.
2. Repo rename on GitHub with `call-monitor` retained as an automatic redirect.
3. User-facing copy updates (page titles, email "from" name, email subject templates).
4. Tag a release immediately before the cutover so rollback is a single `git checkout`.

**Cutover runbook (the scheduled window):**

1. Announce start of window on the internal channel.
2. `systemctl stop call-monitor` — stops the web + scheduler process, drains connections.
3. Verify no remaining Postgres connections to `call_monitor`:
   `SELECT pid, state FROM pg_stat_activity WHERE datname = 'call_monitor';` — must be empty.
   If non-empty, investigate (likely an admin psql session) and close it.
4. `ALTER DATABASE call_monitor RENAME TO call_manager;` — run as a Postgres superuser connected to a different database (`postgres` is fine).
5. Deploy the call-manager release: update `DATABASE_URL` to point at `call_manager`, install the new systemd unit `call-manager.service`, disable `call-monitor.service`.
6. `systemctl start call-manager`.
7. Smoke-test: `/healthz`, `/` (loads), `/admin` (loads), trigger one ingestion run manually from admin.
8. Announce end of window.

**Rollback (if smoke-test or post-start traffic fails):**

1. `systemctl stop call-manager`.
2. Verify no connections to `call_manager`: `SELECT pid FROM pg_stat_activity WHERE datname = 'call_manager';` — empty.
3. `ALTER DATABASE call_manager RENAME TO call_monitor;`.
4. Redeploy the prior tagged release (call-monitor) with `DATABASE_URL` pointing at `call_monitor`.
5. `systemctl start call-monitor`.
6. Investigate the failure offline.

Both the cutover and rollback fit in a single maintenance window of well under an hour. No backwards-compatibility shims — under single-instance deploy with a small user base, shims cost more total engineering time than a clean one-shot rename with a planned window.

## Testing

- **Unit tests per tool.** Each custom agent tool (`edit_proposal`, `search_pubmed`, `describe_figure`, git tools) tested in isolation with a fake workspace directory. No Opus traffic.
- **Integration test for the agent loop.** One end-to-end test that launches an agent subprocess against a **mock SDK server** (stdio-compatible fake returning scripted responses) and asserts: chat turns persist, artifact commits happen, branching works, tool calls are recorded.
- **Contract test against the live API.** One tagged test (`@pytest.mark.live`) runs a short real conversation against Opus weekly in CI. Guards against SDK and API shape drift. Excluded from the default test run.
- **Golden drafting test.** A tiny fixture — one call + one profile + one preliminary data file — with an expected-shape assertion (artifact has N sections, references the preliminary data). Weekly cron alongside the existing golden-ranking eval.
- **Workspace/git correctness tests.** Verify DB `commit_sha` values exist in git; verify commits-match-turns pairing; verify branch tools don't corrupt state.
- **Turn-queue tests.** Concurrent submissions from two simulated tabs land as ordered queue entries with no duplicates. A kill-9 on a subprocess mid-turn leaves exactly one `running` row; the next workspace open transitions it to `failed` with `error='orphaned'` and accepts new turns. The unique partial index rejects a second `running` row for the same workspace.
- **Reconciliation tests.** Inject a git commit with no `artifact_versions` row; open-workspace reconciliation backfills it and writes a `git_drift_reconciled` event. Inject an `artifact_versions` row with no matching commit; reconciliation marks it `unrecoverable` and fires an admin alert. No `git reset` is ever invoked.
- **Compaction tests.** With a crafted workspace whose chat history exceeds the compaction threshold, verify a `chat_summaries` row is produced and that resume uses `summary + turns-since-summary` rather than full-history replay.
- **Attachment-job tests.** Uploading an image returns before Anthropic would have responded; the attachment row exists with null description; the background worker drains the job and populates the description. With Anthropic mocked to always 500, the job retries and then transitions to `failed` with a retry button available.
- **No browser tests.** Server-rendered + HTMX; `TestClient` covers routes.

## Out of scope (revisit later)

- Multi-author live editing.
- Real-time presence indicators.
- Automated submission to funders.
- Proposal-quality scoring.
- Local frontier-model drafting.
- Per-workspace model or API-key overrides.
- Pushing workspace git repos to external remotes.
- Per-user Anthropic keys or per-workspace cost allocation reporting.
- Funder-specific templated section scaffolding.
- Bulk operations across workspaces.

## Open questions

- Word export: `pypandoc` (richer, depends on bundled pandoc binary) vs `python-docx` (lighter, pure Python, limited styling).
- Retention policy for `artifact_versions` and workspace git histories — defer until real usage patterns are visible.
- Whether to surface the branch UI to users directly or keep it agent-driven for the first release.
- Whether `describe_figure` uses Opus (best quality) or Haiku (cheap, usually enough) for its one-shot vision call — likely Haiku; confirmed at implementation.
- Whether `/workspaces` should be the new landing page or `/` (ranked calls) remains the landing.
