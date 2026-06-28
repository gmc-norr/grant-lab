import type { ComponentType } from "react";
import { Outlet, useLocation, useMatches } from "react-router-dom";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";

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
//
// The sidebar/main/right-pane regions live inside a horizontal react-resizable
// PanelGroup so the side edges are draggable; widths persist via autoSaveId
// (localStorage, keyed by the set of panel ids → cockpit {sidebar,main,rightpane}
// and the list routes {sidebar,main} persist independently). The ActivityBar stays
// OUTSIDE the group (fixed 48px, like VS Code — never resizable).
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
  // change. Non-cockpit routes resolve to "" → stable per-region keys. The keys live on
  // the INNER content, NOT the <Panel>s, so a workspace switch never remounts the panels
  // (which would drop their persisted sizes).
  const cockpitId = cockpitIdFromPath(pathname);
  const idSuffix = cockpitId ? `cockpit:${cockpitId}` : "shell";

  return (
    <div className={styles.shell}>
      <TitleBar />
      <div className={styles.body}>
        <ActivityBar />
        <PanelGroup direction="horizontal" autoSaveId="cm-shell" className={styles.panes}>
          {SidebarContent && (
            <>
              <Panel id="sidebar" order={1} minSize={12} defaultSize={18}>
                <Sidebar>
                  <SidebarContent />
                </Sidebar>
              </Panel>
              <PanelResizeHandle className={styles.handle} aria-label="Resize sidebar" />
            </>
          )}
          <Panel id="main" order={2} minSize={30}>
            <div className={styles.main} key={`main:${idSuffix}`}>
              <Outlet />
            </div>
          </Panel>
          {RightPaneContent && (
            <>
              <PanelResizeHandle className={styles.handle} aria-label="Resize chat" />
              <Panel id="rightpane" order={3} minSize={15} defaultSize={22}>
                <RightPaneContent key={`pane:${idSuffix}`} />
              </Panel>
            </>
          )}
        </PanelGroup>
      </div>
      <StatusBar />
    </div>
  );
}
