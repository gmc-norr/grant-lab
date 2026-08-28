import { useSearchParams } from "react-router-dom";

import { useDraft, useSpec } from "../../api/editor";
import { tabFromParams } from "../cockpitNav";
import { useCockpitNav } from "../cockpitNav";
import styles from "../CockpitSidebar.module.css";
import { useEditorDirty } from "../editorStore";
import { requestSectionJump } from "../sectionJump";
import { PaneHeader } from "./PaneHeader";

export function OutlinePane({ id }: { id: string }) {
  const [sp] = useSearchParams();
  const tab = tabFromParams(sp);
  const artifact: "draft" | "spec" = tab === "spec" ? "spec" : "draft";
  const draft = useDraft(id);
  const spec = useSpec(id);
  const q = artifact === "spec" ? spec : draft;
  // Subscribe to ONLY the dirty set (not cursor) so a caret move does not re-render
  // the whole sidebar (the cursor updates on every keystroke).
  const dirty = useEditorDirty();
  const sections = q.data?.sections ?? [];

  const { sec, navigate } = useCockpitNav();

  return (
    <>
      <PaneHeader title="Outline" count={sections.length || undefined} />
      {q.isLoading && <p className={styles.note}>Loading…</p>}
      {!q.isLoading && sections.length === 0 && <p className={styles.note}>No sections.</p>}
      <ul className={styles.outline} aria-label="Outline">
        {sections.map((s) => (
          <li key={s.index}>
            <button
              type="button"
              className={styles.row}
              aria-current={sec === s.index}
              onClick={() => {
                // The URL keeps the active-section STATE (the legacy editor
                // reads it, and it drives aria-current); the jump is the
                // EVENT, so re-clicking the section you are already on still
                // scrolls you back to it. See cockpit/sectionJump.ts.
                navigate({ top_tab: artifact, section_index: s.index });
                requestSectionJump(s.index);
              }}
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
    </>
  );
}
