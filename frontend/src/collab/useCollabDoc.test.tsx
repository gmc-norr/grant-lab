import { act, renderHook, waitFor } from "@testing-library/react";
import * as encoding from "lib0/encoding";
import * as syncProtocol from "y-protocols/sync";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as Y from "yjs";
import { WS_DOCUMENT_REPLACED } from "./collabProvider";
import { useCollabDoc } from "./useCollabDoc";

class FakeSocket {
  static OPEN = 1;
  static instances: FakeSocket[] = [];
  readyState = 0;
  bufferedAmount = 0;
  binaryType = "arraybuffer";
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: ArrayBuffer }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }

  send() {}
  close() {
    this.readyState = 3;
    this.onclose?.({ code: 1000 });
  }
  drop(code: number) {
    this.readyState = 3;
    this.onclose?.({ code });
  }
  deliver(bytes: Uint8Array) {
    this.onmessage?.({
      data: bytes.buffer.slice(
        bytes.byteOffset,
        bytes.byteOffset + bytes.byteLength,
      ) as ArrayBuffer,
    });
  }
}

function syncStep2(): Uint8Array {
  const encoder = encoding.createEncoder();
  encoding.writeVarUint(encoder, 0);
  syncProtocol.writeSyncStep2(encoder, new Y.Doc());
  return encoding.toUint8Array(encoder);
}

afterEach(() => {
  FakeSocket.instances = [];
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("useCollabDoc document identity", () => {
  it("keeps the doc across ordinary rerenders but replaces it after a branch switch", async () => {
    vi.stubGlobal("WebSocket", FakeSocket);
    const { result, rerender } = renderHook(
      ({ canWrite }) => useCollabDoc({ workspaceId: "w", artifact: "draft", canWrite }),
      { initialProps: { canWrite: true } },
    );
    await waitFor(() => expect(result.current.provider).not.toBeNull());
    const firstProvider = result.current.provider;
    const firstDoc = result.current.doc;
    const onDestroy = vi.fn();
    firstDoc!.on("destroy", onDestroy);

    // Query invalidation after a save can rerender the panel, but the same
    // branch must retain its live document and editor state.
    rerender({ canWrite: false });
    expect(result.current.provider).toBe(firstProvider);
    expect(result.current.doc).toBe(firstDoc);

    act(() => FakeSocket.instances[0].drop(WS_DOCUMENT_REPLACED));
    await waitFor(() => {
      expect(result.current.provider).not.toBeNull();
      expect(result.current.provider).not.toBe(firstProvider);
    });

    expect(result.current.doc).not.toBe(firstDoc);
    expect(onDestroy).toHaveBeenCalledOnce();
    expect(FakeSocket.instances).toHaveLength(2);
  });

  it("bounds repeated unsynced document replacements", () => {
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", FakeSocket);
    const { result } = renderHook(() =>
      useCollabDoc({ workspaceId: "w", artifact: "draft", canWrite: true }),
    );
    expect(result.current.provider).not.toBeNull();

    // The hook permits eight fresh Y.Docs, each after a non-zero delay.
    for (let attempt = 0; attempt < 8; attempt++) {
      const socket = FakeSocket.instances[FakeSocket.instances.length - 1];
      act(() => socket.drop(WS_DOCUMENT_REPLACED));
      expect(FakeSocket.instances).toHaveLength(attempt + 1);
      act(() => vi.runOnlyPendingTimers());
      expect(FakeSocket.instances).toHaveLength(attempt + 2);
    }

    const finalSocket = FakeSocket.instances[8];
    act(() => finalSocket.drop(WS_DOCUMENT_REPLACED));

    expect(FakeSocket.instances).toHaveLength(9);
    expect(result.current.provider).toBeNull();
    expect(result.current.status).toBe("offline-permanent");
  });

  it("resets the replacement budget after a document sync", () => {
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", FakeSocket);
    const { result } = renderHook(() =>
      useCollabDoc({ workspaceId: "w", artifact: "draft", canWrite: true }),
    );

    for (let attempt = 0; attempt < 7; attempt++) {
      act(() => FakeSocket.instances.at(-1)!.drop(WS_DOCUMENT_REPLACED));
      act(() => vi.runOnlyPendingTimers());
    }
    act(() => FakeSocket.instances.at(-1)!.deliver(syncStep2()));
    expect(result.current.synced).toBe(true);

    for (let attempt = 0; attempt < 8; attempt++) {
      act(() => FakeSocket.instances.at(-1)!.drop(WS_DOCUMENT_REPLACED));
      act(() => vi.runOnlyPendingTimers());
    }
    expect(FakeSocket.instances).toHaveLength(16);

    act(() => FakeSocket.instances.at(-1)!.drop(WS_DOCUMENT_REPLACED));
    expect(FakeSocket.instances).toHaveLength(16);
    expect(result.current.status).toBe("offline-permanent");
  });
});