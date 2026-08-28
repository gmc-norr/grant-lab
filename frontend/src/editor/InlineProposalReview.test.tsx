// frontend/src/editor/InlineProposalReview.test.tsx
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { theme } from "../theme/theme";
import { InlineProposalReview } from "./InlineProposalReview";

afterEach(() => {
  vi.restoreAllMocks();
});

const ROW = {
  id: "pc1", status: "pending", target_artifact: "draft", section_index: 1,
  kind: "prose_edit", origin_kind: "turn", new_text: "## H\nnew body\n",
  resulting_commit_sha: null, created_at: null, resolved_by: null, resolved_at: null,
};

function renderIPR(rows: unknown[], over: Record<string, unknown> = {}) {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: { items: rows }, error: undefined } as never);
  render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={new QueryClient()}>
        <InlineProposalReview workspaceId="ws-1" artifact="draft" sectionIndex={1}
          currentText={"## H\nold body\n"} canEdit {...over} />
      </QueryClientProvider>
    </MantineProvider>,
  );
}

it("renders the diff for a pending proposal in this section, with word-level spans", async () => {
  // currentText "old body" vs proposed "new body": "old"/"new" differ, " body" is
  // shared, so DiffView wraps only the changed word in a span (not the whole
  // line) — the text is split across elements, not one "new body" text node.
  renderIPR([ROW]);
  expect(await screen.findByText(/suggested edit/i)).toBeInTheDocument();
  const addSpan = screen.getByText("new");
  expect(addSpan).toHaveAttribute("data-span", "add");
  expect(addSpan.closest("[data-diff]")).toHaveAttribute("data-diff", "add");
});

it("renders nothing when no pending proposal targets this section", async () => {
  // Build a section-7 proposal with a distinctive text so we can prove it was loaded.
  const SECTION_7_ROW = { ...ROW, section_index: 7, new_text: "DISTINCT-PROPOSAL-TEXT\n" };

  // Render for sectionIndex=7 first — proves the GET resolved (data loaded + section 7 matches).
  const qc = new QueryClient();
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(
    { data: { items: [SECTION_7_ROW] }, error: undefined } as never,
  );
  const { rerender } = render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <InlineProposalReview workspaceId="ws-1" artifact="draft" sectionIndex={7}
          currentText={"## H\nold body\n"} canEdit />
      </QueryClientProvider>
    </MantineProvider>,
  );
  // findBy awaits query resolution — data is definitely loaded when this resolves.
  await screen.findByText(/suggested edit/i);

  // Rerender same provider tree (cache populated) for sectionIndex=1.
  rerender(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <InlineProposalReview workspaceId="ws-1" artifact="draft" sectionIndex={1}
          currentText={"## H\nold body\n"} canEdit />
      </QueryClientProvider>
    </MantineProvider>,
  );
  // Section 1 must show nothing — data is loaded, so this is genuinely non-vacuous.
  expect(screen.queryByText(/suggested edit/i)).toBeNull();
  expect(screen.queryByText(/DISTINCT-PROPOSAL-TEXT/)).toBeNull();
});

it("Accept posts to the accept endpoint", async () => {
  const post = vi.spyOn(clientMod.api, "POST")
    .mockResolvedValue({ data: { ...ROW, status: "accepted" }, error: undefined } as never);
  renderIPR([ROW]);
  fireEvent.click(await screen.findByRole("button", { name: /accept/i }));
  await waitFor(() => expect(post).toHaveBeenCalled());
  expect(String(post.mock.calls[0][0])).toContain("/accept");
});

it("hides Accept/Reject for non-writers", async () => {
  renderIPR([ROW], { canEdit: false });
  expect(await screen.findByText(/suggested edit/i)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /accept/i })).toBeNull();
});
