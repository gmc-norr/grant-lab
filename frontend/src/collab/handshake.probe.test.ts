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

  it("awareness converges: A sets presence, B sees it", async () => {
    const docA = new Y.Doc();
    const a = new CollabProvider({ url: URL, doc: docA, canWrite: true, WebSocketImpl: WSWithAuth });
    const docB = new Y.Doc();
    const b = new CollabProvider({ url: URL, doc: docB, canWrite: true, WebSocketImpl: WSWithAuth });
    await settle();
    a.awareness.setLocalStateField("user", { name: "Alice", color: "#ff0000" });
    await settle();
    const names = [...b.awareness.getStates().values()].map((s: any) => s?.user?.name);
    expect(names).toContain("Alice");           // GO: JS↔Python awareness interop works
    a.destroy(); b.destroy();
  }, 15000);
});
