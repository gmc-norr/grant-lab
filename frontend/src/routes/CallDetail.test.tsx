import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { theme } from "../theme/theme";
import { CallDetail } from "./CallDetail";

afterEach(() => { vi.restoreAllMocks(); });

function LocationProbe() {
  return <span data-testid="loc">{useLocation().pathname}</span>;
}

function renderDetail(url = "/calls/5?profile=7") {
  const qc = new QueryClient();
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[url]}>
          <Routes>
            <Route path="/calls/:id" element={<><CallDetail /><LocationProbe /></>} />
            <Route path="/" element={<LocationProbe />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

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
    applicant_notes: [{ category: "budget", heading: "Budget & funding", text: "Note." }],
  },
  criteria: [
    {
      criterion: "embedding", label: "Topic similarity", rating: "pass", is_topic_fit: false,
      detail: "Cosine similarity 0.420 (pass threshold 0.30).", rationale: null,
      is_reason_code: true, quoted_evidence: null,
    },
  ],
  best_profile_id: 7,
};

it("renders the ranking hero, analyst notes, and criterion why-string", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  renderDetail();
  expect(await screen.findByRole("heading", { name: "Genomics Grant" })).toBeInTheDocument();
  expect(screen.getByText("72")).toHaveAttribute("data-band", "strong"); // 72 >= 70
  expect(screen.getByText("Deep summary.")).toBeInTheDocument();
  expect(screen.getByText("Budget & funding")).toBeInTheDocument();
  expect(screen.getByText("Cosine similarity 0.420 (pass threshold 0.30).")).toBeInTheDocument();
});

it("Start drafting links to /cockpit/new with the call + best profile prefilled", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  renderDetail();
  const href =
    (await screen.findByRole("link", { name: /start drafting/i })).getAttribute("href") ?? "";
  expect(href).toContain("/cockpit/new?");
  expect(href).toContain("call_id=5");
  expect(href).toContain("profile_id=7"); // best_profile_id from RANKED
});

it("renders the low band for a zero aggregate score", async () => {
  const zero = { ...RANKED, ranking: { ...RANKED.ranking, aggregate_score: 0 } };
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: zero, error: undefined } as never);
  renderDetail();
  expect(await screen.findByText("0")).toHaveAttribute("data-band", "low");
});

it("a not-found error shows the not-found alert", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: undefined,
    error: { error: { code: "not_found", message: "call not found" } },
  } as never);
  renderDetail();
  await screen.findByRole("alert");
  // Proves the notFound branch fires: unwrap throws ApiError with .code = "not_found",
  // which the component reads directly off the error object (cast is correct).
  expect(screen.getByRole("alert").textContent).toBe("Call not found.");
});

it("Hide requires confirm before posting", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  const post = vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue({ data: { hidden: true }, error: undefined } as never);
  renderDetail();
  // First click: shows confirm step, does NOT fire POST yet.
  fireEvent.click(await screen.findByRole("button", { name: /hide this call/i }));
  expect(post).not.toHaveBeenCalled();
  // Confirm button is now visible.
  expect(screen.getByRole("button", { name: /yes, hide it/i })).toBeInTheDocument();
  // Cancel goes back to the initial state.
  fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
  expect(screen.getByRole("button", { name: /hide this call/i })).toBeInTheDocument();
});

it("Hide posts after confirm and shows undo affordance", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  const post = vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue({ data: { hidden: true }, error: undefined } as never);
  // stub DELETE for unhide (not called in this test, but must resolve cleanly)
  vi.spyOn(clientMod.api, "DELETE").mockResolvedValue({ data: { hidden: false }, error: undefined } as never);
  renderDetail();
  fireEvent.click(await screen.findByRole("button", { name: /hide this call/i }));
  fireEvent.click(screen.getByRole("button", { name: /yes, hide it/i }));
  await waitFor(() => expect(post).toHaveBeenCalledWith("/api/calls/{call_id}/hide", {
    params: { path: { call_id: 5 } },
  }));
  // After hide: undo + back-to-calls buttons appear; no auto-navigate.
  expect(await screen.findByRole("button", { name: /undo/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /back to all calls/i })).toBeInTheDocument();
});

it("Undo calls DELETE unhide and reverts to hide button", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { hidden: true }, error: undefined } as never);
  const del = vi
    .spyOn(clientMod.api, "DELETE")
    .mockResolvedValue({ data: { hidden: false }, error: undefined } as never);
  renderDetail();
  fireEvent.click(await screen.findByRole("button", { name: /hide this call/i }));
  fireEvent.click(screen.getByRole("button", { name: /yes, hide it/i }));
  const undoBtn = await screen.findByRole("button", { name: /undo/i });
  fireEvent.click(undoBtn);
  await waitFor(() => expect(del).toHaveBeenCalledWith("/api/calls/{call_id}/hide", {
    params: { path: { call_id: 5 } },
  }));
  // After undo: back to initial hide button.
  expect(await screen.findByRole("button", { name: /hide this call/i })).toBeInTheDocument();
});

it("renders deadline_local_date instead of slicing the UTC string", async () => {
  const withDeadline = {
    ...RANKED,
    call: {
      ...RANKED.call,
      deadline: "2026-09-15T22:00:00Z",
      deadline_local_date: "2026-09-16",
    },
  };
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: withDeadline, error: undefined } as never);
  renderDetail();
  await screen.findByRole("heading", { name: "Genomics Grant" });
  // The local date (2026-09-16) must appear, not the UTC slice (2026-09-15).
  expect(screen.getByText("2026-09-16")).toBeInTheDocument();
  expect(screen.queryByText("2026-09-15")).not.toBeInTheDocument();
});
