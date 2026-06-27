# Auth + final Jinja teardown — React migration (the LAST sub-project)

**Date:** 2026-06-27
**Status:** design approved, pre-plan
**Part of:** the full-React migration (strangler-fig, route-by-route). This is the
**final sub-project** ([[project_full_react_migration]] tracker). After it merges,
the web app is a single React SPA over a pure-JSON `/api` + OAuth-redirect backend;
Jinja2/HTMX/Pico are gone from the web layer. (The email-digest Jinja —
`digest/templates/digest.html.jinja`, its own env — renders EMAIL not pages and STAYS.)

## Reframing — the coordination blocker is moot

Prior memory flagged "coordinate with the in-flight `feat/google-oidc-pkce` branch."
That branch is **already merged into master**: its tip is an ancestor of master
(`git merge-base --is-ancestor` exit 0), `git diff master...feat/google-oidc-pkce`
is empty, and all its code (`auth/oidc.py`, `auth/oauth_txn.py`,
`web/routes/oauth_google.py`) is on master. It is a stale pointer 603 commits behind
and can be deleted. So this sub-project has **no parallel-branch entanglement** — it
is a clean, self-contained migration.

## Goal

Migrate the 8 server-rendered Jinja auth pages to React under a new pre-auth layout,
replace the login/register/logout form-POSTs with typed `POST /api/auth/*` JSON
endpoints, add a sign-out affordance to the SPA shell, and delete the entire web
Jinja stack (`base.html` + all templates + `pico.min.css`/`app.css`/`app.js`/htmx +
the web Jinja env). Done as **ONE atomic slice** (the teardown IS the point; the
assets orphan the moment the React pages go live; a local-only `--no-ff` merge is
revertible if verification fails).

## Surface being migrated

Source (all on master): `web/routes/auth.py` (login/logout), `web/routes/register.py`
(register), `web/routes/oauth_google.py` (OAuth + 3 holding-page GETs); templates
`login.html`, `register.html`, `register_received.html`, `account_pending.html`,
`account_no_access.html`, `auth_error.html`, `error.html`, `base.html`.

**Pages (8 templates → React):** login, register (+ its "request received" state),
account-pending (`/auth/pending`), account-no-access (`/auth/no-access`), auth-error
(`/auth/error`), and the generic `error.html` (retired — the SPA handles 404s
client-side; unmatched non-SPA paths get FastAPI's default JSON 404).

**Stays server-side (forced by OAuth mechanics):** `POST /auth/google/start` (sets
the PKCE `oauth_txn` cookie, 303s to Google) and `GET /auth/google/callback` (the
redirect endpoint). The React login's "Continue with Google" is a native
`<form method="post" action="/auth/google/start">`, NOT a fetch.

## Architecture

Standard slice shape, adapted for pre-auth pages: additive typed `/api/auth`
endpoints → React routes under a NEW pre-auth layout (no IDE shell) → flip the
manifest → delete the web Jinja stack.

### Backend — new `web/api/auth.py`

Lifts the existing password-verify, rate-limiters (`app.state.email_limiter` /
`ip_limiter` sliding windows), `SessionSigner`, status-routing, and `safe_next`
logic out of the doomed Jinja `auth.py`/`register.py` into a typed `/api` module.
All endpoints reuse the same cookie + CSRF mechanics the rest of the SPA uses
(same-origin fetch sends `Origin` → `OriginCSRFMiddleware` passes; the signed cookie
is set/cleared server-side).

**`POST /api/auth/login`** — body `{email, password, next?}`. IP + email rate-limit,
argon2 verify, then the server decides the outcome and returns the path React should
navigate to (so open-redirect sanitization stays server-side via `safe_next`):
- active → set the signed session cookie, update `last_login_at`, `200 {redirect: safe_next(next) or "/"}`
- pending → no cookie, `200 {redirect: "/auth/pending"}`
- disabled → no cookie, `200 {redirect: "/auth/no-access"}`
- bad credentials → `401` envelope; rate-limited → `429`. React renders these inline
  (no redirect → no 401→/login loop; the current Jinja hacks around this by returning
  the error at HTTP 200).

React: on `200`, invalidate the `["me"]` query and navigate to `redirect`; on an
error envelope, show the message in the form.

**`POST /api/auth/register`** — body `{email, name?}`. IP rate-limit, domain
validation, create a pending User. **Anti-enumeration preserved verbatim:** always
`200 {ok: true}` — catch the duplicate-email `IntegrityError` silently and run the
dummy argon2 hash for timing parity, exactly as the current route. React shows the
"request received" state inline (no separate route).

**`POST /api/auth/logout`** — clear the session cookie, bump `session_epoch` (revokes
all outstanding cookies), delete the `oauth_txn` cookie. `200 {ok: true}`; React then
full-page-navs to `/login`.

The old `POST /login` / `POST /logout` / `GET /register` Jinja routes are deleted
(their logic moves here). `safe_next` is kept as the shared sanitizer (now consumed
by `/api/auth/login` instead of the deleted `_redirect_html_401` branch).

### React surface

- **`PreAuthLayout`** — a SECOND top-level pathless layout route, sibling to
  `AppShell` in `router.tsx`. Renders auth pages as a centered card on the app
  background with NO ActivityBar / Sidebar / StatusBar. Children: `/login`,
  `/register`, `/auth/pending`, `/auth/no-access`, `/auth/error`. `AppShell` keeps
  every other route.
- **Components:** `Login` (email/password form + "Continue with Google" native POST +
  "Request access" link), `Register` (email/name form → `/api/auth/register` → inline
  "request received"), and three trivial holding-page components
  (`AccountPending`/`AccountNoAccess`/`AuthError`). `api/auth.ts` hooks for
  login/register/logout. CSS Modules + `--app-*` tokens; no bare element selectors.
- **Sign-out + user chip in `TitleBar`** (top-right): a chip showing the user's email
  (from `/api/me`'s `UserOut.email`) + a **Sign out** button → `POST /api/auth/logout`
  → `window.location.assign("/login")` (full-page nav clears SPA state; the next load
  starts fresh at login). This fills the gap left by deleting `base.html`'s sign-out
  form — it is a NECESSARY scope addition, not optional.
- **Refresh:** the login page reads `?next=` from the URL and threads it to the POST.

### Serving + the `/auth`-prefix resolution

- index.html is already served unauthenticated for SPA routes (no auth gate on
  index.html serving), so a logged-out browser at `/login` gets the React shell.
- Manifest: add `/login`, `/register`, `/auth` to `spa_routes`. The
  `router.test.tsx` set-equality guard collapses the three `/auth/*` leaves to the
  `/auth` prefix.
- **The `/auth` SPA prefix must NOT shadow the real OAuth routes — and it doesn't:**
  `mount_spa`'s catch-all is registered LAST, so the explicit `POST /auth/google/start`
  and `GET /auth/google/callback` match first. Only the *deleted* holding-page GETs
  (`/auth/pending|no-access|error`) fall through to the catch-all → index.html →
  React. `/auth` stays in `backend_prefixes` (doc-only — OAuth lives there);
  `/login` and `/register` move OUT of `backend_prefixes` INTO `spa_routes`.

### The 401-flow simplification

`_redirect_html_401` (main.py) loses its entire non-`/api` HTML branch: the
303-to-`/login`, the `hx_partial` logic, the styled-error `TemplateResponse`, and the
`{detail}` paths collapse to a single minimal `JSONResponse({"detail": exc.detail})`
fallback for the rare non-`/api` exception; the `/api` → envelope branch is unchanged.
The flow becomes uniform and SPA-driven: a logged-out browser at any SPA route loads
index.html → the first `/api` call 401s → `unwrap` → `redirectToLogin()` → full-page
nav to `/login` (now React). The backend never redirects browsers again. This removes
the last consumer of `error.html` and the dormant branch carried from admin Slice 2.

### Teardown (this slice)

- **Delete** all 8 templates incl. `base.html`; `web/routes/auth.py` +
  `web/routes/register.py` whole; **trim** `oauth_google.py` (keep the 2 OAuth routes,
  delete the 3 holding-page GET routes).
- **Delete static:** `pico.min.css`, `app.css`, `app.js`, `htmx.min.js`,
  `htmx-sse.js`, `dedup.js` (keep `favicon.svg`). This is the "deferred dead workspace
  CSS/JS" — it all lived in `app.css`/`app.js`.
- **`main.py`:** remove the web Jinja env (`app.state.templates`) + its filter
  registrations, the `auth`/`register` `include_router` lines, and the
  `_redirect_html_401` non-`/api` HTML branch. Keep filter *helper functions* only if
  something else imports them (verify in planning); the digest Jinja env is untouched.
  Keep the `/static` mount (now serving only `favicon.svg`).
- **Manifest:** `/login`+`/register` `backend_prefixes`→`spa_routes`; `/auth` added to
  `spa_routes` (stays in `backend_prefixes`, doc-only). Two OpenAPI regens
  (`EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`) — after `/api/auth`
  exists and AFTER teardown (route deletions change the schema, the Slice-E lesson) —
  + one routes regen (`uv run python scripts/gen_routes.py`).
- **Stale-branch cleanup (optional):** delete the merged `feat/google-oidc-pkce`
  pointer.

## Testing

- **Backend** `tests/test_api_auth.py` (psycopg3 fixtures): login (active→cookie + `/`
  redirect, pending→`/auth/pending`, disabled→`/auth/no-access`, bad creds→401,
  rate-limit→429, `next` sanitization rejects non-local/oversized), register
  (always-200 anti-enumeration, domain validation, silent duplicate, timing
  dummy-hash), logout (`session_epoch` bump + cookie cleared). The page-behavior tests
  in `test_web_auth.py` migrate to this `/api` shape (coverage-mapped before deleting
  them — the profiles re-embed lesson); the proxy / rate-limit infra tests
  (`X-Forwarded-For`) repoint their POST onto `/api/auth/login`.
- **Frontend** (vitest + RTL): `Login` / `Register` / the three holding pages /
  `PreAuthLayout` (asserts NO AppShell chrome) / `TitleBar` sign-out + user chip;
  hook tests for `api/auth.ts`; the `router.test.tsx` set-equality update (manifest now
  includes `/login`, `/register`, `/auth`).
- **Full flow**: a headless logged-out → `/login` → authenticate → authed shell →
  sign-out → `/login` render check ([[reference_spa_visual_check]] recipe).
- **Teardown guard**: a surface-absent test asserting the Jinja auth GET routes
  (`/login`, `/register`, `/auth/pending|no-access|error`) are gone from `app.routes`,
  the `/api/auth/*` routes present, and NO web-layer `TemplateResponse` / `base.html` /
  the deleted static files remain.
- **Gates**: `tsc --noEmit` 0, `pnpm build`, `gen_routes.py --check` in sync,
  `ruff check .` green (lint tests/ too — the Slice-1 lesson). Whole-suite
  testcontainers Postgres flakes under load → run targeted files; note the
  `test_web_auth`-first `UndefinedTable` migration-ordering flake
  ([[project_full_test_suite_flaky]]).

## Global constraints

- No DB schema change. No new dependency. Strict parity port (only intended behavior
  change: the banner/redirect-UX becomes JSON `{redirect}`; anti-enumeration, rate
  limits, status routing, cookie attributes, `session_epoch` semantics, `safe_next`
  sanitization all preserved).
- Errors via `raise HTTPException` → the global `/api` envelope; never hand-built.
- `OriginCSRFMiddleware` allows an absent Origin; TestClient POST tests pass without an
  Origin header — do not add one. Same-origin browser fetch sends Origin → passes.
- CSS Modules + `--app-*` tokens; no bare element selectors in `.module.css`. The
  pre-auth pages match the SPA's dark-teal theme (centered card on the app background).
- `tsconfig` `noUnusedLocals` + `include: ["src"]` — no unused imports under `src/`.
- Manifest is prefix-not-`:param`: `/login`,`/register`,`/auth` added to `spa_routes`;
  the new React routes + the manifest change land in one commit (`router.test.tsx`
  set-equality).
- The OAuth flow stays server-side redirects; only the *pages* become React. The
  `oauth_txn` PKCE cookie and `/auth/google/callback` are untouched.

## Out of scope

Nothing follows this — it is the LAST sub-project. The email-digest Jinja
(`digest/templates/digest.html.jinja`, separate env) renders email and stays. After
this slice merges, the full-React migration is COMPLETE: FastAPI serves pure-JSON
`/api`, OAuth redirects, and the static React bundle; no web Jinja, HTMX, or Pico.
