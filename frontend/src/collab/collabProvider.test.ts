import * as decoding from "lib0/decoding";
import * as encoding from "lib0/encoding";
import * as sync from "y-protocols/sync";
import { describe, expect, it, vi } from "vitest";
import * as Y from "yjs";
import * as awarenessProtocol from "y-protocols/awareness";
import { CollabProvider } from "./collabProvider";

const MESSAGE_SYNC = 0;
const MESSAGE_AWARENESS = 1;
const SYNC_STEP1 = 0, SYNC_STEP2 = 1, SYNC_UPDATE = 2;

function firstMsgType(b: Uint8Array): number {
  return decoding.readVarUint(decoding.createDecoder(b));
}
function awarenessFrameFor(user: Record<string, unknown>): Uint8Array {
  const d = new Y.Doc();
  const aw = new awarenessProtocol.Awareness(d);
  aw.setLocalStateField("user", user);
  const e = encoding.createEncoder();
  encoding.writeVarUint(e, MESSAGE_AWARENESS);
  encoding.writeVarUint8Array(e, awarenessProtocol.encodeAwarenessUpdate(aw, [d.clientID]));
  return encoding.toUint8Array(e);
}

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

function makeProvider(canWrite: boolean, extra: { maxReconnectAttempts?: number } = {}) {
  const sockets: FakeSocket[] = [];
  const WS = class extends FakeSocket { constructor(u: string) { super(u); sockets.push(this); } } as unknown as typeof WebSocket;
  const doc = new Y.Doc();
  const p = new CollabProvider({ url: "ws://x/api/ws/workspaces/w/draft", doc, canWrite, WebSocketImpl: WS, reconnectBaseMs: 10, reconnectMaxMs: 20, ...extra });
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
    const srv = new Y.Doc();
    const e = encoding.createEncoder(); encoding.writeVarUint(e, MESSAGE_SYNC); sync.writeSyncStep1(e, srv);
    s.deliver(encoding.toUint8Array(e));
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
    const e = encoding.createEncoder(); encoding.writeVarUint(e, MESSAGE_SYNC); sync.writeUpdate(e, update);
    s.deliver(encoding.toUint8Array(e));
    expect(doc.getXmlFragment("prosemirror").length).toBe(1); // applied
    expect(s.sent).toHaveLength(0);                            // not echoed back
  });

  it("read-only client sends its own STEP1 but NO STEP2 reply and NO update", () => {
    const { doc, sockets } = makeProvider(false);
    const s = sockets[0]; s.open();
    expect(s.sent).toHaveLength(1); // its own STEP1 (read request, ungated)
    s.sent.length = 0;
    // server STEP1 -> writer would reply STEP2; read-only suppresses it
    const srv = new Y.Doc(); const e = encoding.createEncoder(); encoding.writeVarUint(e, MESSAGE_SYNC); sync.writeSyncStep1(e, srv);
    s.deliver(encoding.toUint8Array(e));
    // local edit also suppressed
    doc.getXmlFragment("prosemirror").push([new Y.XmlElement("paragraph")]);
    expect(s.sent).toHaveLength(0);
  });

  it("does NOT reconnect on 1008 (policy) or 1009 (too big)", () => {
    vi.useFakeTimers();
    // 1008: policy violation
    const { sockets: sockets1008 } = makeProvider(true);
    sockets1008[0].open(); sockets1008[0].drop(1008);
    vi.advanceTimersByTime(1000);
    expect(sockets1008).toHaveLength(1); // no new socket
    // 1009: message too big
    const { sockets: sockets1009 } = makeProvider(true);
    sockets1009[0].open(); sockets1009[0].drop(1009);
    vi.advanceTimersByTime(1000);
    expect(sockets1009).toHaveLength(1); // no new socket
    vi.useRealTimers();
  });

  it("reconnects on 1006/1011 with backoff", () => {
    vi.useFakeTimers();
    // 1011: server error
    const { sockets: sockets1011 } = makeProvider(true);
    sockets1011[0].open(); sockets1011[0].drop(1011);
    vi.advanceTimersByTime(50);
    expect(sockets1011.length).toBeGreaterThanOrEqual(2);
    // 1006: abnormal closure
    const { sockets: sockets1006 } = makeProvider(true);
    sockets1006[0].open(); sockets1006[0].drop(1006);
    vi.advanceTimersByTime(50);
    expect(sockets1006.length).toBeGreaterThanOrEqual(2);
    // 1013 "try again later": the server sends this when the first-ever open of a
    // room loses the workspace commit mutex (collab_ws.py). It is transient
    // contention, so retrying is the CORRECT behaviour — this asserts 1013 stays
    // out of the no-reconnect set above.
    const { sockets: sockets1013 } = makeProvider(true);
    sockets1013[0].open(); sockets1013[0].drop(1013);
    vi.advanceTimersByTime(50);
    expect(sockets1013.length).toBeGreaterThanOrEqual(2);
    vi.useRealTimers();
  });

  it("gives up after maxReconnectAttempts consecutive failures (no infinite loop)", () => {
    vi.useFakeTimers();
    // Never open() → every connect fails; drop the newest socket to drive the next.
    const { sockets } = makeProvider(true, { maxReconnectAttempts: 2 });
    for (let i = 0; i < 6; i++) {
      sockets[sockets.length - 1].drop(1006);
      vi.advanceTimersByTime(50);
    }
    // initial + exactly 2 reconnects, then it stops — this is what bounds the
    // pre-accept-1008-surfaced-as-1006 permanent-rejection loop.
    expect(sockets).toHaveLength(3);
    vi.useRealTimers();
  });

  it("sends an AWARENESS frame when the local awareness changes", () => {
    const { p, sockets } = makeProvider(true);
    const s = sockets[0]; s.open(); s.sent.length = 0;
    p.awareness.setLocalStateField("user", { name: "Alice", color: "#f00" });
    expect(s.sent).toHaveLength(1);
    expect(firstMsgType(s.sent[0])).toBe(MESSAGE_AWARENESS);
  });

  it("applies inbound AWARENESS to provider.awareness and does NOT echo", () => {
    const { p, sockets } = makeProvider(true);
    const s = sockets[0]; s.open(); s.sent.length = 0;
    s.deliver(awarenessFrameFor({ name: "Bob" }));
    const users = [...p.awareness.getStates().values()].map((st) => (st as any).user?.name);
    expect(users).toContain("Bob");
    expect(s.sent).toHaveLength(0);        // applied with origin `this` → not echoed
  });

  it("flushes local awareness on open (state set before the socket opened)", () => {
    const { p, sockets } = makeProvider(true);
    p.awareness.setLocalStateField("user", { name: "Early" });  // before open: send is a no-op
    const s = sockets[0];
    s.open();
    // open sends STEP1 + the awareness flush
    const types = s.sent.map(firstMsgType);
    expect(types).toContain(MESSAGE_AWARENESS);
  });

  it("read-only client still sends and receives awareness (ungated)", () => {
    const { p, sockets } = makeProvider(false);
    const s = sockets[0]; s.open(); s.sent.length = 0;
    p.awareness.setLocalStateField("user", { name: "Viewer" });
    expect(s.sent.some((b) => firstMsgType(b) === MESSAGE_AWARENESS)).toBe(true);  // sends
    s.deliver(awarenessFrameFor({ name: "Peer" }));
    const users = [...p.awareness.getStates().values()].map((st) => (st as any).user?.name);
    expect(users).toContain("Peer");                                              // receives
  });

  it("sends an awareness removal frame on destroy", () => {
    const { p, sockets } = makeProvider(true);
    const s = sockets[0]; s.open();
    p.awareness.setLocalStateField("user", { name: "Leaver" });
    s.sent.length = 0;
    p.destroy();
    expect(s.sent.some((b) => firstMsgType(b) === MESSAGE_AWARENESS)).toBe(true);
  });

  it("clears REMOTE awareness (keeps local) when the socket closes", () => {
    const { p, doc, sockets } = makeProvider(true);
    const s = sockets[0]; s.open();
    p.awareness.setLocalStateField("user", { name: "Me" });     // local state
    s.deliver(awarenessFrameFor({ name: "Peer" }));             // a remote peer
    expect([...p.awareness.getStates().keys()].length).toBeGreaterThanOrEqual(2);
    s.drop(1011);                                                // server error → reconnecting
    const ids = [...p.awareness.getStates().keys()];
    expect(ids).toEqual([doc.clientID]);                         // only local remains
  });

  function malformedAwarenessFrame(): Uint8Array {
    // A structurally-valid AWARENESS frame whose inner payload carries an
    // invalid-JSON state string → applyAwarenessUpdate's JSON.parse throws
    // (deterministic + version-independent), exercising the provider's drop guard.
    const inner = encoding.createEncoder();
    encoding.writeVarUint(inner, 1); // one updated client
    encoding.writeVarUint(inner, 999); // clientID
    encoding.writeVarUint(inner, 0); // clock
    encoding.writeVarString(inner, "{not valid json"); // state → JSON.parse throws
    const e = encoding.createEncoder();
    encoding.writeVarUint(e, MESSAGE_AWARENESS);
    encoding.writeVarUint8Array(e, encoding.toUint8Array(inner));
    return encoding.toUint8Array(e);
  }

  it("drops a malformed AWARENESS frame without crashing or corrupting awareness", () => {
    const { p, sockets } = makeProvider(true);
    const s = sockets[0];
    s.open();
    s.sent.length = 0;
    expect(() => s.deliver(malformedAwarenessFrame())).not.toThrow(); // handler survives
    expect(p.status).toBe("connected"); // no teardown
    expect([...p.awareness.getStates().values()].some((st) => (st as any).user)).toBe(false); // no bogus peer
    // provider still works afterwards: a subsequent VALID awareness frame applies
    s.deliver(awarenessFrameFor({ name: "Bob" }));
    const users = [...p.awareness.getStates().values()].map((st) => (st as any).user?.name);
    expect(users).toContain("Bob");
  });
});

describe("synced — 'I have the document' is not the same as 'I am connected'", () => {
  // The editor renders the Y.Doc. An unsynced doc is EMPTY, and an empty
  // document looks exactly like a document that has not arrived yet. Without a
  // synced flag the editor draws a blank page for a workspace whose text is
  // sitting on the server — which reads as data loss. Observed on a real
  // workspace whose room held 4,347 bytes while the editor showed nothing.
  function build() {
    const sockets: FakeSocket[] = [];
    const doc = new Y.Doc();
    const p = new CollabProvider({
      url: "ws://x/y",
      doc,
      canWrite: true,
      WebSocketImpl: class extends FakeSocket {
        constructor(u: string) { super(u); sockets.push(this); }
      } as unknown as typeof WebSocket,
    });
    return { p, doc, sockets };
  }

  // Was named step2From but wrote sync.writeUpdate — a SYNC_UPDATE, not a
  // STEP2. The wrong name hid the defect inside the tests meant to pin it: a
  // peer's update arriving before the server's STEP2 latched `synced` over an
  // empty doc. Now it writes what it says.
  function step2From(text: string): Uint8Array {
    const remote = new Y.Doc();
    remote.getText("t").insert(0, text);
    const e = encoding.createEncoder();
    encoding.writeVarUint(e, MESSAGE_SYNC);
    sync.writeSyncStep2(e, remote);
    return encoding.toUint8Array(e);
  }

  it("is false before the socket opens", () => {
    const { p } = build();
    expect(p.synced).toBe(false);
    p.destroy();
  });

  it("is STILL false once merely connected — open is not sync", () => {
    const { p, sockets } = build();
    sockets[0].open();
    expect(p.status).toBe("connected");
    expect(p.synced).toBe(false); // the whole point: connected != has the text
    p.destroy();
  });

  it("becomes true on the first sync frame, and notifies listeners once", () => {
    const { p, sockets } = build();
    const seen: boolean[] = [];
    p.on("sync", (v) => seen.push(v));
    sockets[0].open();
    sockets[0].deliver(step2From("hello"));
    expect(p.synced).toBe(true);
    expect(seen).toEqual([true]);
    // A second frame must not re-notify — listeners drive a render.
    sockets[0].deliver(step2From(" more"));
    expect(seen).toEqual([true]);
    p.destroy();
  });

  it("is true for an EMPTY room too — empty is a real answer, not a missing one", () => {
    // Otherwise a genuinely blank new draft would hang on the loading state.
    const { p, sockets } = build();
    sockets[0].open();
    sockets[0].deliver(step2From(""));
    expect(p.synced).toBe(true);
    p.destroy();
  });

  it("an awareness frame alone does NOT count as synced", () => {
    // Carets can arrive before the document does; treating any frame as sync
    // would show an empty editor as authoritative.
    const { p, sockets } = build();
    sockets[0].open();
    sockets[0].deliver(awarenessFrameFor({ name: "Someone", color: "#fff" }));
    expect(p.synced).toBe(false);
    p.destroy();
  });
});

describe("synced latches on CONTENT, not on any sync frame", () => {
  function build() {
    const sockets: FakeSocket[] = [];
    const doc = new Y.Doc();
    const p = new CollabProvider({
      url: "ws://x/y", doc, canWrite: true,
      WebSocketImpl: class extends FakeSocket {
        constructor(u: string) { super(u); sockets.push(this); }
      } as unknown as typeof WebSocket,
    });
    return { p, doc, sockets };
  }

  it("an inbound STEP1 does NOT latch synced", () => {
    // STEP1 is a state-vector REQUEST: readSyncMessage writes a STEP2 reply and
    // applies nothing to our doc. Measured before the fix: synced went true with
    // the doc still empty — which is the whole bug this flag exists to prevent.
    const { p, doc, sockets } = build();
    sockets[0].open();
    const e = encoding.createEncoder();
    encoding.writeVarUint(e, MESSAGE_SYNC);
    sync.writeSyncStep1(e, new Y.Doc());
    sockets[0].deliver(encoding.toUint8Array(e));
    expect(doc.getText("t").length).toBe(0);
    expect(p.synced).toBe(false);
    p.destroy();
  });

  it("the STEP2 that FOLLOWS a STEP1 does latch it", () => {
    const { p, sockets } = build();
    sockets[0].open();
    const s1 = encoding.createEncoder();
    encoding.writeVarUint(s1, MESSAGE_SYNC);
    sync.writeSyncStep1(s1, new Y.Doc());
    sockets[0].deliver(encoding.toUint8Array(s1));
    expect(p.synced).toBe(false);

    const remote = new Y.Doc();
    remote.getText("t").insert(0, "content");
    const s2 = encoding.createEncoder();
    encoding.writeVarUint(s2, MESSAGE_SYNC);
    sync.writeSyncStep2(s2, remote);
    sockets[0].deliver(encoding.toUint8Array(s2));
    expect(p.synced).toBe(true);
    p.destroy();
  });

  it("an empty STEP2 latches it too — empty is an answer", () => {
    const { p, sockets } = build();
    sockets[0].open();
    const e = encoding.createEncoder();
    encoding.writeVarUint(e, MESSAGE_SYNC);
    sync.writeSyncStep2(e, new Y.Doc());
    sockets[0].deliver(encoding.toUint8Array(e));
    expect(p.synced).toBe(true);
    p.destroy();
  });
});

describe("a peer's SYNC_UPDATE is not proof the document arrived", () => {
  function build() {
    const sockets: FakeSocket[] = [];
    const doc = new Y.Doc();
    const p = new CollabProvider({
      url: "ws://x/y", doc, canWrite: true,
      WebSocketImpl: class extends FakeSocket {
        constructor(u: string) { super(u); sockets.push(this); }
      } as unknown as typeof WebSocket,
    });
    return { p, doc, sockets };
  }

  it("a SYNC_UPDATE arriving BEFORE the server's STEP2 does not latch synced", () => {
    // The window is real: collab_ws.py registers a joining socket in
    // room.clients BEFORE it sends the initial STEP1, with two threadpool DB
    // round-trips in between. A peer editing in that gap has its update
    // broadcast to the new socket first. The update's dependencies are missing,
    // so Yjs BUFFERS it as pending and the doc stays empty — and synced latched
    // true anyway, mounting a blank editable draft over a document with content.
    // The file's own comment records this in production: "the room held 4,347
    // bytes and the Outline listed seven sections while the editor showed
    // nothing at all."
    const { p, sockets } = build();
    sockets[0].open();

    // An update whose dependencies this doc has never seen.
    const peer = new Y.Doc();
    peer.getText("t").insert(0, "seven sections of real content");
    const base = Y.encodeStateVector(new Y.Doc());
    const missingDeps = new Y.Doc();
    missingDeps.getText("t").insert(0, "earlier");
    Y.applyUpdate(peer, Y.encodeStateAsUpdate(missingDeps));
    peer.getText("t").insert(0, "later");
    const partial = Y.encodeStateAsUpdate(peer, Y.encodeStateAsUpdate(missingDeps, base));

    const e = encoding.createEncoder();
    encoding.writeVarUint(e, MESSAGE_SYNC);
    sync.writeUpdate(e, partial);
    sockets[0].deliver(encoding.toUint8Array(e));

    expect(p.synced).toBe(false);
    p.destroy();
  });

  it("the STEP2 that follows still latches it, and the buffered update lands", () => {
    // The other side of the gate: withholding synced must not strand the editor.
    const { p, doc, sockets } = build();
    sockets[0].open();

    const remote = new Y.Doc();
    remote.getText("t").insert(0, "content");
    const s2 = encoding.createEncoder();
    encoding.writeVarUint(s2, MESSAGE_SYNC);
    sync.writeSyncStep2(s2, remote);
    sockets[0].deliver(encoding.toUint8Array(s2));

    expect(p.synced).toBe(true);
    expect(doc.getText("t").toString()).toBe("content");
    p.destroy();
  });
});

describe("giving up on reconnection is told to the user", () => {
  function build() {
    const sockets: FakeSocket[] = [];
    const p = new CollabProvider({
      url: "ws://x/y", doc: new Y.Doc(), canWrite: true,
      WebSocketImpl: class extends FakeSocket {
        constructor(u: string) { super(u); sockets.push(this); }
      } as unknown as typeof WebSocket,
    });
    return { p, sockets };
  }

  it("the status becomes terminal once retries are exhausted", () => {
    // maxAttempts is 12 and the backoff sums to 241 seconds. After that
    // scheduleReconnect returned with NO setStatus, so the status stayed
    // "offline" and the StatusBar kept saying "Offline — reconnecting…" — false
    // from second 241 onward, for the life of the mount. `attempts` resets only
    // in ws.onopen, which by definition cannot fire again.
    //
    // Meanwhile synced is latched so the editor stays mounted and typeable,
    // send() silently no-ops because this.ws is null, and there is no
    // client-side persistence — every character is destroyed by the next reload.
    vi.useFakeTimers();
    const { p, sockets } = build();
    const seen: string[] = [];
    p.on("status", (s) => seen.push(s));
    for (let i = 0; i < 40 && p.status !== "offline-permanent"; i++) {
      sockets[sockets.length - 1].drop(1006);
      vi.advanceTimersByTime(60_000);
    }
    expect(p.status).toBe("offline-permanent");
    expect(seen).toContain("offline-permanent");
    p.destroy();
    vi.useRealTimers();
  });

  it("does not reach the terminal state while it is still retrying", () => {
    // Guards the other direction: a status that went terminal immediately would
    // pass the test above and be just as untrue.
    vi.useFakeTimers();
    const { p, sockets } = build();
    sockets[0].drop(1006);
    vi.advanceTimersByTime(60_000);
    expect(p.status).toBe("offline");
    p.destroy();
    vi.useRealTimers();
  });
});
