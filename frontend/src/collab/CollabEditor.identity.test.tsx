import { waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import * as Y from "yjs";
import { CollabProvider } from "./collabProvider";
import { renderWithProviders } from "../test-utils"; // the repo's real helper (src/test-utils.tsx)

// Control when identity resolves.
const sessionData: { value: any } = { value: undefined };
vi.mock("../api/session", () => ({ useSession: () => ({ data: sessionData.value }) }));

// A stable fake-socket provider, stashed for assertions. useCollabDoc builds the
// provider once in an effect (stable across rerenders); the mock mirrors that by
// creating exactly one provider and reusing it.
let stashed: CollabProvider | null = null;
class FakeSocket {
  static OPEN = 1; readyState = 1; bufferedAmount = 0; binaryType = "arraybuffer";
  onopen: any; onmessage: any; onclose: any; onerror: any;
  constructor(public url: string) { queueMicrotask(() => this.onopen?.()); }
  send() {} close() { this.onclose?.({ code: 1000 }); }
}
vi.mock("./useCollabDoc", () => ({
  useCollabDoc: (opts: any) => {
    if (!stashed) {
      const doc = new Y.Doc();
      stashed = new CollabProvider({
        url: "ws://x/api/ws/workspaces/w/draft", doc, canWrite: opts.canWrite,
        WebSocketImpl: FakeSocket as unknown as typeof WebSocket,
      });
    }
    return { doc: stashed.doc, provider: stashed, status: "connected" };
  },
}));

import { CollabEditor } from "./CollabEditor";

describe("CollabEditor identity (Slice 3b-5b #1: no Anonymous flash)", () => {
  it("does not broadcast any identity until the session resolves, then mounts with the real name", async () => {
    stashed = null;
    sessionData.value = undefined; // unresolved at mount
    const { rerender } = renderWithProviders(
      <CollabEditor workspaceId="w" artifact="draft" canWrite />,
    );
    await waitFor(() => expect(stashed).not.toBeNull());
    // pre-resolve: the editor/caret is NOT mounted, so no local awareness user is
    // ever set — nothing "Anonymous" is broadcast.
    expect((stashed!.awareness.getLocalState() as any)?.user).toBeUndefined();

    sessionData.value = { user: { id: 7, name: "Late Name", email: "late@ki.se" } };
    rerender(<CollabEditor workspaceId="w" artifact="draft" canWrite />);
    await waitFor(() => {
      expect((stashed!.awareness.getLocalState() as any)?.user?.name).toBe("Late Name");
    });
    // and it was NEVER the "Anonymous" fallback
    expect((stashed!.awareness.getLocalState() as any)?.user?.name).not.toBe("Anonymous");
  });
});
