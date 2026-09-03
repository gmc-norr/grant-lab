import { screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { RequireAdmin } from "./RequireAdmin";

afterEach(() => {
  vi.restoreAllMocks();
});

function mockMe(admin: boolean) {
  return vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { user: { id: 1, email: "a@umu.se", status: "active", default_profile_id: null }, capabilities: { admin } },
    error: undefined,
  } as never);
}

it("renders children for an admin", async () => {
  mockMe(true);
  renderWithProviders(
    <RequireAdmin>
      <div>secret panel</div>
    </RequireAdmin>,
  );
  expect(await screen.findByText("secret panel")).toBeInTheDocument();
});

it("blocks a non-admin", async () => {
  mockMe(false);
  renderWithProviders(
    <RequireAdmin>
      <div>secret panel</div>
    </RequireAdmin>,
  );
  expect(await screen.findByText(/not authorized/i)).toBeInTheDocument();
  expect(screen.queryByText("secret panel")).not.toBeInTheDocument();
});
