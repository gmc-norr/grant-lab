import { useLocation } from "react-router-dom";

import { type SaveState, useEditorStore } from "../cockpit/editorStore";
import styles from "./StatusBar.module.css";

const SAVE_LABEL: Record<SaveState, string> = {
  idle: "",
  saving: "Saving…",
  saved: "Saved",
  dirty: "Unsaved",
  error: "Save failed",
};

// Phase 3a: a global status bar that gains cockpit context (save-state, language,
// Ln/Col) on the /cockpit route. Deadline, spec-lock, and scorecard band are wired
// in Plan 3b. Reads the editorStore (published by the mounted editor) + the URL —
// the same global-channel pattern the rest of the cockpit uses across the shell.
export function StatusBar() {
  const onCockpit = useLocation().pathname.startsWith("/cockpit");
  const { cursor, save } = useEditorStore();
  return (
    <footer className={styles.statusbar} aria-label="Status bar">
      <span className={styles.item}>Call Manager</span>
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
