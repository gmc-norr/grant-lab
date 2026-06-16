# Sub-project 1: React SPA Foundation — Design

_Spec · 2026-06-15 · Status: draft for review · **rev 5 (+ serving-architecture re-review)**_

> **Superseded on two axes by the [supply-chain security spec](2026-06-16-supply-chain-security-design.md) (2026-06-16):** the **package manager is pnpm, not npm** (scripts-off, `minimumReleaseAge` cooldown, frozen, exact pins), and the **frontend build runs in an ephemeral secretless container**, not on the host directly. Backend gets `uv sync --frozen` + `[tool.uv] exclude-newer` + wheels-preferred. CSP `script-src 'self'` is reaffirmed. Everything else in this spec stands. The plans carry these in their "Rev 3 — supply-chain hardening" sections.

Part of the collaborative-proposal-workspace effort. Design direction + rationale: [../notes/2026-06-15-collaborative-proposal-workspace-design-direction.md](../notes/2026-06-15-collaborative-proposal-workspace-design-direction.md). This is sub-project **1 of 3** (foundation → cockpit → collaborative writing).

> **Rev 2 changes** (verified against code, Codex review): Vite is the sole dev front door (CSRF/coexistence fix); `GET /api/calls` is strictly read-only with a separate preference mutation; explicit SPA-route allowlist + defined packaging (no global catch-all); enforceable API error envelope via Pydantic + exception translation + generated TS types; defined SSE JSON wire contract; React Router **Data Mode**, npm locked, Zustand/Modal/Drawer/Toast deferred (YAGNI / Radix later); shared Python dashboard read-model service used by both Jinja and JSON.
>
> **Rev 3 changes** (2nd Codex pass): explicit **`SPA_MODE=dev|prod|disabled`** (only `prod` mounts the build + fails fast; tests/dev don't); a single **generated route manifest** (exact + pattern + precedence) consumed by Python/Vite/React Router, with unknown non-SPA paths hitting FastAPI's real **404 in dev too** (Vite global fallback disabled); `/events/ping` emits **replayable named JSON events with `id:`** and the hook **probes `/api/me`** on repeated errors; error envelope adds **403 AccountInactive + sanitized 500**; dashboard **deep-links are read-only** (don't persist the default — only explicit selection does), ranking-status **conditionally polls**; migration milestone scoped to **user-facing page routes**.
>
> **Rev 4 changes** (internal multi-agent scrutiny vs codebase): error envelope **split into FastAPI handlers + path-aware middleware** (CSRF/body-size are emitted by ASGI middleware *above* the exception layer — the rev-3 "exception translation covers CSRF" was structurally impossible); ranking status modeled as **`{ pending, failed }`** with poll-on-`pending`-only + drain-refetch (parity); **CSP must keep global `style-src 'unsafe-inline'`** for Pico pages (hard req is `script-src 'self'`); plus a **Rev 4 refinements** section (SSE fresh-connect cursor, anti-buffering headers, `/api/me` probe policy, native-form logout, OAuth dev caveat, error-boundary/a11y/observability DoD, `node:test`-vs-Vitest, work-item 1a/1b split). Open items flagged for sign-off in the cover note.
>
> **Rev 5 changes** (serving-architecture re-review): named the **route-manifest generation mechanism** (checked-in source + CI/pre-build generator → Python + Vite-regex + React-Router formats, ordered longest-prefix-first; v1 stays trivial); fixed **dev real-404** (proxy must default to FastAPI and carve out SPA routes — disabling Vite's fallback alone yields *Vite's* 404); added **dev-CSP relaxation** under `SPA_MODE=dev` (global `script-src/connect-src 'self'` would block Vite HMR's WebSocket + inline dev scripts → broken dev on first run); pinned **hatch `artifacts`/`force-include` as mandatory** (hatchling drops VCS-ignored files) + a CI wheel-contents check; **dropped the `X-Forwarded-Proto/-Host` requirement** (cookie-Secure + CSRF derive from static `APP_BASE_URL` by design — keep `proxy_buffering off` + `TRUSTED_PROXY`).

## Goal

Stand up a single React SPA as the long-term frontend for call-manager, served same-origin over the existing FastAPI backend exposed as a JSON + event-stream API. Establish every foundational contract — build/serve, auth/CSRF, API shape, realtime, design tokens, testing — and prove it end-to-end by porting the **dashboard** to feature parity. No new product features.

## Scope

**In scope:** React+TS+Vite app; same-origin serving; cookie auth reuse; typed API client + TanStack Query; React Router (Data Mode) shell; design-token split; reusable SSE hook (pattern); **dashboard port** at parity; test setup; route-level coexistence with un-ported HTML pages.

**Out of scope / deferred:** the workspace cockpit (sub-project 2); collaborative writing/propose→accept/presence/soft-locks (sub-project 3); porting every page; the **email digest** (stays server-rendered); citation-level provenance; agent-config governance.

## Architecture

```
Browser → React SPA (Vite, CSR) ──GET/POST /api──► FastAPI ──► Postgres / git / Ollama
                                 ──SSE /events──◄──┘  (server→client live updates)
Same origin. httpOnly session cookie sent automatically (incl. EventSource).
```
One Python backend (no Node/SSR); response format becomes JSON per route as it is ported.

## Consolidated decisions

| Area | Decision |
|---|---|
| Framework | React + TypeScript, client-rendered, Vite |
| Backend | Reuse FastAPI as JSON + SSE API; no Node SSR |
| Realtime | SSE (server→client) + HTTP POST (client actions) |
| Auth | Reuse httpOnly session cookie, same-origin; existing Origin-based CSRF; no JWT/localStorage |
| Data/server-state | TanStack Query (see refetch caveat below) |
| Routing | **React Router, Data Mode** |
| Local UI state | Plain React state; **no Zustand until a real need** |
| Styling | Split existing `app.css` tokens into `tokens.css`; CSS Modules components |
| Components | Build only what the dashboard needs now; **defer Modal/Drawer/Toast**, build on **Radix** primitives when needed |
| Package mgr | **npm**, committed `package-lock.json`, `npm ci` in CI |
| Tests | Vitest + React Testing Library; Playwright run against **both** dev-proxy and prod build |
| Types | FastAPI OpenAPI → **openapi-typescript** types + **openapi-fetch** client, with a **CI drift check** |

## Dev & prod serving

### Serving mode
An explicit setting **`SPA_MODE = dev | prod | disabled`** governs how FastAPI relates to the SPA:
- **`dev`** — Vite is the front door; FastAPI does **not** mount or require any built SPA assets (no `spa/` build needed in a fresh checkout).
- **`prod`** — FastAPI mounts the built assets and **fails fast at startup if the build is absent** (no "maintenance response" alternative).
- **`disabled`** — no SPA mounting at all; SPA page routes simply 404. **The test suite defaults to `disabled`** so `create_app()` can be constructed repeatedly without running npm.

### Route manifest (one generated source of truth)
A single **generated, machine-readable route manifest** is consumed by **Python (serving), Vite (proxy/fallback), and React Router** — not a hand-mirrored constant (which drifts). It specifies, per route, **exact or pattern match** and **precedence**, so siblings can differ (e.g. `/workspaces/{id}` may be SPA-owned while `/workspaces/{id}/<action>` stays backend-owned).
- Initially SPA-owned page routes: `/` (dashboard) only.
- Permanently backend-owned: `/api/*`, `/events/*`, `/login`, `/logout`, OAuth callbacks, downloads, `/static/*`, health, plus all not-yet-ported pages (`/calls/*` detail, `/workspaces/*`, `/admin/*`).
- **Unknown, non-SPA paths must reach FastAPI's real 404 in BOTH dev and prod** — Vite's default global SPA fallback is disabled so it can't mask 404s with `index.html` (dev mechanism detailed below).
- **Generation mechanism:** source of truth is a **checked-in manifest** (e.g. `routes.json`); a small **generator** (CI / pre-build) emits consumer-specific artifacts — a Python module for `create_app()` serving, a JS/JSON module for the `vite.config` proxy, and the React Router table — as **ordered, longest-prefix-first** rules, with documented translation from the manifest's pattern syntax to (i) Vite proxy regex and (ii) React Router `path`s. Precedence matters for sibling splits (`/workspaces/{id}` SPA vs `/workspaces/{id}/<action>` backend → backend matched first).
- **v1 scope:** the only SPA-owned route is `/`, so the v1 manifest is trivially "default → backend, `/` → SPA". The pattern/precedence machinery is the **forward-looking contract** — do **not** build a generic pattern engine before a sibling-split route actually exists.

### Dev — Vite is the sole front door
Browsers hit the **Vite dev server** (e.g. `:5173`). Set **`APP_BASE_URL` to the Vite origin in dev** so CSRF (full `scheme://host:port` vs `APP_BASE_URL`, [csrf.py:79-89](../../../src/call_manager/auth/csrf.py#L79)) passes for same-origin POSTs.
- **Proxy posture — default-to-backend, carve out the SPA:** to make unknown paths reach **FastAPI's real 404 in dev** (disabling Vite's SPA fallback alone just yields *Vite's* 404), the dev server **forwards everything to uvicorn by default**, carving out only (a) SPA-owned page routes (serve `index.html`) and (b) Vite's own dev assets (`/@vite`, `/@id`, `/src`, `/node_modules`, HMR WS). Use `appType:'custom'` + a small Vite middleware (or a catch-all `/`→uvicorn proxy with SPA/asset carve-outs). This keeps un-ported pages (`/login`, `/calls/...`, `/workspaces`) working through the one origin.
- **Dev CSP relaxation (required for first-run):** the global CSP (`script-src 'self'`, `connect-src 'self'`, [web/middleware.py:32-43](../../../src/call_manager/web/middleware.py#L32)) rides every proxied response and would **block Vite's HMR WebSocket (`ws:`) and injected inline dev scripts**. Under `SPA_MODE=dev`, serve a **relaxed dev CSP** (allow `ws:`/the Vite origin + inline for dev) or strip CSP on dev responses; **prod is unchanged** (`script-src 'self'`).
- **Tests:** dev-proxy tests covering (a) a state-changing POST passing CSRF through Vite, (b) navigation to an un-ported page resolving to FastAPI, and (c) an **unknown non-SPA path returning FastAPI's JSON 404** (not Vite's HTML 404).

### Prod (`SPA_MODE=prod`) — manifest-driven, no global catch-all
FastAPI serves built assets and returns `index.html` **only for SPA-owned page routes in the manifest** (history fallback). Everything else routes to existing handlers or a real 404 — **never** turn `/api/typo` or an unknown path into `index.html` 200.
- **Artifact packaging:** the wheel is `packages = ["src/call_manager"]` ([pyproject.toml:51](../../../pyproject.toml#L51)). The Vite build outputs **inside the packaged tree** (`src/call_manager/web/spa/`). Because **hatchling excludes VCS-ignored files by default** and the build dir is gitignored, an explicit **`artifacts = ["src/call_manager/web/spa/**/*"]`** (plus a `force-include` mapping if source ≠ package path) is **mandatory**, not optional. Build ordering: `npm ci && npm run build` **before** `hatch build`; add a **CI assertion that the built wheel contains `call_manager/web/spa/index.html`** (`unzip -l dist/*.whl | grep spa/index.html`) so a missing-build wheel fails the *release pipeline* — a build-time guarantee, not just the runtime fail-fast.
- **Missing build → fail fast** at startup (only in `prod` mode; `dev`/`disabled` don't require a build).
- **CSP (do not regress the global policy):** the CSP is a single **global** policy ([main.py:280](../../../src/call_manager/main.py#L280) → `build_csp`) applied to every route, and the still-rendered Pico pages depend on **`style-src 'unsafe-inline'`** ([web/middleware.py:34](../../../src/call_manager/web/middleware.py#L34); ~15 inline `style=` uses across login/register/call_detail/admin). So: the **hard requirement is `script-src 'self'`** (no `'unsafe-inline'`/`'unsafe-eval'`) for the hashed bundles — configure Vite for hashed, **no-inline JS**; **keep global `style-src 'unsafe-inline'` during coexistence** (or add a per-route/nonce CSP only if SPA-only style tightening is wanted later). `connect-src 'self'` must permit `/api` + `/events`. **DoD adds an explicit `script-src`-has-no-`'unsafe-inline'` assertion** ([tests/test_security_headers.py:34-37](../../../tests/test_security_headers.py#L34) currently doesn't guard this, so a regression passes CI silently).

## Auth & session

- Same-origin ⇒ httpOnly session cookie rides every request incl. `EventSource`. No tokens.
- `GET /api/me` → `{ user, capabilities }` or 401; `useSession()` gates the app; **401 → redirect to `/login`**.
- Keep existing **Origin-based CSRF** for state-changing POSTs (do not reintroduce the `Origin: null` regression). Login stays server-rendered initially.

## API contract (enforceable)

The existing app returns `{"detail": ...}`/HTML by Accept header ([main.py:317](../../../src/call_manager/main.py#L317)); CSRF/body-size middleware can return plain text; FastAPI validation has yet another shape. To make a *uniform* contract real:
- **Pydantic request/response models for every `/api` operation** (no ad-hoc dicts).
- **Uniform JSON error envelope `{ error: { code, message, detail? } }` via TWO mechanisms** (because CSRF + body-size are emitted by ASGI middleware *above* FastAPI's exception layer and cannot be reached by `@app.exception_handler`):
  1. **FastAPI exception handlers**, path-scoped to `/api`, for: auth 401, **AccountInactive 403**, `HTTPException`, request-validation errors, and **sanitized 500s**. The 500 handler MUST be path-scoped (an `/api`-only catch) — **not** a global `@app.exception_handler(Exception)`, which would also reshape HTML routes' 500s (contradicting "leave HTML routes untouched").
  2. **Middleware emits the envelope itself** when `path.startswith("/api")`: `OriginCSRFMiddleware._forbidden` ([csrf.py:118](../../../src/call_manager/auth/csrf.py#L118)) and `MaxBodySizeMiddleware._send_status` ([web/middleware.py:140-151](../../../src/call_manager/web/middleware.py#L140)) currently return `text/plain`/`Response` directly; they must return the JSON envelope for `/api` paths and leave HTML routes unchanged.
- **Generated TS types**: `openapi-typescript` from FastAPI's schema + `openapi-fetch` client; a **CI check fails on drift** between committed types and the live schema.
- **Time:** UTC ISO-8601 on the wire; the browser formats with explicit **Europe/Stockholm** (`Intl` with `timeZone`) to preserve current display behavior rather than relying on the viewer's locale.
- List endpoints: `{ items, page, total }`.

## Dashboard read-model (not "simple")

The dashboard is the validation slice precisely because it is rich: URL state, filters, facets, pagination, auth, and links into un-ported pages. Requirements:
- A **shared Python read-model/service** that both the existing Jinja route and `GET /api/calls` call — **no duplicated query logic** during the overlap.
- **`GET /api/calls` is strictly read-only.** The current GET commits `default_profile_id` (clears on `""`, persists on id) ([calls.py:96-119](../../../src/call_manager/web/routes/calls.py#L96)); a read-only GET + TanStack refetch-on-focus would silently mutate prefs. Move that side effect out.
- **`POST /api/me/default-profile`** — the discrete preference mutation (set/clear the sticky default).
- **Read-model response** includes: ranked call rows, summary counts, profile + source **facets**, **pagination**, the **effective selection** (resolved profile/source), and **ranking status as `{ pending, failed }`** — two independent counts ([calls.py:268-296](../../../src/call_manager/web/routes/calls.py#L268)), not a single status.
- **Effective-selection resolution (read-only, parity with current GET):** the shared service must reproduce, *without persisting*: absent `?profile` → resolve the stored `default_profile_id` for display ([calls.py:96-104](../../../src/call_manager/web/routes/calls.py#L96)); invalid/inactive profile id → unranked, id dropped, no 400/404; non-int `?profile` → 400; stale source slug → cleared/all, no error. It returns the resolved selection so parity tests pin both surfaces.
- **No `/api/calls/{id}`** — keep call-detail links as plain anchors to the un-ported HTML page, which also exercises coexistence.
- **Deep-link vs preference (decided):** a URL like `/?profile=12` is a **transient, read-only view filter** — it sets the *effective selection* for that view but does **not** persist the sticky default. Only an **explicit profile selection** in the UI fires `POST /api/me/default-profile`. (This intentionally decouples the current behavior where a GET with `?profile=id` also persisted — sharing/bookmarking a link no longer changes someone's default. **Approved by user 2026-06-15.**)
- **Ranking status drives conditional polling, preserving current parity:** gate TanStack `refetchInterval` on **`pending > 0` only**; render **`failed > 0`** ("N calls couldn't be scored automatically") **without** polling; on the **`pending → 0` drain transition, refetch `/api/calls`** so newly-scored calls appear (matches the current `HX-Refresh`-on-drain at [calls.py:311-313](../../../src/call_manager/web/routes/calls.py#L311)).
- **Jinja parity tests are transitional** — they assert `/api/calls` matches the Jinja route during the overlap and are removed when the Jinja dashboard handler is deleted.

## SSE wire contract (define now, even if only `/events/ping` ships first)

The existing stream emits HTML fragments with no IDs ([workspaces.py:364-374](../../../src/call_manager/proposal_assist/web/workspaces.py#L364)). The new channel needs a typed contract so the hook is genuinely reusable:
- **Named event types** + a JSON payload envelope (e.g. `event: <type>` / `data: {json}`).
- **`id:` opaque cursor** + catch-up semantics (client sends `Last-Event-ID` on reconnect; server resumes).
- **Heartbeat** format/interval (keep-alive + liveness).
- **Parse-error** behavior (skip + log, don't crash the stream).
- **Auth-expiry / repeated-error** behavior in the hook: `EventSource.onerror` does **not** expose the HTTP status, so on repeated stream errors the hook **probes `GET /api/me`** to distinguish session expiry (401 → surface "session expired", route to `/login`) from a transient drop (let `EventSource` keep retrying).
- **`GET /events/ping`** is the foundation's validation target: it emits **replayable named ping JSON events with `id:` values** (so it actually exercises typed events + ids + catch-up via `Last-Event-ID`), not a bare heartbeat. It is **dev-gated** (not exposed in prod).

## App shell, routing, state, design tokens

- App shell + nav mirroring current chrome, using token-based components.
- **React Router Data Mode** with the SPA route table sourced from the ownership table; links to un-ported pages are normal anchors.
- TanStack Query for server data (mind refetch-on-focus vs side-effecting GETs — solved by read-only GETs above). Zustand only when a real cross-component UI-state need appears (cockpit).
- **Design tokens:** split the existing `app.css` custom properties ([app.css:17](../../../src/call_manager/web/static/app.css#L17)) into a shared `tokens.css` consumed by both remaining server-rendered pages and the SPA (respect the `:root:not([data-theme=dark])` Pico-override selector). Build only the components the dashboard needs (Button, Inputs/Select for filters, Card, pagination); defer Modal/Drawer/Toast to when used, atop Radix headless primitives (accessibility/focus management).

## Migration / coexistence

- **Route-level strangler** driven by the ownership table. Porting a page = add its `/api` endpoint(s) (read-only + explicit mutations) → move its route to SPA-owned → delete the old template/handler.
- **Shared-logic rule:** during any HTML+JSON overlap, new backend logic lives in a **shared service** used by both surfaces (as the dashboard read-model does) — no divergence.
- **Completion milestone:** define "migration done" as **all user-facing *page* routes SPA-owned** (HTML page handlers/templates removed, serving simplified) — `/api`, `/events`, OAuth callbacks, downloads, and health remain backend-owned permanently. This keeps "opportunistically later" from becoming an accidental permanent hybrid.

## Validation slice — definition of done

Port the **dashboard** end-to-end:
- Shared read-model powers both Jinja (until removed) and `GET /api/calls`; sticky-default behavior preserved via `POST /api/me/default-profile`, GET is side-effect-free.
- React dashboard at visual + functional parity (filters, facets, pagination, effective selection, ranking-job status, anchors to un-ported call detail).
- Auth (logged-out → `/login`; logged-in → loads), loading/error/empty states.
- Works in **dev (Vite sole front door, proxy, APP_BASE_URL=Vite)** and **prod (FastAPI-served build, allowlist)**; CSP green.
- Generated TS types in place + CI drift check; error envelope verified for auth/validation/CSRF cases.

## Testing

- **Unit/component:** Vitest + RTL (API client, hooks incl. SSE hook, dashboard, token components).
- **Integration:** dev-proxy test (CSRF POST through Vite + nav to un-ported page).
- **e2e:** **one Playwright scenario run against both** the dev-proxy server and the prod build (login → dashboard → apply a filter → navigate to an un-ported call-detail page).
- **Backend:** pytest for `/api/me`, `/api/calls`, `/api/me/default-profile`, exception-translation, and the shared read-model parity with the Jinja route. (Whole-suite Postgres flakiness is known — verify flagged tests in isolation.)

## Rev 4 refinements (internal multi-agent scrutiny)

Cross-cutting clarifications, listed once.

**SSE wire contract**
- **Fresh-connect catch-up:** browsers don't send `Last-Event-ID` on a brand-new `EventSource()` (reload / SPA remount). Support BOTH: in-session reconnect via `Last-Event-ID` AND fresh-connect via an explicit `?since`/`?cursor` query param the client passes when (re)opening (server accepts either) — mirrors the existing `?since=` cursor ([workspaces.py:302,326-338](../../../src/call_manager/proposal_assist/web/workspaces.py#L302)). The hook must `addEventListener(type, …)` per named event (named events don't reach `onmessage`).
- **Anti-buffering:** every `/events/*` response sets `Cache-Control: no-cache` + `X-Accel-Buffering: no` (dev routes through the Vite proxy — a buffering hop). Assert on the `/events/ping` test. **Deployment posture (decided): assume a reverse proxy in prod** (nginx/Caddy or the institution's gateway — TLS termination, static, request limits). Requirements: **disable SSE buffering** (`proxy_buffering off` / `X-Accel-Buffering: no`); set **`APP_BASE_URL` to the public https origin** — cookie-`Secure` and CSRF origin derive from it by deliberate design ([config.py:436-455](../../../src/call_manager/config.py#L436)), so **`X-Forwarded-Proto/-Host` handling is *not* required**; set **`TRUSTED_PROXY` + hops** for rate-limit correctness ([main.py:294-304](../../../src/call_manager/main.py#L294)). (No proxy is in the repo — add a thin one or document the institutional one as a deploy prerequisite.)
- **`/api/me` probe policy:** probe only after N consecutive `onerror`/T seconds, **single-flight** (each probe opens a DB session), capped exponential backoff; outcomes 401→`/login`, 403 AccountInactive→no-access state, 200→let `EventSource` keep retrying.
- **Scope:** here SSE is validated at **unit/contract level (mocked EventSource) + the `/events/ping` backend test** only; nothing shipping in sub-project 1 rides SSE (the dashboard polls). Real end-to-end transport re-validation is deferred to the first real consumer (sub-project 2).

**Auth**
- **Logout:** `/logout` is a CSRF-bearing POST that bumps the session epoch and 303→`/login` ([auth.py:160-196](../../../src/call_manager/auth/routes.py#L160)). Use the **native `<form method=post action=/logout>`** (full-page nav, follows 303, carries Origin/CSRF), not `openapi-fetch`; reuse the dev-proxy CSRF-POST test to assert logout-through-Vite lands on `/login`.
- **OAuth dev caveat:** pointing `APP_BASE_URL` at the Vite origin also repoints `google_redirect_uri` (config.py:454-455 → oauth_google.py), needing console re-registration + proxying `/auth/*`. OIDC defaults off — **prefer password login in the dev-proxy story.**

**DoD additions**
- Top-level **React error boundary** with a recoverable fallback (CSR white-screen on uncaught render is a new failure mode vs Jinja; distinct from TanStack query-error states).
- Minimal **a11y**: keyboard nav of the dashboard filters + one `axe` check in the Playwright run.
- One-line **client-error/observability** decision (so sub-project 2 needn't re-litigate it).
- **CSP:** assert `script-src` contains no `'unsafe-inline'`.

**Tests / CI**
- `tests/js/` runs today via `node --test` (CI step, no `package.json`). Keep it on **`node:test` during the overlap**; new React code uses **Vitest**; reconcile to one runner at migration completion. State the resulting CI shape.

**Work-breakdown split (dependency fix)**
- Split item 1 into **1a** (SPA_MODE + route manifest + dev serving + real-404 + missing-build-*absence* behavior — testable with no build) and **1b** (prod mount + hatch force-include + fail-fast + CSP-bundle check — after item 2's scaffold yields a build dir). Add a **distinct work item for the CSRF/body-size middleware envelope** change (separate from the FastAPI exception-handler item).

## Risks & deferred

- CSP friction with bundles (mitigate: hashed, no-inline Vite output; verify early).
- Frontend build in CI/release ordering + artifact inclusion (specified above; implement first).
- Hold scope to the dashboard; cockpit/collaboration/rich-text/provenance/agent-governance are later sub-projects.

## Success criteria

1. Dashboard runs as React over a read-only `/api/calls` + `POST /api/me/default-profile`, same-origin, cookie-auth, at parity; sticky-default behavior intact and not mutated by background refetches.
2. Dev (Vite front door) and prod (allowlist, packaged build) both work; CSP green; no global catch-all.
3. Reusable foundation: typed API client (+ CI drift check), TanStack Query, React Router Data Mode shell, `tokens.css` + base components, SSE hook against a defined wire contract, test harness.
4. Un-ported pages still work; shared read-model service in place; migration-completion milestone defined.
5. Patterns documented well enough that sub-project 2 starts without re-deciding foundation questions.

## Rough work breakdown (feeds the plan)

1. Route-ownership table + FastAPI serving (allowlist fallback) + packaging/build ordering + missing-build behavior + CSP check.
2. Scaffold `frontend/` (Vite+React+TS, lint/format/test, npm lockfile); Vite dev proxy (sole front door) + dev `APP_BASE_URL`.
3. API plumbing: Pydantic models, `/api` exception translation, OpenAPI→TS types + CI drift check.
4. Auth: `GET /api/me`, `useSession`, 401→login.
5. Shared dashboard read-model service; `GET /api/calls` (read-only); `POST /api/me/default-profile`; ranking-job status.
6. `tokens.css` split + base components.
7. App shell + React Router Data Mode + dashboard page at parity.
8. SSE wire contract + `useEventStream` hook + dev-gated `/events/ping`.
9. Tests: component + dev-proxy + dual-mode Playwright + backend parity; dev-run docs.
