# Account / Settings React Migration (Design Spec)

**Date:** 2026-06-26
**Status:** Design approved; pending spec-review gate → writing-plans.
**Project:** Full-React migration → retire the remaining Jinja pages, one surface at a time.
**Builds on:** calls-detail merged (`4d7c93a`); the `workspace_main` Jinja teardown is complete; the cockpit is the sole workspace surface.

---

## 1. Why this slice exists

The account/settings page (`/me`) is still Jinja: `GET /me` → `me.html`, plus `POST /me/name`,
`POST /me/password`, `POST /me/subscriptions`, served by `src/call_manager/web/routes/me.py`. The SPA
shell's "Settings" nav item (`shell/nav-items.ts`) is a `kind:"external"` `<a>` that full-page-navigates
to that Jinja page. There is no `/api/me/settings`/name/password/subscriptions yet — only the lean session
read `GET /api/me` (`{user, capabilities}`) + `POST /api/me/default-profile`.

This is the **first sub-project of the profile/account migration**, decomposed account-first (the
self-contained settings page establishes the SPA settings-form + mutation patterns, incl. the password
cookie re-mint) ahead of the larger **profiles CRUD** sub-project (list/create-with-LLM-bootstrap/detail/
edit — its own spec later). The two are independent: `/me` only *reads* active-profile names + subscription
state; it shares no write path with the profiles CRUD.

Same strangler-fig pattern as calls-detail: **additive typed `/api` → React route under the existing
AppShell → per-slice Jinja teardown.** No DB schema change.

---

## 2. Goal & invariant

**Goal:** a React `/me` Settings page at parity with the Jinja page (header, display-name, Google sign-in,
change-password, weekly-digest subscriptions), driven by typed `/api/me/*` endpoints.

**Invariant after this slice:**
1. `/me` is a React route under the AppShell; the shell's Settings nav item is a React-Router `NavLink`
   (`kind:"spa"`), no full reload.
2. The page renders from `GET /api/me/settings`; name/password/subscriptions persist via dedicated `/api/me/*`
   mutations that mirror the Jinja handlers exactly (incl. the password-change **session re-mint +
   revoke-other-sessions**).
3. The Jinja `me_page` + `update_name`/`change_password`/`update_subscriptions` routes, `me.html`, the
   `web/routes/me.py` module, and its `main.py` registration are **deleted**; `/me` drops from
   `backend_prefixes` and is added to `spa_routes` as a **prefix**. No DB schema change.

---

## 3. Architecture

### 3.1 Backend — additive `/api/me/*` (append to `web/api/me.py`)

The existing `web/api/me.py` has `GET /api/me` (lean session `MeOut`) + `POST /api/me/default-profile`.
Append four endpoints (any authenticated user — operates on self):

**`GET /api/me/settings`** → `AccountSettingsOut` — the page read-model, mirroring the `me.html` context:
- `name: str | None`, `email: str`, `is_admin: bool`
- `google_oidc_enabled: bool` (from `settings.google_oidc_enabled`), `google_linked: bool`
  (`user.oidc_sub is not None`), `has_password: bool` (`user.password_hash is not None`)
- `profiles: list[AccountProfileOut]` — active profiles ordered by name, each `{id, name, subscribed: bool}`
  (`subscribed` = a `Subscription` row exists for `(user, profile)`).

**`POST /api/me/name`** — body `{name: str}` → 401 if the user row is missing (mirror the Jinja guard) →
`db_user.name = name.strip() or None` → returns `{name: str | None}`.

**`POST /api/me/password`** — body `{current_password: str, new_password: str}`:
- 401 if the user row vanished; **400 "current password is incorrect"** if `password_hash is None` OR
  `verify_password` fails (an OAuth-only account cannot mint a first password from a session alone);
  **400** with the `WeakPassword` message if `hash_password(new_password)` rejects it.
- On success: bump `session_epoch` (revokes other sessions), `session.commit()`, then **re-mint THIS
  browser's session cookie** via the injected `Response.set_cookie("session", session_signer.make(
  user_id, session_epoch=new_epoch), max_age=session_max_age_seconds, httponly=True,
  secure=cookie_secure, samesite="lax")` and return `{ok: true}`. Byte-for-byte the Jinja cookie attrs.

**`PUT /api/me/subscriptions`** — body `{profile_ids: list[int]}` (the *desired* subscribed set):
- 401 if the user row is missing (mirror the Jinja guard).
- Filter to active profiles in the set (`valid_ids`); apply the **diff-not-replace** logic verbatim
  (`to_remove = existing - valid_ids`, `to_add = valid_ids - existing`) so a surviving row's
  `digest_enabled` is never reset. Returns `{subscribed: list[int]}` (sorted resulting set).

All on the `/api` router, envelope conventions, errors via `raise HTTPException` → global envelope. Response
models namespaced `Account*` / `UpdateName*` / `ChangePassword*` / `Subscriptions*` to avoid schema
collisions (the calls-detail `CriterionOut` lesson — verify against `schema.gen.ts` during the plan).

**OAuth "Connect Google account"** stays a **native `<form method="post" action="/auth/google/start">`**
with a hidden `link=1` (full-page POST, server-side OAuth flow) — NO `/api`, NO React state. Rendered only
when `google_oidc_enabled && !google_linked`. The OAuth flow + its UI changes belong to the later auth slice
(coordinated with `feat/google-oidc-pkce`); this slice renders the button as-is.

### 3.2 Frontend — `Settings` route + hooks

- `api/account.ts` (**NOT `api/settings.ts`** — that already exists for the cockpit *workspace* settings,
  exporting `useSettings(id)`; a second `api/settings.ts`/`useSettings` would overwrite it / collide. Use an
  account-scoped module + hook names): `useAccountSettings()` (query `["me-settings"]` → `GET /api/me/settings`),
  `useUpdateName()` (→ `POST /api/me/name`, invalidate `["me-settings"]`), `useChangePassword()`
  (→ `POST /api/me/password`; no invalidation — `has_password` stays true and the cookie is re-minted by the
  response), `useSaveSubscriptions()` (→ `PUT /api/me/subscriptions`, invalidate `["me-settings"]`). (The
  session query `["me"]` carries no name field, so a name change need not touch it.) `routes/Settings.tsx` has
  no collision.
- `routes/Settings.tsx` + `Settings.module.css`: the five sections at parity —
  header (`Signed in as {email}` + admin badge), **display-name** form (save → inline success),
  **Google sign-in** (only if `google_oidc_enabled`: "connected" when linked, else the native connect form),
  **change-password** (only if `has_password`: current + new → inline success/error; on success the new
  cookie is already set by the response — show "Password changed"), **subscriptions** (checkboxes per active
  profile + a "Save subscriptions" button → `PUT` the checked ids; empty state links to the still-Jinja
  `/profiles/new`). Loading / error states.
- `shell/nav-items.ts`: flip the `settings` item from `kind:"external"` to `kind:"spa"` (`to:"/me"`
  unchanged) so the shell renders it as a `NavLink`. The `profiles` item STAYS `external` (Jinja until the
  next slice).

### 3.3 Manifest / routing

- `/me` owned as a **PREFIX** in `frontend/routes.json` `spa_routes` (the `/calls` precedent); the React
  route is `{ path: "me", element: <Settings/> }` under the AppShell. `router.test.tsx` is set-equality, so
  the route + the manifest prefix land in **one commit**.
- After teardown, remove `/me` from `backend_prefixes` (no bare `/me` backend route survives — the
  `/api/me/*` endpoints live under the `/api` prefix). Regen `routes.gen.ts` + `spa_routes.py`.

---

## 4. Scope & boundaries

- **One slice:** the settings read-model + 3 mutations + the React `/me` route + the nav flip + the manifest
  prefix + the Jinja teardown.
- **Additive**, then **per-slice teardown**: delete the whole Jinja `web/routes/me.py` module (the GET page +
  3 POSTs) + `me.html` + the `main.py` import/`include_router`. No relocation needed (nothing kept imports
  `me.py`).
- **No DB schema change. No new dependency.**
- **Deferred / out of scope:** the **profiles CRUD** sub-project (its own spec next); any OAuth link/unlink UI
  beyond the existing native connect button (auth slice); the admin and auth+final-teardown sub-projects;
  any redesign beyond a faithful parity port; instant-toggle subscriptions (chose strict parity batch-save).

---

## 5. Data flow / error handling

- **Password re-mint:** the success path sets `Set-Cookie: session=…` on the 200 JSON response (injected
  `Response`), so the current browser stays authenticated under the new `session_epoch` while other sessions
  are revoked — identical effect to the Jinja 303-with-cookie, minus the redirect (the SPA stays on `/me`).
- **Subscriptions** are a *desired-set* `PUT`; the diff logic preserves `digest_enabled` on surviving rows.
  Posting an empty set unsubscribes from all. Inactive / unknown ids in the body are silently ignored
  (filtered by the active-profile validation), matching the Jinja form.
- **Missing user row → 401**; wrong/absent current password or weak new password → **400** (envelope
  `bad_request`). **Unauthenticated → 401.**
- **Routing order:** add the React route + `/api` first; in the teardown step delete the Jinja `/me` routes
  **together with** the manifest flip (`+/me` in `spa_routes`, `−/me` in `backend_prefixes`, regen) — deleting
  the Jinja routes without the manifest flip would 404 `/me`. While the Jinja `/me` route exists the manifest
  prefix is inert (real route beats the catch-all), so there is no mid-slice regression.

---

## 6. Verification gates

- `uv run python -c "import call_manager.main"` boots; after teardown no non-`/api` route under `/me/`
  remains (route-table check).
- Backend suite green: settings read-model (name/email/admin, oauth/password flags, active profiles +
  subscribed flag); name update (strip→None); **password** (success re-mints the session cookie — **assert the
  `Set-Cookie` carries `httponly`, `samesite=lax`, `secure == cookie_secure`,
  `max-age == session_max_age_seconds`**, not merely that a cookie is present — and bumps `session_epoch`;
  wrong-current → 400; weak-new → 400; OAuth-only-no-password → 400); **subscriptions** (diff preserves
  `digest_enabled`; validates active; empty set clears). `ruff` clean. (The new mutations are unsafe `/api`
  methods so `OriginCSRFMiddleware` applies, but it 403s only a *present-but-mismatched* / `"null"` Origin —
  an **absent** Origin is allowed (`csrf.py:122-124`), so the TestClient mutation tests pass without an Origin
  header, exactly as the calls-detail hide tests do.)
- Frontend `tsc` + `vitest` green (Settings renders the conditional sections off the flags; name save;
  password change success/error; subscriptions checkbox → `PUT` the checked ids) + `build` clean.
- **Auth re-repoint — THREE tests, not two.** The calls-detail slice pointed all three of `test_web_auth.py`'s
  `/me`-sampling tests at `/me`: `test_html_401_get_threads_next` (`GET /me?x=1` → 303),
  `test_json_401_unaffected_by_next` (`GET /me`, no `Accept: text/html` → raw 401 JSON), and
  `test_html_401_post_does_not_thread_next` (`POST /me/name` → 303). This slice deletes the Jinja `/me` routes
  and `/me` flips from server-auth-gated to a client-auth-gated SPA prefix, so **all three move** (the JSON-401
  one too — after teardown `GET /me` 404s, it does not 401). Repoint: the **two GET** tests → `GET /profiles`
  (still-Jinja, protected); the **POST** test → `POST /profiles/{id}/edit` (its Form fields are all `Form("")`
  defaults, so an unauthenticated POST 401s on the auth dependency, not a 422 — a safe target). Confirm the
  exact `next=` encoding by running.
- Manifest/schema regen: `uv run python scripts/gen_routes.py` → `SPA_ROUTES` gains `/me`,
  `BACKEND_PREFIXES` drops `/me`; `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh` → new
  `/api/me/settings|name|password|subscriptions` paths added, Jinja `/me` HTML path dropped; `routes.gen.ts`
  gains the `/me` prefix.

---

## 7. Risks / notes

- **Cookie on a JSON response:** the password endpoint injects `Response` and calls `set_cookie` while still
  returning a `response_model` — FastAPI merges the cookie onto the serialized body. Mirror the Jinja cookie
  attrs exactly; a missed `httponly`/`samesite`/`secure` is a security regression.
- **Auth-test re-repoint is a recurring chain:** each teardown slice that deletes the current sample protected
  Jinja route must repoint the `test_web_auth.py` redirect tests onto the next surviving one (calls-detail →
  `/me`; this slice → `/profiles`; the profiles slice → `/admin`; the auth slice ultimately moves the 401
  behavior client-side). Expected, small, per-slice.
- **`/me` becomes a pure SPA prefix:** after teardown the only `/me*` backend routes are `/api/me/*` (under
  `/api`), so dropping `/me` from `backend_prefixes` is correct and `/me` joins `spa_routes` cleanly. (`/me`
  ships no React sub-routes, so EXACT ownership would be marginally stricter, but PREFIX matches the `/calls`
  precedent and is harmless — `is_spa_route` appends `/`, so `/me` never swallows a sibling like `/members`.)
- **OAuth connect callback still lands cleanly:** the native `/auth/google/start?link=1` POST redirects back to
  `/me` on callback success. Now that `/me` is a SPA GET prefix, that redirect serves the SPA shell → React
  renders Settings (and `google_linked` flips to "connected") — there is no Jinja `/me` to 404 into. So the
  connect button keeps working across this slice and into the later auth slice.
- **Model-name collisions:** namespace the new models (`Account*`/`UpdateName*`/`ChangePassword*`/
  `Subscriptions*`); verify none collide in `schema.gen.ts` during the plan.

---

## 8. Out of scope

- Profiles CRUD; OAuth link/unlink UI; admin; auth + final teardown (`base.html`/Pico/`app.css`/`app.js`).
- Any redesign beyond parity; instant-toggle subscriptions.
