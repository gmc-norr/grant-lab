# App-Wide Mantine Sweep — Slice 5 (Dashboard / Calls / Workspaces + closers) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-skin the remaining non-cockpit content surfaces — the calls Dashboard cluster (`Dashboard`, `DashboardHeader`, `Deadline`, `CallRow`, `Pagination`), `CallDetail`, the Workspaces routes (`WorkspaceList`, `WorkspaceCreate`), the two sidebar filters' wrapper/heading/error (`CallsFilters`, `WorkspacesFilters`), and the shared `RouteSpinner` + soft-404 in `router.tsx` — to Mantine v9, retiring their ten CSS modules. This **closes the app-wide Mantine sweep**.

**Architecture:** Pure frontend re-skin. Raw `<main>/<header>/<ul>/<li>/<section>/<article>/<form>/<input>/<button>/<span>/<a>` markup becomes Mantine `Box`/`Stack`/`Group`/`Card`/`Title`/`Text`/`Anchor`/`Button`/`TextInput`/`Badge`/`Code`/`Center`/`Loader` — preserving the original elements via the polymorphic `component` prop where they carry semantics. The data layers (`api/dashboard.ts`, `api/calls.ts`, `api/workspace.ts`) are untouched; `router.tsx` is touched **only** for `RouteSpinner` + the soft-404 element (the route manifest, paths, `handle`s, and ownership are NOT changed). No behaviour, data-flow, API, route, or copy changes. No schema, no OpenAPI regen.

**Tech Stack:** React 19.2.7, Mantine v9.4.0, react-router-dom, TanStack Query, Vite, Vitest + Testing Library (jsdom), pnpm.

## Global Constraints

_Every task's requirements implicitly include this section._

- **Re-skin only.** No behaviour / data-flow / API / route / copy changes. Pure frontend; no backend, no schema, no OpenAPI regen. `api/dashboard.ts`, `api/calls.ts`, `api/workspace.ts` are NOT modified. `router.tsx` is modified ONLY inside `RouteSpinner` and the soft-404 element — the route tree, paths, `index`/`path` keys, `handle: { Sidebar/RightPane }`, `Suspense` wiring, and the comments documenting the manifest invariant are byte-unchanged.
- **Mantine v9.4.0 primitive for every UI element.** Dark teal theme inherited from `theme`. Domain `--app-*` colours are kept via the `c`/`bg` props or `style` (they are the irreducible design tokens, as in the cockpit sweep's `bg="var(--app-sidebar)"`).
- **`tsc` is the type gate** (`noUnusedLocals` — import only what you use; drop each `import styles from "./*.module.css"`). Keep `type FormEvent`/`React.FormEvent` exactly as each file uses it.
- **Preserve test hooks verbatim:** `data-band` (the CallDetail score figure), `data-deadline-state` (the `Deadline`/`DeadlineLocal` span), and every `role="alert"` that the ORIGINAL element already carried. Do NOT add `role="alert"` to elements that lacked it, and do NOT drop it from elements that had it.
- **`role="alert"` errors → `Text role="alert"`** (a single text node, no Mantine `Alert` chrome) wherever the original was a plain `<p role="alert">` — so `getByRole("alert").textContent` stays EXACTLY the message (CallDetail's not-found test asserts `.toBe("Call not found.")`; CallsFilters asserts `toHaveTextContent(/default profile/i)`).
- **Button-styled links → `Button component={Link}`**; text links → `Anchor component={Link} to="…"`. Both render a real `<a href>` with client-side nav, so `getByRole("link", { name })` + `href` assertions survive. `Button component={Link}` is what keeps CallDetail's "Start drafting workspace" a **link** (the test queries `getByRole("link", { name: /start drafting/i })`).
- **Irreducible `style` (sanctioned, documented) — and ONLY these four:** (a) the **row/header `border-bottom` dividers** (`style={{ borderBottom: "1px solid var(--app-border)" }}`; Mantine `bd` is all-sides only); (b) the two **`auto-fill minmax` responsive grids** in CallDetail (`style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(…, 1fr))", gap: "…" }}`; `SimpleGrid` can't express auto-fill); (c) the CallRow "Start drafting" link's `style={{ whiteSpace: "nowrap" }}` (Mantine has no white-space style prop); (d) the CallDetail criterion quote's `style={{ borderLeft: "2px solid var(--app-border-strong)" }}` (Mantine `Blockquote`'s default icon/bg chrome diverges from the minimal original — keep the bare left border). **Everything else uses Mantine style props** — `miw={0}`/`miw="3rem"` (min-width), `flex="0 0 auto"` (no-shrink), `lh={1}` (verified to emit a literal `line-height: 1`), plus `mt`/`px`/`py`/`gap`/`fz`/`c`/`bg`. No other inline `style` appears anywhere in the slice.
- **No raw `<span>` for styled inline text** — use `Text span` (separators keep `aria-hidden`; the source/linked-call/status text nodes stay queryable). Bare structural fragments are fine, but visible inline text is a Mantine primitive.
- **Lists:** `Box component="ul" p={0}` (drop the marker gutter) with each row `Box component="li"`/`Group component="li"` (flex `<li>` → no marker, so no `list-style:none` needed) — the same pattern as the admin slice's disabled-user list.
- **Mantine throws without a provider.** `Dashboard.test.tsx` and `CallRow.test.tsx` currently render bare (no `MantineProvider`) because these components use no Mantine today. The re-skin makes them Mantine, so each test's local render helper gets wrapped in `<MantineProvider theme={theme} defaultColorScheme="dark">` — **assertions byte-unchanged**. These are the ONLY two test changes in the slice. Every other test already supplies a provider (`renderWithProviders` / a local `MantineProvider`) or tests pure functions (`Deadline.test` imports `deadlineView`/`deadlineLocalView` only).
- **`Badge` uppercases via CSS** (`text-transform`), so the DOM text node keeps its original case → `getByText("Setting up")`/`getByText("Shared")` still match. Use `tt="none"` only where a chip must stay mixed-case (the CallDetail `gateTag`/keyword/criteria chips).
- **`e.currentTarget.value`** in all Mantine change handlers (WorkspaceCreate currently uses `e.target.value` — switch to `currentTarget`).
- **Out of scope (unchanged):** `MatchBadge` (already the banded-pill domain component, plain, kept), the IDE shell, `ErrorBoundary`, `AdminNav`. `MatchBadge` is plain (no Mantine) so it renders fine inside or outside a provider.
- **Branch / worktree:** isolated worktree `.worktrees/mantine-sweep-dashboard` on branch `feat/mantine-sweep-dashboard`. Controller merges to LOCAL master `--no-ff` (never push) at the user's review gate, then rebuilds + restages the gitignored bundle.
- **Whole-suite `vitest` is reliable here** — only backend testcontainers flakes; this slice changes no backend.

---

## File Structure

**Task 1 — Sidebar filters:** modify `CallsFilters.tsx`, `WorkspacesFilters.tsx`; delete `CallsFilters.module.css`, `WorkspacesFilters.module.css`.
**Task 2 — Shared closers:** modify `router.tsx` (`RouteSpinner` + soft-404 only).
**Task 3 — Calls list:** modify `Pagination.tsx`, `Deadline.tsx`, `DashboardHeader.tsx`, `CallRow.tsx`, `Dashboard.tsx`; delete `Pagination.module.css`, `Deadline.module.css`, `DashboardHeader.module.css`, `CallRow.module.css`, `Dashboard.module.css`; wrap `Dashboard.test.tsx` + `CallRow.test.tsx` renders in `MantineProvider` (harness-only).
**Task 4 — Call detail:** modify `CallDetail.tsx`; delete `CallDetail.module.css`.
**Task 5 — Workspaces:** modify `WorkspaceList.tsx`, `WorkspaceCreate.tsx`; delete `WorkspaceList.module.css`, `WorkspaceCreate.module.css`.

**Unchanged (must stay green, byte-identical):** `DashboardHeader.test.tsx`, `Deadline.test.tsx`, `CallDetail.test.tsx`, `WorkspaceList.test.tsx`, `WorkspaceCreate.test.tsx`, `CallsFilters.test.tsx`, `WorkspacesFilters.test.tsx`, `router.test.tsx`, `MatchBadge.*`.
**Untouched:** `api/dashboard.ts`, `api/calls.ts`, `api/workspace.ts`, `components/MatchBadge.*`, the route manifest in `router.tsx`.

Decomposition rationale: simplest-first. Filters (controls already Mantine; zero test change) establish the `Stack`/`Text`-heading/`Text role="alert"` vocabulary. Router closers are isolated and touch the sensitive manifest file alone (focused review). The calls list converts the 5 coupled list components together (so `Dashboard.test`/`CallRow.test` provider wrappers land with their components). CallDetail is the largest single file. Workspaces close it out.

---

## Task 1: Re-skin the sidebar filters (`CallsFilters` + `WorkspacesFilters`)

Convert ONLY each file's `<div className={styles.filters}>` wrapper → `Stack`, `<h2 className={styles.heading}>` → `Text component="h2"`, and (CallsFilters) the `<p role="alert">` → `Text role="alert"`. The Mantine `Select`/`Switch`/`TextInput` controls and all logic stay byte-identical.

**Files:** Modify `frontend/src/routes/CallsFilters.tsx`, `frontend/src/routes/WorkspacesFilters.tsx`; Delete `frontend/src/routes/CallsFilters.module.css`, `frontend/src/routes/WorkspacesFilters.module.css`. Tests (unchanged): `CallsFilters.test.tsx`, `WorkspacesFilters.test.tsx`.

- [ ] **Step 1: Edit `CallsFilters.tsx`** — change the import line, the wrapper, the heading, and the error. Replace the `@mantine/core` import (line 1) and the `styles` import (line 7):

```tsx
import { Select, Stack, Switch, Text, TextInput } from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
```
(delete `import styles from "./CallsFilters.module.css";`)

Replace the returned wrapper open/heading (the `<div className={styles.filters}>` + `<h2 …>Filters</h2>`):

```tsx
  return (
    <Stack gap="sm" p="sm">
      <Text component="h2" tt="uppercase" fz="0.7rem" fw={600} c="dimmed">Filters</Text>
```

Replace the profile-error line (`{setDefault.isError && <p role="alert">…</p>}`):

```tsx
      {setDefault.isError && <Text role="alert" size="sm">Couldn&apos;t save the default profile.</Text>}
```

Change the closing `</div>` of the wrapper to `</Stack>`. Everything between (the five controls) is unchanged.

- [ ] **Step 2: Edit `WorkspacesFilters.tsx`** — replace the import (line 1) and the `styles` import (line 4):

```tsx
import { Select, Stack, Text } from "@mantine/core";
```
(delete `import styles from "./WorkspacesFilters.module.css";`)

Replace the wrapper + heading:

```tsx
  return (
    <Stack gap="sm" p="sm">
      <Text component="h2" tt="uppercase" fz="0.7rem" fw={600} c="dimmed">Filters</Text>
```
and the closing `</div>` → `</Stack>`. The `<Select>` is unchanged.

- [ ] **Step 3: Run the tests green (from `frontend/`)**

Run: `pnpm exec vitest run src/routes/CallsFilters.test.tsx src/routes/WorkspacesFilters.test.tsx`
Expected: PASS. The label-driven queries (`findByLabelText("Profile"|"Source"|"Sort"|"Min score"|"Search"|"Status", { selector: "input" })`, `getByLabelText("Show closed")`), the option clicks, the URL writes, the disabled-state checks, and the failed-persist `getByRole("alert")).toHaveTextContent(/default profile/i)` all survive — only the wrapper/heading/error markup changed, none of which the tests query (no test asserts the "Filters" heading). No test edits.

- [ ] **Step 4: Delete modules + grep**

Run (from `frontend/`): `grep -rln "CallsFilters.module.css\|WorkspacesFilters.module.css" src` → expect none. Then `git rm src/routes/CallsFilters.module.css src/routes/WorkspacesFilters.module.css`.

- [ ] **Step 5: Gates (from `frontend/`):** `pnpm exec tsc --noEmit` · `pnpm exec vitest run` · `pnpm build` — all green.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(frontend): re-skin CallsFilters + WorkspacesFilters wrapper/heading/error to Mantine; drop 2 modules"
```

---

## Task 2: Re-skin the shared closers in `router.tsx` (`RouteSpinner` + soft-404)

The ONLY edits to `router.tsx`: the `RouteSpinner` function body and the soft-404 `element`. The route tree, paths, `handle`s, `Suspense` wiring, imports of route components, and the manifest comments are NOT touched. `router.test.tsx` asserts only that `/login` renders the pre-auth Login (not the soft-404) and pins the concrete page paths — neither is affected.

**Files:** Modify `frontend/src/router.tsx`. Test (unchanged): `router.test.tsx`.

- [ ] **Step 1: Add the Mantine imports** — add a new import line near the top of `router.tsx` (after the `react` import on line 1):

```tsx
import { Center, Group, Loader, Text, Title } from "@mantine/core";
```

- [ ] **Step 2: Replace `RouteSpinner`** (lines 37-43):

```tsx
function RouteSpinner() {
  return (
    <Center component="main" h="100%" p="xl">
      <Group gap="sm">
        <Loader size="sm" />
        <Text c="dimmed">Loading…</Text>
      </Group>
    </Center>
  );
}
```

- [ ] **Step 3: Replace the soft-404 `element`** (the `<main role="alert"><h1>Not found</h1></main>` at the `path: "*"` route) — keep `role="alert"` and the "Not found" copy:

```tsx
        element: (
          <Center component="main" role="alert" p="xl">
            <Title order={1}>Not found</Title>
          </Center>
        ),
```

- [ ] **Step 4: Run the test green (from `frontend/`)**

Run: `pnpm exec vitest run src/router.test.tsx`
Expected: PASS. `/login` still renders the pre-auth Login heading (`getByRole("heading", { name: /sign in/i })`); the soft-404 is not asserted by the test. No test edits.

- [ ] **Step 5: Gates (from `frontend/`):** `pnpm exec tsc --noEmit` · `pnpm exec vitest run` · `pnpm build` — all green. (No module to delete — `router.tsx` has none.)

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(frontend): re-skin RouteSpinner + soft-404 to Mantine (manifest untouched)"
```

---

## Task 3: Re-skin the calls list (`Pagination`, `Deadline`, `DashboardHeader`, `CallRow`, `Dashboard`)

The five coupled list components convert together; `Pagination.module.css`, `Deadline.module.css`, `DashboardHeader.module.css`, `CallRow.module.css`, `Dashboard.module.css` are deleted. `Dashboard.test.tsx` + `CallRow.test.tsx` get a `MantineProvider` wrapper (harness-only — assertions unchanged), because the re-skin makes those components Mantine and those two tests render bare today.

**Files:** Modify `Pagination.tsx`, `Deadline.tsx`, `DashboardHeader.tsx`, `CallRow.tsx`, `Dashboard.tsx`; Delete the five matching modules; Modify (harness-only) `Dashboard.test.tsx`, `CallRow.test.tsx`. Tests unchanged: `Deadline.test.tsx` (pure fns), `DashboardHeader.test.tsx` (`renderWithProviders`).
**Interfaces:** `Deadline` exports `deadlineView`, `deadlineLocalView`, `Deadline`, `DeadlineLocal` with identical signatures (consumed by `CallRow`, `WorkspaceList`, `CallDetail`); `Pagination` keeps its `{ page, hasPrev, hasNext, onPage }` props.

- [ ] **Step 1: Replace `components/Pagination.tsx`**

```tsx
import { Button, Group, Text } from "@mantine/core";

type Props = {
  page: number;
  hasPrev: boolean;
  hasNext: boolean;
  onPage: (page: number) => void;
};

export function Pagination({ page, hasPrev, hasNext, onPage }: Props) {
  return (
    <Group component="nav" aria-label="Pagination" gap="sm" align="center">
      <Button variant="default" size="xs" disabled={!hasPrev} onClick={() => onPage(page - 1)}>
        Previous
      </Button>
      <Text span size="sm">Page {page}</Text>
      <Button variant="default" size="xs" disabled={!hasNext} onClick={() => onPage(page + 1)}>
        Next
      </Button>
    </Group>
  );
}
```

- [ ] **Step 2: Replace the `Deadline`/`DeadlineLocal` components in `Deadline.tsx`** — KEEP `deadlineView` + `deadlineLocalView` byte-identical (lines 1-41 logic), only swap the `styles` import and the two render functions. Replace the import line 1:

```tsx
import { Text } from "@mantine/core";
```
(delete `import styles from "./Deadline.module.css";`)

Add the state→props map after the `DeadlineState` type and replace the two components (the `Deadline` at lines 43-50 and `DeadlineLocal` at lines 55-72) — the helper functions in between are unchanged:

```tsx
// Domain deadline-urgency colours (parity with the retired Deadline.module.css):
// closed=dim, soon=bad+bold, watch=warn, open=muted.
const DEADLINE_TEXT: Record<DeadlineState, { c: string; fw?: number }> = {
  closed: { c: "var(--app-dim)" },
  soon: { c: "var(--app-bad)", fw: 600 },
  watch: { c: "var(--app-warn)" },
  open: { c: "var(--app-muted)" },
};

export function Deadline({ iso }: { iso: string }) {
  const { state, date } = deadlineView(iso);
  return (
    <Text span data-deadline-state={state} {...DEADLINE_TEXT[state]}>
      {state === "closed" ? `Closed · ${date}` : date}
    </Text>
  );
}

export function DeadlineLocal({
  localDate,
  isoFallback,
}: {
  localDate: string | null;
  isoFallback: string;
}) {
  if (localDate) {
    const { state, date } = deadlineLocalView(localDate);
    return (
      <Text span data-deadline-state={state} {...DEADLINE_TEXT[state]}>
        {state === "closed" ? `Closed · ${date}` : date}
      </Text>
    );
  }
  return <Deadline iso={isoFallback} />;
}
```

- [ ] **Step 3: Replace `DashboardHeader.tsx`** — keep the `useDashboard`/`dashboardParamsFrom` data wiring and the comment; only the markup changes. Replace the `styles` import (line 4) and the returned JSX:

```tsx
import { Group, Text } from "@mantine/core";
import { useSearchParams } from "react-router-dom";

import { dashboardParamsFrom, useDashboard } from "../api/dashboard";

// The dashboard header strip: breadcrumb · the 3 summary counts (shown verbatim
// from the read-model — `high_score` is the server's >=70 top-match count, not
// recomputed from MatchBadge bands). Reads the shared useDashboard
// query (deduped with the list + filters). Sign-out lives in the shell TitleBar.
export function DashboardHeader() {
  const [sp] = useSearchParams();
  const { data } = useDashboard(dashboardParamsFrom(sp));
  const s = data?.summary;

  return (
    <Group
      component="header"
      gap="md"
      align="center"
      px="0.25rem"
      py="xs"
      style={{ borderBottom: "1px solid var(--app-border)" }}
    >
      <Text fw={600}>Calls</Text>
      <Group gap="md" c="dimmed" fz="0.85rem">
        <Text span><Text span fw={700}>{s?.open_count ?? 0}</Text> open</Text>
        <Text span><Text span fw={700}>{s?.high_score ?? 0}</Text> top matches</Text>
        <Text span><Text span fw={700}>{s?.soon_deadline ?? 0}</Text> closing within 30 days</Text>
      </Group>
    </Group>
  );
}
```

- [ ] **Step 4: Replace `CallRow.tsx`** — keep the href-building logic + `MatchBadge` import:

```tsx
import { Anchor, Box, Group, Stack, Text } from "@mantine/core";
import { Link } from "react-router-dom";

import type { CallItem } from "../api/dashboard";
import { MatchBadge } from "../components/MatchBadge";
import { Deadline } from "./Deadline";

// One call in the list: title link (to the React detail route), a muted
// meta line (source · deadline), the banded match pill, and a "Start drafting"
// action that opens the React from-call create form. profileId is the dashboard's
// resolved effective profile (carried so Phase-0 can snapshot it).
export function CallRow({ call, profileId }: { call: CallItem; profileId?: number | null }) {
  const params = new URLSearchParams({ call_id: String(call.id), call_title: call.title });
  if (profileId != null) params.set("profile_id", String(profileId));
  const startHref = `/cockpit/new?${params.toString()}`;
  return (
    <Group
      component="li"
      justify="space-between"
      align="flex-start"
      gap="sm"
      wrap="nowrap"
      px="0.25rem"
      py="xs"
      style={{ borderBottom: "1px solid var(--app-border)" }}
    >
      <Box miw={0}>
        <Anchor
          component={Link}
          fw={500}
          to={profileId != null ? `/calls/${call.id}?profile=${profileId}` : `/calls/${call.id}`}
        >
          {call.title}
        </Anchor>
        <Text mt="0.15rem" c="dimmed" fz="0.85rem">
          {call.source_name && <Text span>{call.source_name}</Text>}
          {call.source_name && call.deadline && <Text span aria-hidden> · </Text>}
          {call.deadline && <Deadline iso={call.deadline} />}
        </Text>
      </Box>
      <Stack gap="0.3rem" align="flex-end" flex="0 0 auto">
        <MatchBadge score={call.aggregate_score} />
        <Anchor component={Link} fz="0.8rem" fw={500} to={startHref} style={{ whiteSpace: "nowrap" }}>
          Start drafting
        </Anchor>
      </Stack>
    </Group>
  );
}
```

- [ ] **Step 5: Replace `Dashboard.tsx`** — keep the `useDashboard`/`useSearchParams`/pagination logic:

```tsx
import { Box, Text } from "@mantine/core";
import { useSearchParams } from "react-router-dom";

import { Pagination } from "../components/Pagination";
import { dashboardParamsFrom, useDashboard } from "../api/dashboard";
import { CallRow } from "./CallRow";
import { DashboardHeader } from "./DashboardHeader";

export function Dashboard() {
  const [sp, setSp] = useSearchParams();
  const params = dashboardParamsFrom(sp);
  const { data, isLoading, isError } = useDashboard(params);

  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data) return <Text role="alert" p="md">Couldn&apos;t load calls.</Text>;

  return (
    <Box component="main">
      <DashboardHeader />

      {data.ranking.pending > 0 && <Text>Scoring {data.ranking.pending} calls…</Text>}
      {data.ranking.failed > 0 && (
        <Text>{data.ranking.failed} calls couldn&apos;t be scored automatically.</Text>
      )}

      <Box component="ul" p={0} my="xs">
        {data.items.map((c) => (
          <CallRow key={c.id} call={c} profileId={data.effective.profile_id} />
        ))}
      </Box>

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
    </Box>
  );
}
```

- [ ] **Step 6: Wrap `Dashboard.test.tsx`'s `renderAt` in `MantineProvider`** (harness-only). Add to the imports:

```tsx
import { MantineProvider } from "@mantine/core";
```
and import the theme:
```tsx
import { theme } from "../theme/theme";
```
Then wrap the render in `renderAt` so the body becomes:

```tsx
function renderAt(url: string) {
  const qc = new QueryClient();
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[url]}>
          <Dashboard />
          <LocationProbe />
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}
```
No assertion changes.

- [ ] **Step 7: Wrap `CallRow.test.tsx`'s `renderRow` in `MantineProvider`** (harness-only). Add to imports:

```tsx
import { MantineProvider } from "@mantine/core";
```
and
```tsx
import { theme } from "../theme/theme";
```
Then:

```tsx
function renderRow(ui: React.ReactElement) {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <MemoryRouter>{ui}</MemoryRouter>
    </MantineProvider>,
  );
}
```
No assertion changes.

- [ ] **Step 8: Run the tests green (from `frontend/`)**

Run: `pnpm exec vitest run src/routes/Dashboard.test.tsx src/routes/CallRow.test.tsx src/routes/Deadline.test.tsx src/routes/DashboardHeader.test.tsx`
Expected: PASS. Dashboard: the `[data-deadline-state="closed"]`/`[="soon"]` querySelectors + `Closed ·` prefix survive (Deadline keeps the attr + text); `findByRole("button", { name: /next/i })` hits the Mantine Pagination Next `<button>`; `findByRole("link", { name: "Start drafting" })` + `profile_id=5` survive; the 401 redirect path is unchanged. CallRow: `getByRole("link", { name: "Grant X" })` href, `getByText("Vinnova")`, `getByText("82")).toHaveAttribute("data-band","strong")` (MatchBadge, unchanged), the Start-drafting href cases. Deadline: pure-function cutoffs unchanged. DashboardHeader: `getByText("Calls")` + the `getByText("12"|"5"|"3")` isolated `<Text span fw={700}>` counts. Only the two wrapper changes touched tests.

- [ ] **Step 9: Delete modules + grep**

Run (from `frontend/`): `grep -rln "Pagination.module.css\|Deadline.module.css\|DashboardHeader.module.css\|CallRow.module.css\|Dashboard.module.css" src` → expect none. Then `git rm src/components/Pagination.module.css src/routes/Deadline.module.css src/routes/DashboardHeader.module.css src/routes/CallRow.module.css src/routes/Dashboard.module.css`.

- [ ] **Step 10: Gates (from `frontend/`):** `pnpm exec tsc --noEmit` · `pnpm exec vitest run` · `pnpm build` — all green.

- [ ] **Step 11: Commit**

```bash
git add -A
git commit -m "feat(frontend): re-skin Dashboard/DashboardHeader/Deadline/CallRow/Pagination to Mantine; drop 5 modules"
```

---

## Task 4: Re-skin `CallDetail` (the call detail route)

The largest single file. Keep `budgetText`, the data wiring (`useCallDetail`/`useHideCall`/`useUnhideCall`), the hide/confirm/undo state machine, and the `data-band` score figure. Convert all markup to Mantine.

**Files:** Modify `frontend/src/routes/CallDetail.tsx`; Delete `frontend/src/routes/CallDetail.module.css`. Test (unchanged): `CallDetail.test.tsx` (already wraps in `MantineProvider`).
**Interfaces:** Consumes `DeadlineLocal` from `./Deadline` (Task 3, Mantine).

- [ ] **Step 1: Replace `CallDetail.tsx`**

```tsx
import { Anchor, Badge, Box, Button, Card, Code, Group, Stack, Text, Title } from "@mantine/core";
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { useCallDetail, useHideCall, useUnhideCall } from "../api/calls";
import { DeadlineLocal } from "./Deadline";

function budgetText(c: { budget_min: number | null; budget_max: number | null; currency: string | null }): string {
  if (c.budget_min == null && c.budget_max == null) return "—";
  const fmt = (n: number) => n.toLocaleString("en-US");
  const lo = c.budget_min != null ? fmt(c.budget_min) : "—";
  const hi = c.budget_max != null ? fmt(c.budget_max) : "—";
  return `${lo} – ${hi} ${c.currency ?? ""}`.trim();
}

const GRID_AUTOFILL = (min: string) => ({
  display: "grid",
  gridTemplateColumns: `repeat(auto-fill, minmax(${min}, 1fr))`,
  gap: "0.75rem",
});

export function CallDetail() {
  const { id = "" } = useParams();
  const callId = Number(id);
  const [sp] = useSearchParams();
  const profileParam = sp.get("profile");
  const profile = profileParam != null && profileParam !== "" ? Number(profileParam) : null;
  const navigate = useNavigate();
  const hide = useHideCall();
  const unhide = useUnhideCall();
  const [confirmHide, setConfirmHide] = useState(false);
  const [hiddenId, setHiddenId] = useState<number | null>(null);
  const { data, isLoading, isError, error } = useCallDetail(callId, profile);

  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data) {
    const notFound = (error as { code?: string } | null)?.code === "not_found";
    return <Text role="alert" p="md">{notFound ? "Call not found." : "Couldn't load this call."}</Text>;
  }

  const { call, ranking, criteria, best_profile_id } = data;
  const startParams = new URLSearchParams({ call_id: String(call.id), call_title: call.title });
  if (best_profile_id != null) startParams.set("profile_id", String(best_profile_id));

  const band = ranking
    ? ranking.aggregate_score >= 70 ? "strong" : ranking.aggregate_score > 0 ? "mid" : "low"
    : "low";
  const figureColor =
    band === "strong" ? "var(--app-good)" : band === "mid" ? "var(--app-warn)" : "var(--app-dim)";

  return (
    <Stack component="main" maw="52rem" mx="auto" px="xs" pt="md" pb="xl" gap="lg">
      <Anchor component={Link} to="/" size="sm">← All calls</Anchor>

      <Box component="header">
        <Title order={1}>{call.title}</Title>
        <Text c="dimmed" fz="0.9rem">
          {call.source_name}
          {call.programme && <> · {call.programme}</>}
          {call.external_id && <> · <Code>{call.external_id}</Code></>}
        </Text>
        <Box mt="sm">
          <Button component={Link} to={`/cockpit/new?${startParams.toString()}`} size="sm">
            Start drafting workspace
          </Button>
        </Box>
      </Box>

      {ranking && (
        <Box component="section" aria-label="Ranking">
          <Card component="article" withBorder radius="md" p="md" bg="var(--app-sidebar)">
            <Group align="flex-start" gap="md" wrap="nowrap">
              <Text data-band={band} fz="2.5rem" fw={700} ta="center" c={figureColor} miw="3rem" lh={1}>
                {ranking.aggregate_score}
              </Text>
              <Stack gap="0.4rem">
                <Text fw={700}>Aggregate score</Text>
                {ranking.aggregate_reason_label && (
                  <Badge variant="default" tt="none" radius="xl">{ranking.aggregate_reason_label}</Badge>
                )}
                <Badge variant="default" tt="none" radius="xl" title="LLM that produced this ranking">
                  Analyzed by {ranking.model_label}
                </Badge>
                {ranking.rationale && !ranking.aggregate_reason_label && (
                  <Text fz="0.9rem">{ranking.rationale}</Text>
                )}
                {ranking.embed_similarity != null && (
                  <Text c="dimmed" fz="0.8rem">Cosine similarity: {ranking.embed_similarity.toFixed(3)}</Text>
                )}
                {ranking.escalated && ranking.escalated_from_score != null && (
                  <Text c="dimmed" fz="0.8rem">
                    Escalated from {ranking.escalated_from_score} (analysis by {ranking.analysis_model_label}).
                  </Text>
                )}
              </Stack>
            </Group>
          </Card>
        </Box>
      )}

      {ranking && (ranking.summary || ranking.applicant_notes.length > 0) && (
        <Box component="section" aria-label="Deep analysis">
          <Title order={2} fz="1.05rem" mb="xs">
            Analyst notes <Text span c="dimmed" fz="0.8rem">— {ranking.model_label}</Text>
          </Title>
          {ranking.summary && <Text>{ranking.summary}</Text>}
          {ranking.applicant_notes.length > 0 && (
            <Box mt="sm" style={GRID_AUTOFILL("14rem")}>
              {ranking.applicant_notes.map((n, i) => (
                <Card key={i} component="article" withBorder radius="sm" py="0.6rem" px="0.75rem">
                  <Text component="h3" fz="0.85rem" c="var(--app-accent)" mb="0.25rem">{n.heading}</Text>
                  <Text fz="0.85rem">{n.text}</Text>
                </Card>
              ))}
            </Box>
          )}
        </Box>
      )}

      <Box component="section" aria-label="Call details">
        <Title order={2} fz="1.05rem" mb="xs">Details</Title>
        <Box style={GRID_AUTOFILL("12rem")}>
          <Stack gap="0.15rem">
            <Text c="dimmed" fz="0.75rem" tt="uppercase">Published</Text>
            <Text>{call.published_at ? call.published_at.slice(0, 10) : "—"}</Text>
          </Stack>
          <Stack gap="0.15rem">
            <Text c="dimmed" fz="0.75rem" tt="uppercase">Deadline</Text>
            <Text component="span">
              {call.deadline
                ? <DeadlineLocal localDate={call.deadline_local_date ?? null} isoFallback={call.deadline} />
                : "—"}
            </Text>
          </Stack>
          <Stack gap="0.15rem">
            <Text c="dimmed" fz="0.75rem" tt="uppercase">Budget</Text>
            <Text>{budgetText(call)}</Text>
          </Stack>
          <Stack gap="0.15rem">
            <Text c="dimmed" fz="0.75rem" tt="uppercase">External link</Text>
            <Anchor href={call.url} target="_blank" rel="noopener noreferrer">Open at source →</Anchor>
          </Stack>
        </Box>
        {call.keywords.length > 0 && (
          <Group mt="sm" gap="0.35rem" align="center">
            <Text c="dimmed" fz="0.8rem">Keywords:</Text>
            {call.keywords.map((kw) => (
              <Badge key={kw} variant="default" tt="none" radius="xl">{kw}</Badge>
            ))}
          </Group>
        )}
      </Box>

      {call.summary && (
        <Box component="section">
          <Title order={2} fz="1.05rem" mb="xs">Summary</Title>
          <Text>{call.summary}</Text>
        </Box>
      )}

      {criteria.length > 0 && (
        <Box component="section">
          <Title order={2} fz="1.05rem" mb="xs">Criterion breakdown</Title>
          <Stack gap="0.6rem">
            {criteria.map((c) => (
              <Card key={c.criterion} withBorder radius="sm" py="0.6rem" px="0.75rem">
                <Group justify="space-between" fw={600} mb="0.25rem">
                  <Text span>{c.label}</Text>
                  <Text span fw={700}>{c.is_topic_fit ? `${c.rating}/5` : c.rating}</Text>
                </Group>
                {c.detail && <Text fz="0.9rem">{c.detail}</Text>}
                {c.rationale && (!c.detail || !c.detail.includes(c.rationale)) && (
                  <Text c="dimmed" fz="0.8rem">
                    {c.is_reason_code ? <>Reason code: <Code>{c.rationale}</Code></> : c.rationale}
                  </Text>
                )}
                {c.quoted_evidence && (
                  <Text component="blockquote" fs="italic" c="dimmed" pl="sm" mt="0.4rem"
                        style={{ borderLeft: "2px solid var(--app-border-strong)" }}>
                    "{c.quoted_evidence}"
                  </Text>
                )}
              </Card>
            ))}
          </Stack>
        </Box>
      )}

      <Group component="section" aria-label="Hide this call" align="center" gap="sm">
        {hiddenId !== null ? (
          <>
            <Text span c="dimmed">Call hidden.</Text>
            <Button variant="subtle" size="xs" disabled={unhide.isPending}
                    onClick={async () => { await unhide.mutateAsync(hiddenId); setHiddenId(null); }}>
              Undo
            </Button>
            <Button variant="subtle" size="xs" onClick={() => navigate("/")}>
              Back to all calls
            </Button>
          </>
        ) : confirmHide ? (
          <>
            <Text span c="dimmed">Hide this call from your feed?</Text>
            <Button variant="default" size="xs" disabled={hide.isPending}
                    onClick={async () => { await hide.mutateAsync(call.id); setConfirmHide(false); setHiddenId(call.id); }}>
              Yes, hide it
            </Button>
            <Button variant="subtle" size="xs" onClick={() => setConfirmHide(false)}>
              Cancel
            </Button>
          </>
        ) : (
          <>
            <Button variant="default" size="xs" onClick={() => setConfirmHide(true)}>
              Hide this call
            </Button>
            <Text c="dimmed" fz="0.8rem">Removes it from your feed. Other users are unaffected.</Text>
          </>
        )}
      </Group>
    </Stack>
  );
}
```

- [ ] **Step 2: Run the test green (from `frontend/`)**

Run: `pnpm exec vitest run src/routes/CallDetail.test.tsx`
Expected: PASS. `findByRole("heading", { name: "Genomics Grant" })` → the `Title order={1}`; `getByText("72")).toHaveAttribute("data-band","strong")` + `findByText("0")).toHaveAttribute("data-band","low")` → the score `Text` keeps `data-band`; `getByText("Deep summary.")`/`getByText("Budget & funding")`/`getByText("Cosine similarity 0.420 (pass threshold 0.30).")` resolve; `findByRole("link", { name: /start drafting/i })` → the `Button component={Link}` `<a href>` with `call_id=5`+`profile_id=7`; the not-found path's `getByRole("alert").textContent).toBe("Call not found.")` (single `Text role="alert"` node); the hide → confirm → undo flow buttons (`/hide this call/i`, `/yes, hide it/i`, `/cancel/i`, `/undo/i`, `/back to all calls/i`) all resolve and POST/DELETE `"/api/calls/{call_id}/hide"`; `getByText("2026-09-16")` (DeadlineLocal). No test edits.

- [ ] **Step 3: Delete module + grep**

Run (from `frontend/`): `grep -rln "CallDetail.module.css" src` → expect none. Then `git rm src/routes/CallDetail.module.css`.

- [ ] **Step 4: Gates (from `frontend/`):** `pnpm exec tsc --noEmit` · `pnpm exec vitest run` · `pnpm build` — all green.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(frontend): re-skin CallDetail to Mantine; drop CallDetail.module.css"
```

---

## Task 5: Re-skin the Workspaces routes (`WorkspaceList` + `WorkspaceCreate`)

**Files:** Modify `frontend/src/routes/WorkspaceList.tsx`, `frontend/src/routes/WorkspaceCreate.tsx`; Delete `frontend/src/routes/WorkspaceList.module.css`, `frontend/src/routes/WorkspaceCreate.module.css`. Tests (unchanged): `WorkspaceList.test.tsx` (`renderWithProviders`), `WorkspaceCreate.test.tsx` (local `MantineProvider`).
**Interfaces:** `WorkspaceList` consumes `Deadline` from `./Deadline` (Task 3, Mantine).

- [ ] **Step 1: Replace `WorkspaceList.tsx`**

```tsx
import { Anchor, Badge, Box, Group, Stack, Text, Title } from "@mantine/core";
import { Link, useSearchParams } from "react-router-dom";

import type { components } from "../api/schema.gen";
import { useWorkspaces } from "../api/workspace";
import { Deadline } from "./Deadline";

type WsItem = components["schemas"]["WorkspaceListItemOut"];

const STATUS_LABEL: Record<string, string> = {
  drafting: "Drafting",
  submitted: "Submitted",
  awarded: "Awarded",
  rejected: "Rejected",
  abandoned: "Abandoned",
};

function WorkspaceRow({ ws }: { ws: WsItem }) {
  // Both setup- and active-phase workspaces open in the React cockpit (Slice E
  // brought the setup + rubric flow in-cockpit). The "Setting up" badge marks setup.
  const setup = ws.current_phase === "setup";
  return (
    <Group
      component="li"
      justify="space-between"
      align="flex-start"
      gap="sm"
      wrap="nowrap"
      px="0.25rem"
      py="xs"
      style={{ borderBottom: "1px solid var(--app-border)" }}
    >
      <Box miw={0}>
        <Anchor component={Link} fw={500} to={`/cockpit/${ws.id}`}>
          {ws.title}
        </Anchor>
        <Group mt="0.15rem" gap="0.4rem" align="center" c="dimmed" fz="0.85rem">
          <Text span tt="uppercase" fz="0.72rem" c="var(--app-link)">{STATUS_LABEL[ws.status] ?? ws.status}</Text>
          {ws.linked_call_title && (
            <>
              <Text span aria-hidden> · </Text>
              <Text span>{ws.linked_call_title}</Text>
            </>
          )}
          {ws.deadline && (
            <>
              <Text span aria-hidden> · </Text>
              <Deadline iso={ws.deadline} />
            </>
          )}
          <Text span aria-hidden> · </Text>
          {/* updated_at is an ISO string; slice to YYYY-MM-DD for a stable label. */}
          <Text span>Updated {ws.updated_at.slice(0, 10)}</Text>
          {!ws.is_owner && <Badge variant="default" size="xs">Shared</Badge>}
          {setup && <Badge variant="default" size="xs">Setting up</Badge>}
        </Group>
      </Box>
    </Group>
  );
}

export function WorkspaceList() {
  const [sp] = useSearchParams();
  const status = sp.get("status");
  const { data, isLoading, isError } = useWorkspaces(status);

  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError) return <Text role="alert" p="md">Couldn&apos;t load workspaces.</Text>;

  const items = data ?? [];
  return (
    <Box component="main">
      <Group justify="space-between" align="center" gap="sm">
        <Title order={1} fz="1.1rem">Workspaces</Title>
        <Anchor component={Link} fw={500} fz="0.9rem" to="/">
          New workspace
        </Anchor>
      </Group>
      {items.length === 0 ? (
        <Text c="dimmed" my="md">
          No workspaces yet — <Anchor component={Link} to="/">pick a call to start drafting</Anchor>.
        </Text>
      ) : (
        <Box component="ul" p={0} my="xs">
          {items.map((ws) => (
            <WorkspaceRow key={ws.id} ws={ws} />
          ))}
        </Box>
      )}
    </Box>
  );
}
```

- [ ] **Step 2: Replace `WorkspaceCreate.tsx`**

```tsx
import { Anchor, Box, Button, Stack, Text, TextInput, Title } from "@mantine/core";
import { type FormEvent, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { useCreateWorkspace } from "../api/workspace";

export function WorkspaceCreate() {
  const [sp] = useSearchParams();
  const callIdRaw = sp.get("call_id");
  const callId = callIdRaw ? Number(callIdRaw) : null;
  const callTitle = sp.get("call_title") ?? "";
  const profileIdRaw = sp.get("profile_id");
  const profileId = profileIdRaw ? Number(profileIdRaw) : null;

  const [title, setTitle] = useState(callTitle);
  const [deadline, setDeadline] = useState("");
  const create = useCreateWorkspace();
  const navigate = useNavigate();

  // From-call only: without a call there is nothing to draft against (Phase-0
  // requires a call). Guard a direct hit / refresh-without-params.
  if (callId === null || Number.isNaN(callId)) {
    return (
      <Box component="main" role="alert" maw="32rem">
        <Title order={1}>Start drafting</Title>
        <Text>Pick a call from the dashboard to start drafting.</Text>
        <Anchor component={Link} to="/">← Back to calls</Anchor>
      </Box>
    );
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const out = await create.mutateAsync({
      call_id: callId as number,
      title,
      deadline: deadline || undefined,
      profile_id: profileId,
    });
    // Setup + rubric now live in the React cockpit (Slice E).
    navigate(`/cockpit/${out.id}`);
  }

  return (
    <Box component="main" maw="32rem">
      <Title order={1}>New workspace</Title>
      <Text c="dimmed" fz="0.9rem" mt="0.25rem" mb="md">Drafting for: {callTitle || `call #${callId}`}</Text>
      <Box component="form" onSubmit={onSubmit}>
        <Stack gap="0.85rem" align="flex-start">
          <TextInput
            label="Title"
            w="100%"
            value={title}
            onChange={(e) => setTitle(e.currentTarget.value)}
            required
            maxLength={500}
          />
          <TextInput
            label="Deadline (optional)"
            type="datetime-local"
            w="100%"
            value={deadline}
            onChange={(e) => setDeadline(e.currentTarget.value)}
          />
          <Button type="submit" variant="default" disabled={create.isPending || !title.trim()}>
            {create.isPending ? "Creating…" : "Start drafting"}
          </Button>
          {create.isError && (
            <Text role="alert" c="var(--app-danger, #e5484d)" fz="0.85rem">
              {create.error instanceof Error ? create.error.message : "Couldn't create the workspace."}
            </Text>
          )}
        </Stack>
      </Box>
    </Box>
  );
}
```

- [ ] **Step 3: Run the tests green (from `frontend/`)**

Run: `pnpm exec vitest run src/routes/WorkspaceList.test.tsx src/routes/WorkspaceCreate.test.tsx`
Expected: PASS. WorkspaceList: `findByRole("link", { name: "AMR grant" }|/setting-up workspace title/i)` hrefs; `getByText("Setting up")`/`getByText("Shared")` (the `Badge` text nodes); `findByText("Submitted")` (the status `Text span`); `getByText(/Updated 2026-06-20/)`; the empty-state `findByText(/pick a call to start drafting/i)` (the `Anchor`); the `?status` query passthrough (logic untouched). WorkspaceCreate: `getByLabelText("Title")` (the Mantine `TextInput` label, accessible name "Title" — the `required` asterisk is `aria-hidden`); `getByRole("button", { name: "Start drafting" })` POSTs and navigates to `/cockpit/new-9`; the missing-`call_id` guard shows `/pick a call from the dashboard/i` and `queryByRole("button", { name: "Start drafting" })` is null. No test edits.

- [ ] **Step 4: Delete modules + grep**

Run (from `frontend/`): `grep -rln "WorkspaceList.module.css\|WorkspaceCreate.module.css" src` → expect none. Then `git rm src/routes/WorkspaceList.module.css src/routes/WorkspaceCreate.module.css`.

- [ ] **Step 5: Teardown grep (slice gate)**

Run (from `frontend/`):
```bash
grep -rln "CallsFilters.module.css\|WorkspacesFilters.module.css\|Pagination.module.css\|Deadline.module.css\|DashboardHeader.module.css\|CallRow.module.css\|Dashboard.module.css\|CallDetail.module.css\|WorkspaceList.module.css\|WorkspaceCreate.module.css" src || echo "10 slice-5 modules gone — clean"
```
Expected: clean. Then sanity-check the sweep is closed (only the deliberate exceptions remain): `find src -name "*.module.css" | sort` should list EXACTLY these ten survivors and nothing else — the three cockpit modules (`cockpit/chat/CockpitChat.module.css`, `cockpit/CockpitPanels.module.css`, `cockpit/CockpitSidebar.module.css`), the five IDE-shell modules (`shell/ActivityBar.module.css`, `shell/AppShell.module.css`, `shell/Sidebar.module.css`, `shell/StatusBar.module.css`, `shell/TitleBar.module.css`), `routes/AdminNav.module.css`, and `components/MatchBadge.module.css`. (There is no `ErrorBoundary` module — it uses inline framework-free markup.) Any `routes/`-content or `components/Pagination` module still present is a miss.

- [ ] **Step 6: Gates (from `frontend/`):** `pnpm exec tsc --noEmit` · `pnpm exec vitest run` · `pnpm build` — all green.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(frontend): re-skin WorkspaceList + WorkspaceCreate to Mantine; drop 2 modules"
```

---

## Verification (whole slice, before merge)

1. `pnpm exec tsc --noEmit` clean.
2. `pnpm exec vitest run` full green. Only two test files changed (`Dashboard.test.tsx`, `CallRow.test.tsx`) and ONLY their render helpers gained a `MantineProvider` wrapper — every assertion byte-unchanged. All other Slice-5 test files byte-unchanged.
3. `pnpm build` succeeds.
4. Teardown grep: the ten Slice-5 modules unreferenced and deleted; `find src -name "*.module.css" | sort` lists EXACTLY the ten sanctioned survivors — 3 cockpit (`CockpitChat`/`CockpitPanels`/`CockpitSidebar`), 5 IDE shell (`ActivityBar`/`AppShell`/`Sidebar`/`StatusBar`/`TitleBar`), `AdminNav`, `MatchBadge` (no `ErrorBoundary` module exists).
5. **Visual:** the backendless Playwright route-mock harness ([[reference_spa_visual_check]]) over `/` (Dashboard list + header + Pagination + a past/near/far deadline row to confirm the four `data-deadline-state` colours), `/calls/:id` (CallDetail — ranking hero with the `data-band` figure colour, analyst-note + criteria cards, keyword chips, the hide→confirm→undo buttons), `/cockpit` (WorkspaceList — status/Shared/Setting-up badges, empty state), `/cockpit/new?call_id=…&call_title=…` (WorkspaceCreate form + the missing-param guard), and the two sidebars (`CallsFilters`/`WorkspacesFilters` heading + controls). Mock `/api/me` as authed (`{ user: {…}, capabilities: { admin: false } }`) plus each route's endpoint (mirror the fixtures in each route test). Confirm the `RouteSpinner` (`Center`+`Loader`) and soft-404 (`Center`+`Title`, `role="alert"`) by hitting an unknown in-app path.
6. Independent diff-vs-parity review before merge: no copy drift (headings, count labels "open"/"top matches"/"closing within 30 days", "Start drafting"/"Start drafting workspace", "Hide this call"/"Yes, hide it"/"Undo"/"Back to all calls", "New workspace", "Setting up"/"Shared", "Not found", filter labels); `data-band` + `data-deadline-state` preserved; every original `role="alert"` preserved and none added; mutation calls + hide/confirm/undo state machine + pagination/profile-persist logic unchanged; `router.tsx` manifest/paths/handles byte-unchanged (only `RouteSpinner` + soft-404 element differ); the two test changes are provider-wrapper-only.

## Finishing

Controller-only, at the user's review gate (not a task): merge `feat/mantine-sweep-dashboard` to LOCAL `master` with `--no-ff` (never push), then `pnpm build` and copy `dist/*` into the gitignored `src/call_manager/web/spa/` to restage the bundle, then clean up the worktree/branch (superpowers:finishing-a-development-branch). This commit **closes the app-wide Mantine sweep** — update the sweep tracker memory.

---

## Self-Review

**Spec coverage** (against `2026-06-28-app-mantine-sweep-design.md`, Slice 5): Dashboard/DashboardHeader/Deadline/CallRow/CallDetail/WorkspaceList/WorkspaceCreate → Mantine ✓ (Tasks 3-5); `Pagination` → Group-of-Buttons, module deleted ✓ (Task 3); `CallsFilters`/`WorkspacesFilters` wrapper+heading+error → Stack/Text/Text-role-alert, modules deleted ✓ (Task 1); shared `RouteSpinner` + soft-404 → Center/Loader + Center/Title ✓ (Task 2). Ten modules retired. After this slice the only non-cockpit raw UI left is the deliberately-excepted `ErrorBoundary` + scoped nav chrome (IDE shell + `AdminNav`) — matching the spec's closing claim.

**Placeholder scan:** No "TBD/TODO". Full target file for every changed component; every run step has command + expected result.

**Type/name consistency:** `Deadline` keeps `deadlineView`/`deadlineLocalView`/`Deadline`/`DeadlineLocal` signatures (consumed by CallRow/WorkspaceList/CallDetail); `Pagination` keeps `{ page, hasPrev, hasNext, onPage }`; all data hooks consumed unchanged; `budgetText` + the hide/confirm/undo machine preserved; `e.currentTarget.value` throughout; `type FormEvent` kept in WorkspaceCreate.

**Behaviour parity:** loading/error early-returns preserve copy and the original `role` (Dashboard/WorkspaceList/CallDetail/WorkspaceCreate errors keep `role="alert"`; loading has none); `data-band` (CallDetail figure: strong≥70/mid>0/low) and `data-deadline-state` (Deadline) hooks preserved; the score-figure band→colour map and deadline state→colour map reproduce the retired modules; pagination URL writes, profile persist-first, debounced search, and the hide confirm/undo flow are untouched; `router.tsx` manifest is byte-unchanged outside `RouteSpinner` + the soft-404 element. The two sanctioned test edits add only a `MantineProvider` wrapper (Mantine throws without one) and change no assertion.
