# Cockpit Cutover Slice D — Settings + Collaborators (Design Spec)

**Status:** design approved 2026-06-24 (placement option B + the shape below).
**Sub-project:** Full-React migration → cockpit-cutover. Builds on merged Slices A (front-door), B (chat), C (refs + media). Both UIs coexist until Slice G.

## 1. Goal

Bring **workspace settings** (title / status / deadline / citation-style) and **collaborator management** (add/remove members) into the React cockpit, at parity with the Jinja `workspace_settings.html` page, and fix the chat-footer gate-message conflation carried over from Slice B. All changes are **additive `/api` + React**; **no DB schema change**, **zero Jinja routes touched** (the Jinja settings/viewer routes are deleted at Slice G).

## 2. Scope

**In:**
- Owner-editable settings: `title`, `status`, `deadline`, `citation_style` — via a `GET`+`PATCH` `/api/workspaces/{id}/settings` pair and a `WorkspaceSettingsModal` (gear in the TitleBar, owner-only trigger).
- Collaborator management: add by email / remove — via `POST`/`DELETE` `/api/workspaces/{id}/members*`, surfaced as owner-only add/remove controls **inside the existing Members sidebar group** (Phase 3b). Reuses the existing owner-gated `GET /api/workspaces/{id}/members`.
- **Slice-B fast-follow:** add `is_owner` + `disabled_reason` to `TurnsResponse`; drive the chat footer from `disabled_reason` (fixes the unreachable "drafting disabled" branch).
- **Audit:** write `member_added` / `member_removed` `WorkspaceEvent`s and surface them in the Activity feed **owner-only** (see §7 PII invariant).

**Out (YAGNI / no Jinja parity):**
- Per-workspace drafting toggle — `drafting_enabled` is a **global** config kill-switch (`config.py:95`), not a workspace setting. Not exposed here.
- Hard delete / archive of a workspace (`soft_deleted_at` exists but has no Jinja UI). Status `abandoned` is the existing soft-close, reachable via the `status` field.
- Audit of settings-field edits (noisy, low value, Jinja writes none).
- Role changes / invites / pending-invite flow (the model is binary owner vs viewer; add is immediate).
- Any `/api` binary serve (Slice G owns those).

## 3. Reuse map (exact, grounded)

- **Jinja settings handler** `proposal_assist/web/workspaces.py`: `post_settings()` (:1263, `@_require_owner`), validates `title` (maxlen 500, :1107), `status` against `_WORKSPACE_STATUSES` (:102 = `{drafting, submitted, awarded, rejected, abandoned}`), `citation_style` against `_CITATION_STYLES` (:104 = `{vancouver, author-year}`), `deadline` via `_parse_deadline_form()` (:112). **Import and reuse** `_WORKSPACE_STATUSES`, `_CITATION_STYLES`, `_parse_deadline_form` (module-level constants/fn) — same boundary-reuse principle as Slices C-refs/C-media; do **not** extract a new shared helper, do **not** touch the Jinja handler.
- **Jinja viewer handlers** `proposal_assist/web/workspaces.py`: `add_viewer()` (:1205, `@_require_owner`, `email` lookup `select(User).where(User.email==email)`, generic 400 on not-found to prevent enumeration, skip-if-owner, dedupe existing `WorkspaceViewer`), `remove_viewer()` (:1239, by `user_id`). These are **inline** (no service fn) → the `/api` handlers inline the equivalent orchestration (mirroring them). Short-lived duplication, resolved at Slice G.
- **Models** `db/models.py`: `Workspace` (`title`:612, `status`:633 CHECK, `deadline`:638, `citation_style`:724 CHECK, `current_phase`:657, `owner_id`:613); `WorkspaceViewer` (:731 — `workspace_id`, `user_id`, `added_at`); `WorkspaceEvent` (:751 — `kind`, `payload` JSONB, `actor_id` nullable, `created_at`). **No new columns, no migration.**
- **Existing `/api` read-models** `web/api/workspaces.py`: `GET .../cockpit-meta` (`get_cockpit_meta` :832, member-gated → `CockpitMetaOut{workspace_title, workspace_deadline, current_phase, role, spec_locked, member_count}`); `GET .../members` (`list_members` :864, **owner-gated** → `list[MemberOut{email, role}]`).
- **TurnsResponse** `web/api/turns.py` (:137): `{turns, can_send, needs_disclosure, phase, running_job_id}`; `can_send = is_owner and current_phase=="active" and drafting_on` (:178-180). Canonical gate order (`_cloud_gate.gate_cloud_llm` :17): setup → drafting-disabled → disclosure.
- **Activity feed** `proposal_assist/activity/feed.py`: pure `build_activity_feed(events, actor_names)` (:144) with `_DROP` (:20) + `_RENDER` (:63) tables; `actor_user_id()` (:86) already reads a per-kind payload fallback. The member-gated `/api` activity endpoint resolves the events + actor names and calls it.
- **Frontend** `frontend/src/api/workspace.ts` (`useCockpitMeta`, `useMembers` no-retry-on-403); `CockpitSidebar.tsx` Members group (:125-140, owner→list / non-owner→count fallback); `CockpitChat.tsx` footer gate (:64-93); `api/turns.ts` `useTurns` → `TurnsResponse`; TitleBar (Phase-3b breadcrumb) for the gear trigger; `MetadataModal` (Mantine Modal) as the modal precedent.

## 4. Backend — endpoints (all additive, in `web/api/`)

New module `web/api/settings.py` (settings + members write surface), mounted via `web/api/__init__.py` after `files`. (Rationale: keep `workspaces.py` from growing further; the read-models stay where they are.)

### 4.1 `GET /api/workspaces/{id}/settings` — owner
`require_owner`. Returns `SettingsOut`:
```
SettingsOut { title: str, status: str, deadline: str | None, citation_style: str }
```
`deadline` is the date portion ISO string (`ws.deadline.date().isoformat()`) or `null`, matching the `<input type=date>`. Owner-only is fine: non-owners never see the modal trigger.

### 4.2 `PATCH /api/workspaces/{id}/settings` — owner
`require_owner`. Body `SettingsUpdateIn` (all four present — the modal is always fully populated; this is a full replace, not sparse-patch):
```
SettingsUpdateIn { title: str, status: str, deadline: str | None, citation_style: str }
```
Validation (reuse the imported Jinja constants/fn): `title` non-empty + ≤500; `status ∈ _WORKSPACE_STATUSES`; `citation_style ∈ _CITATION_STYLES`; `deadline` parsed via `_parse_deadline_form` (None clears it). On any violation → `HTTPException(400)` (envelope `invalid`). Assign the four fields, commit, return the updated `SettingsOut`. **No `WorkspaceEvent`** (parity). No side effects from a `status` change (same as Jinja).

### 4.3 `POST /api/workspaces/{id}/members` — owner
`require_owner`. Body `{ email: str }`. Inline the Jinja `add_viewer` orchestration:
- Look up `User` by exact email. **Not found → `HTTPException(400)`** with a generic message (`"could not add collaborator"`) — enumeration-safe (does not reveal whether the email exists).
- Target **is the owner** → idempotent no-op, `200` returning the owner `MemberOut`.
- Target **already a viewer** → idempotent no-op, `200` returning the existing `MemberOut`.
- Otherwise insert `WorkspaceViewer(workspace_id, user_id)`, write `WorkspaceEvent(kind="member_added", actor_id=owner.id, payload={"target_user_id": target.id, "target_email": target.email})`, commit, `201` returning `MemberOut{email, role:"member"}`.

### 4.4 `DELETE /api/workspaces/{id}/members/{user_id}` — owner
`require_owner`. Delete the `WorkspaceViewer(workspace_id, user_id)` row if present. If a row was deleted, write `WorkspaceEvent(kind="member_removed", actor_id=owner.id, payload={"target_user_id": user_id, "target_email": <resolved email or null>})`. Idempotent: **`204`** whether or not a row existed (removing the owner's id or a non-member → 0 rows → still 204, no event). (Resolve the email from the `User` row before deleting for the audit payload; if the user no longer exists, store `null`.)

### 4.5 `TurnsResponse` extension (`web/api/turns.py`)
Add two fields (additive):
```
is_owner: bool
disabled_reason: str | None   # "not_owner" | "setup" | "drafting_disabled" | "needs_disclosure" | null
```
Compute, preserving the existing `can_send`:
```
is_owner = ws.owner_id == user.id
if not is_owner:                 disabled_reason = "not_owner"
elif ws.current_phase=="setup":  disabled_reason = "setup"
elif not drafting_on:            disabled_reason = "drafting_disabled"
elif needs_disclosure:           disabled_reason = "needs_disclosure"
else:                            disabled_reason = None
# can_send unchanged: is_owner and current_phase=="active" and drafting_on
```
Order matches `gate_cloud_llm` (setup→drafting→disclosure) with `not_owner` prepended (submit is owner-only). `can_send` retained for back-compat; `disabled_reason` is the new authoritative footer driver.

## 5. Permissions (mirror Jinja exactly)

| Endpoint | Gate |
|---|---|
| `GET/PATCH .../settings` | `require_owner` |
| `POST .../members`, `DELETE .../members/{uid}` | `require_owner` |
| `GET .../members` (existing) | `require_owner` |
| `GET .../cockpit-meta` (existing) | `require_member` |
| `GET .../turns` (existing) | `require_member` |

## 6. Frontend

- **`frontend/src/api/settings.ts`** (new): `useSettings(id)` (`["settings", id]`, owner-only — only enabled when role==="owner"), `useUpdateSettings(id)` (PATCH, invalidates `["settings", id]` + `["cockpit-meta", id]` since title/deadline are shown elsewhere), `useAddMember(id)` (POST, invalidates `["members", id]` + `["cockpit-meta", id]` for `member_count`), `useRemoveMember(id)` (DELETE, same invalidations). All via `openapi-fetch` + `unwrap(await …)`.
- **`WorkspaceSettingsModal`** (new, Mantine `Modal`): title text input, `status`/`citation_style` `Select`s (options from the allowlists), `deadline` date input (clearable). Save → `useUpdateSettings`; surfaces a 400 message; closes on success. Trigger = a gear button in the **TitleBar**, rendered only when `cockpit-meta.role === "owner"`.
- **Members sidebar group** (`CockpitSidebar.tsx`): when the owner's `memberRows` is present, render per-row a remove control (`× / Remove <email>`, confirm-gated) and an "add by email" input + button at the foot of the group. Non-owners are unchanged (count fallback, no controls). Wire to `useAddMember`/`useRemoveMember`. The add-by-email 400 (`could not add collaborator`) renders inline near the input.
- **Chat footer** (`CockpitChat.tsx`): replace the inferred branch chain with a switch on `disabled_reason`:
  - `not_owner` → "Read-only — only the owner can message the agent."
  - `setup` → "Finish setup — review the rubric in the legacy workspace before drafting."
  - `drafting_disabled` → "Drafting is currently disabled."
  - `needs_disclosure` → the existing disclosure-ack UI.
  - `null` → the `Composer`.
  Keep `can_send` as a fallback for older cached payloads (defensive), but prefer `disabled_reason`.

No `routes.gen.ts` change (modal + sidebar + footer are all in-cockpit). OpenAPI regen required (additive): `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`.

## 7. Audit + the owner-only PII invariant (critical)

Collaborator emails are **owner-gated** — `GET /api/members` is `require_owner` precisely so non-owner members never see each other's emails. The Activity feed is **member-readable**. Therefore the `member_added`/`member_removed` events **MUST NOT** render their `target_email` to non-owner viewers.

Design:
- The events store `target_user_id` + `target_email` in `payload` (durable audit record).
- The **member-gated `/api` activity handler** drops `member_added`/`member_removed` events from the event list **when the requester is not the owner** (the handler already knows `ws.owner_id` and `user`), *before* calling `build_activity_feed`. This keeps `feed.py` role-agnostic and pure.
- `feed.py` gains `_RENDER` entries for the two kinds that render the collaborator email from `payload.target_email` (e.g. "Added collaborator alice@ki.se" / "Removed collaborator alice@ki.se"). These are the first payload-text-interpolating renders; extend the render path minimally to read `e.payload` for these kinds (the feed already reads payload for `actor_user_id`'s fallback). Actor (the owner) resolves normally.

Net: owners see collaborator-change history with emails; non-owners never see these entries at all. **This invariant is the security-sensitive part of the slice and must be covered by a test** (a non-owner member's `/activity` response excludes `member_added`/`member_removed`).

## 8. Testing

**Backend (targeted):** new `tests/test_api_settings.py` — settings GET/PATCH (owner 200; member/stranger 403; bad status/citation_style/oversize-title/bad-deadline → 400; deadline clear); members POST (owner add → 201 + `WorkspaceEvent(member_added)`; not-found email → generic 400; owner/duplicate → idempotent 200; member/stranger → 403); members DELETE (owner → 204 + `member_removed`; idempotent 204 on missing; member/stranger 403). Extend `tests/test_api_turns.py` — `disabled_reason` for each branch (not_owner / setup / drafting_disabled / needs_disclosure / null) + `is_owner`. **Activity PII test** — a non-owner member's `/activity` excludes the two collaborator kinds; an owner's includes them with the email. Regression: `proposal_assist -k "settings or viewer or activity"` (Jinja untouched).

**Frontend:** `settings.ts` hook tests; `WorkspaceSettingsModal` (renders current values, Save calls PATCH, 400 surfaces); Members group owner add/remove (calls hooks, confirm-gated remove, owner-only controls); footer switch (each `disabled_reason` renders the right message; `null` → Composer). `tsc` + `pnpm build`.

## 9. Global constraints

- No DB schema change / no alembic migration. Zero Jinja routes touched. No new shared helper extracted (reuse the Jinja module constants/`_parse_deadline_form` + inline the orchestration).
- New `/api` module `web/api/settings.py` mounted via `web/api/__init__.py` (not appended to `workspaces.py`).
- Permissions mirror Jinja: settings + member writes owner-gated.
- The owner-only Activity PII invariant (§7) is mandatory and tested.
- OpenAPI regen additive (`EMBEDDING_MODEL=bge-m3`); no `routes.gen.ts` change.
- Backend tests targeted only; `ruff` touched files. Frontend `tsc` is the gate.

## 10. Slice G forward-deps (carry to the G checklist)

- Delete the Jinja settings/viewer routes + `workspace_settings.html` once this lands and nav flips.
- (Already tracked) `/api/.../references/{id}/raw` + `/api/.../files/{aid}` binary serves.
