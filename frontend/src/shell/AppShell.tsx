import { Outlet } from "react-router-dom";

import { ActivityBar } from "./ActivityBar";
import styles from "./AppShell.module.css";
import { StatusBar } from "./StatusBar";
import { TitleBar } from "./TitleBar";

// The persistent IDE shell. Phase 1 ships title / activity / main / status.
// The contextual sidebar column is added in Phase 2 (Calls filters) / Phase 3
// (cockpit outline). Route bodies render unchanged inside <Outlet/>.
export function AppShell() {
  return (
    <div className={styles.shell}>
      <TitleBar />
      <div className={styles.body}>
        <ActivityBar />
        <div className={styles.main}>
          <Outlet />
        </div>
      </div>
      <StatusBar />
    </div>
  );
}
