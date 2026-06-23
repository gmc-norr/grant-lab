import { useSearchParams } from "react-router-dom";

import { Pagination } from "../components/Pagination";
import { dashboardParamsFrom, useDashboard } from "../api/dashboard";
import { CallRow } from "./CallRow";
import styles from "./Dashboard.module.css";

export function Dashboard() {
  const [sp, setSp] = useSearchParams();
  const params = dashboardParamsFrom(sp);
  const { data, isLoading, isError } = useDashboard(params);

  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <p role="alert">Couldn&apos;t load calls.</p>;

  return (
    <main>
      <h1>Funding calls</h1>

      <header>
        {/* Native POST logout: a full-page nav follows the 303 to /login and
            carries Origin/CSRF (no client fetch / token plumbing). */}
        <form method="post" action="/logout">
          <button type="submit">Sign out</button>
        </form>
      </header>

      {/* Summary badges (Jinja-parity): the read-model ships these counts on
          every /api/calls response; surface them rather than discarding them. */}
      <section aria-label="Summary" className={styles.summary}>
        <span>
          <strong>{data.summary.open_count}</strong> open calls
        </span>
        <span>
          <strong>{data.summary.high_score}</strong> top matches
        </span>
        <span>
          <strong>{data.summary.soon_deadline}</strong> closing within 30 days
        </span>
      </section>

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
