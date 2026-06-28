import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { theme } from "../theme/theme";
import { InboxPanel } from "./InboxPanel";

const h = vi.hoisted(() => ({
  draft: { data: { sections: [{ index: 1, heading: "M", text: "## M\nold\n", sha: "s" }] } },
  spec: { data: { sections: [{ index: 0, heading: "", text: "", sha: "s" }], locked: false } },
}));

vi.mock("../api/editor", () => ({
  useDraft: () => h.draft,
  useSpec: () => h.spec,
}));
// ProposalReview is reused as-is; stub it to a recognizable marker per artifact.
vi.mock("./ProposalReview", () => ({
  ProposalReview: ({ artifact }: { artifact: string }) => <div>review:{artifact}</div>,
}));

function wrap(ui: ReactNode) {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>
    </MantineProvider>,
  );
}

describe("InboxPanel", () => {
  it("shows an empty message when count is 0", () => {
    wrap(<InboxPanel id="ws1" count={0} />);
    expect(screen.getByText(/nothing pending/i)).toBeInTheDocument();
  });

  it("renders both artifact groups when count > 0", () => {
    wrap(<InboxPanel id="ws1" count={2} />);
    expect(screen.getByText("review:draft")).toBeInTheDocument();
    expect(screen.getByText("review:spec")).toBeInTheDocument();
  });
});
