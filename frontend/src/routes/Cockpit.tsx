import { useState } from "react";
import { useParams } from "react-router-dom";

import { DraftPanel } from "../cockpit/DraftPanel";
import { SpecPanel } from "../cockpit/SpecPanel";

export function Cockpit() {
  const { id = "" } = useParams();
  // Artifact-level tab: the Spec is a "center View tab" of the cockpit, not a
  // separate URL. Default to Draft so the existing draft UX (and Cockpit.test.tsx)
  // is unchanged on open.
  const [artifact, setArtifact] = useState<"draft" | "spec">("draft");
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
      </div>
      {artifact === "draft" ? <DraftPanel id={id} /> : <SpecPanel id={id} />}
    </div>
  );
}
