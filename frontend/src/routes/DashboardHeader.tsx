import { useSearchParams } from "react-router-dom";

import { dashboardParamsFrom, useDashboard } from "../api/dashboard";
import styles from "./DashboardHeader.module.css";

// The dashboard header strip: breadcrumb · the 3 summary counts (shown verbatim
// from the read-model — `high_score` is the server's >=70 top-match count, not
// recomputed from MatchBadge bands). Reads the shared useDashboard
// query (deduped with the list + filters). Sign-out lives in the shell TitleBar.
export function DashboardHeader() {
  const [sp] = useSearchParams();
  const { data } = useDashboard(dashboardParamsFrom(sp));
  const s = data?.summary;

  return (
    <header className={styles.strip}>
      <span className={styles.crumb}>Calls</span>
      <div className={styles.counts}>
        <span>
          <strong>{s?.open_count ?? 0}</strong> open
        </span>
        <span>
          <strong>{s?.high_score ?? 0}</strong> top matches
        </span>
        <span>
          <strong>{s?.soon_deadline ?? 0}</strong> closing within 30 days
        </span>
      </div>
    </header>
  );
}
