import type { ComponentType } from "react";
import { Outlet, useMatches } from "react-router-dom";

import { ActivityBar } from "./ActivityBar";
import styles from "./AppShell.module.css";
import { Sidebar } from "./Sidebar";
import { StatusBar } from "./StatusBar";
import { TitleBar } from "./TitleBar";

type SidebarHandle = { Sidebar?: ComponentType };

// The persistent IDE shell: title / activity / [sidebar] / main / status. A
// route opts into the contextual sidebar by declaring `handle: { Sidebar }` (a
// component). AppShell renders the deepest matched route that declares one;
// routes without one (Cockpit, the 404) render full-width.
export function AppShell() {
  const matches = useMatches();
  const SidebarContent = [...matches]
    .reverse()
    .map((m) => (m.handle as SidebarHandle | undefined)?.Sidebar)
    .find((S): S is ComponentType => Boolean(S));

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
        <div className={styles.main}>
          <Outlet />
        </div>
      </div>
      <StatusBar />
    </div>
  );
}
