import { useCallback } from "react";
import { useSearchParams } from "react-router-dom";

import type { NavTarget, TopTab, View } from "./nav";
import { confirmDiscardIfDirty } from "./editorStore";

export const TOP_TABS: TopTab[] = ["draft", "spec", "scorecard", "review", "diff"];

/** The comparison destination, by every name it has had. `history` is the name
 *  it shipped under; links already exist. */
const COMPARISON_TABS: string[] = ["diff", "history"];

/** `?tab` parsed to a valid TopTab, defaulting to "draft".
 *
 *  `history` parses to `diff` — a parse-time ALIAS, not a redirect. The URL the
 *  user arrived on is not rewritten: doing so costs them the back button and
 *  hides which link was broken. */
export function tabFromParams(sp: URLSearchParams): TopTab {
  const t = sp.get("tab");
  if (t === "history") return "diff";
  return (TOP_TABS as string[]).includes(t ?? "") ? (t as TopTab) : "draft";
}

/** `?sec` parsed to a safe non-negative int, defaulting to 0.
 *
 *  Number.isSafeInteger, not just /^\d+$/: a long digit string parsed to a
 *  float — measured, twenty-five nines gave 1e+25 — which was then compared
 *  against real section indices and written back to the URL. */
export function secFromParams(sp: URLSearchParams): number {
  const s = sp.get("sec");
  if (s == null || !/^\d+$/.test(s)) return 0;
  const n = Number(s);
  return Number.isSafeInteger(n) ? n : 0;
}

export const VIEWS: View[] = ["outline", "versions", "references", "criteria", "members"];

/** `?view` parsed to a valid View.
 *
 *  Anything unrecognised normalises to the default rather than erroring,
 *  matching tabFromParams and artifactParam: a URL is user input. An explicit
 *  view always wins over the comparison default. */
export function viewFromParams(sp: URLSearchParams): View {
  const v = sp.get("view");
  if (v != null && (VIEWS as string[]).includes(v)) return v as View;
  return COMPARISON_TABS.includes(sp.get("tab") ?? "") ? "versions" : "outline";
}

/** A rail destination that changes the pane and NOTHING else.
 *
 *  A query-only `<Link to="?view=versions">` resolves to a search string that
 *  REPLACES the current one, silently dropping ?tab, ?sec, ?artifact, ?a, ?b
 *  and ?layout — clicking a rail icon would discard the comparison the user was
 *  looking at. Copies first, so the caller's params are not mutated. */
export function viewHref(
  sp: URLSearchParams,
  view: View,
  pathname: string,
): { pathname: string; search: string } {
  const next = new URLSearchParams(sp);
  next.set("view", view);
  // The comparison is reached FROM the Versions pane and belongs with it.
  // Leaving that pane while the middle shows the comparison used to leave you
  // with, say, the draft's Outline beside a diff — and the tab strip
  // highlighting nothing, because the comparison has no tab. Restore the middle
  // to the artifact that was being compared.
  //
  // The selection goes too: a pair left in the URL is the stale-pair trap
  // nextSelection guards, and "two versions picked" must not survive into a
  // state that is not the comparison.
  if (view !== "versions" && COMPARISON_TABS.includes(sp.get("tab") ?? "")) {
    next.set("tab", sp.get("artifact") === "spec" ? "spec" : "draft");
    next.delete("a");
    next.delete("b");
  }
  return { pathname, search: next.toString() };
}

export type CockpitNav = {
  tab: TopTab;
  sec: number;
  view: View;
  setTab: (t: TopTab) => void;
  setSec: (i: number, opts?: { replace?: boolean }) => void;
  setView: (v: View) => void;
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
  const view = viewFromParams(sp);

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

  // No dirty-editor guard: changing the PANE unmounts no editor. Only writers
  // of ?tab carry that obligation.
  const setView = useCallback(
    (v: View) =>
      setSp((prev) => {
        const next = new URLSearchParams(prev);
        next.set("view", v);
        return next;
      }),
    [setSp],
  );

  const navigate = useCallback(
    (target: NavTarget) => {
      // Changing ?tab unmounts the artifact editor and discards its unsaved
      // per-section buffers. That is not a router navigation, so the router
      // blocker cannot see it — the same discard Cockpit.onTab confirms for the
      // tab strip. navigate() is a SECOND writer of ?tab (every Criteria row
      // reaches it) and was silent: measured at zero window.confirm calls with
      // the editor dirty, the URL moving to tab=scorecard.
      //
      // Only when the tab actually changes: an Outline row jumping within the
      // tab it belongs to unmounts nothing, and prompting there would train the
      // user to dismiss the prompt that matters.
      //
      // Read from the render closure, not `prev`: window.confirm must never run
      // inside a setSearchParams updater, which is expected to be pure and runs
      // twice under StrictMode.
      if (target.top_tab && target.top_tab !== tab && !confirmDiscardIfDirty()) return;
      setSp((prev) => {
        const next = new URLSearchParams(prev);
        if (target.top_tab) next.set("tab", target.top_tab);
        if (target.section_index != null) next.set("sec", String(target.section_index));
        return next;
      });
    },
    [tab, setSp],
  );

  return { tab, sec, view, setTab, setSec, setView, navigate };
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
