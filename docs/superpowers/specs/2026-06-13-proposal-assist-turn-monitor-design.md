# Proposal-Assist — Live Turn Monitor — Design Spec

**Date:** 2026-06-13
**Status:** Design pass complete — model chosen (**A2: per-job connectors + stable-id client dedup**); ready to plan. Depends on **B1** ([2026-06-12-proposal-assist-turn-outcomes-design.md](2026-06-12-proposal-assist-turn-outcomes-design.md)).
**Audience:** Engineers working on call-manager
**Theme:** Product review 2026-06-11, Theme 7 (proposal-assist workspace seams), slice **B2** (live turn monitor). B1 handles terminal-outcome recording + error hygiene; this slice handles the live feedback loop.

## Purpose

After B1, every terminal outcome is recorded, categorized, viewer-safe, audited, and **visible on render**. But the live loop is still broken:

1. **The artifact pane never refreshes.** The SSE bridge streams only chat rows; the proposal pane (`#artifact-rendered`) is untouched when a turn edits `proposal.md`, so "ask the assistant to revise, see the revision" needs a manual full-page reload.
2. **No working/Cancel affordance.** During a turn there is no in-flight indicator and no way to cancel a queued turn from the UI.
3. **Mid-turn reload loses the live stream entirely.** Connectors are emitted only by `post_turn`; `get_workspace_main` never queries `TurnJob`, so reloading while a turn runs leaves no stream, no indicator, no Cancel, and no eventual artifact refresh — silently breaking the exact "ask → see the result" loop this theme exists to close.

Two latent SSE bugs underlie all three (below). This slice closes the live loop by **evolving the existing per-turn connector** into a robust per-job connector that survives stacking, reload, and reconnect.

## The model: keep per-job connectors; make them duplicate-safe (A2)

`_stream_turn_generator` ([workspaces.py:273-343](../../../src/call_manager/proposal_assist/web/workspaces.py#L273)) fetches `ChatTurn WHERE workspace_id == … AND position > last_seen` — **not** filtered by `turn_job_id`. Only the *seed position* (the job's user-turn position) and the *terminal check* (`tj.status`) are job-scoped. The stream is therefore **workspace-global**: any connector, after its own seed, sees *every* later workspace row. Because the composer stays enabled ([app.js:62-69](../../../src/call_manager/web/static/app.js#L62) disables only `.section-edit-btn`), turns can stack — and two live connectors then stream the same later rows → duplicate chat rows. This is a latent bug today, and it is the one thing that made naive "one connector per active job on reload" unsafe.

An earlier draft of this spec resolved that by **unifying into a single per-tab workspace monitor** (sole row renderer, terminate-on-empty-queue, drop the optimistic echo). Codex review (2026-06-13) showed that model forces two hard, unsolved sub-problems: a **per-tab monitor-start** decision that a global queue check cannot make, and a **terminate-vs-submit TOCTOU**. Both arise *only* from trying to keep exactly one monitor per tab.

**Chosen model (A2): keep one connector per job; suppress duplicate rows client-side by stable DOM id.** Each connector watches exactly one `turn_job_id` (terminal = that job terminal, a single-row check), and the page drops any chat row whose `id="chat-turn-{turn.id}"` is already present. This dissolves both hard problems:

- **No per-tab start decision.** `post_turn` always emits the submitting turn's connector; `get_workspace_main` always emits one connector per active job on reload. Per-**job**, not per-**tab** — no client/tab identity required.
- **No terminate-vs-submit TOCTOU.** A connector ends on *its own* job's terminal status; new submits create new, independent connectors. There is no global "any active job?" race.
- **The optimistic echo stays** (snappy submit), because duplicate suppression is handled by the dedup, not by removing a writer.

The cost is bounded redundant streaming (while several connectors are live they each carry the same later rows, deduped client-side) and a small, well-specified dedup layer. For the common 1–2-active-turn case this is negligible. The more principled alternative — a `turn_job_id` column so each connector streams only its own rows (no client dedup) — is recorded under *Rejected alternatives*; it is the better future model if job-grouped transcripts or retries become product requirements.

## Two latent bugs (fixed as part of this slice)

**Bug 1 — `sse-close="done"` is a no-op.** The vendored `htmx-sse.js` reads `sse-swap` but **never reads `sse-close`** (it only closes the `EventSource` when the connector leaves the DOM — [htmx-sse.js:144-148](../../../src/call_manager/web/static/htmx-sse.js#L144)). So the connector ([workspaces.py:1011-1019](../../../src/call_manager/proposal_assist/web/workspaces.py#L1011)) never self-removes on `done`; the `EventSource` stays open, the browser reconnects, and the `htmx:beforeCleanupElement` hook ([app.js:73-78](../../../src/call_manager/web/static/app.js#L73)) never reliably fires. Fixed with an explicit `sse-swap="done"` **delete-child** so removal (and cleanup) is real.

**Bug 2 — terminal-vs-rows ordering race.** Each poll fetches new rows *then* checks terminal status ([workspaces.py:316-341](../../../src/call_manager/proposal_assist/web/workspaces.py#L316)). Under READ COMMITTED the two reads see different snapshots: if a finalize commits **between** them, the generator sees terminal and emits `done` **without yielding the just-written outcome row** — so a fresh failure row is dropped from the live stream (it only appears on a later reload). Fixed by reordering each poll: **read terminal status → fetch + yield rows → if terminal, emit `done`.** Because B1's `record_terminal_outcome` writes the outcome row **and** the status flip in one transaction, any poll that reads "terminal" is guaranteed (by the later in-poll row fetch) to also see the atomically-committed outcome row before `done`.

## The echo model — keep the echo, render the *persisted* turn, dedup by stable id

The optimistic echo stays for snappy submits. Two changes make it duplicate-safe:

1. **`post_turn` renders the persisted user `ChatTurn` (via `job.user_turn_id`), not a synthetic one.** Today it builds an anonymous `ChatTurn(...)` with no database `id` ([workspaces.py:987-1010](../../../src/call_manager/proposal_assist/web/workspaces.py#L987)), so it cannot carry a stable DOM id. Fetching the row `submit_user_turn` already persisted gives it a real `id` (and drops the now-redundant `echo_content` rebuild — the persisted `content` is authoritative).
2. **Every chat row carries `id="chat-turn-{turn.id}"`**, and the page suppresses a row whose id is already present, across **both** swap channels (they are distinct code paths — see *Component: dedup*).

The race this defends against (Codex's headline regression): a connector for an *earlier* job is workspace-global, so it can stream a *new* turn's user row at the same time `post_turn` echoes it. Whichever lands second is dropped by id. This holds in both orderings — the streamed copy arriving after the echo, or before it (slow POST response).

## Components

### F. Generator — `since_position` seed + ordering fix — `_stream_turn_generator`

Keep the per-job terminal check; add a caller-supplied seed and reorder the poll body.

- **Signature:** add `since_position: int | None = None`. Seed `last_seen_position = since_position` when supplied; otherwise fall back to today's query (the job's user-turn position). The job-existence guard (`job is None or job.workspace_id != workspace_id` → `done`/`missing`) is unchanged.
- **Poll body reorder (fixes bug 2):** **(1)** `tj = s.get(TurnJob, turn_job_id)`; `terminal = tj is None or tj.status in TERMINAL_STATUSES`; **(2)** fetch + yield all rows `position > last_seen_position` (unchanged workspace-global query + reference-map render), advancing `last_seen_position`; **(3)** if `terminal`, emit `event: done` (payload = the status, or `missing`) and return.
- **Terminal condition stays job-scoped** (`turn_job_id`), so each connector ends exactly when *its* turn ends — no queue-drain coordination, no TOCTOU.
- `request.is_disconnected()` early-exit is unchanged.
- **Route:** `GET /workspaces/{id}/turns/{job}/stream` gains an optional `?since={position}` query param (owner-or-viewer; existing `job belongs to workspace` 404 guard retained), forwarded as `since_position`. Omitted by `post_turn`; set to the last rendered position by reload.

### G. Connector partial — `partials/turn_connector.html`

Extract the connector markup (today an f-string at [workspaces.py:1011-1019](../../../src/call_manager/proposal_assist/web/workspaces.py#L1011)) into a partial rendered by **both** `post_turn` and `workspace_main.html`, so there is one source of truth for it. Parameters: `workspace_id`, `job_id`, `since` (optional), `job_status` (`"queued"`/`"running"`).

```html
<div id="sse-mount-{{ job_id }}" hx-ext="sse"
     sse-connect="/workspaces/{{ workspace_id }}/turns/{{ job_id }}/stream{% if since is not none %}?since={{ since }}{% endif %}">
  <div sse-swap="message" hx-target="#chat-history" hx-swap="beforeend"></div>
  <div sse-swap="done" hx-target="#sse-mount-{{ job_id }}" hx-swap="delete"></div>
  <div class="turn-progress" aria-busy="true">
    Working…
    {% if job_status == "queued" %}
      <button type="submit"
              hx-post="/workspaces/{{ workspace_id }}/turns/{{ job_id }}/cancel"
              hx-target="#cancel-status-{{ job_id }}" hx-swap="innerHTML">Cancel</button>
      <span id="cancel-status-{{ job_id }}"></span>
    {% endif %}
  </div>
</div>
```

- `sse-swap="message"` → appends streamed rows to `#chat-history`.
- `sse-swap="done"` → **deletes** `#sse-mount-{job}` (`hx-swap="delete"`), which makes `htmx-sse.js` close the `EventSource` (no reconnect loop — [htmx-sse.js:144-148](../../../src/call_manager/web/static/htmx-sse.js#L144)) and fires `htmx:beforeCleanupElement` — the reliable "this turn finished" signal that drives the artifact refresh + the re-enable check (component K/L).
- **Cancel** shows only for a `queued` job (a `running` turn can't be cancelled — B1's helper guards on `WHERE status='queued'`), each with its own `#cancel-status-{job}` target so a cancel response never lands in the transcript. The id keeps the `sse-mount-` prefix the app.js cleanup hook keys on.

**Invariant — at most one connector per job id per document.** Each submit creates a distinct job (so `post_turn` never emits two connectors for one job), and a full reload replaces the DOM (so `post_turn` connectors and `get_workspace_main`'s reload connectors never coexist). The `done` delete-child relies on this: two live elements sharing `id="sse-mount-{job}"` would make `document.getElementById` (and thus the delete swap) ambiguous. Do **not** introduce a partial htmx swap of `workspace_main` into a still-live page without revisiting this.

**Stale Cancel is benign.** The button is rendered from a status read a moment earlier, so a job can flip `queued→running` before the click. B1's helper guards on `WHERE status='queued'`, so a stale click is safely rejected with the "too late" fragment (component M) — never a bad state. No client-side staleness handling is needed.

### H. `post_turn` — echo the persisted turn + always connect

After enqueue (unchanged error branches → `HTTPException`, from B1):

- Fetch the persisted user turn: `persisted = session.get(ChatTurn, job.user_turn_id)`; build its reference map (`_collect_reference_ids([persisted])` → `_load_reference_map`) and render it through the `chat_turn` partial (now carrying `id="chat-turn-{id}"`). This replaces the synthetic-echo block ([workspaces.py:987-1010](../../../src/call_manager/proposal_assist/web/workspaces.py#L987)). `job.user_turn_id` is a committed NOT-NULL FK that `submit_user_turn` wrote in the same transaction, so the fetch is guaranteed to return the row; if it ever returns `None`, that is an invariant violation — **raise** rather than fall back to an id-less echo (an id-less row would silently defeat the dedup, the one failure mode worth a guard).
- Render the **connector partial** for `job.id` (`since` omitted → generator seeds at the user-turn position; `job_status="queued"`).
- Return `persisted_user_row_html + connector_html`.

No "single-emit" decision and no echo removal: always echo, always connect. Duplicate suppression is the dedup's job, not the server's.

### I. `get_workspace_main` — reconnect on render (active-jobs query **before** history)

- **(1)** Query active jobs **first**: `select(TurnJob.id, TurnJob.status).where(TurnJob.workspace_id == ws.id, TurnJob.status.in_(("queued","running"))).order_by(TurnJob.created_at)`. **(2)** Then fetch chat history and note the max rendered `position` (`P`). Ordering matters: querying active jobs *before* history means a job that finalizes between the two reads still has its outcome row picked up by the (later) history fetch — so the page never renders "no connector **and** missing final row." (The reverse order can drop the final row with no connector to ever stream it.)
- For **each** active job, render the connector partial seeded at `?since=P`, with `job_status` driving Cancel-vs-working. Two active jobs → two connectors, both workspace-global, deduped client-side. `P` is the max `position` the history loop rendered; because an active job always has its user turn in that history, `P ≥ 1` for any rendered connector (an empty history cannot coincide with an active job). Reload connectors pass `?since=P`; `post_turn`'s connector omits `since` so the generator seeds at the job's user-turn position. `since` omitted ≠ `since=0`: only an explicit `0` seeds at genesis (the generator's `None` check selects the user-turn-position default).
- Render section-edit buttons **disabled** when any active job exists; enabled otherwise (today's behaviour).
- Expose `data-workspace-id="{ws.id}"` on a stable page element for the app.js artifact refetch.

### J. Dedup — stable id + two client hooks

- **Stable id:** `chat_turn.html` ([:26](../../../src/call_manager/web/templates/proposal_assist/partials/chat_turn.html#L26)) emits `{% if turn.id %}id="chat-turn-{{ turn.id }}"{% endif %}` on the `.chat-turn` div. All three render paths now pass a persisted turn (history loop, SSE renderer, post_turn echo), so all three carry the same id for the same row — the invariant the dedup relies on.
- **Streamed rows — `htmx:sseBeforeMessage`** (cancelable; [htmx-sse.js:151](../../../src/call_manager/web/static/htmx-sse.js#L151)): parse the row id out of `evt.detail.data`; if `document.getElementById(id)` already exists, `evt.preventDefault()` to skip the swap. (The SSE extension's own `swap()` does **not** fire `htmx:beforeSwap` — [htmx-sse.js:315](../../../src/call_manager/web/static/htmx-sse.js#L315) — so streamed rows must be deduped here.)
- **POST-response rows — `htmx:beforeSwap`:** when `evt.detail.target?.id === "chat-history"`, parse `evt.detail.serverResponse`, drop any `.chat-turn[id]` already present in `#chat-history`, and write the trimmed HTML back to `evt.detail.serverResponse` (the `sse-mount-…` connector div has no `chat-turn` id and passes through). This covers the case where a racing connector rendered the new user row *before* the (slow) POST response lands.
- Both hooks share one helper: extract `id` from a fragment / drop already-present `.chat-turn` nodes via a detached `<template>`.

### K. Section-edit gating — restore the multi-connector guard

With per-job connectors, several `sse-mount-*` can coexist (stacking, reload), so the B-era "finding 7" is **not** structurally subsumed and the guard must be restored:

- `htmx:beforeRequest` on `#composer` → disable `.section-edit-btn` (unchanged, [app.js:62-69](../../../src/call_manager/web/static/app.js#L62)).
- `htmx:beforeCleanupElement` on an `sse-mount-*` element → **defer** the re-enable (it fires *before* removal, so the leaving element is still in the DOM): schedule a `setTimeout(…, 0)` that re-enables **only if** `document.querySelector('[id^="sse-mount-"]')` is now null. Re-enabling immediately ([app.js:73-78](../../../src/call_manager/web/static/app.js#L73)) would wrongly unlock editing while another turn is still live. The check **must** run after removal (hence the deferred macrotask) — a synchronous check inside the handler would still count the leaving element and never re-enable. If a new connector mounts before the deferred check runs, the query finds it and editing correctly stays disabled (a turn is live). Do not "simplify" this into an in-handler synchronous check.
- `get_workspace_main` renders the disabled state on reload (component I).

### L. Artifact-pane refresh — `artifact_body.html` + GET route + app.js hook

- **New partial `artifact_body.html`** — extract the sections-or-placeholder conditional now inline in `artifact_pane.html` ([:17-23](../../../src/call_manager/web/templates/proposal_assist/partials/artifact_pane.html#L17)) — the `{% if artifact_sections %} … {% else %}` "(no proposal.md yet)" placeholder — into a standalone partial that `artifact_pane.html` `{% include %}`s. Without this, a **failed first turn** (no `proposal.md` yet → empty `artifact_sections`) would refresh `#artifact-rendered` to blank.
- **Route** `GET /workspaces/{id}/artifact` returns **only** `partials/artifact_body.html` (sections-**or-placeholder**), with `artifact_sections`/`artifact_rendered`/`is_owner`/`annotations_enabled` populated as `get_workspace_main` does — *not* `artifact_pane.html` (which would nest a duplicate `#artifact-rendered`). Owner-or-viewer.
- **app.js hook** — in the same `htmx:beforeCleanupElement` handler (component K), when an `sse-mount-*` is cleaned up, fire `htmx.ajax('GET', '/workspaces/'+wsId+'/artifact', {target:'#artifact-rendered', swap:'innerHTML'})`. This runs on **each** connector completion; the refetch is idempotent, so a no-op-looking repaint after an unchanged/failed turn is harmless. `wsId` from `data-workspace-id`.

### M. Cancel route returns a small status fragment — `cancel_turn`

`cancel_turn` (already routed through B1's `record_terminal_outcome`, so the state change + system row + event are handled) returns a tiny fragment for `#cancel-status-{job}`: on a real cancel, "Cancelled."; on a no-op (already running/terminal), "Too late — the assistant already started." The route keeps its race-safe `WHERE status='queued'` guard (the helper's conditional update).

## Data flow

**Success (single turn):** owner sends → `post_turn` enqueues, returns the persisted user row + the job's connector (seeded at the user-turn position) → connector streams assistant rows into `#chat-history` → turn completes → next poll reads terminal, yields any final row, emits `done` → the `done` child deletes `#sse-mount-{job}` → `EventSource` closes, `beforeCleanupElement` fires → artifact refetch repaints `#artifact-rendered`; no other `sse-mount-*` remains → section editing re-enables.

**Stacked submit:** a second turn while one runs → `post_turn` returns the new persisted user row + a second connector. Both connectors are workspace-global; overlapping rows are deduped by id, so each row appears once. Each connector ends with its own job; editing re-enables only after the **last** `sse-mount-*` is cleaned up. Cancel shows on the still-`queued` connector.

**Mid-turn reload:** `get_workspace_main` queries active jobs (before history) → renders one connector per active job seeded at the last rendered position + Cancel(s) for queued + disabled editing → streaming resumes with no page reload; each connector ends with its job.

**Failure (B1 writes the outcome row):** the connector's next poll reads terminal, then fetches the atomically-committed `turn_outcome` row (yields it), then emits `done` → connector deleted → artifact refetch (no-op) + re-enable check. The categorized failure row is visible **live**, not only on reload.

**Cancel (while queued):** owner clicks a job's Cancel → route flips `queued→cancelled` via B1's helper + returns "Cancelled." into `#cancel-status-{job}` → that job's next poll reads terminal → `done` → connector removed.

## Error handling / edge cases

- **Cross-tab is best-effort, not a sync mechanism (honest scope).** A workspace-global per-job connector can *briefly* stream another tab's turn's rows (deduped if already present, otherwise shown live) and then stop when **its own** job finishes. A2 guarantees the *submitting* and *reloaded* tab's own loop; it does **not** guarantee a tab keeps live-syncing turns started elsewhere. A tab that started no turn and is not reloaded may not see another tab's turn until its next render. Acceptable for v1.
- **Two tabs on one workspace:** each tab renders its own connectors; within a tab, dedup keeps each row once. Cross-tab streams are independent (no shared DOM).
- **Dedup linchpin:** correctness depends on all render paths emitting the *same* `id="chat-turn-{id}"` for the same row — guarded by the stable-id contract test.
- **Connection dropped mid-turn:** `request.is_disconnected()` exits the loop; on reload, reconnect-on-render re-establishes the connector seeded at the last rendered position (no re-stream of already-rendered rows; any that slip through are deduped).
- **`done` payload:** carries the terminal status string; the dedup hook ignores it (no extractable `chat-turn` id), and the delete-child triggers on the event name, not the payload.
- **Viewer:** may watch a connector (owner-or-viewer on the stream route); sees streamed assistant rows + B1's viewer-safe `turn_outcome` rows; no Cancel (not owner).

## Rejected alternatives

- **Single per-tab workspace monitor (drop the echo, terminate on empty queue).** The model the prior draft chose. Forces a per-tab monitor-start decision a global queue check cannot make, and a terminate-vs-submit TOCTOU. Both vanish under per-job connectors. Rejected.
- **A1 — job-scoped stream via a `turn_job_id` column on `chat_turns`.** Each connector streams only its own job's rows (no client dedup, no redundant streaming, cleaner server semantics). Rejected for B2 on cost/benefit: a migration plus job attribution on every chat writer, for a robustness the cheap client dedup already delivers at 1–2 active turns. It is the better future model if job-grouped transcripts, retries, or stronger server-side stream semantics become requirements.

## Non-goals (later Theme-7 slices)

DOCX export (slice C); resolved call-title/profile pickers, colored status badge, link-back, deadline-in-header, version-history restore (slice D); a richer per-token streaming indicator; tool-chatter collapse; capping the queue depth.

## Testing

Following `tests/proposal_assist/*` patterns (TestClient + `pg_session_factory`; the generator is unit-driven directly):

- **Stable-id contract (the dedup linchpin — automatable, highest value):** for one persisted user turn, assert the `post_turn` echo render, the generator's `_render_chat_turn_html`, and the history-loop render all emit the **identical** `id="chat-turn-{turn.id}"`. If these match, the client dedup can collapse duplicates; this is the load-bearing server-side guard for Codex's headline race.
- **Client cross-channel dedup (manual — no JS test harness today):** documented manual check — with one connector live, submit a second turn under throttled network and confirm exactly one user row in `#chat-history`. The project has no jsdom/Playwright harness; adding one for the two hooks is a possible follow-up. The stable-id contract test above is the automated proxy.
- **Generator ordering (bug 2) — durable property:** under terminal-read-first ordering, with a finalize that commits the outcome row + terminal status atomically (B1's helper), assert the generator **always yields the outcome row before `done`** — never after — across several finalize timings (before the poll; immediately after a non-terminal poll's reads; concurrent with a poll). The row may arrive on the same or the next poll; the invariant is "no row is lost behind `done`."
- **`since_position` seeding:** the generator does not re-yield rows at/below `since_position`; with no `since`, it seeds at the job's user-turn position (back-compat).
- **`post_turn` echo + connector:** returns the **persisted** user row (carrying `id="chat-turn-{id}"`, not a synthetic row) **and** a `#sse-mount-{job}` connector; the connector contains the `sse-swap="done"` delete-child and (queued job) a Cancel targeting `#cancel-status-{job}` (not `#chat-history`); the legacy `sse-close` attribute is gone.
- **Reconnect on render:** `get_workspace_main` with one active job renders one connector seeded at the last rendered position + disabled section-edit; with two active jobs, two connectors (one Cancel for the queued, working-only for the running); with none, no connector and editing enabled. Assert active-jobs query precedes the history query (e.g. via the rendered seed reflecting the full history).
- **Artifact route:** `GET /{id}/artifact` returns `artifact_body.html` (no nested `#artifact-rendered`), owner-or-viewer; the **empty-artifact case** (no `proposal.md`) returns the "(no proposal.md yet)" placeholder, **not** a blank body; 404 on a missing workspace.
- **Cancel route:** queued → "Cancelled." fragment (+ B1's `cancelled` row/event); running → "too late" fragment + no state change.

## Files

- Create: `src/call_manager/web/templates/proposal_assist/partials/turn_connector.html` (the connector markup, rendered by both `post_turn` and `workspace_main.html`).
- Create: `src/call_manager/web/templates/proposal_assist/partials/artifact_body.html` (sections-or-placeholder body, extracted from `artifact_pane.html`).
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` (`_stream_turn_generator` → `since_position` + ordering fix; `stream_turn` route `?since`; `post_turn` → render persisted turn + connector partial; `get_workspace_main` → active-jobs-before-history query + per-job connectors + disabled-edit context + `data-workspace-id`; `cancel_turn` fragment; new `GET /{id}/artifact`).
- Modify: `src/call_manager/web/templates/proposal_assist/partials/chat_turn.html` (stable `id="chat-turn-{id}"`); `partials/artifact_pane.html` (include `artifact_body.html`); `workspace_main.html` (per-job connector render on reload via the partial, `data-workspace-id`, section-edit disabled state).
- Modify: `src/call_manager/web/static/app.js` (`htmx:sseBeforeMessage` + `htmx:beforeSwap` dedup; deferred multi-connector re-enable guard; artifact refetch on cleanup); `src/call_manager/web/static/app.css` (`.turn-progress` / Cancel styling).
- Add tests: extend `test_web_workspaces_sse.py` (ordering durable property, `since` seeding, stable-id contract), `test_web_workspaces_turns.py` (persisted-echo + connector, reconnect-on-render + query order), `test_web_workspaces_cancel.py` (fragment), + an artifact-route test.
