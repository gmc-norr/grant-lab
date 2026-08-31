import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { EditorView } from "@codemirror/view";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import * as citationsApi from "../api/citations";
import { resetEditorStore } from "../cockpit/editorStore";
import { theme } from "../theme/theme";
import { ArtifactSectionEditor } from "./ArtifactSectionEditor";

afterEach(() => {
  vi.restoreAllMocks();
  resetEditorStore();
});

function renderEditor(
  sections: { index: number; heading: string; text: string; sha: string }[],
  url = "/cockpit/ws-1",
) {
  const qc = new QueryClient();
  const save = { mutateAsync: vi.fn(), isPending: false };
  const { container } = render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <MemoryRouter initialEntries={[url]}>
        <QueryClientProvider client={qc}>
          <Routes>
            <Route
              path="/cockpit/:id"
              element={
                <ArtifactSectionEditor
                  workspaceId="ws-1"
                  branch="main"
                  queryKey={["spec", "ws-1"]}
                  head={"a".repeat(40)}
                  canEdit={true}
                  sections={sections}
                  save={save}
                  refetch={() => {}}
                  citations={false}
                />
              }
            />
          </Routes>
        </QueryClientProvider>
      </MemoryRouter>
    </MantineProvider>,
  );
  return { save, container };
}

it("resolves the active section by section.index, not array position", async () => {
  // Array order is REVERSED relative to section.index. Positional lookup would
  // open the wrong section; identity lookup opens the right one.
  // Navigate to ?sec=1 directly — the Outline is now in the shell sidebar, not the
  // editor. The editor resolves the section by its index (1), not array position (0).
  const sections = [
    { index: 1, heading: "Approach", text: "## Approach\nB.\n", sha: "s1" },
    { index: 0, heading: "", text: "preamble.\n", sha: "s0" },
  ];
  const { container } = renderEditor(sections, "/cockpit/ws-1?sec=1");
  // The editor's .cm-content shows section 1's text, though it is array element 0.
  // Assert via .cm-content textContent (the codebase idiom): the markdown() extension
  // splits a heading into sibling spans, so getByText(/## Approach/) would not match.
  await waitFor(() =>
    expect(container.querySelector(".cm-content")?.textContent).toContain("## Approach"),
  );
});

it("keeps buffers on same-branch saves and drops them when the branch changes", async () => {
  const qc = new QueryClient();
  const save = { mutateAsync: vi.fn(), isPending: false };
  const source = [{ index: 0, heading: "", text: "source branch\n", sha: "s0" }];
  const target = [{ index: 0, heading: "", text: "target branch\n", sha: "t0" }];
  const ui = (branch: string, head: string, sections: typeof source) => (
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <MemoryRouter initialEntries={["/cockpit/ws-1"]}>
        <QueryClientProvider client={qc}>
          <ArtifactSectionEditor
            workspaceId="ws-1"
            branch={branch}
            queryKey={["draft", "ws-1"]}
            head={head}
            canEdit
            sections={sections}
            save={save}
            refetch={() => {}}
          />
        </QueryClientProvider>
      </MemoryRouter>
    </MantineProvider>
  );
  const rendered = render(ui("main", "head-1", source));
  const editorNode = rendered.container.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: EditorView;
  };
  const view = editorNode.__cmView!;
  act(() => view.dispatch({ changes: { from: view.state.doc.length, insert: "LOCAL" } }));
  expect(view.state.doc.toString()).toContain("LOCAL");

  rendered.rerender(ui("main", "head-2", source));
  expect(view.state.doc.toString()).toContain("LOCAL");

  rendered.rerender(ui("short-version", "head-3", target));
  await waitFor(() => expect(view.state.doc.toString()).toBe("target branch\n"));
  expect(view.state.doc.toString()).not.toContain("LOCAL");
});

it("drops a save result that arrives after the branch changed underneath it", async () => {
  // `onSave` captures `content` from `buffers` BEFORE its await, then writes it
  // into `qc.setQueryData(queryKey, ...)`. `queryKey` is ["draft", id] -- no
  // branch in it -- so ONE cache entry is shared by every branch, and without
  // the guard a save begun on one branch lands on whichever is current when it
  // resolves.
  //
  // `branchChanged` cannot cover this: it masks editorValue, isDirty, setDirty
  // and activeDirty, but `onSave` reads `buffers[index]` raw, and by the time
  // the result arrives branchChanged has gone false again anyway -- so the leak
  // would read as ordinary content.
  //
  // This guards the CACHE. The request is refused separately, by the server —
  // tests/test_editing_save.py::test_a_form_loaded_on_another_branch_cannot_write_here —
  // and the test below covers what the user is shown when it is.
  const qc = new QueryClient();
  let releaseSave: ((v: { head: string; section_sha: string }) => void) | undefined;
  const save = {
    mutateAsync: vi.fn(
      () =>
        new Promise<{ head: string; section_sha: string }>((resolve) => {
          releaseSave = resolve;
        }),
    ),
    isPending: false,
  };
  const source = [{ index: 0, heading: "", text: "source branch\n", sha: "s0" }];
  const target = [{ index: 0, heading: "", text: "target branch\n", sha: "t0" }];
  qc.setQueryData(["draft", "ws-1"], { head: "head-1", sections: source });

  const ui = (branch: string, head: string, sections: typeof source) => (
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <MemoryRouter initialEntries={["/cockpit/ws-1"]}>
        <QueryClientProvider client={qc}>
          <ArtifactSectionEditor
            workspaceId="ws-1"
            branch={branch}
            queryKey={["draft", "ws-1"]}
            head={head}
            canEdit
            sections={sections}
            save={save}
            refetch={() => {}}
          />
        </QueryClientProvider>
      </MemoryRouter>
    </MantineProvider>
  );

  const rendered = render(ui("main", "head-1", source));
  const editorNode = rendered.container.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: EditorView;
  };
  const view = editorNode.__cmView!;
  act(() => view.dispatch({ changes: { from: view.state.doc.length, insert: "LEAKED" } }));

  // The user saves while still on `main`; the request is now in flight.
  fireEvent.click(screen.getByRole("button", { name: "Save section" }));
  expect(save.mutateAsync).toHaveBeenCalledOnce();

  // The branch switches underneath, and the cache is refilled with the target's
  // sections, exactly as a post-switch refetch would.
  qc.setQueryData(["draft", "ws-1"], { head: "head-3", sections: target });
  rendered.rerender(ui("short-version", "head-3", target));

  // Only NOW does the save from the other branch come back.
  await act(async () => {
    releaseSave!({ head: "head-9", section_sha: "leaked-sha" });
  });

  const cached = qc.getQueryData(["draft", "ws-1"]) as {
    head: string;
    sections: typeof source;
  };
  // Untouched: still the target branch's own section, sha and head.
  expect(cached.sections[0].text).toBe("target branch\n");
  expect(cached.sections[0].text).not.toContain("LEAKED");
  expect(cached.sections[0].sha).toBe("t0");
  expect(cached.head).toBe("head-3");
});

it("hands back the text of a save the server refused for the wrong branch", async () => {
  // The switch has already wiped the buffers by the time this lands, so the
  // rejected text is the ONLY remaining copy. It must be shown, and it must
  // survive the branch change that destroyed the buffers -- that is why the
  // branch effect does not reset it.
  const qc = new QueryClient();
  const save = {
    mutateAsync: vi
      .fn()
      .mockRejectedValue(
        new ApiError(
          "branch_mismatch",
          "this edit belongs to branch 'main'; the workspace is now on 'v2' — it was not saved",
        ),
      ),
    isPending: false,
  };
  const source = [{ index: 0, heading: "", text: "source branch\n", sha: "s0" }];
  const target = [{ index: 0, heading: "", text: "target branch\n", sha: "t0" }];
  const ui = (branch: string, sections: typeof source) => (
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <MemoryRouter initialEntries={["/cockpit/ws-1"]}>
        <QueryClientProvider client={qc}>
          <ArtifactSectionEditor
            workspaceId="ws-1"
            branch={branch}
            queryKey={["draft", "ws-1"]}
            head="head-1"
            canEdit
            sections={sections}
            save={save}
            refetch={() => {}}
          />
        </QueryClientProvider>
      </MemoryRouter>
    </MantineProvider>
  );

  const rendered = render(ui("main", source));
  const editorNode = rendered.container.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: EditorView;
  };
  const view = editorNode.__cmView!;
  act(() => view.dispatch({ changes: { from: view.state.doc.length, insert: "MY WORK" } }));
  fireEvent.click(screen.getByRole("button", { name: "Save section" }));

  // The branch it was made on is named, and so is the one we are on now.
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(/belongs to branch 'main'/);
  expect(alert).toHaveTextContent(/now on 'v2'/);
  // NOT the stale-section prompt: "reload and re-apply" is wrong advice here.
  expect(alert).not.toHaveTextContent(/changed underneath you/);

  // The text itself, verbatim.
  const kept = screen.getByLabelText("the edit that was not saved");
  expect(kept).toHaveTextContent("MY WORK");

  // And it is still there after the branch change wipes everything else.
  rendered.rerender(ui("v2", target));
  await waitFor(() => expect(view.state.doc.toString()).toBe("target branch\n"));
  expect(screen.getByLabelText("the edit that was not saved")).toHaveTextContent("MY WORK");
});

it("citations={false} renders no Cite control", () => {
  renderEditor([{ index: 0, heading: "", text: "x\n", sha: "s0" }]);
  expect(screen.queryByRole("toolbar", { name: /selection actions/i })).toBeNull();
});

it("openPeek prop opens the citation source peek panel", async () => {
  const fakeSource = {
    ev_id: "ev-9",
    citekey: "smith2021",
    reference_title: "Smith et al. 2021",
    reference_kind: "paper",
    chunk_index: 0,
    section_path: null,
    evidence_quote: "the azure sky",
    prev_ev_id: null,
    next_ev_id: null,
  };
  vi.spyOn(citationsApi, "useCitationSource").mockReturnValue({
    data: fakeSource,
    isLoading: false,
    isError: false,
  } as unknown as ReturnType<typeof citationsApi.useCitationSource>);

  const qc = new QueryClient();
  const save = { mutateAsync: vi.fn(), isPending: false };
  render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <MemoryRouter initialEntries={["/cockpit/ws-1"]}>
        <QueryClientProvider client={qc}>
          <Routes>
            <Route
              path="/cockpit/:id"
              element={
                <ArtifactSectionEditor
                  workspaceId="ws-1"
                  branch="main"
                  queryKey={["draft", "ws-1"]}
                  head={"a".repeat(40)}
                  canEdit={true}
                  sections={[{ index: 0, heading: "", text: "x\n", sha: "s0" }]}
                  save={save}
                  refetch={() => {}}
                  citations={true}
                  openPeek={{ ev: "ev-9", n: 1 }}
                />
              }
            />
          </Routes>
        </QueryClientProvider>
      </MemoryRouter>
    </MantineProvider>,
  );

  await waitFor(() =>
    expect(screen.getByRole("region", { name: /citation source peek/i })).toBeInTheDocument(),
  );
});

// Current CitationSourceOut shape (mirrors sourcePayload in Cockpit.test.tsx).
const FAKE_SOURCE = {
  evidence_id: "ev-9",
  reference_id: "r1",
  source_line: "smith2021 · Smith et al. 2021",
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
};

function renderDraftEditor(openPeek?: { ev: string; n: number }) {
  const qc = new QueryClient();
  const save = { mutateAsync: vi.fn(), isPending: false };
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <MemoryRouter initialEntries={["/cockpit/ws-1"]}>
        <QueryClientProvider client={qc}>
          <Routes>
            <Route
              path="/cockpit/:id"
              element={
                <ArtifactSectionEditor
                  workspaceId="ws-1"
                  branch="main"
                  queryKey={["draft", "ws-1"]}
                  head={"a".repeat(40)}
                  canEdit={true}
                  sections={[{ index: 0, heading: "", text: "x\n", sha: "s0" }]}
                  save={save}
                  refetch={() => {}}
                  citations={true}
                  openPeek={openPeek}
                />
              }
            />
          </Routes>
        </QueryClientProvider>
      </MemoryRouter>
    </MantineProvider>,
  );
}

it("renders a resize handle and the source viewer when a citation peek is open", async () => {
  vi.spyOn(citationsApi, "useCitationSource").mockReturnValue({
    data: FAKE_SOURCE,
    isLoading: false,
    isError: false,
  } as unknown as ReturnType<typeof citationsApi.useCitationSource>);
  const { container } = renderDraftEditor({ ev: "ev-9", n: 1 });
  await waitFor(() =>
    expect(screen.getByRole("separator", { name: "Resize source peek" })).toBeInTheDocument(),
  );
  // The source pane mounted with real content through the new Panel wrapper.
  expect(
    container.querySelector('[data-region="peek"] [data-testid="source-viewer"]'),
  ).not.toBeNull();
});

it("renders no resize handle when citations are on but no source pane is open", () => {
  // citations={true} with NO openPeek → peekEv/pinnedEv stay null → no source pane,
  // so the editor is a single full-width panel with no handle. Catches a regression
  // that would render a separator unconditionally whenever citations is enabled.
  vi.spyOn(citationsApi, "useCitationSource").mockReturnValue({
    data: undefined,
    isLoading: false,
    isError: false,
  } as unknown as ReturnType<typeof citationsApi.useCitationSource>);
  renderDraftEditor();
  expect(screen.queryByRole("separator")).toBeNull();
});
