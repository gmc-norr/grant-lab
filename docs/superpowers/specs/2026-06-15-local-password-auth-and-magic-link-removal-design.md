# Local Password Auth + Magic-Link Removal — Design

**Status:** approved design (brainstorm 2026-06-15). Ready for an implementation plan.
**Stack:** FastAPI + Jinja2 + HTMX, PostgreSQL + SQLAlchemy 2.x + Alembic, `uv`/`ruff`/`pytest`.
**Relationship to prior work:** revises the authentication approach from
[2026-06-14-google-oauth-oidc-pkce-design.md](2026-06-14-google-oauth-oidc-pkce-design.md).
The Google **OAuth2/OIDC** method, the **approval gate** (`users.status`
pending/active/disabled), and the **admin user-management** UI all carry over
unchanged. This spec **removes magic-link** and **adds local email+password**.

## Goal

Replace passwordless magic-link login with **local email + password** auth, kept
alongside **Google OAuth2**, all behind the existing admin-approval gate. Provide
a **bootstrap admin** that can sign in with credentials before any OAuth2/
registered users exist. **LDAP is deferred** to a later sub-project.

## Decisions (from the brainstorm)

1. **Two auth methods:** local **email + password** and **Google OAuth2**. Email
   is the single shared identifier (`users.email`). LDAP is out of scope.
2. **Self-registration → pending → admin approve.** Anyone (within the email
   domain allowlist) can register; the account lands `status='pending'` with no
   access until an admin approves it.
3. **Magic-link is removed entirely** (routes, tokens, table, templates, and the
   auth-side email sender).
4. **Bootstrap admin via env:** `INITIAL_ADMIN_EMAIL` (existing) +
   `INITIAL_ADMIN_PASSWORD` (new). While `INITIAL_ADMIN_PASSWORD` is set, the
   configured admins' password is **(re)set to it on every startup** — a reliable
   break-glass; unset it once you manage passwords in-app.
5. **No email-based password reset** (email is being dropped). Recovery is
   **admin reset** or the bootstrap break-glass. Admin reset does **not** force a
   change on next login (MVP).
6. **Hashing: argon2id** via `argon2-cffi`.

## What carries over (do not rebuild)

- **Google OIDC + PKCE** — `auth/oidc.py`, `auth/oauth_txn.py`, `auth/accounts.py`
  (`resolve_google_account`/`link_google_identity`), `web/routes/oauth_google.py`,
  the holding/error pages, `Connect Google` on `/me`.
- **Approval gate** — `users.status` (`pending`/`active`/`disabled`,
  fail-closed default `pending`), central enforcement in `get_current_user`
  (`AccountInactive` → holding page / 403), digest `status='active'` filter.
- **Admin UI** — `/admin/users` + `/admin/users/pending`, approve/deny/disable/
  enable/role-change with the serialized last-admin guard.
- **Session cookies** — `auth/session_cookie.py` (`SessionSigner`), `session_epoch`
  revocation. **CSRF / security headers / rate limiters** — unchanged.

## Data model changes

One Alembic migration (**`0049`**, head is `0048`):

- `users`: **add `password_hash`** `Text`, **nullable** (OAuth2-only accounts have
  none).
- **Drop the `magic_link_tokens` table** and remove the `MagicLinkToken` model +
  the `User.magic_link_tokens` relationship.
- `status`/`oidc_*`/approval columns are unchanged.

`downgrade()` re-creates `magic_link_tokens` (schema per migration `0001`) and
drops `password_hash`.

## Registration flow

New `web/routes/register.py`:

- **`GET /register`** — email + password + confirm-password form. Links back to
  `/login`.
- **`POST /register`** — rate-limited (per-IP + per-email limiters):
  1. Normalize email (lowercase); enforce the **domain allowlist**
     (`domain_allowed`, same as today).
  2. Enforce **password policy** (min length ≥ 12).
  3. If the email is free: create `User(email, password_hash=argon2(pw),
     status='pending')`.
  4. If the email already exists: do **not** reveal it — return the same
     "registration received — pending approval" response (enumeration-resistant
     body). Internally, no second account is created.
  5. Show the existing **`account_pending`** holding page (or a dedicated
     "registration received" page).

## Login flow

Rewrite `web/routes/auth.py` (`login_submit` becomes a password check; the
`/login/verify` magic-link routes are deleted):

- **`GET /login`** — email + password form; **Register** link; **Continue with
  Google** button when OIDC is enabled.
- **`POST /login`** — rate-limited (per-IP + per-email):
  1. Look up the user by normalized email.
  2. **Verify** the password with argon2id. On the **unknown-email** path, run a
     verify against a fixed **dummy hash** so response time doesn't reveal whether
     the email exists.
  3. **Generic failure** — unknown email, no `password_hash` (OAuth2-only
     account), or wrong password all return the same "invalid email or password".
  4. On valid credentials: branch on `status` — `active` → set session cookie and
     redirect to a safe `next`; `pending` → `/auth/pending`; `disabled` →
     `/auth/no-access`. (Mirrors the OIDC callback's status handling.)
- **Brute-force protection:** reuse the existing per-IP (30/15 min) and per-email
  (5/15 min) `SlidingWindowLimiter`s on `POST /login` and `POST /register`. No new
  lockout columns (the single-instance constraint makes in-memory limiters
  sufficient).

## Bootstrap admin (credential break-glass)

Extend `admin_bootstrap.py` (runs at startup):

- For each `INITIAL_ADMIN_EMAIL` (domain-allowlisted as today): ensure the user
  exists, `is_admin=True`, `status='active'` (existing behavior).
- **If `INITIAL_ADMIN_PASSWORD` is set**, set each configured admin's
  `password_hash = argon2(INITIAL_ADMIN_PASSWORD)` on every startup (idempotent
  re-hash). This guarantees a credential login before any other accounts exist and
  doubles as password recovery. If unset, bootstrap leaves `password_hash`
  untouched so the admin can manage it in-app.
- Documented trade-off (in code + `.env.example`): a configured admin's password
  is controlled by the env var while it is set; unset it to manage passwords in
  the app.

## Password management

- **Change own password** — on `/me`: current password + new password (an
  OAuth2-only user with no `password_hash` may **set** one to add local login).
  Verify current (or allow set-if-none), enforce policy, re-hash.
- **Admin reset** — on `/admin/users`: admin types a new password for a user; it's
  applied immediately (no email, no forced change). Bumps `session_epoch` to
  invalidate the target's live sessions.
- **No self-service "forgot password"** (needs email, which is being dropped).

## Magic-link removal scope

Delete:
- Routes: `login_submit` (replaced), `login_verify_confirm`,
  `login_verify_consume` in `web/routes/auth.py`.
- `auth/tokens.py` (`issue_token`/`consume_token`/`hash_token`).
- `MagicLinkToken` model + `User.magic_link_tokens` relationship; the table (via
  migration `0049`).
- Templates: `login_sent.html`, `login_confirm.html`, `login_link_expired.html`.
- **Auth email sender:** `auth/email.py` (`ResendEmailSender`, `SMTPEmailSender`,
  `ConsoleEmailSender`, `InMemoryEmailSender`) **only if nothing else uses it** —
  the email-sender selection + the "refuse to start without a sender in prod"
  guard in `create_app()` exist solely for magic links and are removed.
  **Caveat (verify in implementation):** the **digest** subsystem uses its own
  `email.client.EmailClient` (`digest/send.py`); confirm the digest path does not
  import `auth/email.py` before deleting it. Email-transport env (`RESEND_API_KEY`/
  `SMTP_HOST`) is retained if (and only if) digests use it.
- Associated tests (`test_auth_tokens.py`, magic-link cases in `test_web_auth*`,
  `test_auth_email.py` if the sender is removed, etc.).

## Login page & templates

`login.html`: email + password fields, **Register** link, **Continue with Google**
(when enabled). Remove the magic-link email-only form. New `register.html`.
Holding pages (`account_pending`, `account_no_access`, `auth_error`) unchanged.

## Configuration

- **Add** `INITIAL_ADMIN_PASSWORD` (str | None).
- **Remove** the magic-link-driven email-sender requirement from `create_app()`
  (the prod refuse-to-start-without-sender check). Keep digest email config if
  digests need it.
- `.env.example`: document `INITIAL_ADMIN_PASSWORD` (+ the "unset after first
  login" note).

## Security

- **argon2id** (`argon2-cffi` defaults), `password_hash` never logged.
- **Generic auth errors** + dummy-hash timing equalization (no account
  enumeration on login; enumeration-resistant registration body).
- **Min password length ≥ 12**; bootstrap warns if `INITIAL_ADMIN_PASSWORD` is
  shorter.
- Rate limiting on login/register; `session_epoch` bump on admin password reset
  and disable.
- Cookies / CSRF / security headers / CSP unchanged.

## Testing

pytest (testcontainers Postgres):
- Register → `pending`; duplicate-email registration returns the identical body
  and creates no second row; domain-disallowed rejected.
- Login: valid active → session; wrong password → generic error; unknown email →
  generic error (and dummy-verify path exercised); OAuth2-only account (no
  `password_hash`) → generic error; `pending` → `/auth/pending`; `disabled` →
  `/auth/no-access`.
- Bootstrap: with `INITIAL_ADMIN_PASSWORD` set, the admin can log in with it; it's
  re-applied on a second bootstrap run; unset → `password_hash` untouched.
- Change-own-password (incl. set-when-none for OAuth2 user); admin reset applies +
  bumps `session_epoch`.
- Rate-limit on repeated bad logins.
- **Regression:** magic-link routes/table/model are gone (no `/login/verify`,
  no `magic_link_tokens`); Google OIDC flow and the approval gate still pass.
- Migration `0049` up/down round-trips on testcontainers.

## Out of scope (deferred)

- **LDAP** (next sub-project). **MFA.** **Email-based password reset / "forgot
  password".** **Email delivery for auth** (registration/approval notifications).
