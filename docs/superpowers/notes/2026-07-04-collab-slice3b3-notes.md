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
