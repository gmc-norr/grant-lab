/**
 * Review 2026-09-02 A7/F3: `Cockpit` had no `isError` arm, so a workspace that
 * was soft-deleted by admin retention, or one the member had been removed from,
 * answered 404 and rendered "Loading…" forever — no alert, no explanation, and
 * no way out but the rail. `useCockpitMeta` also retried the 404 three times
 * first, so the dead spinner took several seconds to even settle.
 */
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, it, vi, afterEach } from "vitest";

vi.mock("../api/annotations", () => ({ useAnnotations: () => ({ data: undefined }) }));

import * as workspaceMod from "../api/workspace";
import { theme } from "../theme/theme";
import { Cockpit } from "./Cockpit";

afterEach(() => {
  vi.restoreAllMocks();
});

function renderAt() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/cockpit/ws-gone"]}>
          <Routes>
            <Route path="/cockpit/:id" element={<Cockpit />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

it("tells the user the workspace is unavailable instead of spinning forever", async () => {
  vi.spyOn(workspaceMod, "useCockpitMeta").mockReturnValue({
    isLoading: false,
    isError: true,
    data: undefined,
  } as never);

  renderAt();

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(/not found|no longer shared/i);
  // The precise regression: the dead spinner must be gone, not merely joined.
  expect(screen.queryByText("Loading…")).toBeNull();
});

it("still shows Loading… while the request is genuinely in flight", async () => {
  vi.spyOn(workspaceMod, "useCockpitMeta").mockReturnValue({
    isLoading: true,
    isError: false,
    data: undefined,
  } as never);

  renderAt();

  expect(screen.getByText("Loading…")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("does not retry a 404: the error arm is reached on the first failure", async () => {
  // retry:false is load-bearing, not tidiness — three retries is several seconds
  // of the very spinner this finding is about. Counting the HTTP calls is the
  // only way to see it: the rendered output is identical either way.
  const client = await import("../api/client");
  const get = vi
    .spyOn(client.api, "GET")
    .mockResolvedValue({ error: { error: { code: "not_found", message: "gone" } } } as never);

  function Probe() {
    const q = workspaceMod.useCockpitMeta("ws-gone");
    return <span data-testid="s">{q.isError ? "error" : "pending"}</span>;
  }

  render(
    <QueryClientProvider client={new QueryClient()}>
      <Probe />
    </QueryClientProvider>,
  );

  await waitFor(() => expect(screen.getByTestId("s")).toHaveTextContent("error"));
  expect(get).toHaveBeenCalledTimes(1);
});
