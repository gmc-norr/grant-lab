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
  mock([{ id: 1, name: "Lab A", kind: "organizational_unit", level: "group", areas_count: 2, budget_currency: "SEK", description: "Imaging.", is_owner: true, can_edit: true }]);
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

it("shows a profile's kind as words, not as the enum name", async () => {
  // UX pass 2026-10-31: the card read "organizational_unit · 4 areas · SEK".
  mock([{ id: 1, name: "Lab A", kind: "organizational_unit", level: "group", areas_count: 2, budget_currency: "SEK", description: "", is_owner: true, can_edit: true }]);
  renderWithProviders(<ProfilesList />);
  expect(await screen.findByText(/^Organizational unit · 2 areas · SEK$/)).toBeInTheDocument();
  expect(screen.queryByText(/organizational_unit/)).toBeNull();
});

it("labels each profile Yours or Shared with you, and offers Edit only where it works", async () => {
  // UX review 2026-10-31: every card offered Edit, and a reader of a shared
  // profile landed on a disabled form with no explanation.
  mock([
    { id: 1, name: "Mine", kind: "individual", level: null, areas_count: 0, budget_currency: null, description: "", is_owner: true, can_edit: true },
    { id: 2, name: "Theirs", kind: "individual", level: null, areas_count: 0, budget_currency: null, description: "", is_owner: false, can_edit: false },
  ]);
  const { container } = renderWithProviders(<ProfilesList />);
  await screen.findByRole("link", { name: "Mine" });
  const cards = [...container.querySelectorAll('[data-field="ownership"]')].map((b) => b.textContent);
  expect(cards).toEqual(["Yours", "Shared with you"]);
  const edits = screen.getAllByRole("link", { name: "Edit" });
  expect(edits).toHaveLength(1);
  expect(edits[0]).toHaveAttribute("href", "/profiles/1/edit");
});
