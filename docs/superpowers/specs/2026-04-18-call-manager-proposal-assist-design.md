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
  content (jsonb)                            -- full SDK message payload, incl. tool calls/results
  tokens_in (int, nullable)
  tokens_out (int, nullable)
  model (text, nullable)                     -- e.g., 'claude-opus-4-7'
  created_at (timestamptz)
  INDEX on (workspace_id, created_at)

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
  description (text, nullable)               -- populated by vision-on-ingest for figures
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

- **`chat_turns.content` is jsonb** storing the SDK's native message shape (including tool-use and tool-result blocks). This makes resuming a subprocess from history a straightforward replay.
- **`artifact_versions` is pointers-only** — actual content history lives in git (see Artifact versioning). The DB stores just enough to join commits to turns and timestamps.
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

**Drift risk (DB ↔ git).** Mitigations:
- `artifact_versions.commit_sha` is written inside the same DB transaction that records the triggering `chat_turns` row, and only *after* the git commit succeeds. A failed commit → no DB row.
- An `/admin` "verify workspace" action walks each workspace, confirms every recorded sha exists in git, flags mismatches.
- Backups: `var/workspaces/` is in the backup set alongside Postgres. Restore requires both.

**Commit overhead.** ~20–50 ms per commit on SSD — negligible vs turn latency dominated by Opus (seconds).

## Figures and preliminary data

**Hybrid vision: describe-on-ingest, re-vision on demand.**

On upload of an image attachment, the server runs `describe_figure` synchronously (a one-shot vision call — likely Haiku for cost; confirmed at implementation). The returned description is stored in `attachments.description` and surfaced inline in the chat as a system/tool message: "Figure figure-1.png described: …". The user can see what the agent now "knows."

Subsequent turns carry only the text description. When the agent needs to double-check a visual claim, it calls `look_at_figure(attachment_id)` to re-attach the image to the next turn.

Preliminary data (CSVs, PDFs) is analogous — `read_preliminary_data` pulls text content into the conversation on demand, rather than keeping it in context permanently.

This matches the Q3-C decision: description by default, re-vision on demand. It avoids paying vision token cost on every turn while keeping the "re-check when it matters" path available.

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

- Attachments upload per-turn or per-workspace. Uploading an image triggers `describe_figure` synchronously; the description renders inline in the chat.
- Long agent turns stream via SSE. The artifact pane refreshes on each `edit_proposal` / `replace_proposal` completion.
- A small "branch: main" indicator in the artifact pane is the source of truth for the current branch.
- User can send a turn at any time (no blocking while the agent generates), but the agent completes its current turn before starting the next — no interleaved edits.

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

One coordinated change at cutover:

1. **Code/package:** `src/call_monitor/` → `src/call_manager/`. Systematic import-path rename; module file names stay identical.
2. **Repo:** rename the GitHub repo; keep `call-monitor` as a redirect alias.
3. **Database:** `ALTER DATABASE call_monitor RENAME TO call_manager` as part of the deploy script; connection strings updated in the same release.
4. **Service/deployment:** systemd unit or orchestrator service renamed.
5. **Env-var prefix:** existing vars don't use a `CALL_MONITOR_` prefix, so no migration needed.
6. **User-facing copy:** page titles, email "from" name, email subject templates.
7. A tagged release marks the rename point so rollbacks are clean.

No backwards-compatibility shims. Small user base, single-instance deploy, one-shot rename.

## Testing

- **Unit tests per tool.** Each custom agent tool (`edit_proposal`, `search_pubmed`, `describe_figure`, git tools) tested in isolation with a fake workspace directory. No Opus traffic.
- **Integration test for the agent loop.** One end-to-end test that launches an agent subprocess against a **mock SDK server** (stdio-compatible fake returning scripted responses) and asserts: chat turns persist, artifact commits happen, branching works, tool calls are recorded.
- **Contract test against the live API.** One tagged test (`@pytest.mark.live`) runs a short real conversation against Opus weekly in CI. Guards against SDK and API shape drift. Excluded from the default test run.
- **Golden drafting test.** A tiny fixture — one call + one profile + one preliminary data file — with an expected-shape assertion (artifact has N sections, references the preliminary data). Weekly cron alongside the existing golden-ranking eval.
- **Workspace/git correctness tests.** Verify DB `commit_sha` values exist in git; verify commits-match-turns pairing; verify branch tools don't corrupt state.
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
