import { NavLink } from "react-router-dom";

import { isMatchEdition } from "../edition";
import styles from "./AdminNav.module.css";

// Release 1 mounts only /admin, /admin/users and /admin/users/pending
// (router.simple.tsx). Linking to the others would render tabs that resolve to
// the in-app soft 404. These are GATED, not deleted — the full edition still
// needs them.
//
// noCockpitLinks.test.tsx EXEMPTs this file: a source scan cannot tell a gated
// string literal from an ungated one, so both halves of this gate are pinned by
// AdminNav.test.tsx instead — the match case that the three are absent, and the
// full case that all six render.
const MATCH_EXCLUDED = ["/admin/drafting", "/admin/golden-set", "/admin/deleted-workspaces"];

const LINKS: { to: string; label: string }[] = [
  { to: "/admin", label: "Operational health" },
  { to: "/admin/drafting", label: "Drafting" },
  { to: "/admin/golden-set", label: "Golden set" },
  { to: "/admin/users", label: "Users" },
  { to: "/admin/users/pending", label: "Pending" },
  { to: "/admin/deleted-workspaces", label: "Deleted workspaces" },
];

export function AdminNav() {
  return (
    <nav className={styles.nav} aria-label="Admin sections">
      {LINKS.filter((l) => !isMatchEdition || !MATCH_EXCLUDED.includes(l.to)).map((l) => (
        <NavLink key={l.to} to={l.to} end={l.to === "/admin"}
                 className={({ isActive }) => (isActive ? `${styles.link} ${styles.active}` : styles.link)}>
          {l.label}
        </NavLink>
      ))}
    </nav>
  );
}
