import { Menu, UnstyledButton } from "@mantine/core";
import { Link, NavLink, Outlet } from "react-router-dom";

import { useLogout } from "../api/auth";
import { useSession } from "../api/session";
// The match edition's light ground. Imported HERE, not in main.tsx, so it enters
// the module graph only through router.simple.tsx — the full build never sees it.
// Must come before the CSS module so the module's own rules win the cascade.
import "../theme/theme.match.css";
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
  // `end` is INERT for to="/" in this react-router version, measured: NavLink
  // computes isActive as `to === pathname || (!end && pathname.startsWith(to)
  // && pathname.charAt(to.length) === "/")`, and for to="/" that last char is
  // the first letter of the segment, never "/". Flipping this to false changes
  // nothing and reddens no test. Kept because it states the intent and would
  // become load-bearing the moment this entry stops being the root.
  { to: "/", label: "Calls", end: true, adminOnly: false },
  { to: "/profiles", label: "Profiles", end: false, adminOnly: false },
  { to: "/boards", label: "Boards", end: false, adminOnly: false },
  { to: "/admin", label: "Admin", end: false, adminOnly: true },
] as const;

export function SimpleShell() {
  // GET /api/me. `capabilities.admin` is the admin flag the SPA reads —
  // RequireAdmin.tsx:13 uses the same field. `user.is_admin` exists on the wire
  // schema but no component reads it; do not introduce a second source.
  const { data: session } = useSession();
  const isAdmin = Boolean(session?.capabilities.admin);
  const email = session?.user.email;
  // Sign-out. The full edition's only sign-out control lives in TitleBar.tsx,
  // which check_match_bundle.mjs excludes from the match graph — so without this
  // item release 1 ships with NO way to end a session, on shared hospital
  // workstations. Same call shape as TitleBar.tsx:38-39 (hard navigation on
  // success, so every cached query is dropped with the document), deliberately
  // NOT an import of TitleBar itself: that would put the excluded module back in
  // the match bundle and redden gate 1.
  const logout = useLogout();
  const signOut = () =>
    logout.mutate(undefined, {
      onSuccess: () => window.location.assign("/login"),
    });

  return (
    <div className={styles.shell}>
      <header className={styles.topbar}>
        {/* The capped row inside the full-bleed bar. A plain div, not a
            landmark: <header> already is one, and nesting a second changes
            nothing for a screen reader but adds a stop to tab through. */}
        <div className={styles.topbarInner}>
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
              {/* The accessible name must CONTAIN the visible text (WCAG 2.5.3,
                label-in-name) or a voice-control user cannot say what they see.
                A bare aria-label="Account menu" would replace the email
                outright. */}
              <UnstyledButton
                className={styles.link}
                aria-label={
                  email ? `Account menu for ${email}` : "Account menu"
                }
              >
                {email ?? "Account"}
              </UnstyledButton>
            </Menu.Target>
            <Menu.Dropdown>
              <Menu.Item component={Link} to="/me">
                Settings
              </Menu.Item>
              <Menu.Item onClick={signOut} disabled={logout.isPending}>
                Sign out
              </Menu.Item>
            </Menu.Dropdown>
          </Menu>
        </div>
      </header>
      {/* A plain div, NOT <main>: every release-1 page renders its own <main>
          landmark (Dashboard.tsx:18, CallDetail.tsx:31, Settings.tsx:39,
          ProfilesList.tsx:17, AdminUsers.tsx:83, RequireAdmin.tsx:14, ...), and
          a wrapper here nests them. AppShell.tsx:142 is a div for the same
          reason. */}
      <div className={styles.content}>
        <Outlet />
      </div>
    </div>
  );
}
