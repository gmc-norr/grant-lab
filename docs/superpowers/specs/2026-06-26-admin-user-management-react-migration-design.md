# Admin user-management — React migration (Slice 1 of admin)

**Date:** 2026-06-26
**Status:** design approved, pre-plan
**Part of:** the full-React migration (strangler-fig, route-by-route). The admin
sub-project is split into **two slices** (see *Decomposition* below); this spec
covers **Slice 1 — user management**.

## Goal

Migrate the two admin user-management pages and their six mutations from
Jinja2+HTMX to the React SPA, as an additive-`/api` → React-route → Jinja-teardown
slice. Leave the rest of `/admin` (ops dashboard, drafting panel, golden-set,
ops actions) on Jinja for Slice 2.

## Decomposition (why two slices, why this one first)

Unlike `/profiles` — where the single manifest prefix greedily coupled all four
pages into one atomic slice — the admin surface has clean **sub-prefix seams**.
`is_spa_route(path)` matches a path *exactly or beneath* a registered prefix
(`path == r or path.startswith(r.rstrip("/") + "/")`), so registering
`/admin/users` as an SPA prefix captures `/admin/users` and
`/admin/users/pending` **but not** bare `/admin` or `/admin/golden-set`. That
lets the admin surface decompose, with one hard ordering constraint: **bare
`/admin` must be the last admin slice**, because registering it would subsume
every `/admin/*` sub-prefix.

- **Slice 1 (this spec):** user management (`/admin/users`, `/admin/users/pending`
  + 6 actions) — the cohesive, security-sensitive unit (password issuance,
  last-admin guards, session-epoch revocation), reviewed on its own.
- **Slice 2 (separate, later):** everything else under bare `/admin` — the ops
  dashboard, the drafting panel, golden-set CRUD, and the ops actions
  (rerank / digest send+reenable / source fetch). Registers the `/admin`
  prefix, wires the in-SPA admin nav, and finishes the admin Jinja teardown.

## Surface being migrated (Slice 1)

All under `/admin`, all admin-gated. Source: `web/routes/admin.py` (user-mgmt
functions only) + templates `admin_users.html`, `admin_users_pending.html`.

**Pages:**
- `GET /admin/users?page=N` → `admin_users.html` — active/disabled users,
  paginated (`per_page=50`, `status != "pending"`, ordered by email asc),
  with `total` and prev/next.
- `GET /admin/users/pending?page=N` → `admin_users_pending.html` — pending
  approval queue (`status == "pending"`, ordered by `created_at` asc,
  `per_page=50`). The current template renders **no** pagination controls; we
  preserve that (page-1 list only) — a pre-existing limitation, not introduced
  here.

**Mutations (currently form-POST → 303 redirect):**
- `approve` `{role, initial_password?}` — pending → active; `is_admin =
  role == "admin"`; sets `approved_by_user_id`/`approved_at`. Local (non-OAuth,
  `oidc_sub is None`) accounts **require** `initial_password`; OAuth pending
  accounts may be approved without one. Issuing a password bumps `session_epoch`.
- `deny` — pending → disabled.
- `disable` — active → disabled; bumps `session_epoch`; **refuses to disable the
  last active admin** (row-locked count guard).
- `enable` — disabled → active.
- `role` `{role}` — active user admin↔member; **refuses to demote the last active
  admin** (row-locked count guard).
- `reset-password` `{new_password}` — set a new password hash; bumps
  `session_epoch`; rejects weak passwords.

## Architecture

Standard slice shape, three layers in one branch:

1. **Additive typed `/api`** — new module `web/api/admin_users.py`, registered in
   `web/api/__init__.py`. All endpoints `require_admin`. Errors raised as
   `HTTPException` and mapped by the global `/api` handler to the
   `{"error":{"code","message"}}` envelope — never hand-built. Response models
   namespaced `Admin*` (an unrelated `UserOut` already exists in the `/api`
   schema; `Admin*` avoids the collision — re-verify at regen).

2. **React** — `frontend/src/routes/AdminUsers.tsx` and `AdminPendingUsers.tsx`
   under the existing AppShell; hooks in `frontend/src/api/admin.ts`. Styling =
   CSS Modules + `--app-*` tokens, **never bare element selectors** in a
   `.module.css` (scope under a hashed class).

3. **Teardown** — trim the migrated routes/imports out of `web/routes/admin.py`,
   delete the two templates, migrate the two test files, update the manifest.

### API endpoints

- `GET /api/admin/users?page=N`
  → `{ users: [{id, email, name, status, is_admin}], page, per_page, total }`
  (`page = max(1, page)`, `per_page = 50`).
- `GET /api/admin/users/pending?page=N`
  → `{ users: [{id, email, name, created_at, google_linked}], page }`
  (`google_linked = oidc_sub is not None`).
- `POST /api/admin/users/{id}/approve` `{role="member", initial_password?}` →
  `200`. **Preserve the existing intra-handler check order** (`approve_user`,
  admin.py:430-451): the role param is validated **before** the DB lookup, and
  404 comes **before** the status/password checks. In order:
  1. `400` invalid role (not in `{member, admin}`) — *before* the lookup
  2. `404` user not found
  3. `400` user is not pending
  4. `400` local account (no `oidc_sub`) with no `initial_password`
  5. `400` weak password (`str(WeakPassword)` as the envelope message)
  The request model **defaults `role` to `"member"`** (parity with the Jinja
  `Form("member")`; the UI always sends it explicitly).
- `POST /api/admin/users/{id}/deny` → `200`; `400` not pending; `404`
- `POST /api/admin/users/{id}/disable` → `200`; `400` not active;
  `400` cannot disable the last active admin; `404`
- `POST /api/admin/users/{id}/enable` → `200`; `400` not disabled; `404`
- `POST /api/admin/users/{id}/role` `{role}` → `200`. Same role-before-lookup
  order as approve (`change_role`, admin.py:497-517): `400` invalid role
  (*before* the lookup) → `404` not found → `400` not active → `400` cannot
  demote the last active admin.
- `POST /api/admin/users/{id}/reset-password` `{new_password}` → `200`;
  `400` weak password; `404`. **No status gate** — see invariants.

Mutations return a minimal body (e.g. `{"id": <user_id>}`) on success; the React
client refetches the list rather than consuming a full updated row.

**POST sub-resource actions are intentional** (not RESTful PATCH): a strict
parity port that matches the existing codebase pattern (e.g. cockpit
`POST .../turns/{job}/cancel`). Verb-as-subpath keeps the guard semantics and
the 1:1 mapping to the retired Jinja routes obvious.

### Parity-critical invariants (preserve verbatim)

- **Last-active-admin guard** — `_lock_active_admin_count` does
  `SELECT ... WHERE is_admin AND status='active' FOR UPDATE` before counting, so
  two concurrent demote/disable transactions serialize and cannot both pass the
  `<= 1` guard. This helper moves into `web/api/admin_users.py` (nothing else
  imports it). Both `disable` and `role` use it.
- **`session_epoch` bumps** — on `disable`, on `reset-password`, and on
  `approve` when a password is issued. (Epoch bump revokes the target's existing
  sessions.) `enable`, `deny`, `role`, and password-less OAuth `approve` do
  **not** bump.
- **Local-vs-OAuth approve branch** — `oidc_sub is None` ⇒ `initial_password`
  required; OAuth pending users approve without one.
- **Status-transition gates** — `approve`/`deny` require `status == "pending"`;
  `disable`/`role` require `status == "active"`; `enable` requires
  `status == "disabled"`. Each wrong-state attempt is a `400`.
- **`reset-password` has NO status gate (explicit non-gate)** — the current
  route resets a password for a user in *any* status (admin.py:520-537 checks
  only 404, then weak-password); its only failure modes are `404` and weak-`400`.
  Do **not** add a `status == "active"` (or any status) gate — that is exactly
  the kind of "helpful hardening" that would silently break parity.
- **Check ordering** — for `approve` and `role`, the role param is validated
  **before** the DB lookup (so an invalid role on a nonexistent user is `400`,
  not `404`); for all mutations, the `404` user-not-found check precedes the
  state/guard checks. Preserve this order verbatim.
- **Weak passwords** — `hash_password` raises `WeakPassword`; map to `400` with
  `str(e)` as the message. (`MIN_PASSWORD_LENGTH = 12`; test fixtures must seed
  ≥12-char passwords.)

### React surface

- **Routes:** `/admin/users` → `AdminUsers.tsx`; `/admin/users/pending` →
  `AdminPendingUsers.tsx`. Registered in `router.tsx`; the manifest owns the
  `/admin/users` **prefix** (the two routes + the prefix land in one commit —
  `router.test.tsx` is set-equality over owning prefixes).
- **Admin guard:** a small wrapper keying off `useMe().capabilities.admin`.
  Non-admins get a "not authorized" state (the `/api` 403 is the backstop;
  the guard avoids a broken-page flash on direct navigation). Unauthenticated
  users never reach it — `/api/me` 401s and the SPA redirects to login.
- **Hooks (`api/admin.ts`):** `useAdminUsers(page)` and
  `useAdminPendingUsers(page)` (both `retry: false`, per house pattern); six
  mutation hooks (`useApproveUser`, `useDenyUser`, `useDisableUser`,
  `useEnableUser`, `useChangeRole`, `useResetPassword`), each invalidating the
  relevant list query on success. **`approve` and `deny` invalidate *both* the
  pending and the active/disabled lists** — an approved/denied user leaves the
  pending queue and shows up in the other list (active, resp. disabled), so a
  navigated-to users list stays fresh. This is a deliberate judgment call beyond
  strict parity (the Jinja flow just reloads pending); the other four mutations
  invalidate the active/disabled list only. Backend guard messages are surfaced
  inline to the admin from the error envelope.
- **`AdminUsers` UI:** table (Email · Status · Role · Actions). Active rows:
  **Disable**, **Make admin**/**Make member** (toggle by current role), and an
  inline **reset-password** input + button. Disabled rows: **Enable**.
  Prev/next pagination from `total`/`per_page`.
- **`AdminPendingUsers` UI:** one block per pending user (email, optional name,
  a "(Google)" marker when `google_linked`). Actions: **Approve as member**,
  **Approve as admin** (each shows an inline `initial_password` input **only when
  not** `google_linked`), and **Deny**. Empty state: "No accounts awaiting
  approval."
- **Password-entry UX:** replicate the **inline per-row password inputs**
  (parity) — no modals.
- **Interim nav:** no SPA ActivityBar change in this slice. Entry stays via the
  still-Jinja `/admin` dashboard's "Users"/"Pending approvals" links, which now
  cross-document-land on the React pages. The React pages link to each other via
  react-router and back to `/admin` via full navigation. Slice 2 wires the
  in-SPA admin nav when bare `/admin` becomes React.

### Teardown (this slice's branch)

- **Trim** `web/routes/admin.py`: delete the eight user-mgmt route functions
  (`pending_users`, `list_users`, `approve_user`, `deny_user`, `disable_user`,
  `enable_user`, `change_role`, `reset_password`) and the `_lock_active_admin_count`
  helper; remove imports that become unused (`WeakPassword`, `hash_password`, and
  any others) or ruff F401 fails. **The module stays** — Slice 2 owns the rest.
- **Delete** templates `admin_users.html`, `admin_users_pending.html`.
- **Manifest:** add `/admin/users` to `frontend/routes.json` `spa_routes`; regen
  `frontend/src/api/schema.gen.ts` (drops the 8 Jinja user paths, keeps the
  Slice-2 admin paths) via `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`,
  and `routes.gen.ts` + `spa_routes.py` via `uv run python scripts/gen_routes.py`.
  **Re-run the OpenAPI regen AFTER the teardown** — deleting routes changes the
  schema (the Slice-E lesson). Bare `/admin` **stays** in `backend_prefixes`.
- **Tests:** migrate `tests/test_web_admin_users.py` and
  `tests/test_web_admin_passwords.py` from form-POST/303 to `/api` JSON
  (200/400/403/404 + envelope codes), preserving **every** guard, epoch, and
  status-transition assertion. These are the only two test files that touch
  `/admin/users` (verified). A route-absence guard asserts the eight Jinja
  user paths return 404 after teardown.

## Testing

- **Backend** (`tests/`, root — psycopg3, `pg_url`/`pg_session_factory` fixtures):
  migrated `test_web_admin_users.py` + `test_web_admin_passwords.py` against the
  new `/api/admin/users*` endpoints, covering: list/pending read shapes +
  pagination; approve member/admin; approve rejects non-pending; local approve
  requires password + bumps epoch; OAuth approve without password; deny only
  pending; enable only disabled; disable bumps epoch; cannot disable/demote the
  last active admin; reset-password bumps epoch + rejects weak; non-admin → 403;
  unknown user → 404. **Ordering locks:** "invalid role on a nonexistent user →
  `400` (not `404`)" pins role-before-lookup for both approve and role; a
  reset-password on a non-active (e.g. disabled) user succeeds, pinning the
  no-status-gate invariant. Plus a route-absence guard for the retired Jinja
  paths.
- **Frontend** (`frontend`, vitest + RTL): `AdminUsers.test.tsx`,
  `AdminPendingUsers.test.tsx`, `api/admin.test.tsx`, and the `router.test.tsx`
  set-equality update. Cover the role toggle, disable/enable, inline
  reset-password, the conditional `initial_password` input (google vs local),
  the not-authorized guard, and that each mutation invalidates its list query.
- **Gates:** `tsc --noEmit` (0), `pnpm build`, `gen_routes.py --check` in sync.
  Whole-suite testcontainers Postgres is flaky under load — run the targeted
  files; a connection-refused on a targeted re-run is infra.

## Global constraints

- No DB schema change. No new dependency. Strict parity port.
- Errors via `raise HTTPException` → global `/api` envelope; never hand-built.
- `OriginCSRFMiddleware` allows an **absent** Origin; TestClient mutation tests
  pass without an Origin header — do not add one.
- CSS Modules + `--app-*` tokens; no bare element selectors in `.module.css`.
- `tsconfig` has `noUnusedLocals: true` + `include: ["src"]` — tsc checks test
  files too; no unused imports/vars anywhere under `src/`.

## Out of scope (Slice 2)

The `/admin` ops dashboard (sources/ingest, ranking queue, digest panel,
embedding-coverage badges, golden-set report summary, calibration warning), the
`/admin/drafting` panel, golden-set CRUD (`/admin/golden-set*`), the ops actions
(`/admin/rerank`, `/admin/digest/{send,reenable}`, `/admin/sources/{slug}/fetch`),
the SPA ActivityBar admin nav, and the final admin Jinja teardown (deleting
`web/routes/admin.py` + `golden_set.py` + remaining templates). The
`test_web_auth` repoint target (bare `/admin`, `/admin/rerank`) is **untouched**
by Slice 1 — both are Slice 2.
