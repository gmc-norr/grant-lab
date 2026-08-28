import { type ReactNode, useEffect, useRef, useState } from "react";

import styles from "./CockpitSidebar.module.css";

// A collapsible VS Code-style sidebar group: codicon chevron + title + count.
export function SidebarGroup({
  title,
  count,
  defaultOpen = true,
  openWhen,
  children,
}: {
  title: string;
  count?: number;
  defaultOpen?: boolean;
  /** Unfold, once, when this becomes true — for a group the active view depends
   *  on. Not a controlled `open`: the user can fold it again and it stays
   *  folded, because the effect fires on the TRANSITION, not on every render. */
  openWhen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!openWhen) return;
    setOpen(true);
    // A collapsed fourth group can sit below the fold of a scrolling sidebar,
    // and unfolding something nobody can see is not an improvement.
    // `block: "nearest"` so an already-visible group does not jump.
    ref.current?.scrollIntoView?.({ block: "nearest" });
  }, [openWhen]);
  return (
    <section className={styles.group} ref={ref}>
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
