import { screen } from "@testing-library/react";
import { StrictMode } from "react";
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "../test-utils";

// Async factory + dynamic imports so the mock exposes a REAL y-protocols
// Awareness (CollaborationCaret reads provider.awareness.setLocalStateField on
// mount — a bare stub would crash). Dynamic import avoids the vi.mock hoisting
// init-order problem with top-level imports.
vi.mock("./collabProvider", async () => {
  const Y = await import("yjs");
  const awarenessProtocol = await import("y-protocols/awareness");
  return {
    CollabProvider: class {
      awareness = new awarenessProtocol.Awareness(new Y.Doc());
      on() {
        return () => {};
      }
      destroy() {}
      isSettled() {
        return false;
      }
      get status() {
        return "connected";
      }
    },
  };
});

vi.mock("./useSaveVersion", () => ({
  useSaveVersion: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

import { CollabEditor } from "./CollabEditor";

describe("CollabEditor", () => {
  it("renders read-only when canWrite is false", async () => {
    renderWithProviders(<CollabEditor workspaceId="w" artifact="draft" canWrite={false} />);
    // The doc/provider are built in an effect, so the editor appears after a
    // state update — find it async rather than assuming a synchronous mount.
    const container = await screen.findByTestId("collab-editor");
    const el = container.querySelector("[contenteditable]");
    expect(el?.getAttribute("contenteditable")).toBe("false");
  });

  it("survives React StrictMode's dev double-mount (regression: useCollabDoc null-ref)", async () => {
    // The real app wraps in <StrictMode> (main.tsx). StrictMode dev-double-mounts
    // effects (setup → cleanup → setup); the previous create-in-render +
    // null-the-ref-in-cleanup pattern threw
    //   "Cannot destructure property 'provider' of 'ref.current' as it is null"
    // on the second setup. This pins the state-driven lifecycle that fixed it.
    renderWithProviders(
      <StrictMode>
        <CollabEditor workspaceId="w" artifact="draft" canWrite />
      </StrictMode>,
    );
    expect(await screen.findByTestId("collab-editor")).toBeInTheDocument();
  });
});
