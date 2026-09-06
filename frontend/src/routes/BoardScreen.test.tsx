import { MantineProvider } from "@mantine/core";
import { act, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { PublicBoardItemOut, PublicBoardOut } from "../api/publicBoard";
import { theme } from "../theme/theme";
import { BoardScreen } from "./BoardScreen";

const FIXED_NOW = new Date("2026-01-01T22:50:00.000Z"); // Stockholm 23:50, Jan 1

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(FIXED_NOW);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function makeBoard(overrides: Partial<PublicBoardOut> = {}): PublicBoardOut {
  return {
    title: "Radiology corridor board",
    generated_at: FIXED_NOW.toISOString(),
    orientation: "landscape",
    items_per_page: 8,
    max_pages: 3,
    page_seconds: 30,
    items: [],
    ...overrides,
  };
}

function makeItem(n: number, overrides: Partial<PublicBoardItemOut> = {}): PublicBoardItemOut {
  return {
    title: `Call ${n}`,
    source_name: "Vinnova",
    deadline_local_date: "2026-06-01", // far in the future, well clear of "closed"
    budget_min: null,
    budget_max: null,
    currency: null,
    url: `https://example.org/call/${n}`,
    qr_modules: 29,
    qr_path: "M0 0h1v1h-1z",
    ...overrides,
  };
}

function mockBoardFetchOnce(board: PublicBoardOut) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => board } as unknown as Response),
  );
}

function renderScreen(slug = "test-slug") {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <MemoryRouter initialEntries={[`/b/${slug}`]}>
        <Routes>
          <Route path="/b/:slug" element={<BoardScreen />} />
        </Routes>
      </MemoryRouter>
    </MantineProvider>,
  );
}

// Flush the initial (and any subsequently mocked) fetch's promise chain
// without letting real time pass — `usePublicBoard` polls via `fetch` +
// `.json()`, both microtasks, which fake timers do not auto-flush.
async function flush(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

it("renders 8 slots on a landscape board and 4 on a portrait one", async () => {
  const landscape = makeBoard({
    orientation: "landscape",
    items_per_page: 8,
    items: Array.from({ length: 8 }, (_, i) => makeItem(i)),
  });
  mockBoardFetchOnce(landscape);
  const { container } = renderScreen("landscape-slug");
  await flush();
  expect(container.querySelectorAll('[data-field="board-card"]')).toHaveLength(8);

  const portrait = makeBoard({
    orientation: "portrait",
    items_per_page: 4,
    items: Array.from({ length: 4 }, (_, i) => makeItem(i)),
  });
  mockBoardFetchOnce(portrait);
  const { container: container2 } = renderScreen("portrait-slug");
  await flush();
  expect(container2.querySelectorAll('[data-field="board-card"]')).toHaveLength(4);
});

it("three calls on a portrait board leaves ONE empty slot", async () => {
  const board = makeBoard({
    orientation: "portrait",
    items_per_page: 4,
    items: Array.from({ length: 3 }, (_, i) => makeItem(i)),
  });
  mockBoardFetchOnce(board);
  const { container } = renderScreen();
  await flush();
  expect(container.querySelectorAll('[data-field="board-card"]')).toHaveLength(3);
  expect(container.querySelectorAll('[data-field="empty-slot"]')).toHaveLength(1);
});

it("three calls on a landscape board leaves FIVE empty slots", async () => {
  const board = makeBoard({
    orientation: "landscape",
    items_per_page: 8,
    items: Array.from({ length: 3 }, (_, i) => makeItem(i)),
  });
  mockBoardFetchOnce(board);
  const { container } = renderScreen();
  await flush();
  expect(container.querySelectorAll('[data-field="board-card"]')).toHaveLength(3);
  expect(container.querySelectorAll('[data-field="empty-slot"]')).toHaveLength(5);
});

it("zero approved items shows 'No calls are currently posted'", async () => {
  mockBoardFetchOnce(makeBoard({ items: [] }));
  renderScreen();
  await flush();
  expect(screen.getByText("No calls are currently posted")).toBeInTheDocument();
});

it("does NOT say 'No open calls right now'", async () => {
  mockBoardFetchOnce(makeBoard({ items: [] }));
  renderScreen();
  await flush();
  expect(screen.queryByText(/no open calls right now/i)).not.toBeInTheDocument();
});

it("the header shows the nearest deadline ACROSS ALL PAGES, not the current page", async () => {
  // 17 items at 8/page = 3 pages. The nearest deadline sits on the LAST item
  // (page 3); everything else is far in the future. Rendered at page 1.
  const items = Array.from({ length: 17 }, (_, i) =>
    makeItem(i, { deadline_local_date: "2027-01-01" }),
  );
  items[16] = makeItem(16, { deadline_local_date: "2026-01-06" }); // 5 days out
  mockBoardFetchOnce(makeBoard({ orientation: "landscape", items_per_page: 8, items }));
  renderScreen();
  await flush();
  // Still on page 1 (no rotation has happened yet).
  expect(screen.getByText("Sida 1 av 3")).toBeInTheDocument();
  expect(screen.getByText("Closes in 5 days")).toBeInTheDocument();
});

it("a card whose deadline has passed is dropped even from a stale copy", async () => {
  // "Today" (Stockholm) at mount is 2026-01-01; the deadline is also
  // 2026-01-01, so the card starts OPEN (days === 0, not "closed").
  const board = makeBoard({ items: [makeItem(1, { deadline_local_date: "2026-01-01" })] });
  mockBoardFetchOnce(board);
  renderScreen();
  await flush();
  expect(screen.getByText("Call 1")).toBeInTheDocument();

  // Advance 11 real minutes — well under BOTH the ~5-minute refetch cadence's
  // effect on correctness (a refetch would just return the same payload) and
  // the 60-minute last-good-copy bound — but enough to cross Stockholm local
  // midnight, so "today" becomes 2026-01-02 and the card's deadline is now in
  // the past. No 404/410 involved: this is the SAME cached payload, filtered
  // again locally. A mutation that only filters in the 200-fetch branch never
  // re-evaluates this and leaves the card on screen.
  await flush(11 * 60 * 1000);
  expect(screen.queryByText("Call 1")).not.toBeInTheDocument();
});

it("the footer shows Sida N av M", async () => {
  const board = makeBoard({
    orientation: "landscape",
    items_per_page: 8,
    items: Array.from({ length: 9 }, (_, i) => makeItem(i)), // 2 pages
  });
  mockBoardFetchOnce(board);
  renderScreen();
  await flush();
  expect(screen.getByText("Sida 1 av 2")).toBeInTheDocument();
});

it("pages advance after page_seconds", async () => {
  const board = makeBoard({
    orientation: "landscape",
    items_per_page: 8,
    page_seconds: 30,
    items: Array.from({ length: 9 }, (_, i) => makeItem(i)), // 2 pages
  });
  mockBoardFetchOnce(board);
  renderScreen();
  await flush();
  expect(screen.getByText("Sida 1 av 2")).toBeInTheDocument();

  await flush(30 * 1000);
  expect(screen.getByText("Sida 2 av 2")).toBeInTheDocument();
});

it("renders no score, no rationale and no profile name", async () => {
  const board = makeBoard({ items: [makeItem(1)] });
  mockBoardFetchOnce(board);
  const { container } = renderScreen();
  await flush();
  // MINOR 4 fix: a positive anchor first — without it this test passed on a
  // completely blank render (three absence checks against an empty string
  // are trivially true), which proves nothing about what the closed payload
  // actually omits.
  expect(screen.getByText("Call 1")).toBeInTheDocument();
  const text = container.textContent ?? "";
  expect(text).not.toMatch(/score/i);
  expect(text).not.toMatch(/rationale/i);
  expect(text).not.toMatch(/profile/i);
});

it("a 404 blanks a board that was previously showing cards", async () => {
  mockBoardFetchOnce(makeBoard({ items: [makeItem(1)] }));
  const { container } = renderScreen();
  await flush();
  expect(screen.getByText("Call 1")).toBeInTheDocument();

  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({}) } as unknown as Response),
  );
  await flush(5 * 60 * 1000); // the ~5-minute refresh interval
  expect(screen.queryByText("Call 1")).not.toBeInTheDocument();
  expect(container.querySelector('[data-board-state="blank"]')).not.toBeNull();
});

// IMPORTANT 2: the mirror of the test above, through the SAME hook
// (usePublicBoard, wired up inside BoardScreen) — a 500 must NOT blank the
// board. This is the untested "stale copy survives" direction end to end;
// the existing publicBoard.test.ts cases exercise the reducer in isolation,
// which a bug INSIDE the hook's own wiring (not the reducer) can bypass.
it("a 500 leaves the cards on the wall (through the hook)", async () => {
  mockBoardFetchOnce(makeBoard({ items: [makeItem(1)] }));
  const { container } = renderScreen();
  await flush();
  expect(screen.getByText("Call 1")).toBeInTheDocument();

  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ ok: false, status: 500, json: async () => ({}) } as unknown as Response),
  );
  await flush(5 * 60 * 1000);
  expect(screen.getByText("Call 1")).toBeInTheDocument();
  expect(container.querySelector('[data-board-state="live"]')).not.toBeNull();
});

// Same mirror, for a rejected fetch (a network drop, not an HTTP error).
it("a rejected fetch leaves the cards on the wall (through the hook)", async () => {
  mockBoardFetchOnce(makeBoard({ items: [makeItem(1)] }));
  const { container } = renderScreen();
  await flush();
  expect(screen.getByText("Call 1")).toBeInTheDocument();

  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
  await flush(5 * 60 * 1000);
  expect(screen.getByText("Call 1")).toBeInTheDocument();
  expect(container.querySelector('[data-board-state="live"]')).not.toBeNull();
});
