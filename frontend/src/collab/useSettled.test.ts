import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CollabProvider } from "./collabProvider";
import { useSettled } from "./useSettled";

// Controllable stub: a mutable flag, NOT a fixed call-sequence — useSettled samples
// isSettled() eagerly in BOTH the useState initializer and the immediate effect, so a
// "false then true" sequence would flip before any timer advance and prove nothing.
function stubProvider(initial: boolean) {
  const ref = { val: initial };
  const provider = { isSettled: () => ref.val } as unknown as CollabProvider;
  return { provider, ref };
}

describe("useSettled", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("starts at the provider's current settle state and re-samples on the interval", () => {
    const { provider, ref } = stubProvider(false);
    const { result } = renderHook(() => useSettled(provider));
    expect(result.current).toBe(false); // initializer + immediate effect both see false

    ref.val = true;
    act(() => vi.advanceTimersByTime(200));
    expect(result.current).toBe(true); // the poll picked up the drain

    ref.val = false;
    act(() => vi.advanceTimersByTime(200));
    expect(result.current).toBe(false);
  });
});
