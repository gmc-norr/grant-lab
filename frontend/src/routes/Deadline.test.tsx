import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { deadlineLocalView, deadlineView } from "./Deadline";

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

it("deadlineLocalView classifies a YYYY-MM-DD local date string correctly", () => {
  // Past date -> closed; preserve the original date string as-is.
  const past = "2026-06-22";
  const { state: s0, date: d0 } = deadlineLocalView(past);
  expect(s0).toBe("closed");
  expect(d0).toBe(past);

  // 5 days from NOW (2026-06-23T12:00Z) -> soon
  const soon = "2026-06-28";
  expect(deadlineLocalView(soon).state).toBe("soon");

  // 20 days -> watch
  const watch = "2026-07-13";
  expect(deadlineLocalView(watch).state).toBe("watch");

  // 45 days -> open
  const open = "2026-08-07";
  expect(deadlineLocalView(open).state).toBe("open");
});

it("deadlineLocalView: same-day Stockholm deadline is NOT closed after UTC midnight", () => {
  // The old implementation used `new Date(localDate + "T00:00:00Z").getTime() - Date.now()`
  // with `ms <= 0 → closed`.  At 01:30 UTC (= 03:30 Stockholm on the same calendar day)
  // that gave a negative ms for a deadline dated today Stockholm → incorrectly "closed".
  // The fix compares against today's Europe/Stockholm date string, so "today" is never closed.
  //
  // Freeze at 2026-06-23T01:30:00Z — past UTC midnight but still June 23 in Stockholm (UTC+2).
  const utcMidnightPlus90min = new Date("2026-06-23T01:30:00Z").getTime();
  vi.setSystemTime(utcMidnightPlus90min);
  // "2026-06-23" IS today in Stockholm — must NOT be closed.
  const today = "2026-06-23";
  expect(deadlineLocalView(today).state).not.toBe("closed");
  // "2026-06-22" is yesterday in Stockholm — must still be closed.
  const yesterday = "2026-06-22";
  expect(deadlineLocalView(yesterday).state).toBe("closed");
});

// Beta review 2026-10-31 F5. The backend closes a call at its exact instant
// (EU calls at 17:00 Brussels), but deadlineLocalView compared calendar dates
// only, so on the deadline day a call that had ALREADY closed still read as
// open ("soon", "today") in Saved/Hidden, which keep closed calls on purpose.
// The instant now decides closed when it is known; the date stays the label.
it("a deadline that passed earlier TODAY is closed when its instant is known", () => {
  vi.setSystemTime(new Date("2026-10-31T18:00:00Z").getTime());
  const v = deadlineLocalView("2026-10-31", "2026-10-31T13:00:00Z");
  expect(v.state).toBe("closed");
  expect(v.date).toBe("2026-10-31");
});

it("the same deadline is still open before its instant on the same day", () => {
  vi.setSystemTime(new Date("2026-10-31T12:00:00Z").getTime());
  const v = deadlineLocalView("2026-10-31", "2026-10-31T13:00:00Z");
  expect(v.state).toBe("soon");
  expect(v.days).toBe(0);
});

it("without an instant the calendar-date rule is unchanged", () => {
  vi.setSystemTime(new Date("2026-10-31T18:00:00Z").getTime());
  expect(deadlineLocalView("2026-10-31").state).not.toBe("closed");
});
