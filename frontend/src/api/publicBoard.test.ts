import { afterEach, describe, expect, it, vi } from "vitest";

import {
  applyPublicBoardResult,
  BOARD_MAX_AGE_MS,
  fetchPublicBoard,
  isBoardVisible,
  type PublicBoardOut,
} from "./publicBoard";

afterEach(() => {
  vi.unstubAllGlobals();
});

const BOARD: PublicBoardOut = {
  title: "Radiology corridor board",
  generated_at: "2026-01-01T00:00:00Z",
  orientation: "landscape",
  items_per_page: 8,
  max_pages: 3,
  page_seconds: 30,
  items: [],
};

function mockFetch(res: Partial<Response> & { json?: () => Promise<unknown> }) {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(res));
}

describe("fetchPublicBoard + applyPublicBoardResult — the D6 state machine", () => {
  it("a 200 replaces the last good copy", async () => {
    mockFetch({ ok: true, status: 200, json: async () => BOARD } as unknown as Response);
    const result = await fetchPublicBoard("slug-a");
    expect(result.kind).toBe("ok");
    const prev = { data: { ...BOARD, title: "Old title" }, fetchedAt: Date.now() - 1000 };
    const next = applyPublicBoardResult(prev, result);
    expect(next?.data.title).toBe("Radiology corridor board");
  });

  it("a 500 keeps rendering the last good copy", async () => {
    mockFetch({ ok: false, status: 500, json: async () => ({}) } as unknown as Response);
    const result = await fetchPublicBoard("slug-a");
    expect(result.kind).toBe("transport_error");
    const prev = { data: BOARD, fetchedAt: 12345 };
    expect(applyPublicBoardResult(prev, result)).toBe(prev); // same reference: untouched
  });

  it("a network rejection keeps rendering the last good copy", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    const result = await fetchPublicBoard("slug-a");
    expect(result.kind).toBe("transport_error");
    const prev = { data: BOARD, fetchedAt: 6789 };
    expect(applyPublicBoardResult(prev, result)).toBe(prev);
  });

  // rev 1 of the decisions note folded 404 into "any failed fetch" and kept
  // showing the last good copy forever — both reviewers caught it. A 404 is
  // the unpublish path and MUST blank the board, not merely fail to refresh.
  it("a 404 BLANKS the board", async () => {
    mockFetch({ ok: false, status: 404, json: async () => ({}) } as unknown as Response);
    const result = await fetchPublicBoard("slug-a");
    expect(result.kind).toBe("gone");
    const prev = { data: BOARD, fetchedAt: Date.now() };
    expect(applyPublicBoardResult(prev, result)).toBeNull();
  });

  it("a 410 BLANKS the board", async () => {
    mockFetch({ ok: false, status: 410, json: async () => ({}) } as unknown as Response);
    const result = await fetchPublicBoard("slug-a");
    expect(result.kind).toBe("gone");
    const prev = { data: BOARD, fetchedAt: Date.now() };
    expect(applyPublicBoardResult(prev, result)).toBeNull();
  });

  it("a last good copy older than 60 minutes stops showing cards", () => {
    const now = Date.now();
    const justUnderTheBound = { data: BOARD, fetchedAt: now - (BOARD_MAX_AGE_MS - 1000) };
    const justOverTheBound = { data: BOARD, fetchedAt: now - (BOARD_MAX_AGE_MS + 1000) };
    expect(isBoardVisible(justUnderTheBound, now)).toBe(true);
    expect(isBoardVisible(justOverTheBound, now)).toBe(false);
    expect(isBoardVisible(null, now)).toBe(false);
  });

  // unwrap() (client.ts) full-page-navigates on 401 and on 403
  // account_inactive — catastrophic on a screen nobody is standing at to
  // dismiss a login page. fetchPublicBoard uses raw fetch precisely so no
  // status code can ever reach a navigation call.
  it("never navigates away on any status", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { assign } as never);
    for (const status of [401, 403, 404, 410, 500, 502]) {
      mockFetch({ ok: status < 400, status, json: async () => ({}) } as unknown as Response);
      await fetchPublicBoard("slug-a");
    }
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    await fetchPublicBoard("slug-a");
    expect(assign).not.toHaveBeenCalled();
  });
});
