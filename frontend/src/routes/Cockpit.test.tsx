import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { Cockpit } from "./Cockpit";

afterEach(() => {
  vi.restoreAllMocks();
});

function draftPayload(canEdit: boolean) {
  return {
    data: {
      head: "a".repeat(40),
      can_edit: canEdit,
      sections: [
        { index: 0, heading: "", text: "preamble\n", sha: "s0" },
        { index: 1, heading: "Methods", text: "## Methods\nbody.\n", sha: "s1" },
      ],
      citations: {},
    },
    error: undefined,
  };
}

function renderCockpit() {
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/cockpit/ws-1"]}>
        <Routes>
          <Route path="/cockpit/:id" element={<Cockpit />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

it("renders the Outline from sections", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(draftPayload(true) as never);
  renderCockpit();
  // Outline lists section headings; the preamble shows a stable label.
  expect(await screen.findByRole("button", { name: /Methods/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /preamble/i })).toBeInTheDocument();
});

it("an owner gets an editable editor; the Full-draft tab is read-only", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(draftPayload(true) as never);
  renderCockpit();
  await screen.findByRole("button", { name: /Methods/ });
  // Default section editor is editable.
  const content = document.querySelector(".cm-content");
  expect(content?.getAttribute("contenteditable")).toBe("true");
  // Switch to the Full-draft tab -> read-only.
  fireEvent.click(screen.getByRole("tab", { name: /full draft/i }));
  await waitFor(() => {
    const full = document.querySelector(".cm-content");
    expect(full?.getAttribute("contenteditable")).toBe("false");
  });
  // Switch back via the Section tab -> editable again (lastSectionIndex restore,
  // not a reset to section 0).
  fireEvent.click(screen.getByRole("tab", { name: /^section$/i }));
  await waitFor(() => {
    const back = document.querySelector(".cm-content");
    expect(back?.getAttribute("contenteditable")).toBe("true");
  });
});

it("a non-owner (can_edit=false) gets a read-only section editor", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(draftPayload(false) as never);
  renderCockpit();
  await screen.findByRole("button", { name: /Methods/ });
  const content = document.querySelector(".cm-content");
  expect(content?.getAttribute("contenteditable")).toBe("false");
});

it("the Save button is hidden for a non-owner", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(draftPayload(false) as never);
  renderCockpit();
  await screen.findByRole("button", { name: /Methods/ });
  expect(screen.queryByRole("button", { name: /^save section$/i })).not.toBeInTheDocument();
});

it("a pill click invokes onPillClick with (citekey, evId) [SP2a-4 wiring]", async () => {
  // Cockpit's own onPillClick is an SP2a-4 no-op stub, so we assert the (citekey,
  // evId) contract directly against CodeMirrorEditor (the production pill path),
  // then confirm Cockpit actually renders a pill from the draft bytes below.
  const { CodeMirrorEditor } = await import("../editor/CodeMirrorEditor");
  const onPillClick = vi.fn();
  const doc = "## Methods\nSee [@smith2021]<!-- ev:ev-abc123def456 --> here.\n";
  const { container } = render(
    <CodeMirrorEditor value={doc} onChange={() => {}} requireHeading onPillClick={onPillClick} />,
  );
  const pill = container.querySelector(".cm-citation-pill") as HTMLElement;
  expect(pill).not.toBeNull();
  // The widget binds mousedown (not click) so CM6 doesn't move the caret first.
  fireEvent.mouseDown(pill);
  expect(onPillClick).toHaveBeenCalledWith("smith2021", "ev-abc123def456");
});

it("Cockpit renders a citation pill from the draft bytes", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: {
      head: "a".repeat(40),
      can_edit: true,
      sections: [
        { index: 0, heading: "", text: "preamble\n", sha: "s0" },
        {
          index: 1,
          heading: "Methods",
          text: "## Methods\nSee [@smith2021]<!-- ev:ev-abc123def456 --> here.\n",
          sha: "s1",
        },
      ],
      citations: {},
    },
    error: undefined,
  } as never);
  renderCockpit();
  fireEvent.click(await screen.findByRole("button", { name: /Methods/ }));
  await waitFor(() =>
    expect(document.querySelector(".cm-citation-pill")).not.toBeNull(),
  );
});

it("edits to one section survive switching to another section and back", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(draftPayload(true) as never);
  renderCockpit();
  // Start on the Methods section, type into the editor.
  fireEvent.click(await screen.findByRole("button", { name: /Methods/ }));
  const view = (document.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: import("@codemirror/view").EditorView;
  }).__cmView!;
  view.dispatch({ changes: { from: view.state.doc.length, insert: "EDIT" } });
  expect(document.querySelector(".cm-content")?.textContent).toContain("EDIT");
  // Switch to the preamble, then back to Methods — the edit must still be there.
  fireEvent.click(screen.getByRole("button", { name: /preamble/i }));
  await waitFor(() =>
    expect(document.querySelector(".cm-content")?.textContent).toContain("preamble"),
  );
  fireEvent.click(screen.getByRole("button", { name: /Methods/ }));
  await waitFor(() =>
    expect(document.querySelector(".cm-content")?.textContent).toContain("EDIT"),
  );
});

it("a 409 keeps the buffer and shows a conflict prompt; Reload discards and reloads", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(draftPayload(true) as never);
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: undefined,
    error: { error: { code: "conflict", message: "stale" } },
  } as never);
  renderCockpit();
  fireEvent.click(await screen.findByRole("button", { name: /Methods/ }));
  const view = (document.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: import("@codemirror/view").EditorView;
  }).__cmView!;
  // The dispatch fires onChange -> setBuffers (a React state update outside React's
  // event system); wrap it in act() so the dirty buffer flushes and the Save button
  // is enabled before the synchronous click below (otherwise the click no-ops on a
  // still-disabled button). Mechanics only — assertions/intent unchanged.
  act(() => {
    view.dispatch({ changes: { from: view.state.doc.length, insert: "MINE" } });
  });
  fireEvent.click(screen.getByRole("button", { name: /^save section$/i }));
  // Conflict prompt appears; the buffer (MINE) is PRESERVED (spec 194).
  expect(await screen.findByRole("alert")).toBeInTheDocument();
  expect(document.querySelector(".cm-content")?.textContent).toContain("MINE");
  // Reload discards my edits -> editor falls back to the fetched section text.
  fireEvent.click(screen.getByRole("button", { name: /reload \(discard my edits\)/i }));
  await waitFor(() =>
    expect(document.querySelector(".cm-content")?.textContent).not.toContain("MINE"),
  );
});
