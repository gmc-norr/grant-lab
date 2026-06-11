# Login & Digest Deep-Link Reliability — Design Spec

**Date:** 2026-06-11
**Status:** Draft for implementation
**Audience:** Engineers working on call-manager

## Purpose

The weekly digest is the product's primary re-engagement channel, and the first-run value loop just made it reach users **by default** (auto-subscribe on profile creation). But the journey from a digest email to the call it advertises is broken at the login hop. This slice repairs it so a researcher who taps a Monday-digest link actually lands on the call.

### The broken journey, with evidence

1. **The magic link is consumed on a plain GET.** `GET /login/verify?token=…` calls `consume_token` immediately ([auth.py:203](../../../src/call_manager/web/routes/auth.py#L203)) and the token is single-use. Hospital mail gateways (Microsoft Safe Links / Proofpoint / Mimecast) **pre-fetch URLs** to scan them — that GET burns the token before the human clicks, so the real click gets `400 invalid or expired link`. In such an environment this can break **every** login.

2. **The requested path is discarded on 401.** The browser-401 handler redirects to a bare `/login` and throws away the originally-requested path — its own comment admits "encoding it now means the next-URL feature is a one-line change in the login_verify handler" ([main.py:293-301](../../../src/call_manager/main.py#L293-L301)). And `login_verify` hardcodes a redirect to `/` ([auth.py:217](../../../src/call_manager/web/routes/auth.py#L217)). So a digest click from an expired session strands the user on the generic dashboard, hunting for the call they tapped.

3. **Digest links are plain authenticated URLs.** `digest.html.jinja` links straight to `{{ dashboard_url }}calls/{id}?profile={pid}` ([digest.html.jinja:14](../../../src/call_manager/digest/templates/digest.html.jinja#L14)) — every click from an expired session hits the 401 wall above. A weekly-cadence user's session is usually expired by Monday.

4. **Silent dead-ends compound it.** The expired/used-link case renders the generic `error.html` whose only CTA loops back to `/login`; the "check your email" page offers no spam/latency guidance; and a wrong-domain or typo'd email shows the same success page (the only domain hint is a hardcoded `you@ki.se` placeholder in `login.html`).

## Goals

- A magic link survives mail-scanner pre-fetch (the token is only consumed by a deliberate human action).
- A digest click from an expired session lands on the **actual call** after sign-in, not the generic dashboard.
- A user who hits an expired/used link, a typo'd domain, or a slow email recovers in one step instead of a dead-end.
- No new authentication vector, no regression to the existing enumeration-resistance and single-use guarantees.

## Non-goals (kept for later passes — per the "Core" scope decision)

- **Tokenized digest links** (signed, view-scoped tokens in each digest call-link so a phone mail-app webview opens the call with no magic-link round-trip). The real mobile fix, but a distinct bearer-token-in-email design — its own slice.
- **Session ergonomics** for shared workstations: shorter session lifetime, per-device logout, relabeling "Sign out" (which today is secretly sign-out-everywhere via `session_epoch`).
- **Phishing-resistant email-copy rewrite** (app context, requested-at time, "ignore if not you").
- Account approval/roles/offboarding (the separate, still-unimplemented 2026-06-10 user-system spec).

## Approach decision: carrying `next`

The `next` path rides through the flow as a **query parameter, validated at every consumption point** — not stored in the DB. It travels `/login?next=…` → emailed magic-link `…/login/verify?token=X&next=…` → hidden field on the interstitial → final redirect. Rejected alternative: a `next_path` column on `magic_link_tokens` (tamper-proof after issue, but needs a migration). For an internal tool where `next` is validated to a **local relative path**, the only tamper surface is "redirect to some internal page," which is harmless — so the query-param approach is chosen for its simplicity and zero migration.

## Components

### A. Scanner-proof verify (interstitial)

Split the single `GET /login/verify` into two handlers:

- **`GET /login/verify?token=…&next=…`** — renders a small **"Confirm sign-in"** page: a heading, one explanatory line, a single `<form method="post" action="/login/verify">` with a submit button and hidden `token` + `next` fields. It does **not** call `consume_token` and has **no** side effects. A mail-scanner pre-fetch (GET only) is therefore harmless — the token is untouched and still valid for the human's click.
- **`POST /login/verify`** — reads `token` + `next` from the form body, consumes the token in its own transaction (the existing `consume_token` logic, unchanged), mints the session cookie, and redirects (`303`) to the validated `next` (else `/`). The IP rate-limit currently on the GET moves here, to the verb that actually consumes.

The email link itself stays a normal `GET` URL (so it's clickable in any mail client); clicking it shows the interstitial rather than auto-completing sign-in. This adds exactly one click ("Confirm sign-in") — the accepted, standard cost of scanner-proofing.

### B. Next-URL threading

Four small touch points carry the originally-requested path end to end:

1. **401 handler** ([main.py:293-301](../../../src/call_manager/main.py#L293-L301)) — when a browser (Accept: text/html) hits 401, encode the originally-requested relative path (path + query) as `?next=<urlencoded>` on the `/login` redirect, after passing it through `_safe_next` (below).
2. **`GET /login`** — read `next` from the query, pass it to `login.html` as a hidden form field (validated; absent/invalid → omit).
3. **`POST /login`** — accept `next` as an optional form field, validate it, and thread it into the emailed magic-link URL (`/login/verify?token=…&next=…`). The "check your email" response is unchanged (enumeration-resistance preserved). `next` is **not** consulted on any rate-limit/domain/shape branch.
4. **`POST /login/verify`** — redirect to the validated `next`, else `/`. Because `/` now resolves to the user's sticky default profile feed (shipped in the first-run slice), the no-`next` case is also personalized.

### C. Dead-ends + guidance

- **Expired/used/invalid link** — instead of the generic `error.html`, render a dedicated **"This link expired or was already used"** page with an inline `POST /login` form ("enter your email to get a new one") — a plain empty email field (the consumed/invalid token gives us no address to safely pre-fill, and looking one up would be a needless info-leak). Reached from the `InvalidToken` path of `POST /login/verify` (a human who clicks a stale interstitial). It carries the page's `next` forward into the new login form so a re-issued link still deep-links correctly.
- **`login_sent.html`** — add "This can take a minute — check your spam folder if it doesn't arrive" guidance, defusing the rate-limit-lockout / scanner-delay help-desk tickets.
- **`login.html`** — render the allowed domain(s) from `settings.allowed_email_domains` (replace the hardcoded `you@ki.se` placeholder), so a wrong-domain/typo'd address can be caught visibly before submit rather than failing into silence.

## Security details (required, not optional)

- **Open-redirect prevention — `_safe_next(path) -> str | None`.** A single validator, used everywhere `next` is read. Accept only a **local relative path**: must start with exactly one `/`; reject a leading `//` or `/\`, any backslash, control characters, and anything whose `urlparse` yields a scheme or netloc. Invalid → treated as absent (fall back to `/`). Unit-tested against `//evil.com`, `https://evil.com`, `/\evil.com`, `\t`/newline injection, and the legitimate `/calls/5?profile=3`.
- **Token-leak vs CSRF.** The interstitial `GET /login/verify` response sets `Referrer-Policy: same-origin` (not the `no-referrer` the old GET used). This keeps the token in the verify URL from leaking to third parties **and** preserves the same-origin `Origin`/`Referer` the CSRF check requires for the interstitial's POST. (The codebase already learned that `no-referrer` produces `Origin: null` → 403 on browser form POSTs — see the same-origin fix; this design must not reintroduce that.)
- **Single-use + TTL unchanged.** The token remains 32-byte, single-use, 15-minute TTL; consumption stays in its own committed transaction so nothing after it can roll it back. Only the **verb** that consumes changes (GET → POST).
- **Enumeration-resistance unchanged.** `POST /login` still returns the identical "check your email" page on every branch; `next` never alters that.

## Data flow (the fixed digest journey)

Researcher opens the Monday digest on a phone → taps a call → `GET /calls/5?profile=3` → expired session → 401 → redirect `GET /login?next=%2Fcalls%2F5%3Fprofile%3D3` (allowed domain shown) → submits email → magic-link email arrives carrying `next` → taps the link → **"Confirm sign-in"** interstitial (scanner-safe) → taps the button → `POST /login/verify` consumes the token, sets the cookie → `303` to `/calls/5?profile=3` — the actual call.

## Error handling

- **Scanner pre-fetch** of the magic link: hits `GET /login/verify`, renders the interstitial, consumes nothing → the human's subsequent GET+POST still works.
- **Stale interstitial** (token expired/used by the time the human clicks Confirm): `POST /login/verify` raises `InvalidToken` → the "expired or already used" recovery page with the inline new-link form.
- **Missing/invalid `next`** at any point: `_safe_next` drops it → redirect to `/`.
- **Verify rate-limit** (IP): preserved on `POST /login/verify` → `429`.
- **Email send failure / disallowed domain / rate-limited**: `POST /login` behavior unchanged (same "check your email" page).

## Testing

Following the existing `tests/test_web_auth.py`, `tests/test_login_flow.py`, and `tests/test_auth_tokens.py` patterns:

- **GET verify does not consume:** a `GET /login/verify?token=X` leaves the token valid; a subsequent `POST /login/verify` with the same token succeeds (scanner-prefetch-then-human simulation).
- **POST verify consumes + redirects:** sets the session cookie and `303`s to a valid `next`; a replayed `POST` with the now-consumed token → the recovery page (single-use preserved).
- **`_safe_next`:** rejects `//evil.com`, `https://evil.com`, `/\evil.com`, backslash/control-char injection; accepts `/calls/5?profile=3`; absent/invalid → `/`.
- **Next-URL round-trip:** a 401 on `/calls/5?profile=3` redirects to `/login?next=…`; the threaded `next` reaches the magic-link and the final post-verify redirect lands on `/calls/5?profile=3`.
- **Dead-ends:** the expired/used-link page renders the inline recovery form; `login_sent.html` shows the spam/latency guidance; `login.html` shows the configured domain(s).
- **CSRF/referrer:** the interstitial POST succeeds under the same-origin CSRF check (regression guard against reintroducing `no-referrer`).
- **Enumeration-resistance unchanged:** `POST /login` returns the same page for allowed/disallowed/rate-limited, with or without `next`.

## Files (anticipated)

- Modify: `src/call_manager/web/routes/auth.py` (split verify into GET interstitial + POST consume; `next` on login form/submit; `_safe_next`), `src/call_manager/main.py` (401 handler encodes `next`).
- Add: `src/call_manager/web/templates/login_confirm.html` (interstitial), `src/call_manager/web/templates/login_link_expired.html` (recovery page).
- Modify: `src/call_manager/web/templates/login.html` (domain hint + hidden `next`), `login_sent.html` (guidance).
- `_safe_next` lives in `auth.py` (or a small `auth/redirects.py` if it reads cleaner) with its own unit tests.

## Out of scope (restated)

Tokenized digest links; session ergonomics; phishing-resistant email copy; user approval/roles. Each is a separate slice.
