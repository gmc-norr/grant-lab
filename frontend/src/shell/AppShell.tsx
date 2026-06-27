import type { ComponentType } from "react";
import { Outlet, useLocation, useMatches } from "react-router-dom";

import { cockpitIdFromPath } from "../cockpit/cockpitNav";
import { ActivityBar } from "./ActivityBar";
import styles from "./AppShell.module.css";
import { Sidebar } from "./Sidebar";
import { StatusBar } from "./StatusBar";
import { TitleBar } from "./TitleBar";

type SidebarHandle = { Sidebar?: ComponentType };
type RightPaneHandle = { RightPane?: ComponentType };

// The persistent IDE shell: title / activity / [sidebar] / main / [right-pane] / status. A
// route opts into the contextual sidebar by declaring `handle: { Sidebar }` (a
// component). AppShell renders the deepest matched route that declares one (the
// Calls list -> CallsFilters, the Cockpit -> CockpitSidebar); routes without one
// (the 404) render full-width. A route may also declare `handle: { RightPane }`
// to mount a right-pane region (e.g. CockpitChat); the component renders its own
// landmark element.
export function AppShell() {
  const matches = useMatches();
  const { pathname } = useLocation();
  const SidebarContent = [...matches]
    .reverse()
    .map((m) => (m.handle as SidebarHandle | undefined)?.Sidebar)
    .find((S): S is ComponentType => Boolean(S));
  const RightPaneContent = [...matches]
    .reverse()
    .map((m) => (m.handle as RightPaneHandle | undefined)?.RightPane)
    .find((R): R is ComponentType => Boolean(R));

  // Per-workspace remount key for the WHOLE cockpit subtree (main Outlet + right pane).
  // A param-only nav /cockpit/A → /cockpit/B keeps this layout mounted and would
  // otherwise reconcile the same Cockpit/CockpitChat instances, leaking A's local
  // state into B (stale jobId/streamed turns → SSE targeting the wrong workspace;
  // stale bottomTab/collapsed/openPeek). Keying by the cockpit id forces a fresh
  // mount on every workspace change, resetting all of it. Non-cockpit routes resolve
  // to "" → a stable key so they reconcile normally.
  // The main and right-pane regions are SIBLINGS, so they need DISTINCT keys (a shared
  // key collides → React warns + reuses one). Both keys carry the same cockpit id, so
  // both regions remount together on a workspace change but reconcile on a query/section
  // change. Non-cockpit routes resolve to "" → stable per-region keys.
  const cockpitId = cockpitIdFromPath(pathname);
  const idSuffix = cockpitId ? `cockpit:${cockpitId}` : "shell";

  return (
    <div className={styles.shell}>
      <TitleBar />
      <div className={styles.body}>
        <ActivityBar />
        {SidebarContent && (
          <Sidebar>
            <SidebarContent />
          </Sidebar>
        )}
        <div className={styles.main} key={`main:${idSuffix}`}>
          <Outlet />
        </div>
        {RightPaneContent && <RightPaneContent key={`pane:${idSuffix}`} />}
      </div>
      <StatusBar />
    </div>
  );
}
