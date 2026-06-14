# Google Accounts + OIDC + OAuth 2.0 Authorization Code with PKCE — Design

**Status:** approved design (brainstorm 2026-06-14). Ready for an implementation plan.
**Stack:** current — FastAPI + Jinja2 + HTMX, server-rendered, no SPA. PostgreSQL +
SQLAlchemy 2.x + Alembic. Additive, with one deliberate breaking change to the
magic-link flow (removal of auto-provisioning).

## Goal

Let people sign in with their **Google account** via standard **OpenID Connect**
(OAuth 2.0 **Authorization Code flow with PKCE**), while keeping account creation
fully under admin control. Self-service sign-in is allowed, but it grants **no
access** until an admin **individually approves** the account and sets its role.
Google sign-in **coexists** with the existing magic-link login.

The driving requirement (from the brainstorm): *"It must not be open for everyone.
As an admin I will accept all users individually and set roles for them."*

## Decisions (from the brainstorm)

1. **Approach:** bespoke Google OIDC in the app (not an external identity broker
   such as Keycloak/Authentik). A broker was considered and explicitly deferred —
   see Non-goals — to avoid running a second on-prem service for the current
   small user base. The OIDC client is built **discovery-URL-driven** (not
   Google-hardcoded internals) so a broker could be slotted in later as config.
2. **Provisioning gate = self-service sign-in + admin approval queue** (not
   token-bound email invitations, which were considered and dropped as more
   machinery than needed). Anyone may *authenticate*; nobody gets *access* until
   an admin approves them.
3. **Coexist** with magic-link. Magic-link becomes **login-only for already-active
   users**; its auto-create-on-login behaviour is removed (it is the current
   "open door").
4. **Roles:** **member** vs **admin** only, expressed with the existing
   `users.is_admin` boolean. No new role taxonomy.
5. **Identity storage:** **columns on `users`** (`oidc_sub`, `oidc_iss`), not a
   separate identities table — single issuer (Google) for now.
6. **OAuth transaction state** (`state`, `nonce`, PKCE `code_verifier`, `next`)
   lives in a **short-lived signed cookie**, consistent with the existing
   stateless-cookie architecture (no server-side OAuth-state table).
7. **Library:** **Authlib** for OIDC plumbing (discovery, JWKS, `id_token`
   signature + claims validation, PKCE helpers) over the already-vendored `httpx`.

## What already exists (build on, don't rebuild)

- **Magic-link auth** — `web/routes/auth.py`. `login_submit` currently
  **auto-creates** a `User` for any allowed-domain email
  ([`auth.py:163-170`](../../../src/call_manager/web/routes/auth.py#L163-L170)).
  Single-use sha256 tokens via `auth/tokens.py` (`issue_token` / `consume_token`,
  atomic single-statement consume). Scanner-proof GET interstitial pattern with
  per-handler `Referrer-Policy` override (`login_verify_confirm`).
- **Session cookies** — `auth/session_cookie.py` (`SessionSigner`, `itsdangerous`
  HMAC, payload `{uid, ep}`). `cookie_secure` derived from https `APP_BASE_URL`.
- **Current-user dependency** — `auth/dependencies.py`:
  `get_current_user` (verifies cookie, loads user, checks `session_epoch`) and
  `require_admin` (adds `is_admin` check, 403 otherwise).
- **User model** — `db/models.py`: `id, email (unique, lowercased), name,
  is_admin, created_at, last_login_at, digest_*, session_epoch,
  default_profile_id`. No password, no OIDC columns yet.
- **Admin bootstrap** — `admin_bootstrap.py`, run at startup from
  [`main.py:69`](../../../src/call_manager/main.py#L69): seeds
  `INITIAL_ADMIN_EMAILS` as `is_admin=True` users.
- **Config** — `config.py` pydantic `Settings`, `.env`-driven, with production
  guards (https ⇒ strong `SESSION_SECRET`; refuse to start without an email sender).
- **Security middleware** — `auth/csrf.py` (Origin/Referer on unsafe methods);
  `web/middleware.py` (security headers + CSP `script-src 'self'`,
  `Referrer-Policy: same-origin`, `form-action 'self'`). The "Continue with
  Google" control is a same-origin form POST → CSP- and CSRF-clean; the redirect
  to Google is a server-side 303 (not subject to `connect-src`/`form-action`).
- **Admin UI** — `web/routes/admin.py` (`require_admin`), existing user-management
  pages to extend.

## The one rule

> **No account gets access without explicit admin approval.** Authentication
> (Google OIDC or magic-link) only proves *who* a person is. Authorization — does
> this person have access, and as member or admin — is decided by the admin and
> enforced centrally in `get_current_user`.

## Data model changes

### `users` — new columns (one Alembic migration)

| column | type | notes |
|---|---|---|
| `status` | enum/text `pending` \| `active` \| `disabled`, NOT NULL | New self-service accounts default `pending`. `disabled` revokes an approved user. |
| `oidc_sub` | text, nullable | Google `sub` claim (stable per-user-per-issuer identifier). |
| `oidc_iss` | text, nullable | Issuer (`https://accounts.google.com`). Matched together with `oidc_sub`. |
| `approved_by_user_id` | bigint FK → users.id, nullable | Audit: who approved. |
| `approved_at` | timestamptz, nullable | Audit. |

- **Unique constraint** on `(oidc_iss, oidc_sub)` where both non-null (partial
  unique index) — one Google identity maps to at most one account.
- `is_admin` is retained as the role flag (member = `false`, admin = `true`).
- **Migration backfill:** all existing rows → `status = 'active'` (they predate the
  gate and are already trusted). Bootstrap admins are likewise `active`.

No new tables. (The earlier `invitations` / `oauth_identities` tables from the
first design draft are **not** built.)

## Authentication flows

### A. Google OIDC + PKCE — sign-in / sign-up

Routes (new router, e.g. `web/routes/oauth_google.py`):

1. **`POST /auth/google/start`** (same-origin form POST from the login page;
   carries optional `next`):
   - Load OIDC metadata from the discovery document (cached).
   - Generate PKCE `code_verifier` + `code_challenge` (**S256**), a random
     `state`, and a random `nonce`.
   - Store `{state, nonce, code_verifier, next}` in a **short-lived signed
     cookie** (`itsdangerous`; HttpOnly, Secure per `cookie_secure`,
     `SameSite=Lax`, `Max-Age ≈ 600s`, `Path=/auth/google`).
   - **303 redirect** to the `authorization_endpoint` with
     `response_type=code`, `client_id`, `redirect_uri`, `scope=openid email
     profile`, `state`, `nonce`, `code_challenge`, `code_challenge_method=S256`.

2. **`GET /auth/google/callback?code&state`** (top-level navigation back from
   Google; a `SameSite=Lax` cookie is sent on this GET):
   - Read + delete the transaction cookie; **reject** if missing.
   - Verify `state` equals the cookie's `state` (CSRF on the callback).
   - Exchange `code` + `code_verifier` at the `token_endpoint` (server-to-server
     over `httpx`).
   - Validate the `id_token`: signature via Google **JWKS**; `iss` matches the
     issuer; `aud` == our `client_id`; `exp`/`iat` valid; `nonce` equals the
     cookie's `nonce`.
   - Require **`email_verified == true`**; otherwise reject (holding page,
     "email not verified").
   - **Resolve the account** (see "Account resolution" below).
   - On success for an `active` user: set the session cookie via the existing
     `SessionSigner` and **303 redirect** to a safe `next` (default `/`).
   - The callback **never renders HTML containing `?code=`** — it always 303s —
     so the authorization code cannot leak via `Referer`. Set
     `Referrer-Policy: no-referrer` on the redirect as belt-and-suspenders.

### B. Magic-link — login only (changed)

- Remove the auto-create branch
  ([`auth.py:167-170`](../../../src/call_manager/web/routes/auth.py#L167-L170)).
  A token is issued **only** if an `active` user already exists for the email.
- Enumeration-resistance is preserved: the "check your email" page is still
  returned byte-identically on every branch; we simply don't issue/send a token
  for unknown or non-`active` emails.
- Existing users (bootstrap admins, approved users) continue to log in by
  magic-link unchanged.

## Account resolution (callback)

Given a validated Google identity `(iss, sub, email)`:

1. **Match by identity** — if a user has this `(oidc_iss, oidc_sub)`: that's the
   account. Update `last_login_at`. Proceed to the access check.
2. **Else link by verified email** — if an `active` (or `disabled`) user exists
   with this exact normalized `email` and no `oidc_sub` yet: attach
   `oidc_sub`/`oidc_iss` to that account (first-time link). This lets a
   bootstrap admin (created by email) sign in with Google without creating a
   duplicate. Safe because Google asserted `email_verified`.
3. **Else create** — insert a new `User` with `email`, `name` (from the `name`
   claim if present), `oidc_sub`, `oidc_iss`, `is_admin=False`,
   **`status='pending'`**.

### Access check (applied after resolution, and on every request)

- `status == 'active'` → issue/keep session, normal access.
- `status == 'pending'` → **no session**; render the **"awaiting approval"**
  holding page.
- `status == 'disabled'` → **no session**; render a generic **"this account does
  not have access — contact an administrator"** holding page. (Deny and disable
  both land here, so the wording must not assume the account was ever active.)

Central enforcement lives in `get_current_user`: it already loads the user and
checks `session_epoch`; it gains a `status == 'active'` check and, when not
active, signals the holding page (so even a previously-issued cookie stops
working the moment an admin disables an account — same spirit as `session_epoch`).
`require_admin` continues to layer the `is_admin` check on top.

## Admin approval UI (`web/routes/admin.py`, `require_admin`)

- **Pending queue** — `GET /admin/users/pending`: list `status='pending'` users
  (email, name, when, Google-linked?), each with **Approve as member**, **Approve
  as admin**, and **Deny**.
- **Approve** — `POST /admin/users/{id}/approve` (form field `role=member|admin`):
  set `status='active'`, `is_admin` per role, `approved_by_user_id`/`approved_at`.
- **Deny** — `POST /admin/users/{id}/deny`: set `status='disabled'` (no access;
  reversible).
- **Disable / re-enable / change role** — on the existing user-management list:
  toggle `status` between `active`/`disabled` and toggle `is_admin`. Disabling
  also bumps `session_epoch` to kill live sessions immediately.

## Configuration (`config.py`)

| env var | default | notes |
|---|---|---|
| `GOOGLE_OIDC_ENABLED` | `false` | Master switch; when false, the Google button is hidden and routes 404. |
| `GOOGLE_CLIENT_ID` | — | Required when enabled. |
| `GOOGLE_CLIENT_SECRET` | — | Required when enabled. |
| `GOOGLE_OIDC_DISCOVERY_URL` | `https://accounts.google.com/.well-known/openid-configuration` | Discovery-driven; swappable for a broker later. |

- **Redirect URI is derived**: `APP_BASE_URL.rstrip('/') + '/auth/google/callback'`
  (registered in the Google Cloud console). Not a separate env var, to avoid drift.
- **Production guard** (mirrors existing https checks): if `GOOGLE_OIDC_ENABLED`
  and `client_id`/`client_secret` are missing → refuse to start.
- `.env.example` updated with the new keys (commented, disabled by default).

## Login page

Add a **"Continue with Google"** control to `login.html` — a same-origin `<form
method="post" action="/auth/google/start">` button (no inline JS; CSP-safe),
shown only when `GOOGLE_OIDC_ENABLED`. Magic-link email form remains. Carry
`next` through both. New templates: `account_pending.html` (awaiting approval)
and `account_no_access.html` (generic "no access — contact an administrator",
shown for both denied and disabled accounts).

## Security specifics

- **PKCE S256** mandatory; `code_verifier` never leaves the server except as the
  hashed challenge.
- **`state`** verified at callback (CSRF); **`nonce`** verified in the `id_token`
  (replay/injection); both single-use via the one-shot transaction cookie.
- **`id_token` validation:** signature (JWKS w/ key rotation handled by Authlib),
  `iss`, `aud`, `exp`/`iat`, `nonce`. **`email_verified` required.**
- **Returning-user match is by `(oidc_iss, oidc_sub)`**, the stable identifier —
  not by email (email can change at the IdP).
- **Transaction cookie** is HttpOnly/Secure/`SameSite=Lax`/path-scoped/short-TTL
  and deleted on use. `Lax` is correct: the callback is a top-level GET.
- **No code leak:** callback always 303s; never renders the code-bearing URL.
- **Central status enforcement** means disabling an account revokes access on the
  next request even with a valid cookie (plus `session_epoch` bump for instant
  kill).
- Existing CSRF / security-header / CSP middleware is unchanged and sufficient;
  no CSP relaxations are required.

## Testing

pytest, with Google's discovery / JWKS / token endpoints **mocked** (no live
network). Cover:

- PKCE/`state`/`nonce` generation and **callback validation** (mismatch → reject).
- `id_token` claim validation: bad `iss`/`aud`/`exp`/`nonce` → reject;
  `email_verified=false` → holding page.
- **New user → `pending`**, no session, holding page.
- **Admin approve → active** (member and admin), then sign-in succeeds.
- **Deny / disable** → no access even with a previously valid cookie.
- **Account linking:** existing-by-email user gets `oidc_sub` attached, no dup.
- **Returning user matched by `sub`** (email change at IdP still resolves).
- **Regression: magic-link no longer auto-creates**; magic-link works for active
  users; unknown email issues no token but returns the identical page.
- Admin pending-queue routes are `require_admin`-gated.

## Scope boundaries

**In scope:** Google OIDC + PKCE routes; `users` status/oidc columns + migration;
account resolution + central access enforcement; admin approval queue + disable;
magic-link auto-create removal; config + `.env.example`; login-page button +
holding pages; tests.

## Non-goals (explicit, deferred)

- **LDAP / Active Directory internal SSO** and group-based provisioning — a
  separate future sub-project (the hospital's internal path). The
  discovery-URL-driven OIDC client keeps the door open to route this through a
  broker later without reworking the approval logic.
- **External identity broker** (Keycloak / Authentik / Zitadel) — considered;
  deferred to avoid a second on-prem service now.
- **Multiple OIDC providers** beyond Google; **richer role taxonomy** beyond
  member/admin; **self-service role requests**; account self-deletion.
