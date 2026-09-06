import { useEffect, useRef, useState } from "react";

import type { components } from "./schema.gen";

export type PublicBoardOut = components["schemas"]["PublicBoardOut"];
export type PublicBoardItemOut = components["schemas"]["PublicBoardItemOut"];

// D6 (decisions note): two DIFFERENT numbers. The refresh interval is how
// often a healthy screen re-polls; the max age is how long a copy the screen
// can no longer refresh is allowed to keep showing. Neither implies the
// other — a screen that refreshes every 5 minutes but has been failing for an
// hour must still go dark.
export const BOARD_REFRESH_MS = 5 * 60 * 1000;
export const BOARD_MAX_AGE_MS = 60 * 60 * 1000;

// A THIRD number, independent of the two above: the ceiling on a single
// fetch attempt. Well under BOARD_REFRESH_MS so that on a firewall that
// blackholes rather than resets (common on hospital networks) a hung attempt
// is force-settled before the next poll tick fires — otherwise pending
// fetches pile up one per tick against the browser's per-origin connection
// limit, and the board goes dark at BOARD_MAX_AGE_MS and stays dark longer
// than the outage that caused it.
export const BOARD_FETCH_TIMEOUT_MS = 30 * 1000;

// Deliberately NOT `unwrap()` (frontend/src/api/client.ts). `unwrap` performs
// a full-page navigation on 401 (redirectToLogin) and on 403
// account_inactive (redirectToNoAccess) — on an unattended corridor screen
// that means the board silently becomes a login page nobody is standing at
// to dismiss. This fetches directly and reads `response.status`, so no code
// path here can ever call `window.location.assign`.
//
// "ok" -> render + remember. "gone" -> 404 or 410, the unpublish path: blank
// the board, never the old cards. Everything else (a 5xx, a non-JSON body, a
// thrown network error, OR a 200 that doesn't parse into a PublicBoardOut
// shape) is "transport_error": keep whatever the last good copy was, because
// a stale board beats a browser error on a hospital wall.
export type PublicBoardFetchResult =
  | { kind: "ok"; data: PublicBoardOut }
  | { kind: "gone" }
  | { kind: "transport_error" };

// CRITICAL fix-round-1 guard: a 200 whose body parses as JSON but isn't a
// PublicBoardOut (`{}`, `{"detail": "..."}`, a bare `[]`, `{"items": null}`)
// used to be cast with `as PublicBoardOut` and trusted outright. BoardScreen
// then called `board.items.filter(...)`, which throws
// `Cannot read properties of undefined (reading 'filter')` DURING RENDER.
// There is no `errorElement` anywhere in this app, so that throw reaches
// App.tsx's global ErrorBoundary, which unmounts the whole tree (clearing
// both setIntervals in usePublicBoard) and renders a "Try again" button
// nobody on an unattended wall will ever click — permanent, not stale.
//
// Depth of this check, decided on purpose rather than by reflex: it
// validates only that `items` is present and is an array, which is the one
// property every downstream read (`board.items.filter`,
// `nearestDeadlineLocal`'s iteration) actually dereferences without a prior
// null/undefined guard. Every OTHER field BoardScreen/BoardCard touch is
// already read defensively — `items_per_page`/`page_seconds` fall back with
// `??`, `title`/`source_name` render fine as `undefined`, `budgetText` and
// `closesInText` both bail out on a falsy value before doing arithmetic on
// it — so a missing or wrong-typed field elsewhere degrades the DISPLAY
// (e.g. a blank title) rather than throwing. Going further — checking every
// item's field types — would need a real schema validator (a new
// dependency, which is out of scope under the 7-day pnpm cooldown) to be
// done properly, for a shape the real endpoint's Pydantic response_model
// cannot actually produce (`items: list[PublicBoardItemOut]` cannot
// serialize a `None` element). This one check closes the actual render-throw
// gap without over-building for shapes the backend cannot emit.
function hasItemsArray(data: unknown): data is PublicBoardOut {
  return (
    typeof data === "object" && data !== null && Array.isArray((data as { items?: unknown }).items)
  );
}

export async function fetchPublicBoard(slug: string): Promise<PublicBoardFetchResult> {
  // IMPORTANT-1 fix: bound every attempt to BOARD_FETCH_TIMEOUT_MS. Chosen
  // over a bare monotonic-request-id guard because an id check alone only
  // decides whether a LATE response gets written — it never cancels the
  // underlying request, so a truly blackholed connection would stay open
  // (and un-retried) indefinitely, which is worse than the pile-up it was
  // meant to fix. The timeout guarantees every attempt settles, one way or
  // another, well inside one refresh cycle.
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), BOARD_FETCH_TIMEOUT_MS);
  let res: Response;
  try {
    res = await fetch(`/api/public/boards/${encodeURIComponent(slug)}`, {
      // MINOR 3: this endpoint carries no auth dependency at all (it is the
      // one deliberately-public, unauthenticated route — see
      // web/api/public_boards.py's module docstring) — "omit" makes that
      // structural instead of true only by the endpoint's good behaviour,
      // so the httpOnly session cookie never rides along on an unattended
      // screen's poll of a public URL.
      credentials: "omit",
      signal: controller.signal,
    });
  } catch {
    return { kind: "transport_error" };
  } finally {
    clearTimeout(timeoutId);
  }
  // The unpublish path (§6: unpublished and nonexistent give the SAME 404).
  // 410 is handled the same way in case the endpoint ever grows it — nothing
  // downstream here distinguishes "unpublished" from "deleted for good".
  if (res.status === 404 || res.status === 410) return { kind: "gone" };
  if (!res.ok) return { kind: "transport_error" };
  try {
    const data: unknown = await res.json();
    if (!hasItemsArray(data)) return { kind: "transport_error" };
    return { kind: "ok", data };
  } catch {
    return { kind: "transport_error" };
  }
}

export type LastGoodBoard = { data: PublicBoardOut; fetchedAt: number } | null;

// Pure D6 state-transition table, factored out of the polling loop so each
// branch is testable without faking a timer or a network per case.
export function applyPublicBoardResult(
  prev: LastGoodBoard,
  result: PublicBoardFetchResult,
  now = Date.now(),
): LastGoodBoard {
  if (result.kind === "ok") return { data: result.data, fetchedAt: now };
  if (result.kind === "gone") return null;
  return prev; // transport_error: the last good copy survives untouched
}

// The bound stays independent of `applyPublicBoardResult` above: a copy that
// is simply never refreshed (every poll returns transport_error) must still
// expire, and the reducer above has no way to notice time passing on its own.
export function isBoardVisible(lastGood: LastGoodBoard, now = Date.now()): boolean {
  return lastGood != null && now - lastGood.fetchedAt <= BOARD_MAX_AGE_MS;
}

/** Poll the public board endpoint and expose the D6 state machine's current
 *  verdict: the last good payload, or null once it has been blanked (404/410)
 *  or has aged past BOARD_MAX_AGE_MS. Polls immediately on mount/slug change,
 *  then every BOARD_REFRESH_MS. */
export function usePublicBoard(slug: string): PublicBoardOut | null {
  const [lastGood, setLastGood] = useState<LastGoodBoard>(null);
  // A tick that advances on every poll (regardless of outcome) forces a
  // re-render so the 60-minute staleness check above gets re-evaluated even
  // when a run of transport_errors leaves `lastGood` referentially unchanged.
  const [, setTick] = useState(0);
  const lastGoodRef = useRef<LastGoodBoard>(null);
  // IMPORTANT-1 fix, the other half: a monotonic id, bumped every time a poll
  // starts. BOARD_FETCH_TIMEOUT_MS above keeps two fetches from realistically
  // overlapping, but this is the unconditional guarantee — if an older
  // request's response nonetheless lands after a newer one has already been
  // issued, `id !== requestIdRef.current` below discards it. Without this, a
  // slow 200 resolving after a fast 404 would overwrite `lastGoodRef` with
  // stale cards: "the emergency stop would not stop anything."
  const requestIdRef = useRef(0);

  useEffect(() => {
    let cancelled = false;
    setLastGood(null);
    lastGoodRef.current = null;
    requestIdRef.current = 0;

    const poll = async () => {
      const id = ++requestIdRef.current;
      const result = await fetchPublicBoard(slug);
      if (cancelled) return;
      if (id !== requestIdRef.current) return; // superseded by a newer poll while this one was in flight
      const next = applyPublicBoardResult(lastGoodRef.current, result);
      lastGoodRef.current = next;
      setLastGood(next);
      setTick((t) => t + 1);
    };

    void poll();
    const intervalId = setInterval(() => void poll(), BOARD_REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(intervalId);
    };
  }, [slug]);

  return isBoardVisible(lastGood) ? (lastGood as NonNullable<LastGoodBoard>).data : null;
}
