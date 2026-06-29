import { useLocation } from "react-router-dom";

import { useScorecard } from "../api/evaluator";
import { useCockpitMeta } from "../api/workspace";
import { cockpitIdFromPath } from "../cockpit/cockpitNav";
import { type SaveState, useEditorStore } from "../cockpit/editorStore";
import { deadlineView } from "../routes/Deadline";
import styles from "./StatusBar.module.css";

const SAVE_LABEL: Record<SaveState, string> = {
  idle: "",
  saving: "Saving…",
  saved: "Saved",
  dirty: "Unsaved",
  error: "Save failed",
};
const BAND_LABEL: Record<string, string> = {
  strong: "Strong",
  adequate: "Adequate",
  at_risk: "At-risk",
  gap: "Gap",
};
const DEADLINE_CLASS: Record<string, string | undefined> = {
  soon: styles.deadlineSoon,
  watch: styles.deadlineWatch,
};

// The global status bar gains cockpit context on /cockpit: workspace deadline, spec-lock,
// scorecard band (this plan) + save-state, language, Ln/Col (Phase 3a). It derives the
// workspace id from the URL and gates its queries (enabled:!!id), so it never fetches off
// the cockpit — the same global-channel discipline the rest of the cockpit uses.
export function StatusBar() {
  const { pathname } = useLocation();
  const id = cockpitIdFromPath(pathname);
  const onCockpit = !!id;
  const { cursor, save } = useEditorStore();
  const meta = useCockpitMeta(id).data;
  const scorecard = useScorecard(id).data;
  const dl = meta?.workspace_deadline ? deadlineView(meta.workspace_deadline) : null;
  const band = scorecard?.scorecard?.overall_band;

  return (
    <footer className={styles.statusbar} aria-label="Status bar">
      <span className={styles.item}>Grant Lab</span>
      {onCockpit && dl && (
        <span className={`${styles.item} ${DEADLINE_CLASS[dl.state] ?? ""}`}>
          {dl.state === "closed" ? `Closed · ${dl.date}` : `Due ${dl.date}`}
        </span>
      )}
      {onCockpit && meta?.spec_locked && <span className={styles.item}>🔒 Spec locked</span>}
      {onCockpit && band && <span className={styles.item}>Band: {BAND_LABEL[band] ?? band}</span>}
      <span className={styles.spacer} />
      {onCockpit && (
        <>
          {SAVE_LABEL[save] && <span className={styles.item}>{SAVE_LABEL[save]}</span>}
          <span className={styles.item}>Markdown</span>
          {cursor && (
            <span className={styles.item}>
              Ln {cursor.line}, Col {cursor.col}
            </span>
          )}
        </>
      )}
    </footer>
  );
}
