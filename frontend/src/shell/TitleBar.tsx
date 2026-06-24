import { useLocation } from "react-router-dom";

import { useCockpitMeta } from "../api/workspace";
import { cockpitIdFromPath } from "../cockpit/cockpitNav";
import { BrandMark } from "./BrandMark";
import styles from "./TitleBar.module.css";

export function TitleBar() {
  const { pathname } = useLocation();
  const id = cockpitIdFromPath(pathname);
  const meta = useCockpitMeta(id).data;
  // On the cockpit, the breadcrumb is the workspace title (falls back until it loads);
  // off the cockpit it's the dashboard label. useCockpitMeta is gated (id===""), so the
  // off-cockpit branch never fetches.
  const title = id ? (meta?.workspace_title ?? "Proposal workspace") : "Funding calls";
  return (
    <header className={styles.titlebar}>
      <span className={styles.brand}>
        <BrandMark size={18} />
        <span className={styles.app}>Call Manager</span>
      </span>
      <span className={styles.title}>{title}</span>
    </header>
  );
}
