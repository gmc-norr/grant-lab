import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../../api/client";
import { resetEditorStore } from "../editorStore";
import { CockpitChat } from "./CockpitChat";

afterEach(() => {
  vi.restoreAllMocks();
  resetEditorStore();
});

function mockTurns(over: Partial<{ can_send: boolean; needs_disclosure: boolean; phase: string; role: string }>) {
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/turns"))
      return Promise.resolve({ data: { turns: [], can_send: over.can_send ?? true, needs_disclosure: over.needs_disclosure ?? false, phase: over.phase ?? "active", running_job_id: null }, error: undefined });
    if (path.endsWith("/references")) return Promise.resolve({ data: [], error: undefined });
    return Promise.resolve({ data: null, error: undefined });
  }) as never);
}

function renderChat() {
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/cockpit/ws-1"]}>
        <Routes>
          <Route path="/cockpit/:id" element={<CockpitChat />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

it("renders the chat pane header", async () => {
  mockTurns({});
  renderChat();
  expect(await screen.findByRole("complementary", { name: /chat/i })).toBeInTheDocument();
});

it("shows the composer for an owner on an active acked workspace", async () => {
  mockTurns({ can_send: true, needs_disclosure: false });
  renderChat();
  expect(await screen.findByPlaceholderText(/message the agent/i)).toBeInTheDocument();
});

it("shows the disclosure gate (not composer) when not acked", async () => {
  mockTurns({ can_send: true, needs_disclosure: true });
  renderChat();
  expect(await screen.findByRole("button", { name: /i understand/i })).toBeInTheDocument();
  expect(screen.queryByPlaceholderText(/message the agent/i)).not.toBeInTheDocument();
});

it("shows the setup gate (not the disclosure gate) on a setup-phase workspace", async () => {
  mockTurns({ phase: "setup", needs_disclosure: true, can_send: false });
  renderChat();
  expect(await screen.findByText(/finish setup/i)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /i understand/i })).not.toBeInTheDocument();
});

it("read-only for a non-owner (can_send false, phase active)", async () => {
  mockTurns({ can_send: false, needs_disclosure: false, phase: "active" });
  renderChat();
  expect(await screen.findByText(/read-only/i)).toBeInTheDocument();
});
