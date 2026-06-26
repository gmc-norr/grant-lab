import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { AdminUsers } from "./AdminUsers";

afterEach(() => {
  vi.restoreAllMocks();
});

function mockUsers(users: unknown[], total = users.length) {
  return vi
    .spyOn(clientMod.api, "GET")
    .mockResolvedValue({ data: { users, page: 1, per_page: 50, total }, error: undefined } as never);
}

function mockPost() {
  return vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 1 }, error: undefined } as never);
}

it("renders active and disabled rows with the right actions", async () => {
  mockUsers([
    { id: 1, email: "act@ki.se", name: null, status: "active", is_admin: false },
    { id: 2, email: "dis@ki.se", name: null, status: "disabled", is_admin: false },
  ]);
  renderWithProviders(<AdminUsers />);
  expect(await screen.findByText("act@ki.se")).toBeInTheDocument();
  // active row -> Disable + Make admin + reset-password; disabled row -> Enable
  expect(screen.getByRole("button", { name: /disable/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /make admin/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /reset password/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /enable/i })).toBeInTheDocument();
});

it("clicking Make admin posts role=admin", async () => {
  mockUsers([{ id: 1, email: "act@ki.se", name: null, status: "active", is_admin: false }]);
  const post = mockPost();
  renderWithProviders(<AdminUsers />);
  await userEvent.click(await screen.findByRole("button", { name: /make admin/i }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith("/api/admin/users/{user_id}/role", {
      params: { path: { user_id: 1 } },
      body: { role: "admin" },
    }),
  );
});

it("clicking Disable posts disable", async () => {
  mockUsers([{ id: 3, email: "act@ki.se", name: null, status: "active", is_admin: false }]);
  const post = mockPost();
  renderWithProviders(<AdminUsers />);
  await userEvent.click(await screen.findByRole("button", { name: /disable/i }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith("/api/admin/users/{user_id}/disable", { params: { path: { user_id: 3 } } }),
  );
});

it("clicking Enable on a disabled row posts enable", async () => {
  mockUsers([{ id: 4, email: "dis@ki.se", name: null, status: "disabled", is_admin: false }]);
  const post = mockPost();
  renderWithProviders(<AdminUsers />);
  await userEvent.click(await screen.findByRole("button", { name: /enable/i }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith("/api/admin/users/{user_id}/enable", { params: { path: { user_id: 4 } } }),
  );
});

it("submitting the reset-password form posts the new password", async () => {
  mockUsers([{ id: 5, email: "act@ki.se", name: null, status: "active", is_admin: false }]);
  const post = mockPost();
  renderWithProviders(<AdminUsers />);
  await userEvent.type(await screen.findByPlaceholderText(/new password/i), "fresh-strong-pw");
  await userEvent.click(screen.getByRole("button", { name: /reset password/i }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith("/api/admin/users/{user_id}/reset-password", {
      params: { path: { user_id: 5 } },
      body: { new_password: "fresh-strong-pw" },
    }),
  );
});
