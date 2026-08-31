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
});