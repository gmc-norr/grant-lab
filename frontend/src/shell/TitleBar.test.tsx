import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
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

it("shows the workspace title on the cockpit", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { workspace_title: "AMR surveillance grant", workspace_deadline: null, current_phase: "active", role: "owner", spec_locked: false, member_count: 1 },
    error: undefined,
  } as never);
  renderAt("/cockpit/ws-1");
  expect(await screen.findByText("AMR surveillance grant")).toBeInTheDocument();
});

it("shows 'Funding calls' off the cockpit and does not fetch", () => {
  const get = vi.spyOn(clientMod.api, "GET");
  renderAt("/");
  expect(screen.getByText("Funding calls")).toBeInTheDocument();
  expect(get).not.toHaveBeenCalled();
});

it("shows 'Workspaces' on the cockpit list and does not fetch", () => {
  const get = vi.spyOn(clientMod.api, "GET");
  renderAt("/cockpit");
  expect(screen.getByText("Workspaces")).toBeInTheDocument();
  expect(get).not.toHaveBeenCalled();
});

it("shows 'New workspace' on the create route and does not fetch", () => {
  const get = vi.spyOn(clientMod.api, "GET");
  renderAt("/cockpit/new");
  expect(screen.getByText("New workspace")).toBeInTheDocument();
  expect(get).not.toHaveBeenCalled();
});
