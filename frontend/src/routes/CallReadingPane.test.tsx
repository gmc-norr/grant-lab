// frontend/src/routes/CallReadingPane.test.tsx
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { theme } from "../theme/theme";
import { CallReadingPane } from "./CallReadingPane";

afterEach(() => { vi.restoreAllMocks(); });

const RANKED = {
  call: {
    id: 5, title: "Genomics Grant", source_name: "Vinnova", programme: "Prog",
    external_id: "EX-1", published_at: null, deadline: null, budget_min: null,
    budget_max: null, currency: null, url: "https://x", keywords: ["dna"], summary: "Sum.",
  },
  ranking: {
    aggregate_score: 72, aggregate_reason_label: null, model_label: "Sonnet 4.6",
    rationale: "Strong fit.", embed_similarity: 0.42, escalated: false,
    escalated_from_score: null, analysis_model_label: "Sonnet 4.6", summary: "Deep summary.",
    ranking_failed: false, ranking_failed_at: null,
    applicant_notes: [],
  },
  criteria: [],
  best_profile_id: 7,
};

function renderPane(url: string) {
  const qc = new QueryClient();
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[url]}>
          <CallReadingPane />
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

it("shows a placeholder and the landmark when nothing is selected", () => {
  renderPane("/");
  expect(screen.getByRole("complementary", { name: /call preview/i })).toBeInTheDocument();
  expect(screen.getByText(/select a call to preview/i)).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: /open full page/i })).toBeNull();
});

it("renders the selected call's detail with an Open-full-page link", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  renderPane("/?selected=5&profile=7");
  expect(await screen.findByRole("heading", { name: "Genomics Grant" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /open full page/i })).toHaveAttribute("href", "/calls/5?profile=7");
});

it("close clears the selection and returns to the placeholder", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  renderPane("/?selected=5&profile=7");
  await screen.findByRole("heading", { name: "Genomics Grant" });
  fireEvent.click(screen.getByRole("button", { name: /close preview/i }));
  expect(screen.getByText(/select a call to preview/i)).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Genomics Grant" })).toBeNull();
});

it("shows a not-found message when the call is missing", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: undefined,
    error: { error: { code: "not_found", message: "call not found" } },
  } as never);
  renderPane("/?selected=999");
  expect(await screen.findByText("Call not found.")).toBeInTheDocument();
});
