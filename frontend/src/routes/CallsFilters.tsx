import { Select, Switch } from "@mantine/core";
import { useSearchParams } from "react-router-dom";

import { dashboardParamsFrom, useDashboard, useSetDefaultProfile } from "../api/dashboard";
import styles from "./CallsFilters.module.css";

// Sidebar filters (declared on the Calls route via `handle.Sidebar`). Reads the
// SAME useDashboard query + URL as the Dashboard list — React Query dedupes by
// key, the URL holds filter state, so the two stay in sync with no props across
// the shell boundary. Renders independently, so it must tolerate `data ===
// undefined` (first load / error): facet-driven selects disable until data
// arrives; Sort + Show-closed bind to the URL and always work.
export function CallsFilters() {
  const [sp, setSp] = useSearchParams();
  const params = dashboardParamsFrom(sp);
  const { data } = useDashboard(params);
  const setDefault = useSetDefaultProfile();
  const loading = data === undefined;

  function updateParam(key: string, value: string) {
    setSp((prev) => {
      const next = new URLSearchParams(prev);
      if (value) next.set(key, value);
      else next.delete(key);
      next.delete("page"); // any filter change resets to page 1
      return next;
    });
  }

  async function onSelectProfile(value: string) {
    // Explicit selection: PERSIST FIRST (awaited), reflect the view only on success.
    try {
      await setDefault.mutateAsync(value ? Number(value) : null);
    } catch {
      return; // persist failed -> leave the URL unchanged (error surfaced below)
    }
    // Functional updater: merge against the LATEST params, not the `sp` captured
    // before the awaited mutation.
    setSp((prev) => {
      const next = new URLSearchParams(prev);
      if (value) next.set("profile", value);
      else next.delete("profile");
      return next;
    });
  }

  const profileOptions = [
    { value: "", label: "— No profile —" },
    ...(data?.facets.profiles ?? []).map((p) => ({ value: String(p.id), label: p.name })),
  ];
  const sourceOptions = [
    { value: "", label: "All sources" },
    ...(data?.facets.sources ?? []).map((s) => ({ value: s.slug, label: s.name })),
  ];

  return (
    <div className={styles.filters}>
      <h2 className={styles.heading}>Filters</h2>

      <Select
        label="Profile"
        data={profileOptions}
        value={data?.effective.profile_id != null ? String(data.effective.profile_id) : ""}
        onChange={(v) => onSelectProfile(v ?? "")}
        disabled={loading || setDefault.isPending}
        allowDeselect={false}
        comboboxProps={{ withinPortal: false }}
      />
      {setDefault.isError && <p role="alert">Couldn&apos;t save the default profile.</p>}

      <Select
        label="Source"
        data={sourceOptions}
        value={data?.effective.source ?? ""}
        onChange={(v) => updateParam("source", v ?? "")}
        disabled={loading}
        allowDeselect={false}
        comboboxProps={{ withinPortal: false }}
      />

      <Select
        label="Sort"
        data={[
          { value: "newest", label: "Newest" },
          { value: "deadline", label: "Deadline" },
        ]}
        value={params.sort ?? "newest"}
        onChange={(v) => updateParam("sort", v ?? "newest")}
        allowDeselect={false}
        comboboxProps={{ withinPortal: false }}
      />

      <Switch
        label="Show closed"
        checked={params.show_closed ?? false}
        onChange={(e) => updateParam("show_closed", e.currentTarget.checked ? "1" : "")}
      />
    </div>
  );
}
