import { Tooltip } from "@mantine/core";
import { NavLink, useLocation } from "react-router-dom";

import styles from "./ActivityBar.module.css";
import { ACTIVITY_BOTTOM, ACTIVITY_TOP, type ActivityItem } from "./nav-items";

function Glyph({ icon }: { icon: string }) {
  return (
    <span className={styles.icon} aria-hidden="true">
      <span className={`codicon codicon-${icon}`} />
    </span>
  );
}

function Item({ item }: { item: ActivityItem }) {
  const body =
    item.kind === "spa" ? (
      <NavLink
        to={item.to}
        end={item.to === "/"}
        aria-label={item.label}
        className={({ isActive }) =>
          isActive ? `${styles.item} ${styles.active}` : styles.item
        }
      >
        <Glyph icon={item.icon} />
      </NavLink>
    ) : (
      <a href={item.to} aria-label={item.label} className={styles.item}>
        <Glyph icon={item.icon} />
      </a>
    );
  return (
    <Tooltip label={item.label} position="right" withArrow>
      {body}
    </Tooltip>
  );
}

export function ActivityBar() {
  const { pathname } = useLocation();
  const inCockpit = pathname.startsWith("/cockpit");
  return (
    <nav className={styles.activitybar} aria-label="Primary">
      <div className={styles.group}>
        {ACTIVITY_TOP.map((it) => (
          <Item key={it.id} item={it} />
        ))}
        {inCockpit && (
          <Item
            item={{
              id: "active-ws",
              label: "Current workspace",
              icon: "edit",
              kind: "spa",
              to: pathname,
            }}
          />
        )}
      </div>
      <div className={styles.spacer} />
      <div className={styles.group}>
        {ACTIVITY_BOTTOM.map((it) => (
          <Item key={it.id} item={it} />
        ))}
      </div>
    </nav>
  );
}
