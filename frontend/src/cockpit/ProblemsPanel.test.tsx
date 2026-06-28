import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { theme } from "../theme/theme";

import { ProblemsPanel } from "./ProblemsPanel";

const h = vi.hoisted(
  (): { problems: { data: unknown; isError?: boolean }; dismiss: { mutate: ReturnType<typeof vi.fn>; isPending: boolean } } => ({
    problems: { data: undefined },
    dismiss: { mutate: vi.fn(), isPending: false },
  }),
);

vi.mock("../api/problems", () => ({ useProblems: () => h.problems }));

vi.mock("../api/uncitedClaims", () => ({
  useLatestUncitedClaimsRun: () => ({ data: undefined }),
  useStartUncitedClaimsRun: () => ({ mutate: vi.fn(), isPending: false }),
  useDismissClaim: () => h.dismiss,
}));

function wrap(ui: ReactNode) {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>
    </MantineProvider>,
  );
}

afterEach(() => {
  h.problems = { data: undefined };
  h.dismiss = { mutate: vi.fn(), isPending: false };
});

describe("ProblemsPanel", () => {
  it("shows a calm empty state when there are no problems", () => {
    h.problems = { data: { problems: [], counts: { error: 0, warning: 0 }, scorecard_stale: [], uncited_claims_stale: false } };
    wrap(<ProblemsPanel id="ws1" onNavigate={vi.fn()} />);
    expect(screen.getByText(/no problems detected/i)).toBeInTheDocument();
  });

  it("renders rows and routes 'act' to the row target", () => {
    h.problems = {
      data: {
        problems: [
          { category: "criterion_gap", severity: "error", title: "Criterion gap: Impact",
            detail: "weak", target: { top_tab: "scorecard", bottom_tab: null, section_index: null } },
          { category: "coherence_drift", severity: "warning", title: "Coherence: 2 pending",
            detail: "Open the Inbox.", target: { top_tab: null, bottom_tab: "inbox", section_index: null } },
        ],
        counts: { error: 1, warning: 1 },
        scorecard_stale: ["draft"],
        uncited_claims_stale: false,
      },
    };
    const onNavigate = vi.fn();
    wrap(<ProblemsPanel id="ws1" onNavigate={onNavigate} />);
    expect(screen.getByText("Criterion gap: Impact")).toBeInTheDocument();
    expect(screen.getByText(/may be outdated/i)).toBeInTheDocument(); // stale badge
    fireEvent.click(screen.getByRole("button", { name: /go to criterion gap: impact/i }));
    expect(onNavigate).toHaveBeenCalledWith({ top_tab: "scorecard" });
    fireEvent.click(screen.getByRole("button", { name: /go to coherence: 2 pending/i }));
    expect(onNavigate).toHaveBeenCalledWith({ bottom_tab: "inbox" });
  });

  it("surfaces a fetch error inline", () => {
    h.problems = { data: undefined, isError: true };
    wrap(<ProblemsPanel id="ws1" onNavigate={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent(/could not load problems/i);
  });

  it("renders uncited rows with a dismiss action and a run button", () => {
    h.problems = {
      data: {
        problems: [
          {
            category: "uncited_claim",
            severity: "warning",
            title: "30% relapse",
            detail: "statistic",
            target: { top_tab: "draft", bottom_tab: "problems", section_index: 1 },
            claim_hash: "h1",
          },
        ],
        counts: { error: 0, warning: 1 },
        scorecard_stale: [],
        uncited_claims_stale: true,
      },
    };
    wrap(<ProblemsPanel id="ws1" onNavigate={vi.fn()} />);
    expect(screen.getByRole("button", { name: /check uncited claims/i })).toBeInTheDocument();
    expect(screen.getByText(/may be outdated/i)).toBeInTheDocument(); // uncited stale badge
    fireEvent.click(screen.getByRole("button", { name: /^dismiss$/i }));
    expect(h.dismiss.mutate).toHaveBeenCalledWith("h1");
  });
});
