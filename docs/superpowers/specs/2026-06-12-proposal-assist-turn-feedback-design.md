# Proposal-Assist — Turn Feedback Loop — Design Spec

**Date:** 2026-06-12
**Status:** Draft for implementation
**Audience:** Engineers working on call-manager
**Theme:** Product review 2026-06-11, Theme 7 (proposal-assist workspace seams), second slice ("turn feedback loop").

## Purpose

Once a workspace is past setup (slice A), the owner can chat with the drafting assistant — but the **ask → see-the-result loop is broken**:

1. **The artifact pane never refreshes.** The SSE bridge ([workspaces.py:273](../../../src/call_manager/proposal_assist/web/workspaces.py#L273)) streams only chat rows; the proposal pane (`#artifact-rendered`) is untouched when a turn edits `proposal.md`, so "ask the assistant to revise, see the revision" needs a manual full-page reload no one mentions.
2. **Failures are silent.** A failed turn writes only `turn_jobs.status='failed'` + a `cloud_turn_failed` `WorkspaceEvent` ([processor.py:429](../../../src/call_manager/proposal_assist/turns/processor.py#L429)) — no chat row — so a 600 s timeout looks identical to "the assistant ignored me." The error text lives only on the admin panel. Worse, **two other failure paths don't even write the audit event**: the stuck-turn reaper ([reaper.py:78](../../../src/call_manager/proposal_assist/turns/reaper.py#L78)) and the crash/redeploy orphan sweep ([orphan_sweep.py:25](../../../src/call_manager/proposal_assist/drafting/orphan_sweep.py#L25)) do bulk `UPDATE … status='failed'` with no event and no row.
3. **Error responses eat the typed message.** Most `post_turn` error branches return `HTMLResponse(4xx/5xx)` with an HTML body htmx won't swap (the composer targets `#chat-history` with `hx-swap='beforeend'`, which ignores non-2xx); and `hx-on::after-request="this.reset()"` clears the textarea even on failure — so a rejected turn vanishes with no message and no recoverable text.

This slice closes the loop: refresh the artifact when a turn ends, make every terminal failure/cancel a **visible, viewer-safe** chat row, surface a **working… + Cancel** affordance while a turn is in flight, and make error responses **readable** without eating the typed message — fixing two latent SSE bugs in the process.

## Goals

- When a turn ends, the proposal pane re-renders without a full-page reload.
- Every terminal **failed**/**cancelled** outcome — from any of the four writers — produces a persistent, categorized, **viewer-safe** chat row (raw diagnostics stay owner/admin-only) and a consistent audit event.
- While a turn is in flight: a "working…" indicator and a **Cancel** button (cancel works only before processing starts).
- `post_turn` errors render inline (JSON `detail`) and never clear the composer on failure.
- No regression to the streaming of assistant rows.

## Background: the SSE bridge as it works today (and two bugs)

`post_turn` returns the user-turn HTML plus an **SSE connector** div ([workspaces.py:1006-1015](../../../src/call_manager/proposal_assist/web/workspaces.py#L1006-L1015)):

```html
<div id='sse-mount-{job}' hx-ext='sse' sse-connect='…/stream' sse-swap='message' sse-close='done' …>
```

`_stream_turn_generator` ([workspaces.py:273-343](../../../src/call_manager/proposal_assist/web/workspaces.py#L273-L343)) polls `chat_turns` rows `position > last_seen`, yields each as an SSE `message` event (appended to `#chat-history`), and emits `event: done` once the `TurnJob` reaches a `TERMINAL_STATUSES` value.

**Bug 1 — `sse-close="done"` is a no-op.** The vendored `htmx-sse.js` reads `sse-swap` but **never reads `sse-close`** ([htmx-sse.js](../../../src/call_manager/web/static/htmx-sse.js) — only closes the `EventSource` when the connector leaves the DOM, line ~280). So the connector does **not** self-remove on `done`; the `EventSource` stays open and the browser reconnects, and the `htmx:beforeCleanupElement` hook in [app.js:73-78](../../../src/call_manager/web/static/app.js#L73-L78) (which re-enables section editing) **never reliably fires**. Any feature keyed on "the connector was cleaned up" is currently broken.

**Bug 2 — terminal-vs-rows ordering race.** Each poll fetches new rows *then* checks terminal status, in two separate Postgres snapshots within the same loop body ([workspaces.py:316-341](../../../src/call_manager/proposal_assist/web/workspaces.py#L316-L341)). If a finalize commit lands **between** those two reads, the generator sees `terminal` and emits `done` **without ever yielding the just-written outcome row** — so the new failure row would be lost from the live stream (it would only appear on a later page reload).

This slice fixes both as prerequisites for items 1 and 2.

## Components

### A. Terminal-outcome helper (the centralization) — `turns/outcomes.py` (new)

A single writer for terminal **failed**/**cancelled** outcomes so status, the visible row, and the audit event stay consistent across all four call sites.

```python
def record_terminal_outcome(
    session,                      # caller's open Session (caller commits)
    *,
    turn_job_id: uuid.UUID,
    workspace_id: uuid.UUID,
    outcome: str,                 # 'failed' | 'cancelled'
    expected_status: str,         # 'running' | 'queued' — the conditional guard
    raw_error: str | None,        # stored in the audit event, NEVER in the chat row
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> bool:
    ...
```

Behaviour, all in the caller's transaction:

1. **Conditional status flip** — `UPDATE turn_jobs SET status=<terminal>, error=raw_error, completed_at=now WHERE id=… AND status=expected_status` (terminal = `failed` for `outcome='failed'`, `cancelled` for `'cancelled'`). If `rowcount == 0` (already terminal / reaped — avoid double-finalize), **return `False`** and do nothing else. This preserves the existing no-double-finalize guard ([processor.py:456-469](../../../src/call_manager/proposal_assist/turns/processor.py#L456-L469)).
2. Read the job's `user_turn_id` + `submitted_by_user_id`. Clear the user-turn's `tokens_in` pre-count (mirrors the current finalize/reaper behaviour so a failed turn doesn't burn cap budget — [processor.py:446-449](../../../src/call_manager/proposal_assist/turns/processor.py#L446-L449), [reaper.py:87-90](../../../src/call_manager/proposal_assist/turns/reaper.py#L87-L90)).
3. **Write the visible row** — `ChatTurn(workspace_id, role='system', content={"type":"turn_outcome","outcome":outcome,"text": <safe_text>})`. `position` comes from the existing `chat_turns_position_seq`. `safe_text` is produced by `outcome_message(outcome, raw_error)` (component B) — **never the raw error**.
4. **Write the audit event** — `WorkspaceEvent(kind='cloud_turn_failed' | 'cloud_turn_cancelled', actor_id=submitted_by_user_id, payload={turn_job_id, tokens_in, tokens_out, error: raw_error})`. The raw diagnostics live here (owner/admin-only).
5. Return `True`.

Ordering note for the SSE race (bug 2): because the row and the status flip commit together in step 1+3, a generator that reads `terminal` is guaranteed to also see the row on its subsequent row-fetch (see component F).

### B. Safe outcome message — `outcome_message(outcome, raw_error) -> str` (pure, in `turns/outcomes.py`)

Maps the outcome + the raw error to a short, viewer-safe sentence. **Never** echoes `raw_error`. Categorization (Codex: do not call every cancellation a timeout):

| Condition | Message |
|---|---|
| `outcome == 'cancelled'` | "You cancelled this turn before it started." |
| `raw_error` starts with `"reaped:"` | "This turn was stopped after running too long." |
| `raw_error == "orphaned"` | "This turn was interrupted (the service restarted). Please try again." |
| `outcome == 'failed'`, any other `raw_error` | "The drafting assistant couldn't finish this turn. Please try again." |

(We deliberately do **not** emit a "timed out" label: the in-loop `turn_max_seconds` bound surfaces as a generic `CancelledError`-derived `processor_failure:`/`sdk_turn_error:` string — [drain.py:105-118](../../../src/call_manager/proposal_assist/turns/drain.py#L105-L118), [processor.py:288-301](../../../src/call_manager/proposal_assist/turns/processor.py#L288-L301) — so "couldn't finish… please try again" is the honest message. The reaper's `reaped:` prefix is the one case we *can* attribute to over-runtime.) Pure function → unit-testable without a DB.

### C. Migration — `'system'` role

Alembic migration extends `ck_chat_turns_role` from `('user','assistant','tool')` to `('user','assistant','tool','system')` ([models.py:778-780](../../../src/call_manager/db/models.py#L778-L780)) and updates the model's `CheckConstraint`. No data backfill. `'system'` rows are produced only by `record_terminal_outcome`.

### D. Render the outcome row — `chat_turn.html`

Add a dispatch branch for the new content shape `{"type":"turn_outcome", ...}` ([chat_turn.html](../../../src/call_manager/web/templates/proposal_assist/partials/chat_turn.html)): render a muted system notice (e.g. `<p class="turn-outcome">{{ c.text }}</p>`) rather than falling through to the `<pre>` default. Autoescape stays on (no `| safe`). The existing `data-role="{{ turn.role }}"` already carries `system` for styling. One small CSS rule for `.turn-outcome` (reuse `--cm-muted`/callout tokens; prefer no new tokens).

### E. The four writers route through the helper

- **Processor** ([processor.py:429 `_finalize_failed`](../../../src/call_manager/proposal_assist/turns/processor.py#L429)) — its **body** becomes a call to `record_terminal_outcome(outcome='failed', expected_status='running', raw_error=error, …)` inside one `session_factory()` transaction; it stays as a thin wrapper with its current signature so its callers ([processor.py:170,193,288,300](../../../src/call_manager/proposal_assist/turns/processor.py#L170)) are unchanged (same `error=` strings). The helper re-reads `user_turn_id`/`submitted_by_user_id` from the `TurnJob` row, so the wrapper need not thread them.
- **Cancel** ([cancel_turn:1376](../../../src/call_manager/proposal_assist/web/workspaces.py#L1376)) — replaced by `record_terminal_outcome(outcome='cancelled', expected_status='queued', raw_error=None, …)`; the route still returns its small status fragment (component H) for the Cancel button's own target.
- **Reaper** ([reaper.py:78](../../../src/call_manager/proposal_assist/turns/reaper.py#L78)) — the bulk `UPDATE` becomes a select-then-loop: for each stuck job, `record_terminal_outcome(outcome='failed', expected_status='running', raw_error="reaped: …")`. Stuck jobs are normally zero, so per-job cost is irrelevant.
- **Orphan sweep** ([orphan_sweep.py:19](../../../src/call_manager/proposal_assist/drafting/orphan_sweep.py#L19)) — same select-then-loop with `raw_error="orphaned"`. (Caller still commits, per its docstring.)

After this, **every** terminal failed/cancelled outcome writes a row + an audit event, and the two previously-eventless paths (reaper, orphan) gain `cloud_turn_failed` events too.

### F. SSE generator: fix the ordering race + emit `done` reliably — `_stream_turn_generator`

Reorder each poll body ([workspaces.py:312-343](../../../src/call_manager/proposal_assist/web/workspaces.py#L312-L343)) to: **(1)** read the `TurnJob` terminal status; **(2)** fetch + yield all rows `position > last_seen` (this now includes any outcome row, since it committed with the status); **(3)** if step 1 saw terminal, emit `event: done` and return. Because the outcome row and the status flip commit atomically (component A), a terminal read in step 1 guarantees the row is visible to the step-2 fetch. The `done` payload stays the terminal status string.

### G. Connector redesign — fix `sse-close` (bug 1) + drive artifact refresh

`post_turn` builds the connector with an explicit **`done` child** instead of the no-op `sse-close` attribute:

```html
<div id="sse-mount-{job}" hx-ext="sse" sse-connect="/workspaces/{ws}/turns/{job}/stream">
  <div sse-swap="message" hx-target="#chat-history" hx-swap="beforeend"></div>
  <div sse-swap="done" hx-target="#sse-mount-{job}" hx-swap="delete"></div>
  <div class="turn-progress" aria-busy="true">
    Working…
    <button type="submit"
            hx-post="/workspaces/{ws}/turns/{job}/cancel"
            hx-target="#cancel-status-{job}" hx-swap="innerHTML">Cancel</button>
    <span id="cancel-status-{job}"></span>
  </div>
</div>
```

- The `sse-swap="message"` child appends streamed chat rows to `#chat-history` (today's behaviour, now on a dedicated child rather than the connect element).
- The `sse-swap="done"` child, on the `done` event, **deletes the connector** (`hx-swap="delete"` on `hx-target=#sse-mount-{job}`). Removing the connector from the DOM makes `htmx-sse.js` close the `EventSource` (no reconnect/duplicate-row loop) and fires `htmx:beforeCleanupElement` — the reliable "turn finished" signal that drives re-enabling **and** the artifact refresh (component I).
- The **Cancel** button has its **own** `hx-target` (`#cancel-status-{job}`, inside the connector) so the cancel response never lands in the transcript. It POSTs to the existing cancel route.

### H. Cancel route returns a small status fragment

`cancel_turn` returns a tiny HTML fragment for `#cancel-status-{job}`: on a real cancel, "Cancelled." ; on a no-op (already running/terminal), "Too late — the assistant already started." The route keeps its existing race-safe `WHERE status='queued'` guard and now routes the state change through `record_terminal_outcome` (so a cancel also produces the system row). The button copy already tells the user cancel only works before processing starts.

### I. Artifact pane refresh — new GET route + app.js hook

- **Route** `GET /workspaces/{id}/artifact` ([workspaces.py](../../../src/call_manager/proposal_assist/web/workspaces.py)) returns **only** `partials/artifact_sections.html` (the inner sections), with `is_owner`/`annotations_enabled` populated as `get_workspace_main` does — *not* `artifact_pane.html` (which contains the `#artifact-rendered` wrapper and would nest a duplicate). Owner-or-viewer access; reuses the same `split_sections` + bleach-clean render path as `get_workspace_main` ([workspaces.py:593-602](../../../src/call_manager/proposal_assist/web/workspaces.py#L593-L602)).
- **app.js hook** — extend the existing `htmx:beforeCleanupElement` handler ([app.js:73-78](../../../src/call_manager/web/static/app.js#L73-L78)): when an `sse-mount-*` element is cleaned up (now reliable, per bug-1 fix), in addition to `reenable()`, refetch the artifact: `htmx.ajax('GET', '/workspaces/'+wsId+'/artifact', {target:'#artifact-rendered', swap:'innerHTML'})`. `wsId` is read from a `data-workspace-id` attribute added to a stable page element (e.g. the workspace layout container). Fires once per turn-end; a no-op-looking refetch after a failed turn (proposal unchanged) is harmless.

### J. `post_turn` error branches → JSON `detail` + composer reset gate

- Convert **every** user-facing error branch in `post_turn` to `HTTPException(status_code=…, detail="…")` so app.js's global `htmx:responseError` handler ([app.js:88-104](../../../src/call_manager/web/static/app.js#L88-L104)) renders the `detail` inline. Branches: kill-switch 503 ([:760](../../../src/call_manager/proposal_assist/web/workspaces.py#L760)), disclosure 403 ([:780](../../../src/call_manager/proposal_assist/web/workspaces.py#L780)), cap 429, and the upload-validation / storage 400/500 branches around [:824-1004](../../../src/call_manager/proposal_assist/web/workspaces.py#L824). The setup-guard 409 (slice A) is already in this form — it's the template.
- Composer ([workspace_main.html](../../../src/call_manager/web/templates/proposal_assist/partials)) — change `hx-on::after-request="this.reset()"` to `hx-on::after-request="if (event.detail.successful) this.reset()"` so the typed text survives a rejected turn.

## Data flow

**Success:** owner sends → `post_turn` returns user row + connector (working…/Cancel) → assistant rows stream into `#chat-history` → turn completes → generator yields `done` → `done` child deletes the connector → `EventSource` closes, cleanup fires → `reenable()` + artifact refetch repaints `#artifact-rendered` with the revised proposal.

**Failure (any of the four writers):** `record_terminal_outcome` writes `status=failed` + a `system` `turn_outcome` row + the audit event atomically → generator's next poll reads terminal, fetches the new row (yields it into chat), emits `done` → connector deleted → artifact refetch (no-op) + reenable. The user sees a categorized "couldn't finish… try again" row instead of silence.

**Cancel (while queued):** owner clicks Cancel → cancel route flips `queued→cancelled` via the helper (writes the `turn_outcome` row + event) and returns "Cancelled." into `#cancel-status-{job}` → generator sees terminal → `done` → connector removed.

**Rejected submit:** `post_turn` raises `HTTPException(detail=…)` → app.js shows the detail inline; the composer keeps the typed text (reset gated on `successful`).

## Error handling / edge cases

- **Double-finalize / reaped-then-finalized:** the helper's `WHERE status=expected_status` guard returns `False` and writes nothing — no duplicate row or event (preserves [processor.py:456-469](../../../src/call_manager/proposal_assist/turns/processor.py#L456-L469)).
- **Cancel after processing starts:** `WHERE status='queued'` matches nothing → helper returns `False`, route returns the "too late" fragment; no row written (the eventual real outcome — success or failure — writes its own row).
- **Viewer:** sees the streamed assistant rows and the viewer-safe `turn_outcome` row (no raw diagnostics, by construction). Viewers may already watch the stream ([stream_turn:1030](../../../src/call_manager/proposal_assist/web/workspaces.py#L1030)).
- **Connection dropped mid-turn:** unchanged — the generator's `request.is_disconnected()` check still exits the loop; on reload the persisted rows (including any outcome row) render via the history loop.
- **Migration rollback safety:** removing `'system'` later would require no `system` rows to exist; out of scope (forward-only).

## Non-goals (later Theme-7 slices / explicitly deferred)

DOCX export (slice C); resolved call-title/profile pickers, colored status badge, link-back, deadline-in-header, version-history restore (slice D); tool-chatter collapse; a richer per-token streaming indicator; surfacing the raw error to the owner via a separate owner-only channel (the audit event + admin panel remain the diagnostics path); distinguishing "timed out" from other failures in the user message.

## Testing

Following `tests/proposal_assist/*` patterns (TestClient + `pg_session_factory`; the SSE generator is unit-driven directly per its docstring):

- **`outcome_message` unit tests:** cancelled / `reaped:` / `orphaned` / generic-failed each map to the right viewer-safe sentence; raw error never appears in the output.
- **`record_terminal_outcome`:** writes a `system` `turn_outcome` `ChatTurn` + the audit event + flips status, atomically; `expected_status` mismatch (already terminal) → returns `False`, writes nothing; clears the user-turn `tokens_in`.
- **All four writers:** processor failure, reaper, orphan sweep, and queued-cancel each produce a `system` row + the corresponding `cloud_turn_failed`/`cloud_turn_cancelled` event.
- **SSE ordering (bug 2):** drive `_stream_turn_generator` with a finalize that writes the outcome row + terminal status, and assert the `done`-terminating poll **also** yields the outcome row (a regression test for the race — write row+status, then assert the row is streamed before/with `done`).
- **Migration:** a `ChatTurn(role='system')` inserts cleanly; the constraint still rejects an unknown role.
- **`chat_turn.html`:** a `turn_outcome` content row renders the `.turn-outcome` notice (not the `<pre>` fallback) and is autoescaped.
- **Artifact route:** `GET /{id}/artifact` returns the sections partial (no nested `#artifact-rendered`), owner-or-viewer, 404 missing.
- **Connector markup:** `post_turn`'s response contains the `sse-swap="done"` delete-child and the Cancel button targeting `#cancel-status-{job}` (not `#chat-history`); the legacy `sse-close` attribute is gone.
- **post_turn errors (item 3):** kill-switch/disclosure/cap/upload-validation each return their status with a JSON `detail` body (not HTML); a static-content assertion that the composer markup gates the reset on `event.detail.successful`.
- **Cancel route:** queued → "Cancelled." fragment + a `cancelled` row; running → "too late" fragment + no row.

## Files

- Create: `src/call_manager/proposal_assist/turns/outcomes.py` (`record_terminal_outcome`, `outcome_message`).
- Create: Alembic migration (extend `ck_chat_turns_role` with `'system'`).
- Modify: `src/call_manager/db/models.py` (the `ChatTurn` `CheckConstraint`).
- Modify: `src/call_manager/proposal_assist/turns/processor.py` (`_finalize_failed` → helper), `turns/reaper.py`, `drafting/orphan_sweep.py` (bulk → per-job helper calls).
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` (`_stream_turn_generator` ordering; connector markup in `post_turn`; `cancel_turn` fragment + helper; new `GET /{id}/artifact`; error branches → `HTTPException`).
- Modify: `src/call_manager/web/templates/proposal_assist/partials/chat_turn.html` (`turn_outcome` branch); `workspace_main.html` (composer reset gate; `data-workspace-id`); `src/call_manager/web/static/app.js` (artifact refetch on sse-mount cleanup); `src/call_manager/web/static/app.css` (`.turn-outcome` rule).
- Add tests: `tests/proposal_assist/test_turn_outcomes.py` (+ extend `test_web_workspaces_sse.py`, `test_web_workspaces_turns.py`, `test_web_workspaces_cancel.py`, reaper/orphan tests).
