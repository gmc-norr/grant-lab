import { ActionIcon, Button, Group, NativeSelect, SegmentedControl, TextInput } from "@mantine/core";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { dashboardParamsFrom } from "../api/dashboard";

export function TriageToolbar({ sources, view }: { sources: { slug: string; name: string }[]; view: string }) {
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState(params.get("q") ?? "");
  function change(name: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(name, value);
    else next.delete(name);
    next.delete("page");
    setParams(next);
  }
  return (
    <section aria-label="Filter calls">
      <form onSubmit={event => { event.preventDefault(); change("q", query.trim()); }}>
        <Group gap="xs" align="flex-end" wrap="nowrap">
          <TextInput label="Search calls" type="search" maxLength={200} value={query} onChange={event => setQuery(event.currentTarget.value)} style={{ flex: 1, minWidth: 0 }} />
          <ActionIcon type="submit" variant="filled" size={36} aria-label="Search" title="Search"><span className="codicon codicon-search" aria-hidden="true" /></ActionIcon>
        </Group>
      </form>
      <Group gap="sm" mt="sm" align="flex-end">
        <NativeSelect label="Funder" value={params.get("source") ?? ""} onChange={event => change("source", event.currentTarget.value)} data={[{ value: "", label: "All funders" }, ...sources.map(source => ({ value: source.slug, label: source.name }))]} style={{ flex: 1, minWidth: 140 }} />
        {/* THE SANITISED sort, not params.get("sort"). Same source of truth as
            the wire request (TriageFeed.tsx also builds its query from
            dashboardParamsFrom), so the control cannot claim an order the list
            is not in. With the raw read, `?sort=foo` matched no <option> and
            what got displayed was whatever react-dom's updateOptions fell back
            to — the FIRST non-disabled option. That is "newest" today, so the
            raw read happened to look right. MEASURED, by putting "Soonest
            deadline" first and leaving the raw read in place: the control then
            reported `deadline` for `?sort=foo` over a list the server had
            ordered by newest. Also measured: the blank control the review
            predicted does not occur in react-dom 19 at all (see the note in
            TriageFeed.test.tsx's gate 13), so this closes a coincidence rather
            than a visible bug. */}
        <NativeSelect label="Order" value={dashboardParamsFrom(params).sort ?? "newest"} onChange={event => change("sort", event.currentTarget.value)} data={[{ value: "newest", label: view === "inbox" ? "Best match" : "Recently decided" }, { value: "deadline", label: "Soonest deadline" }]} style={{ flex: 1, minWidth: 140 }} />
        {(params.has("q") || params.has("source")) && <Button variant="subtle" onClick={() => { const next = new URLSearchParams(params); next.delete("q"); next.delete("source"); next.delete("page"); setQuery(""); setParams(next); }}>Clear filters</Button>}
      </Group>
      {view === "saved" && <SegmentedControl mt="sm" aria-label="Saved view" value={params.get("layout") === "expanded" ? "expanded" : "compact"} onChange={value => change("layout", value === "compact" ? "" : value)} data={[{ value: "compact", label: "Shortlist" }, { value: "expanded", label: "Details" }]} />}
    </section>
  );
}