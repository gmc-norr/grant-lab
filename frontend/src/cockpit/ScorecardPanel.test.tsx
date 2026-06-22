import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ScorecardPanel } from "./ScorecardPanel";

const h = vi.hoisted(() => ({
  start: { mutate: () => {}, isPending: false, isError: false, error: null as Error | null },
  scorecard: { data: null as unknown },
}));

vi.mock("../api/evaluator", () => ({
  useLatestEvaluator: () => ({ data: { status: "completed", error: null } }),
  useStartEvaluator: () => h.start,
  useScorecard: () => h.scorecard,
}));

const COMPLETED = {
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
};

function wrap(ui: ReactNode) {
  const qc = new QueryClient();
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

afterEach(() => {
  h.start = { mutate: () => {}, isPending: false, isError: false, error: null };
  h.scorecard = { data: null };
});

describe("ScorecardPanel", () => {
  it("renders criteria bands sorted by weight, overall band, and staleness", () => {
    h.scorecard = COMPLETED;
    wrap(<ScorecardPanel id="ws1" onNavigate={vi.fn()} />);
    expect(screen.getByText("Quality")).toBeInTheDocument();
    expect(screen.getByText("Impact")).toBeInTheDocument();
    expect(screen.getByText(/at.?risk/i)).toBeInTheDocument(); // overall band pill
    expect(screen.getByText(/changed since/i)).toBeInTheDocument();
    expect(screen.getByText(/draft, rubric/i)).toBeInTheDocument();
    expect(screen.getByText("Mostly there.")).toBeInTheDocument();
  });

  it("surfaces a Re-evaluate gate failure (409/503/403) in the UI", () => {
    h.start = {
      mutate: () => {},
      isPending: false,
      isError: true,
      error: new Error("conflict: Workspace is still setting up"),
    };
    wrap(<ScorecardPanel id="ws1" onNavigate={vi.fn()} />);
    expect(screen.getByText(/still setting up/i)).toBeInTheDocument();
  });
});
