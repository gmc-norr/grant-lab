import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { Dashboard } from "./Dashboard";

function renderAt(url: string) {
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>
        <Dashboard />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

it("deep link does NOT POST default; selecting a profile DOES", async () => {
  const GET = vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: {
      items: [],
      summary: { open_count: 0, high_score: 0, soon_deadline: 0 },
      pagination: { page: 1, limit: 50, has_prev: false, has_next: false, total: 0 },
      ranking: { pending: 0, failed: 0 },
      effective: {},
      facets: { sources: [], profiles: [{ id: 7, name: "G" }] },
    },
    error: undefined,
    response: new Response(),
  } as never);
  const POST = vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: { ok: true },
    error: undefined,
    response: new Response(),
  } as never);

  renderAt("/?profile=7"); // deep link
  await waitFor(() => expect(GET).toHaveBeenCalled());
  expect(POST).not.toHaveBeenCalled(); // read-only

  fireEvent.change(await screen.findByLabelText(/profile/i), { target: { value: "7" } });
  await waitFor(() =>
    expect(POST).toHaveBeenCalledWith("/api/me/default-profile", { body: { profile_id: 7 } }),
  );
});
