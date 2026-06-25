import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../../api/client";
import { resetEditorStore } from "../editorStore";
import { CockpitChat } from "./CockpitChat";

afterEach(() => {
  vi.restoreAllMocks();
  resetEditorStore();
});

function mockTurns(over: Partial<{ can_send: boolean; needs_disclosure: boolean; phase: string; role: string; is_owner: boolean; disabled_reason: string | null }>) {
  const can_send = over.can_send ?? true;
  const needs_disclosure = over.needs_disclosure ?? false;
  const phase = over.phase ?? "active";
  const disabled_reason =
    over.disabled_reason !== undefined
      ? over.disabled_reason
      : phase === "setup" ? "setup" : !can_send ? "not_owner" : needs_disclosure ? "needs_disclosure" : null;
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/turns"))
      return Promise.resolve({ data: { turns: [], can_send, needs_disclosure, phase, is_owner: over.is_owner ?? true, disabled_reason, running_job_id: null }, error: undefined });
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

it("shows the setup placeholder (not the disclosure gate) on a setup-phase workspace", async () => {
  mockTurns({ phase: "setup", needs_disclosure: true, can_send: false });
  renderChat();
  expect(await screen.findByText(/chat opens once the workspace is active/i)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /i understand/i })).not.toBeInTheDocument();
});

it("setup-phase viewer sees the setup placeholder, not the read-only message", async () => {
  mockTurns({ phase: "setup", is_owner: false, can_send: false, disabled_reason: "not_owner" });
  renderChat();
  expect(await screen.findByText(/chat opens once the workspace is active/i)).toBeInTheDocument();
  expect(screen.queryByText(/read-only/i)).not.toBeInTheDocument();
});

it("read-only for a non-owner (can_send false, phase active)", async () => {
  mockTurns({ can_send: false, needs_disclosure: false, phase: "active" });
  renderChat();
  expect(await screen.findByText(/read-only/i)).toBeInTheDocument();
});

it("streams turns from a running job and invalidates on done", async () => {
  const captured: { turn?: (e: MessageEvent) => void; done?: (e: MessageEvent) => void } = {};
  class FakeES {
    onerror = null; onopen: (() => void) | null = null; close = vi.fn();
    addEventListener(type: string, fn: (e: MessageEvent) => void) {
      if (type === "turn") captured.turn = fn;
      if (type === "done") captured.done = fn;
    }
    constructor(_u: string) {}
  }
  vi.stubGlobal("EventSource", FakeES as unknown as typeof EventSource);
  // running_job_id set → CockpitChat re-attaches the stream on mount.
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/turns"))
      return Promise.resolve({ data: { turns: [], can_send: true, needs_disclosure: false, phase: "active", running_job_id: "job-1" }, error: undefined });
    return Promise.resolve({ data: [], error: undefined });
  }) as never);
  renderChat();
  // Wait for the data fetch to resolve and the FakeES to be instantiated with the
  // job-1 stream URL (the effect runs after running_job_id is set).
  await waitFor(() => expect(captured.turn).toBeDefined());
  // Stream one assistant turn → it appears in the transcript.
  captured.turn!({ data: JSON.stringify({ id: "a1", role: "assistant", position: 5, created_at: "", content: { type: "text", text: "streamed!" } }) } as MessageEvent);
  expect(await screen.findByText("streamed!")).toBeInTheDocument();
  // done closes cleanly (no throw).
  captured.done?.({ data: JSON.stringify({ status: "completed" }) } as MessageEvent);
});

it("footer shows the drafting-disabled message via disabled_reason", async () => {
  mockTurns({ disabled_reason: "drafting_disabled", can_send: false, phase: "active" });
  renderChat();
  expect(await screen.findByText(/drafting is currently disabled/i)).toBeInTheDocument();
});
