import { Outlet } from "react-router-dom";

import { BrandMark } from "../shell/BrandMark";
import styles from "./PreAuth.module.css";

// SECOND top-level pathless layout (sibling to AppShell). Centered card on the
// app background, NO ActivityBar / Sidebar / StatusBar. INVARIANT: this layout
// and its pages make NO authenticated /api call on mount — a useSession here
// would 401 -> unwrap -> redirectToLogin -> infinite loop. The public
// /api/auth/config call lives in the pages (Login/Register), not here.
export function PreAuthLayout() {
  return (
    <div className={styles.wrap}>
      <main className={styles.card}>
        <span className={styles.brand}>
          <BrandMark size={36} />
        </span>
        <Outlet />
      </main>
    </div>
  );
}
