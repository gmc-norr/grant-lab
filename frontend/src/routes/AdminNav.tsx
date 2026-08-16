import { NavLink } from "react-router-dom";

import styles from "./AdminNav.module.css";

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
      {LINKS.map((l) => (
        <NavLink key={l.to} to={l.to} end={l.to === "/admin"}
                 className={({ isActive }) => (isActive ? `${styles.link} ${styles.active}` : styles.link)}>
          {l.label}
        </NavLink>
      ))}
    </nav>
  );
}
