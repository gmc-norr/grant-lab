import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { theme } from "../theme/theme";
import { ActivityPanel } from "./ActivityPanel";

const h = vi.hoisted((): { activity: { data: unknown; isError?: boolean; isLoading?: boolean } } => ({
  activity: { data: undefined },
}));

vi.mock("../api/activity", () => ({ useActivity: () => h.activity }));

function wrap(ui: ReactNode) {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>
    </MantineProvider>,
  );
}

afterEach(() => {
  h.activity = { data: undefined };
});

describe("ActivityPanel", () => {
  it("shows an empty state when there is no activity", () => {
    h.activity = { data: { items: [] } };
    wrap(<ActivityPanel id="ws1" onNavigate={vi.fn()} />);
    expect(screen.getByText(/no activity yet/i)).toBeInTheDocument();
  });

  it("renders rows with actor, summary, relative time and routes Go-to", () => {
    const twoHoursAgo = new Date(Date.now() - 2 * 3600 * 1000).toISOString();
    h.activity = {
      data: {
        items: [
          {
            id: "e1",
            category: "review",
            actor: "alice@umu.se",
            summary: "started a panel review",
            count: 1,
            created_at: twoHoursAgo,
            target: { top_tab: "review", bottom_tab: null },
          },
          {
            id: "e2",
            category: "created",
            actor: null,
            summary: "created the workspace",
            count: 1,
            created_at: twoHoursAgo,
            target: null,
          },
        ],
      },
    };
    const onNavigate = vi.fn();
    wrap(<ActivityPanel id="ws1" onNavigate={onNavigate} />);
    expect(screen.getByText("alice@umu.se")).toBeInTheDocument();
    expect(screen.getByText("started a panel review")).toBeInTheDocument();
    expect(screen.getByText("System")).toBeInTheDocument(); // null actor
    expect(screen.getAllByText(/2h ago/).length).toBeGreaterThan(0);
    // Only the row with a target has a Go-to button.
    const gotos = screen.getAllByRole("button", { name: /go to/i });
    expect(gotos).toHaveLength(1);
    fireEvent.click(gotos[0]);
    expect(onNavigate).toHaveBeenCalledWith({ top_tab: "review", bottom_tab: undefined });
  });

  it("surfaces a fetch error inline", () => {
    h.activity = { data: undefined, isError: true };
    wrap(<ActivityPanel id="ws1" onNavigate={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent(/could not load activity/i);
  });

  it("renders a member-category row", () => {
    h.activity = {
      data: {
        items: [
          { id: "e1", category: "member", actor: "owner@umu.se", summary: "added collaborator a@umu.se", count: 1, created_at: new Date().toISOString(), target: null },
        ],
      },
    };
    wrap(<ActivityPanel id="ws1" onNavigate={vi.fn()} />);
    expect(screen.getByText(/added collaborator a@umu\.se/i)).toBeInTheDocument();
    expect(document.querySelector('[data-category="member"]')).not.toBeNull();
  });
});
