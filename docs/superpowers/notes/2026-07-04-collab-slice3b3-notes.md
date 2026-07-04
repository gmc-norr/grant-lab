# Collab Slice 3b-3 — Task 1 findings (2026-07-04)

## GATE OUTCOME: GO

The live handshake probe ran against a real uvicorn backed by a testcontainers
Postgres (`pgvector/pgvector:pg16`, all 60 migrations applied), with a seeded
active workspace and collab enabled on the draft artifact.

### Driver output (abridged)

```
[alembic] Running upgrade 0059 -> 0060, collab artifact state (flag + checkpoint watermark)
 RUN  v2.0.5 ...
 ✓ src/collab/handshake.probe.test.ts  (1 test) 1620ms
HANDSHAKE_PROBE: GO
DRIVER_RC=0
```

The three probe assertions all passed:
1. Client A writes a `paragraph` element with text `hello-from-A`.
2. Client B receives and converges (`docB.getXmlFragment("prosemirror").toJSON()` contains `hello-from-A`).
3. A fresh read-only client C reconnects from the durable Yjs log and sees the same content (persistence confirmed).

## Path taken

Testcontainers path (not the fallback). Docker image `pgvector/pgvector:pg16`
was available locally. The driver boots Postgres, runs Alembic to head, builds
the FastAPI app via `create_app()`, overrides `app.state.session_factory` with
the testcontainers engine, seeds user+workspace+collab-state, initialises the
git repo via `_init_repo`, mints a signed session cookie, starts uvicorn in a
daemon thread, then spawns vitest with `--config vitest.probe.config.ts`.

## Harness fix required

The probe test file matches `*.probe.test.ts` which is excluded from the normal
vitest run by the `vite.config.ts` exclude rule (by design — no live server in
CI). When the driver passed the file directly to `vitest run`, the exclude glob
still filtered it out. Fix: created `frontend/vitest.probe.config.ts` with
`include: ["src/**/*.probe.test.ts"]` and passed `--config vitest.probe.config.ts`
to the driver's subprocess call. This is a one-line addition to the driver and a
new tracked config file — both committed.

## TDD evidence

- RED: `pnpm test src/collab/collabProvider.test.ts` → `Error: Failed to resolve import "./collabProvider"` (1 failed, 0 tests collected).
- GREEN (after implementing `collabProvider.ts`): 7/7 tests passing in 310ms.

## Unit test result

```
Test Files  93 passed (93)
Tests  473 passed (473)
```

No probe file collected in the normal run (exclude working correctly).

## Provider correctness

All six test cases pass:
- Outer `MESSAGE_SYNC=0` varuint prefix on STEP1 frame.
- Writer replies STEP2 to a server STEP1.
- Local edit produces exactly one `SYNC_UPDATE` frame.
- Inbound update is applied with `this` as origin, NOT echoed back (infinite-loop guard).
- Read-only client sends its own STEP1 but suppresses STEP2 reply and local edit frames.
- No reconnect on 1008/1009; reconnects on 1006/1011 with backoff.

## Concerns

- The Yjs stderr warning `"Yjs was already imported. This breaks constructor checks"` appears in the collabProvider unit tests. This is caused by `require("yjs")` in the test's CJS-style require() calls for y-protocols alongside the ESM `import * as Y from "yjs"` at the top. Benign in vitest (two module instances in test scope); will not affect the runtime (browser bundles ESM cleanly). No action needed for Task 1.
- `pnpm dedupe` emits `ERR_PNPM_MISSING_TIME` for `css.escape` (unrelated to collab changes); pre-existing.
- The authlib deprecation warning (`authlib.jose` → `joserfc`) is pre-existing in the app.

## Files changed

- `frontend/package.json` — y-protocols, lib0 added to deps; yjs promoted from dev; ws, @types/ws added to devDeps.
- `frontend/pnpm-lock.yaml` — updated.
- `frontend/vite.config.ts` — added `exclude: ["**/node_modules/**", "src/**/*.probe.test.ts"]`.
- `frontend/vitest.probe.config.ts` — NEW: minimal config for the live probe run.
- `frontend/src/collab/collabProvider.ts` — NEW: thin Yjs WS provider.
- `frontend/src/collab/collabProvider.test.ts` — NEW: 7-test fake-socket unit suite.
- `frontend/src/collab/handshake.probe.test.ts` — NEW: live convergence + persistence probe.
- `scripts/collab_handshake_probe.py` — NEW: testcontainers + uvicorn driver.
- `docs/superpowers/notes/2026-07-04-collab-slice3b3-notes.md` — this file.

---

# Collab Slice 3b-3 — Task 6 findings (2026-07-04)

## Dev CSP permits WebSocket — CONFIRMED

`src/call_manager/web/middleware.py` `build_csp(dev=True)` emits:

```
connect-src 'self' ws: http://localhost:5173
```

This is only emitted when `SPA_MODE=dev`; `prod` and `disabled` keep strict
`connect-src 'self'` (no `ws:` token). The `ws:` scheme wildcard covers
`ws://localhost:8000/api/ws/workspaces/<id>/draft` (the Yjs WS URL) as proxied
by the Vite dev server from `:5173` to uvicorn on `:8000`. No change was made to
`build_csp` — this was a read-only confirmation.

## Authored artifacts

### `frontend/e2e/collab.spec.ts` (NEW)

Real executable Playwright spec. The test is gated by
`test.skip(!process.env.E2E_COLLAB_WS_ID, ...)` so the Playwright suite stays
GREEN in an unseeded env (confirmed: `pnpm exec playwright test
e2e/collab.spec.ts` reports `1 skipped`). When `E2E_COLLAB_WS_ID` is set it
runs for real: two authed browser contexts open `/cockpit/<id>`, client A types
`hello-collab`, client B asserts convergence within 5 s, client A clicks "Save a
version" and asserts the button re-enables, client A reloads and asserts
persistence. Auth is via a session cookie added to each browser context (NOT a
password form — the old Jinja login route was deleted in the full-React auth
migration).

### `scripts/collab_e2e_seed.py` (NEW)

Mirrors `scripts/collab_handshake_probe.py` — boots testcontainers Postgres,
runs Alembic to head, seeds a `current_phase=active` workspace with collab
enabled on the `draft` artifact, starts uvicorn in `SPA_MODE=dev`
(`APP_BASE_URL` configurable to match the Vite port), mints a signed session
cookie, and prints `E2E_COLLAB_WS_ID` + `E2E_SESSION_COOKIE` for the caller to
export before running Playwright. The script blocks until Ctrl-C (keeps the
testcontainers Postgres alive for the Playwright run).

## Live run outcome: DEFERRED

A genuine attempt was made. The seed machinery was verified to work: testcontainers
Postgres boot + 60 Alembic migrations + seed user/workspace/collab-state + uvicorn
start + session cookie mint all succeeded. The API endpoint
`GET /api/workspaces/<id>/draft` returned `{"collab_enabled": true, "can_write": true}`.

The live Playwright run could not complete because:

1. **Port 5173 is occupied by another Vite instance** (the main-branch dev server,
   PID 1138593). Our worktree's Vite therefore started on `:5174`. The
   `playwright.config.ts` `use.baseURL` is hardcoded to `http://localhost:5173`
   (dev mode), so the browser navigated to the wrong Vite instance which served
   the **pre-collab DraftPanel** (no `CollabEditor` branch).
2. Serving via `APP_BASE_URL=http://localhost:5174` (our Vite) would work
   for CSRF, but the playwright baseURL would also need updating to `:5174`.
   That change is a config-file commit on the main repo (not appropriate for this
   worktree slice) and requires the other 5173 process to be stopped.

This is the standard "dev front-door requires manual coordination" constraint
noted in the existing deferred e2e suites (`dashboard.spec.ts` header). It does
NOT indicate a defect in the spec or the transport. The transport is already
proven end-to-end by Task 1's GO probe (two-client convergence + persistence
asserted by Node/ws against real uvicorn + testcontainers).

**To run the live e2e:**

```bash
# 1. Kill any existing :5173 Vite or start this one first
source .venv/bin/activate
EMBEDDING_MODEL=bge-m3 python scripts/collab_e2e_seed.py   # prints E2E_COLLAB_WS_ID + E2E_SESSION_COOKIE

# In a second terminal:
cd frontend && pnpm dev  # starts on :5173 if free

# In a third terminal, export the vars from the seed output, then:
cd frontend
E2E_COLLAB_WS_ID=<ws_id> E2E_SESSION_COOKIE=<cookie> pnpm exec playwright test e2e/collab.spec.ts
```

## Suite health

- `pnpm test` (vitest): **96 files, 496 tests — all passed**. `e2e/*.spec.ts`
  are NOT collected by vitest (vitest `include` is `src/**`).
- `pnpm exec tsc --noEmit`: **clean** (0 errors).
- `pnpm exec playwright test e2e/collab.spec.ts` (unseeded): **1 skipped** (not failed).

## Files changed

- `frontend/e2e/collab.spec.ts` — NEW: Playwright e2e round-trip spec.
- `scripts/collab_e2e_seed.py` — NEW: testcontainers + uvicorn seed helper.
- `docs/superpowers/notes/2026-07-04-collab-slice3b3-notes.md` — this section.
