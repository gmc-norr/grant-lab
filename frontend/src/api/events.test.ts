import { renderHook } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { useEventStream } from "./events";

it("subscribes per named event and passes a fresh-connect cursor on (re)open", () => {
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
