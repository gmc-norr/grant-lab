import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { ScorecardPanel } from "./ScorecardPanel";

vi.mock("../api/evaluator", () => ({
  useLatestEvaluator: () => ({ data: { status: "completed", error: null } }),
  useStartEvaluator: () => ({ mutate: vi.fn(), isPending: false }),
  useScorecard: () => ({
    data: {
      scorecard: {
        overall_band: "at_risk",
        summary: "Mostly there.",
        criteria: [
          { index: 0, name: "Quality", band: "strong", note: "Rigorous.",
            deep_link: null, weight: 0.6, effective_weight: 0.6 },
          { index: 1, name: "Impact", band: "gap", note: "No KPIs.",
            deep_link: { artifact: "draft", section_index: 1 }, weight: 0.4, effective_weight: 0.4 },
        ],
      },
      stale: true,
      stale_reasons: ["draft", "rubric"],
      completed_at: "2026-06-22T10:00:00Z",
    },
  }),
}));

function wrap(ui: ReactNode) {
  const qc = new QueryClient();
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("ScorecardPanel", () => {
  it("renders criteria bands sorted by weight, overall band, and staleness", () => {
    wrap(<ScorecardPanel id="ws1" onNavigate={vi.fn()} />);
    expect(screen.getByText("Quality")).toBeInTheDocument();
    expect(screen.getByText("Impact")).toBeInTheDocument();
    expect(screen.getByText(/at.?risk/i)).toBeInTheDocument(); // overall band pill
    expect(screen.getByText(/changed since/i)).toBeInTheDocument();
    expect(screen.getByText(/draft, rubric/i)).toBeInTheDocument();
    expect(screen.getByText("Mostly there.")).toBeInTheDocument();
  });
});
