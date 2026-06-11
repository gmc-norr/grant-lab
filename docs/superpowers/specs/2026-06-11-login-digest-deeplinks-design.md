# Login & Digest Deep-Link Reliability — Design Spec

**Date:** 2026-06-11
**Status:** Draft for implementation
**Audience:** Engineers working on call-manager

## Purpose

The weekly digest is the product's primary re-engagement channel, and the first-run value loop just made it reach users **by default** (auto-subscribe on profile creation). But the journey from a digest email to the call it advertises is broken at the login hop. This slice repairs it so a researcher who taps a Monday-digest link actually lands on the call.

### The broken journey, with evidence

1. **The magic link is consumed on a plain GET.** `GET /login/verify?token=…` calls `consume_token` immediately ([auth.py:203](../../../src/call_manager/web/routes/auth.py#L203)) and the token is single-use. Hospital mail gateways (Microsoft Safe Links / Proofpoint / Mimecast) **pre-fetch URLs** to scan them — that GET burns the token before the human clicks, so the real click gets `400 invalid or expired link`. In such an environment this can break **every** login.

2. **The requested path is discarded on 401.** The browser-401 handler redirects to a bare `/login` and throws away the originally-requested path — its own comment admits "encoding it now means the next-URL feature is a one-line change in the login_verify handler" ([main.py:293-301](../../../src/call_manager/main.py#L293-L301)). And `login_verify` hardcodes a redirect to `/` ([auth.py:217](../../../src/call_manager/web/routes/auth.py#L217)). So a digest click from an expired session strands the user on the generic dashboard, hunting for the call they tapped.

3. **Digest links are plain authenticated URLs.** `digest.html.jinja` links straight to `{{ dashboard_url }}calls/{id}?profile={pid}` ([digest.html.jinja:14](../../../src/call_manager/digest/templates/digest.html.jinja#L14)) — any click **without a live session** hits the 401 wall above and is stranded. The session cookie lasts 30 days ([config.py:304](../../../src/call_manager/config.py#L304)), so this isn't "every Monday," but it is the common case for the people who matter most: a researcher opening the digest on a **new device**, in a **different browser / incognito**, after **clearing cookies**, after the **30-day cookie lapses**, or — most often — inside a **phone mail-app webview**, which never carries the dashboard's browser cookie at all.

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

**Scope of the scanner-proofing (documented residual).** This defeats **GET-prefetch** scanners (Safe Links/Proofpoint/Mimecast in their common configuration, which only `GET` URLs to scan them) — the dominant real-world case. It does **not** defeat a scanner that actively *submits forms*: the `OriginCSRFMiddleware` deliberately allows a POST that carries **neither `Origin` nor `Referer`** (the same Django-style relaxation, [auth/csrf.py](../../../src/call_manager/auth/csrf.py)), so a form-submitting bot stripping both headers could still consume the token via `POST /login/verify`. Closing that would require mandating `Origin` on the verify POST, which risks breaking legitimate privacy-hardened clients; it is left as a known, documented residual rather than addressed here.

### B. Next-URL threading

Four small touch points carry the originally-requested path end to end:

1. **401 handler** ([main.py:293-301](../../../src/call_manager/main.py#L293-L301)) — when a browser (Accept: text/html) hits 401 **on a safe method only** (`request.method in {"GET", "HEAD"}`), encode the originally-requested relative path (path + query) as `?next=<urlencoded>` on the `/login` redirect, after passing it through `_safe_next` (below). For any other method (an expired-session `POST` that 401s) redirect to bare `/login` with **no** `next` — threading a POST path would otherwise produce a final `303 GET` to a POST-only route and a `405`.
2. **`GET /login`** — read `next` from the query, validate it, pass it to `login.html` as a hidden form field (absent/invalid → omit).
3. **`POST /login`** — accept `next` as an optional form field, validate it, and thread it into the emailed magic-link URL. The "check your email" `login_sent.html` response is the shared object returned on **every** branch; `next` is passed into its context (and only its context) so the page's "Back to sign in" link preserves it on a retry — this does **not** weaken enumeration-resistance (the page is byte-identical regardless of allow/deny/rate-limit, and `next` was already supplied by this same user). `next` is **not** consulted on any rate-limit/domain/shape decision.
4. **`POST /login/verify`** — redirect to the validated `next`, else `/`. Because `/` now resolves to the user's sticky default profile feed (shipped in the first-run slice), the no-`next` case is also personalized.

**Every link/redirect that embeds `next` MUST build the URL with a proper encoder** (`urllib.parse.urlencode` / `quote`), never string interpolation: `next` itself contains a query string (`/calls/5?profile=3&source=x`), and naive concatenation into `…/verify?token=X&next=/calls/5?profile=3&source=x` collapses the nested `?`/`&` and silently loses parameters. Reading side decodes with `urllib.parse.unquote` (or reads the already-decoded query value) before validation.

### C. Dead-ends + guidance

- **Expired/used/invalid link** — instead of the generic `error.html`, render a dedicated **"This link expired or was already used"** page (served with **HTTP 400**, preserving the existing status semantics) with an inline `POST /login` form ("enter your email to get a new one") — a plain empty email field (the consumed/invalid token gives us no address to safely pre-fill, and looking one up would be a needless info-leak). Reached from the `InvalidToken` path of `POST /login/verify` (a human who clicks a stale interstitial). It carries the page's `next` forward into the new login form so a re-issued link still deep-links correctly.
- **`login_sent.html`** — add "This can take a minute — check your spam folder if it doesn't arrive" guidance, defusing the rate-limit-lockout / scanner-delay help-desk tickets.
- **`login.html`** — render the allowed domain(s) from `settings.allowed_email_domains` (replace the hardcoded `you@ki.se` placeholder), so a wrong-domain/typo'd address can be caught visibly before submit rather than failing into silence.

## Security details (required, not optional)

- **Open-redirect prevention + bound — `_safe_next(path) -> str | None`.** A single validator, used everywhere `next` is read. Accept only a **local relative path**: must start with exactly one `/`; reject a leading `//` or `/\`, any backslash, control characters, and anything whose `urlparse` yields a scheme or netloc. **Length cap: reject anything over 2048 bytes** (an allowed user could otherwise submit a giant `next` and inflate the emailed magic-link URL). Invalid/oversized → treated as absent (fall back to `/`). Unit-tested against `//evil.com`, `https://evil.com`, `/\evil.com`, `\t`/newline injection, a >2048-byte value, a **nested-query** path (`/calls/5?profile=3&source=x`, which must survive the encode→email→decode round-trip intact), and the legitimate `/calls/5?profile=3`.
- **Token-leak vs CSRF.** The interstitial `GET /login/verify` response **overrides** the app-wide `Referrer-Policy` (which `SecurityHeadersMiddleware` sets to `same-origin`, and which it leaves alone when a handler already set its own — [middleware.py:84](../../../src/call_manager/web/middleware.py#L84)) to **`strict-origin`**. This matters because the interstitial extends `base.html`, which loads same-origin `/static` assets (pico/app CSS+JS, favicon, htmx) — under `same-origin` those asset requests would carry the *full* verify URL **including `?token=`** in the `Referer`, a real leak. `strict-origin` sends only the origin (scheme+host, no path/query) so the token never travels, while still providing the non-null `Origin`/`Referer` the CSRF check needs for the interstitial's POST. (`no-referrer` is forbidden — it produces `Origin: null` → 403 on browser form POSTs, the regression the codebase already hit.)
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
- **POST verify consumes + redirects:** sets the session cookie and `303`s to a valid `next`; a replayed `POST` with the now-consumed token → the recovery page at **status 400** (single-use preserved).
- **`_safe_next`:** rejects `//evil.com`, `https://evil.com`, `/\evil.com`, backslash/control-char injection, and a >2048-byte value; accepts `/calls/5?profile=3`; absent/invalid → `/`.
- **Next-URL round-trip incl. nested query:** a `GET` 401 on `/calls/5?profile=3&source=x` redirects to `/login?next=…`; the threaded `next` is URL-encoded into the magic-link, survives decode, and the final post-verify redirect lands on `/calls/5?profile=3&source=x` **with both query params intact**.
- **Safe-method-only next:** a `GET` 401 yields `?next=`; a `POST` 401 (expired-session form submit) redirects to bare `/login` with no `next` (no 405-producing path threaded).
- **`login_sent` preserves `next`:** posting `/login` with a `next` renders the check-your-email page whose "Back to sign in" link carries the same `next`.
- **Dead-ends:** the expired/used-link page (400) renders the inline recovery form and forwards `next`; `login_sent.html` shows the spam/latency guidance; `login.html` shows the configured domain(s).
- **Token not leaked in Referer:** the interstitial `GET /login/verify` response carries `Referrer-Policy: strict-origin` (regression guard: not `same-origin`, never `no-referrer`).
- **CSRF:** the interstitial POST succeeds under the Origin/Referer CSRF check (regression guard against reintroducing `no-referrer` / `Origin: null`).
- **Enumeration-resistance unchanged:** `POST /login` returns the same page for allowed/disallowed/rate-limited, with or without `next`.

## Files (anticipated)

- Modify: `src/call_manager/web/routes/auth.py` (split verify into GET interstitial + POST consume; `next` on login form/submit; `_safe_next`), `src/call_manager/main.py` (401 handler encodes `next`).
- Add: `src/call_manager/web/templates/login_confirm.html` (interstitial), `src/call_manager/web/templates/login_link_expired.html` (recovery page).
- Modify: `src/call_manager/web/templates/login.html` (domain hint + hidden `next`), `login_sent.html` (guidance).
- `_safe_next` lives in `auth.py` (or a small `auth/redirects.py` if it reads cleaner) with its own unit tests.

## Out of scope (restated)

Tokenized digest links; session ergonomics; phishing-resistant email copy; user approval/roles. Each is a separate slice.

## Revision History

**R1 (2026-06-11) — incorporated Codex spec review.** All findings verified against the code and accepted:
- *Same-origin leaks the token to same-origin assets (High):* the interstitial extends `base.html` (loads `/static` CSS/JS/favicon); `Referrer-Policy: same-origin` would carry `?token=` in those asset requests' `Referer`. Changed to **`strict-origin`** (origin-only, no path/query), which still satisfies the CSRF Origin/Referer check. The interstitial response **overrides** the middleware's app-wide `same-origin` default.
- *Only preserve `next` for safe-method 401s (Medium):* the 401 handler threads `next` only for `GET`/`HEAD`; other methods → bare `/login` (avoids a `303 GET` → `405` to a POST-only route).
- *Bound and structurally encode `next` (Medium):* added a 2048-byte cap to `_safe_next`, mandated `urlencode`/`quote` at every embed point (naive interpolation drops nested query params), and added nested-query + overlong tests.
- *`login_sent.html` loses the deep link on retry (Medium):* `next` is threaded into the shared check-your-email response and preserved in its "Back to sign in" link (enumeration-resistance intact).
- *Scope the scanner-proof claim (Low):* documented the residual — `OriginCSRFMiddleware` allows POSTs lacking both `Origin` and `Referer` (Django-style relaxation), so a form-submitting scanner could still consume the token; the interstitial defeats the dominant GET-prefetch case only.
- *Notes:* the expired-link recovery page stays **HTTP 400**; softened the "expired by Monday" evidence (session TTL is 30 days — the wall is hit on new device / cleared cookies / incognito / mobile mail-app webview / lapsed session, not weekly).
- *Confirmed defaults:* no DB column for `next` (validated query param); interstitial stays unconditional.
