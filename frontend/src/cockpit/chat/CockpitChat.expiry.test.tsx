/**
 * Review 2026-09-02 A7/F2: `useEventStream` classifies a dead SSE stream by
 * probing /api/me, then CLOSES the EventSource so it cannot auto-reconnect —
 * but CockpitChat, its only caller, passed neither `onExpired` nor
 * `onNoAccess`. The classification was thrown away: `jobId` stayed set with no
 * stream behind it, so the composer stayed disabled under "Assistant is
 * working…" over a socket that was never coming back. The user was rescued only
 * by an unrelated 15 s Problems poller happening to hit the 401 chokepoint, and
 * the 401/403 branch of events.ts was otherwise dead code.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../../api/client";
import { CockpitChat } from "./CockpitChat";

/** Minimal EventSource that lets a test drive `onerror`. */
class FakeES {
  static instances: FakeES[] = [];
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  closed = false;
  constructor(public url: string) {
    FakeES.instances.push(this);
  }
  addEventListener() {}
  close() {
    this.closed = true;
  }
}

afterEach(() => {
  FakeES.instances = [];
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/** A workspace with a turn already running, so the chat opens a stream on mount. */
function mockRunningTurn() {
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/turns"))
      return Promise.resolve({
        data: {
          turns: [],
          can_send: true,
          needs_disclosure: false,
          phase: "active",
          is_owner: true,
          disabled_reason: null,
          running_job_id: "job-1",
        },
        error: undefined,
      });
    if (path.endsWith("/references")) return Promise.resolve({ data: [], error: undefined });
    return Promise.resolve({ data: null, error: undefined });
  }) as never);
}

function renderChat() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={["/cockpit/ws-1"]}>
        <Routes>
          <Route path="/cockpit/:id" element={<CockpitChat />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Drive the three onerror calls events.ts needs before it probes /api/me. */
async function failTheStream(status: number) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ status })) as never,
  );
  await waitFor(() => expect(FakeES.instances.length).toBeGreaterThan(0));
  const es = FakeES.instances[0];
  for (let i = 0; i < 3; i++) await es.onerror?.();
  return es;
}

it("a 401 mid-turn routes to login instead of leaving a phantom running turn", async () => {
  vi.stubGlobal("EventSource", FakeES);
  const toLogin = vi.spyOn(clientMod, "redirectToLogin").mockImplementation(() => {});
  mockRunningTurn();
  renderChat();

  const es = await failTheStream(401);

  await waitFor(() => expect(toLogin).toHaveBeenCalled());
  expect(es.closed, "events.ts closes the stream; the callback is the only exit").toBe(true);
});

it("a 403 mid-turn routes to the no-access page", async () => {
  vi.stubGlobal("EventSource", FakeES);
  const toNoAccess = vi.spyOn(clientMod, "redirectToNoAccess").mockImplementation(() => {});
  mockRunningTurn();
  renderChat();

  await failTheStream(403);

  await waitFor(() => expect(toNoAccess).toHaveBeenCalled());
});

it("a transient failure (200 from the probe) redirects nowhere", async () => {
  // The other half of the contract: a wifi blip must not throw the user out.
  vi.stubGlobal("EventSource", FakeES);
  const toLogin = vi.spyOn(clientMod, "redirectToLogin").mockImplementation(() => {});
  const toNoAccess = vi.spyOn(clientMod, "redirectToNoAccess").mockImplementation(() => {});
  mockRunningTurn();
  renderChat();

  const es = await failTheStream(200);

  expect(toLogin).not.toHaveBeenCalled();
  expect(toNoAccess).not.toHaveBeenCalled();
  expect(es.closed).toBe(false);
});
