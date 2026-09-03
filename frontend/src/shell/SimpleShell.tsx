import { Menu, UnstyledButton } from "@mantine/core";
import { Link, NavLink, Outlet } from "react-router-dom";

import { useSession } from "../api/session";
import styles from "./SimpleShell.module.css";

// Release-1 shell. AppShell's IDE furniture (activity bar, status bar,
// resizable panes, bottom panel) exists to make the proposal-assist cockpit
// coherent; release 1 has no cockpit, so it carries none of it. Keeping the
// theme tokens means the two editions still look like one product.
const DESTINATIONS = [
  // adminOnly is declared on EVERY entry, not just the gated one: under
  // "as const" a heterogeneous array widens to a union, and reading d.adminOnly
  // is then a type error on the members that omit it (TS2339). vitest does not
  // typecheck, so only "pnpm build" catches that.
  { to: "/", label: "Calls", end: true, adminOnly: false },
  { to: "/profiles", label: "Profiles", end: false, adminOnly: false },
  { to: "/admin", label: "Admin", end: false, adminOnly: true },
] as const;

export function SimpleShell() {
  // GET /api/me. `capabilities.admin` is the admin flag the SPA reads —
  // RequireAdmin.tsx:13 uses the same field. `user.is_admin` exists on the wire
  // schema but no component reads it; do not introduce a second source.
  const { data: session } = useSession();
  const isAdmin = Boolean(session?.capabilities.admin);

  return (
    <div className={styles.shell}>
      <header className={styles.topbar}>
        <Link to="/" className={styles.brand}>
          Call Match
        </Link>
        <nav className={styles.nav} aria-label="Main">
          {DESTINATIONS.filter((d) => !d.adminOnly || isAdmin).map((d) => (
            <NavLink
              key={d.to}
              to={d.to}
              end={d.end}
              className={styles.link}
              // NavLink sets aria-current="page" itself when active; the test
              // asserts on that rather than on a class name.
            >
              {d.label}
            </NavLink>
          ))}
        </nav>
        <Menu position="bottom-end" withinPortal>
          <Menu.Target>
            <UnstyledButton className={styles.link} aria-label="Account menu">
              {session?.user.email ?? "Account"}
            </UnstyledButton>
          </Menu.Target>
          <Menu.Dropdown>
            <Menu.Item component={Link} to="/me">
              Settings
            </Menu.Item>
          </Menu.Dropdown>
        </Menu>
      </header>
      <main className={styles.content}>
        <Outlet />
      </main>
    </div>
  );
}
