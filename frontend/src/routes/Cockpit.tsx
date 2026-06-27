import { useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { useCockpitMeta } from "../api/workspace";
import { BottomPanel } from "../cockpit/BottomPanel";
import { useCockpitNav } from "../cockpit/cockpitNav";
import { DraftPanel } from "../cockpit/DraftPanel";
import { confirmDiscardIfDirty } from "../cockpit/editorStore";
import type { BottomTab, NavTarget, TopTab } from "../cockpit/nav";
import { ReviewPanel } from "../cockpit/ReviewPanel";
import { ScorecardPanel } from "../cockpit/ScorecardPanel";
import { SpecPanel } from "../cockpit/SpecPanel";
import { UnsavedChangesGuard } from "../cockpit/UnsavedChangesGuard";
import { WorkspaceSetup } from "../cockpit/WorkspaceSetup";

export function Cockpit() {
  const { id = "" } = useParams();
  const meta = useCockpitMeta(id);
  // Artifact tab + active section live in the URL (?tab/?sec) so the shell sidebar
  // Outline and the editor stay in sync across the AppShell boundary. The bottom
  // region (tab + collapse) stays local — it never crosses that boundary.
  const { tab, setTab, navigate } = useCockpitNav();
  const [bottomTab, setBottomTab] = useState<BottomTab>("inbox");
  const [collapsed, setCollapsed] = useState(false);
  const peekNonce = useRef(0);
  const [openPeek, setOpenPeek] = useState<{ ev: string; n: number } | null>(null);

  // Switching the artifact tab unmounts the active editor and discards its unsaved
  // per-section buffers. That is NOT a router navigation (the router blocker can't
  // see it), so confirm here before discarding. A clean editor → no prompt.
  const onTab = (t: TopTab) => {
    if (t === tab) return;
    if (!confirmDiscardIfDirty()) return;
    setTab(t);
  };

  const onNavigate = (target: NavTarget) => {
    navigate(target); // ?tab + ?sec
    if (target.bottom_tab) {
      setBottomTab(target.bottom_tab);
      setCollapsed(false);
    }
    if (target.evidence_id) {
      peekNonce.current += 1;
      setOpenPeek({ ev: target.evidence_id, n: peekNonce.current });
    }
  };

  // Branch AFTER all hooks. The shell sidebar/right-pane are mounted by AppShell
  // from the route handle (router.tsx) and handle setup themselves (Task 10).
  if (meta.isLoading || !meta.data) return <p className="cockpit-loading">Loading…</p>;
  if (meta.data.current_phase === "setup") return <WorkspaceSetup id={id} />;

  return (
    <div className="cockpit-shell">
      <UnsavedChangesGuard />
      <div role="tablist" aria-label="Artifact">
        <button type="button" role="tab" aria-selected={tab === "draft"} onClick={() => onTab("draft")}>
          Draft
        </button>
        <button type="button" role="tab" aria-selected={tab === "spec"} onClick={() => onTab("spec")}>
          Spec
        </button>
        <button type="button" role="tab" aria-selected={tab === "scorecard"} onClick={() => onTab("scorecard")}>
          Scorecard
        </button>
        <button type="button" role="tab" aria-selected={tab === "review"} onClick={() => onTab("review")}>
          Panel review
        </button>
      </div>
      {tab === "draft" && <DraftPanel id={id} openPeek={openPeek} />}
      {tab === "spec" && <SpecPanel id={id} />}
      {tab === "scorecard" && <ScorecardPanel id={id} onNavigate={onNavigate} />}
      {tab === "review" && <ReviewPanel id={id} />}
      <BottomPanel
        id={id}
        activeTab={bottomTab}
        onTab={setBottomTab}
        collapsed={collapsed}
        onToggleCollapsed={() => setCollapsed((c) => !c)}
        onNavigate={onNavigate}
      />
    </div>
  );
}
