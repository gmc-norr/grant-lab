# Proposal-Assist — Unblock the Workspace Lifecycle — Design Spec

**Date:** 2026-06-12
**Status:** Draft for implementation
**Audience:** Engineers working on call-manager
**Theme:** Product review 2026-06-11, Theme 7 (proposal-assist workspace seams) — first slice ("unblock the lifecycle").

## Purpose

A newly created proposal-assist workspace is a **dead-end**: Phase 0 parks it at `waiting_for_rubric_review` ([setup.py:373](../../../src/call_manager/proposal_assist/phases/setup.py#L373)), but nothing in the UI links to the page that unblocks it, and the composer is live the whole time — so the owner can't advance their own workspace and any message typed in the first ~30 seconds runs the agent against a not-yet-created git repo and fails silently.

This slice makes a new workspace **advanceable end-to-end without shell access**: gate the composer on setup state, show a status card that links to the rubric-review gate when it's ready, auto-poll while Phase 0 runs, humanize the misleading phase chip, and close the first-message race with a server-side guard.

### The gaps, with evidence (current code)

1. **The rubric-review gate is orphaned.** Phase 0 sets `WorkspaceSetupJob.status = 'waiting_for_rubric_review'` ([setup.py:373](../../../src/call_manager/proposal_assist/phases/setup.py#L373)) and stops. The page that advances it — `GET/POST /workspaces/{id}/rubric-review` ([rubric_review.py:54](../../../src/call_manager/proposal_assist/web/rubric_review.py#L54), [:112](../../../src/call_manager/proposal_assist/web/rubric_review.py#L112)) — exists and works (its POST flips the job to `done` and `current_phase` to `spec`, then redirects to the workspace — [rubric_review.py:163-179](../../../src/call_manager/proposal_assist/web/rubric_review.py#L163-L179)). But `get_workspace_main` ([workspaces.py:499](../../../src/call_manager/proposal_assist/web/workspaces.py#L499)) never queries the setup job and the template always renders the composer for owners ([workspace_main.html:34-91](../../../src/call_manager/web/templates/proposal_assist/workspace_main.html#L34-L91)). There is no discoverable path to the gate.
2. **First-message race.** `current_phase` defaults to `'setup'` ([models.py:652-656](../../../src/call_manager/db/models.py#L652-L656)); creation enqueues a `WorkspaceSetupJob(status='queued')` ([workspaces.py creation path](../../../src/call_manager/workspaces.py#L56-L61)) and redirects straight to the live composer. The git repo is created only when the ≤30s Phase 0 worker runs, so a turn submitted in the first seconds hits a missing repo and fails with no useful signal.
3. **The phase chip is misleading.** The header renders the raw enum value `{{ workspace.current_phase }}` ([workspace_main.html:11](../../../src/call_manager/web/templates/proposal_assist/workspace_main.html#L11)) — e.g. a bare `setup` — with no explanation.

## Goals

- During Phase 0 setup the owner sees a **status card** (not a live composer) that reflects the real `WorkspaceSetupJob` state and, when ready, **links to `/workspaces/{id}/rubric-review`**.
- While setup is in progress the card **auto-polls** and flips to the rubric link the moment it's ready — no manual reload.
- The composer is only live once setup is done (`current_phase != 'setup'`), and a turn submitted during setup is **rejected loudly** (not silently) by the server.
- The phase chip shows a **human-readable** label.
- No schema change; no change to Phase 0, the rubric-review routes, or the turn pipeline beyond the one guard.

## Execution model (the gate)

`current_phase == 'setup'` is true from creation until the rubric-review POST advances the workspace to `'spec'` ([rubric_review.py:166](../../../src/call_manager/proposal_assist/web/rubric_review.py#L166)). That single predicate is the gate:

- **In `setup`** → left pane = status card; composer suppressed; `post_turn` rejects.
- **Past `setup`** (`spec`/`plan`/`draft`/`review`) → composer renders as today.

Within `setup`, the **`WorkspaceSetupJob.status`** drives *which* card renders. The job is unique per workspace ([models.py:1097](../../../src/call_manager/db/models.py#L1097), `uq_workspace_setup_jobs_ws`) with statuses `queued | running | waiting_for_rubric_review | done | failed` and a nullable `last_error`.

## Components

### A. Composer gating (`get_workspace_main` + `workspace_main.html`)

- `get_workspace_main` ([workspaces.py:499](../../../src/call_manager/proposal_assist/web/workspaces.py#L499)) loads the workspace's `WorkspaceSetupJob` (one-row `select … where workspace_id == … `) and passes it (or a derived `setup_status` string) into the template context. One extra indexed query on a page that already issues several.
- `workspace_main.html` left pane: `{% if workspace.current_phase == 'setup' %}` render `partials/setup_status.html`; `{% else %}` render the existing composer block ([workspace_main.html:34-91](../../../src/call_manager/web/templates/proposal_assist/workspace_main.html#L34-L91)) unchanged. The right-hand artifact pane and the disclosure modal are untouched.
- Rationale for gating on `current_phase` (not the job status alone): it's the authoritative lifecycle field, it's already on the workspace object (no null-job edge to special-case for the *composer-vs-card* decision), and it stays correct even if a job row were ever missing.

### B. Status-card partial (`partials/setup_status.html`)

Branches on the setup-job status. The card is the **self-replacing poll element** (component C), so its root element carries the htmx polling attributes only in the non-terminal states.

| Job status | Card content | Polls? |
|---|---|---|
| `queued`, `running` (or **no job row**, defensive) | "Setting up your workspace…" + a quiet working indicator | **yes** |
| `waiting_for_rubric_review` | "Your workspace is ready. Review the rubric to start drafting." + a prominent **link/button to `/workspaces/{id}/rubric-review`** | no |
| `failed` | "Setup failed." + the `last_error` text + guidance: delete and recreate the workspace, or contact an admin | no |
| `done` (only reachable by a still-open card after the rubric was confirmed in another tab — the same transaction sets `current_phase='spec'`) | "Setup is complete." + a link to `/workspaces/{id}` — clicking re-loads the main view, which now renders the composer | no |

Owner vs viewer: the **owner** sees the actionable card (rubric link in the `waiting` state). A **viewer** (shared-read) sees the same status *text* without the owner-only rubric link (the rubric-review route is owner-only — [rubric_review.py:38-43](../../../src/call_manager/proposal_assist/web/rubric_review.py#L38-L43) — so showing a viewer a link they'd get 403 on would be a dead-end of its own). The existing template already distinguishes `is_owner` ([workspace_main.html:34](../../../src/call_manager/web/templates/proposal_assist/workspace_main.html#L34)).

The `last_error` is rendered as escaped text (Jinja autoescape), never as markup.

### C. Poll endpoint (`GET /workspaces/{id}/setup-status`)

- New route in `proposal_assist/web/workspaces.py` returning **only** `partials/setup_status.html` (same context the main view builds for the card). Access mirrors `get_workspace_main`: owner-or-viewer via the existing `_require_owner_or_viewer` ([workspaces.py:380](../../../src/call_manager/proposal_assist/web/workspaces.py#L380)); 404 on missing/soft-deleted.
- The card polls itself: root element has `hx-get="/workspaces/{id}/setup-status"`, `hx-trigger="every 3s"`, `hx-swap="outerHTML"`. Standard htmx "poll until done": while `queued`/`running` the returned partial **includes** the trigger; once `waiting_for_rubric_review` or `failed` the returned partial **omits** it, so the swap replaces the polling element with a non-polling one and polling stops. (Implementation: a single `{% if polling %}`-style guard on the htmx attributes inside the partial, where `polling = status in {queued, running, <none>}`.)
- No new SSE wiring; the chat SSE bridge is for chat turns and is untouched.

### D. Phase chip (`workspace_main.html`)

Replace `{{ workspace.current_phase }}` ([workspace_main.html:11](../../../src/call_manager/web/templates/proposal_assist/workspace_main.html#L11)) with a label from a fixed map covering the schema's five phases ([models.py:587](../../../src/call_manager/db/models.py#L587)): `setup`→"Setting up", `spec`→"Drafting", `plan`→"Planning", `draft`→"Revising", `review`→"Review" (distinct labels — no two phases share a chip). Only `setup` and `spec` are reachable today; the other three are sensible placeholders for when later phases ship. Implemented as a small Jinja dict in the template (or a one-line template filter if cleaner); an unknown value falls back to the raw string so a future phase can't blank the chip. No new machinery — addresses only the "raw enum chip" seam, not a full phase UI.

### E. Server-side turn guard (`post_turn`)

In `post_turn` ([workspaces.py:656](../../../src/call_manager/proposal_assist/web/workspaces.py#L656)), immediately after the existing `_require_owner` call ([workspaces.py:693](../../../src/call_manager/proposal_assist/web/workspaces.py#L693)) and before the kill-switch / submit logic, reject when `ws.current_phase == 'setup'`:

```python
if ws.current_phase == "setup":
    raise HTTPException(
        status_code=409,
        detail="Workspace is still setting up — review the rubric to start drafting.",
    )
```

This closes the first-message race at the source: even a stale tab or a hand-crafted POST can't run a turn against a missing repo and fail silently — it fails fast with a clear reason. And the message is **already user-visible today**: the global `htmx:responseError` handler in app.js ([app.js:88-104](../../../src/call_manager/web/static/app.js#L88-L104)) parses a JSON `{"detail": …}` body and shows it as an inline notice next to the composer, which is exactly what `HTTPException(409, detail=…)` produces. So the loud failure needs no Slice-B work; Slice B only improves the *richer* turn-failure UX (system chat rows, Cancel, reset-on-success).

## Data flow (happy path)

Create → `303 /workspaces/{id}` (phase=`setup`, job=`queued`) → main view left pane shows "Setting up…", polling every 3s → Phase 0 worker (≤30s) advances job `queued`→`running`→`waiting_for_rubric_review` → next poll swaps in the "Review the rubric" link and **stops polling** → owner clicks → rubric-review page → edits/confirms → POST flips job=`done`, phase=`spec`, redirects to `/workspaces/{id}` → main view now renders the **composer** (repo exists; no race).

## Error handling / edge cases

- **Failed setup:** card shows "Setup failed" + `last_error` + recreate guidance; polling stopped. The error is now visible to the owner (previously admin-panel-only).
- **No `WorkspaceSetupJob` row** (shouldn't happen — creation always enqueues one): the card falls back to the neutral "Setting up…" polling state rather than erroring.
- **Viewer during setup:** informational card, no rubric link.
- **Turn POST during setup:** `409` with a clear message (component E).
- **Poll endpoint on a missing/soft-deleted/forbidden workspace:** 404 / 403 exactly as the main view.
- **Race already resolved in another tab** (owner confirmed the rubric elsewhere): on the still-open setup page, the next poll returns the `done` card (terminal, non-polling, links back to the workspace — see the table). The poll never materializes the composer itself.

## Non-goals (other Theme-7 slices, explicitly deferred)

Artifact-pane refresh on turn-complete, failed-turn system chat row + Cancel/working indicator, and error-response rendering (**Slice B**); DOCX/PDF export (**Slice C**); resolved call-title/profile pickers, colored status badge, link-back-to-call, deadline-in-header, version-history restore (**Slice D**); **self-service setup retry** (chosen: inform + recreate, no re-queue endpoint); a full multi-phase UI (only `setup`→`spec` exists today).

## Testing

Following `tests/test_proposal_assist_*` / `tests/test_web_*` patterns (TestClient + `pg_session_factory`, an owner user, a created workspace):

- **Gating:** main view of a workspace in `current_phase='setup'` renders the status card and **not** the `id="composer"` form; a workspace advanced to `spec` renders the composer and not the card.
- **Card per status:** with the `WorkspaceSetupJob` set to each status, the rendered card matches the table in component B — `queued`/`running` include the `hx-trigger` poll attribute and no rubric link; `waiting_for_rubric_review` includes the `/rubric-review` link and **omits** the poll trigger; `failed` shows the `last_error` and omits the trigger.
- **Poll endpoint:** `GET /workspaces/{id}/setup-status` returns the card partial for the current status; enforces owner-or-viewer (viewer gets the no-link variant); 404 on missing/soft-deleted.
- **Owner vs viewer:** a viewer in the `waiting_for_rubric_review` state sees the status text but no rubric link.
- **Turn guard:** `POST /workspaces/{id}/turns` while `current_phase='setup'` returns `409`; the same POST after advancing to `spec` proceeds.

### Existing-test migration (required — the guard changes what a default workspace means)

Existing proposal-assist tests build workspaces without setting `current_phase`, so they sit at the server-default `'setup'` — which until now behaved like a fully-ready workspace. With the guard + composer gating, **every turn-posting and composer-asserting test would 409 / lose its composer**. Affected: the shared `make_workspace` helper ([tests/proposal_assist/conftest.py:214](../../../tests/proposal_assist/conftest.py#L214)) and the direct `Workspace(...)` constructor sites in the turn-posting files (`test_web_workspaces_turns.py`, `test_kill_switch.py`, `test_global_cap.py`, `test_models_chat_summaries.py`, `test_b7_e2e.py`, `test_web_workspaces_disclosure_gate.py`, `test_web_workspaces_viewer_enforcement.py`, plus main-view assertions in the citations/references/files modules that use the helper).

**Migration:** give `make_workspace` a `current_phase: str = "spec"` parameter-with-default (semantically honest — those tests simulate a post-setup workspace), and add `current_phase="spec"` to the direct constructor sites in turn-posting tests. `test_rubric_review_endpoint.py` keeps its own `setup`-phase workspaces — it exercises the setup→spec transition itself and needs no change. The new gate tests explicitly create `'setup'`-phase workspaces.
- **Phase chip:** the header renders "Setting up" for `setup` and "Drafting" for `spec` (not the raw enum); an unknown phase falls back to its raw string.

## Files

- Modify: `src/call_manager/proposal_assist/web/workspaces.py` (`get_workspace_main`: load setup job into context; new `GET /{id}/setup-status` route; `post_turn`: setup guard).
- Create: `src/call_manager/web/templates/proposal_assist/partials/setup_status.html` (the status card + self-poll).
- Modify: `src/call_manager/web/templates/proposal_assist/workspace_main.html` (composer-vs-card branch; humanized phase chip).
- Modify/Add: `src/call_manager/web/static/app.css` if the card needs a rule (reuse existing `.callout`/`.banner` tokens where possible — prefer no new CSS).
- Add tests: `tests/proposal_assist/test_web_workspace_setup_gate.py` (the proposal-assist test tree, not the top-level one).
- Modify tests: `tests/proposal_assist/conftest.py` (`make_workspace` gains `current_phase="spec"` default) + the direct `Workspace(...)` constructor sites in the turn-posting test files listed in the migration note.

No migration.
