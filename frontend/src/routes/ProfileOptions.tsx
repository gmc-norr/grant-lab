import type { ReactNode } from "react";

// Optional fields, folded into a closed <details> so the form leads with what the
// ranker needs. EligibilityWarning sits OUTSIDE it: a folded warning is not read.
export function ProfileOptions({ children, title = "Eligibility and funding preferences" }: { children: ReactNode; title?: string }) {
  return <details className="match-options"><summary>{title}</summary>{children}</details>;
}
