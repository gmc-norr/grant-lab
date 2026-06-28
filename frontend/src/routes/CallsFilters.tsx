import { Select, Stack, Switch, Text, TextInput } from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { dashboardParamsFrom, useDashboard, useSetDefaultProfile } from "../api/dashboard";

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

  const [searchInput, setSearchInput] = useState(params.q ?? "");
  const [debouncedSearch] = useDebouncedValue(searchInput, 300);
  // Commit only when the debounced value differs from the (already-trimmed) URL
  // value — this guard prevents an echo loop with the re-sync effect below.
  // params.q is in the deps so the closure never reads a stale URL value; the
  // guard makes the resulting self-triggered re-run a no-op.
  useEffect(() => {
    const next = debouncedSearch.trim();
    if (next !== (params.q ?? "")) updateParam("q", next);
  }, [debouncedSearch, params.q]);
  // Re-sync the box when the URL q changes externally (back/forward, deep link).
  useEffect(() => {
    setSearchInput(params.q ?? "");
  }, [params.q]);

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
    <Stack gap="sm" p="sm">
      <Text component="h2" tt="uppercase" fz="0.7rem" fw={600} c="dimmed">Filters</Text>

      <TextInput
        label="Search"
        placeholder="Search calls…"
        value={searchInput}
        onChange={(e) => setSearchInput(e.currentTarget.value)}
      />

      <Select
        label="Profile"
        data={profileOptions}
        value={data?.effective.profile_id != null ? String(data.effective.profile_id) : ""}
        onChange={(v) => onSelectProfile(v ?? "")}
        disabled={loading || setDefault.isPending}
        allowDeselect={false}
        comboboxProps={{ withinPortal: false }}
      />
      {setDefault.isError && <Text role="alert" size="sm">Couldn&apos;t save the default profile.</Text>}

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

      <Select
        label="Min score"
        description={data?.effective.profile_id == null ? "Select a profile to filter by score" : undefined}
        data={[
          { value: "", label: "Any" },
          { value: "60", label: "60+ (Good)" },
          { value: "80", label: "80+ (Strong)" },
        ]}
        value={params.min_score != null ? String(params.min_score) : ""}
        onChange={(v) => updateParam("min_score", v ?? "")}
        disabled={loading || data?.effective.profile_id == null}
        allowDeselect={false}
        comboboxProps={{ withinPortal: false }}
      />

      <Switch
        label="Show closed"
        checked={params.show_closed ?? false}
        onChange={(e) => updateParam("show_closed", e.currentTarget.checked ? "1" : "")}
      />
    </Stack>
  );
}
