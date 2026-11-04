import { Menu, UnstyledButton } from "@mantine/core";
import { Link, NavLink, Outlet } from "react-router-dom";

import { ApiError } from "../api/client";
import { useLogout } from "../api/auth";
import { BrandMark } from "./BrandMark";
import { useSession } from "../api/session";
// The light ground. Imported here so its cascade order is fixed: it must come
// before the CSS module so the module's own rules win the cascade.
import "../theme/theme.match.css";
import styles from "./SimpleShell.module.css";

// The app shell: a top bar with the destinations and the account menu, then the
// page.
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
  // Sign-out. Without this item the app has NO way to end a session, on shared
  // hospital workstations. A hard navigation on success, so every cached query
  // is dropped with the document.
  const logout = useLogout();
  const signOut = () => {
    void logout;
    window.location.assign("/login");
  };

  return (
    <div className={styles.shell}>
      <header className={styles.topbar}>
        {/* The capped row inside the full-bleed bar. A plain div, not a
            landmark: <header> already is one, and nesting a second changes
            nothing for a screen reader but adds a stop to tab through. */}
        <div className={styles.topbarInner}>
          {/* GRANT LAB LEADS, Call Match names the edition. Decided 2026-09-08;
              index.html's comment had flagged this as "a naming decision for the
              humans shipping release 1, not a defect to patch".

              Every other surface already said Grant Lab — the login screen, the
              corridor board, the alert emails' [Grant Lab] subject prefix, the
              OpenAPI title and the browser tab. This header was the only place
              that said something else, and the only one carrying no BrandMark,
              so a user went logo+Grant Lab (login) -> bare "Call Match" (app)
              and back to Grant Lab on the board.

              Only "Grant Lab" is the LINK. "Call Match" is a label, not a
              destination, so putting it inside the anchor would promise a page
              that does not exist and make the link's accessible name read
              "Grant Lab Call Match". The mark is aria-hidden because it carries
              aria-label="Grant Lab" of its own and would otherwise say the name
              twice. */}
          <Link to="/" className={styles.brand}>
            <span aria-hidden="true" className={styles.brandMark}>
              <BrandMark size={18} />
            </span>
            Grant Lab
          </Link>
          <span className={styles.edition}>Call Match</span>
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
                className={`${styles.link} ${styles.account}`}
                aria-label={
                  email ? `Account menu for ${email}` : "Account menu"
                }
              >
                <span className={styles.accountEmail}>{email ?? "Account"}</span>
                <span className={`codicon codicon-account ${styles.accountIcon}`} aria-hidden="true" />
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
        {/* A FAILED SIGN-OUT USED TO BE SILENT (review 2026-10-26, A8): on a 500
            the measurement was `url=/ alerts=[]` — the member stays on the page
            with the session still live and nothing on screen says so. On a
            shared hospital workstation that is the worst silence in the app.
            Read off the mutation's own state rather than an onError callback, so
            it survives a re-render and clears itself on the next attempt. */}
        {logout.isError && (
          <div role="alert" className={styles.signOutError}>
            {/* The generic sentence ALWAYS, the server's own detail after it.
                apiErrorMessage alone returns the server sentence and drops the
                context, so a 500 read as a bare "the session store is down"
                with nothing saying the sign-out had failed. */}
            Couldn&apos;t sign out — you are still signed in. Please try again.
            {logout.error instanceof ApiError && ` (${logout.error.userMessage})`}
          </div>
        )}
      </header>
      {/* A plain div, NOT <main>: every page renders its own <main> landmark
          (CallDetail, Settings, ProfilesList, AdminUsers, RequireAdmin, ...),
          and a wrapper here nests them. */}
      <div className={styles.content}>
        <Outlet />
      </div>
    </div>
  );
}
