import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { AdminDashboard } from "./AdminDashboard";

afterEach(() => {
  vi.restoreAllMocks();
});

const DASH = {
  sources: [{ name: "Vinnova", slug: "vinnova", last_fetched_at: null, call_count: 3 }],
  queue_counts: { pending: 2, running: 0, done: 5, failed: 1 },
  rerank_estimate: 6,
  active_profiles_count: 2,
  recent_calls_count: 3,
  backfill_days: 90,
  digest: {
    last_sent_at: null,
    runs_this_week: 0,
    disabled_users: 1,
    disabled_user_rows: [{ id: 9, email: "bounced@ki.se", disabled_at: "2026-06-01T00:00:00Z" }],
  },
  embedding_coverage: {
    call_embedded: 1, call_total: 3, call_stale: 2,
    profile_embedded: 1, profile_total: 2, profile_stale: 1, current_model: "bge-m3",
  },
  golden_set_report: null,
  calibration_warning: null,
};

function mockDash(data: unknown = DASH) {
  return vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data, error: undefined } as never);
}

it("renders source health, queue counts and coverage", async () => {
  mockDash();
  renderWithProviders(<AdminDashboard />);
  expect(await screen.findByText("Vinnova")).toBeInTheDocument();
  expect(screen.getByText("bounced@ki.se")).toBeInTheDocument();
  expect(screen.getByText(/1\/3 embedded/)).toBeInTheDocument();
  expect(screen.getByText(/up to ~6/)).toBeInTheDocument();
});

it("renders a clean error state when the read-model fails", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: undefined, error: { error: { code: "internal_error", message: "boom" } },
    response: { status: 500 },
  } as never);
  renderWithProviders(<AdminDashboard />);
  expect(await screen.findByRole("alert")).toHaveTextContent(/couldn't load/i);
});

it("fetch-now posts the slug and shows the ok banner", async () => {
  mockDash();
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { triggered: true }, error: undefined } as never);
  renderWithProviders(<AdminDashboard />);
  await userEvent.click(await screen.findByRole("button", { name: /fetch now/i }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith("/api/admin/sources/{slug}/fetch", {
      params: { path: { slug: "vinnova" } },
    }),
  );
  expect(await screen.findByText(/fetch triggered/i)).toBeInTheDocument();
});

it("fetch-now shows the err banner when not triggered", async () => {
  mockDash();
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { triggered: false }, error: undefined } as never);
  renderWithProviders(<AdminDashboard />);
  await userEvent.click(await screen.findByRole("button", { name: /fetch now/i }));
  expect(await screen.findByText(/couldn't trigger the fetch/i)).toBeInTheDocument();
});

it("rerank confirms before posting", async () => {
  mockDash();
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { enqueued: 2 }, error: undefined } as never);
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  renderWithProviders(<AdminDashboard />);
  await userEvent.click(await screen.findByRole("button", { name: /re-rank everything/i }));
  expect(confirm).toHaveBeenCalled();
  expect(post).not.toHaveBeenCalled();  // declined -> no POST
});

it("reenable posts the user id", async () => {
  mockDash();
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 9 }, error: undefined } as never);
  renderWithProviders(<AdminDashboard />);
  await userEvent.click(await screen.findByRole("button", { name: /re-enable/i }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith("/api/admin/digest/reenable", { body: { user_id: 9 } }),
  );
});
