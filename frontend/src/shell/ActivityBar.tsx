import type { MouseEvent } from "react";
import { Tooltip } from "@mantine/core";
import { Link, NavLink, useLocation, useSearchParams } from "react-router-dom";

import { useSession } from "../api/session";
import { cockpitIdFromPath, viewFromParams, viewHref } from "../cockpit/cockpitNav";
import styles from "./ActivityBar.module.css";
import { ACTIVITY_BOTTOM, ACTIVITY_TOP, ACTIVITY_WORKSPACE, type ActivityItem } from "./nav-items";

function Glyph({ icon }: { icon: string }) {
  return (
    <span className={styles.icon} aria-hidden="true">
      <span className={`codicon codicon-${icon}`} />
    </span>
  );
}

/** A modified or non-primary click belongs to the BROWSER — "open in a new tab",
 *  "open in a new window". The rail's sidebar behaviour must not swallow it, and
 *  must not preventDefault it. This matters more here than in most apps: the
 *  design's answer to "several workspaces at once" is browser tabs, so
 *  Cmd/Ctrl-clicking a rail destination is a first-class workflow. */
function isBrowserClick(e: MouseEvent<HTMLAnchorElement>) {
  return e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0;
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
    if (isBrowserClick(e)) return;
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

function WorkspaceBand({
  pathname,
  onToggleSidebar,
  onRevealSidebar,
}: {
  pathname: string;
  onToggleSidebar?: () => void;
  onRevealSidebar?: () => void;
}) {
  const [sp] = useSearchParams();
  const view = viewFromParams(sp);
  return (
    <>
      <div className={styles.divider} data-testid="activity-divider" />
      <div className={styles.group}>
        {ACTIVITY_WORKSPACE.map((it) => {
          const active = it.id === view;
          return (
            <Tooltip key={it.id} label={it.label} position="right" withArrow>
              <Link
                to={viewHref(sp, it.id, pathname)}
                aria-label={it.label}
                aria-current={active ? "page" : undefined}
                className={active ? `${styles.item} ${styles.active}` : styles.item}
                onClick={(e) => {
                  if (isBrowserClick(e)) return;
                  // Same rule as the app band: clicking the ALREADY-ACTIVE item
                  // collapses the pane instead of navigating. preventDefault
                  // stops react-router's Link nav, so ?tab/?sec survive.
                  if (active && onToggleSidebar) {
                    e.preventDefault();
                    onToggleSidebar();
                    return;
                  }
                  // An INACTIVE click changes ?view, and the pane may be
                  // collapsed — in which case the highlight moved and nothing
                  // appeared. Reveal (never toggle: toggling would collapse an
                  // open pane) and let the navigation proceed.
                  onRevealSidebar?.();
                }}
              >
                <Glyph icon={it.icon} />
              </Link>
            </Tooltip>
          );
        })}
      </div>
    </>
  );
}

export function ActivityBar({
  onToggleSidebar,
  onRevealSidebar,
}: {
  onToggleSidebar?: () => void;
  /** Expand the pane if it is collapsed; a no-op when it is already open.
   *  Deliberately NOT the toggle — see WorkspaceBand's inactive-click branch. */
  onRevealSidebar?: () => void;
}) {
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
          <WorkspaceBand
            pathname={pathname}
            onToggleSidebar={onToggleSidebar}
            onRevealSidebar={onRevealSidebar}
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
