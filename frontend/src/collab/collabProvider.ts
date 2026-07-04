import * as decoding from "lib0/decoding";
import * as encoding from "lib0/encoding";
import * as syncProtocol from "y-protocols/sync";
import * as awarenessProtocol from "y-protocols/awareness";
import type * as Y from "yjs";

const MESSAGE_SYNC = 0;
const MESSAGE_AWARENESS = 1;
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
  /** Give up after this many CONSECUTIVE failed connects (reset on open). */
  maxReconnectAttempts?: number;
}

export class CollabProvider {
  readonly doc: Y.Doc;
  readonly awareness: awarenessProtocol.Awareness;
  private readonly url: string;
  private readonly canWrite: boolean;
  private readonly WS: typeof WebSocket;
  private readonly baseMs: number;
  private readonly maxMs: number;
  private readonly maxAttempts: number;
  private ws: WebSocket | null = null;
  private _status: ProviderStatus = "connecting";
  private readonly listeners = new Set<StatusListener>();
  private attempts = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private destroyed = false;
  private lastLocalEditMs = 0;
  private readonly onUpdate: (update: Uint8Array, origin: unknown) => void;
  private readonly onAwarenessUpdate: (
    changes: { added: number[]; updated: number[]; removed: number[] },
    origin: unknown,
  ) => void;

  constructor(opts: CollabProviderOptions) {
    this.doc = opts.doc;
    this.awareness = new awarenessProtocol.Awareness(this.doc);
    this.url = opts.url;
    this.canWrite = opts.canWrite;
    this.WS = opts.WebSocketImpl ?? (globalThis.WebSocket as typeof WebSocket);
    this.baseMs = opts.reconnectBaseMs ?? 1000;
    this.maxMs = opts.reconnectMaxMs ?? 30000;
    this.maxAttempts = opts.maxReconnectAttempts ?? 12;
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
    this.onAwarenessUpdate = (changes, origin) => {
      // Origin-tag guard: never echo an update we applied FROM the socket.
      // Awareness send is UNGATED by canWrite — viewers also broadcast their caret.
      if (origin === this) return;
      this.sendAwareness([...changes.added, ...changes.updated, ...changes.removed]);
    };
    this.awareness.on("update", this.onAwarenessUpdate);
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
    // Remove the listener FIRST so the manual removal isn't double-sent by the handler.
    this.awareness.off("update", this.onAwarenessUpdate);
    // Announce our departure so peers drop our caret immediately (before close).
    awarenessProtocol.removeAwarenessStates(this.awareness, [this.doc.clientID], "destroy");
    this.sendAwareness([this.doc.clientID]);
    this.awareness.destroy();
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

  private sendAwareness(ids: number[]): void {
    if (!ids.length) return;
    const enc = encoding.createEncoder();
    encoding.writeVarUint(enc, MESSAGE_AWARENESS);
    encoding.writeVarUint8Array(enc, awarenessProtocol.encodeAwarenessUpdate(this.awareness, ids));
    this.send(encoding.toUint8Array(enc));
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
      // Flush our local awareness so peers/server learn it even if the caret set
      // the local user state before the socket opened (Amendment 5).
      // Only flush if the local state is non-empty (Awareness initialises to {}).
      const localState = this.awareness.getLocalState();
      if (localState !== null && Object.keys(localState).length > 0) {
        this.sendAwareness([this.doc.clientID]);
      }
    };

    ws.onmessage = (ev: MessageEvent) => {
      // Post-destroy guard: a message task queued before unmount can still fire
      // AFTER destroy()/doc.destroy() — never apply an update to a torn-down doc.
      if (this.destroyed) return;
      // Malformed/unexpected frames are DROPPED, not thrown (mirrors the server's
      // decode guard in room.py): a stray text/truncated frame must not crash the
      // handler. binaryType is "arraybuffer", so a non-ArrayBuffer is unexpected.
      try {
        if (!(ev.data instanceof ArrayBuffer)) return;
        const dec = decoding.createDecoder(new Uint8Array(ev.data));
        const messageType = decoding.readVarUint(dec);
        if (messageType === MESSAGE_AWARENESS) {
          // Ungated read: viewers see peers' carets. origin `this` → no echo.
          awarenessProtocol.applyAwarenessUpdate(this.awareness, decoding.readVarUint8Array(dec), this);
          return;
        }
        if (messageType !== MESSAGE_SYNC) return;
        const enc = encoding.createEncoder();
        encoding.writeVarUint(enc, MESSAGE_SYNC);
        // readSyncMessage applies inbound STEP2/UPDATE to the doc with `this` as the
        // txn origin (origin-tag), and writes a STEP2 reply into `enc` iff it read a STEP1.
        syncProtocol.readSyncMessage(dec, enc, this.doc, this);
        if (encoding.length(enc) > 1 && this.canWrite) {
          // A STEP2 reply is a WRITE frame — read-only clients suppress it.
          this.send(encoding.toUint8Array(enc));
        }
      } catch {
        // drop malformed frame
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
    if (this.attempts >= this.maxAttempts) {
      // Give up after this many CONSECUTIVE failures with no successful open
      // (attempts resets to 0 on open). Bounds the retry loop for a PERMANENT
      // rejection: a pre-accept server close(1008) (expired cookie / non-member /
      // collab disabled) reaches the browser as a 1006 handshake failure — NOT
      // catchable by the 1008 guard — so without this cap it would retry forever.
      // Stays "offline"; a remount (page reload / re-navigation) retries.
      return;
    }
    const delay = Math.min(this.baseMs * 2 ** this.attempts, this.maxMs);
    this.attempts += 1;
    this.timer = setTimeout(() => { this.timer = null; this.connect(); }, delay);
  }
}
