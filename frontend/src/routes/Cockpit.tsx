import { useState } from "react";
import { useParams } from "react-router-dom";

import { DraftPanel } from "../cockpit/DraftPanel";
import { ScorecardPanel } from "../cockpit/ScorecardPanel";
import { SpecPanel } from "../cockpit/SpecPanel";

type Artifact = "draft" | "spec" | "scorecard";

export function Cockpit() {
  const { id = "" } = useParams();
  // Artifact-level tab: the Spec and Scorecard are "center View tabs" of the
  // cockpit, not separate URLs. Default to Draft so the existing draft UX (and
  // Cockpit.test.tsx) is unchanged on open.
  const [artifact, setArtifact] = useState<Artifact>("draft");
  // Scorecard deep-link floor: switch the view tab to the criterion's target
  // artifact. The callback accepts sectionIndex so scroll-to-section is a
  // non-breaking future enhancement.
  const onNavigate = (target: "draft" | "spec") => setArtifact(target);
  return (
    <div className="cockpit-shell">
      <div role="tablist" aria-label="Artifact">
        <button
          type="button"
          role="tab"
          aria-selected={artifact === "draft"}
          onClick={() => setArtifact("draft")}
        >
          Draft
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={artifact === "spec"}
          onClick={() => setArtifact("spec")}
        >
          Spec
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={artifact === "scorecard"}
          onClick={() => setArtifact("scorecard")}
        >
          Scorecard
        </button>
      </div>
      {artifact === "draft" && <DraftPanel id={id} />}
      {artifact === "spec" && <SpecPanel id={id} />}
      {artifact === "scorecard" && <ScorecardPanel id={id} onNavigate={onNavigate} />}
    </div>
  );
}
