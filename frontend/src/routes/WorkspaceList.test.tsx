import { screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { WorkspaceList } from "./WorkspaceList";

function wsItem(over: Record<string, unknown> = {}) {
  return {
    id: "ws-1",
    title: "AMR grant",
    status: "drafting",
    current_phase: "active",
    deadline: null,
    updated_at: "2026-06-24T00:00:00Z",
    linked_call_title: "Vinnova call",
    is_owner: true,
    ...over,
  };
}

it("an active workspace links to the React cockpit", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: [wsItem()], error: undefined } as never);
  renderWithProviders(<WorkspaceList />, { route: "/cockpit" });
  const link = await screen.findByRole("link", { name: "AMR grant" });
  expect(link).toHaveAttribute("href", "/cockpit/ws-1");
});

it("a setup workspace links to the React cockpit and is marked 'Setting up'", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: [wsItem({ id: "ws-setup", title: "Setting-up workspace title", current_phase: "setup", is_owner: false, linked_call_title: null })],
    error: undefined,
  } as never);
  renderWithProviders(<WorkspaceList />, { route: "/cockpit" });
  const link = await screen.findByRole("link", { name: /setting-up workspace title/i });
  expect(link).toHaveAttribute("href", "/cockpit/ws-setup");
  expect(screen.getByText("Setting up")).toBeInTheDocument();
  expect(screen.getByText("Shared")).toBeInTheDocument(); // viewer-of badge
});

it("shows the lifecycle status badge and a last-activity date", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: [wsItem({ status: "submitted", updated_at: "2026-06-20T09:00:00Z" })],
    error: undefined,
  } as never);
  renderWithProviders(<WorkspaceList />, { route: "/cockpit" });
  expect(await screen.findByText("Submitted")).toBeInTheDocument();
  expect(screen.getByText(/Updated 2026-06-20/)).toBeInTheDocument();
});

it("shows an empty state that points back to the dashboard", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: [], error: undefined } as never);
  renderWithProviders(<WorkspaceList />, { route: "/cockpit" });
  expect(await screen.findByText(/pick a call to start drafting/i)).toBeInTheDocument();
});

it("reads the ?status filter from the URL into the query", async () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: [], error: undefined } as never);
  renderWithProviders(<WorkspaceList />, { route: "/cockpit?status=submitted" });
  await waitFor(() =>
    expect(get).toHaveBeenCalledWith("/api/workspaces", { params: { query: { status: "submitted" } } }),
  );
});
