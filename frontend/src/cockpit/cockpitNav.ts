import { useCallback } from "react";
import { useSearchParams } from "react-router-dom";

import type { NavTarget, TopTab } from "./nav";

export const TOP_TABS: TopTab[] = ["draft", "spec", "scorecard", "review", "history"];

/** `?tab` parsed to a valid TopTab, defaulting to "draft". */
export function tabFromParams(sp: URLSearchParams): TopTab {
  const t = sp.get("tab");
  return (TOP_TABS as string[]).includes(t ?? "") ? (t as TopTab) : "draft";
}

/** `?sec` parsed to a non-negative int, defaulting to 0. */
export function secFromParams(sp: URLSearchParams): number {
  const s = sp.get("sec");
  return s != null && /^\d+$/.test(s) ? Number(s) : 0;
}

export type CockpitNav = {
  tab: TopTab;
  sec: number;
  setTab: (t: TopTab) => void;
  setSec: (i: number, opts?: { replace?: boolean }) => void;
  navigate: (target: NavTarget) => void;
};

// Single source of truth for the cockpit's active artifact tab (?tab) and active
// section (?sec). Both the editor (main) and CockpitSidebar (a shell sibling) read
// these — the URL is the shared channel across the AppShell boundary, exactly as
// Phase 2 used it for the dashboard filters. setSec({ replace: true }) backs the
// out-of-range clamp (an invalid deep-link self-corrects with no history entry).
// navigate() owns the URL half of a NavTarget (tab + section); the bottom-panel
// tab is local cockpit state (handled by Cockpit's onNavigate wrapper).
export function useCockpitNav(): CockpitNav {
  const [sp, setSp] = useSearchParams();
  const tab = tabFromParams(sp);
  const sec = secFromParams(sp);

  const setTab = useCallback(
    (t: TopTab) =>
      setSp((prev) => {
        const next = new URLSearchParams(prev);
        next.set("tab", t);
        return next;
      }),
    [setSp],
  );

  const setSec = useCallback(
    (i: number, opts?: { replace?: boolean }) =>
      setSp(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.set("sec", String(i));
          return next;
        },
        { replace: opts?.replace ?? false },
      ),
    [setSp],
  );

  const navigate = useCallback(
    (target: NavTarget) =>
      setSp((prev) => {
        const next = new URLSearchParams(prev);
        if (target.top_tab) next.set("tab", target.top_tab);
        if (target.section_index != null) next.set("sec", String(target.section_index));
        return next;
      }),
    [setSp],
  );

  return { tab, sec, setTab, setSec, navigate };
}

// static /cockpit/* subpaths that are NOT workspace ids
const COCKPIT_RESERVED = new Set(["new"]);

/** Extract the workspace id from a `/cockpit/:id` editor pathname ("" off the
 *  editor: off-cockpit, the `/cockpit` list, or a reserved subpath like
 *  `/cockpit/new`). Lets the always-mounted shell chrome gate its cockpit
 *  queries by route. */
export function cockpitIdFromPath(pathname: string): string {
  const m = pathname.match(/^\/cockpit\/([^/]+)/);
  if (!m) return "";
  return COCKPIT_RESERVED.has(m[1]) ? "" : m[1];
}
