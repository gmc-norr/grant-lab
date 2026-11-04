import { NavLink } from "react-router-dom";

import styles from "./AdminNav.module.css";

// The admin sections router.tsx mounts. A link to anything else would render
// the in-app soft 404; AdminNav.test.tsx walks the router to keep the two in step.
const LINKS: { to: string; label: string }[] = [
  { to: "/admin", label: "Operational health" },
  { to: "/admin/golden-set", label: "Golden set" },
  { to: "/admin/users", label: "Users" },
  { to: "/admin/users/pending", label: "Pending" },
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
