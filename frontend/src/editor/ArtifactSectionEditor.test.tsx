import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as citationsApi from "../api/citations";
import { resetEditorStore } from "../cockpit/editorStore";
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
    <MemoryRouter initialEntries={[url]}>
      <QueryClientProvider client={qc}>
        <Routes>
          <Route
            path="/cockpit/:id"
            element={
              <ArtifactSectionEditor
                workspaceId="ws-1"
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
    </MemoryRouter>,
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

it("citations={false} renders no Cite control", () => {
  renderEditor([{ index: 0, heading: "", text: "x\n", sha: "s0" }]);
  expect(screen.queryByRole("button", { name: /cite selection/i })).toBeNull();
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
    <MemoryRouter initialEntries={["/cockpit/ws-1"]}>
      <QueryClientProvider client={qc}>
        <Routes>
          <Route
            path="/cockpit/:id"
            element={
              <ArtifactSectionEditor
                workspaceId="ws-1"
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
    </MemoryRouter>,
  );

  await waitFor(() =>
    expect(screen.getByRole("region", { name: /citation source peek/i })).toBeInTheDocument(),
  );
});
