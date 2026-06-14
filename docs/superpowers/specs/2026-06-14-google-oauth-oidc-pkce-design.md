# Google Accounts + OIDC + OAuth 2.0 Authorization Code with PKCE — Design

**Status:** approved design (brainstorm 2026-06-14), revised after Codex review
(2026-06-14, all findings incorporated). Ready for an implementation plan.
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

## Decisions

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
6. **No automatic account linking.** A Google sign-in is resolved to an account
   **only** by its stable `(oidc_iss, oidc_sub)` pair. We never link a Google
   identity to a pre-existing account by matching email — Google itself warns that
   third-party email ownership can change while still reporting
   `email_verified=true` (revised per Codex). Linking an existing account to
   Google is an **explicit, already-authenticated** "Connect Google" action (see
   §6). This also sidesteps the `users.email` UNIQUE constraint, which a
   create-on-email-match rule would have violated.
7. **OAuth transaction state** (`state`, `nonce`, PKCE `code_verifier`, `next`,
   optional link-intent) lives in a short-lived **encrypted** cookie — *not merely
   signed*. The existing `SessionSigner` docstring
   ([`session_cookie.py:21`](../../../src/call_manager/auth/session_cookie.py#L21))
   explicitly forbids stashing sensitive state in a signed-only cookie, and a PKCE
   `code_verifier` is exactly that. We keep statelessness (no server-side
   OAuth-state table) but encrypt the payload (revised per Codex).
8. **Library:** **Authlib**, used via its **lower-level Jose/OAuth primitives**
   (`authlib.jose` for JWKS + `id_token` validation; manual PKCE + code exchange
   over the already-vendored `httpx`). We do **not** use Authlib's Starlette
   `SessionMiddleware` integration — it would conflict with our hand-rolled cookie
   model (revised per Codex).

## What already exists (build on, don't rebuild)

- **Magic-link auth** — `web/routes/auth.py`. `login_submit` currently
  **auto-creates** a `User` for any allowed-domain email
  ([`auth.py:163-170`](../../../src/call_manager/web/routes/auth.py#L163-L170)).
  `login_verify_consume`
  ([`auth.py:272`](../../../src/call_manager/web/routes/auth.py#L272)) loads the
  user and issues a session **unconditionally** — no status gate. Single-use
  sha256 tokens via `auth/tokens.py`. Scanner-proof GET interstitial with
  per-handler `Referrer-Policy` override.
- **Session cookies** — `auth/session_cookie.py` (`SessionSigner`, `itsdangerous`
  HMAC, payload `{uid, ep}`, signed-not-encrypted). `cookie_secure` from https
  `APP_BASE_URL`.
- **Current-user dependency** — `auth/dependencies.py`: `get_current_user`
  (verifies cookie, loads user, checks `session_epoch`) and `require_admin`.
- **Error handling** — `main.py`
  ([`main.py:285-298`](../../../src/call_manager/main.py#L285-L298)): an
  `HTTPException` handler redirects HTML 401 → `/login`, renders styled HTML for
  `{400,403,404,409,413}`, and returns JSON to API/HTMX-partial callers.
- **User model** — `db/models.py`: `id, email (unique, lowercased), name,
  is_admin, created_at, last_login_at, digest_*, session_epoch,
  default_profile_id`.
- **Admin bootstrap** — `admin_bootstrap.py`
  ([`admin_bootstrap.py:47-54`](../../../src/call_manager/admin_bootstrap.py#L47-L54)),
  run at startup from [`main.py:69`](../../../src/call_manager/main.py#L69):
  creates/promotes `INITIAL_ADMIN_EMAILS` as admins (does not set status).
- **Digest** — `digest/send.py`
  ([`send.py:64-76`](../../../src/call_manager/digest/send.py#L64-L76)): selects
  recipients by `digest_disabled_at IS NULL` only — it **bypasses**
  `get_current_user`, so account status is not consulted.
- **Rate limiters** — `app.state.ip_limiter` / `email_limiter`
  (`SlidingWindowLimiter`), already used by the login routes.
- **Config / middleware / admin UI** — pydantic `Settings` with prod guards;
  CSRF (Origin/Referer) + security headers + CSP (`script-src 'self'`); admin
  router under `require_admin`.

## The one rule

> **No account gets access without explicit admin approval.** Authentication
> (Google OIDC or magic-link) only proves *who* a person is. Authorization — does
> this person have access, member or admin — is decided by the admin and enforced
> centrally in `get_current_user`, plus every non-request data path (digests).

## Data model changes

### `users` — new columns (one Alembic migration)

| column | type | notes |
|---|---|---|
| `status` | text NOT NULL, CHECK in (`pending`,`active`,`disabled`) | New self-service accounts default `pending`. `disabled` revokes. |
| `oidc_sub` | text, nullable | Google `sub` claim. |
| `oidc_iss` | text, nullable | Issuer (`https://accounts.google.com`). |
| `approved_by_user_id` | bigint FK → users.id, nullable | Audit. |
| `approved_at` | timestamptz, nullable | Audit. |

Constraints (defense-in-depth, per Codex):
- **Partial UNIQUE** on `(oidc_iss, oidc_sub)` where both non-null — one Google
  identity ↔ at most one account.
- **CHECK** paired-null: `(oidc_iss IS NULL) = (oidc_sub IS NULL)`.
- **CHECK** `status IN ('pending','active','disabled')`.

`is_admin` retained as the role flag (member=`false`, admin=`true`). No new tables.

**Migration backfill:** existing rows → `status='active'` (they predate the gate
and are already trusted). New rows default to `pending` at the application layer;
only the migration backfill and the bootstrap flow create `active` users.

## Authentication flows

### A. Google OIDC + PKCE — sign-in / sign-up

New router `web/routes/oauth_google.py`. Both endpoints are **rate-limited** by
`ip_limiter` (per Codex abuse-controls).

1. **`POST /auth/google/start`** (same-origin form POST; optional `next`; optional
   link-intent when an authenticated active user initiates from account settings):
   - 404 when `GOOGLE_OIDC_ENABLED` is false.
   - Load OIDC metadata from the discovery document (cached).
   - Generate PKCE `code_verifier` + `code_challenge` (**S256**), random `state`,
     random `nonce`.
   - Write `{state, nonce, code_verifier, next (validated), link_uid?}` to the
     **encrypted transaction cookie** (HttpOnly, Secure per `cookie_secure`,
     `SameSite=Lax`, `Max-Age≈600s`, `Path=/auth/google`, **distinct key context**
     from the session cookie).
   - **303** to `authorization_endpoint` with `response_type=code`, `client_id`,
     `redirect_uri`, `scope=openid email profile`, `state`, `nonce`,
     `code_challenge`, `code_challenge_method=S256`.

2. **`GET /auth/google/callback`** (top-level GET back from Google; `SameSite=Lax`
   cookie is sent). **One clean-redirect contract** (per Codex): *every* outcome
   first **deletes the transaction cookie**, then **303-redirects** to a clean GET
   route — never renders HTML containing `?code=`/`?error=`. Outcomes:

   | outcome | detection | redirect |
   |---|---|---|
   | provider error | `?error=` present | `/auth/error` |
   | missing/garbled params | no `code`/`state` | `/auth/error` |
   | no/invalid txn cookie | decrypt fails / absent | `/auth/error` |
   | `state` mismatch | cookie vs query | `/auth/error` |
   | token exchange / JWKS / network failure | exception | `/auth/error` |
   | `id_token` invalid | sig/`iss`/`aud`/`azp`/`exp`/`nonce` | `/auth/error` |
   | `email_verified != true` | claim | `/auth/error` |
   | email matches an **existing** account, identity not linked | resolution §“Account resolution” | `/auth/error` (message: sign in your usual way, then Connect Google) |
   | **new** account created | resolution | `/auth/pending` |
   | resolved account `pending` | resolution | `/auth/pending` |
   | resolved account `disabled` | resolution | `/auth/no-access` |
   | resolved account `active` | resolution | set session cookie → `next` (safe) or `/` |

   - Token exchange and JWKS fetch are server-to-server over `httpx`; **`id_token`
     validation** = signature via Google JWKS (rotation handled by `authlib.jose`),
     `iss` match, `aud` contains our `client_id` (**accept array-valued `aud`**),
     **validate `azp` == client_id when `aud` is multi-valued or `azp` present**,
     `exp`/`iat` fresh, `nonce` == cookie nonce.
   - On success the 303 sets the session cookie via the existing `SessionSigner`
     and carries `Referrer-Policy: no-referrer`.

3. **`GET /auth/pending`, `/auth/no-access`, `/auth/error`** — session-less GET
   pages (templates: `account_pending.html`, `account_no_access.html`,
   `auth_error.html`). Reachable without a cookie; no side effects.

### B. Connect Google (explicit linking) — replaces auto-link

- On the account page, an **already-authenticated `active` user** can start the
  same flow with link-intent (`link_uid` = their id, carried in the encrypted
  txn cookie).
- In the callback, after full `id_token` validation: if `(iss, sub)` is unused,
  attach it to `link_uid`'s account and redirect to account settings; if `(iss,
  sub)` is already linked to another account → `/auth/error`. Email match is **not**
  required — the user is authenticated and asserting ownership of that Google
  account.
- This is how a bootstrap admin (created by email, no Google link) enables Google
  login: sign in via magic-link once, then Connect Google.

### C. Magic-link — login only (changed)

- Remove the auto-create branch
  ([`auth.py:167-170`](../../../src/call_manager/web/routes/auth.py#L167-L170)): a
  token is issued **only** for an existing `active` user. The response body stays
  identical on every branch ("check your email"); no token is issued for
  unknown/non-active emails. **Residual risk:** because an active account now does
  extra work (token write + synchronous email send) while unknown/non-active
  emails return early, a *timing* side-channel exists that the old auto-create
  flow did not have. It is bounded by the per-email (5/15 min) and per-IP (30/15
  min) limiters; fully closing it (constant-time / off-request email dispatch) is
  noted as future work, not done here. We therefore claim *body*-level
  enumeration resistance, not timing-level.
- **Re-check status at consume** (per Codex): `login_verify_consume`
  ([`auth.py:272`](../../../src/call_manager/web/routes/auth.py#L272)) must verify
  `status == 'active'` **immediately before issuing the session** — a user can be
  active when the link is mailed and disabled before it is clicked. Non-active →
  redirect to `/auth/no-access` (or `/auth/pending`); no session.

## Account resolution (callback)

Given a validated `(iss, sub, email)`:

1. **Match by identity** — user with this `(oidc_iss, oidc_sub)` → that account;
   update `last_login_at`; go to the access check.
2. **Email collision, no link** — else if any account exists with this normalized
   `email` → **do not create, do not link** → `/auth/error` instructing them to
   sign in their usual way and Connect Google. (Avoids the email-trust hazard and
   the UNIQUE-email crash.)
3. **Create** — else insert a new `User`: `email`, `name` (from claim if present),
   `oidc_sub`, `oidc_iss`, `is_admin=False`, **`status='pending'`**.

**Race-safety** (per Codex): step 3 inserts under the partial-unique
`(oidc_iss, oidc_sub)` index; on `IntegrityError` (a concurrent first login, or a
concurrent email insert) the handler **rolls back and re-resolves from step 1**,
which now finds the winning row (or routes to step 2). No lost updates, no
duplicate identities.

### Access check (after resolution, and on every request)

- `active` → issue/keep session, normal access.
- `pending` → no session → `/auth/pending`.
- `disabled` → no session → `/auth/no-access`.

## Central enforcement of inactive accounts (per Codex)

`get_current_user` already loads the user and checks `session_epoch`. It gains a
status gate: a **non-active** user raises a dedicated `AccountInactive(status)`
exception (distinct from the 401 "no session" path, so a pending user is **not**
bounced to `/login` in a loop). A new handler:

- **HTML navigation** (`Accept: text/html`, not an HTMX partial): **delete the
  session cookie** and 303 → `/auth/pending` or `/auth/no-access` per status.
- **API / HTMX-partial**: **403** JSON (consistent with `require_admin`).

This means disabling an account revokes access on its **next request** even with a
still-valid cookie; admin disable additionally bumps `session_epoch` for an
instant kill of live sessions.

## Disabling propagates to digests (per Codex)

`digest/send.py` recipient selection
([`send.py:64-76`](../../../src/call_manager/digest/send.py#L64-L76)) currently
filters only on `digest_disabled_at`. Add **`User.status == 'active'`** to the
candidate query (and as a guard in per-user assembly). Regression test: a
`disabled` subscribed user receives no digest.

## Admin approval UI (`web/routes/admin.py`, `require_admin`)

- **Pending queue** — `GET /admin/users/pending`: list `status='pending'`
  (email, name, when, Google-linked?), **paginated/capped** (per Codex
  abuse-controls), each with **Approve as member**, **Approve as admin**, **Deny**.
- **Approve** — `POST /admin/users/{id}/approve` (`role=member|admin`): set
  `status='active'`, `is_admin` per role, `approved_by_user_id`/`approved_at`.
- **Deny** — `POST /admin/users/{id}/deny`: set `status='disabled'` (reversible).
- **Disable / re-enable / change role** — on the existing user list. Disable bumps
  `session_epoch`.
- **Last-admin guard** (per Codex): refuse (400) to disable or demote the **final
  remaining active admin**; tested.

## Admin bootstrap (per Codex)

`bootstrap_admins` ([`admin_bootstrap.py:47-54`](../../../src/call_manager/admin_bootstrap.py#L47-L54))
is updated to ensure configured admins are **`active` AND `is_admin`** on startup —
create if missing (status `active`), and **re-activate + re-promote** an existing
one if it was disabled/demoted. This makes `INITIAL_ADMIN_EMAILS` the documented
**lockout-recovery** mechanism. Trade-off (documented in code + `.env.example`): a
configured admin cannot be permanently disabled without first removing them from
`INITIAL_ADMIN_EMAILS`. Domain-allowlist gating of bootstrap entries is unchanged.

## Configuration (`config.py`)

| env var | default | notes |
|---|---|---|
| `GOOGLE_OIDC_ENABLED` | `false` | Master switch; button hidden + routes 404 when off. |
| `GOOGLE_CLIENT_ID` | — | Required when enabled. |
| `GOOGLE_CLIENT_SECRET` | — | Required when enabled. |
| `GOOGLE_OIDC_DISCOVERY_URL` | `https://accounts.google.com/.well-known/openid-configuration` | Discovery-driven; swappable for a broker later. |

- **Redirect URI derived**: `APP_BASE_URL.rstrip('/') + '/auth/google/callback'`
  (registered in the Google Cloud console). No separate env var (avoids drift).
- **Production guard** (mirrors existing https checks): `GOOGLE_OIDC_ENABLED` with
  missing client id/secret → refuse to start.
- The transaction-cookie encryption key is derived from the existing
  `SESSION_SECRET` via HKDF with a **distinct info label** (no new secret to
  manage; never reuses the session signer's key/salt).
- `.env.example` updated (new keys, commented/disabled; bootstrap-reactivation note).

## Login page & templates

Add a **"Continue with Google"** control to `login.html` — a same-origin `<form
method="post" action="/auth/google/start">` button (no inline JS; CSP-safe), shown
only when `GOOGLE_OIDC_ENABLED`. Magic-link form stays; `next` carried through both.
New templates: `account_pending.html`, `account_no_access.html`, `auth_error.html`.
Account page gains a **Connect Google** button for authenticated active users.

## Security specifics (summary)

- **PKCE S256**; `code_verifier` only ever in the **encrypted** txn cookie.
- **`state`** (CSRF) verified at callback; **`nonce`** verified in `id_token`;
  both single-use via the one-shot cookie. **`next` re-validated** with `safe_next`
  *after* restoring it from the cookie (don't trust restored state).
- **`id_token`**: JWKS signature, `iss`, `aud` (array-aware), `azp` when required,
  `exp`/`iat`, `nonce`; **`email_verified` required**.
- **Returning-user match by `(oidc_iss, oidc_sub)`**, never by email.
- **No code leak**: callback always 303s; `Referrer-Policy: no-referrer`.
- **Central status enforcement** + `session_epoch` bump on disable.
- **Multi-tab note** (documented behavior): a second `start` overwrites the txn
  cookie, so a first tab's callback fails the `state` check and the user simply
  re-initiates — acceptable, no security impact.
- **Rate-limited** OAuth endpoints; **paginated** pending queue.
- Existing CSRF / security-header / CSP middleware unchanged; no CSP relaxations.

## Testing

pytest, Google discovery/JWKS/token endpoints **mocked** (no live network):

- PKCE/`state`/`nonce` generation; callback `state`-mismatch → `/auth/error`.
- `id_token` validation: bad `iss`/`aud`/`azp`/`exp`/`nonce` → `/auth/error`;
  `email_verified=false` → `/auth/error`; **array-valued `aud`** accepted.
- Callback clean-redirect contract: provider error, missing params, bad/absent
  cookie, token-exchange failure, JWKS/network failure → all delete cookie + 303
  `/auth/error`.
- **New user → `pending`** (no session) → `/auth/pending`.
- **Email-collision → `/auth/error`** (no row created, no link).
- **Connect Google** links `(iss,sub)` to the authenticated account; re-link of an
  already-used identity → `/auth/error`.
- **Concurrent first-login race** → single account, second insert re-resolves.
- **Admin approve → active** (member + admin), then sign-in succeeds; **deny /
  disable → no access** even with a previously valid cookie (HTML → no-access page
  + cookie cleared; HTMX/API → 403).
- **Last-admin guard**: cannot disable/demote the final active admin.
- **Bootstrap reactivation**: a disabled configured admin is re-activated on
  startup.
- **Magic-link**: no longer auto-creates; works for active users; **disabled
  after issuance → no session at consume**; unknown email issues no token but
  returns the identical page.
- **Digest**: a `disabled` subscribed user receives no digest.
- DB constraints: paired-null and `status` CHECK reject bad rows.

## Scope boundaries

**In scope:** Google OIDC + PKCE routes (start/callback + holding/error routes);
Connect-Google linking; `users` status/oidc columns + constraints + migration;
race-safe account resolution; central inactive-account enforcement; digest active
filter; magic-link auto-create removal + consume-time status check; admin approval
queue + disable + last-admin guard; bootstrap reactivation; config + encrypted
txn-cookie helper + `.env.example`; login button, Connect-Google button, holding
pages; tests.

## Non-goals (explicit, deferred)

- **LDAP / Active Directory internal SSO** and group-based provisioning — a
  separate future sub-project. The discovery-URL-driven OIDC client keeps the door
  open to route this through a broker later without reworking the approval logic.
- **External identity broker** (Keycloak / Authentik / Zitadel) — considered;
  deferred to avoid a second on-prem service now.
- **Multiple OIDC providers** beyond Google; **richer role taxonomy** beyond
  member/admin; **`hd`-based domain auto-approval** (if domain filtering is added
  later, validate the Google `hd` claim, **not** the email suffix — per Codex);
  **self-service role requests**; account self-deletion.
