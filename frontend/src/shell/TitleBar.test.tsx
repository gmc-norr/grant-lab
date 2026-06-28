import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { TitleBar } from "./TitleBar";

afterEach(() => { vi.restoreAllMocks(); });

function renderAt(path: string) {
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <TitleBar />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const ME = { data: { user: { id: 1, email: "p@ki.se", status: "active", default_profile_id: null }, capabilities: { admin: false } }, error: undefined };
const META = (title: string) => ({ data: { workspace_title: title, workspace_deadline: null, current_phase: "active", role: "owner", spec_locked: false, member_count: 1 }, error: undefined });

it("shows the workspace title on the cockpit", async () => {
  vi.spyOn(clientMod.api, "GET").mockImplementation((path: string) =>
    Promise.resolve((path === "/api/me" ? ME : META("AMR surveillance grant")) as never),
  );
  renderAt("/cockpit/ws-1");
  expect(await screen.findByText("AMR surveillance grant")).toBeInTheDocument();
});

it("shows 'Funding calls' off the cockpit and does not fetch workspace meta", () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue(ME as never);
  renderAt("/");
  expect(screen.getByText("Funding calls")).toBeInTheDocument();
  // /api/me is allowed (the session chip); the cockpit-meta endpoint is NOT hit.
  for (const call of get.mock.calls)
    expect(call[0]).not.toBe("/api/workspaces/{workspace_id}/cockpit-meta");
});

it("shows 'Workspaces' on the cockpit list and does not fetch workspace meta", () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue(ME as never);
  renderAt("/cockpit");
  expect(screen.getByText("Workspaces")).toBeInTheDocument();
  for (const call of get.mock.calls)
    expect(call[0]).not.toBe("/api/workspaces/{workspace_id}/cockpit-meta");
});

it("shows 'New workspace' on the create route and does not fetch workspace meta", () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue(ME as never);
  renderAt("/cockpit/new");
  expect(screen.getByText("New workspace")).toBeInTheDocument();
  for (const call of get.mock.calls)
    expect(call[0]).not.toBe("/api/workspaces/{workspace_id}/cockpit-meta");
});

it("shows the signed-in email chip and a working sign-out", async () => {
  const assign = vi.fn();
  vi.stubGlobal("location", { assign } as never);
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(ME as never);
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { ok: true }, error: undefined } as never);
  renderAt("/");
  expect(await screen.findByText("p@ki.se")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /sign out/i }));
  await waitFor(() => expect(post).toHaveBeenCalledWith("/api/auth/logout", {}));
  await waitFor(() => expect(assign).toHaveBeenCalledWith("/login"));
  vi.unstubAllGlobals();
});

function renderWithProps(path: string, props: {
  hasRightPane?: boolean;
  chatCollapsed?: boolean;
  onToggleChat?: () => void;
}) {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(ME as never);
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <TitleBar {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

it("shows a chat-toggle on cockpit routes and calls onToggleChat when clicked", async () => {
  const onToggleChat = vi.fn();
  renderWithProps("/cockpit/ws-1", { hasRightPane: true, chatCollapsed: false, onToggleChat });
  const btn = await screen.findByRole("button", { name: "Toggle chat panel" });
  expect(btn).toHaveAttribute("aria-pressed", "true"); // expanded → pressed
  await userEvent.click(btn);
  expect(onToggleChat).toHaveBeenCalledTimes(1);
});

it("reflects collapsed state on the chat-toggle (aria-pressed=false when collapsed)", async () => {
  renderWithProps("/cockpit/ws-1", { hasRightPane: true, chatCollapsed: true, onToggleChat: vi.fn() });
  const btn = await screen.findByRole("button", { name: "Toggle chat panel" });
  expect(btn).toHaveAttribute("aria-pressed", "false");
});

it("omits the chat-toggle when the route has no right pane", () => {
  renderWithProps("/", { hasRightPane: false, onToggleChat: vi.fn() });
  expect(screen.queryByRole("button", { name: "Toggle chat panel" })).toBeNull();
});
