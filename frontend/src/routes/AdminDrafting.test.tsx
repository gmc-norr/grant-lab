import { screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { AdminDrafting } from "./AdminDrafting";

afterEach(() => {
  vi.restoreAllMocks();
});

const DRAFT = {
  drafting_enabled: true,
  global_cap: { allowed: false, used_in: 1500, used_out: 0, cap_in: 1000, cap_out: 500, reason: "input_cap_exceeded" },
  per_workspace: [
    { id: "11111111-1111-1111-1111-111111111111", title: "Big", owner_email: "o@ki.se", used_in: 1500, used_out: 0 },
  ],
  recent_events: [
    { created_at: "2026-06-01T00:00:00Z", workspace_id: "11111111-1111-1111-1111-111111111111", kind: "cloud_turn_failed", payload: { error: "boom_xyz" } },
  ],
};

function mockDraft(data: unknown = DRAFT) {
  return vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data, error: undefined } as never);
}

it("renders kill switch, cap, per-workspace and events", async () => {
  mockDraft();
  renderWithProviders(<AdminDrafting />);
  expect(await screen.findByText(/drafting enabled/i)).toBeInTheDocument();
  // "EXCEEDED" appears twice (input-row cell + overall-status line) — a /EXCEEDED/
  // regex would double-match and throw. Assert each specifically.
  expect(screen.getByText("EXCEEDED")).toBeInTheDocument(); // input-row cell (exact)
  expect(screen.getByText(/Overall status: EXCEEDED/)).toBeInTheDocument();
  const link = screen.getByRole("link", { name: "Big" });
  expect(link).toHaveAttribute("href", "/cockpit/11111111-1111-1111-1111-111111111111");
  expect(screen.getByText(/boom_xyz/)).toBeInTheDocument();
});

it("shows kill switch off", async () => {
  mockDraft({ ...DRAFT, drafting_enabled: false });
  renderWithProviders(<AdminDrafting />);
  expect(await screen.findByText(/drafting disabled/i)).toBeInTheDocument();
});

it("renders a clean error state on failure", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: undefined, error: { error: { code: "internal_error", message: "x" } }, response: { status: 500 },
  } as never);
  renderWithProviders(<AdminDrafting />);
  expect(await screen.findByRole("alert")).toHaveTextContent(/couldn't load/i);
});
