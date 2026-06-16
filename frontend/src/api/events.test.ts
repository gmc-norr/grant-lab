import { renderHook } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { useEventStream } from "./events";

it("subscribes per named event and the FIRST open carries no cursor", () => {
  const add = vi.fn();
  const instances: FakeES[] = [];
  class FakeES {
    url: string;
    onerror: ((e: unknown) => void) | null = null;
    onopen: (() => void) | null = null;
    addEventListener = add;
    removeEventListener = vi.fn();
    close = vi.fn();
    constructor(u: string) {
      this.url = u;
      instances.push(this);
    }
  }
  vi.stubGlobal("EventSource", FakeES as unknown as typeof EventSource);

  renderHook(() => useEventStream("/events/ping", { ping: () => {} }));

  // Named-event subscription (NOT onmessage):
  expect(add).toHaveBeenCalledWith("ping", expect.any(Function));
  // First open has no cursor; ?since is added only once an id has been seen.
  expect(instances[0].url).not.toContain("since=");
});

it("re-open carries the last-seen id as a ?since cursor, with credentials", () => {
  const instances: FakeES[] = [];
  // Hold the captured listener on an object property, not a `let` — a local
  // reassigned only inside a class method gets narrowed to `never` at the call site.
  const captured: { ping?: (e: MessageEvent) => void } = {};
  class FakeES {
    url: string;
    withCreds: boolean;
    onerror: ((e: unknown) => void) | null = null;
    onopen: (() => void) | null = null;
    removeEventListener = vi.fn();
    close = vi.fn();
    addEventListener(type: string, fn: (e: MessageEvent) => void) {
      if (type === "ping") captured.ping = fn;
    }
    constructor(u: string, init?: { withCredentials?: boolean }) {
      this.url = u;
      this.withCreds = !!init?.withCredentials;
      instances.push(this);
    }
  }
  vi.stubGlobal("EventSource", FakeES as unknown as typeof EventSource);

  const { rerender } = renderHook(({ url }) => useEventStream(url, { ping: () => {} }), {
    initialProps: { url: "/events/ping" },
  });
  // Credentials are sent so the session cookie authenticates the stream.
  expect(instances[0].withCreds).toBe(true);
  expect(instances[0].url).not.toContain("since=");

  // Deliver a named ping carrying an id; the hook records it as the resume cursor.
  captured.ping?.({ lastEventId: "42", data: "{}" } as MessageEvent);
  // Force the effect to re-run (re-open) by changing the url dep.
  rerender({ url: "/events/ping?x=1" });

  // The re-open URL carries ?since=42 — the fresh-connect catch-up cursor.
  expect(instances[1].url).toContain("since=42");
});
