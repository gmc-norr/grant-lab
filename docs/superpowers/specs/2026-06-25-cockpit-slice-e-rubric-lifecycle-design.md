# Cockpit Slice E — Rubric Lifecycle + Setup→Active Gate (Design Spec)

**Date:** 2026-06-25
**Status:** Design — revised after external review (2026-06-25); pending implementation plan.
**Sub-project:** Full-React migration → cockpit-cutover, Slice E (after A front-door, B chat, C refs+media, D settings+collaborators).

## 1. Goal

Bring the **funder-rubric review and the `setup → active` activation gate** into the React cockpit, and — because the backend already supports it cleanly — make the rubric **editable for the life of the workspace**, not only during the setup window. Retire the Jinja rubric-review + setup-status routes/templates it replaces (per-slice teardown; the 2026-06-25 "Jinja need not be preserved" directive).

This removes the Slice-A interim that hands setup-phase workspaces to the Jinja `/workspaces/{id}` page from **both** entry points: the from-call **create** flow (`WorkspaceCreate.tsx`) and the **list** rows (`WorkspaceList.tsx`).

## 2. Principle (why this is not a 1:1 port)

A straight port would carry forward the Jinja UI's two real defects:

1. **Weights look absolute but are relative.** The evaluator normalizes weights at scoring time — `compute_overall_band` divides by `sum(weights)` (`src/call_manager/proposal_assist/evaluator/scoring.py:31`), and the gap-floor threshold `GAP_FLOOR_NORM_WEIGHT = 0.25` (defined `scoring.py:15`) is applied to the *normalized* weight (`scoring.py:36`). So `weight` is a **relative priority**, yet the Jinja form presents a bare `0–1` field with no indication of that. The principled fix is **honesty, not a fake constraint**: show a live normalized-percentage readout; do **not** invent a sum-to-1.0 rule (the system already normalizes any positive sum).
2. **The rubric is frozen after activation.** The Jinja flow only exposes rubric editing during `waiting_for_rubric_review`. But the rubric is the backbone read live by the drafting agent (`tools/context.py:37-38`), the coherence-keeper (`coherence/context.py:43-44`), persona review (`review/context.py:38-39`), and the evaluator (`evaluator/context.py:87-88`, pinned per-run). A mis-extracted criterion therefore silently degrades all of them with no way to fix it. Post-activation editing is **design-clean already**: editing bumps `rubric_updated_at`, which the existing staleness machinery (`web/evaluator.py:88`, `problems/aggregate.py:117`) uses to flag scorecards stale → re-run. No scoring is corrupted; the old scorecard is simply marked stale.

## 3. Current state (grounding, with citations)

- **Phase model:** `Workspace.current_phase` TEXT, `CHECK IN ('setup','active')`, default `'setup'` (`db/models.py:592`, `:660`). New workspaces are created in `setup` and enqueue a `WorkspaceSetupJob` (`workspaces.py:24-62`).
- **Setup job + repo:** `WorkspaceSetupJob` (`db/models.py:1352-1392`), statuses `queued → running → waiting_for_rubric_review → done` (or `failed`). The Phase-0 worker `_run_steps` (`phases/setup.py:362-374`) **git-inits the proposal repo first** (`_snapshot_and_init_repo`, step 1), seeds section extracts + rubric, and only **then** sets `waiting_for_rubric_review`. So by the time the owner reviews, the repo exists (with empty/sparse `proposal.md`/`proposal-spec.md`) — there is no "no repo yet" state at review time.
- **Rubric storage (JSONB on the workspace, no separate table):** `rubric_criteria` (list of dicts), `rubric_funder_notes` (TEXT, nullable), `rubric_updated_at` (DateTime, NOT NULL, server-default now) — `db/models.py:686-696`. Initialized in Phase 0 from the call's `CallRubric` snapshot (`phases/setup.py:344-346`).
- **Criterion shape:** the extractor (`extractors/rubric.py:29`) emits `{name, description, weight: float, scale_max: int, subcriteria?: list}` — **`subcriteria` is an optional field the evaluator renders** (`evaluator/context.py:74`). The Jinja form (`rubric_review.py:61-90`) only round-trips the four scalar fields and **silently drops `subcriteria` on every save** (a pre-existing Jinja data-loss bug — see §4.2).
- **Activation (Jinja, to be replaced):** `POST /workspaces/{id}/rubric-review` → `post_rubric_review` (`proposal_assist/web/rubric_review.py:97-149`). Owner-only; **enforces ≥1 criterion** (`rubric_review.py:88-89`, 400 otherwise); writes `WorkspaceEvent(kind="rubric_edited")` iff content changed (`:117-135`) — **`actor_id` is left NULL**, the editor is recorded in `payload.edited_by_user_id`, and the payload carries the **full** `old_criteria`/`new_criteria`/`old_funder_notes`/`new_funder_notes` (forward-replay audit contract). Then `job.status="done"` + `job.completed_at` + `ws.current_phase="active"` in one commit (`:144-149`). No spec/disclosure precondition. Activation itself is currently **unaudited** (no distinct event).
- **Setup card (Jinja, to be replaced):** `GET /workspaces/{id}/setup-status` → HTMX partial polled every 3s (`workspaces.py:652-680`, `templates/proposal_assist/partials/setup_status.html`). Polling is **role-aware**: owner polls during `queued`/`running` and stops at `waiting_for_rubric_review` (they have the action); **viewers keep polling through `waiting`** to catch the owner's confirm. The card has explicit `done` and `failed` states; `failed` shows `setup_last_error` to the owner only. **A missing job row reports `queued`** (`workspaces.py:676`), not `done`.
- **Existing React read-side:** `GET /api/workspaces/{id}/cockpit-meta` → `CockpitMetaOut{workspace_title, workspace_deadline, current_phase, role, spec_locked, member_count}` (`web/api/workspaces.py:823-856`, member-gated). The cockpit **Criteria** sidebar group is read-only and scorecard-derived (`frontend/src/cockpit/CockpitSidebar.tsx:100-116`), not the raw rubric.
- **Shell mounts the panes from the route handle (independent of phase):** `router.tsx:24` mounts `cockpit/:id` with `handle: { Sidebar: CockpitSidebar, RightPane: CockpitChat }`. `AppShell` renders those panes regardless of what `<Cockpit/>` renders in the main column — so a setup-phase cockpit **co-renders the full sidebar + chat** (see §4.1).
- **Slice-A interim (both entry points):** `WorkspaceCreate.tsx:39-40` hard-navigates **every newly-created** workspace via `window.location.assign('/workspaces/{id}')` (the *primary* path into setup); `WorkspaceList.tsx:18-34` routes `setup`-phase rows to a full-page `<a href="/workspaces/{id}">`. `active` rows already use React `<Link to="/cockpit/{id}">`.
- **Permissions helpers:** `require_owner` / `require_member(*, workspace_id, user, session) -> Workspace` at `proposal_assist/web/_perms.py` (keyword-only, return the `Workspace`, filter `soft_deleted_at IS NULL`).
- **400 envelope:** the global `StarletteHTTPException` handler maps `400 → "bad_request"` (`web/api/errors.py`); tests assert `400` / `bad_request`.

## 4. Design

### 4.1 Architecture — phase-branched main column + setup-aware shell panes

The cockpit route component (`frontend/src/routes/Cockpit.tsx`) gains a `useCockpitMeta(id)` dependency and branches the **main column**:
- meta **loading/undefined** → a neutral loading state (NOT the active cockpit — avoid an active-cockpit flash before the branch resolves).
- `current_phase === "setup"` → `<WorkspaceSetup id={id} />`.
- otherwise → today's cockpit, unchanged.

`/cockpit/:id` stays the single canonical URL across the whole lifecycle; on activation, invalidating `cockpit-meta` flips the branch in place. (`Cockpit.test.tsx` mocks must add `current_phase` to the cockpit-meta stub.)

**Shell panes co-render in setup** (`router.tsx:24`), so both must handle the setup phase explicitly rather than assume an active draft:
- **`CockpitChat` (RightPane):** when `phase === "setup"`, render a short placeholder ("The agent chat opens once the workspace is active.") instead of the composer/disabled-footer. Independently, **fix the existing setup copy** in `CockpitChat.tsx:88` — drop "review the rubric in the **legacy workspace**" (a Slice-D string that is wrong once setup lives in the cockpit); reword to point at this cockpit's setup view.
- **`CockpitSidebar`:** the draft/scorecard-dependent reads (`useDraft`/`useSpec`/`useScorecard`/`useVersions`) 404 during setup; give them `retry:false` (or a skip-4xx predicate) so a setup render doesn't retry each 3×. The groups show their existing empty states; the Members group is unaffected. The owner **"Edit rubric"** affordance (§4.4) is **active-phase only**.

### 4.2 Backend — new `src/call_manager/web/api/rubric.py` (additive, NO schema change)

Owner-gated except setup-status (member). All write paths funnel through one shared internal saver (DRY), which **takes the acting user** and preserves unknown criterion fields:

```
_apply_rubric(session, ws, user, criteria, funder_notes) -> None
    # criteria are dicts that PRESERVE passthrough fields (e.g. subcriteria).
    content_changed = (criteria != ws.rubric_criteria) or (funder_notes != (ws.rubric_funder_notes or ""))
    old_c, old_n = ws.rubric_criteria, ws.rubric_funder_notes or ""
    ws.rubric_criteria = criteria ; ws.rubric_funder_notes = funder_notes
    if content_changed:
        ws.rubric_updated_at = now(UTC)
        session.add(WorkspaceEvent(kind="rubric_edited", actor_id=user.id,
            payload={"old_criteria": old_c, "new_criteria": criteria,
                     "old_funder_notes": old_n, "new_funder_notes": funder_notes}))
```

> **Event-contract decision:** we set `actor_id=user.id` (cleaner than Jinja's `NULL` + `payload.edited_by_user_id`; the activity feed resolves actor from `actor_id` with a payload fallback). We **keep the full before/after payload** — it matters more now that the rubric is editable for life. State this divergence in the plan.

Endpoints:

| Method & path | Auth | Body | Behavior | Returns |
|---|---|---|---|---|
| `GET /api/workspaces/{id}/rubric` | owner | — | Read the raw rubric (criteria incl. passthrough fields) | `RubricOut{criteria:[CriterionOut], funder_notes: str\|None, updated_at: datetime}` |
| `GET /api/workspaces/{id}/setup-status` | member | — | Latest `WorkspaceSetupJob.status` for polling; **missing job row → `"queued"`** (parity); owner also gets `last_error` | `SetupStatusOut{status: str, last_error: str\|None}` (`last_error` populated for owner only) |
| `POST /api/workspaces/{id}/activate` | owner | `RubricUpdateIn` | `_apply_rubric(...)`, then re-read the setup job **`with_for_update()`**: require `waiting_for_rubric_review` else **409**; set `job.status="done"`, `job.completed_at`, `ws.current_phase="active"`; emit `WorkspaceEvent(kind="workspace_activated", actor_id=owner)`; one commit | `RubricOut` |
| `PUT /api/workspaces/{id}/rubric` | owner | `RubricUpdateIn` | `_apply_rubric(...)` only (post-activation edit path; also a save-without-activate during setup); no phase change | `RubricOut` |

**`RubricUpdateIn` / `CriterionIn` validation:** `criteria` is a **non-empty** list (parity — `rubric_review.py:88-89`; empty → **400** `bad_request`). Each `CriterionIn`: `name: str` (non-empty after strip), `description: str` (**required field, may be empty** — parity; Jinja requires presence, not non-emptiness), `weight: float >= 0` (relative — **no upper cap, no sum rule**), `scale_max: int` in `1..10`, plus an **optional `subcriteria` passthrough** carried verbatim (so it is not lost — closes the Jinja data-loss bug and keeps `_apply_rubric`'s `content_changed` honest on no-edit saves). Use `model_config = ConfigDict(extra="allow")` (or an explicit `subcriteria` field) so unknown criterion keys round-trip. Invalid → **400** `bad_request`.

**Edge cases:** `POST /activate` on an already-`active` workspace → **409** (job not `waiting_for_rubric_review`). `PUT /rubric` in `setup` is allowed. Concurrent double-activate is guarded by the `with_for_update()` job re-read (the loser sees `done` → 409), improving on the Jinja unlocked path. Cross-workspace / missing → `require_*` yields 404/403.

Mount in `web/api/__init__.py` after `settings`.

### 4.3 Activation audit + Activity feed

`POST /activate` emits `WorkspaceEvent(kind="workspace_activated", actor_id=owner, payload={})` — purely additive (Jinja leaves activation unaudited). Add a `_RENDER` entry in `proposal_assist/activity/feed.py` mapping `workspace_activated` → the **existing** `"rubric"` category, summary `"activated the workspace"`. The `ActivityRowOut.category` Literal already includes `"rubric"` (`proposal_assist/web/activity.py:48`) → **no Literal change, no migration**. No PII → no owner-only filter (unlike Slice D's `member` rows).

### 4.4 Frontend

- **`api/rubric.ts`:** `useRubric(id)` (owner; `["rubric", id]`, `retry:false`), `useSetupStatus(id, {phase, role})` (`["setup-status", id]`; **poll until terminal** so viewers auto-advance — `refetchInterval` while `status ∉ {done, failed}`; on `done`, invalidate `["cockpit-meta", id]` so the phase branch flips for the viewer too), `useActivate(id)` (POST → invalidate `["cockpit-meta",id]`, `["turns",id]`, `["setup-status",id]`), `useUpdateRubric(id)` (PUT → invalidate `["rubric",id]`, `["scorecard",id]`, `["problems",id]`).
- **`cockpit/RubricEditor.tsx`:** shared controlled editable criteria table — per row: name, description, weight (number, min 0, step 0.01), scale_max (1–10), a **live normalized-weight %** column (`weight / Σweight`), and a funder-notes textarea. **Preserves each criterion's `subcriteria` passthrough untouched** (edits only the four scalar fields). Props `{ criteria, funderNotes, onChange }`; the parent supplies the save button (so one editor serves both setup-activate and post-activation save).
- **`cockpit/WorkspaceSetup.tsx`:** the setup-phase main view. Polls `useSetupStatus`. States: `queued`/`running` → progress; `waiting_for_rubric_review` → owner sees `RubricEditor` + **"Confirm & activate"** (`useActivate`), viewer sees "Waiting for the owner to review the rubric…"; **`done` → brief "Opening cockpit…"** (cockpit-meta invalidation flips the branch); **`failed` → error panel** showing `last_error` for the owner (parity — no retry path).
- **Post-activation editing:** an owner-only **"Edit rubric"** control at the **Criteria sidebar-group header call site** in `CockpitSidebar.tsx` that **conditionally mounts** a Mantine `Modal` hosting `RubricEditor` (gate the mount with `isOwner && open`, mirroring how `TitleBar.tsx:41` gates `WorkspaceSettingsModal` so provider-less tests stay green) wired to `useUpdateRubric`. Saving bumps staleness → the scorecard/problems surfaces already show "rubric changed".
- **`routes/Cockpit.tsx`:** the loading/`setup`/active branch (§4.1).
- **Entry-point repoints (both):** `routes/WorkspaceCreate.tsx:31-41` — replace `window.location.assign('/workspaces/{id}')` with react-router `navigate('/cockpit/{id}')`; drop the "hand off to the Jinja flow" comments here and in `api/workspace.ts:64-66`; update `WorkspaceCreate.test.tsx` (the `/workspaces/new`-style assertion + any title check). `routes/WorkspaceList.tsx:18-34` — setup rows switch from `<a href="/workspaces/{id}">` to React `<Link to="/cockpit/{id}">`; keep the "Setting up" badge.

### 4.5 Per-slice Jinja teardown

Once the React path is verified, **delete**: `proposal_assist/web/rubric_review.py` (GET+POST routes + its router registration) and the `GET /workspaces/{id}/setup-status` route in `proposal_assist/web/workspaces.py:652-680`, plus the templates `rubric_review.html` and `partials/setup_status.html`. The Jinja `/workspaces/{id}` two-pane page is **kept** (broader cutover deferred) but is still reachable via backlinks (files/refs/settings/history), so its `current_phase=='setup'` branch must not break: **replace the deleted `setup_status` include in `workspace_main.html` with a static notice** ("This workspace is set up in the cockpit →" linking to `/cockpit/{id}`) and **stop computing `setup_status`/`setup_last_error` in `get_workspace_main`**. Do **not** let the setup branch fall through to the `{% elif is_owner %}` composer (its turn-submit is hard-409'd in setup → dead-end). Grep first to confirm nothing else imports the deleted symbols.

## 5. Permissions matrix

| Action | Owner | Member (viewer) | Stranger |
|---|---|---|---|
| GET rubric | ✓ | 403 | 403 |
| GET setup-status | ✓ (+`last_error`) | ✓ (no `last_error`) | 403 |
| POST activate | ✓ | 403 | 403 |
| PUT rubric | ✓ | 403 | 403 |

## 6. Out of scope (YAGNI)

Weight sum/normalization **enforcement** (only surfaced); criteria add/remove/reorder beyond what the editor needs; **editing `subcriteria`** (preserved, not edited); new activation preconditions (disclosure stays gated at turn-submit, `proposal_assist/web/_cloud_gate.py`); viewer-editable or viewer-visible raw rubric; any DB schema/migration; the broader Jinja `workspace_main` two-pane deletion; `scale_max` semantics changes (kept editable for parity though unused by `compute_overall_band`).

## 7. Testing strategy

- **Backend (`tests/test_api_rubric.py`, new):** GET rubric owner-200/member-403; setup-status member-200 (+ owner `last_error`, missing-row→`queued`); PUT rubric saves + bumps `rubric_updated_at` + writes `rubric_edited` only on change + **preserves `subcriteria`** + owner-gating; POST activate from `waiting_for_rubric_review` → `current_phase=="active"` + job `done` + `workspace_activated` event, **409** when not waiting / already active, and **no spurious `rubric_edited` on an unedited activate** (subcriteria round-trips); **400** on empty criteria list, empty name, negative weight, `scale_max` out of `1..10`. Harness mirrors `tests/test_api_settings.py` (`build_app`/`authed_client`/`make_user`/`make_workspace`); seed the setup job in `waiting_for_rubric_review` for the activate happy-path.
- **Activity (`tests/proposal_assist/activity/test_feed.py`):** `workspace_activated` renders under category `"rubric"`, summary "activated the workspace".
- **Test migration (NOT blanket deletion):** delete `tests/proposal_assist/test_rubric_review_endpoint.py` with the route. **Re-point** the create→activate→active coverage in `tests/proposal_assist/test_phase0_e2e.py` and `tests/proposal_assist/test_web_workspace_setup_gate.py` from `POST /workspaces/{id}/rubric-review` to `POST /api/workspaces/{id}/activate`. **Keep/split** the `workspace_main` + turn-409-in-setup assertions in `test_web_workspace_setup_gate.py` (they cover §4.5-retained behavior). Check `tests/proposal_assist/conftest.py` for shared activation helpers.
- **Frontend (vitest):** `RubricEditor` renders rows + normalized-% updates on weight change + preserves subcriteria on change; `WorkspaceSetup` shows progress/rubric-editor/waiting/**done**/**failed** by status; `useActivate`/`useUpdateRubric` POST/PUT + invalidate; viewer poll auto-advances on `done`; the Criteria-group "Edit rubric" modal (owner-only, `renderWithProviders`); `Cockpit` route loading→phase branch (mocks include `current_phase`); `WorkspaceCreate` navigates to `/cockpit/:id`; `WorkspaceList` setup row links to `/cockpit/:id`; `CockpitChat` setup placeholder + copy.
- **Regression:** OpenAPI regen additive (`EMBEDDING_MODEL=bge-m3`), `routes.gen.ts` empty diff; `proposal_assist` targeted suites green after teardown.

## 8. Risks / decisions pinned

- **Stale scorecard after a post-activation rubric edit** references old criterion names in its frozen JSONB. The existing staleness banner prompts a re-run; we surface it, we do not re-score (documented §2). Acceptable.
- **Setup polling cadence:** ~2–3s while non-terminal (matching Jinja's 3s), stopping at `done`/`failed`; viewers poll through `waiting` (role-aware) so activation auto-advances them.
- **`POST /activate` carries the rubric body** (save-then-activate atomic, mirroring the Jinja single POST). An unedited activate round-trips the current rubric including `subcriteria`, so `_apply_rubric` correctly no-ops the event (the subcriteria-preservation fix is what makes this true).
- **Concurrent double-activate** is guarded by the `with_for_update()` job re-read inside the activate transaction (improves on the pre-existing Jinja unlocked race).
- **`description` empty-allowed** is intentional parity (Jinja requires the field present, not non-empty); the editor surfaces it but does not force non-empty.
