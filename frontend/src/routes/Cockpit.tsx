import { useState } from "react";
import { useParams } from "react-router-dom";

import { BottomPanel } from "../cockpit/BottomPanel";
import { DraftPanel } from "../cockpit/DraftPanel";
import type { BottomTab, NavTarget, TopTab } from "../cockpit/nav";
import { ScorecardPanel } from "../cockpit/ScorecardPanel";
import { SpecPanel } from "../cockpit/SpecPanel";

export function Cockpit() {
  const { id = "" } = useParams();
  // Artifact-level tab: the Spec and Scorecard are "center View tabs" of the
  // cockpit, not separate URLs. Default to Draft so the existing draft UX (and
  // Cockpit.test.tsx) is unchanged on open.
  const [topTab, setTopTab] = useState<TopTab>("draft");
  const [bottomTab, setBottomTab] = useState<BottomTab>("inbox");
  const [collapsed, setCollapsed] = useState(false);

  // Two-axis navigation: a Problems/Scorecard "act" can switch the artifact tab
  // (incl. Scorecard) and/or focus a bottom-region tab (expanding it if collapsed).
  const onNavigate = (target: NavTarget) => {
    if (target.top_tab) setTopTab(target.top_tab);
    if (target.bottom_tab) {
      setBottomTab(target.bottom_tab);
      setCollapsed(false);
    }
  };

  return (
    <div className="cockpit-shell">
      <div role="tablist" aria-label="Artifact">
        <button
          type="button"
          role="tab"
          aria-selected={topTab === "draft"}
          onClick={() => setTopTab("draft")}
        >
          Draft
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={topTab === "spec"}
          onClick={() => setTopTab("spec")}
        >
          Spec
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={topTab === "scorecard"}
          onClick={() => setTopTab("scorecard")}
        >
          Scorecard
        </button>
      </div>
      {topTab === "draft" && <DraftPanel id={id} />}
      {topTab === "spec" && <SpecPanel id={id} />}
      {topTab === "scorecard" && <ScorecardPanel id={id} onNavigate={onNavigate} />}
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
