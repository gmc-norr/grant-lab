import { useState } from "react";
import { useParams } from "react-router-dom";

import { BottomPanel } from "../cockpit/BottomPanel";
import { useCockpitNav } from "../cockpit/cockpitNav";
import { DraftPanel } from "../cockpit/DraftPanel";
import type { BottomTab, NavTarget } from "../cockpit/nav";
import { ReviewPanel } from "../cockpit/ReviewPanel";
import { ScorecardPanel } from "../cockpit/ScorecardPanel";
import { SpecPanel } from "../cockpit/SpecPanel";

export function Cockpit() {
  const { id = "" } = useParams();
  // Artifact tab + active section live in the URL (?tab/?sec) so the shell sidebar
  // Outline and the editor stay in sync across the AppShell boundary. The bottom
  // region (tab + collapse) stays local — it never crosses that boundary.
  const { tab, setTab, navigate } = useCockpitNav();
  const [bottomTab, setBottomTab] = useState<BottomTab>("inbox");
  const [collapsed, setCollapsed] = useState(false);

  const onNavigate = (target: NavTarget) => {
    navigate(target); // ?tab + ?sec
    if (target.bottom_tab) {
      setBottomTab(target.bottom_tab);
      setCollapsed(false);
    }
  };

  return (
    <div className="cockpit-shell">
      <div role="tablist" aria-label="Artifact">
        <button type="button" role="tab" aria-selected={tab === "draft"} onClick={() => setTab("draft")}>
          Draft
        </button>
        <button type="button" role="tab" aria-selected={tab === "spec"} onClick={() => setTab("spec")}>
          Spec
        </button>
        <button type="button" role="tab" aria-selected={tab === "scorecard"} onClick={() => setTab("scorecard")}>
          Scorecard
        </button>
        <button type="button" role="tab" aria-selected={tab === "review"} onClick={() => setTab("review")}>
          Panel review
        </button>
      </div>
      {tab === "draft" && <DraftPanel id={id} />}
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
