import { useState } from "react";
import { useLocation } from "react-router-dom";

import { useLogout } from "../api/auth";
import { useSession } from "../api/session";
import { useCockpitMeta } from "../api/workspace";
import { cockpitIdFromPath } from "../cockpit/cockpitNav";
import { WorkspaceSettingsModal } from "../cockpit/WorkspaceSettingsModal";
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
  const [settingsOpen, setSettingsOpen] = useState(false);
  const isOwner = !!id && meta?.role === "owner";
  const email = useSession().data?.user.email;
  const logout = useLogout();
  const signOut = () =>
    logout.mutate(undefined, { onSuccess: () => window.location.assign("/login") });
  return (
    <header className={styles.titlebar}>
      <span className={styles.brand}>
        <BrandMark size={18} />
        <span className={styles.app}>Call Manager</span>
      </span>
      <span className={styles.title}>{title}</span>
      {isOwner && (
        <button
          type="button"
          className={styles.settingsGear}
          aria-label="Workspace settings"
          onClick={() => setSettingsOpen(true)}
        >
          ⚙
        </button>
      )}
      {isOwner && settingsOpen && (
        <WorkspaceSettingsModal id={id} opened onClose={() => setSettingsOpen(false)} />
      )}
      <span className={styles.account}>
        {email && <span className={styles.chip} title={email}>{email}</span>}
        <button type="button" className={styles.signout} onClick={signOut} disabled={logout.isPending}>
          Sign out
        </button>
      </span>
    </header>
  );
}
