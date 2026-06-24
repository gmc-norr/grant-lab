import { useParams } from "react-router-dom";

import { useReferences } from "../api/citations";
import { useDraft, useSpec } from "../api/editor";
import { type ScorecardCriterion, useScorecard } from "../api/evaluator";
import { useCockpitNav } from "./cockpitNav";
import styles from "./CockpitSidebar.module.css";
import { useEditorStore } from "./editorStore";
import { SidebarGroup } from "./SidebarGroup";

const BAND_CLASS: Record<ScorecardCriterion["band"], string> = {
  strong: "band-strong",
  adequate: "band-adequate",
  at_risk: "band-at-risk",
  gap: "band-gap",
};
const BAND_LABEL: Record<ScorecardCriterion["band"], string> = {
  strong: "Strong",
  adequate: "Adequate",
  at_risk: "At-risk",
  gap: "Gap",
};

// The cockpit's left sidebar (the route's handle.Sidebar). Reads the SAME queries +
// ?tab/?sec as the editor/panels — React Query dedupes, the URL is the shared channel —
// so everything stays in sync across the AppShell boundary with no props. All hooks are
// always called (stable order); on scorecard/review tabs the Outline shows Draft sections.
export function CockpitSidebar() {
  const { id = "" } = useParams();
  const { tab, sec, navigate } = useCockpitNav();
  const artifact: "draft" | "spec" = tab === "spec" ? "spec" : "draft";
  const draft = useDraft(id);
  const spec = useSpec(id);
  const q = artifact === "spec" ? spec : draft;
  const { dirty } = useEditorStore();
  const sections = q.data?.sections ?? [];

  const references = useReferences(id);
  // Array.isArray guards keep the never-500 sidebar robust to an unexpected response
  // shape (and keep the Phase-3a tests — which stub every GET with the draft fixture —
  // green); criteria uses deep optional chaining for the same reason.
  const refs = Array.isArray(references.data) ? references.data : [];
  const scorecard = useScorecard(id);
  const criteria = scorecard.data?.scorecard?.criteria ?? [];

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

      <SidebarGroup title="References" count={refs.length || undefined} defaultOpen={false}>
        {refs.length === 0 && <p className={styles.note}>No references.</p>}
        <ul className={styles.outline} aria-label="References">
          {refs.map((r) => (
            <li key={r.id} className={styles.metaRow}>
              {r.citekey && <span className={styles.metaSub}>{r.citekey}</span>}
              <span className={styles.rowLabel}>{r.title}</span>
            </li>
          ))}
        </ul>
      </SidebarGroup>

      <SidebarGroup title="Criteria" count={criteria.length || undefined} defaultOpen={false}>
        {criteria.length === 0 && <p className={styles.note}>No criteria yet.</p>}
        <ul className={styles.outline} aria-label="Criteria">
          {criteria.map((c) => (
            <li key={c.index}>
              <button
                type="button"
                className={styles.criterion}
                onClick={() => navigate({ top_tab: "scorecard" })}
              >
                <span className={`band-pill ${BAND_CLASS[c.band]}`}>{BAND_LABEL[c.band]}</span>
                <span className={styles.criterionName}>{c.name}</span>
              </button>
            </li>
          ))}
        </ul>
      </SidebarGroup>
    </div>
  );
}
