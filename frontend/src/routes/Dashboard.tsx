import { useSearchParams } from "react-router-dom";

import { Pagination } from "../components/Pagination";
import { useDashboard, useSetDefaultProfile, type DashboardParams } from "../api/dashboard";
import { Deadline } from "./Deadline";
import styles from "./Dashboard.module.css";

export function Dashboard() {
  const [sp, setSp] = useSearchParams();
  const params: DashboardParams = {
    source: sp.get("source") ?? undefined,
    sort: (sp.get("sort") as "newest" | "deadline" | null) ?? undefined,
    profile: sp.get("profile") ?? undefined,
    page: sp.get("page") ? Number(sp.get("page")) : 1,
    show_closed: sp.get("show_closed") === "1",
  };
  const { data, isLoading, isError } = useDashboard(params);
  const setDefault = useSetDefaultProfile();

  // Read-only view state: filters update the URL only, never persist a default.
  // Reset to page 1 whenever a filter changes. Use the FUNCTIONAL updater so the
  // write merges against the latest params (never a stale `sp` snapshot).
  function updateParam(key: string, value: string) {
    setSp((prev) => {
      const next = new URLSearchParams(prev);
      if (value) next.set(key, value);
      else next.delete(key);
      next.delete("page");
      return next;
    });
  }

  async function onSelectProfile(value: string) {
    // Explicit selection: PERSIST FIRST (awaited), reflect the view only on success.
    try {
      await setDefault.mutateAsync(value ? Number(value) : null);
    } catch {
      return; // persist failed -> leave URL/selection unchanged (error surfaced below)
    }
    // FUNCTIONAL updater: merge against the LATEST params, not the `sp` captured
    // before the awaited mutation — otherwise a Source/Sort/show_closed change the
    // user made WHILE the POST was in flight would be silently clobbered here.
    setSp((prev) => {
      const next = new URLSearchParams(prev);
      if (value) next.set("profile", value);
      else next.delete("profile");
      return next;
    });
  }

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
        <label>
          Profile
          <select
            // Bind to the SERVER-RESOLVED id (effective.profile_id), not the raw
            // ?profile URL param: a bare load resolves the stored default, and an
            // unknown/inactive id degrades to null — the select must reflect what
            // the server actually filtered by (Jinja parity). data is guaranteed
            // present here (loading early-returns above).
            value={data.effective.profile_id != null ? String(data.effective.profile_id) : ""}
            disabled={setDefault.isPending}
            onChange={(e) => onSelectProfile(e.target.value)}
          >
            <option value="">— No profile —</option>
            {data.facets.profiles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        {setDefault.isError && <p role="alert">Couldn&apos;t save the default profile.</p>}

        <label>
          Source
          <select
            // Bind to the SERVER-RESOLVED source (effective.source), not the raw
            // ?source param: an unknown/stale slug degrades to "all sources"
            // server-side, and the control must reflect that (profile-select parity).
            value={data.effective.source ?? ""}
            onChange={(e) => updateParam("source", e.target.value)}
          >
            <option value="">All sources</option>
            {data.facets.sources.map((s) => (
              <option key={s.slug} value={s.slug}>
                {s.name}
              </option>
            ))}
          </select>
        </label>

        <label>
          Sort
          <select
            value={params.sort ?? "newest"}
            onChange={(e) => updateParam("sort", e.target.value)}
          >
            <option value="newest">Newest</option>
            <option value="deadline">Deadline</option>
          </select>
        </label>

        <label>
          <input
            type="checkbox"
            checked={params.show_closed ?? false}
            onChange={(e) => updateParam("show_closed", e.target.checked ? "1" : "")}
          />
          Show closed
        </label>
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

      <ul>
        {data.items.map((c) => (
          <li key={c.id}>
            {/* plain anchor to the un-ported call-detail HTML page (coexistence) */}
            <a href={`/calls/${c.id}`}>{c.title}</a>
            {c.source_name && <span> · {c.source_name}</span>}
            {c.deadline && (
              <span>
                {" · "}
                <Deadline iso={c.deadline} />
              </span>
            )}
            {c.aggregate_score != null && <span> · {Math.round(c.aggregate_score)}</span>}
          </li>
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
