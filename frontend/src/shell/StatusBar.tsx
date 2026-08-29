import { useLocation, useSearchParams } from "react-router-dom";

import { useDraft, useSpec } from "../api/editor";
import { useScorecard } from "../api/evaluator";
import { useCockpitMeta } from "../api/workspace";
import { cockpitIdFromPath, tabFromParams } from "../cockpit/cockpitNav";
import { shaParam } from "../cockpit/historyNav";
import { type SaveState, useEditorStore } from "../cockpit/editorStore";
import { deadlineView } from "../routes/Deadline";
import styles from "./StatusBar.module.css";

const SAVE_LABEL: Record<SaveState, string> = {
  idle: "",
  saving: "Saving…",
  saved: "Saved",
  dirty: "Unsaved",
  error: "Save failed",
  "collab-connecting": "Connecting…",
  "collab-synced": "Synced",
  "collab-offline": "Offline — reconnecting…",
  "collab-readonly": "Read-only",
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
/** Git's own display length. The bar is one line; a 40-char sha would own it. */
function short(sha: string): string {
  return sha.slice(0, 7);
}

export function StatusBar() {
  const { pathname } = useLocation();
  const id = cockpitIdFromPath(pathname);
  const onCockpit = !!id;
  const { cursor, save } = useEditorStore();
  const [sp] = useSearchParams();
  const tab = tabFromParams(sp);
  // What the MIDDLE is showing. The cockpit can display the live draft, one
  // historical version, or two side by side, and the bar said nothing — you
  // could be reading a two-month-old version and not know it.
  //
  // No branch chip: every workspace repo is on `main` and nothing in the app
  // creates another, so it would be a permanent constant, not information.
  // Only the two EDITING tabs have a live artifact. Falling back to "draft" for
  // every non-spec tab made the bar say "Draft · a1b2c3d" on Scorecard and Panel
  // review — measured — and fetch the draft on tabs that do not show it.
  const editing = tab === "draft" || tab === "spec" ? tab : null;
  const comparing = tab === "diff";
  // Fetch the artifact ONLY when the middle is actually editing it: same
  // queryKey as the panel, so React Query dedupes rather than refetches.
  const wantLive = onCockpit && editing != null;
  const draft = useDraft(id, { enabled: wantLive && editing === "draft" });
  const spec = useSpec(id, { enabled: wantLive && editing === "spec" });
  const head = (editing === "spec" ? spec.data?.head : editing === "draft" ? draft.data?.head : null) ?? null;
  const a = shaParam(sp, "a");
  const b = shaParam(sp, "b");
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
      {onCockpit && comparing && (a || b) && (
        <span className={styles.item}>
          {a && b
            ? `Comparing ${short(a)} ↔ ${short(b)}`
            : `Viewing ${short((a ?? b) as string)} (read-only)`}
        </span>
      )}
      {onCockpit && editing && head && (
        <span className={styles.item}>
          {editing === "spec" ? "Spec" : "Draft"} · {short(head)}
        </span>
      )}
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
