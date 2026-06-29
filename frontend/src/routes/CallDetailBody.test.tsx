// frontend/src/routes/CallDetailBody.test.tsx
import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it } from "vitest";

import type { CallDetail } from "../api/calls";
import { theme } from "../theme/theme";
import { CallDetailBody } from "./CallDetailBody";

const DATA = {
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
  criteria: [],
  best_profile_id: 7,
} as unknown as CallDetail;

function renderBody(data: CallDetail) {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <MemoryRouter>{<CallDetailBody {...data} />}</MemoryRouter>
    </MantineProvider>,
  );
}

it("renders the title heading, ranking score band, and analyst notes", () => {
  renderBody(DATA);
  expect(screen.getByRole("heading", { name: "Genomics Grant" })).toBeInTheDocument();
  expect(screen.getByText("72")).toHaveAttribute("data-band", "strong");
  expect(screen.getByText("Deep summary.")).toBeInTheDocument();
  expect(screen.getByText("Budget & funding")).toBeInTheDocument();
});

it("Start drafting links to /cockpit/new with the call + best profile prefilled", () => {
  renderBody(DATA);
  const href = screen.getByRole("link", { name: /start drafting/i }).getAttribute("href") ?? "";
  expect(href).toContain("/cockpit/new?");
  expect(href).toContain("call_id=5");
  expect(href).toContain("profile_id=7");
});

it("renders the low band for a zero aggregate score", () => {
  renderBody({ ...DATA, ranking: { ...DATA.ranking, aggregate_score: 0 } } as CallDetail);
  expect(screen.getByText("0")).toHaveAttribute("data-band", "low");
});
