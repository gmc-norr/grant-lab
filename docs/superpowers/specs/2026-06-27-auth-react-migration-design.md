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
argon2 verify (the dummy-hash path runs even when the user/hash is absent, so timing
never reveals account existence — preserved verbatim), then the server decides the
outcome and returns the path React should navigate to (so open-redirect sanitization
stays server-side via `safe_next`):
- active → set the signed session cookie, update `last_login_at`, `200 {redirect: safe_next(next) or "/"}`
- non-active: **pending → `200 {redirect: "/auth/pending"}`** (no cookie); **any other
  status (the catch-all) → `200 {redirect: "/auth/no-access"}`** (no cookie). Mirrors
  `auth.py:140` (`"/auth/pending" if status=="pending" else "/auth/no-access"`) — the
  plan must NOT hard-code `status=="disabled"` or it silently drops a future status.
- **failure → a SINGLE generic `400 {message: "Invalid email or password."}` for
  unknown-email / wrong-password / no-credential / rate-limited alike**, indistinguishable
  across all four modes (verbatim parity with the deleted `_denied()`, auth.py:103-127).
  React renders it inline.

**Why `400`, not `401` — and one bucket for rate-limit (a pinned invariant, not an
accident):** the SPA's `unwrap` redirects to `/login` on ANY `401` (the single
session-expiry chokepoint), so a `401` from `/api/auth/login` would bounce the user
back to `/login` and lose their input — exactly the loop the current Jinja dodges by
returning failures at HTTP 200 (see the `_denied()` comment: *"Status 200 so the global
401→/login handler can't loop"*). `400` flows through `unwrap` as a thrown `ApiError`
with NO redirect, so React shows the message in the form. Rate-limited is folded into
the same generic `400` (NOT a distinct `429`) to preserve `_denied()`'s deliberate
non-disclosure — the endpoint must never reveal whether a caller failed on credentials
vs. the limiter. (A `429` "too many attempts" UX would be an explicit opt-in behavior
change, not parity.) A test pins: the three credential-failure modes return byte-identical
status + message.

React: on `200`, invalidate the `["me"]` query and navigate to `redirect`; on the `400`,
show the generic message in the form (strip the `ApiError` code prefix).

**`POST /api/auth/register`** — body `{email, name?}`. IP rate-limit, domain
validation, create a pending User. **Anti-enumeration preserved verbatim:** always
`200 {ok: true}` — catch the duplicate-email `IntegrityError` silently and run the
dummy argon2 hash for timing parity, exactly as the current route. React shows the
"request received" state inline (no separate route).

**`POST /api/auth/logout`** — bump `session_epoch` (revokes all outstanding cookies)
and clear the `session` cookie, mirroring `logout_submit` (auth.py:160-196) EXACTLY.
It does NOT touch the `oauth_txn` cookie — the current route doesn't either, and adding
that would be a (small) behavior change, so it stays out for strict parity. `200 {ok:
true}`; React then full-page-navs to `/login`.

The old `POST /login` / `POST /logout` / `GET /register` Jinja routes are deleted
(their logic moves here). `safe_next` is kept as the shared sanitizer (now consumed
by `/api/auth/login` instead of the deleted `_redirect_html_401` branch).

### React surface

- **`PreAuthLayout`** — a SECOND top-level pathless layout route, sibling to
  `AppShell` in `router.tsx`. Renders auth pages as a centered card on the app
  background with NO ActivityBar / Sidebar / StatusBar. Children: `/login`,
  `/register`, `/auth/pending`, `/auth/no-access`, `/auth/error`. `AppShell` keeps
  every other route.
  - **INVARIANT (the slice's single most dangerous failure mode):** PreAuthLayout and
    its pages make NO authenticated `/api` call on mount (no `useSession`, no protected
    query). A logged-out `/login` that fired `useSession` would `401` → `unwrap` →
    `redirectToLogin("/login")` → infinite loop. `PreAuthLayout` is therefore a bare
    layout — it must NOT render the shell (which calls `useSession`). The Login page's
    only `/api` call is the `POST /api/auth/login` mutation, on submit (never on mount).
- **Components:** `Login` (email/password form + "Continue with Google" native POST +
  "Request access" link), `Register` (email/name form → `/api/auth/register` → inline
  "request received"), and three trivial holding-page components
  (`AccountPending`/`AccountNoAccess`/`AuthError`). `api/auth.ts` hooks for
  login/register/logout. CSS Modules + `--app-*` tokens; no bare element selectors.
- **Sign-out RELOCATED to `TitleBar`** (top-right) + a user-email chip. This is a
  RELOCATION, not a new feature: the SPA's current sign-out is a native
  `<form method="post" action="/logout">` in **`DashboardHeader.tsx:31`** (NOT
  `base.html` — that only backs the retired Jinja pages), and it POSTs to the very
  `POST /logout` route this slice deletes. So the teardown WOULD break it; the fix is
  to move the control into the shell's `TitleBar` (visible on every authed route, not
  just the dashboard) and **remove it from `DashboardHeader`**. Mechanism shifts from
  native-form-303 to a `useLogout` fetch → `POST /api/auth/logout` → `200 {ok}` →
  `window.location.assign("/login")` (the native form existed precisely to carry
  Origin/CSRF without fetch; the same-origin fetch sends `Origin` → `OriginCSRFMiddleware`
  passes, consistent with the rest of the SPA). The chip reads
  `useSession().data?.user.email` (`/api/me` returns `MeOut = {user: UserOut, capabilities}`,
  so it's `MeOut.user.email`). No new request: the shell already calls `useSession`
  (`ActivityBar`, since admin Slice 2), so the chip reuses the cached `["me"]` query —
  a deliberate, already-realized universal session check.
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
- **Frontend cleanup:** remove the sign-out `<form action="/logout">` from
  `DashboardHeader.tsx` (relocated to `TitleBar`); update the now-stale comments in
  `redirectToLogin()` (client.ts) and `session.ts:6` that call `/login`
  "server-rendered" — mechanism is unchanged (still a full-page nav) but `/login` is
  React now.
- **Manifest:** `/login`+`/register` `backend_prefixes`→`spa_routes`; `/auth` added to
  `spa_routes` (stays in `backend_prefixes`, doc-only). Two OpenAPI regens
  (`EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`) — after `/api/auth`
  exists and AFTER teardown (route deletions change the schema, the Slice-E lesson) —
  + one routes regen (`uv run python scripts/gen_routes.py`).
- **Stale-branch cleanup (optional):** delete the merged `feat/google-oidc-pkce`
  pointer.

## Testing

- **Backend** `tests/test_api_auth.py` (psycopg3 fixtures): login (active→cookie + `/`
  redirect, pending→`/auth/pending`, disabled→`/auth/no-access`, bad creds→generic
  `400`, rate-limit→the SAME generic `400` (non-disclosure — never `429`), `next`
  sanitization rejects non-local/oversized), register
  (always-200 anti-enumeration, domain validation, silent duplicate, timing
  dummy-hash), logout (`session_epoch` bump + cookie cleared). The page-behavior tests
  in `test_web_auth.py` migrate to this `/api` shape (coverage-mapped before deleting
  them — the profiles re-embed lesson); the proxy / rate-limit infra tests
  (`X-Forwarded-For`) repoint their POST onto `/api/auth/login`.
- **Frontend** (vitest + RTL): `Login` / `Register` / the three holding pages /
  `PreAuthLayout` (asserts NO AppShell chrome AND fires NO authenticated `/api` call on
  mount — no `/api/me` — the anti-loop invariant) / `TitleBar` sign-out + user chip;
  hook tests for `api/auth.ts`; the `router.test.tsx` set-equality update (manifest now
  includes `/login`, `/register`, `/auth`). The `Login` test pins the anti-enumeration
  invariant: unknown-email, wrong-password, and no-credential failures all surface the
  identical generic message.
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

- No DB schema change. No new dependency. Strict parity port. **Intended behavior
  changes (exhaustive):** (1) the form-POST/redirect/banner UX becomes JSON `{redirect}`
  + inline errors; (2) login-failure HTTP status `200` → `400` (the generic message and
  cross-mode indistinguishability are preserved — the `200` was only a Jinja-loop
  workaround). **Everything else is preserved verbatim:** the generic
  `"Invalid email or password."` message, the argon2 dummy-hash timing path, IP+email
  rate-limit keying AND its non-disclosure (rate-limit folded into the same `400`),
  status routing incl. the `/auth/no-access` catch-all, cookie attributes
  (`httponly`/`secure`/`samesite=lax`/`max_age`), `session_epoch` revocation, `safe_next`
  sanitization, register anti-enumeration, and `oauth_txn` left untouched on logout.
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
