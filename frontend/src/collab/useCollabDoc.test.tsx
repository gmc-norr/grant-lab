import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
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

/** The hook invalidates the branch-scoped query keys on a 4409, so every render
 *  needs a client. `retry: false` keeps a refetch of an unmocked key from
 *  looping in the background. */
function harness() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  return { qc, wrapper };
}

describe("useCollabDoc document identity", () => {
  it("keeps the doc across ordinary rerenders but replaces it after a branch switch", async () => {
    vi.stubGlobal("WebSocket", FakeSocket);
    const { result, rerender } = renderHook(
      ({ canWrite }) => useCollabDoc({ workspaceId: "w", artifact: "draft", canWrite }),
      { initialProps: { canWrite: true }, wrapper: harness().wrapper },
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
    const { result } = renderHook(
      () => useCollabDoc({ workspaceId: "w", artifact: "draft", canWrite: true }),
      { wrapper: harness().wrapper },
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
    const { result } = renderHook(
      () => useCollabDoc({ workspaceId: "w", artifact: "draft", canWrite: true }),
      { wrapper: harness().wrapper },
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

  it("a remote branch switch invalidates the branch-scoped read-models", async () => {
    // Review 2026-09-02 A7/F1. The 4409 handler rebuilt the Y.Doc and nothing
    // else, so when ANOTHER member switched the branch the editor body followed
    // but every cached read-model stayed on the old branch — and none of these
    // keys can recover on their own (no interval, refetchOnWindowFocus off,
    // 20 s staleTime, observers never unmount). The status bar named the old
    // branch, BranchBar offered to switch to the branch you were already on, and
    // the Versions pane listed the old history.
    vi.stubGlobal("WebSocket", FakeSocket);
    const { qc, wrapper } = harness();
    for (const key of [["branches", "w"], ["draft", "w"], ["spec", "w"], ["versions", "w"]]) {
      qc.setQueryData(key, { seeded: true });
      expect(qc.getQueryState(key)!.isInvalidated).toBe(false);
    }

    const { result } = renderHook(
      () => useCollabDoc({ workspaceId: "w", artifact: "draft", canWrite: true }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.provider).not.toBeNull());
    act(() => FakeSocket.instances[0].drop(WS_DOCUMENT_REPLACED));

    for (const key of [["branches", "w"], ["draft", "w"], ["spec", "w"], ["versions", "w"]]) {
      await waitFor(() =>
        expect(qc.getQueryState(key)!.isInvalidated, `${key[0]} was left stale`).toBe(true),
      );
    }
  });

  it("does not invalidate cockpit-meta, which carries nothing branch-scoped", async () => {
    // CockpitMetaOut is title/deadline/phase/role/spec_locked/member_count/setup
    // status (web/api/workspaces.py). Invalidating it on every 4409 would be a
    // request per replacement that can never return anything different.
    vi.stubGlobal("WebSocket", FakeSocket);
    const { qc, wrapper } = harness();
    qc.setQueryData(["cockpit-meta", "w"], { workspace_title: "T" });
    // Seeded so the branches key is a real POSITIVE signal that the handler ran.
    // Waiting on an absent key would pass whether or not it ever fired.
    qc.setQueryData(["branches", "w"], { current: "main" });

    const { result } = renderHook(
      () => useCollabDoc({ workspaceId: "w", artifact: "draft", canWrite: true }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.provider).not.toBeNull());
    act(() => FakeSocket.instances[0].drop(WS_DOCUMENT_REPLACED));
    await waitFor(() => expect(qc.getQueryState(["branches", "w"])!.isInvalidated).toBe(true));

    expect(qc.getQueryState(["cockpit-meta", "w"])!.isInvalidated).toBe(false);
  });
});