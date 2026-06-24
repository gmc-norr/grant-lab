import { Tooltip } from "@mantine/core";
import { Link, NavLink, useLocation } from "react-router-dom";

import { cockpitIdFromPath } from "../cockpit/cockpitNav";
import styles from "./ActivityBar.module.css";
import { ACTIVITY_BOTTOM, ACTIVITY_TOP, type ActivityItem } from "./nav-items";

function Glyph({ icon }: { icon: string }) {
  return (
    <span className={styles.icon} aria-hidden="true">
      <span className={`codicon codicon-${icon}`} />
    </span>
  );
}

function Item({ item, active }: { item: ActivityItem; active?: boolean }) {
  const glyph = <Glyph icon={item.icon} />;
  let body;
  if (item.kind !== "spa") {
    body = (
      <a href={item.to} aria-label={item.label} className={styles.item}>
        {glyph}
      </a>
    );
  } else if (active !== undefined) {
    // Caller-controlled active state: a plain prefix-matching NavLink would mark
    // Workspaces active across the whole /cockpit subtree, double-lighting it with
    // the Current-workspace chip on /cockpit/:id. Drive it explicitly instead.
    const cls = active ? `${styles.item} ${styles.active}` : styles.item;
    body = (
      <Link
        to={item.to}
        aria-label={item.label}
        aria-current={active ? "page" : undefined}
        className={cls}
      >
        {glyph}
      </Link>
    );
  } else {
    body = (
      <NavLink
        to={item.to}
        end={item.to === "/"}
        aria-label={item.label}
        className={({ isActive }) =>
          isActive ? `${styles.item} ${styles.active}` : styles.item
        }
      >
        {glyph}
      </NavLink>
    );
  }
  return (
    <Tooltip label={item.label} position="right" withArrow>
      {body}
    </Tooltip>
  );
}

export function ActivityBar() {
  const { pathname } = useLocation();
  const wsId = cockpitIdFromPath(pathname);
  // Workspaces (the list) is active on the non-editor cockpit routes; on the
  // editor the Current-workspace chip is the active item instead.
  const onCockpitList = pathname.startsWith("/cockpit") && !wsId;
  return (
    <nav className={styles.activitybar} aria-label="Primary">
      <div className={styles.group}>
        {ACTIVITY_TOP.map((it) =>
          it.id === "workspaces" ? (
            <Item key={it.id} item={it} active={onCockpitList} />
          ) : (
            <Item key={it.id} item={it} />
          ),
        )}
        {wsId && (
          <Item
            item={{
              id: "active-ws",
              label: "Current workspace",
              icon: "edit",
              kind: "spa",
              to: `/cockpit/${wsId}`,
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
