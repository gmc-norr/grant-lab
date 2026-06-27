// frontend/src/cockpit/BandBadge.tsx
import { Badge } from "@mantine/core";

import type { ScorecardCriterion } from "../api/evaluator";
import styles from "./CockpitPanels.module.css";

const BAND_LABEL: Record<ScorecardCriterion["band"], string> = {
  strong: "Strong",
  adequate: "Adequate",
  at_risk: "At-risk",
  gap: "Gap",
};

// Banded readiness pill, shared by the Scorecard and the sidebar Criteria list so
// the band styling has a single owner. Mantine Badge gives the shape/size; the
// module class overrides bg/colour/border per band. data-band is the stable
// test/contract hook (forwarded to the Badge root).
export function BandBadge({ band }: { band: ScorecardCriterion["band"] }) {
  return (
    <Badge
      size="sm"
      radius="xl"
      className={`${styles.bandBadge} ${styles[band]}`}
      data-band={band}
    >
      {BAND_LABEL[band]}
    </Badge>
  );
}
