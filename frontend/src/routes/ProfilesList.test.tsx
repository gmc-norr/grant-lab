import { screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { ProfilesList } from "./ProfilesList";

afterEach(() => {
  vi.restoreAllMocks();
});

function mock(profiles: unknown[]) {
  return vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: { profiles }, error: undefined } as never);
}

it("renders profile cards with a detail link", async () => {
  mock([{ id: 1, name: "Lab A", kind: "organizational_unit", level: "group", areas_count: 2, budget_currency: "SEK", description: "Imaging." }]);
  renderWithProviders(<ProfilesList />);
  const link = await screen.findByRole("link", { name: "Lab A" });
  expect(link).toHaveAttribute("href", "/profiles/1");
  expect(screen.getByText(/2 areas/)).toBeInTheDocument();
});

it("renders an empty state with a create link", async () => {
  mock([]);
  renderWithProviders(<ProfilesList />);
  expect(await screen.findByRole("link", { name: /create your first profile/i })).toHaveAttribute("href", "/profiles/new");
});
