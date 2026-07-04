# Collab Slice 3b-3 — Tiptap Frontend + Yjs WS Provider — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the first frontend runtime for collaborative editing — a schema-locked Tiptap WYSIWYG editor bound over a custom thin Yjs WebSocket provider to the existing backend sync server — mounted only when the per-artifact collab flag is enabled, with the CodeMirror-6 path preserved byte-for-byte when it is off.

**Architecture:** A thin provider (`collabProvider.ts`) speaks the exact three-frame y-sync subset the hand-rolled pycrdt server implements (bidirectional STEP1, origin-tagged apply so remote updates are never echoed, `canWrite`-gated write frames, close-code-aware reconnect). A Tiptap editor whose ProseMirror schema is structurally identical to `frontend/scripts/collab-schema.mjs` (enforced by an equality test) binds a `Y.Doc` to that provider via `@tiptap/extension-collaboration` on the `'prosemirror'` XmlFragment. Two additive read-model fields (`collab_enabled`, `can_write` from `resolve_access`) drive a conditional mount in `DraftPanel`/`SpecPanel`. "Save a version" calls the existing checkpoint endpoint, gated on a settled socket. The flag stays OFF in prod; the editor is exercised via dev/e2e enabling it per-workspace.

**Tech Stack:** React 19.2 + Vite 5.4 + Mantine 9.4 + pnpm 10.16; Tiptap v2 (`@tiptap/react`,`@tiptap/core`,`@tiptap/pm`,`@tiptap/extension-collaboration`); `yjs` 13.6.31 + `y-prosemirror` 1.3.7 (pinned to the write-path-validated versions) + `y-protocols` + `lib0`; vitest 2.0 (jsdom) + Playwright 1.60; FastAPI + SQLAlchemy backend (read-model only).

## Global Constraints

- **The collab flag STAYS OFF in prod.** Every new runtime behavior is reachable only when `is_collab_enabled(workspace, artifact)` is true, which prod never sets. Flag-off parity is a HARD gate.
- **CM6 flag-off path is byte-for-byte unchanged.** `ArtifactSectionEditor` / `CodeMirrorEditor` / `useDraft` / `useSaveSection` / REST section-save are untouched on the flag-off path. `can_edit` keeps its current owner-oriented semantics — do NOT repurpose it.
- **Backend changes are additive & read-only.** Only add `collab_enabled` + `can_write` to `DraftOut`/`SpecOut`. NO migration, NO new write endpoint, NO schema change.
- **Single-instance Yjs.** Exactly one `yjs` and one `y-prosemirror` in the bundle. Pin to `yjs@13.6.31`, `y-prosemirror@1.3.7`; run `pnpm dedupe` and verify a single copy of each.
- **Schema identity is load-bearing.** The Tiptap-generated schema MUST equal `collab-schema.mjs` (node/mark names, content expressions, group, inline/atom, attr names+defaults). The equality test (Task 2) is the CI gate.
- **pnpm cooldown:** fresh `pnpm add` is blocked by `minimumReleaseAge` — add new packages with `--config.minimumReleaseAge=0` (ephemeral; the committed lockfile install is unaffected). Promoting a dep dev→runtime is a `package.json` edit + `pnpm install --frozen-lockfile`, no cooldown.
- **Backend tests:** `EMBEDDING_MODEL=bge-m3` ALWAYS; grouped-dir runs (`tests/proposal_assist/`, `tests/test_api_workspaces.py`), never whole-suite; `-p no:cacheprovider`. Frontend gate: `tsc` (in `build`) + `pnpm test` (vitest) + Playwright. CI runs `ruff check .` (not `ruff format`).
- **Git:** worktree `.worktrees/collab-slice3b3` only; explicit-path `git add <files>` (never `-A`); never touch master/main; **STOP before merge** (human verifies + merges).
- **Node deps for tests:** `frontend/node_modules` install via `pnpm install --frozen-lockfile`. OpenAPI regen must run with `EMBEDDING_MODEL=bge-m3` (mxbai default logs a WARNING that breaks openapi-typescript).

---

### Task 1: Thin Yjs provider + unit tests + live handshake de-risk (GO/NO-GO gate)

The transport layer, proven end-to-end against a real uvicorn **before** any editor is built. If the live probe is NO-GO, STOP and fix transport — do not proceed to Task 2.

**Files:**
- Modify: `frontend/package.json` (add `y-protocols`, `lib0` to `dependencies`; promote `yjs` from `devDependencies`→`dependencies`; add `ws`, `@types/ws` to `devDependencies`)
- Create: `frontend/src/collab/collabProvider.ts`
- Create: `frontend/src/collab/collabProvider.test.ts` (vitest, fake socket)
- Create: `frontend/src/collab/handshake.probe.test.ts` (vitest, `@vitest-environment node`, real `ws` against live uvicorn — run only by the driver, excluded from the normal vitest run)
- Create: `scripts/collab_handshake_probe.py` (Python driver: testcontainers PG + migrations + uvicorn-in-thread + seed + spawn the probe)
- Modify: `frontend/vitest.config` include/exclude (exclude `*.probe.test.ts` from the default run — see Step 2)

**Interfaces:**
- Produces: `class CollabProvider` with constructor `new CollabProvider({ url: string, doc: Y.Doc, canWrite: boolean, WebSocketImpl?: typeof WebSocket, reconnectBaseMs?: number, reconnectMaxMs?: number })`; getters `status: "connecting"|"connected"|"offline"|"closed-policy"`, `bufferedAmount: number`; methods `on("status", (s)=>void): ()=>void`, `isSettled(settleMs?: number): boolean`, `destroy(): void`. Consumed by Tasks 4 & 5.

- [ ] **Step 1: Add deps.**

```bash
cd frontend
# Fresh adds need the cooldown override (ephemeral); promotions do not.
pnpm add --config.minimumReleaseAge=0 y-protocols lib0
pnpm add --config.minimumReleaseAge=0 -D ws @types/ws
```
Then hand-edit `frontend/package.json`: move the existing `"yjs": "13.6.31"` line from `devDependencies` to `dependencies` (keep the exact version). Run:
```bash
pnpm install --frozen-lockfile 2>/dev/null || pnpm install
pnpm dedupe
node -e "console.log(require('yjs/package.json').version)"   # expect 13.6.31, single copy
```
Commit: `git add frontend/package.json frontend/pnpm-lock.yaml && git commit -m "build(collab): add y-protocols/lib0/ws, promote yjs to runtime dep"`

- [ ] **Step 2: Exclude the live probe from the default vitest run.**

The probe needs a live server + env; it must NOT run in the normal `pnpm test`. In `frontend/vite.config.ts` `test.include`/add `test.exclude`:

```ts
// inside test: { ... }
include: ["src/**/*.{test,spec}.{ts,tsx}"],
exclude: ["**/node_modules/**", "src/**/*.probe.test.ts"],
```
Run `pnpm test` to confirm it still passes (no probe collected). Commit.

- [ ] **Step 3: Write the provider unit test (RED).** `frontend/src/collab/collabProvider.test.ts`

```ts
import * as decoding from "lib0/decoding";
import { describe, expect, it, vi } from "vitest";
import * as Y from "yjs";
import { CollabProvider } from "./collabProvider";

const MESSAGE_SYNC = 0;
const SYNC_STEP1 = 0, SYNC_STEP2 = 1, SYNC_UPDATE = 2;

/** A synchronous fake WebSocket capturing sent frames and exposing hooks. */
class FakeSocket {
  static OPEN = 1;
  readyState = 0;
  bufferedAmount = 0;
  binaryType = "arraybuffer";
  sent: Uint8Array[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: ArrayBuffer }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {}
  send(b: Uint8Array) { this.sent.push(b); }
  close() { this.readyState = 3; this.onclose?.({ code: 1000 }); }
  // test helpers
  open() { this.readyState = 1; this.onopen?.(); }
  deliver(bytes: Uint8Array) { this.onmessage?.({ data: bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) }); }
  drop(code: number) { this.readyState = 3; this.onclose?.({ code }); }
}

function frameType(b: Uint8Array): { msg: number; sub: number } {
  const d = decoding.createDecoder(b);
  const msg = decoding.readVarUint(d);
  const sub = decoding.readVarUint(d);
  return { msg, sub };
}

function makeProvider(canWrite: boolean) {
  const sockets: FakeSocket[] = [];
  const WS = class extends FakeSocket { constructor(u: string) { super(u); sockets.push(this); } } as unknown as typeof WebSocket;
  const doc = new Y.Doc();
  const p = new CollabProvider({ url: "ws://x/api/ws/workspaces/w/draft", doc, canWrite, WebSocketImpl: WS, reconnectBaseMs: 10, reconnectMaxMs: 20 });
  return { p, doc, sockets: sockets as unknown as FakeSocket[] };
}

describe("CollabProvider", () => {
  it("sends its own STEP1 (with outer SYNC byte) on open", () => {
    const { sockets } = makeProvider(true);
    sockets[0].open();
    expect(sockets[0].sent).toHaveLength(1);
    const { msg, sub } = frameType(sockets[0].sent[0]);
    expect(msg).toBe(MESSAGE_SYNC);
    expect(sub).toBe(SYNC_STEP1);
  });

  it("a writer replies STEP2 to the server's STEP1", () => {
    const { p, sockets } = makeProvider(true);
    const s = sockets[0]; s.open(); s.sent.length = 0;
    // server STEP1 = create_sync_message equivalent: build one from a fresh doc
    const enc = require("lib0/encoding");
    const srv = new Y.Doc();
    const sync = require("y-protocols/sync");
    const e = enc.createEncoder(); enc.writeVarUint(e, MESSAGE_SYNC); sync.writeSyncStep1(e, srv);
    s.deliver(enc.toUint8Array(e));
    expect(s.sent).toHaveLength(1);
    expect(frameType(s.sent[0]).sub).toBe(SYNC_STEP2);
    void p;
  });

  it("a local edit produces exactly one SYNC_UPDATE frame", () => {
    const { doc, sockets } = makeProvider(true);
    const s = sockets[0]; s.open(); s.sent.length = 0;
    doc.getXmlFragment("prosemirror").push([new Y.XmlElement("paragraph")]);
    expect(s.sent).toHaveLength(1);
    expect(frameType(s.sent[0]).sub).toBe(SYNC_UPDATE);
  });

  it("applies inbound updates with the provider origin and does NOT echo them", () => {
    const { doc, sockets } = makeProvider(true);
    const s = sockets[0]; s.open(); s.sent.length = 0;
    // build an update from a separate doc and deliver it as SYNC_UPDATE
    const other = new Y.Doc();
    other.getXmlFragment("prosemirror").push([new Y.XmlElement("paragraph")]);
    const update = Y.encodeStateAsUpdate(other);
    const enc = require("lib0/encoding"); const sync = require("y-protocols/sync");
    const e = enc.createEncoder(); enc.writeVarUint(e, MESSAGE_SYNC); sync.writeUpdate(e, update);
    s.deliver(enc.toUint8Array(e));
    expect(doc.getXmlFragment("prosemirror").length).toBe(1); // applied
    expect(s.sent).toHaveLength(0);                            // not echoed back
  });

  it("read-only client sends its own STEP1 but NO STEP2 reply and NO update", () => {
    const { doc, sockets } = makeProvider(false);
    const s = sockets[0]; s.open();
    expect(s.sent).toHaveLength(1); // its own STEP1 (read request, ungated)
    s.sent.length = 0;
    // server STEP1 -> writer would reply STEP2; read-only suppresses it
    const enc = require("lib0/encoding"); const sync = require("y-protocols/sync");
    const srv = new Y.Doc(); const e = enc.createEncoder(); enc.writeVarUint(e, MESSAGE_SYNC); sync.writeSyncStep1(e, srv);
    s.deliver(enc.toUint8Array(e));
    // local edit also suppressed
    doc.getXmlFragment("prosemirror").push([new Y.XmlElement("paragraph")]);
    expect(s.sent).toHaveLength(0);
  });

  it("does NOT reconnect on 1008 (policy) or 1009 (too big)", () => {
    vi.useFakeTimers();
    const { sockets } = makeProvider(true);
    sockets[0].open(); sockets[0].drop(1008);
    vi.advanceTimersByTime(1000);
    expect(sockets).toHaveLength(1); // no new socket
    vi.useRealTimers();
  });

  it("reconnects on 1006/1011 with backoff", () => {
    vi.useFakeTimers();
    const { sockets } = makeProvider(true);
    sockets[0].open(); sockets[0].drop(1011);
    vi.advanceTimersByTime(50);
    expect(sockets.length).toBeGreaterThanOrEqual(2);
    vi.useRealTimers();
  });
});
```

- [ ] **Step 4: Run — expect FAIL** (`collabProvider` not found): `pnpm test src/collab/collabProvider.test.ts`

- [ ] **Step 5: Implement the provider.** `frontend/src/collab/collabProvider.ts`

```ts
import * as decoding from "lib0/decoding";
import * as encoding from "lib0/encoding";
import * as syncProtocol from "y-protocols/sync";
import type * as Y from "yjs";

const MESSAGE_SYNC = 0;
const WS_OPEN = 1;

export type ProviderStatus = "connecting" | "connected" | "offline" | "closed-policy";
type StatusListener = (s: ProviderStatus) => void;

export interface CollabProviderOptions {
  url: string;
  doc: Y.Doc;
  canWrite: boolean;
  /** Injectable for the Node probe / unit tests. Defaults to the global WebSocket. */
  WebSocketImpl?: typeof WebSocket;
  reconnectBaseMs?: number;
  reconnectMaxMs?: number;
}

export class CollabProvider {
  readonly doc: Y.Doc;
  private readonly url: string;
  private readonly canWrite: boolean;
  private readonly WS: typeof WebSocket;
  private readonly baseMs: number;
  private readonly maxMs: number;
  private ws: WebSocket | null = null;
  private _status: ProviderStatus = "connecting";
  private readonly listeners = new Set<StatusListener>();
  private attempts = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private destroyed = false;
  private lastLocalEditMs = 0;
  private readonly onUpdate: (update: Uint8Array, origin: unknown) => void;

  constructor(opts: CollabProviderOptions) {
    this.doc = opts.doc;
    this.url = opts.url;
    this.canWrite = opts.canWrite;
    this.WS = opts.WebSocketImpl ?? (globalThis.WebSocket as typeof WebSocket);
    this.baseMs = opts.reconnectBaseMs ?? 1000;
    this.maxMs = opts.reconnectMaxMs ?? 30000;
    this.onUpdate = (update, origin) => {
      // Origin-tag guard: an update we applied FROM the socket carries `this` as
      // its transaction origin — never echo it back (infinite loop otherwise).
      if (origin === this) return;
      this.lastLocalEditMs = Date.now();
      if (!this.canWrite) return; // read-only: never emit a write frame
      const enc = encoding.createEncoder();
      encoding.writeVarUint(enc, MESSAGE_SYNC);
      syncProtocol.writeUpdate(enc, update);
      this.send(encoding.toUint8Array(enc));
    };
    this.doc.on("update", this.onUpdate);
    this.connect();
  }

  get status(): ProviderStatus { return this._status; }
  get bufferedAmount(): number { return this.ws?.bufferedAmount ?? 0; }

  isSettled(settleMs = 350): boolean {
    return (
      this._status === "connected" &&
      this.ws?.readyState === WS_OPEN &&
      this.ws.bufferedAmount === 0 &&
      Date.now() - this.lastLocalEditMs > settleMs
    );
  }

  on(_ev: "status", cb: StatusListener): () => void {
    this.listeners.add(cb);
    return () => this.listeners.delete(cb);
  }

  destroy(): void {
    this.destroyed = true;
    if (this.timer) { clearTimeout(this.timer); this.timer = null; }
    this.doc.off("update", this.onUpdate);
    if (this.ws) { try { this.ws.close(); } catch { /* already closed */ } this.ws = null; }
    this.listeners.clear();
  }

  private setStatus(s: ProviderStatus): void {
    if (this._status === s) return;
    this._status = s;
    for (const l of this.listeners) l(s);
  }

  private send(bytes: Uint8Array): void {
    if (this.ws && this.ws.readyState === WS_OPEN) this.ws.send(bytes);
  }

  private connect(): void {
    if (this.destroyed) return;
    this.setStatus(this.attempts === 0 ? "connecting" : "offline");
    const ws = new this.WS(this.url);
    ws.binaryType = "arraybuffer";
    this.ws = ws;

    ws.onopen = () => {
      this.attempts = 0;
      this.setStatus("connected");
      // Client STEP1: download whatever the server has that we don't (read path).
      const enc = encoding.createEncoder();
      encoding.writeVarUint(enc, MESSAGE_SYNC);
      syncProtocol.writeSyncStep1(enc, this.doc);
      this.send(encoding.toUint8Array(enc));
    };

    ws.onmessage = (ev: MessageEvent) => {
      const data = new Uint8Array(ev.data as ArrayBuffer);
      const dec = decoding.createDecoder(data);
      const messageType = decoding.readVarUint(dec);
      if (messageType !== MESSAGE_SYNC) return; // AWARENESS (1) etc: ignored in 3b-3
      const enc = encoding.createEncoder();
      encoding.writeVarUint(enc, MESSAGE_SYNC);
      // readSyncMessage applies inbound STEP2/UPDATE to the doc with `this` as the
      // txn origin (origin-tag), and writes a STEP2 reply into `enc` iff it read a STEP1.
      syncProtocol.readSyncMessage(dec, enc, this.doc, this);
      if (encoding.length(enc) > 1 && this.canWrite) {
        // A STEP2 reply is a WRITE frame — read-only clients suppress it.
        this.send(encoding.toUint8Array(enc));
      }
    };

    ws.onclose = (ev: CloseEvent) => {
      this.ws = null;
      if (this.destroyed) return;
      if (ev.code === 1008 || ev.code === 1009) { this.setStatus("closed-policy"); return; }
      this.setStatus("offline");
      this.scheduleReconnect();
    };
    ws.onerror = () => { /* onclose follows */ };
  }

  private scheduleReconnect(): void {
    if (this.destroyed || this.timer) return;
    const delay = Math.min(this.baseMs * 2 ** this.attempts, this.maxMs);
    this.attempts += 1;
    this.timer = setTimeout(() => { this.timer = null; this.connect(); }, delay);
  }
}
```

- [ ] **Step 6: Run — expect PASS.** `pnpm test src/collab/collabProvider.test.ts`. Fix until green. Commit: `git add frontend/src/collab/collabProvider.ts frontend/src/collab/collabProvider.test.ts frontend/vite.config.ts && git commit -m "feat(collab): thin Yjs WS provider + unit tests"`

- [ ] **Step 7: Write the live probe (vitest, node env).** `frontend/src/collab/handshake.probe.test.ts`

```ts
// @vitest-environment node
import { describe, expect, it } from "vitest";
import WS from "ws";
import * as Y from "yjs";
import { CollabProvider } from "./collabProvider";

const URL = process.env.PROBE_WS_URL!;    // ws://127.0.0.1:PORT/api/ws/workspaces/<id>/draft
const COOKIE = process.env.PROBE_COOKIE!;  // session=<signed>
const ORIGIN = process.env.PROBE_ORIGIN!;  // must match app_base_url host:port

// Node `ws` needs headers; the browser sends the cookie automatically. Bind them here.
const WSWithAuth = class extends (WS as unknown as typeof WebSocket) {
  constructor(url: string) { super(url, { headers: { Cookie: COOKIE, Origin: ORIGIN } } as never); }
} as unknown as typeof WebSocket;

function settle(ms = 400) { return new Promise((r) => setTimeout(r, ms)); }

describe("collab handshake (live uvicorn)", () => {
  it("two clients converge and the edit persists", async () => {
    const docA = new Y.Doc();
    const a = new CollabProvider({ url: URL, doc: docA, canWrite: true, WebSocketImpl: WSWithAuth });
    const docB = new Y.Doc();
    const b = new CollabProvider({ url: URL, doc: docB, canWrite: true, WebSocketImpl: WSWithAuth });
    await settle();
    // A writes a paragraph with text
    const el = new Y.XmlElement("paragraph");
    el.push([new Y.XmlText("hello-from-A")]);
    docA.getXmlFragment("prosemirror").push([el]);
    await settle();
    expect(docB.getXmlFragment("prosemirror").toJSON()).toContain("hello-from-A"); // convergence
    a.destroy(); b.destroy();
    await settle();
    // Fresh client rebuilds from the durable log -> persistence
    const docC = new Y.Doc();
    const c = new CollabProvider({ url: URL, doc: docC, canWrite: false, WebSocketImpl: WSWithAuth });
    await settle();
    expect(docC.getXmlFragment("prosemirror").toJSON()).toContain("hello-from-A");
    c.destroy();
  }, 15000);
});
```

- [ ] **Step 8: Write the Python driver.** `scripts/collab_handshake_probe.py`

Boots a testcontainers Postgres, applies migrations, starts uvicorn in a background thread bound to that DB + a temp `workspaces_dir`, seeds an active workspace with a committed draft and collab enabled, prints the env, then runs the probe as a subprocess and exits with its code. Mirror the seed pattern from `tests/proposal_assist/collab/test_collab_ws_route.py` (`make_user`, `make_workspace(..., current_phase="active")`, `collab_state.enable_collab(...)`) and the repo seed from `tests/test_api_workspaces.py` (`_init_repo(tmp_path, ws_id, doc=...)`), and the cookie from `app.state.session_signer.make(user_id=...)`.

```python
"""Live handshake de-risk driver (Slice 3b-3, Task 1). GO/NO-GO gate.

Boots real Postgres + uvicorn, seeds a collab-enabled draft, runs the Node/ws
probe against the real server, exits with the probe's code. Run:

    EMBEDDING_MODEL=bge-m3 python scripts/collab_handshake_probe.py
"""
from __future__ import annotations
import os, subprocess, sys, tempfile, threading, time, uuid
from pathlib import Path

def main() -> int:
    os.environ.setdefault("EMBEDDING_MODEL", "bge-m3")
    from testcontainers.postgres import PostgresContainer
    import uvicorn
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    with PostgresContainer("postgres:16") as pg:
        db_url = pg.get_connection_url()  # postgresql+psycopg2://...
        # Normalize to the driver the app uses (psycopg v3) if needed:
        db_url = db_url.replace("postgresql+psycopg2://", "postgresql+psycopg://")
        workspaces = Path(tempfile.mkdtemp())
        os.environ["DATABASE_URL"] = db_url
        os.environ["WORKSPACES_DIR"] = str(workspaces)
        os.environ["SPA_MODE"] = "disabled"
        os.environ["APP_BASE_URL"] = "http://127.0.0.1:8199"

        # --- migrate ---
        from alembic import command
        from alembic.config import Config
        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", db_url)
        command.upgrade(cfg, "head")

        # --- build app + seed ---
        from call_manager.web.main import create_app  # adjust to the real factory
        app = create_app()
        engine = create_engine(db_url, future=True)
        Session = sessionmaker(bind=engine, future=True)
        app.state.session_factory = Session  # ensure the app uses this DB

        from call_manager.proposal_assist.collab import state as collab_state
        from call_manager.proposal_assist.paths import workspace_repo_dir
        from tests.proposal_assist.conftest import make_user, make_workspace
        from tests.test_api_workspaces import _init_repo  # (tmp_path, ws_id, doc=...)

        with Session() as s:
            owner = make_user(s, email=f"probe-{uuid.uuid4().hex[:8]}@ki.se")
            ws = make_workspace(s, owner=owner, current_phase="active")
            collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
            s.commit()
            wid, oid = ws.id, owner.id
        _init_repo(workspaces, wid, doc="Preamble.\n\n## A\n\nBody a.\n")
        cookie = "session=" + app.state.session_signer.make(user_id=oid)

        # --- uvicorn in a thread ---
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8199, log_level="warning"))
        t = threading.Thread(target=server.run, daemon=True); t.start()
        for _ in range(100):
            if server.started: break
            time.sleep(0.1)

        env = {**os.environ,
               "PROBE_WS_URL": f"ws://127.0.0.1:8199/api/ws/workspaces/{wid}/draft",
               "PROBE_COOKIE": cookie,
               "PROBE_ORIGIN": "http://127.0.0.1:8199"}
        rc = subprocess.call(
            ["pnpm", "exec", "vitest", "run", "src/collab/handshake.probe.test.ts"],
            cwd="frontend", env=env)
        server.should_exit = True
        print("HANDSHAKE_PROBE:", "GO" if rc == 0 else "NO-GO")
        return rc

if __name__ == "__main__":
    sys.exit(main())
```

Notes for the implementer: the exact app factory import (`create_app`) and how the app resolves its `session_factory`/`workspaces_dir`/`session_signer` must be matched to this repo (grep `create_app`, `session_signer`, `state.session_factory`). If the factory reads settings from env, the env vars set above are sufficient; if it takes explicit settings, construct them. The point of this task is the GO/NO-GO — if wiring uvicorn against testcontainers proves fiddly, an acceptable fallback is to drive the probe against a `uvicorn` already running on a dev DB, seeding via a thin script that prints the same three env values; record which path was used in the findings note.

- [ ] **Step 9: Run the gate.**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/collab-slice3b3
source .venv/bin/activate
EMBEDDING_MODEL=bge-m3 python scripts/collab_handshake_probe.py; echo "DRIVER_RC=$?"
```
Expected: the probe test passes, driver prints `HANDSHAKE_PROBE: GO`, `DRIVER_RC=0`. **If NO-GO: STOP.** Diagnose (frame framing? cookie/origin? artifact gate?) and fix the provider — do not weaken the probe. Record GO/NO-GO + evidence in `docs/superpowers/notes/2026-07-04-collab-slice3b3-notes.md`.

- [ ] **Step 10: Commit.** `git add frontend/src/collab/handshake.probe.test.ts scripts/collab_handshake_probe.py docs/superpowers/notes/2026-07-04-collab-slice3b3-notes.md frontend/package.json && git commit -m "test(collab): live handshake de-risk probe — GO"`

---

### Task 2: Tiptap schema extensions + schema-equality test

**Files:**
- Modify: `frontend/package.json` (add `@tiptap/core`, `@tiptap/pm`)
- Create: `frontend/src/collab/schema.ts` (Tiptap extensions matching `collab-schema.mjs`)
- Create: `frontend/src/collab/CitationNode.tsx` (citation Node **spec** here; the React NodeView is added in Task 4 — this task ships the schema-level node only)
- Create: `frontend/src/collab/collabSchema.equality.test.ts`

**Interfaces:**
- Consumes: `frontend/scripts/collab-schema.mjs` `export const schema` (the canonical ProseMirror schema).
- Produces: `export const collabExtensions: Extension[]` (array passed to Tiptap `useEditor`), consumed by Task 4. Node/mark type names MUST be exactly: nodes `doc, paragraph, text, heading, bullet_list, ordered_list, list_item, blockquote, code_block, horizontal_rule, image, hard_break, citation`; marks `link, em, strong, code, strikethrough`.

**Target schema (from `collab-schema.mjs` — the equality test enforces this):**

| Node | content | group | inline | atom | attrs (defaults) |
|---|---|---|---|---|---|
| doc | `block+` | — | — | — | — |
| paragraph | `inline*` | block | — | — | — |
| text | — | inline | — | — | — |
| heading | `inline*` | block | — | — | `level` (1) |
| bullet_list | `list_item+` | block | — | — | — |
| ordered_list | `list_item+` | block | — | — | (order default per prosemirror-schema-list) |
| list_item | `paragraph block*` | — | — | — | — |
| blockquote | `block+` | block | — | — | — |
| code_block | `text*` (marks `""`) | block | — | — | — |
| horizontal_rule | — | block | — | — | — |
| image | — | inline | true | — | `src`, `alt` (null), `title` (null) |
| hard_break | — | inline | true | — | — |
| citation | — | inline | true | true | `citekey`, `ev_id` |
| Mark | attrs (defaults) |
| link | `href`, `title` (null); `inclusive:false` |
| em / strong / code / strikethrough | — |

- [ ] **Step 1: Add Tiptap schema deps.**
```bash
cd frontend && pnpm add --config.minimumReleaseAge=0 @tiptap/core @tiptap/pm
```
Commit `package.json` + lockfile.

- [ ] **Step 2: Write the equality test (RED).** `frontend/src/collab/collabSchema.equality.test.ts`

```ts
import { getSchema } from "@tiptap/core";
import { describe, expect, it } from "vitest";
// The canonical twin (raw ProseMirror schema) — reuse it directly as truth.
import { schema as canonical } from "../../scripts/collab-schema.mjs";
import { collabExtensions } from "./schema";

const tiptap = getSchema(collabExtensions);

function nodeShape(s: any, name: string) {
  const n = s.nodes[name].spec;
  return {
    content: n.content ?? null,
    group: n.group ?? null,
    inline: !!n.inline,
    atom: !!n.atom,
    attrs: Object.fromEntries(Object.entries(n.attrs ?? {}).map(([k, v]: any) => [k, v.default ?? null])),
  };
}
function markShape(s: any, name: string) {
  const m = s.marks[name].spec;
  return { attrs: Object.fromEntries(Object.entries(m.attrs ?? {}).map(([k, v]: any) => [k, v.default ?? null])) };
}

describe("Tiptap schema ≡ collab-schema.mjs", () => {
  it("has the same node names", () => {
    expect(Object.keys(tiptap.nodes).sort()).toEqual(Object.keys(canonical.nodes).sort());
  });
  it("has the same mark names", () => {
    expect(Object.keys(tiptap.marks).sort()).toEqual(Object.keys(canonical.marks).sort());
  });
  it.each(Object.keys((canonical as any).nodes))("node %s matches structurally", (name) => {
    expect(nodeShape(tiptap, name)).toEqual(nodeShape(canonical, name));
  });
  it.each(Object.keys((canonical as any).marks))("mark %s matches structurally", (name) => {
    expect(markShape(tiptap, name)).toEqual(markShape(canonical, name));
  });
});
```
(If importing the `.mjs` under vitest needs a tweak, add `resolve` for the extension or import the schema via a tiny re-export; the golden tooling already imports it, so mirror that.)

- [ ] **Step 3: Run — expect FAIL** (`./schema` not found): `pnpm test src/collab/collabSchema.equality.test.ts`

- [ ] **Step 4: Implement `frontend/src/collab/schema.ts`.** Define each node/mark with `@tiptap/core` `Node.create` / `Mark.create` using the EXACT names/content/group/attrs from the target table. Full citation node:

```ts
import { Node, Mark, mergeAttributes, type Extensions } from "@tiptap/core";

const Doc = Node.create({ name: "doc", topNode: true, content: "block+" });
const Text = Node.create({ name: "text", group: "inline" });
const Paragraph = Node.create({
  name: "paragraph", group: "block", content: "inline*",
  parseHTML: () => [{ tag: "p" }], renderHTML: ({ HTMLAttributes }) => ["p", mergeAttributes(HTMLAttributes), 0],
});
const Heading = Node.create({
  name: "heading", group: "block", content: "inline*", defining: true,
  addAttributes: () => ({ level: { default: 1 } }),
  parseHTML: () => [1, 2, 3, 4, 5, 6].map((level) => ({ tag: `h${level}`, attrs: { level } })),
  renderHTML: ({ node, HTMLAttributes }) => [`h${node.attrs.level}`, mergeAttributes(HTMLAttributes), 0],
});
const Blockquote = Node.create({
  name: "blockquote", group: "block", content: "block+", defining: true,
  parseHTML: () => [{ tag: "blockquote" }], renderHTML: ({ HTMLAttributes }) => ["blockquote", mergeAttributes(HTMLAttributes), 0],
});
const HorizontalRule = Node.create({
  name: "horizontal_rule", group: "block",
  parseHTML: () => [{ tag: "hr" }], renderHTML: () => ["hr"],
});
const CodeBlock = Node.create({
  name: "code_block", group: "block", content: "text*", marks: "", code: true, defining: true,
  parseHTML: () => [{ tag: "pre", preserveWhitespace: "full" }], renderHTML: ({ HTMLAttributes }) => ["pre", ["code", mergeAttributes(HTMLAttributes), 0]],
});
const HardBreak = Node.create({
  name: "hard_break", group: "inline", inline: true, selectable: false,
  parseHTML: () => [{ tag: "br" }], renderHTML: () => ["br"],
});
const Image = Node.create({
  name: "image", group: "inline", inline: true, draggable: true,
  addAttributes: () => ({ src: { default: null }, alt: { default: null }, title: { default: null } }),
  parseHTML: () => [{ tag: "img[src]" }], renderHTML: ({ HTMLAttributes }) => ["img", mergeAttributes(HTMLAttributes)],
});
const BulletList = Node.create({
  name: "bullet_list", group: "block", content: "list_item+",
  parseHTML: () => [{ tag: "ul" }], renderHTML: ({ HTMLAttributes }) => ["ul", mergeAttributes(HTMLAttributes), 0],
});
const OrderedList = Node.create({
  name: "ordered_list", group: "block", content: "list_item+",
  addAttributes: () => ({ order: { default: 1 } }),
  parseHTML: () => [{ tag: "ol" }], renderHTML: ({ HTMLAttributes }) => ["ol", mergeAttributes(HTMLAttributes), 0],
});
const ListItem = Node.create({
  name: "list_item", content: "paragraph block*", defining: true,
  parseHTML: () => [{ tag: "li" }], renderHTML: ({ HTMLAttributes }) => ["li", mergeAttributes(HTMLAttributes), 0],
});
export const Citation = Node.create({
  name: "citation", group: "inline", inline: true, atom: true,
  addAttributes: () => ({ citekey: { default: null }, ev_id: { default: null } }),
  parseHTML: () => [{ tag: "span[data-citekey][data-ev-id]" }],
  renderHTML: ({ HTMLAttributes }) => ["span", mergeAttributes(HTMLAttributes)],
  // NodeView (pill) is attached in Task 4 via addNodeView.
});

const Link = Mark.create({
  name: "link", inclusive: false,
  addAttributes: () => ({ href: { default: null }, title: { default: null } }),
  parseHTML: () => [{ tag: "a[href]" }], renderHTML: ({ HTMLAttributes }) => ["a", mergeAttributes(HTMLAttributes), 0],
});
const Em = Mark.create({ name: "em", parseHTML: () => [{ tag: "em" }, { tag: "i" }], renderHTML: () => ["em", 0] });
const Strong = Mark.create({ name: "strong", parseHTML: () => [{ tag: "strong" }, { tag: "b" }], renderHTML: () => ["strong", 0] });
const Code = Mark.create({ name: "code", code: true, parseHTML: () => [{ tag: "code" }], renderHTML: () => ["code", 0] });
const Strikethrough = Mark.create({
  name: "strikethrough", parseHTML: () => [{ tag: "s" }, { tag: "strike" }, { tag: "del" }], renderHTML: () => ["s", 0],
});

export const collabExtensions: Extensions = [
  Doc, Text, Paragraph, Heading, Blockquote, HorizontalRule, CodeBlock, HardBreak,
  Image, BulletList, OrderedList, ListItem, Citation, Link, Em, Strong, Code, Strikethrough,
];
```
Adjust attr defaults until the equality test's `attrs` comparison matches `collab-schema.mjs` exactly (e.g. `ordered_list` order default; `image`/`link` nullable defaults). The equality test is the acceptance gate.

- [ ] **Step 5: Run — expect PASS.** `pnpm test src/collab/collabSchema.equality.test.ts`. Iterate until green (this is the drift guard; every field must match).

- [ ] **Step 6: `tsc` + commit.** `pnpm exec tsc --noEmit` then `git add frontend/src/collab/schema.ts frontend/src/collab/collabSchema.equality.test.ts frontend/package.json && git commit -m "feat(collab): Tiptap schema matching collab-schema.mjs + equality test"`

---

### Task 3: Backend read-model fields (`collab_enabled` + `can_write`) + OpenAPI regen

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py` (imports; `DraftOut` +2 fields; `SpecOut` +2 fields; `get_draft` + `get_spec` populate them)
- Create: `tests/proposal_assist/test_readmodel_collab_fields.py`
- Modify: `frontend/src/api/schema.gen.ts` (regenerated)

**Interfaces:**
- Produces: `DraftOut.collab_enabled: bool`, `DraftOut.can_write: bool`, `SpecOut.collab_enabled: bool`, `SpecOut.can_write: bool` — consumed by Task 5 (`draft.data.collab_enabled` / `.can_write`).

- [ ] **Step 1: Write the backend test (RED).** `tests/proposal_assist/test_readmodel_collab_fields.py` — mirror the seed helpers in `tests/test_api_workspaces.py` (`_setup`, `_init_repo`, `_client`). Assert: collab OFF by default (`collab_enabled is False`); owner has `can_write True`; after `enable_collab(draft)`, `collab_enabled is True`; a non-owner active member has `can_write True` (this is the amendment — proves `can_write` follows `resolve_access`, not the owner-only `can_edit`).

```python
import uuid
from call_manager.proposal_assist.collab import state as collab_state
# reuse the module's existing helpers:
from tests.test_api_workspaces import _init_repo, _client, _setup_owner_and_member  # adapt names to actual

def test_draft_exposes_collab_enabled_and_can_write(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app, ws_id, owner_id, member_id = _setup_owner_and_member(monkeypatch, pg_url, pg_session_factory, tmp_path)
    _init_repo(tmp_path, ws_id, doc="Preamble.\n\n## A\n\nBody.\n")

    # collab OFF by default
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/draft")
    assert r.status_code == 200
    assert r.json()["collab_enabled"] is False
    assert r.json()["can_write"] is True         # owner

    # a non-owner ACTIVE member: can_edit is owner-only today, but can_write must be True
    rm = _client(app, member_id).get(f"/api/workspaces/{ws_id}/draft")
    assert rm.json()["can_edit"] is False        # unchanged owner-oriented semantics
    assert rm.json()["can_write"] is True        # resolver semantics (the amendment)

    # enable collab -> flag flips True
    with pg_session_factory() as s:
        collab_state.enable_collab(s, workspace_id=ws_id, artifact="draft"); s.commit()
    r2 = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/draft")
    assert r2.json()["collab_enabled"] is True
```
If `test_api_workspaces.py` has no member+active fixture, add a small local one following its `_setup` pattern (owner + one `WorkspaceViewer`, `current_phase="active"`). Add the symmetric spec assertions in a second test (unlocked spec → member `can_write True`).

- [ ] **Step 2: Run — expect FAIL** (KeyError `collab_enabled`): `EMBEDDING_MODEL=bge-m3 pytest tests/proposal_assist/test_readmodel_collab_fields.py -p no:cacheprovider -q`

- [ ] **Step 3: Add the fields + populate.** In `src/call_manager/web/api/workspaces.py`:

Add imports near the top:
```python
from call_manager.proposal_assist.collab import state as collab_state
from call_manager.proposal_assist.collab.ws_auth import resolve_access
```
`DraftOut` (line ~109) — add two fields:
```python
class DraftOut(BaseModel):
    head: str
    can_edit: bool
    sections: list[SectionOut]
    citations: dict[str, CitationOut]
    collab_enabled: bool
    can_write: bool
```
`SpecOut` (line ~117) — add the same two fields.
In `get_draft` (before building the response, while the session is open):
```python
_, can_write = resolve_access(session, user=user, workspace_id=workspace_id, artifact="draft")
collab_enabled = collab_state.is_collab_enabled(session, workspace_id=workspace_id, artifact="draft")
```
and pass `collab_enabled=collab_enabled, can_write=can_write` into `DraftOut(...)`. Do the same in `get_spec` with `artifact="spec"`. (`resolve_access` re-checks membership; `require_member` already guaranteed it, so the returned `is_member` is discarded.)

- [ ] **Step 4: Run — expect PASS.** Same pytest command. Then run the existing endpoint tests to confirm parity: `EMBEDDING_MODEL=bge-m3 pytest tests/test_api_workspaces.py -p no:cacheprovider -q` (all pass; the additive fields don't break existing assertions).

- [ ] **Step 5: Regenerate OpenAPI types.**
```bash
cd frontend && EMBEDDING_MODEL=bge-m3 pnpm gen:api
git diff --stat src/api/schema.gen.ts   # should show DraftOut/SpecOut gaining collab_enabled + can_write
pnpm exec tsc --noEmit
```

- [ ] **Step 6: Commit.** `git add src/call_manager/web/api/workspaces.py tests/proposal_assist/test_readmodel_collab_fields.py frontend/src/api/schema.gen.ts && git commit -m "feat(api): additive DraftOut/SpecOut collab_enabled + can_write (resolve_access)"`

---

### Task 4: CollabEditor + CitationNode NodeView + useCollabDoc + useSaveVersion

**Files:**
- Modify: `frontend/package.json` (add `@tiptap/react`, `@tiptap/extension-collaboration`; promote `y-prosemirror` dev→runtime)
- Create: `frontend/src/collab/CitationNode.tsx` (React NodeView pill — attach to the `Citation` node from Task 2)
- Create: `frontend/src/collab/useCollabDoc.ts` (Y.Doc + provider lifecycle hook)
- Create: `frontend/src/collab/useSaveVersion.ts` (checkpoint mutation)
- Create: `frontend/src/collab/CollabEditor.tsx`
- Create: `frontend/src/collab/CollabEditor.test.tsx` (jsdom smoke: renders, read-only when `canWrite=false`)

**Interfaces:**
- Consumes: `collabExtensions` (Task 2), `CollabProvider` (Task 1), `collab_enabled`/`can_write` (Task 3).
- Produces: `<CollabEditor workspaceId={string} artifact={"draft"|"spec"} canWrite={boolean} citations={Record<string,CitationOut>} />` — mounted by Task 5. `useSaveVersion(id, artifact)` → `{ mutateAsync, isPending }`. `useCollabDoc({ workspaceId, artifact, canWrite })` → `{ doc, provider, status }`.

- [ ] **Step 1: Add deps.**
```bash
cd frontend && pnpm add --config.minimumReleaseAge=0 @tiptap/react @tiptap/extension-collaboration
```
Promote `y-prosemirror` dev→runtime in `package.json` (move the line, keep `1.3.7`). `pnpm install && pnpm dedupe` then verify single `y-prosemirror`/`yjs`. Commit.

- [ ] **Step 2: `useSaveVersion` hook.** `frontend/src/collab/useSaveVersion.ts`
```ts
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "../api/client";

export function useSaveVersion(id: string, artifact: "draft" | "spec") {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(await api.POST("/api/workspaces/{workspace_id}/collab/{artifact}/checkpoint", {
        params: { path: { workspace_id: id, artifact } },
      })),
    // Refetch the artifact so the Outline reflects the checkpointed version (§7.4).
    onSuccess: () => qc.invalidateQueries({ queryKey: [artifact, id] }),
  });
}
```

- [ ] **Step 3: `useCollabDoc` hook.** `frontend/src/collab/useCollabDoc.ts` — builds the Y.Doc + provider once per `(workspaceId, artifact)`, tears down on unmount, tracks status.
```ts
import { useEffect, useRef, useState } from "react";
import * as Y from "yjs";
import { CollabProvider, type ProviderStatus } from "./collabProvider";

function wsUrl(workspaceId: string, artifact: string): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/api/ws/workspaces/${workspaceId}/${artifact}`;
}

export function useCollabDoc(opts: { workspaceId: string; artifact: "draft" | "spec"; canWrite: boolean }) {
  const [status, setStatus] = useState<ProviderStatus>("connecting");
  const ref = useRef<{ doc: Y.Doc; provider: CollabProvider } | null>(null);
  if (ref.current === null) {
    const doc = new Y.Doc();
    const provider = new CollabProvider({ url: wsUrl(opts.workspaceId, opts.artifact), doc, canWrite: opts.canWrite });
    ref.current = { doc, provider };
  }
  useEffect(() => {
    const { provider } = ref.current!;
    const off = provider.on("status", setStatus);
    return () => { off(); provider.destroy(); ref.current!.doc.destroy(); ref.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opts.workspaceId, opts.artifact]);
  return { doc: ref.current.doc, provider: ref.current.provider, status };
}
```
(The `key` on `<CollabEditor>` — Task 5 — forces a fresh mount per artifact so the `ref`-once construction is correct.)

- [ ] **Step 4: Citation NodeView.** `frontend/src/collab/CitationNode.tsx` — a read-only React NodeView bound to the `Citation` node. Reuse the existing pill visual (grep the CM6 `citations.ts`/pill CSS for the class names).
```tsx
import { NodeViewWrapper, ReactNodeViewRenderer } from "@tiptap/react";
import { Citation } from "./schema";

function Pill(props: { node: { attrs: { citekey: string; ev_id: string } } }) {
  const { citekey } = props.node.attrs;
  return <NodeViewWrapper as="span" className="citation-pill" data-testid="citation-pill">[{citekey}]</NodeViewWrapper>;
}

/** The schema-level Citation from Task 2, augmented with a read-only NodeView. */
export const CitationWithView = Citation.extend({ addNodeView: () => ReactNodeViewRenderer(Pill) });
```
In `schema.ts`'s `collabExtensions`, the Task-4 editor swaps `Citation` → `CitationWithView` (the schema is identical — only the view differs, so the equality test still passes on `collabExtensions`; the editor composes `[...collabExtensions.filter(e => e.name !== 'citation'), CitationWithView]` or export a `collabEditorExtensions`). Add `collabEditorExtensions` to `schema.ts` to keep the equality test importing the pure `collabExtensions`.

- [ ] **Step 5: CollabEditor.** `frontend/src/collab/CollabEditor.tsx`
```tsx
import Collaboration from "@tiptap/extension-collaboration";
import { EditorContent, useEditor } from "@tiptap/react";
import { collabEditorExtensions } from "./schema";
import { useCollabDoc } from "./useCollabDoc";

export function CollabEditor(props: {
  workspaceId: string;
  artifact: "draft" | "spec";
  canWrite: boolean;
}) {
  const { doc, provider, status } = useCollabDoc(props);
  const editor = useEditor(
    {
      editable: props.canWrite,
      extensions: [
        ...collabEditorExtensions,
        Collaboration.configure({ document: doc, field: "prosemirror" }),
      ],
    },
    [doc], // rebuild the editor if the doc identity changes
  );
  return (
    <div data-collab-status={status}>
      <EditorContent editor={editor} data-testid="collab-editor" />
    </div>
  );
  void provider;
}
```
Notes: `Collaboration` disables Tiptap's own history (uses y-undo — undo/redo keymap comes with the extension). `field: "prosemirror"` MUST match the server XmlFragment name. `editable` is keyed off `canWrite` (NOT `can_edit`).

- [ ] **Step 6: jsdom smoke test.** `frontend/src/collab/CollabEditor.test.tsx` — mock the provider transport (inject a fake socket that never connects, or mock `./collabProvider`) so the test doesn't open a real WS; assert the editor renders and is contenteditable only when `canWrite`. Keep it a smoke test — the real round-trip is the Playwright e2e (Task 6) and the handshake probe (Task 1).
```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
vi.mock("./collabProvider", () => ({
  CollabProvider: class { on() { return () => {}; } destroy() {} get status() { return "connected"; } },
}));
import { CollabEditor } from "./CollabEditor";
describe("CollabEditor", () => {
  it("renders read-only when canWrite is false", () => {
    render(<CollabEditor workspaceId="w" artifact="draft" canWrite={false} />);
    const el = screen.getByTestId("collab-editor").querySelector('[contenteditable]');
    expect(el?.getAttribute("contenteditable")).toBe("false");
  });
});
```

- [ ] **Step 7: `tsc` + vitest + commit.** `pnpm exec tsc --noEmit && pnpm test src/collab/` then commit all Task-4 files.

---

### Task 5: Cockpit integration — conditional mount, StatusBar, Save-a-version, Vite proxy

**Files:**
- Modify: `frontend/src/cockpit/DraftPanel.tsx` (conditional mount)
- Modify: `frontend/src/cockpit/SpecPanel.tsx` (conditional mount)
- Modify: `frontend/src/cockpit/editorStore.ts` (widen `SaveState` additively)
- Modify: `frontend/src/shell/StatusBar.tsx` (`SAVE_LABEL` entries for the new states)
- Create: `frontend/src/collab/SaveVersionButton.tsx` (settled-gated button, publishes status to `editorStore`)
- Modify: `frontend/vite.config.ts` (WS-enabled `/api/ws` proxy)
- Create: `frontend/src/collab/collabStatus.ts` (map provider status + canWrite → `SaveState`)
- Modify test: extend `frontend/src/collab/CollabEditor.test.tsx` or add `frontend/src/cockpit/DraftPanel.collab.test.tsx` (renders CollabEditor when `collab_enabled`, ArtifactSectionEditor otherwise)

**Interfaces:**
- Consumes: `CollabEditor` (Task 4), `useSaveVersion` (Task 4), `collab_enabled`/`can_write` (Task 3), `setSave`/`SaveState` (`editorStore`).

- [ ] **Step 1: Widen `SaveState` (additive).** `frontend/src/cockpit/editorStore.ts` line 3:
```ts
export type SaveState =
  | "idle" | "saving" | "saved" | "dirty" | "error"
  | "collab-connecting" | "collab-synced" | "collab-offline" | "collab-readonly";
```
`StatusBar.tsx` `SAVE_LABEL` (Record<SaveState,string> — MUST be exhaustive or `tsc` fails) add:
```ts
  "collab-connecting": "Connecting…",
  "collab-synced": "Synced",
  "collab-offline": "Offline — reconnecting…",
  "collab-readonly": "Read-only",
```
Run `pnpm exec tsc --noEmit` — confirm no exhaustiveness error. The CM6 path still emits only the original five (parity). Commit.

- [ ] **Step 2: Status mapping.** `frontend/src/collab/collabStatus.ts`
```ts
import type { SaveState } from "../cockpit/editorStore";
import type { ProviderStatus } from "./collabProvider";
export function collabSaveState(status: ProviderStatus, canWrite: boolean): SaveState {
  if (!canWrite) return "collab-readonly";
  switch (status) {
    case "connected": return "collab-synced";
    case "offline": return "collab-offline";
    case "closed-policy": return "collab-readonly";
    default: return "collab-connecting";
  }
}
```

- [ ] **Step 3: Save-a-version button (settled-gated).** `frontend/src/collab/SaveVersionButton.tsx`
```tsx
import { Button } from "@mantine/core";
import { useEffect, useState } from "react";
import { setSave } from "../cockpit/editorStore";
import type { CollabProvider } from "./collabProvider";
import { collabSaveState } from "./collabStatus";
import { useSaveVersion } from "./useSaveVersion";

export function SaveVersionButton(props: {
  id: string; artifact: "draft" | "spec"; provider: CollabProvider; canWrite: boolean; status: string;
}) {
  const save = useSaveVersion(props.id, props.artifact);
  const [settled, setSettled] = useState(false);
  useEffect(() => {
    // Poll the settled predicate (socket open + drained + idle since last edit).
    const t = setInterval(() => setSettled(props.provider.isSettled()), 200);
    return () => clearInterval(t);
  }, [props.provider]);
  useEffect(() => { setSave(collabSaveState(props.status as never, props.canWrite)); }, [props.status, props.canWrite]);
  const disabled = !props.canWrite || !settled || save.isPending;
  return (
    <Button size="xs" variant="default" disabled={disabled} onClick={() => void save.mutateAsync()}>
      {save.isPending ? "Saving…" : "Save a version"}
    </Button>
  );
}
```
Wire `SaveVersionButton` into `CollabEditor` (pass `provider`/`status`/`canWrite`), replacing the CM6 per-section save affordance on this path.

- [ ] **Step 4: Conditional mount.** `DraftPanel.tsx` — after `const { head, can_edit, sections } = draft.data;` add:
```tsx
  if (draft.data.collab_enabled) {
    return (
      <Stack gap="sm">
        {/* keep the Export ▾ menu group above */}
        <CollabEditor key={`collab-draft:${id}`} workspaceId={id} artifact="draft" canWrite={draft.data.can_write} citations={draft.data.citations} />
      </Stack>
    );
  }
```
(Leave the existing `ArtifactSectionEditor` return as the else path — untouched.) Mirror in `SpecPanel.tsx` with `artifact="spec"`, `canWrite={spec.data.can_write}`, no citations.

- [ ] **Step 5: Vite WS proxy.** `frontend/vite.config.ts` — add a dedicated WS-enabled entry BEFORE the catch-all so the upgrade is proxied (the `^/` entry has `ws:false`):
```ts
    proxy: {
      "/api/ws": { target: BACKEND.replace(/^http/, "ws"), ws: true, changeOrigin: true },
      "^/": { target: BACKEND, changeOrigin: true, ws: false, bypass(req) { /* unchanged */ } },
    },
```
(Confirm `BACKEND` is `http://localhost:8000`; the WS target is `ws://localhost:8000`.)

- [ ] **Step 6: Panel test.** Add a test that `DraftPanel` renders `<CollabEditor>` (by `data-testid="collab-editor"`, provider mocked) when `collab_enabled:true` and `ArtifactSectionEditor` otherwise — mock `useDraft` to return each shape.

- [ ] **Step 7: `tsc` + vitest + commit.** `pnpm exec tsc --noEmit && pnpm test src/` then commit all Task-5 files.

---

### Task 6: Playwright e2e round-trip

**Files:**
- Create: `frontend/e2e/collab.spec.ts`
- Create/extend: an e2e seed helper (Python) that creates an active workspace + committed draft + `enable_collab(draft)` and yields `workspace_id` + a login the Playwright context can use (mirror the existing e2e auth setup in `frontend/e2e/*.spec.ts` + `playwright.config.ts`).

**Interfaces:** none exported; validates the full stack behind the flag.

- [ ] **Step 1: Seed + serve.** Reuse the existing e2e harness (grep `frontend/e2e` + `playwright.config.ts` for how the backend + SPA are started and how auth is seeded). Enable collab for the seeded workspace's draft. If the e2e harness runs against `SPA_MODE=dev` (Vite + proxy), the WS proxy from Task 5 Step 5 is exercised; confirm the dev CSP permits the WS (§11 of the spec).

- [ ] **Step 2: Write the e2e.** `frontend/e2e/collab.spec.ts`
```ts
import { expect, test } from "@playwright/test";

test("two clients converge; edit persists; Save a version checkpoints", async ({ browser }) => {
  const ctxA = await browser.newContext(/* authed storageState */);
  const ctxB = await browser.newContext(/* authed storageState */);
  const a = await ctxA.newPage();
  const b = await ctxB.newPage();
  const url = `/cockpit/${process.env.E2E_COLLAB_WS_ID}`;
  await a.goto(url); await b.goto(url);
  const edA = a.getByTestId("collab-editor");
  await edA.click();
  await a.keyboard.type("hello-collab");
  await expect(b.getByTestId("collab-editor")).toContainText("hello-collab", { timeout: 5000 }); // convergence
  await a.getByRole("button", { name: /save a version/i }).click();
  await expect(a.getByRole("button", { name: /save a version/i })).toBeEnabled({ timeout: 5000 });
  await a.reload();
  await expect(a.getByTestId("collab-editor")).toContainText("hello-collab"); // persistence
});
```

- [ ] **Step 3: Run.** `pnpm exec playwright test collab.spec.ts` (or the repo's e2e script). Iterate until green. Commit.

---

### Task 7: Final parity sweep + whole-branch review + handoff

**Files:** none (or small fixes surfaced by review).

- [ ] **Step 1: Frontend gates.**
```bash
cd frontend
pnpm exec tsc --noEmit && pnpm test && pnpm build
```
All green; `build` (which runs `tsc && vite build`) must succeed.

- [ ] **Step 2: Backend parity sweep (flag-off).**
```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/collab-slice3b3 && source .venv/bin/activate
EMBEDDING_MODEL=bge-m3 pytest tests/proposal_assist/ tests/test_api_workspaces.py tests/test_api_spec_lock.py tests/proposal_assist/proposals/test_accepting.py -p no:cacheprovider > /tmp/3b3-parity.log 2>&1; echo "PYTEST_EXIT=$?"
grep -E "passed|failed|error" /tmp/3b3-parity.log | tail -5
```
Expect `PYTEST_EXIT=0`, no failures. (`| tail` WITHOUT capturing the real exit masks it — always `> log; echo $?`.) Confirm `git grep -n "enable_collab\|is_collab_enabled" src/` shows the flag only in `state.py` + the new read-model reads — no prod default flip.

- [ ] **Step 3: `ruff`.** `ruff check .` — clean. Confirm no migration added: `git diff --name-only master..HEAD -- 'alembic/**'` is empty.

- [ ] **Step 4: Whole-branch review.** Dispatch a code-reviewer subagent (superpowers:requesting-code-review, most-capable model) over `git diff master..HEAD`, scoped to: (a) provider protocol correctness (framing, origin-tag, read-only suppression, reconnect), (b) schema equality integrity, (c) flag-off parity (CM6 path untouched; `can_edit` unchanged), (d) no prod flag flip / no migration. Dispatch ONE fix subagent for any Critical/Important findings. Do NOT invoke `/code-review` or `/ultrareview` (user-billed).

- [ ] **Step 5: Handoff.** Write `/tmp/collab-slice3b3-STATUS.md`: per-task commit SHAs, exact test commands + pass counts, the handshake GO/NO-GO evidence, the final review verdict, and `git -C <worktree> log --oneline master..HEAD`. **STOP — do not merge.** The human verifies + merges.

---

## Self-Review

**Spec coverage:** §1 scope → Tasks 1–6; §2 constraints → Global Constraints + Task 7; §3 modules → Tasks 1/2/4/5; §4 provider (framing/STEP1/origin/reconnect/proxy) → Task 1 + Task 5 Step 5; §5 schema+citation+editable → Tasks 2/4; §6 read-model → Task 3; §7 save-version+settled+editorStore+Outline → Tasks 4/5; §8 tests (probe/equality/unit/e2e) → Tasks 1/2/4/6; §9 process → Global + Task 7; §10 deferred → not built (correct); §11 CSP → Task 6 Step 1 (verify dev CSP only). No gaps.

**Placeholder scan:** No "TBD"/"handle errors"/vague steps — every code step carries real code; the two spots that say "mirror the existing helper" (Task 3 fixture, Task 6 auth) point at named existing files because the exact fixture is repo-local and must be read, not invented.

**Type consistency:** `CollabProvider` ctor/`status`/`isSettled`/`bufferedAmount`/`on`/`destroy` used identically across Tasks 1/4/5. `collabExtensions` (equality test) vs `collabEditorExtensions` (editor, with NodeView) distinction is explicit (Task 4 Step 4). `SaveState` widened once (Task 5 Step 1) and consumed via `collabSaveState`. `collab_enabled`/`can_write` defined in Task 3, consumed in Task 5. `field: "prosemirror"` matches the server XmlFragment name.
