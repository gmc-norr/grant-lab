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

function renderChat() {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { turns: [], can_send: true, needs_disclosure: false, phase: "active", running_job_id: null },
    error: undefined,
  } as never);
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
  renderChat();
  expect(await screen.findByRole("complementary", { name: /chat/i })).toBeInTheDocument();
});
