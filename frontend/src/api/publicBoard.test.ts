import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  applyPublicBoardResult,
  BOARD_MAX_AGE_MS,
  BOARD_REFRESH_MS,
  fetchPublicBoard,
  isBoardVisible,
  usePublicBoard,
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

  // CRITICAL fix round 1: a 200 that parses as JSON but is not a
  // PublicBoardOut used to be trusted outright (`as PublicBoardOut`, no
  // check). BoardScreen then calls `board.items.filter(...)`, which throws
  // `Cannot read properties of undefined (reading 'filter')` DURING RENDER —
  // and with no errorElement anywhere in this app, that throw reaches
  // App.tsx's global ErrorBoundary, which unmounts the tree (clearing both
  // setIntervals) and shows a "Try again" button nobody on the wall will
  // click. Each of these must become transport_error instead, routing into
  // the existing last-good-copy path rather than a permanent, loud death.
  it.each<[string, unknown]>([
    ["an empty object", {}],
    ["items explicitly null", { items: null }],
    ["a bare JSON array (no items key at all)", []],
  ])("a 200 with %s becomes transport_error, not a thrown render", async (_label, body) => {
    mockFetch({ ok: true, status: 200, json: async () => body } as unknown as Response);
    const result = await fetchPublicBoard("slug-a");
    expect(result.kind).toBe("transport_error");
    const prev = { data: BOARD, fetchedAt: Date.now() };
    expect(applyPublicBoardResult(prev, result)).toBe(prev); // last good copy survives
  });
});

describe("usePublicBoard — hook-level wiring (IMPORTANT 1 + 2)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  // IMPORTANT 2: BoardScreen.test.tsx's 404/500/reject cases exercise the
  // hook only through the FULL component; this drives usePublicBoard
  // directly so a bug in ITS wiring (not the reducer, which is already
  // covered above) has nowhere to hide.
  it("keeps the last good copy through the hook across a 500", async () => {
    let call = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(() => {
        call += 1;
        if (call === 1) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => BOARD,
          } as unknown as Response);
        }
        return Promise.resolve({ ok: false, status: 500, json: async () => ({}) } as unknown as Response);
      }),
    );

    const { result } = renderHook(() => usePublicBoard("slug-a"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current?.title).toBe(BOARD.title);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(BOARD_REFRESH_MS);
    });
    expect(result.current?.title).toBe(BOARD.title); // unchanged by the 500
  });

  it("keeps the last good copy through the hook across a rejected fetch", async () => {
    let call = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(() => {
        call += 1;
        if (call === 1) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => BOARD,
          } as unknown as Response);
        }
        return Promise.reject(new Error("offline"));
      }),
    );

    const { result } = renderHook(() => usePublicBoard("slug-a"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current?.title).toBe(BOARD.title);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(BOARD_REFRESH_MS);
    });
    expect(result.current?.title).toBe(BOARD.title);
  });

  // IMPORTANT 1: the out-of-order case, named explicitly by the finding — a
  // slow 200 resolving after a fast 404 must NOT restore the board. The
  // first poll's fetch is held open (never resolves on its own); the second
  // poll (one refresh tick later) resolves immediately with a 404 and blanks
  // the board; only THEN does the first, older request finally resolve with
  // a 200. Without the request-id guard in usePublicBoard, that late "ok"
  // would unconditionally overwrite lastGoodRef and restore the board —
  // "the emergency stop would not stop anything."
  it("an out-of-order response does not restore a board that was already blanked", async () => {
    let resolveSlow!: (value: unknown) => void;
    const slow = new Promise((resolve) => {
      resolveSlow = resolve;
    });
    let call = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(() => {
        call += 1;
        if (call === 1) return slow; // the FIRST (older) poll: held open
        return Promise.resolve({ ok: false, status: 404, json: async () => ({}) } as unknown as Response);
      }),
    );

    const { result } = renderHook(() => usePublicBoard("slug-a"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current).toBeNull(); // first poll still pending, nothing to show yet

    // The SECOND (newer, faster) poll fires at the next tick and blanks it.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(BOARD_REFRESH_MS);
    });
    expect(result.current).toBeNull();

    // NOW the first, older request finally resolves with a 200.
    await act(async () => {
      resolveSlow({ ok: true, status: 200, json: async () => BOARD } as unknown as Response);
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current).toBeNull(); // must stay blank, not restored
  });
});
