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

const PEEK_EV = "ev-abc123def456";

function draftWithPill() {
  return {
    data: {
      head: "a".repeat(40),
      can_edit: true,
      sections: [
        { index: 0, heading: "", text: "preamble\n", sha: "s0" },
        {
          index: 1,
          heading: "Methods",
          text: `## Methods\nSee [@smith2021]<!-- ev:${PEEK_EV} --> here.\n`,
          sha: "s1",
        },
      ],
      citations: {
        [PEEK_EV]: { citekey: "smith2021", number: 1, stance: "supports", title: "Smith 2021" },
      },
    },
    error: undefined,
  };
}

function sourcePayload(over: Record<string, unknown> = {}) {
  return {
    data: {
      evidence_id: PEEK_EV,
      reference_id: "r1",
      source_line: "smith2021 · The Smith 2021 Study",
      claim: null,
      stance: "supports",
      confidence: null,
      index: 1,
      total: 1,
      prev_ev: null,
      next_ev: null,
      tombstoned: false,
      no_text: false,
      note: null,
      text: "The cat sat on the mat.",
      highlight: [0, 23],
      ...over,
    },
    error: undefined,
  };
}

// Route GET by path: /draft -> draft, /source -> source. The whole impl is cast
// `as never` because openapi-fetch's GET has a complex generic signature that a
// plain (path: string) lambda doesn't structurally satisfy under tsc.
function mockGetByPath(draft: unknown, source: unknown) {
  return vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) =>
    path.endsWith("/source")
      ? Promise.resolve(source)
      : Promise.resolve(draft)) as never);
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

it("a successful save clears the dirty buffer (Save disables) and keeps the saved content", async () => {
  // Model a real save round-trip with a server that canonicalises: the user's edit
  // inserts "MINE_EDIT"; the committed/served bytes the refetch returns carry a
  // DIFFERENT token, "SERVER_NORM" (e.g. trailing-newline / whitespace normalization
  // — the kind of divergence that makes onSave's optimistic reconcile load-bearing).
  //
  // Why divergent tokens (and not just echoing the saved bytes): with committed ==
  // buffer, the refetch ALONE would make served-text == buffer, so the section would
  // read "not dirty" even if the local save path did nothing — i.e. the test would be
  // tautological. Diverging the committed text forces the assertions to depend on the
  // success-path reconcile (optimistic setQueryData of head+sha+content, BEFORE the
  // buffer-clear): only that keeps the user's "MINE_EDIT" on screen and the section
  // not-dirty; a neutered success path would let the divergent "SERVER_NORM" land and
  // leave the section dirty (Save enabled) — verified by deliberately neutering the
  // success path and watching this test fail (non-tautology check).
  const original = draftPayload(true);
  const serverCanonicalText = "## Methods\nbody.\nSERVER_NORM";
  const committed = {
    data: {
      head: "b".repeat(40),
      can_edit: true,
      sections: [
        { index: 0, heading: "", text: "preamble\n", sha: "s0" },
        { index: 1, heading: "Methods", text: serverCanonicalText, sha: "s1-new" },
      ],
      citations: {},
    },
    error: undefined,
  };
  // ORIGINAL for the initial load; COMMITTED for the post-save invalidate-refetch
  // (mockResolvedValueOnce -> mockResolvedValue, so the refetch returns committed
  // bytes — realistic prod behavior).
  vi.spyOn(clientMod.api, "GET")
    .mockResolvedValueOnce(original as never)
    .mockResolvedValue(committed as never);
  // POST resolves a success SaveResultOut (head/section_sha advance to the commit).
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: {
      commit_sha: "b".repeat(40),
      head: "b".repeat(40),
      section_sha: "s1-new",
      issues: [],
      job_enqueued: false,
      changed: true,
    },
    error: undefined,
  } as never);

  renderCockpit();
  fireEvent.click(await screen.findByRole("button", { name: /Methods/ }));
  const view = (document.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: import("@codemirror/view").EditorView;
  }).__cmView!;
  // Same act() wrap the 409 test uses: the dispatch fires onChange -> setBuffers (a
  // React state update outside React's event system); wrap it so the dirty buffer
  // flushes and Save is enabled before the synchronous click below (otherwise the
  // click no-ops on a still-disabled button).
  act(() => {
    view.dispatch({ changes: { from: view.state.doc.length, insert: "MINE_EDIT" } });
  });
  // Dirty -> Save is enabled before we save.
  const saveBtn = screen.getByRole("button", { name: /^save section$/i });
  expect(saveBtn).toBeEnabled();
  expect(document.querySelector(".cm-content")?.textContent).toContain("MINE_EDIT");

  fireEvent.click(saveBtn);

  // (a) After a successful save the section is no longer dirty, so "Save section"
  // disables again. The success path optimistically reconciles the cache to the saved
  // content (head + section_sha + text) and clears this section's buffer; the divergent
  // "SERVER_NORM" refetch does NOT re-dirty the section.
  await waitFor(() =>
    expect(screen.getByRole("button", { name: /^save section$/i })).toBeDisabled(),
  );
  // (b) No data loss / no revert: the editor still shows the user's saved content
  // ("MINE_EDIT"), NOT a stale/empty value and NOT the divergent server-canonical
  // bytes. (We do NOT assert the pure pre-refetch optimistic "no flash" timing —
  // untestable in jsdom — that property stays inspection-verified in Cockpit.onSave,
  // which reconciles the cache BEFORE clearing the buffer.)
  expect(document.querySelector(".cm-content")?.textContent).toContain("MINE_EDIT");
});

it("a non-conflict save error surfaces a dismissible banner and keeps the buffer", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(draftPayload(true) as never);
  // A non-conflict failure — here a 400 structure error, but the SAME onSave path
  // handles 429 busy / 500 / network. Before this hardening onSave re-threw these as
  // an unhandled async rejection: no UI feedback, the user had no idea the save failed
  // (though the buffer was kept). They must now SURFACE without dropping the buffer.
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: undefined,
    error: { error: { code: "bad_request", message: "keep the section's ## heading line" } },
  } as never);
  renderCockpit();
  fireEvent.click(await screen.findByRole("button", { name: /Methods/ }));
  const view = (document.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: import("@codemirror/view").EditorView;
  }).__cmView!;
  // act() wrap so the dirty buffer flushes and Save is enabled before the click.
  act(() => {
    view.dispatch({ changes: { from: view.state.doc.length, insert: "MYWORK" } });
  });
  fireEvent.click(screen.getByRole("button", { name: /^save section$/i }));
  // The error surfaces (carrying the backend message) and the buffer is PRESERVED.
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(/keep the section's ## heading/i);
  // The banner shows the human detail, NOT the raw "bad_request: " machine-code prefix.
  expect(alert).not.toHaveTextContent(/bad_request/i);
  expect(document.querySelector(".cm-content")?.textContent).toContain("MYWORK");
  // Dismiss clears the banner; the edit stays so the user can fix and re-Save.
  fireEvent.click(screen.getByRole("button", { name: /^dismiss$/i }));
  await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  expect(document.querySelector(".cm-content")?.textContent).toContain("MYWORK");
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

it("clicking a citation pill opens the source peek", async () => {
  mockGetByPath(draftWithPill(), sourcePayload());
  const { container } = renderCockpit();
  // Open the Methods section so its pill renders.
  fireEvent.click(await screen.findByRole("button", { name: /Methods/ }));
  const pill = container.querySelector(".cm-citation-pill");
  expect(pill).not.toBeNull();
  fireEvent.mouseDown(pill!);
  expect(await screen.findByText(/The Smith 2021 Study/)).toBeInTheDocument();
  expect(container.querySelector(".source-viewer mark")?.textContent).toBe(
    "The cat sat on the mat.",
  );
});

it("pinning moves the source to the pinned pane and closes the center peek", async () => {
  mockGetByPath(draftWithPill(), sourcePayload());
  const { container } = renderCockpit();
  fireEvent.click(await screen.findByRole("button", { name: /Methods/ }));
  fireEvent.mouseDown(container.querySelector(".cm-citation-pill")!);
  await screen.findByText(/The Smith 2021 Study/);
  fireEvent.click(screen.getByRole("button", { name: /pin source/i }));
  // Pinned pane shows it; the Pin button is gone (only the pinned, unpinnable copy remains).
  expect(container.querySelector(".cockpit-pinned .source-viewer")).not.toBeNull();
  expect(screen.queryByRole("button", { name: /pin source/i })).toBeNull();
});

it("closing the peek removes the source pane", async () => {
  mockGetByPath(draftWithPill(), sourcePayload());
  const { container } = renderCockpit();
  fireEvent.click(await screen.findByRole("button", { name: /Methods/ }));
  fireEvent.mouseDown(container.querySelector(".cm-citation-pill")!);
  await screen.findByText(/The Smith 2021 Study/);
  fireEvent.click(screen.getByRole("button", { name: /close source/i }));
  expect(container.querySelector(".cockpit-peek .source-viewer")).toBeNull();
});
