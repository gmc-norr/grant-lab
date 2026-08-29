import { useParams, useSearchParams } from "react-router-dom";

import { viewFromParams } from "./cockpitNav";
import styles from "./CockpitSidebar.module.css";
import { BranchPicker } from "./panes/BranchPicker";
import { CriteriaPane } from "./panes/CriteriaPane";
import { MembersPane } from "./panes/MembersPane";
import { OutlinePane } from "./panes/OutlinePane";
import { ReferencesPane } from "./panes/ReferencesPane";
import { VersionGraph } from "./VersionGraph";

// The cockpit's left sidebar (the route's handle.Sidebar). Renders exactly ONE
// view, chosen by ?view — the rail icon and the pane are the same decision.
// Each pane owns its own queries; React Query dedupes, so a pane that shares a
// query with the middle stays in sync with no props across the AppShell
// boundary.
export function CockpitSidebar() {
  const { id = "" } = useParams();
  const [sp] = useSearchParams();
  const view = viewFromParams(sp);
  return (
    <div className={styles.sidebar}>
      {view === "outline" && <OutlinePane id={id} />}
      {view === "versions" && (
        <>
          <BranchPicker id={id} />
          <VersionGraph id={id} />
        </>
      )}
      {view === "references" && <ReferencesPane id={id} />}
      {view === "criteria" && <CriteriaPane id={id} />}
      {view === "members" && <MembersPane id={id} />}
    </div>
  );
}
