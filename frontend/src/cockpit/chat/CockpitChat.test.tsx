import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useNavigate, useParams } from "react-router-dom";
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
  // Stream one assistant turn → it appears in the transcript. The handler fires a
  // React state update (streamed turns), so drive it inside act().
  act(() => captured.turn!({ data: JSON.stringify({ id: "a1", role: "assistant", position: 5, created_at: "", content: { type: "text", text: "streamed!" } }) } as MessageEvent));
  expect(await screen.findByText("streamed!")).toBeInTheDocument();
  // done closes cleanly (no throw).
  act(() => captured.done?.({ data: JSON.stringify({ status: "completed" }) } as MessageEvent));
});

it("remounting on a workspace-id change (per-id key) starts a FRESH stream and does not bleed A's turns into B (Defect 3)", async () => {
  // The AppShell keys the cockpit subtree by workspace id, so /cockpit/A -> /cockpit/B
  // REMOUNTS CockpitChat. This test renders CockpitChat with that key and asserts the
  // remount resets jobId/streamed: B opens its own stream (no leftover A job in the
  // URL) and A's streamed turn does NOT appear under B.
  const urls: string[] = [];
  const captured: { turn?: (e: MessageEvent) => void } = {};
  class CapES {
    onerror = null; onopen: (() => void) | null = null; close = vi.fn();
    addEventListener(type: string, fn: (e: MessageEvent) => void) {
      if (type === "turn") captured.turn = fn;
    }
    constructor(u: string) {
      urls.push(u);
    }
  }
  vi.stubGlobal("EventSource", CapES as unknown as typeof EventSource);
  // A has a running job; B has its own different running job.
  vi.spyOn(clientMod.api, "GET").mockImplementation(((
    path: string,
    init?: { params?: { path?: { workspace_id?: string } } },
  ) => {
    const ws = init?.params?.path?.workspace_id ?? "";
    if (path.endsWith("/turns"))
      return Promise.resolve({
        data: { turns: [], can_send: true, needs_disclosure: false, phase: "active", is_owner: true, disabled_reason: null, running_job_id: ws === "ws-b" ? "job-B" : "job-A" },
        error: undefined,
      });
    return Promise.resolve({ data: [], error: undefined });
  }) as never);

  // Mirror AppShell's per-id keying: the chat is keyed by the live workspace id, so a
  // real param-changing navigation /cockpit/ws-a -> /cockpit/ws-b REMOUNTS it.
  function KeyedChat() {
    const { id = "" } = useParams();
    return <CockpitChat key={`cockpit:${id}`} />;
  }
  function GoB() {
    const navigate = useNavigate();
    return (
      <button type="button" onClick={() => navigate("/cockpit/ws-b")}>
        to-b
      </button>
    );
  }
  const qc = new QueryClient();
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/cockpit/ws-a"]}>
        <Routes>
          <Route path="/cockpit/:id" element={<><KeyedChat /><GoB /></>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await waitFor(() => expect(captured.turn).toBeDefined());
  // A streams a turn.
  act(() => captured.turn!({ data: JSON.stringify({ id: "a1", role: "assistant", position: 5, created_at: "", content: { type: "text", text: "A-SECRET-TURN" } }) } as MessageEvent));
  expect(await screen.findByText("A-SECRET-TURN")).toBeInTheDocument();
  expect(urls.some((u) => u.includes("ws-a") && u.includes("job-A"))).toBe(true);

  // Navigate to B (param change -> the per-id key changes -> CockpitChat remounts).
  fireEvent.click(screen.getByRole("button", { name: /to-b/i }));
  // B opens its OWN stream (job-B on ws-b); the URL never targets A's job under B.
  await waitFor(() => expect(urls.some((u) => u.includes("ws-b") && u.includes("job-B"))).toBe(true));
  expect(urls.some((u) => u.includes("ws-b") && u.includes("job-A"))).toBe(false);
  // A's streamed turn did not bleed into B's transcript.
  expect(screen.queryByText("A-SECRET-TURN")).toBeNull();
});

it("footer shows the drafting-disabled message via disabled_reason", async () => {
  mockTurns({ disabled_reason: "drafting_disabled", can_send: false, phase: "active" });
  renderChat();
  expect(await screen.findByText(/drafting is currently disabled/i)).toBeInTheDocument();
});

class FakeESNoop {
  onerror = null; onopen: (() => void) | null = null; close = vi.fn();
  addEventListener() {}
  constructor(_u: string) {}
}

// Stateful mock: after a SUCCESSFUL cancel the server read-model drops the
// in-flight job (running_job_id → null), so the post-cancel refetch cannot
// re-set the local jobId. This lets the {cancelled:true} test prove the
// success handler's setJobId(null) actually ran (the effect never clears
// jobId on its own — it only sets it when running_job_id is truthy).
function mockInFlightOwner(cancelled: boolean) {
  let cleared = false;
  vi.stubGlobal("EventSource", FakeESNoop as unknown as typeof EventSource);
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/turns"))
      return Promise.resolve({ data: { turns: [], can_send: true, needs_disclosure: false, phase: "active", is_owner: true, disabled_reason: null, running_job_id: cleared ? null : "job-1" }, error: undefined });
    if (path.endsWith("/references")) return Promise.resolve({ data: [], error: undefined });
    return Promise.resolve({ data: null, error: undefined });
  }) as never);
  vi.spyOn(clientMod.api, "POST").mockImplementation(((path: string) => {
    if (path.endsWith("/cancel")) {
      if (cancelled) cleared = true;
      return Promise.resolve({ data: { cancelled }, error: undefined });
    }
    return Promise.resolve({ data: null, error: undefined });
  }) as never);
}

it("owner sees Cancel while in-flight; {cancelled:true} clears the in-flight state", async () => {
  mockInFlightOwner(true);
  renderChat();
  fireEvent.click(await screen.findByRole("button", { name: /^cancel$/i }));
  await waitFor(() =>
    expect(clientMod.api.POST).toHaveBeenCalledWith(
      expect.stringContaining("/cancel"),
      expect.objectContaining({ params: { path: { workspace_id: "ws-1", job_id: "job-1" } } }),
    ),
  );
  // Cancel disappears ONLY if setJobId(null) ran AND the refetch returned
  // running_job_id:null — proving the local clear (spec §4.3).
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: /^cancel$/i })).not.toBeInTheDocument(),
  );
  expect(screen.queryByText(/too late/i)).not.toBeInTheDocument();
});

it("{cancelled:false} shows the too-late note and KEEPS the turn in-flight", async () => {
  mockInFlightOwner(false);
  renderChat();
  fireEvent.click(await screen.findByRole("button", { name: /^cancel$/i }));
  expect(await screen.findByText(/too late/i)).toBeInTheDocument();
  // jobId retained → the Cancel control stays (composer remains disabled), per spec §4.3.
  expect(screen.getByRole("button", { name: /^cancel$/i })).toBeInTheDocument();
});

it("a viewer does not see the Cancel control", async () => {
  vi.stubGlobal("EventSource", FakeESNoop as unknown as typeof EventSource);
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/turns"))
      return Promise.resolve({ data: { turns: [], can_send: false, needs_disclosure: false, phase: "active", is_owner: false, disabled_reason: "not_owner", running_job_id: "job-1" }, error: undefined });
    return Promise.resolve({ data: [], error: undefined });
  }) as never);
  renderChat();
  expect(await screen.findByText(/read-only/i)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /^cancel$/i })).not.toBeInTheDocument();
});

it("clears the stale 'too late' note when the turn finishes (SSE done)", async () => {
  const captured: { done?: (e: MessageEvent) => void } = {};
  class CapturingES {
    onerror = null; onopen: (() => void) | null = null; close = vi.fn();
    addEventListener(type: string, fn: (e: MessageEvent) => void) {
      if (type === "done") captured.done = fn;
    }
    constructor(_u: string) {}
  }
  vi.stubGlobal("EventSource", CapturingES as unknown as typeof EventSource);
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/turns"))
      return Promise.resolve({ data: { turns: [], can_send: true, needs_disclosure: false, phase: "active", is_owner: true, disabled_reason: null, running_job_id: "job-1" }, error: undefined });
    if (path.endsWith("/references")) return Promise.resolve({ data: [], error: undefined });
    return Promise.resolve({ data: null, error: undefined });
  }) as never);
  vi.spyOn(clientMod.api, "POST").mockImplementation(((path: string) => {
    if (path.endsWith("/cancel")) return Promise.resolve({ data: { cancelled: false }, error: undefined });
    return Promise.resolve({ data: null, error: undefined });
  }) as never);
  renderChat();
  fireEvent.click(await screen.findByRole("button", { name: /^cancel$/i }));
  expect(await screen.findByText(/too late/i)).toBeInTheDocument();
  // The turn finishes → SSE done fires → the stale note must NOT survive into the next turn.
  await waitFor(() => expect(captured.done).toBeDefined());
  act(() => captured.done!({ data: JSON.stringify({ status: "completed" }) } as MessageEvent));
  await waitFor(() => expect(screen.queryByText(/too late/i)).not.toBeInTheDocument());
});
