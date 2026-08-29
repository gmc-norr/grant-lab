import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import * as awarenessProtocol from "y-protocols/awareness";
import * as Y from "yjs";

import { renderWithProviders } from "../test-utils";

// A connected socket that has not yet delivered the document leaves the Y.Doc
// EMPTY, and an empty doc renders as a blank page — indistinguishable from a
// draft whose text was lost. This happened on a real workspace: the room held
// 4,347 bytes, the outline listed seven sections, and the editor showed nothing.
//
// The editor must therefore gate its CONTENT on `synced`, never on `status`.
const state = vi.hoisted(() => ({
  status: "connected" as string,
  synced: false,
}));

// A REAL Y.Doc and awareness: the synced cases mount the actual Tiptap editor,
// and a stub object cannot back Collaboration/CollaborationCaret. A mock that
// cannot reach the mounted state cannot fail the "it mounts" assertions.
const live = vi.hoisted(() => {
  return { doc: null as unknown, provider: null as unknown };
});

vi.mock("./useCollabDoc", () => ({
  useCollabDoc: () => ({
    doc: live.doc,
    provider: live.provider,
    status: state.status,
    synced: state.synced,
  }),
}));
vi.mock("../api/session", () => ({
  useSession: () => ({ data: { user: { id: 1, name: "T", email: "t@ki.se" } } }),
}));

import { CollabEditor } from "./CollabEditor";

const ACTIONS = <button type="button">Export ▾</button>;

function render() {
  const doc = new Y.Doc();
  live.doc = doc;
  live.provider = {
    awareness: new awarenessProtocol.Awareness(doc),
    isSettled: () => true,
    on: () => () => {},
    status: state.status,
    synced: state.synced,
  };
  renderWithProviders(
    <CollabEditor workspaceId="w" artifact="draft" canWrite actions={ACTIONS} />,
  );
}

describe("CollabEditor does not render an unsynced document as empty", () => {
  it("holds the placeholder while CONNECTED but not yet synced", () => {
    state.status = "connected";
    state.synced = false;
    render();
    // The bug in one assertion: "connected" used to be enough to mount the
    // editor and draw an empty document.
    expect(screen.getByTestId("collab-connecting")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /export/i })).toBeInTheDocument();
  });

  it("says the document could not be reached when the socket is offline", () => {
    // Silence is the worst outcome: the user cannot tell a broken connection
    // from an empty draft.
    state.status = "offline";
    state.synced = false;
    render();
    const box = screen.getByTestId("collab-connecting");
    expect(box.textContent ?? "").toMatch(/could not|can't|cannot|reach/i);
    expect(box.textContent ?? "").not.toMatch(/^Connecting…$/);
  });

  it("says so for a policy close too", () => {
    state.status = "closed-policy";
    state.synced = false;
    render();
    expect(screen.getByTestId("collab-connecting").textContent ?? "").toMatch(
      /could not|can't|cannot|reach/i,
    );
  });

  it("mounts the editor once synced — even while offline afterwards", () => {
    // synced latches: a doc we already hold is stale, not absent. Dropping back
    // to the placeholder on a blip would throw the user out of their editor.
    state.status = "offline";
    state.synced = true;
    render();
    expect(screen.queryByTestId("collab-connecting")).toBeNull();
  });

  it("mounts the editor for a genuinely EMPTY synced room", () => {
    // Otherwise a new draft hangs on a loading state forever.
    state.status = "connected";
    state.synced = true;
    render();
    expect(screen.queryByTestId("collab-connecting")).toBeNull();
  });
});
