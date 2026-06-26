import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { AdminPendingUsers } from "./AdminPendingUsers";

afterEach(() => {
  vi.restoreAllMocks();
});

function mockPending(users: unknown[]) {
  return vi
    .spyOn(clientMod.api, "GET")
    .mockResolvedValue({ data: { users, page: 1 }, error: undefined } as never);
}

it("shows an initial-password input for local accounts, not for Google", async () => {
  mockPending([
    { id: 1, email: "local@ki.se", name: null, created_at: "2026-06-01T00:00:00Z", google_linked: false },
    { id: 2, email: "goog@ki.se", name: "G", created_at: "2026-06-01T00:00:00Z", google_linked: true },
  ]);
  renderWithProviders(<AdminPendingUsers />);
  expect(await screen.findByText("local@ki.se")).toBeInTheDocument();
  // one password input total (only the local account has one)
  expect(screen.getAllByPlaceholderText(/initial password/i)).toHaveLength(1);
  expect(screen.getByText(/google/i)).toBeInTheDocument();
});

it("approve-as-admin posts role=admin with the typed password", async () => {
  mockPending([
    { id: 1, email: "local@ki.se", name: null, created_at: "2026-06-01T00:00:00Z", google_linked: false },
  ]);
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 1 }, error: undefined } as never);
  renderWithProviders(<AdminPendingUsers />);
  await userEvent.type(await screen.findByPlaceholderText(/initial password/i), "issued-strong-pw");
  await userEvent.click(screen.getByRole("button", { name: /approve as admin/i }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith("/api/admin/users/{user_id}/approve", {
      params: { path: { user_id: 1 } },
      body: { role: "admin", initial_password: "issued-strong-pw" },
    }),
  );
});

it("renders an empty state", async () => {
  mockPending([]);
  renderWithProviders(<AdminPendingUsers />);
  expect(await screen.findByText(/no accounts awaiting approval/i)).toBeInTheDocument();
});
