# Cockpit Slice E — Rubric Lifecycle + Setup→Active Gate (Design Spec)

**Date:** 2026-06-25
**Status:** Design — pending Codex review, then implementation plan.
**Sub-project:** Full-React migration → cockpit-cutover, Slice E (after A front-door, B chat, C refs+media, D settings+collaborators).

## 1. Goal

Bring the **funder-rubric review and the `setup → active` activation gate** into the React cockpit, and — because the backend already supports it cleanly — make the rubric **editable for the life of the workspace**, not only during the setup window. Retire the Jinja rubric-review + setup-status routes/templates it replaces (per-slice teardown; the 2026-06-25 "Jinja need not be preserved" directive).

This removes the Slice-A interim that deep-links setup-phase workspaces to the Jinja `/workspaces/{id}` page.

## 2. Principle (why this is not a 1:1 port)

A straight port would carry forward the Jinja UI's two real defects:

1. **Weights look absolute but are relative.** The evaluator normalizes weights at scoring time — `compute_overall_band` divides by `sum(weights)` (`src/call_manager/proposal_assist/evaluator/scoring.py:31`), and the gap-floor threshold `GAP_FLOOR_NORM_WEIGHT = 0.25` is applied to the *normalized* weight (`scoring.py:36`). So `weight` is a **relative priority**, yet the Jinja form presents a bare `0–1` field with no indication of that. The principled fix is **honesty, not a fake constraint**: show a live normalized-percentage readout; do **not** invent a sum-to-1.0 rule (the system already normalizes any positive sum).
2. **The rubric is frozen after activation.** The Jinja flow only exposes rubric editing during `waiting_for_rubric_review`. But the rubric is the backbone read live by the drafting agent (`tools/context.py:37-38`), the coherence-keeper (`coherence/context.py:43-44`), persona review (`review/context.py:38-39`), and the evaluator (`evaluator/context.py:87-88`, pinned per-run). A mis-extracted criterion therefore silently degrades all of them with no way to fix it. Post-activation editing is **design-clean already**: editing bumps `rubric_updated_at` (`web/rubric_review.py:117-122`), which the existing staleness machinery (`web/evaluator.py:88`, `problems/aggregate.py:117`) uses to flag scorecards stale → re-run. No scoring is corrupted; the old scorecard is simply marked stale.

## 3. Current state (grounding, with citations)

- **Phase model:** `Workspace.current_phase` TEXT, `CHECK IN ('setup','active')`, default `'setup'` (`db/models.py:592`, `:660`). New workspaces are created in `setup` and enqueue a `WorkspaceSetupJob` (`workspaces.py:24-62`).
- **Setup job:** `WorkspaceSetupJob` (`db/models.py:1352-1392`), statuses `queued → running → waiting_for_rubric_review → done` (or `failed`). Phase-0 worker extracts the rubric (`phases/setup.py:326-360`).
- **Rubric storage (JSONB on the workspace, no separate table):** `rubric_criteria` (list of `{name, description, weight: float, scale_max: int}`), `rubric_funder_notes` (TEXT, nullable), `rubric_updated_at` (DateTime, NOT NULL, server-default now) — `db/models.py:686-696`. Initialized in Phase 0 from the call's `CallRubric` snapshot (`phases/setup.py:344-346`).
- **Activation (Jinja, to be replaced):** `POST /workspaces/{id}/rubric-review` → `post_rubric_review` (`proposal_assist/web/rubric_review.py:97-171`). Owner-only; requires the setup job in `waiting_for_rubric_review`; writes `WorkspaceEvent(kind="rubric_edited")` iff content changed (`:117-135`), then `job.status="done"` + `job.completed_at` + `ws.current_phase="active"` in one commit (`:145-149`). No spec/disclosure precondition.
- **Setup card (Jinja, to be replaced):** `GET /workspaces/{id}/setup-status` → HTMX partial polled every 3s (`workspaces.py:652-680`, `templates/proposal_assist/partials/setup_status.html`). Owner sees a "Review the rubric" button; viewers see "Waiting for owner…".
- **Existing React read-side:** `GET /api/workspaces/{id}/cockpit-meta` → `CockpitMetaOut{workspace_title, workspace_deadline, current_phase, role, spec_locked, member_count}` (`web/api/workspaces.py:823-856`, member-gated). The cockpit **Criteria** sidebar group is read-only and scorecard-derived (`frontend/src/cockpit/CockpitSidebar.tsx:100-116`), not the raw rubric.
- **Slice-A interim:** `frontend/src/routes/WorkspaceList.tsx:18-34` routes `setup`-phase rows to a full-page `<a href="/workspaces/{id}">` (Jinja); `active` rows to React `<Link to="/cockpit/{id}">`.
- **Permissions helpers:** `require_owner` / `require_member(*, workspace_id, user, session) -> Workspace` at `proposal_assist/web/_perms.py` (keyword-only, return the `Workspace`, filter `soft_deleted_at IS NULL`) — the same guards Slice D's `web/api/settings.py` uses.

## 4. Design

### 4.1 Architecture — phase-branched cockpit, one URL

The cockpit route component (`frontend/src/routes/Cockpit.tsx`) reads `useCockpitMeta(id)` and branches:
- `current_phase === "setup"` → render `<WorkspaceSetup id={id} />` (full main area; no editor/tabs — a setup workspace has no draft repo yet).
- otherwise → today's cockpit, unchanged.

`/cockpit/:id` stays the single canonical URL across the whole lifecycle. No `/setup` sub-route, no Jinja deep-link. On activation the same URL re-renders as the live cockpit (cockpit-meta invalidation flips the branch).

### 4.2 Backend — new `src/call_manager/web/api/rubric.py` (additive, NO schema change)

Owner-gated except setup-status (member). All write paths funnel through one shared internal saver so the edit semantics are identical everywhere (DRY):

```
_apply_rubric(session, ws, criteria, funder_notes) -> None
    # mirrors web/rubric_review.py:117-122
    content_changed = (new != old)
    ws.rubric_criteria = criteria ; ws.rubric_funder_notes = funder_notes
    if content_changed:
        ws.rubric_updated_at = now(UTC)
        session.add(WorkspaceEvent(kind="rubric_edited", actor_id=user.id,
                    payload={... old/new summary ...}))   # parity with Jinja payload
```

Endpoints:

| Method & path | Auth | Body | Behavior | Returns |
|---|---|---|---|---|
| `GET /api/workspaces/{id}/rubric` | owner | — | Read the raw rubric | `RubricOut{criteria:[CriterionOut], funder_notes: str\|None, updated_at: datetime}` |
| `GET /api/workspaces/{id}/setup-status` | member | — | Latest `WorkspaceSetupJob.status` for polling | `SetupStatusOut{status: str}` (`queued\|running\|waiting_for_rubric_review\|done\|failed`); `done` if no job row |
| `POST /api/workspaces/{id}/activate` | owner | `RubricUpdateIn` | `_apply_rubric(...)` then advance: require the setup job `waiting_for_rubric_review` else **409**; set `job.status="done"`, `job.completed_at`, `ws.current_phase="active"`; emit `WorkspaceEvent(kind="workspace_activated")`; one commit | `RubricOut` (caller invalidates cockpit-meta/turns) |
| `PUT /api/workspaces/{id}/rubric` | owner | `RubricUpdateIn` | `_apply_rubric(...)` only (the post-activation edit path; also usable in setup as save-without-activate); no phase change | `RubricOut` |

**Validation (`CriterionIn` Pydantic):** `name: str` (non-empty after strip), `description: str` (may be empty), `weight: float >= 0` (relative — **no upper cap, no sum rule**; the system normalizes), `scale_max: int` in `1..10`. `funder_notes: str | None`. Invalid → **400** `bad_request` (the `web/api/errors.py:57` envelope). `criteria` may be any length (parity — no hard minimum).

**Idempotency / edge cases:** `POST /activate` on an already-`active` workspace → **409** (job not `waiting_for_rubric_review`). `PUT /rubric` in `setup` is allowed (saves without activating). Cross-workspace / missing → `require_*` already yields 404/403.

Mount in `web/api/__init__.py` after `settings` (the Slice-D module).

### 4.3 Activation audit + Activity feed

`POST /activate` emits `WorkspaceEvent(kind="workspace_activated", actor_id=owner)`, payload `{}` (no PII). Add a `_RENDER` entry in `proposal_assist/activity/feed.py` mapping `workspace_activated` to the **existing** `"rubric"` category (it is part of the rubric/setup flow) with summary `"activated the workspace"`. **No `ActivityRowOut.category` Literal change** (`"rubric"` already exists) and no owner-only filter (no PII) — distinct from Slice D's `member` rows.

### 4.4 Frontend

- **`api/rubric.ts`:** `useRubric(id)` (owner; `["rubric", id]`, `retry:false`), `useSetupStatus(id)` (`["setup-status", id]`, `refetchInterval` while `status ∈ {queued,running}`, stop once `waiting_for_rubric_review`/terminal), `useActivate(id)` (POST → invalidate `["cockpit-meta",id]`, `["turns",id]`, `["setup-status",id]`), `useUpdateRubric(id)` (PUT → invalidate `["rubric",id]`, `["scorecard",id]`, `["problems",id]` so staleness surfaces).
- **`cockpit/RubricEditor.tsx`:** shared editable criteria table — per row: name, description, weight (number, min 0, step 0.01), scale_max (1–10), plus a **live normalized-weight %** column (`weight / Σweight`) and a funder-notes textarea. Props: `{ criteria, funderNotes, onChange }` controlled, plus a render-agnostic save button supplied by the parent (so the same editor serves both the setup "Confirm & activate" and the post-activation "Save").
- **`cockpit/WorkspaceSetup.tsx`:** the setup-phase main view. Polls `useSetupStatus`. States: progress (`queued`/`running`), `RubricEditor` + **"Confirm & activate"** (owner, `waiting_for_rubric_review`) calling `useActivate`, viewer "Waiting…" message, and a `failed` error panel (parity — no retry).
- **Post-activation editing:** an owner-only **"Edit rubric"** affordance on the existing **Criteria** sidebar group header (`CockpitSidebar.tsx`) opens `RubricEditor` inside a Mantine `Modal` (reuse the `WorkspaceSettingsModal.tsx` pattern: gate mount on `isOwner && open` so provider-less tests stay green) wired to `useUpdateRubric`. Saving bumps staleness → the scorecard/problems surfaces already show "rubric changed".
- **`routes/Cockpit.tsx`:** the `current_phase` branch (4.1).
- **`routes/WorkspaceList.tsx:18-34`:** setup rows switch from the Jinja `<a href="/workspaces/{id}">` to React `<Link to="/cockpit/{id}">`; drop the interim comment. Keep the "Setting up" badge.

### 4.5 Per-slice Jinja teardown

Once the React path is verified, **delete**: `proposal_assist/web/rubric_review.py` (the GET+POST routes + its router registration), the `GET /workspaces/{id}/setup-status` route in `proposal_assist/web/workspaces.py:652-680`, and the templates `templates/proposal_assist/rubric_review.html` + `templates/proposal_assist/partials/setup_status.html`, plus their references in `workspace_main.html`. Grep first to confirm nothing else imports them. The broader Jinja `workspace_main` two-pane deletion remains a later, separate cutover (not this slice).

## 5. Permissions matrix

| Action | Owner | Member (viewer) | Stranger |
|---|---|---|---|
| GET rubric | ✓ | 403 | 403 |
| GET setup-status | ✓ | ✓ | 403 |
| POST activate | ✓ | 403 | 403 |
| PUT rubric | ✓ | 403 | 403 |

Parity with Jinja: the raw rubric is owner-only; viewers see the scorecard-derived Criteria group they already have, plus the setup waiting state.

## 6. Out of scope (YAGNI)

Weight sum/normalization **enforcement** (only surfaced); criteria add/remove/reorder beyond what the editor naturally needs; new activation preconditions (disclosure stays gated at turn-submit, `web/_cloud_gate.py`); retry-failed-setup; viewer-editable or viewer-visible raw rubric; any DB schema/migration; the broader Jinja `workspace_main` deletion; `scale_max` semantics changes (kept editable for parity though unused by `compute_overall_band`).

## 7. Testing strategy

- **Backend (`tests/test_api_rubric.py`, new):** GET rubric owner-200/member-403; setup-status member-200 (status value) + owner-200; PUT rubric saves + bumps `rubric_updated_at` + writes `rubric_edited` only on change + owner-gating; POST activate from `waiting_for_rubric_review` → `current_phase=="active"` + job `done` + `workspace_activated` event, and 409 when the job is not waiting / already active; 400 on bad criterion (empty name, negative weight, scale_max out of 1..10). Harness mirrors `tests/test_api_settings.py` (`build_app`/`authed_client`/`make_user`/`make_workspace`) — note the setup job must exist in `waiting_for_rubric_review` for the activate-happy-path (seed it).
- **Activity (`tests/proposal_assist/activity/test_feed.py`):** `workspace_activated` renders under category `"rubric"` with summary "activated the workspace".
- **Frontend (vitest):** `RubricEditor` renders rows + the normalized-% readout updates on weight change; `WorkspaceSetup` shows progress vs rubric-editor vs waiting by status; `useActivate`/`useUpdateRubric` POST/PUT and invalidate; the Criteria-group "Edit rubric" modal (owner-only, `renderWithProviders`); `Cockpit` route branches on phase; `WorkspaceList` setup row links to `/cockpit/:id`.
- **Regression:** OpenAPI regen additive (`EMBEDDING_MODEL=bge-m3`), `routes.gen.ts` empty diff; after teardown, the deleted Jinja routes' tests are removed and `proposal_assist` targeted suites stay green.

## 8. Risks / open points

- **Stale scorecard after a post-activation rubric edit** references old criterion names in its frozen JSONB. The existing staleness banner already prompts a re-run; we surface that, we do not re-score. Acceptable (documented in §2).
- **Setup polling cadence:** ~2–3s while `queued`/`running` (matching the Jinja 3s), stopping at `waiting_for_rubric_review`/terminal — avoid a hot loop.
- **`POST /activate` carries the rubric body** (save-then-activate atomic, mirroring the Jinja single POST) rather than requiring a prior `PUT`; if the owner made no edits the client just sends the current rubric back (`_apply_rubric` no-ops the event).
