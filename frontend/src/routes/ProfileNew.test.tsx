import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { theme } from "../theme/theme";
import { ProfileNew } from "./ProfileNew";

afterEach(() => { vi.restoreAllMocks(); });

function Probe() { return <span data-testid="loc">{useLocation().pathname}</span>; }

function renderNew() {
  const qc = new QueryClient();
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/profiles/new"]}>
          <Routes>
            <Route path="/profiles/new" element={<ProfileNew />} />
            <Route path="/profiles/:id/edit" element={<Probe />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

it("toggles individual fields when kind changes", () => {
  renderNew();
  expect(screen.getByLabelText(/organization type/i)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText(/^kind/i), { target: { value: "individual" } });
  expect(screen.getByLabelText(/career stage/i)).toBeInTheDocument();
  expect(screen.queryByLabelText(/organization type/i)).toBeNull();
});

it("submits and navigates to the new profile's edit page", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 42 }, error: undefined } as never);
  renderNew();
  fireEvent.change(screen.getByLabelText(/profile name/i), { target: { value: "New Lab" } });
  fireEvent.click(screen.getByRole("button", { name: /bootstrap profile/i }));
  await waitFor(() => expect(post).toHaveBeenCalledWith("/api/profiles", expect.objectContaining({ body: expect.objectContaining({ name: "New Lab", kind: "organizational_unit" }) })));
  await waitFor(() => expect(screen.getByTestId("loc")).toHaveTextContent("/profiles/42/edit"));
});
