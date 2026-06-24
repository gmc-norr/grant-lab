import { useParams } from "react-router-dom";

import { useDraft, useSpec } from "../api/editor";
import { useCockpitNav } from "./cockpitNav";
import styles from "./CockpitSidebar.module.css";
import { useEditorStore } from "./editorStore";
import { SidebarGroup } from "./SidebarGroup";

// The cockpit's left sidebar (the route's handle.Sidebar). Reads the SAME
// useDraft/useSpec queries + ?tab/?sec as the editor — React Query dedupes by key,
// the URL is the shared channel — so the Outline stays in sync with the editor across
// the AppShell boundary with no props. Dirty markers come from editorStore, which the
// mounted editor publishes. Both hooks are always called (stable hook order); on the
// scorecard/review tabs the Outline shows the Draft sections and a click navigates
// into Draft.
export function CockpitSidebar() {
  const { id = "" } = useParams();
  const { tab, sec, navigate } = useCockpitNav();
  const artifact: "draft" | "spec" = tab === "spec" ? "spec" : "draft";
  const draft = useDraft(id);
  const spec = useSpec(id);
  const q = artifact === "spec" ? spec : draft;
  const { dirty } = useEditorStore();
  const sections = q.data?.sections ?? [];

  return (
    <div className={styles.sidebar}>
      <SidebarGroup title="Outline" count={sections.length || undefined}>
        {q.isLoading && <p className={styles.note}>Loading…</p>}
        {!q.isLoading && sections.length === 0 && <p className={styles.note}>No sections.</p>}
        <ul className={styles.outline} aria-label="Outline">
          {sections.map((s) => (
            <li key={s.index}>
              <button
                type="button"
                className={styles.row}
                aria-current={sec === s.index}
                onClick={() => navigate({ top_tab: artifact, section_index: s.index })}
              >
                <span className={styles.rowLabel}>{s.heading || "preamble"}</span>
                {dirty.includes(s.index) && (
                  <span className={styles.dirty} aria-label="unsaved">
                    •
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>
      </SidebarGroup>
    </div>
  );
}
