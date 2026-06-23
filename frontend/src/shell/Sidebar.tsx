import type { ReactNode } from "react";

import styles from "./Sidebar.module.css";

// Thin shell wrapper providing the sidebar chrome (surface, right border,
// scroll) around whatever the active route supplies via its `handle.Sidebar`.
export function Sidebar({ children }: { children: ReactNode }) {
  return (
    <aside className={styles.sidebar} aria-label="Sidebar">
      {children}
    </aside>
  );
}
