import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MantineProvider } from "@mantine/core";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { theme } from "../theme/theme";
import { WorkspaceCreate } from "./WorkspaceCreate";

afterEach(() => {
  vi.restoreAllMocks();
});

function LocationProbe() {
  return <span data-testid="loc">{useLocation().pathname}</span>;
}

function renderAt(url: string) {
  const qc = new QueryClient();
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[url]}>
          <Routes>
            <Route path="/cockpit/new" element={<WorkspaceCreate />} />
            <Route path="/cockpit/:id" element={<LocationProbe />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

it("prefills the title from ?call_title and creates, then navigates to the React cockpit", async () => {
  const post = vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue({ data: { id: "new-9" }, error: undefined } as never);
  const user = userEvent.setup();
  renderAt("/cockpit/new?call_id=3&call_title=Grant%20X&profile_id=7");
  const title = screen.getByLabelText("Title") as HTMLInputElement;
  expect(title.value).toBe("Grant X");
  await user.click(screen.getByRole("button", { name: "Start drafting" }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/api/workspaces",
      expect.objectContaining({ body: expect.objectContaining({ call_id: 3, title: "Grant X", profile_id: 7 }) }),
    ),
  );
  await waitFor(() => expect(screen.getByTestId("loc")).toHaveTextContent("/cockpit/new-9"));
});

it("shows a guard (not a form) when call_id is missing", () => {
  const qc = new QueryClient();
  render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/cockpit/new"]}>
          <Routes>
            <Route path="/cockpit/new" element={<WorkspaceCreate />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
  expect(screen.getByText(/pick a call from the dashboard/i)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Start drafting" })).toBeNull();
});
