import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
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
}

afterEach(() => {
  FakeSocket.instances = [];
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
    await waitFor(() => expect(result.current.provider).not.toBe(firstProvider));

    expect(result.current.doc).not.toBe(firstDoc);
    expect(onDestroy).toHaveBeenCalledOnce();
    expect(FakeSocket.instances).toHaveLength(2);
  });

  it("KNOWN GAP: every replacement builds a new socket, with no cap and no backoff", async () => {
    // PINS A GAP. This test passing is the problem; invert it when bounded.
    //
    // collabProvider.ts:267-274 returns BEFORE scheduleReconnect(), so the 4409
    // path skips both the exponential backoff and the maxReconnectAttempts
    // ceiling (12) that bound every other close code. useCollabDoc then bumps
    // documentGeneration, and the replacement provider connects inside its own
    // constructor. A server that keeps answering 4409 -- repeated branch
    // switches, or a generation that stays stale -- therefore gets one fresh
    // Y.Doc and one fresh socket per round trip, with nothing counting or
    // delaying them.
    vi.stubGlobal("WebSocket", FakeSocket);
    const { result } = renderHook(() =>
      useCollabDoc({ workspaceId: "w", artifact: "draft", canWrite: true }),
    );
    await waitFor(() => expect(result.current.provider).not.toBeNull());

    const ROUNDS = 20;
    for (let i = 0; i < ROUNDS; i++) {
      const socket = FakeSocket.instances[FakeSocket.instances.length - 1];
      act(() => socket.drop(WS_DOCUMENT_REPLACED));
      // Asserted every round, so a loop that silently stopped advancing would
      // fail HERE rather than quietly satisfying the count below.
      await waitFor(() =>
        expect(FakeSocket.instances[FakeSocket.instances.length - 1]).not.toBe(socket),
      );
    }

    // One socket per round plus the original: nothing ever stopped it.
    expect(FakeSocket.instances).toHaveLength(ROUNDS + 1);
    // And the status never reaches the terminal state maxReconnectAttempts
    // produces on the 1013/1006 path, so the UI keeps promising a reconnect.
    expect(result.current.status).not.toBe("offline-permanent");
  });
});