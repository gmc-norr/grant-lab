# Proposal-Assist — Live Turn Monitor — Design Spec

**Date:** 2026-06-13
**Status:** Draft — the **monitor-start protocol needs a design pass before planning** (see *Open design issues*); depends on **B1** ([2026-06-12-proposal-assist-turn-outcomes-design.md](2026-06-12-proposal-assist-turn-outcomes-design.md))
**Audience:** Engineers working on call-manager
**Theme:** Product review 2026-06-11, Theme 7 (proposal-assist workspace seams), slice **B2** (live turn monitor). B1 handles terminal-outcome recording + error hygiene; this slice handles the live feedback loop.

## Purpose

After B1, every terminal outcome is recorded, categorized, viewer-safe, audited, and **visible on render**. But the live loop is still broken:

1. **The artifact pane never refreshes.** The SSE bridge streams only chat rows; the proposal pane (`#artifact-rendered`) is untouched when a turn edits `proposal.md`, so "ask the assistant to revise, see the revision" needs a manual full-page reload.
2. **No working/Cancel affordance.** During a turn there is no in-flight indicator and no way to cancel a queued turn from the UI.
3. **Mid-turn reload loses the live stream entirely.** Connectors are emitted only by `post_turn`; `get_workspace_main` never queries `TurnJob`, so reloading while a turn runs leaves no stream, no indicator, no Cancel, and no eventual artifact refresh — silently breaking the exact "ask → see the result" loop this theme exists to close.

Two latent SSE bugs underlie all three (below). This slice closes the live loop by **rebuilding the connector as a single workspace monitor**, the principled model the workspace-global stream actually demands.

## The core realization: the stream is workspace-global, so the connector must be a single workspace monitor

`_stream_turn_generator` ([workspaces.py:316-324](../../../src/call_manager/proposal_assist/web/workspaces.py#L316)) fetches `ChatTurn WHERE workspace_id == … AND position > last_seen` — **not** filtered by `turn_job_id`. Only the *seed position* (from the job's user turn) and the *terminal check* (`tj.status`) are job-scoped. Two consequences:

- **Per-turn connectors double-stream.** The composer is **not** disabled during a turn ([app.js:64-66](../../../src/call_manager/web/static/app.js#L64) disables only `.section-edit-btn`), so a user can submit turn B while A runs. Each connector is workspace-global after its own seed, so rows after B's seed stream through **both** connectors → duplicate chat rows. This is a latent bug today.
- **Per-job reconnection on reload would compound it.** Rendering one connector per active job on reload would double-stream whenever ≥2 turns are queued.

The principled model: **one monitor per browser tab**, the sole source of streamed chat rows, seeded at a chat position, that terminates when the workspace queue drains. This subsumes the per-turn connector, fixes the latent duplicate, and makes reload a non-special case.

## Two latent bugs (fixed as part of the rebuild)

**Bug 1 — `sse-close="done"` is a no-op.** The vendored `htmx-sse.js` reads `sse-swap` but **never reads `sse-close`** (it only closes the `EventSource` when the connector leaves the DOM, ~line 280). So the connector doesn't self-remove on `done`; the `EventSource` stays open and the browser reconnects, and the `htmx:beforeCleanupElement` hook ([app.js:73-78](../../../src/call_manager/web/static/app.js#L73)) never reliably fires. The monitor instead uses an explicit `sse-swap="done"` **delete-child** so removal (and cleanup) is real.

**Bug 2 — terminal-vs-rows ordering race.** Each poll fetches new rows *then* checks terminal status, in two separate snapshots ([workspaces.py:316-341](../../../src/call_manager/proposal_assist/web/workspaces.py#L316)). If a finalize commits **between** those reads, the generator sees terminal and emits `done` **without yielding the just-written outcome row** — so a fresh failure row is dropped from the live stream (it only appears on a later reload). Fixed by reordering: read terminal → fetch+yield rows → emit `done`.

## The central decision: the echo model — single source of truth

A persistent monitor streams **all** workspace rows after its seed — including the user's own newly-submitted turn. Today's per-turn connector avoids re-echoing the user's message only because it seeds *at* that message's position; a monitor that persists across turns sits *before* later user turns and would re-stream them, duplicating the optimistic echo `post_turn` renders.

**Resolution (chosen): the monitor is the sole renderer of chat rows; drop the optimistic echo.** `post_turn` no longer returns the user-row HTML. The monitor renders the user row + assistant rows. This is correct by construction (one writer of DOM rows → no dedup, no double-render) and the latency cost is negligible: the generator fetches **immediately on connect** (it polls before its first `sleep`), so the user's own row appears within the SSE connect + first-fetch window (tens of ms), not `poll_seconds`. The monitor already renders via the same `_CHAT_TURN_PARTIAL` + reference-map path the echo used, so chips/attachments render identically. If SSE never establishes, the row still appears on reload via B1's history loop — consistent degradation.

**Rejected alternative — keep the echo, dedupe the stream by stable DOM id.** Stamp each streamed row with `id="chat-turn-{uuid}"` and drop it client-side if already present. More surface (id stamping + a dedup hook), more failure modes, and no real latency win given the immediate first poll. Single-source-of-truth wins on principle and on simplicity.

## Open design issues (resolve in a follow-up design pass before B2 is planned)

Codex review (2026-06-13) found the **monitor-start protocol** below to be insufficient. These do **not** affect B1 and do **not** change the *direction* (one workspace monitor, single source of truth), but the **server-decided empty→non-empty emit** mechanism as specced cannot deliver the per-tab guarantee. They are the first agenda items of B2's own brainstorm.

1. **Per-tab start is undecidable from a global queue check.** "Emit the monitor only if the queue was empty" is evaluated against the *workspace*, not the *tab*. If tab A has an active turn and a previously-idle tab B submits, B's submit sees a non-empty queue → returns nothing → **tab B has no monitor and (echo removed) no visible feedback at all**, and the already-rendered monitor in *any* tab never gains a Cancel control for newly stacked jobs (its markup was emitted once). A correct protocol needs **per-connection/per-tab identity** — e.g. the client owns a monitor with a client-generated id and always (re)connects on submit; the server streams to whoever connects — rather than a global emit decision.
2. **Terminate-vs-submit TOCTOU.** The generator can read "no active jobs," then a new submit commits a user row + job; the generator yields that row and emits `done`, deleting itself **while the new job is active** — a dropped-monitor / duplicate-row window that contradicts the single-source guarantee. The terminal check must be coordinated with submit (a generation/epoch token, "don't terminate in a poll that yielded rows," or submit re-arming the monitor) — to be designed.

The rest of B2 (generator ordering fix, `sse-close` fix, `artifact_body` refresh, reconnect-on-render scaffolding) is unaffected by these.

## Components

### F. Generator → monitor mode + ordering fix — `_stream_turn_generator`

- **Seed** at an explicit `since_position` (caller-supplied) rather than a job's user-turn position.
- **Poll body reorder** (fixes bug 2): **(1)** read whether the workspace still has an active job — `SELECT EXISTS(SELECT 1 FROM turn_jobs WHERE workspace_id=… AND status IN ('queued','running'))`; **(2)** fetch + yield all rows `position > last_seen` (now guaranteed to include any outcome row that committed with the status flip — B1's helper writes row+status atomically); **(3)** if step 1 saw **no** active job, emit `event: done` and return.
- **Terminal condition** changes from "this `turn_job_id` is terminal" to "no `queued`/`running` job remains for the workspace" — so one monitor correctly covers a multi-turn queue and stops only when the queue is fully drained.
- **Route:** `GET /workspaces/{id}/turns/monitor?since={position}` (owner-or-viewer; validates workspace access). The legacy `/turns/{job}/stream` route can delegate to the same generator with `since_position` = that job's user-turn position, or be retired once no caller remains — decided in the plan.

### G. The singleton workspace-monitor connector

One connector per tab, fixed id `#ws-turn-monitor`:

```html
<div id="ws-turn-monitor" hx-ext="sse" sse-connect="/workspaces/{ws}/turns/monitor?since={pos}">
  <div sse-swap="message" hx-target="#chat-history" hx-swap="beforeend"></div>
  <div sse-swap="done" hx-target="#ws-turn-monitor" hx-swap="delete"></div>
  <div class="turn-progress" aria-busy="true">
    Working…
    {% for job in cancellable_jobs %}
      <button type="submit"
              hx-post="/workspaces/{ws}/turns/{job.id}/cancel"
              hx-target="#cancel-status-{job.id}" hx-swap="innerHTML">Cancel</button>
      <span id="cancel-status-{job.id}"></span>
    {% endfor %}
  </div>
</div>
```

- `sse-swap="message"` → appends streamed chat rows to `#chat-history`.
- `sse-swap="done"` → **deletes** the monitor (`hx-swap="delete"`), which makes `htmx-sse.js` close the `EventSource` (no reconnect loop) and fires `htmx:beforeCleanupElement` — the reliable "queue drained" signal that drives the artifact refresh (component I).
- A **Cancel** button per *cancellable* (queued) job, each with its own `#cancel-status-{job}` target so a cancel response never lands in the transcript.

### Server-decided single emit — `post_turn`

After enqueue, `post_turn` returns the monitor **only on the empty→non-empty transition**:

- **Queue was empty** before this turn (no other `queued`/`running` job) → return the monitor connector seeded at the prior max chat position. **No user-row echo** (the monitor streams it).
- **A turn was already active** → return an empty `200` (nothing visible). The existing monitor, being workspace-global, streams the new turn's rows.

The "any other active job?" check is one cheap query. This keeps exactly one monitor per tab without the server needing to know client DOM state, and it preserves queuing (the composer stays enabled).

### `get_workspace_main` → reconnect on render

- Query active (`queued`/`running`) jobs for the workspace. If any exist: render the `#ws-turn-monitor` connector seeded at the **last rendered chat position** (the max `position` the history loop emitted), the working indicator, and a Cancel button per queued job.
- Render section-edit buttons **disabled** (an active job exists).
- If none active: no connector, editing enabled — today's behaviour.

### Section-edit gating (subsumes B-era "finding 7")

Section editing is disabled whenever any active job exists — set on submit and on reload (above), cleared when the monitor is removed (queue drained). Because there is now **structurally one monitor**, exactly one cleanup event fires → exactly one re-enable. The earlier per-connector `reenable`-gating problem (multiple connectors each re-enabling early) cannot occur and needs no guard.

### I. Artifact-pane refresh — `artifact_body.html` + GET route + app.js hook

- **New partial `artifact_body.html`** — extract the sections-or-placeholder conditional now inline in `artifact_pane.html` ([:17-23](../../../src/call_manager/web/templates/proposal_assist/partials/artifact_pane.html#L17)) — the `{% if artifact_sections %} … {% else %}` "(no proposal.md yet)" placeholder — into a standalone partial that `artifact_pane.html` `{% include %}`s. This shares the empty-state placeholder between the page-load render and the refresh route; without it, a **failed first turn** (no `proposal.md` yet → empty `artifact_sections`) would refresh `#artifact-rendered` to blank.
- **Route** `GET /workspaces/{id}/artifact` returns **only** `partials/artifact_body.html` (sections-**or-placeholder**), with `artifact_sections`/`artifact_rendered`/`is_owner`/`annotations_enabled` populated as `get_workspace_main` does ([workspaces.py:582-603](../../../src/call_manager/proposal_assist/web/workspaces.py#L582)) — *not* `artifact_pane.html` (which would nest a duplicate `#artifact-rendered`). Owner-or-viewer.
- **app.js hook** — extend `htmx:beforeCleanupElement` ([app.js:73-78](../../../src/call_manager/web/static/app.js#L73)): when `#ws-turn-monitor` is cleaned up, `htmx.ajax('GET', '/workspaces/'+wsId+'/artifact', {target:'#artifact-rendered', swap:'innerHTML'})` (idempotent; a no-op-looking refetch after an unchanged/failed turn is harmless) **and** re-enable section editing. `wsId` from a `data-workspace-id` attribute on a stable page element.

### H. Cancel route returns a small status fragment — `cancel_turn`

`cancel_turn` (already routed through B1's `record_terminal_outcome`, so the state change + system row + event are handled) now returns a tiny fragment for `#cancel-status-{job}`: on a real cancel, "Cancelled." ; on a no-op (already running/terminal), "Too late — the assistant already started." The route keeps its race-safe `WHERE status='queued'` guard (the helper's conditional update).

## Data flow

**Success (queue was empty):** owner sends → `post_turn` enqueues, returns the monitor (seeded at prior max position), **no echo** → monitor streams the user row, then assistant rows, into `#chat-history` → turn completes → next poll sees no active job → emits `done` → the `done` child deletes the monitor → `EventSource` closes, cleanup fires → artifact refetch repaints `#artifact-rendered`, section editing re-enables.

**Stacked submit:** a second turn while one runs → `post_turn` returns empty `200` → the existing monitor streams the new turn's rows → it stays until **both** drain. Cancel shows for the still-queued turn.

**Mid-turn reload:** `get_workspace_main` sees an active job → renders the monitor seeded at the last rendered position + Cancel(s) + disabled editing → streaming resumes with no page reload, terminating when the queue drains.

**Failure (B1 writes the outcome row):** generator's next poll reads no active job, fetches the new `turn_outcome` row (yields it), emits `done` → monitor deleted → artifact refetch (no-op) + reenable. The categorized failure row is now visible **live**, not just on reload.

**Cancel (while queued):** owner clicks a job's Cancel → route flips `queued→cancelled` via B1's helper + returns "Cancelled." into `#cancel-status-{job}` → next poll: if that was the last active job, `done`.

## Error handling / edge cases

- **Monitor-start races (per-tab emit; terminate-vs-submit):** the empty→non-empty emit rule does **not** safely deliver one-monitor-per-tab, and the terminal check can race a new submit — see **Open design issues**. Resolution is deferred to B2's design pass; do not plan B2's `post_turn`/`get_workspace_main` emit logic until it's resolved.
- **Two tabs on one workspace:** each tab renders its own monitor; within a tab there is exactly one. Cross-tab streams are independent (no shared DOM), so no duplication.
- **Connection dropped mid-turn:** the generator's `request.is_disconnected()` check still exits the loop; on reload the monitor reconnects (reconnect-on-render).
- **Viewer:** may watch the monitor (owner-or-viewer on the stream route); sees streamed assistant rows + B1's viewer-safe `turn_outcome` rows; no Cancel (not owner).
- **`done` payload:** carries a terminal marker (e.g. `"idle"`); the delete-child triggers on the event name, not the payload.

## Non-goals (later Theme-7 slices)

DOCX export (slice C); resolved call-title/profile pickers, colored status badge, link-back, deadline-in-header, version-history restore (slice D); a richer per-token streaming indicator; tool-chatter collapse.

## Testing

Following `tests/proposal_assist/*` patterns (TestClient + `pg_session_factory`; the generator is unit-driven directly):

- **Generator monitor mode:** with two queued turns, `done` is emitted only after **both** reach terminal (not after the first); `since_position` seeding does not re-yield rows at/below the seed.
- **SSE ordering (bug 2):** under the new **terminal-read-first** order there is no longer a "between the two reads" injection point, so test the **durable property** instead: with a finalize that commits the outcome row + terminal status atomically (B1's helper), assert the generator **always yields the outcome row before `done`** — never after it — across several finalize timings (before the poll; immediately after a non-terminal poll's reads; concurrent with a poll). The row may arrive on the same or the next poll; the invariant under test is "no row is lost behind `done`."
- **Single-emit decision:** `post_turn` from an idle workspace returns a `#ws-turn-monitor` connector and **no** user-row echo; a stacked `post_turn` (active job present) returns an empty body (no second connector).
- **Reconnect on render:** `get_workspace_main` with an active job renders the monitor seeded at the last rendered position + Cancel per queued job + disabled section-edit buttons; with no active job, no connector and editing enabled.
- **No double-echo:** across a stacked submit, each chat row appears in `#chat-history` exactly once.
- **Artifact route:** `GET /{id}/artifact` returns `artifact_body.html` (no nested `#artifact-rendered`), owner-or-viewer; the **empty-artifact case** (no `proposal.md`) returns the "(no proposal.md yet)" placeholder, **not** a blank body; 404 on a missing workspace.
- **Connector markup:** the monitor contains the `sse-swap="done"` delete-child and Cancel button(s) targeting `#cancel-status-{job}` (not `#chat-history`); the legacy `sse-close` attribute is gone.
- **Cancel route:** queued → "Cancelled." fragment (+ B1's `cancelled` row/event); running → "too late" fragment + no state change.

## Files

- Create: `src/call_manager/web/templates/proposal_assist/partials/artifact_body.html` (sections-or-placeholder body, extracted from `artifact_pane.html`).
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` (`_stream_turn_generator` → monitor mode + ordering fix; new `monitor` route; `post_turn` single-emit + drop echo; `get_workspace_main` reconnect query + context; `cancel_turn` fragment; new `GET /{id}/artifact`).
- Modify: `src/call_manager/web/templates/proposal_assist/partials/artifact_pane.html` (include `artifact_body.html`); `workspace_main.html` (monitor render on reload, `data-workspace-id`, Cancel/working markup, section-edit disabled state); `src/call_manager/web/static/app.js` (artifact refetch + reenable on monitor cleanup); `src/call_manager/web/static/app.css` (`.turn-progress` / Cancel styling).
- Add tests: extend `test_web_workspaces_sse.py` (monitor mode, ordering race, no-double-echo), `test_web_workspaces_turns.py` (single-emit, reconnect-on-render), `test_web_workspaces_cancel.py` (fragment), + an artifact-route test.
