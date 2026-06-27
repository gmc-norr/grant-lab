import { screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { DashboardHeader } from "./DashboardHeader";

function dashData() {
  return {
    data: {
      items: [],
      summary: { open_count: 12, high_score: 5, soon_deadline: 3 },
      pagination: { page: 1, limit: 50, has_prev: false, has_next: false, total: 0 },
      ranking: { pending: 0, failed: 0 },
      effective: { source: null, sort: "newest", profile_id: null, show_closed: false },
      facets: { sources: [], profiles: [] },
    },
    error: undefined,
    response: new Response(),
  };
}

it("shows the breadcrumb and the summary counts verbatim", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData() as never);
  renderWithProviders(<DashboardHeader />, { route: "/" });
  expect(screen.getByText("Calls")).toBeInTheDocument();
  await waitFor(() => expect(screen.getByText("12")).toBeInTheDocument());
  expect(screen.getByText("5")).toBeInTheDocument();
  expect(screen.getByText("3")).toBeInTheDocument();
});
