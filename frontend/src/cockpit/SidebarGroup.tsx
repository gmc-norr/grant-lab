import { type ReactNode, useState } from "react";

import styles from "./CockpitSidebar.module.css";

// A collapsible VS Code-style sidebar group: codicon chevron + title + count.
export function SidebarGroup({
  title,
  count,
  defaultOpen = true,
  children,
}: {
  title: string;
  count?: number;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className={styles.group}>
      <button
        type="button"
        className={styles.groupHeader}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className={`codicon codicon-chevron-${open ? "down" : "right"}`} aria-hidden="true" />
        <span className={styles.groupTitle}>{title}</span>
        {count != null && <span className={styles.groupCount}>{count}</span>}
      </button>
      {open && <div className={styles.groupBody}>{children}</div>}
    </section>
  );
}
