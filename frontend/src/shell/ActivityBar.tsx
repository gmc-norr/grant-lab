import type { MouseEvent } from "react";
import { Tooltip } from "@mantine/core";
import { Link, NavLink, useLocation } from "react-router-dom";

import { useSession } from "../api/session";
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

function Item({
  item,
  active,
  onToggleSidebar,
}: {
  item: ActivityItem;
  active?: boolean;
  onToggleSidebar?: () => void;
}) {
  const glyph = <Glyph icon={item.icon} />;
  // VS-Code behaviour: clicking the ALREADY-ACTIVE item toggles the sidebar instead
  // of navigating. preventDefault stops react-router's Link nav (it honours
  // defaultPrevented) so we don't re-navigate to the bare route and drop ?tab/?sec.
  // Only fires when this route actually has a sidebar (onToggleSidebar provided).
  const handleClick = (e: MouseEvent<HTMLAnchorElement>) => {
    if (active && onToggleSidebar) {
      e.preventDefault();
      onToggleSidebar();
    }
  };
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
        onClick={handleClick}
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

export function ActivityBar({ onToggleSidebar }: { onToggleSidebar?: () => void }) {
  const { pathname } = useLocation();
  const { data: session } = useSession();
  const isAdmin = Boolean(session?.capabilities.admin);
  const wsId = cockpitIdFromPath(pathname);
  const onCalls = pathname === "/";
  const onCockpitList = pathname.startsWith("/cockpit") && !wsId;
  const bottom = ACTIVITY_BOTTOM.filter((it) => it.id !== "admin" || isAdmin);
  return (
    <nav className={styles.activitybar} aria-label="Primary">
      <div className={styles.group}>
        {ACTIVITY_TOP.map((it) =>
          it.id === "workspaces" ? (
            <Item key={it.id} item={it} active={onCockpitList} onToggleSidebar={onToggleSidebar} />
          ) : it.id === "calls" ? (
            <Item key={it.id} item={it} active={onCalls} onToggleSidebar={onToggleSidebar} />
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
            active
            onToggleSidebar={onToggleSidebar}
          />
        )}
      </div>
      <div className={styles.spacer} />
      <div className={styles.group}>
        {bottom.map((it) => (
          <Item key={it.id} item={it} />
        ))}
      </div>
    </nav>
  );
}
