import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "../test-utils";

// `CollabEditor` early-returns a "Connecting…" placeholder until doc, provider
// AND identity all resolve — it never reaches CollabEditorInner, which is what
// renders the toolbar. Both panels pass their panel-level button ONLY as
// `actions` when collab is on (DraftPanel.tsx, SpecPanel.tsx), so if that slot
// is dropped here the button blinks out of existence on every mount.
//
// These mocks drive the two independent reasons for that branch, so a failure
// names its own cause. Deliberately NOT mocking CollabEditor itself: a mock
// that cannot enter the connecting state cannot fail this test.
const doc = vi.hoisted(() => ({ ready: false }));
const session = vi.hoisted(() => ({ user: { id: 1, name: "T", email: "t@ki.se" } as unknown }));

vi.mock("./useCollabDoc", () => ({
  useCollabDoc: () =>
    doc.ready
      ? { doc: {}, provider: {}, status: "connected" }
      : { doc: null, provider: null, status: "connecting" },
}));
vi.mock("../api/session", () => ({ useSession: () => ({ data: { user: session.user } }) }));

import { CollabEditor } from "./CollabEditor";

const ACTIONS = <button type="button">Export ▾</button>;

describe("CollabEditor — the actions slot survives the connecting state", () => {
  it("still renders actions while the doc and provider are unresolved", () => {
    doc.ready = false;
    session.user = { id: 1, name: "T", email: "t@ki.se" };
    renderWithProviders(
      <CollabEditor workspaceId="w" artifact="draft" canWrite actions={ACTIONS} />,
    );
    expect(screen.getByTestId("collab-connecting")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /export/i })).toBeInTheDocument();
  });

  it("still renders actions while identity is unresolved", () => {
    doc.ready = true; // doc/provider are fine; only `me.user` is missing
    session.user = undefined;
    renderWithProviders(
      <CollabEditor workspaceId="w" artifact="draft" canWrite actions={ACTIONS} />,
    );
    expect(screen.getByTestId("collab-connecting")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /export/i })).toBeInTheDocument();
  });

  it("renders no stray action when the panel passes none", () => {
    doc.ready = false;
    session.user = { id: 1, name: "T", email: "t@ki.se" };
    renderWithProviders(<CollabEditor workspaceId="w" artifact="draft" canWrite />);
    expect(screen.getByTestId("collab-connecting")).toHaveTextContent("Connecting…");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
