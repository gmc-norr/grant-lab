import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { ProposalReview } from "./ProposalReview";

afterEach(() => {
  vi.restoreAllMocks();
});

function renderReview(props: { locked?: boolean } = {}) {
  const qc = new QueryClient();
  render(
    <QueryClientProvider client={qc}>
      <ProposalReview
        workspaceId="ws-1"
        artifact="spec"
        currentSections={[{ index: 1, text: "## Aims\norig.\n" }]}
        locked={props.locked ?? false}
      />
    </QueryClientProvider>,
  );
}

const ROW = {
  id: "pc-1",
  status: "pending",
  target_artifact: "spec",
  section_index: 1,
  kind: "prose_edit",
  origin_kind: "interactive_turn",
  new_text: "## Aims\nbetter.\n",
  resulting_commit_sha: null,
  created_at: "2026-06-21T10:00:00Z",
  resolved_by: null,
  resolved_at: null,
};

it("renders nothing when there are no pending proposals", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: { items: [] }, error: undefined } as never);
  renderReview();
  await waitFor(() =>
    expect(screen.queryByText(/pending proposal/i)).not.toBeInTheDocument(),
  );
});

it("shows a prose proposal with accept/reject and hides citation rows", async () => {
  const citation = { ...ROW, id: "pc-2", kind: "citation_insert", new_text: null };
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { items: [ROW, citation] },
    error: undefined,
  } as never);
  renderReview();
  await screen.findByText(/pending proposals \(1\)/i); // count reflects prose only
  expect(screen.getByRole("button", { name: /accept/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /reject/i })).toBeInTheDocument();
});

it("disables Accept with a hint when the spec is locked", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: { items: [ROW] }, error: undefined } as never);
  renderReview({ locked: true });
  const accept = await screen.findByRole("button", { name: /accept/i });
  expect(accept).toBeDisabled();
});

it("renders an add/del line diff of the proposed section", async () => {
  // current section "## Aims\norig.\n" vs proposed "## Aims\nbetter.\n":
  // the heading line is unchanged, "orig." is deleted, "better." is added.
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: { items: [ROW] }, error: undefined } as never);
  renderReview();
  const del = await screen.findByText("- orig.");
  expect(del).toHaveClass("diff-del");
  expect(screen.getByText("+ better.")).toHaveClass("diff-add");
});

it("surfaces a reject error in the notice", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: { items: [ROW] }, error: undefined } as never);
  // The /api error envelope is { error: { code, message } }; unwrap throws ApiError,
  // and onReject strips the machine code prefix before showing the message.
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: undefined,
    error: { error: { code: "conflict", message: "already resolved" } },
  } as never);
  renderReview();
  fireEvent.click(await screen.findByRole("button", { name: /reject/i }));
  const notice = await screen.findByRole("status");
  expect(notice).toHaveTextContent("already resolved");
  expect(notice).not.toHaveTextContent("conflict:");
});
