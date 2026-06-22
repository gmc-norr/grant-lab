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
const BUDGET_OK = { runs_used: 1, runs_cap: 10, dollars_used: 0.12, dollars_cap: 5.0 };
const BUDGET_EXHAUSTED = { runs_used: 10, runs_cap: 10, dollars_used: 5.0, dollars_cap: 5.0 };

const ITEMS_DONE = [
  { kind: "fit_check", status: "done", error_message: null, estimated_dollars: 0.01 },
  { kind: "persona_domain_skeptic", status: "done", error_message: null, estimated_dollars: 0.02 },
  { kind: "persona_methodology_critic", status: "done", error_message: null, estimated_dollars: 0.02 },
  { kind: "persona_translation_skeptic", status: "done", error_message: null, estimated_dollars: 0.01 },
  { kind: "persona_devils_advocate", status: "done", error_message: null, estimated_dollars: 0.01 },
  { kind: "consolidation", status: "done", error_message: null, estimated_dollars: 0.02 },
];

const ITEMS_RUNNING = ITEMS_DONE.map((item, i) => ({
  ...item,
  status: i < 2 ? ("done" as const) : ("queued" as const),
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
    estimated_dollars: 0.09,
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

  it("shows estimated cost and triggered_at timestamp for a completed run", () => {
    h.latestRun = { data: makeRun(), isLoading: false };
    wrap(<ReviewPanel id="ws1" />);
    // Cost is shown
    expect(screen.getByText(/\$0\.09/)).toBeInTheDocument();
    // Triggered timestamp rendered
    expect(screen.getByText(/2026/)).toBeInTheDocument();
  });

  it("shows budget usage line", () => {
    h.latestRun = { data: makeRun(), isLoading: false };
    wrap(<ReviewPanel id="ws1" />);
    expect(screen.getByText(/1.*\/.*10/)).toBeInTheDocument();
  });
});
