import { useSearchParams } from "react-router-dom";

import { Pagination } from "../components/Pagination";
import { useDashboard, useSetDefaultProfile, type DashboardParams } from "../api/dashboard";

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
  // Reset to page 1 whenever a filter changes.
  function updateParam(key: string, value: string) {
    const next = new URLSearchParams(sp);
    if (value) next.set(key, value);
    else next.delete(key);
    next.delete("page");
    setSp(next);
  }

  async function onSelectProfile(value: string) {
    // Explicit selection: PERSIST FIRST (awaited), reflect the view only on success.
    try {
      await setDefault.mutateAsync(value ? Number(value) : null);
    } catch {
      return; // persist failed -> leave URL/selection unchanged (error surfaced below)
    }
    const next = new URLSearchParams(sp);
    if (value) next.set("profile", value);
    else next.delete("profile");
    setSp(next);
  }

  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <p role="alert">Couldn&apos;t load calls.</p>;

  return (
    <main>
      <h1>Funding calls</h1>

      <header>
        <label>
          Profile
          <select
            aria-label="profile"
            value={params.profile ?? ""}
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
            aria-label="source"
            value={params.source ?? ""}
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
            aria-label="sort"
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
            {c.deadline && <span> · due {c.deadline}</span>}
            {c.aggregate_score != null && <span> · {Math.round(c.aggregate_score)}</span>}
          </li>
        ))}
      </ul>

      <Pagination
        page={data.pagination.page}
        hasPrev={data.pagination.has_prev}
        hasNext={data.pagination.has_next}
        onPage={(p) => {
          const n = new URLSearchParams(sp);
          n.set("page", String(p));
          setSp(n);
        }}
      />
    </main>
  );
}
