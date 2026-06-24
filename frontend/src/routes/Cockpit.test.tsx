import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { CockpitSidebar } from "../cockpit/CockpitSidebar";
import { resetEditorStore } from "../cockpit/editorStore";
import { ScorecardPanel } from "../cockpit/ScorecardPanel";
import { Cockpit } from "./Cockpit";

afterEach(() => {
  vi.restoreAllMocks();
  resetEditorStore();
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

function renderCockpit(url = "/cockpit/ws-1") {
  const qc = new QueryClient();
  return {
    qc,
    ...render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[url]}>
          <Routes>
            <Route
              path="/cockpit/:id"
              element={
                <>
                  <CockpitSidebar />
                  <Cockpit />
                </>
              }
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  };
}

// add near the top, after the other helpers:
function LocationProbe() {
  return <span data-testid="loc">{useLocation().search}</span>;
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

// Like mockGetByPath but routes /source by the evidence_id path param (openapi-
// fetch passes the path TEMPLATE as arg1 and the interpolated id in arg2's
// params.path), so prev/next navigation can be asserted to refetch the new ev.
function mockGetByEv(draft: unknown, sourceByEv: Record<string, unknown>) {
  return vi.spyOn(clientMod.api, "GET").mockImplementation(((
    path: string,
    init?: { params?: { path?: { evidence_id?: string } } },
  ) => {
    if (path.endsWith("/source")) {
      const ev = init?.params?.path?.evidence_id ?? "";
      return Promise.resolve(sourceByEv[ev]);
    }
    return Promise.resolve(draft);
  }) as never);
}

// Route GET by path with a DRAFT-ONLY sequence (first /draft -> firstDraft, rest ->
// restDraft). The always-mounted BottomPanel also fires /proposed-changes, /problems
// (InboxPanel + badges) and /spec (InboxPanel.useSpec) — give each a fixed payload so
// they NEVER consume a draft-sequence slot. Only the /draft counter advances, so the
// draft sequence the test depends on is fully decoupled from the bottom-panel calls.
function mockDraftSequenceByPath(firstDraft: unknown, restDraft: unknown) {
  let draftCalls = 0;
  return vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/proposed-changes")) {
      return Promise.resolve({ data: { items: [] }, error: undefined });
    }
    if (path.endsWith("/problems")) {
      return Promise.resolve({
        data: { problems: [], counts: { error: 0, warning: 0 }, scorecard_stale: [] },
        error: undefined,
      });
    }
    if (path.endsWith("/spec")) {
      return Promise.resolve({
        data: {
          head: "a".repeat(40),
          can_edit: false,
          sections: [{ index: 0, heading: "", text: "", sha: "s0" }],
          locked: false,
        },
        error: undefined,
      });
    }
    if (path.endsWith("/draft")) {
      const payload = draftCalls === 0 ? firstDraft : restDraft;
      draftCalls += 1;
      return Promise.resolve(payload);
    }
    return Promise.resolve(firstDraft); // no other GET paths in these two tests
  }) as never);
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
  // Switch back via the Section tab -> editable again (?sec persists across the Full tab,
  // so switching back to Section view restores the same section).
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
  mockDraftSequenceByPath(original, committed);
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
  // Pinned pane shows it; the Pin button is gone (only the pinned, unpinnable copy remains)
  // AND the center peek has closed (pin does setPinnedEv + setPeekEv(null)).
  expect(container.querySelector(".cockpit-pinned .source-viewer")).not.toBeNull();
  expect(container.querySelector(".cockpit-peek")).toBeNull();
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

it("Next citation refetches and shows the next reference cite in the peek", async () => {
  const SECOND_EV = "ev-bbbbbbbbbbbb";
  mockGetByEv(draftWithPill(), {
    [PEEK_EV]: sourcePayload({
      text: "FIRST SOURCE.",
      highlight: null,
      index: 1,
      total: 2,
      next_ev: SECOND_EV,
    }),
    [SECOND_EV]: sourcePayload({
      evidence_id: SECOND_EV,
      text: "SECOND SOURCE.",
      highlight: null,
      index: 2,
      total: 2,
      prev_ev: PEEK_EV,
      next_ev: null,
    }),
  });
  const { container } = renderCockpit();
  fireEvent.click(await screen.findByRole("button", { name: /Methods/ }));
  fireEvent.mouseDown(container.querySelector(".cm-citation-pill")!);
  expect(await screen.findByText("FIRST SOURCE.")).toBeInTheDocument();
  // Clicking Next must drive peekEv -> next_ev and refetch THAT cite (a wrong-setter
  // or stale-key regression would keep showing FIRST SOURCE).
  fireEvent.click(screen.getByRole("button", { name: /next citation/i }));
  expect(await screen.findByText("SECOND SOURCE.")).toBeInTheDocument();
  expect(screen.queryByText("FIRST SOURCE.")).toBeNull();
});

it("select-to-cite: selecting a saved claim opens the composer and adds a citation", async () => {
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/references")) {
      return Promise.resolve({ data: [{ id: "r1", citekey: "k", title: "Smith", kind: "paper", text_status: "done" }], error: undefined });
    }
    if (path.endsWith("/chunks")) {
      return Promise.resolve({ data: { reference_id: "r1", title: "Smith", chunks: [{ chunk_index: 0, section_path: null, text: "the azure sky" }] }, error: undefined });
    }
    return Promise.resolve(draftPayload(true) as never); // /draft
  }) as never);
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { ev_id: "ev-1", citekey: "k", commit_sha: "a".repeat(40) }, error: undefined } as never);
  renderCockpit();
  fireEvent.click(await screen.findByRole("button", { name: /Methods/ }));
  // make a selection in the (clean) section via the editor handle
  const view = (document.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: import("@codemirror/view").EditorView;
  }).__cmView!;
  const docText = view.state.doc.toString();
  const f = docText.indexOf("body");
  act(() => view.dispatch({ selection: { anchor: f, head: f + 5 } })); // "body." incl. period
  // the Cite control appears (section is not dirty)
  fireEvent.click(await screen.findByRole("button", { name: /cite selection/i }));
  fireEvent.click(await screen.findByRole("button", { name: /Smith/ }));
  const pre = await screen.findByText("the azure sky");
  vi.spyOn(window, "getSelection").mockReturnValue({
    rangeCount: 1,
    getRangeAt: () => ({ startOffset: 4, endOffset: 9, startContainer: pre.firstChild, endContainer: pre.firstChild, toString: () => "azure" }),
    toString: () => "azure",
  } as never);
  fireEvent.mouseUp(pre);
  fireEvent.click(await screen.findByRole("button", { name: /add citation/i }));
  await waitFor(() => expect(post).toHaveBeenCalledOnce());
  const body = (post.mock.calls[0][1] as { body: Record<string, unknown> }).body;
  expect(body).toMatchObject({ reference_id: "r1", evidence_quote: "azure", anchor: "body." });
  // onDone refetches + clears the selection inside React state; flush it so the
  // composer (the single "Add a citation" aside) closes and no act() warning leaks.
  await waitFor(() => expect(screen.queryByLabelText("Add a citation")).toBeNull());
});

it("select-to-cite gate: dirtying the section disables Cite (must save first)", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(draftPayload(true) as never);
  renderCockpit();
  fireEvent.click(await screen.findByRole("button", { name: /Methods/ }));
  const view = (document.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: import("@codemirror/view").EditorView;
  }).__cmView!;
  // Select "body." in the CLEAN section -> Cite enabled (anchor exists on disk).
  const docText = view.state.doc.toString();
  const f = docText.indexOf("body");
  act(() => view.dispatch({ selection: { anchor: f, head: f + 5 } }));
  const cite = await screen.findByRole("button", { name: /cite selection/i });
  expect(cite).toBeEnabled();
  // Now dirty the section by inserting at end-of-doc (the held [f,f+5] selection is
  // unaffected and stays non-empty — see the editor's re-emit test). Cite must
  // disable because the anchor isn't yet committed to proposal.md.
  act(() => view.dispatch({ changes: { from: view.state.doc.length, insert: "DIRTY" } }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: /cite selection/i })).toBeDisabled(),
  );
  expect(screen.getByRole("button", { name: /cite selection/i })).toHaveAttribute(
    "title",
    "Save this section first",
  );
});

it("clicking the Spec tab switches the artifact view and sets ?tab=spec", async () => {
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/spec")) {
      return Promise.resolve({
        data: { head: "a".repeat(40), can_edit: true, sections: [{ index: 0, heading: "", text: "", sha: "s0" }], locked: false },
        error: undefined,
      });
    }
    if (path.endsWith("/proposed-changes")) return Promise.resolve({ data: { items: [] }, error: undefined });
    if (path.endsWith("/problems")) return Promise.resolve({ data: { problems: [], counts: { error: 0, warning: 0 }, scorecard_stale: [] }, error: undefined });
    return Promise.resolve(draftPayload(true)); // /draft
  }) as never);
  renderCockpit();
  await screen.findByRole("button", { name: /Methods/ });
  fireEvent.click(screen.getByRole("tab", { name: /^spec$/i }));
  // The empty-spec CTA proves SpecPanel mounted from ?tab=spec.
  expect(await screen.findByRole("button", { name: /start spec from skeleton/i })).toBeInTheDocument();
});

it("ScorecardPanel 'Go to §' passes section_index to onNavigate", async () => {
  const onNavigate = vi.fn();
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/scorecard")) {
      return Promise.resolve({
        data: {
          scorecard: {
            criteria: [
              { index: 0, name: "Impact", band: "gap", note: "", deep_link: { artifact: "draft", section_index: 2 }, weight: 1, effective_weight: 1 },
            ],
            overall_band: "gap",
            summary: "",
          },
          evaluated_draft_sha: null, evaluated_spec_sha: null, completed_at: null, stale: false, stale_reasons: [],
        },
        error: undefined,
      });
    }
    return Promise.resolve({ data: null, error: undefined }); // /evaluator-runs/latest
  }) as never);
  const qc = new QueryClient();
  render(
    <QueryClientProvider client={qc}>
      <ScorecardPanel id="ws-1" onNavigate={onNavigate} />
    </QueryClientProvider>,
  );
  fireEvent.click(await screen.findByRole("button", { name: /go to §2/i }));
  expect(onNavigate).toHaveBeenCalledWith({ top_tab: "draft", section_index: 2 });
});

it("an out-of-range ?sec clamps to a valid section and history-replaces the URL", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(draftPayload(true) as never);
  const qc = new QueryClient();
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/cockpit/ws-1?sec=99"]}>
        <Routes>
          <Route path="/cockpit/:id" element={<><Cockpit /><LocationProbe /></>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  // The editor renders the preamble (section 0), not a blank doc...
  await waitFor(() =>
    expect(document.querySelector(".cm-content")?.textContent).toContain("preamble"),
  );
  // ...and the URL self-corrected to a valid section.
  await waitFor(() => expect(screen.getByTestId("loc").textContent).toContain("sec=0"));
});

it("select-to-cite: retains the held selection across a background draft refetch (skips remote echoes)", async () => {
  // Reconciled for the skip-remote selection fix (CodeMirrorEditor): a held selection
  // is mapped across a controlled `value` replace when a background draft refetch
  // returns SHORTER section bytes. The push is a Transaction.remote, which the editor
  // now SKIPS — it does NOT re-derive (or empty) the held selection. So Cite stays
  // ENABLED; the held anchor remains exactly what the user selected and staleness is
  // caught server-side at cite time (a 409 invalidates the draft to re-select).
  const original = draftPayload(true);
  const shortened = {
    data: {
      head: "a".repeat(40),
      can_edit: true,
      sections: [
        { index: 0, heading: "", text: "preamble\n", sha: "s0" },
        // Same heading line, but the body collapses so the held [f,f+5] range empties.
        { index: 1, heading: "Methods", text: "## Methods\n", sha: "s1" },
      ],
      citations: {},
    },
    error: undefined,
  };
  mockDraftSequenceByPath(original, shortened);
  const { qc } = renderCockpit();
  fireEvent.click(await screen.findByRole("button", { name: /Methods/ }));
  const view = (document.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: import("@codemirror/view").EditorView;
  }).__cmView!;
  // Select "body." in the CLEAN section -> Cite enabled.
  const docText = view.state.doc.toString();
  const f = docText.indexOf("body");
  act(() => view.dispatch({ selection: { anchor: f, head: f + 5 } }));
  const cite = await screen.findByRole("button", { name: /cite selection/i });
  expect(cite).toBeEnabled();
  // Drive a background refetch that returns the shortened section text. The controlled
  // value-push is a Transaction.remote; the editor SKIPS it, so the held selection is
  // retained (not cleared) and the section stays clean.
  await act(async () => {
    await qc.invalidateQueries({ queryKey: ["draft", "ws-1"] });
  });
  // The section is still clean and the held anchor was retained, so Cite stays enabled.
  await waitFor(() =>
    expect(screen.getByRole("button", { name: /cite selection/i })).not.toBeDisabled(),
  );
});
