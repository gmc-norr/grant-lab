import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import * as clientMod from "./client";
import { useCockpitMeta } from "./workspace";

function Probe({ id }: { id: string }) {
  const q = useCockpitMeta(id);
  return <span data-testid="title">{q.data?.workspace_title ?? "—"}</span>;
}
function renderWith(id: string) {
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <Probe id={id} />
    </QueryClientProvider>,
  );
}

it("useCockpitMeta does NOT fetch when id is empty (off-cockpit gating)", () => {
  const get = vi.spyOn(clientMod.api, "GET");
  renderWith("");
  expect(get).not.toHaveBeenCalled();
});

it("useCockpitMeta fetches and returns the header when id is present", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { workspace_title: "My WS", workspace_deadline: null, current_phase: "active", role: "owner", spec_locked: false, member_count: 2 },
    error: undefined,
  } as never);
  const { getByTestId } = renderWith("ws-1");
  await waitFor(() => expect(getByTestId("title").textContent).toBe("My WS"));
});
