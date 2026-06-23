import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { deadlineView } from "./Deadline";

const NOW = new Date("2026-06-23T12:00:00Z").getTime();
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});
afterEach(() => { vi.useRealTimers(); });

function isoInDays(days: number): string {
  return new Date(NOW + days * 86_400_000).toISOString();
}

it("maps deadline distance to states at the 14d / 30d cutoffs", () => {
  expect(deadlineView(isoInDays(-1)).state).toBe("closed");
  expect(deadlineView(isoInDays(5)).state).toBe("soon");
  expect(deadlineView(isoInDays(14)).state).toBe("soon"); // <= 14
  expect(deadlineView(isoInDays(20)).state).toBe("watch");
  expect(deadlineView(isoInDays(30)).state).toBe("watch"); // <= 30
  expect(deadlineView(isoInDays(45)).state).toBe("open");
});
