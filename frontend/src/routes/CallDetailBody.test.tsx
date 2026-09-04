// frontend/src/routes/CallDetailBody.test.tsx
import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import type { CallDetail } from "../api/calls";
import { theme } from "../theme/theme";
import { CallDetailBody } from "./CallDetailBody";

// The Start-drafting button is edition-gated and isMatchEdition is a BUILD-TIME
// constant, so a test that read the real value would pass or fail by environment.
// Mutable holder + GETTER, the pattern from AdminNav.test.tsx:25-34: the getter is
// load-bearing, because a plain value would be frozen at import time and the match
// case below would silently run the FULL branch.
const edition = vi.hoisted(() => ({ isMatch: false }));
vi.mock("../edition", () => ({
  get isMatchEdition() {
    return edition.isMatch;
  },
  get APP_EDITION() {
    return edition.isMatch ? "match" : "full";
  },
  resolveEdition: (raw: string | undefined) => (raw === "match" ? "match" : "full"),
}));

afterEach(() => {
  edition.isMatch = false;
});

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

it("the FULL edition links Start drafting to /cockpit/new with the call + best profile", () => {
  // The control for the match test below. Without it a fix that dropped the
  // button unconditionally — i.e. broke the full product, which is run-record
  // ESCALATE E1 — would leave the match test green.
  renderBody(DATA);
  const link = screen.getByRole("link", { name: /start drafting/i });
  const href = link.getAttribute("href") ?? "";
  expect(href).toContain("/cockpit/new?");
  expect(href).toContain("call_id=5");
  expect(href).toContain("profile_id=7");
});

it("the FULL edition omits profile_id when there is no best profile", () => {
  renderBody({ ...DATA, best_profile_id: null } as CallDetail);
  const href = screen.getByRole("link", { name: /start drafting/i }).getAttribute("href") ?? "";
  expect(href).toContain("call_id=5");
  expect(href).not.toContain("profile_id");
});

it("the MATCH edition renders NO Start-drafting control — /cockpit/new is not mounted", () => {
  // It is a <Button component={Link}>, so it can carry a link or a button role
  // depending on the react-router version; both are checked, and so is every
  // rendered href — a renamed link would pass a name-only assertion.
  edition.isMatch = true;
  renderBody(DATA);
  expect(screen.queryByRole("link", { name: /start drafting/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /start drafting/i })).not.toBeInTheDocument();
  for (const a of screen.queryAllByRole("link")) {
    expect(a.getAttribute("href") ?? "").not.toContain("/cockpit");
  }
});

it("renders the low band for a zero aggregate score", () => {
  renderBody({ ...DATA, ranking: { ...DATA.ranking, aggregate_score: 0 } } as CallDetail);
  expect(screen.getByText("0")).toHaveAttribute("data-band", "low");
});
