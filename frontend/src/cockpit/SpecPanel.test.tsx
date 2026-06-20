import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { SpecPanel } from "./SpecPanel";

afterEach(() => {
  vi.restoreAllMocks();
});

function renderPanel() {
  const qc = new QueryClient();
  render(
    <QueryClientProvider client={qc}>
      <SpecPanel id="ws-1" />
    </QueryClientProvider>,
  );
}

const EMPTY = {
  data: { head: "a".repeat(40), can_edit: true, sections: [{ index: 0, heading: "", text: "", sha: "x" }] },
  error: undefined,
};
const SEEDED = {
  data: {
    head: "b".repeat(40),
    can_edit: true,
    sections: [
      { index: 0, heading: "", text: "", sha: "p" },
      { index: 1, heading: "Aims", text: "## Aims\n\n", sha: "a" },
    ],
  },
  error: undefined,
};

it("empty spec shows the skeleton CTA, then init reveals sections", async () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue(EMPTY as never);
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue(SEEDED as never);
  renderPanel();
  const cta = await screen.findByRole("button", { name: /Start spec from skeleton/i });
  // After init's onSuccess invalidates ['spec', id], the refetch returns the seeded spec.
  get.mockResolvedValue(SEEDED as never);
  fireEvent.click(cta);
  await waitFor(() => expect(post).toHaveBeenCalled());
  await screen.findByText(/Aims/);
});

it("a non-empty spec renders the editor with a 'Full spec' tab", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(SEEDED as never);
  renderPanel();
  await screen.findByRole("tab", { name: /full spec/i });
});
