import * as decoding from "lib0/decoding";
import * as encoding from "lib0/encoding";
import * as sync from "y-protocols/sync";
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
});
