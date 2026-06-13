# Proposal-Assist — Terminal-Outcome Recording & Error Hygiene — Design Spec

**Date:** 2026-06-13
**Status:** Draft for implementation
**Audience:** Engineers working on call-manager
**Theme:** Product review 2026-06-11, Theme 7 (proposal-assist workspace seams). The original "turn feedback loop" slice is split into two concern-aligned sub-slices: **B1 (this spec) — terminal-outcome recording + error hygiene**, and **B2 — the live turn monitor** ([2026-06-13-proposal-assist-turn-monitor-design.md](2026-06-13-proposal-assist-turn-monitor-design.md)).

## Why the split

The original slice tangled three concerns: (1) *recording* every terminal outcome durably and visibly, (2) *error hygiene* so a rejected submit doesn't eat the typed message, and (3) *live monitoring* — real-time streaming, reload-resilience, the artifact-pane refresh, and working/Cancel affordances. Concerns (1) and (2) are independent of the streaming model and ship standalone value: a failed turn becomes a persistent, categorized chat row **visible on the next render** (the history loop) with **no connector or generator changes**. Concern (3) requires rebuilding the connector as a single workspace monitor (the SSE stream is workspace-global, so per-turn connectors double-stream once turns stack) — that is B2, and it builds the correct model **once**, with no per-turn connector to demolish.

This spec is B1. It needs no changes to `_stream_turn_generator` or the SSE connector.

## Purpose

Two failures today:

1. **Silent failures.** A failed turn writes only `turn_jobs.status='failed'` + a `cloud_turn_failed` `WorkspaceEvent` ([processor.py:429](../../../src/call_manager/proposal_assist/turns/processor.py#L429)) — **no chat row** — so a 600 s timeout looks identical to "the assistant ignored me." The error text lives only on the admin panel. Worse, **three** other failure paths don't even write the audit event: the stuck-turn reaper ([reaper.py:78](../../../src/call_manager/proposal_assist/turns/reaper.py#L78)), the crash/redeploy orphan sweep ([orphan_sweep.py:25](../../../src/call_manager/proposal_assist/drafting/orphan_sweep.py#L25)), and workspace **soft-delete** ([workspaces.py:1305-1309](../../../src/call_manager/proposal_assist/web/workspaces.py#L1305)) all do bulk `UPDATE … status='failed'` with no event and no row. Soft-delete additionally orphans the per-turn `cloud_turn_sent` audit chain ([submit.py:401](../../../src/call_manager/proposal_assist/turns/submit.py#L401)) — a `cloud_turn_sent` with no matching outcome event.
2. **Error responses eat the typed message.** Most `post_turn` error branches return `HTMLResponse(4xx/5xx)` with an HTML body htmx won't swap (the composer targets `#chat-history` with `hx-swap='beforeend'`, which ignores non-2xx); and `hx-on::after-request="this.reset()"` clears the textarea even on failure — so a rejected turn vanishes with no message and no recoverable text.

This slice makes every terminal failure/cancel a **persistent, categorized, viewer-safe** chat row + a consistent audit event, and makes error responses **readable** without eating the typed message.

## Goals

- Every terminal **failed**/**cancelled** transition — from **all five writers** — emits a consistent audit event, closing the reaper / orphan / soft-delete gaps.
- Every **user-facing** terminal outcome emits a persistent, categorized, **viewer-safe** `system` chat row (raw diagnostics stay owner/admin-only), visible on the next render via the history loop.
- `post_turn` errors render inline as JSON `detail` for htmx requests (regardless of `Accept`) and never clear the composer on failure.
- Soft-delete's audit guarantee holds **under concurrency** — no turn can be enqueued after a workspace is soft-deleted.
- No behavior change to streaming of assistant rows — the connector and generator are untouched here (B2 owns them).

## Two invariants

- **Audit:** every terminal turn transition — including reaper, orphan, and soft-delete — emits `cloud_turn_failed` / `cloud_turn_cancelled`.
- **Visible:** every **user-facing** terminal outcome emits a `system` `turn_outcome` chat row. Soft-delete is the sole non-user-facing exception (the workspace is 404 the instant it's deleted) — audit event only, no row.

## Components

### A. Terminal-outcome helper (the centralization) — `turns/outcomes.py` (new)

A single writer for terminal **failed**/**cancelled** outcomes so status, the visible row, and the audit event stay consistent across all five call sites.

```python
def record_terminal_outcome(
    session,                      # caller's open Session (caller commits)
    *,
    turn_job_id: uuid.UUID,
    workspace_id: uuid.UUID,
    outcome: str,                 # 'failed' | 'cancelled'
    expected_status: str,         # 'running' | 'queued' — the conditional guard
    raw_error: str | None,        # stored in the audit event, NEVER in the chat row
    emit_chat: bool = True,       # False → audit event only (soft-delete: no user-facing surface)
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> bool:
    ...
```

Behaviour, all in the caller's transaction:

1. **Conditional status flip (workspace-scoped, atomic read-back)** — `UPDATE turn_jobs SET status=<terminal>, error=raw_error, completed_at=now WHERE id=turn_job_id AND status=expected_status AND workspace_id=workspace_id RETURNING user_turn_id, submitted_by_user_id` (terminal = `failed` for `outcome='failed'`, `cancelled` for `'cancelled'`). The `workspace_id` predicate mirrors the existing cancel route's deliberate scoping ([cancel_turn:1402-1410](../../../src/call_manager/proposal_assist/web/workspaces.py#L1402-L1410)) and prevents a mis-call from terminalizing a job in workspace A while writing its row/event into workspace B. If the statement returns **no row** (already terminal / reaped / wrong workspace), **return `False`** and do nothing else — preserving the existing no-double-finalize guard ([processor.py:456-469](../../../src/call_manager/proposal_assist/turns/processor.py#L456-L469)). (Test the `RETURNING` result, **not** `rowcount`: under psycopg3 `rowcount` is unreliable for `UPDATE … RETURNING`.)
2. Take `user_turn_id` + `submitted_by_user_id` from the `RETURNING` row (no separate read). Clear the user-turn's `tokens_in` pre-count (mirrors the current finalize/reaper behaviour so a failed turn doesn't burn cap budget — [processor.py:446-449](../../../src/call_manager/proposal_assist/turns/processor.py#L446-L449), [reaper.py:87-90](../../../src/call_manager/proposal_assist/turns/reaper.py#L87-L90)).
3. **Write the visible row (if `emit_chat`)** — `ChatTurn(workspace_id, role='system', content={"type":"turn_outcome","outcome":outcome,"text": <safe_text>,"turn_job_id": str(turn_job_id),"user_turn_id": str(user_turn_id)})`. `position` comes from the existing `chat_turns_position_seq`. `safe_text` is produced by `outcome_message(outcome, raw_error)` (component B) — **never the raw error**. The `turn_job_id`/`user_turn_id` give the row **stable attribution**: because user turns can stack, an outcome for turn A can land in the transcript *after* user turn B and read as B's failure, so the row must carry which turn it belongs to. The visible copy stays generic for now; B2's retry/grouping UI consumes the ids. When `emit_chat=False` (soft-delete), skip the row.
4. **Write the audit event** — `WorkspaceEvent(kind='cloud_turn_failed' | 'cloud_turn_cancelled', actor_id=submitted_by_user_id, payload={turn_job_id, tokens_in, tokens_out, error: raw_error})`. Always written. The raw diagnostics live here (owner/admin-only).
5. Return `True`.

### B. Safe outcome message — `outcome_message(outcome, raw_error) -> str` (pure, in `turns/outcomes.py`)

Maps the outcome + raw error to a short, viewer-safe sentence. **Never** echoes `raw_error`. Categorization (do not call every cancellation a timeout):

| Condition | Message |
|---|---|
| `outcome == 'cancelled'` | "This turn was cancelled before it started." |
| `raw_error` starts with `"reaped:"` | "This turn was stopped after running too long." |
| `raw_error == "orphaned"` | "This turn was interrupted. Please try again." |
| `outcome == 'failed'`, any other `raw_error` | "The drafting assistant couldn't finish this turn. Please try again." |

(We deliberately do **not** emit a "timed out" label: the in-loop `turn_max_seconds` bound surfaces as a generic `CancelledError`-derived `processor_failure:`/`sdk_turn_error:` string — [drain.py:105-118](../../../src/call_manager/proposal_assist/turns/drain.py#L105-L118), [processor.py:288-301](../../../src/call_manager/proposal_assist/turns/processor.py#L288-L301) — so "couldn't finish… please try again" is the honest message. The reaper's `reaped:` prefix is the one case we *can* attribute to over-runtime.) The cancel and orphan lines are phrased **neutrally** — not "You cancelled…" (the same row is shown to **viewers** who can't cancel) and not "the service restarted" (orphaning can follow a subprocess crash *or* a redeploy). Pure function → unit-testable without a DB. The `emit_chat=False` (soft-delete) path never reaches this, so its `"workspace soft-deleted"` raw error needs no mapping.

### C. Migration — `'system'` role

Alembic migration extends `ck_chat_turns_role` from `('user','assistant','tool')` to `('user','assistant','tool','system')` ([models.py:778-780](../../../src/call_manager/db/models.py#L778-L780)) and updates the model's `CheckConstraint`. No data backfill. `'system'` rows are produced only by `record_terminal_outcome`. Forward-only (removing `'system'` later would require no `system` rows to exist — out of scope).

### D. Render the outcome row — `chat_turn.html`

Add a dispatch branch for the new content shape `{"type":"turn_outcome", ...}` ([chat_turn.html](../../../src/call_manager/web/templates/proposal_assist/partials/chat_turn.html)): render a muted system notice (e.g. `<p class="turn-outcome">{{ c.text }}</p>`) rather than falling through to the `<pre>` default. Autoescape stays on (no `| safe`). The existing `data-role="{{ turn.role }}"` already carries `system` for styling. One small CSS rule for `.turn-outcome` (reuse `--cm-muted`/callout tokens; prefer no new tokens). This is what makes a recorded outcome **visible on render** — the history loop renders it on the next page load with no connector involved.

### E. The five writers route through the helper

- **Processor** ([processor.py:429 `_finalize_failed`](../../../src/call_manager/proposal_assist/turns/processor.py#L429)) — its **body** becomes a call to `record_terminal_outcome(outcome='failed', expected_status='running', raw_error=error, emit_chat=True, …)` inside one `session_factory()` transaction; it stays as a thin wrapper with its current signature so its callers ([processor.py:170,193,288,300](../../../src/call_manager/proposal_assist/turns/processor.py#L170)) are unchanged (same `error=` strings). The helper re-reads `user_turn_id`/`submitted_by_user_id` from the `TurnJob` row, so the wrapper need not thread them.
- **Cancel** ([cancel_turn:1376](../../../src/call_manager/proposal_assist/web/workspaces.py#L1376)) — its state change is replaced by `record_terminal_outcome(outcome='cancelled', expected_status='queued', raw_error=None, emit_chat=True, …)` so a cancel produces the system row + event. The route keeps its **current response contract** in B1; surfacing a Cancel **button** + a status fragment is B2's component H.
- **Reaper** ([reaper.py:78](../../../src/call_manager/proposal_assist/turns/reaper.py#L78)) — the bulk `UPDATE` becomes a select-then-loop: for each stuck job, `record_terminal_outcome(outcome='failed', expected_status='running', raw_error="reaped: …", emit_chat=True)`. Stuck jobs are normally zero, so per-job cost is irrelevant. The function returns the count of **successful** transitions (`record_terminal_outcome` returned `True`), **not** the number selected — a job that raced to terminal between the select and the call returns `False` and must not be counted.
- **Orphan sweep** ([orphan_sweep.py:19](../../../src/call_manager/proposal_assist/drafting/orphan_sweep.py#L19)) — same select-then-loop with `raw_error="orphaned"`, `emit_chat=True`, returning the count of **successful** transitions (as reaper). (Caller still commits, per its docstring.)
- **Soft-delete** ([workspaces.py:1305-1309](../../../src/call_manager/proposal_assist/web/workspaces.py#L1305)) — the bulk `UPDATE … status='failed'` becomes a select-then-loop over the workspace's **queued** turns calling `record_terminal_outcome(outcome='cancelled', expected_status='queued', raw_error="workspace soft-deleted", emit_chat=False)`. **`cancelled`, not `failed`**: these turns never started and were intentionally stopped by the owner. **`emit_chat=False`**: the workspace is 404 the instant it's soft-deleted, so a viewer-safe row would be dead-on-arrival; the audit event is the value (it closes the per-turn `cloud_turn_sent → cloud_turn_cancelled` chain that the bulk update previously orphaned). Runs in soft-delete's existing transaction, before its `session.commit()`. Per-workspace queued turns are ~0–1, so the loop is cheap.

After this, **every** terminal failed/cancelled transition writes an audit event (the three previously-eventless paths gain one), and every **user-facing** outcome writes a visible row.

### J. `post_turn` error branches → JSON `detail` + composer reset gate

- Convert **every** user-facing error branch in `post_turn` to `HTTPException(status_code=…, detail="…")` so app.js's global `htmx:responseError` handler ([app.js:88-104](../../../src/call_manager/web/static/app.js#L88-L104)) renders the `detail` inline. Branches: kill-switch 503 ([:760](../../../src/call_manager/proposal_assist/web/workspaces.py#L760)), disclosure 403 ([:780](../../../src/call_manager/proposal_assist/web/workspaces.py#L780)), the **two** cap 429s — global and per-workspace ([:940-960](../../../src/call_manager/proposal_assist/web/workspaces.py#L940-L960)) — and the upload-validation / storage 400/500 branches around [:824-1004](../../../src/call_manager/proposal_assist/web/workspaces.py#L824). **`HTTPException` accepts `headers=`: the global-cap 429 must keep `headers={"X-Token-Cap": "global"}` and a `detail` that still contains "Global"/"global" — both asserted at [test_global_cap.py:144](../../../tests/proposal_assist/test_global_cap.py#L144).** The per-workspace cap 429 has no header to preserve. The setup-guard 409 (slice A) is already in this form — it's the template.
- **Cleanup ordering:** each converted branch must run its existing cleanup — `session.rollback()` and `_rollback_created_uploads()` (the upload/file rollback the current `HTMLResponse` branches do, e.g. [:940-950](../../../src/call_manager/proposal_assist/web/workspaces.py#L940)) — **before** raising the `HTTPException`. The `raise` must not skip the rollback that the `return HTMLResponse(...)` performed.
- Composer ([workspace_main.html](../../../src/call_manager/web/templates/proposal_assist/partials)) — change `hx-on::after-request="this.reset()"` to `hx-on::after-request="if (event.detail.successful) this.reset()"` so the typed text survives a rejected turn.

### K. Close the enqueue-after-delete race — `turns/submit.py`

`submit_user_turn` locks the workspace row `FOR UPDATE` ([submit.py:243-245](../../../src/call_manager/proposal_assist/turns/submit.py#L243)) but selects only `Workspace.id` — it **does not re-check `soft_deleted_at`**. A `post_turn` whose `_require_owner` soft-delete check ([workspaces.py:361](../../../src/call_manager/proposal_assist/web/workspaces.py#L361)) passed just before a concurrent `soft_delete` commits will then acquire the lock — the two serialize, since submit's `SELECT … FOR UPDATE` conflicts with soft_delete's `UPDATE workspaces` row write-lock — and enqueue a turn **after** soft-delete's cancel-loop already ran, writing a `cloud_turn_sent` for a turn the drain will never run ([drain.py:69](../../../src/call_manager/proposal_assist/turns/drain.py#L69) excludes soft-deleted workspaces). That orphans the very `cloud_turn_sent → outcome` chain component E's soft-delete path exists to close.

Fix: the locked query becomes `SELECT id FROM workspaces WHERE id=… AND soft_deleted_at IS NULL FOR UPDATE`; a `None` result now means *missing or soft-deleted* → reject the submit (surfaced via the web layer's existing 404/409 path, no longer a programmer-error `ValueError`). Because submit re-reads `soft_deleted_at` **under the lock**, the post-deletion ordering (soft_delete commits first) is now caught, and the soft-delete audit invariant holds under concurrency.

### L. Exception handler: htmx requests get JSON `detail` regardless of `Accept` — `main.py`

Component J only works if the `HTTPException` reaches app.js as JSON. But the global handler ([main.py:294-319](../../../src/call_manager/main.py#L294)) returns a **styled HTML page** for `{400, 403, 404, 409, 413}` whenever `Accept: text/html` is present — so an htmx submit that carries `text/html` (under `hx-boost`, via `hx-headers`, or future browser/htmx defaults) gets HTML, and app.js's JSON `detail` parse falls back to generic text. Make `HX-Request: true` **and not** `HX-Boosted: true` take precedence: a genuine htmx **partial** request always gets the JSON `{detail}` body, regardless of `Accept`; full-page navigations (no `HX-Request`, or boosted) keep the styled error page. This hardens every htmx error path, not just `post_turn` — the slice-A 409 currently relies on htmx not sending `text/html`, which is fragile. **Blast-radius note for the plan:** audit existing tests that assert a styled HTML error page on an `HX-Request` flow (they may need to expect JSON now).

## Data flow

**Failure (any of the five writers):** `record_terminal_outcome` flips status + (if user-facing) writes a `system` `turn_outcome` row + the audit event, atomically. The user sees a categorized "couldn't finish… try again" row **on their next render** (history loop) instead of silence. (Live, real-time appearance of this row — and the SSE terminal-ordering fix — is B2; here it is guaranteed on render.)

**Rejected submit:** `post_turn` raises `HTTPException(detail=…)` → app.js shows the detail inline; the composer keeps the typed text (reset gated on `successful`).

## Error handling / edge cases

- **Double-finalize / reaped-then-finalized:** the helper's `WHERE status=expected_status` guard returns `False` and writes nothing — no duplicate row or event (preserves [processor.py:456-469](../../../src/call_manager/proposal_assist/turns/processor.py#L456-L469)).
- **Cancel after processing starts:** `WHERE status='queued'` matches nothing → helper returns `False`; the route returns its current response; no row written (the eventual real outcome writes its own row).
- **Viewer:** sees the viewer-safe `turn_outcome` row only (no raw diagnostics, by construction).
- **Soft-delete:** audit event per queued turn, no chat row; the workspace returns 404 so there is no surface to render to.

## Non-goals (→ B2 / later Theme-7 slices)

- **All live-monitoring work is B2** ([2026-06-13-proposal-assist-turn-monitor-design.md](2026-06-13-proposal-assist-turn-monitor-design.md)): real-time streaming of the outcome row, the SSE terminal-ordering-race fix, the `sse-close` connector fix, the working/Cancel affordances, the single-workspace-monitor rebuild, reload-resilience, and the artifact-pane refresh.
- DOCX export (slice C); resolved call-title/profile pickers, colored status badge, link-back, deadline-in-header, version-history restore (slice D); tool-chatter collapse; surfacing the raw error to the owner via a separate owner-only channel (the audit event + admin panel remain the diagnostics path).

## Testing

Following `tests/proposal_assist/*` patterns (TestClient + `pg_session_factory`):

- **`outcome_message` unit tests:** cancelled / `reaped:` / `orphaned` / generic-failed each map to the right viewer-safe sentence; raw error never appears in the output.
- **`record_terminal_outcome`:** writes a `system` `turn_outcome` `ChatTurn` + the audit event + flips status, atomically; `emit_chat=False` writes the event but **no** chat row; `expected_status` mismatch (already terminal) → returns `False`, writes nothing; wrong `workspace_id` → returns `False`; clears the user-turn `tokens_in`.
- **All five writers:** processor failure, reaper, orphan sweep, queued-cancel, and **soft-delete** each flip status and write the corresponding `cloud_turn_failed`/`cloud_turn_cancelled` event; the first four (user-facing) write a `system` row, soft-delete does **not**; soft-delete uses `cancelled` status.
- **Migration:** a `ChatTurn(role='system')` inserts cleanly; the constraint still rejects an unknown role.
- **`chat_turn.html`:** a `turn_outcome` content row renders the `.turn-outcome` notice (not the `<pre>` fallback) and is autoescaped.
- **post_turn errors:** kill-switch/disclosure/cap/upload-validation each return their status with a JSON `detail` body (not HTML); the **global-cap 429 still carries `X-Token-Cap: global` and a detail containing "global"** (keep `test_global_cap.py` green); a static-content assertion that the composer markup gates the reset on `event.detail.successful`. An upload-validation rejection still rolls back created upload rows/files before the `HTTPException`.
- **Exception handler (component L):** an `HTTPException(403/409/…)` carrying **both** `HX-Request: true` and `Accept: text/html` returns a JSON `detail` body (not the styled HTML page); a plain navigation (no `HX-Request`) still gets the styled page; an `HX-Boosted: true` request keeps the styled page.
- **Enqueue-after-delete race (component K):** a submit whose request raced a `soft_delete` commit (workspace soft-deleted between the owner check and the lock) is rejected — no `TurnJob`, no `cloud_turn_sent` — ideally a concurrency regression test (mirroring the existing concurrent-cap test) that interleaves `soft_delete` and `submit_user_turn`.

## Files

- Create: `src/call_manager/proposal_assist/turns/outcomes.py` (`record_terminal_outcome`, `outcome_message`).
- Create: Alembic migration (extend `ck_chat_turns_role` with `'system'`).
- Modify: `src/call_manager/db/models.py` (the `ChatTurn` `CheckConstraint`).
- Modify: `src/call_manager/proposal_assist/turns/processor.py` (`_finalize_failed` → helper), `turns/reaper.py`, `drafting/orphan_sweep.py` (bulk → per-job helper calls, return successful-transition counts); `turns/submit.py` (lock query `+ soft_deleted_at IS NULL`, component K); `web/workspaces.py` (`cancel_turn` + `soft_delete` route through the helper; `post_turn` error branches → `HTTPException` with cleanup-before-raise).
- Modify: `src/call_manager/main.py` (exception handler: `HX-Request` non-boosted → JSON, component L).
- Modify: `src/call_manager/web/templates/proposal_assist/partials/chat_turn.html` (`turn_outcome` branch); `workspace_main.html` (composer reset gate); `src/call_manager/web/static/app.css` (`.turn-outcome` rule).
- Add tests: `tests/proposal_assist/test_turn_outcomes.py` (+ extend reaper / orphan / soft-delete / cancel tests; keep `test_global_cap.py` green).
