# Local Password Auth + Magic-Link Removal — Design

**Status:** approved design (brainstorm 2026-06-15), revised after Codex review
(2026-06-15, all findings incorporated). Ready for an implementation plan.
**Stack:** FastAPI + Jinja2 + HTMX, PostgreSQL + SQLAlchemy 2.x + Alembic, `uv`/`ruff`/`pytest`.
**Relationship to prior work:** revises the authentication approach from
[2026-06-14-google-oauth-oidc-pkce-design.md](2026-06-14-google-oauth-oidc-pkce-design.md).
The Google **OAuth2/OIDC** method, the **approval gate** (`users.status`
pending/active/disabled), and the **admin user-management** UI carry over. This
spec **removes magic-link** and **adds local email+password**.

## Goal

Replace passwordless magic-link login with **local email + password** auth,
alongside **Google OAuth2**, behind the existing admin-approval gate. Identity for
local accounts is established by **admin-issued credentials** (no email
verification, since email is being dropped). Provide a **bootstrap admin** that
signs in with credentials before any other accounts exist. **LDAP is deferred.**

## Decisions

1. **Two methods:** local **email + password** and **Google OAuth2**. Email is the
   single shared identifier (`users.email`). LDAP out of scope.
2. **Self-registration is a request, not a credential** (revised per Codex
   critical). `/register` collects email only (+ optional name/reason) and creates
   a `pending` account with **no password**. The applicant never sets a trusted
   credential — this prevents pre-registering someone else's address with an
   attacker-chosen password.
3. **Admin issues the credential on approval.** Approving a local (non-OAuth)
   pending account requires the admin to set an **initial password**, handed to
   the (out-of-band identity-verified) person; they change it after first login.
4. **Magic-link removed** (routes, tokens, table, templates, and the magic-link
   *use* of the email sender) — but **`auth/email.py` is retained** (ingestion
   alerts + CLI depend on it; see Removal scope).
5. **Bootstrap admin via env:** `INITIAL_ADMIN_EMAIL` (existing) +
   `INITIAL_ADMIN_PASSWORD` (new). While set, the configured admins' password is
   (re)applied every startup (break-glass). **Reject** a non-empty value shorter
   than 12 chars; an empty string is treated as **unset**.
6. **Every credential change bumps `session_epoch`** (revoking outstanding
   cookies); an own-password change also mints a fresh replacement cookie.
7. **Hashing: argon2id** (`argon2-cffi`).

## What carries over (do not rebuild)

- **Google OIDC + PKCE** — `auth/oidc.py`, `auth/oauth_txn.py`, `auth/accounts.py`,
  `web/routes/oauth_google.py`, holding/error pages, `Connect Google` on `/me`.
- **Approval gate** — `users.status` (fail-closed default `pending`), central
  enforcement in `get_current_user` (`AccountInactive`), digest `status='active'`
  filter, last-admin guard.
- **Session cookies** — `auth/session_cookie.py` (`SessionSigner`), `session_epoch`
  revocation. **CSRF / security headers / rate limiters** unchanged.

## Data model changes

One Alembic migration (**`0049`**; head is `0048`):

- `users`: **add `password_hash`** `Text`, **nullable** (OAuth2-only accounts and
  not-yet-approved local requests have none).
- **Drop the `magic_link_tokens` table**; remove the `MagicLinkToken` model + the
  `User.magic_link_tokens` relationship.
- `status`/`oidc_*`/approval columns unchanged.

`downgrade()` re-creates `magic_link_tokens` (per migration `0001`) and drops
`password_hash`.

## Registration (access request)

New `web/routes/register.py`:

- **`GET /register`** — email (+ optional display name) form; link back to `/login`.
- **`POST /register`** — rate-limited (per-IP + per-email):
  1. Normalize email; enforce the **domain allowlist** (`domain_allowed`).
  2. Insert a `pending` account with **no `password_hash`** and no oidc identity.
     Use **insert-then-catch `IntegrityError`** (mirrors `resolve_google_account`)
     to stay race-safe against a concurrent local/Google registration.
  3. On a duplicate email (or the caught `IntegrityError`), return the **same**
     "request received — pending approval" response and create nothing — and still
     perform an **argon2 dummy hash** so the duplicate path costs the same as a
     fresh insert (no existence leak by timing).
  4. Render the "request received / awaiting approval" page.

A pending request grants **no access** and holds **no usable credential** until an
admin approves it and sets a password.

## Login

Rewrite `web/routes/auth.py` (`login_submit` → password check; delete the
`/login/verify` magic-link routes):

- **`GET /login`** — email + password form; **Register** link; **Continue with
  Google** (when OIDC enabled).
- **`POST /login`** — rate-limited (per-IP + per-email):
  1. Look up the user by normalized email.
  2. **Verify** with argon2id. On **every** path lacking a valid local credential
     — unknown email, account with `password_hash IS NULL` (pending request or
     OAuth-only), or wrong password — run a verify against a fixed **dummy hash**
     and return the **same** "invalid email or password". No enumeration by
     response or timing.
  3. On valid credentials, branch on `status`: `active` → set session cookie →
     safe `next`; `pending` → `/auth/pending`; `disabled` → `/auth/no-access`
     (mirrors the OIDC callback).

**Brute-force protection:** reuse the existing per-IP (30/15 min) and per-email
(5/15 min) `SlidingWindowLimiter`s on `POST /login` and `POST /register`. No new
lockout state (single-instance constraint makes in-memory limiters sufficient).

## Admin approval & user management (extends existing `/admin/users`)

- **Approve** (`POST /admin/users/{id}/approve`) now takes `role` **and an optional
  `initial_password`**:
  - Account **without** an oidc identity (local request): an initial password is
    **required** (else the user could never log in). Validate ≥ 12 chars, hash,
    store; the admin communicates it out-of-band; set `status='active'`, role,
    approval audit.
  - Account **with** an oidc identity (Google pending): `initial_password`
    optional (they log in via Google). Activate as today.
- **Deny / disable / enable / role-change / last-admin guard** — unchanged.
- **Admin password reset** (`POST /admin/users/{id}/reset-password`): set a new
  password (≥ 12), **bump `session_epoch`** (kills the target's live sessions). No
  forced change on next login (MVP).

## Password management (`/me`)

- **Change own password** — requires the **current** password; validate new
  (≥ 12), re-hash, **bump `session_epoch`**, and **mint a fresh session cookie**
  for the current browser (so the user isn't logged out by their own change).
- **OAuth-only accounts (no `password_hash`) cannot self-set a first password** —
  `/me` change-password requires a current credential to verify, which they don't
  have. If such a user wants local login, an **admin grants it via reset**. This
  removes any session-only path to minting a permanent local credential (a stolen
  session can't be upgraded into a standing password).
- **No self-service "forgot password"** (needs email): recovery is admin reset or
  the bootstrap break-glass.

## Bootstrap admin (credential break-glass)

Extend `admin_bootstrap.py` (startup):

- For each `INITIAL_ADMIN_EMAIL` (domain-allowlisted): ensure the user exists,
  `is_admin=True`, `status='active'` (existing behavior).
- **`INITIAL_ADMIN_PASSWORD`** handling: empty/whitespace → treated as unset. A
  non-empty value **shorter than 12 chars → startup error** (no trivial admin
  credential). When valid, set each configured admin's `password_hash` to
  `argon2(INITIAL_ADMIN_PASSWORD)` every startup (idempotent) and bump
  `session_epoch`. Unset it to manage passwords in-app.
- Documented trade-off (code + `.env.example`): while set, the admin password is
  the env value, re-applied each startup.

## Magic-link removal scope

Delete:
- Routes `login_verify_confirm` / `login_verify_consume`; rewrite `login_submit`
  as the password check.
- `auth/tokens.py`; the `MagicLinkToken` model + relationship + table (migration
  `0049`).
- Templates `login_sent.html`, `login_confirm.html`, `login_link_expired.html`.
- Magic-link tests (`test_auth_tokens.py`, magic-link cases in `test_web_auth*`).

**Retain (corrected per Codex):** `auth/email.py` and the
`app.state.email_sender` wiring — they are used by **ingestion alerts**
(`ingestion/alerts.py`, injected at `main.py`) and the **ingestion CLI**
(`ingestion/cli.py`), not just magic-link. Changes there:
- Remove the magic-link `email_sender.send(...)` call in the login flow.
- **Relax the "refuse to start without an email sender" prod guard** in
  `create_app()`: it existed because console-logging a *magic link* leaked a live
  credential. With magic-link gone, fall back to `ConsoleEmailSender` (or a no-op)
  for best-effort alerts instead of hard-failing. Keep `RESEND_API_KEY`/`SMTP_HOST`
  config for alert delivery.

## Login page & templates

`login.html`: email + password + **Register** link + **Continue with Google**
(when enabled); magic-link form removed. New `register.html`. Holding pages
(`account_pending`, `account_no_access`, `auth_error`) unchanged.

## Configuration

- **Add** `INITIAL_ADMIN_PASSWORD` (str | None; empty → unset; non-empty < 12 →
  startup error).
- **Keep** `RESEND_API_KEY`/`SMTP_HOST`/`EMAIL_FROM` (ingestion alerts).
- **Relax** the create_app email-sender prod requirement (above).
- `.env.example`: document `INITIAL_ADMIN_PASSWORD` (+ "unset after first login").

## Security

- **argon2id**; `password_hash` never logged.
- **No enumeration** — generic auth errors; dummy-argon2 on all no-valid-credential
  login paths and the duplicate-registration path (timing parity).
- **Race-safe registration** — insert-then-catch `IntegrityError`.
- **Min password length ≥ 12** everywhere (registration-issued, admin set/reset,
  own change, bootstrap).
- **Revocation** — `session_epoch` bumped on every credential change (own,
  admin-reset, bootstrap, disable); own-change re-mints the cookie.
- **No session-only privilege escalation** — there is no self-set-first-password
  path; OAuth-only accounts get a local password only via admin reset.
- Cookies / CSRF / security headers / CSP unchanged.

## Known residual (documented, not blocking)

A pending **local request** for `victim@ki.se` occupies that email, so the real
victim's first Google sign-in hits `email_collision` and is refused. Mitigation:
the admin denies/deletes the unexpected pending request (it has no access and no
credential), freeing the address. *Optional future enhancement:* let a verified
Google sign-in **claim** a pending, credential-less, oidc-less request of the same
email (Google proves ownership) — not in this scope.

## Testing

pytest (testcontainers Postgres):
- Register → `pending`, **no `password_hash`**; duplicate-email request returns
  the identical body, creates no row, and exercises the dummy-hash path; concurrent
  insert hits the `IntegrityError` branch; domain-disallowed rejected.
- Approve a local request **requires** an initial password (≥ 12) → user can then
  log in; approving a Google-pending account works **without** a password.
- Login: valid active → session; wrong password / unknown email / OAuth-only
  (no hash) / pending-request (no hash) → identical generic error; `pending` →
  `/auth/pending`; `disabled` → `/auth/no-access`.
- Own-password change requires current, bumps epoch, re-mints cookie; an
  OAuth-only account (no hash) cannot self-set a password via `/me`; admin reset
  grants one and bumps epoch.
- Bootstrap: valid `INITIAL_ADMIN_PASSWORD` logs in + re-applies on a second run;
  empty → unset (untouched); non-empty < 12 → startup error.
- Rate-limit on repeated bad logins.
- **Regression:** no `/login/verify`, no `magic_link_tokens`, no `MagicLinkToken`;
  ingestion alerts still construct/send via the retained sender; OIDC flow + gate
  still pass; migration `0049` up/down round-trips.

## Out of scope (deferred)

LDAP (next sub-project); MFA; email-based password reset / "forgot password";
auth-related email delivery (registration/approval notifications); Google-claims-
pending-request auto-link.
