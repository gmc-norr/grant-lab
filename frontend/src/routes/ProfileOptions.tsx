import type { ReactNode } from "react";
import { isMatchEdition } from "../edition";

export function ProfileOptions({ children, title = "Eligibility and funding preferences" }: { children: ReactNode; title?: string }) {
  return isMatchEdition ? <details className="match-options"><summary>{title}</summary>{children}</details> : <>{children}</>;
}