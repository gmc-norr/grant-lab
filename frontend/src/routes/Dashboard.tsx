import { useSearchParams } from "react-router-dom";

import { Pagination } from "../components/Pagination";
import { dashboardParamsFrom, useDashboard } from "../api/dashboard";
import { CallRow } from "./CallRow";
import { DashboardHeader } from "./DashboardHeader";
import styles from "./Dashboard.module.css";

export function Dashboard() {
  const [sp, setSp] = useSearchParams();
  const params = dashboardParamsFrom(sp);
  const { data, isLoading, isError } = useDashboard(params);

  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <p role="alert">Couldn&apos;t load calls.</p>;

  return (
    <main>
      <DashboardHeader />

      {data.ranking.pending > 0 && <p>Scoring {data.ranking.pending} calls…</p>}
      {data.ranking.failed > 0 && (
        <p>{data.ranking.failed} calls couldn&apos;t be scored automatically.</p>
      )}

      <ul className={styles.list}>
        {data.items.map((c) => (
          <CallRow key={c.id} call={c} />
        ))}
      </ul>

      <Pagination
        page={data.pagination.page}
        hasPrev={data.pagination.has_prev}
        hasNext={data.pagination.has_next}
        onPage={(p) =>
          setSp((prev) => {
            const next = new URLSearchParams(prev);
            next.set("page", String(p));
            return next;
          })
        }
      />
    </main>
  );
}
