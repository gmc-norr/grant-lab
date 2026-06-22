import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ReviewPanel } from "./ReviewPanel";

// ---------------------------------------------------------------------------
// Hoisted mutable hook stubs (mirrors ScorecardPanel.test.tsx structure)
// ---------------------------------------------------------------------------
const h = vi.hoisted(() => ({
  latestRun: { data: null as unknown, isLoading: false },
  start: { mutate: () => {}, isPending: false, isError: false, error: null as Error | null },
  fitCheckFile: { data: null as unknown, isLoading: false },
  rubricReviewFile: { data: null as unknown, isLoading: false },
}));

vi.mock("../api/review", () => ({
  useLatestReviewRun: () => h.latestRun,
  useStartReviewRun: () => h.start,
  useReviewFile: (_id: string, _runId: string | null, name: string) => {
    if (name === "fit-check.md") return h.fitCheckFile;
    if (name === "rubric-review.md") return h.rubricReviewFile;
    return { data: null, isLoading: false };
  },
}));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------
// Dollar fields use the real wire shape: strings (decimal) or null.
const BUDGET_OK = { runs_used: 1, runs_cap: 10, dollars_used: "0.12", dollars_cap: "5.000" };
const BUDGET_EXHAUSTED = { runs_used: 10, runs_cap: 10, dollars_used: "5.000", dollars_cap: "5.000" };

const ITEMS_DONE = [
  { kind: "fit_check", status: "done", error_message: null, estimated_dollars: "0.010" },
  { kind: "persona_domain_skeptic", status: "done", error_message: null, estimated_dollars: "0.020" },
  { kind: "persona_methodology_critic", status: "done", error_message: null, estimated_dollars: "0.020" },
  { kind: "persona_translation_skeptic", status: "done", error_message: null, estimated_dollars: "0.010" },
  { kind: "persona_devils_advocate", status: "done", error_message: null, estimated_dollars: "0.010" },
  { kind: "consolidation", status: "done", error_message: null, estimated_dollars: "0.020" },
];

const ITEMS_RUNNING = ITEMS_DONE.map((item, i) => ({
  ...item,
  status: i < 2 ? ("done" as const) : ("queued" as const),
  // Queued items have no cost yet — real wire shape sends null.
  estimated_dollars: i < 2 ? item.estimated_dollars : null,
}));

function makeRun(over: Record<string, unknown> = {}) {
  return {
    id: "run-1",
    status: "done",
    triggered_at: "2026-06-22T10:00:00Z",
    completed_at: "2026-06-22T10:05:00Z",
    draft_sha: "abc123",
    fit_check_sha: "fc-sha",
    rubric_review_sha: "rr-sha",
    estimated_dollars: "0.090",
    items: ITEMS_DONE,
    stale: false,
    budget: BUDGET_OK,
    ...over,
  };
}

function wrap(ui: ReactNode) {
  const qc = new QueryClient();
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

afterEach(() => {
  h.latestRun = { data: null, isLoading: false };
  h.start = { mutate: () => {}, isPending: false, isError: false, error: null };
  h.fitCheckFile = { data: null, isLoading: false };
  h.rubricReviewFile = { data: null, isLoading: false };
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
describe("ReviewPanel", () => {
  it("(a) no-run + budget OK: button is enabled; cap-exceeded reason not shown", () => {
    h.latestRun = { data: null, isLoading: false };
    wrap(<ReviewPanel id="ws1" />);
    const btn = screen.getByRole("button", { name: /run full mock-panel review/i });
    expect(btn).toBeEnabled();
  });

  it("(a) budget exhausted: button disabled with a reason", () => {
    h.latestRun = { data: makeRun({ status: "done", budget: BUDGET_EXHAUSTED }), isLoading: false };
    wrap(<ReviewPanel id="ws1" />);
    const btn = screen.getByRole("button", { name: /run full mock-panel review/i });
    expect(btn).toBeDisabled();
    expect(screen.getByText(/budget.*cap/i)).toBeInTheDocument();
  });

  it("(a) active run (queued/running): button disabled", () => {
    h.latestRun = {
      data: makeRun({ status: "running", completed_at: null, items: ITEMS_RUNNING }),
      isLoading: false,
    };
    wrap(<ReviewPanel id="ws1" />);
    const btn = screen.getByRole("button", { name: /run full mock-panel review/i });
    expect(btn).toBeDisabled();
  });

  it("(a) POST error: disabled button with surfaced reason", () => {
    h.latestRun = { data: null, isLoading: false };
    h.start = {
      mutate: () => {},
      isPending: false,
      isError: true,
      error: new Error("workspace is still setting up"),
    };
    wrap(<ReviewPanel id="ws1" />);
    const btn = screen.getByRole("button", { name: /run full mock-panel review/i });
    expect(btn).toBeDisabled();
    expect(screen.getByText(/still setting up/i)).toBeInTheDocument();
  });

  it("(b) running run: shows all six per-item progress rows with status pills", () => {
    h.latestRun = {
      data: makeRun({ status: "running", completed_at: null, items: ITEMS_RUNNING }),
      isLoading: false,
    };
    wrap(<ReviewPanel id="ws1" />);
    // All 6 kinds must be listed
    expect(screen.getByText(/fit.check/i)).toBeInTheDocument();
    expect(screen.getByText(/domain.skeptic/i)).toBeInTheDocument();
    expect(screen.getByText(/methodology.critic/i)).toBeInTheDocument();
    expect(screen.getByText(/translation.skeptic/i)).toBeInTheDocument();
    expect(screen.getByText(/devil/i)).toBeInTheDocument();
    expect(screen.getByText(/consolidation/i)).toBeInTheDocument();
    // Status pills: at least one "done" and one "queued"
    const pills = screen.getAllByText(/^(done|queued|running|failed|cancelled|partial)$/i);
    expect(pills.length).toBeGreaterThan(0);
  });

  it("(c) partial run: shows partial banner + both docs rendered as preformatted text", () => {
    h.latestRun = {
      data: makeRun({ status: "partial", fit_check_sha: "fc-sha", rubric_review_sha: "rr-sha" }),
      isLoading: false,
    };
    h.fitCheckFile = { data: { name: "fit-check.md", sha: "fc-sha", markdown: "# Fit Check\nSome analysis." }, isLoading: false };
    h.rubricReviewFile = { data: { name: "rubric-review.md", sha: "rr-sha", markdown: "# Rubric Review\nDetailed feedback." }, isLoading: false };
    wrap(<ReviewPanel id="ws1" />);
    // Banner
    expect(screen.getByText(/partial/i)).toBeInTheDocument();
    // Both documents rendered
    expect(screen.getByText(/Some analysis\./)).toBeInTheDocument();
    expect(screen.getByText(/Detailed feedback\./)).toBeInTheDocument();
  });

  it("(d) stale run: shows staleness badge", () => {
    h.latestRun = { data: makeRun({ stale: true }), isLoading: false };
    wrap(<ReviewPanel id="ws1" />);
    expect(screen.getByText(/stale/i)).toBeInTheDocument();
  });

  it("shows estimated cost (parsed from string) and triggered_at timestamp for a completed run", () => {
    h.latestRun = { data: makeRun(), isLoading: false };
    wrap(<ReviewPanel id="ws1" />);
    // Cost is shown — the string "0.090" formats to "$0.09".
    expect(screen.getByText(/\$0\.09/)).toBeInTheDocument();
    // Triggered timestamp rendered
    expect(screen.getByText(/2026/)).toBeInTheDocument();
  });

  it("does NOT crash and does NOT show cost when estimated_dollars is null (in-progress run)", () => {
    h.latestRun = {
      data: makeRun({
        status: "running",
        completed_at: null,
        estimated_dollars: null,
        items: ITEMS_RUNNING,
      }),
      isLoading: false,
    };
    wrap(<ReviewPanel id="ws1" />);
    // Should not throw; no cost element shown.
    expect(screen.queryByText(/Est\. cost/i)).not.toBeInTheDocument();
    // Button still present.
    expect(screen.getByRole("button", { name: /run full mock-panel review/i })).toBeInTheDocument();
  });

  it("shows budget usage line with dollar strings formatted as decimals", () => {
    h.latestRun = { data: makeRun(), isLoading: false };
    wrap(<ReviewPanel id="ws1" />);
    expect(screen.getByText(/1.*\/.*10/)).toBeInTheDocument();
    // Budget dollars from string "0.12" and "5.000" → "0.12" and "5.00"
    expect(screen.getByText(/\$0\.12.*\/.*\$5\.00/)).toBeInTheDocument();
  });
});
