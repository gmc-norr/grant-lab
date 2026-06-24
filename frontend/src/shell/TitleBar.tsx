import { useLocation } from "react-router-dom";

import { useCockpitMeta } from "../api/workspace";
import { cockpitIdFromPath } from "../cockpit/cockpitNav";
import { BrandMark } from "./BrandMark";
import styles from "./TitleBar.module.css";

export function TitleBar() {
  const { pathname } = useLocation();
  const id = cockpitIdFromPath(pathname);
  const meta = useCockpitMeta(id).data;
  // Editor → workspace title (falls back until it loads). Non-editor cockpit
  // routes get their own labels; everything else is the dashboard. useCockpitMeta
  // is gated (id===""), so the non-editor branches never fetch.
  let title: string;
  if (id) title = meta?.workspace_title ?? "Proposal workspace";
  else if (pathname === "/cockpit/new") title = "New workspace";
  else if (pathname.startsWith("/cockpit")) title = "Workspaces";
  else title = "Funding calls";
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
