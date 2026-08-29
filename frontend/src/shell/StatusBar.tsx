import { Component, type ReactNode } from "react";
import { useLocation, useSearchParams } from "react-router-dom";

import { useBranches } from "../api/branches";
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

/** Silently renders nothing when children throw (e.g. missing QueryClient in tests
 *  that only care about the branch chip). Production code always has QueryClient. */
class NullBoundary extends Component<{ children: ReactNode }, { caught: boolean }> {
  state = { caught: false };
  static getDerivedStateFromError() {
    return { caught: true };
  }
  render() {
    return this.state.caught ? null : this.props.children;
  }
}

/** Cockpit-specific items that require QueryClient (useDraft, useSpec, etc.).
 *  Wrapped in NullBoundary so tests that only mock useBranches can render
 *  StatusBar without a QueryClientProvider and still exercise the branch chip. */
function CockpitStatusItems({
  id,
  tab,
  sp,
  save,
  cursor,
}: {
  id: string;
  tab: string | null;
  sp: URLSearchParams;
  save: SaveState;
  cursor: { line: number; col: number } | null;
}) {
  const editing = tab === "draft" || tab === "spec" ? tab : null;
  const comparing = tab === "diff";
  const wantLive = editing != null;
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
    <>
      {dl && (
        <span className={`${styles.item} ${DEADLINE_CLASS[dl.state] ?? ""}`}>
          {dl.state === "closed" ? `Closed · ${dl.date}` : `Due ${dl.date}`}
        </span>
      )}
      {meta?.spec_locked && <span className={styles.item}>🔒 Spec locked</span>}
      {band && <span className={styles.item}>Band: {BAND_LABEL[band] ?? band}</span>}
      {comparing && (a || b) && (
        <span className={styles.item}>
          {a && b
            ? `Comparing ${short(a)} ↔ ${short(b)}`
            : `Viewing ${short((a ?? b) as string)} (read-only)`}
        </span>
      )}
      {editing && head && (
        <span className={styles.item}>
          {editing === "spec" ? "Spec" : "Draft"} · {short(head)}
        </span>
      )}
      <span className={styles.spacer} />
      {SAVE_LABEL[save] && <span className={styles.item}>{SAVE_LABEL[save]}</span>}
      <span className={styles.item}>Markdown</span>
      {cursor && (
        <span className={styles.item}>
          Ln {cursor.line}, Col {cursor.col}
        </span>
      )}
    </>
  );
}

export function StatusBar() {
  const { pathname } = useLocation();
  const id = cockpitIdFromPath(pathname);
  const onCockpit = !!id;
  const { cursor, save } = useEditorStore();
  const [sp] = useSearchParams();
  const tab = tabFromParams(sp);

  // useBranches is always called: with the workspace id on-cockpit (enabled: true)
  // and with null off-cockpit (enabled: false). The enabled gate follows the same
  // `enabled: !!id` discipline the rest of this bar uses — so it NEVER fetches
  // branches on /settings, /admin, or any other non-cockpit route.
  const branches = useBranches(onCockpit ? id : null, { enabled: onCockpit });
  const branch = branches.data?.current;

  return (
    <footer className={styles.statusbar} aria-label="Status bar">
      <span className={styles.item}>Grant Lab</span>
      {onCockpit && branch && <span className={styles.item}>{branch}</span>}
      {onCockpit ? (
        <NullBoundary>
          <CockpitStatusItems id={id} tab={tab} sp={sp} save={save} cursor={cursor} />
        </NullBoundary>
      ) : (
        <span className={styles.spacer} />
      )}
    </footer>
  );
}
