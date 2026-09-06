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

// Deliberately NOT `unwrap()` (frontend/src/api/client.ts). `unwrap` performs
// a full-page navigation on 401 (redirectToLogin) and on 403
// account_inactive (redirectToNoAccess) — on an unattended corridor screen
// that means the board silently becomes a login page nobody is standing at
// to dismiss. This fetches directly and reads `response.status`, so no code
// path here can ever call `window.location.assign`.
//
// "ok" -> render + remember. "gone" -> 404 or 410, the unpublish path: blank
// the board, never the old cards. Everything else (a 5xx, a non-JSON body, a
// thrown network error) is "transport_error": keep whatever the last good
// copy was, because a stale board beats a browser error on a hospital wall.
export type PublicBoardFetchResult =
  | { kind: "ok"; data: PublicBoardOut }
  | { kind: "gone" }
  | { kind: "transport_error" };

export async function fetchPublicBoard(slug: string): Promise<PublicBoardFetchResult> {
  let res: Response;
  try {
    res = await fetch(`/api/public/boards/${encodeURIComponent(slug)}`, {
      credentials: "same-origin",
    });
  } catch {
    return { kind: "transport_error" };
  }
  // The unpublish path (§6: unpublished and nonexistent give the SAME 404).
  // 410 is handled the same way in case the endpoint ever grows it — nothing
  // downstream here distinguishes "unpublished" from "deleted for good".
  if (res.status === 404 || res.status === 410) return { kind: "gone" };
  if (!res.ok) return { kind: "transport_error" };
  try {
    const data = (await res.json()) as PublicBoardOut;
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

  useEffect(() => {
    let cancelled = false;
    setLastGood(null);
    lastGoodRef.current = null;

    const poll = async () => {
      const result = await fetchPublicBoard(slug);
      if (cancelled) return;
      const next = applyPublicBoardResult(lastGoodRef.current, result);
      lastGoodRef.current = next;
      setLastGood(next);
      setTick((t) => t + 1);
    };

    void poll();
    const id = setInterval(() => void poll(), BOARD_REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [slug]);

  return isBoardVisible(lastGood) ? (lastGood as NonNullable<LastGoodBoard>).data : null;
}
