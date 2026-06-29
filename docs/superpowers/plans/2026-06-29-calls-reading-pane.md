# Funding Calls Reading Pane — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Funding calls page an Outlook-style layout — the list stays in the center, and clicking a call opens its full detail in the shell's existing right pane — while keeping the `/calls/:id` full page for deep links, rendered from one shared detail component.

**Architecture:** Selection lives in the URL (`/?selected=123`) on the index (Dashboard) route. The detail body is extracted from `CallDetail` into a presentational `CallDetailBody` shared by the full page and a new `CallReadingPane`, which the shell mounts via the existing `handle.RightPane` mechanism. A row click sets `selected` (preserving other params); the shell's right-pane toggle/resize labels are parameterized so the calls route reads "reading pane" while the cockpit keeps "chat".

**Tech Stack:** React 19 + Vite, TypeScript, Mantine v9, react-router-dom v6, TanStack Query, `react-resizable-panels` (already wired in the shell), Vitest + Testing Library.

## Global Constraints

- **Frontend has NO TS linter — `tsc` is the gate.** Typecheck with `pnpm exec tsc --noEmit`; full build is `pnpm build` (`tsc && vite build`). Run tests with `pnpm exec vitest run <file>`. All commands run from `frontend/`.
- **No backend / OpenAPI / routes.json changes.** Selection is a query param on the existing index route; `/calls/:id` already exists. Do not regenerate `schema.gen.ts`.
- **Preserve the cockpit's existing accessible labels:** the right-pane toggle stays `"Toggle chat panel"` and its resize handle stays `"Resize chat"` when a route supplies no `rightPaneLabel`. `frontend/src/shell/TitleBar.test.tsx` and `frontend/src/shell/AppShell.test.tsx` assert these verbatim — keep them green.
- **`CallRow`'s title-link target changes intentionally** from `/calls/:id` to the reading-pane selection URL. Updating the two affected assertions in `frontend/src/routes/CallRow.test.tsx` is sanctioned by this plan.
- **v1 scope boundaries:** hide/unhide stays on the full page only; Calls list only (not Workspaces); the reading pane reuses the shell's single `rightpane` panel (shares remembered width with the cockpit chat); no keyboard list navigation.
- **Use `--app-*` CSS variables** (e.g. `--app-selected`, `--app-border`), never hard-coded colors or retired Jinja globals.
- Mantine gotchas in play: `Box`/`Group`/`Center` are polymorphic (`component="aside"`/`"li"` typecheck + forward `aria-*`/`onClick`); a `Box component="aside"` exposes `role="complementary"`; Mantine throws without a `MantineProvider`, so bare-render tests wrap in one.

---

### Task 1: `callSelection` URL helpers

**Files:**
- Create: `frontend/src/routes/callSelection.ts`
- Test: `frontend/src/routes/callSelection.test.ts`

**Interfaces:**
- Produces:
  - `selectionSearch(current: URLSearchParams, id: number, profileId?: number | null): string` — returns a `URLSearchParams` query string (no leading `?`) that selects `id`: copies `current`, sets `selected=<id>`, and when `profileId != null` pins `profile=<profileId>` (so the pane ranks against the list's effective profile even when it came from the user's default, not the URL).
  - `deselectSearch(current: URLSearchParams): string` — copies `current`, deletes `selected`, returns the query string.

- [ ] **Step 1: Write the failing test**

```ts
// frontend/src/routes/callSelection.test.ts
import { describe, expect, it } from "vitest";

import { deselectSearch, selectionSearch } from "./callSelection";

describe("selectionSearch", () => {
  it("sets selected and preserves existing params", () => {
    const out = new URLSearchParams(selectionSearch(new URLSearchParams("source=eu&page=2"), 7));
    expect(out.get("selected")).toBe("7");
    expect(out.get("source")).toBe("eu");
    expect(out.get("page")).toBe("2");
    expect(out.has("profile")).toBe(false);
  });

  it("pins the profile when profileId is provided", () => {
    const out = new URLSearchParams(selectionSearch(new URLSearchParams(""), 7, 3));
    expect(out.get("selected")).toBe("7");
    expect(out.get("profile")).toBe("3");
  });

  it("does not mutate the input params", () => {
    const input = new URLSearchParams("a=1");
    selectionSearch(input, 7, 3);
    expect(input.has("selected")).toBe(false);
    expect(input.has("profile")).toBe(false);
  });
});

describe("deselectSearch", () => {
  it("removes selected and keeps the rest", () => {
    const out = new URLSearchParams(deselectSearch(new URLSearchParams("selected=7&source=eu")));
    expect(out.has("selected")).toBe(false);
    expect(out.get("source")).toBe("eu");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm exec vitest run src/routes/callSelection.test.ts`
Expected: FAIL — `Cannot find module './callSelection'`.

- [ ] **Step 3: Write minimal implementation**

```ts
// frontend/src/routes/callSelection.ts

// URL helpers for the dashboard reading pane. Selection lives in the list's
// query string so it is addressable (refresh/back/share) and survives
// pagination/filter changes. Pure functions — no router, unit-testable.

// Query string (no leading "?") that selects `id` in the reading pane: copies
// the current params, sets `selected`, and pins `profile` to the list's
// effective profile when given so the pane ranks against the same profile.
export function selectionSearch(
  current: URLSearchParams,
  id: number,
  profileId?: number | null,
): string {
  const next = new URLSearchParams(current);
  next.set("selected", String(id));
  if (profileId != null) next.set("profile", String(profileId));
  return next.toString();
}

// Query string with `selected` removed (deselect / close the pane).
export function deselectSearch(current: URLSearchParams): string {
  const next = new URLSearchParams(current);
  next.delete("selected");
  return next.toString();
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm exec vitest run src/routes/callSelection.test.ts`
Expected: PASS (4 tests).

- [ ] **Step 5: Typecheck + commit**

```bash
pnpm exec tsc --noEmit
git add src/routes/callSelection.ts src/routes/callSelection.test.ts
git commit -m "feat(frontend): add callSelection URL helpers for the reading pane"
```

---

### Task 2: Extract `CallDetailBody` and refactor `CallDetail` to use it

**Files:**
- Create: `frontend/src/routes/CallDetailBody.tsx`
- Test: `frontend/src/routes/CallDetailBody.test.tsx`
- Modify: `frontend/src/routes/CallDetail.tsx` (replace its body with `<CallDetailBody {...data} />`; keep the page chrome + hide/unhide)
- Existing (must stay green): `frontend/src/routes/CallDetail.test.tsx`

**Interfaces:**
- Consumes: `CallDetail` type from `../api/calls` (`components["schemas"]["CallDetailOut"]` = `{ call, ranking, criteria, best_profile_id }`); `DeadlineLocal` from `./Deadline`.
- Produces: `CallDetailBody(props: CallDetail): JSX.Element` — a **Fragment** of the detail sections (header with "Start drafting workspace", ranking hero, analyst notes, details grid, summary, criterion breakdown). No hooks, no routing state, no mutations. The parent supplies the wrapping `<Stack>` for vertical rhythm.

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/src/routes/CallDetailBody.test.tsx
import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it } from "vitest";

import type { CallDetail } from "../api/calls";
import { theme } from "../theme/theme";
import { CallDetailBody } from "./CallDetailBody";

const DATA = {
  call: {
    id: 5, title: "Genomics Grant", source_name: "Vinnova", programme: "Prog",
    external_id: "EX-1", published_at: null, deadline: null, budget_min: null,
    budget_max: null, currency: null, url: "https://x", keywords: ["dna"], summary: "Sum.",
  },
  ranking: {
    aggregate_score: 72, aggregate_reason_label: null, model_label: "Sonnet 4.6",
    rationale: "Strong fit.", embed_similarity: 0.42, escalated: false,
    escalated_from_score: null, analysis_model_label: "Sonnet 4.6", summary: "Deep summary.",
    applicant_notes: [{ category: "budget", heading: "Budget & funding", text: "Note." }],
  },
  criteria: [],
  best_profile_id: 7,
} as unknown as CallDetail;

function renderBody(data: CallDetail) {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <MemoryRouter>{<CallDetailBody {...data} />}</MemoryRouter>
    </MantineProvider>,
  );
}

it("renders the title heading, ranking score band, and analyst notes", () => {
  renderBody(DATA);
  expect(screen.getByRole("heading", { name: "Genomics Grant" })).toBeInTheDocument();
  expect(screen.getByText("72")).toHaveAttribute("data-band", "strong");
  expect(screen.getByText("Deep summary.")).toBeInTheDocument();
  expect(screen.getByText("Budget & funding")).toBeInTheDocument();
});

it("Start drafting links to /cockpit/new with the call + best profile prefilled", () => {
  renderBody(DATA);
  const href = screen.getByRole("link", { name: /start drafting/i }).getAttribute("href") ?? "";
  expect(href).toContain("/cockpit/new?");
  expect(href).toContain("call_id=5");
  expect(href).toContain("profile_id=7");
});

it("renders the low band for a zero aggregate score", () => {
  renderBody({ ...DATA, ranking: { ...DATA.ranking, aggregate_score: 0 } } as CallDetail);
  expect(screen.getByText("0")).toHaveAttribute("data-band", "low");
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm exec vitest run src/routes/CallDetailBody.test.tsx`
Expected: FAIL — `Cannot find module './CallDetailBody'`.

- [ ] **Step 3: Create `CallDetailBody.tsx`**

Move the presentational body out of `CallDetail` verbatim (the header through the criterion breakdown), parameterized by props:

```tsx
// frontend/src/routes/CallDetailBody.tsx
import { Anchor, Badge, Box, Button, Card, Code, Group, Stack, Text, Title } from "@mantine/core";
import { Link } from "react-router-dom";

import type { CallDetail } from "../api/calls";
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

// Presentational call detail, shared verbatim by the /calls/:id full page
// (CallDetail) and the dashboard reading pane (CallReadingPane). Pure render of
// the useCallDetail payload — no hooks, no routing state, no mutations. Returns a
// Fragment so the parent's <Stack> controls vertical rhythm. Hide/unhide and the
// page chrome live in CallDetail (v1: hide is full-page only).
export function CallDetailBody({ call, ranking, criteria, best_profile_id }: CallDetail) {
  const startParams = new URLSearchParams({ call_id: String(call.id), call_title: call.title });
  if (best_profile_id != null) startParams.set("profile_id", String(best_profile_id));

  const band = ranking
    ? ranking.aggregate_score >= 70 ? "strong" : ranking.aggregate_score > 0 ? "mid" : "low"
    : "low";
  const figureColor =
    band === "strong" ? "var(--app-good)" : band === "mid" ? "var(--app-warn)" : "var(--app-dim)";

  return (
    <>
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
    </>
  );
}
```

- [ ] **Step 4: Refactor `CallDetail.tsx` to render the shared body**

Replace the entire file with the version below: it keeps the page chrome ("← All calls", the centered `<main>`, hide/unhide) and delegates the body to `CallDetailBody`. The body imports (`Badge`, `Box`, `Card`, `Code`, `Title`, `DeadlineLocal`, `budgetText`, `GRID_AUTOFILL`) are now gone from this file.

```tsx
// frontend/src/routes/CallDetail.tsx
import { Anchor, Button, Group, Stack, Text } from "@mantine/core";
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { useCallDetail, useHideCall, useUnhideCall } from "../api/calls";
import { CallDetailBody } from "./CallDetailBody";

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

  const { call } = data;

  return (
    <Stack component="main" maw="52rem" mx="auto" px="xs" pt="md" pb="xl" gap="lg">
      <Anchor component={Link} to="/" size="sm">← All calls</Anchor>

      <CallDetailBody {...data} />

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

- [ ] **Step 5: Run both test files to verify they pass**

Run: `pnpm exec vitest run src/routes/CallDetailBody.test.tsx src/routes/CallDetail.test.tsx`
Expected: PASS — the new `CallDetailBody` suite (3) and the unchanged `CallDetail` suite (all existing tests, including hide/unhide, still green because the rendered output is identical).

- [ ] **Step 6: Typecheck + commit**

```bash
pnpm exec tsc --noEmit
git add src/routes/CallDetailBody.tsx src/routes/CallDetailBody.test.tsx src/routes/CallDetail.tsx
git commit -m "refactor(frontend): extract CallDetailBody shared by full page and reading pane"
```

---

### Task 3: `CallReadingPane`

**Files:**
- Create: `frontend/src/routes/CallReadingPane.tsx`
- Test: `frontend/src/routes/CallReadingPane.test.tsx`

**Interfaces:**
- Consumes: `useCallDetail` from `../api/calls`; `CallDetailBody` from `./CallDetailBody`; `deselectSearch` from `./callSelection`; `useSearchParams` from `react-router-dom`.
- Produces: `CallReadingPane(): JSX.Element` — the `RightPane` handle component. Always renders an `<aside aria-label="Call preview">` landmark. With no `selected` param → a "Select a call to preview" placeholder. With `selected` → a header (an "Open full page ↗" link to `/calls/:selected?profile=…` and a "Close preview" button that strips `selected`) above the fetched `<CallDetailBody/>` in an independently scrollable region. States: placeholder · loading · error · not-found.

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/src/routes/CallReadingPane.test.tsx
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { theme } from "../theme/theme";
import { CallReadingPane } from "./CallReadingPane";

afterEach(() => { vi.restoreAllMocks(); });

const RANKED = {
  call: {
    id: 5, title: "Genomics Grant", source_name: "Vinnova", programme: "Prog",
    external_id: "EX-1", published_at: null, deadline: null, budget_min: null,
    budget_max: null, currency: null, url: "https://x", keywords: ["dna"], summary: "Sum.",
  },
  ranking: {
    aggregate_score: 72, aggregate_reason_label: null, model_label: "Sonnet 4.6",
    rationale: "Strong fit.", embed_similarity: 0.42, escalated: false,
    escalated_from_score: null, analysis_model_label: "Sonnet 4.6", summary: "Deep summary.",
    applicant_notes: [],
  },
  criteria: [],
  best_profile_id: 7,
};

function renderPane(url: string) {
  const qc = new QueryClient();
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[url]}>
          <CallReadingPane />
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

it("shows a placeholder and the landmark when nothing is selected", () => {
  renderPane("/");
  expect(screen.getByRole("complementary", { name: /call preview/i })).toBeInTheDocument();
  expect(screen.getByText(/select a call to preview/i)).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: /open full page/i })).toBeNull();
});

it("renders the selected call's detail with an Open-full-page link", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  renderPane("/?selected=5&profile=7");
  expect(await screen.findByRole("heading", { name: "Genomics Grant" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /open full page/i })).toHaveAttribute("href", "/calls/5?profile=7");
});

it("close clears the selection and returns to the placeholder", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: RANKED, error: undefined } as never);
  renderPane("/?selected=5&profile=7");
  await screen.findByRole("heading", { name: "Genomics Grant" });
  fireEvent.click(screen.getByRole("button", { name: /close preview/i }));
  expect(screen.getByText(/select a call to preview/i)).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Genomics Grant" })).toBeNull();
});

it("shows a not-found message when the call is missing", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: undefined,
    error: { error: { code: "not_found", message: "call not found" } },
  } as never);
  renderPane("/?selected=999");
  expect(await screen.findByText("Call not found.")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm exec vitest run src/routes/CallReadingPane.test.tsx`
Expected: FAIL — `Cannot find module './CallReadingPane'`.

- [ ] **Step 3: Write the implementation**

```tsx
// frontend/src/routes/CallReadingPane.tsx
import { ActionIcon, Anchor, Box, Center, Group, Stack, Text } from "@mantine/core";
import { Link, useSearchParams } from "react-router-dom";

import { useCallDetail } from "../api/calls";
import { CallDetailBody } from "./CallDetailBody";
import { deselectSearch } from "./callSelection";

// Inner data-fetcher. Split out so useCallDetail is called unconditionally (the
// outer pane decides placeholder-vs-preview by conditionally RENDERING this).
function CallPreview({ callId, profile }: { callId: number; profile: number | null }) {
  const { data, isLoading, isError, error } = useCallDetail(callId, profile);
  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data) {
    const notFound = (error as { code?: string } | null)?.code === "not_found";
    return <Text role="alert" p="md">{notFound ? "Call not found." : "Couldn't load this call."}</Text>;
  }
  return (
    <Stack gap="lg" px="md" py="md">
      <CallDetailBody {...data} />
    </Stack>
  );
}

// Dashboard reading pane (the shell's RightPane on the calls route). Selection is
// the `selected` URL param; `profile` mirrors the list's effective profile so the
// pane's ranking matches the list. Always renders the <aside> landmark.
export function CallReadingPane() {
  const [sp, setSp] = useSearchParams();
  const selected = sp.get("selected");
  const profileParam = sp.get("profile");
  const profile = profileParam != null && profileParam !== "" ? Number(profileParam) : null;

  return (
    <Box component="aside" aria-label="Call preview" h="100%" style={{ display: "flex", flexDirection: "column" }}>
      {selected ? (
        <>
          <Group
            justify="space-between"
            align="center"
            px="md"
            py="xs"
            flex="0 0 auto"
            style={{ borderBottom: "1px solid var(--app-border)" }}
          >
            <Anchor
              component={Link}
              fz="0.85rem"
              fw={500}
              to={`/calls/${selected}${profile != null ? `?profile=${profile}` : ""}`}
            >
              Open full page ↗
            </Anchor>
            <ActionIcon
              variant="subtle"
              aria-label="Close preview"
              onClick={() => setSp(new URLSearchParams(deselectSearch(sp)))}
            >
              ✕
            </ActionIcon>
          </Group>
          <Box style={{ flex: 1, overflowY: "auto" }}>
            <CallPreview callId={Number(selected)} profile={profile} />
          </Box>
        </>
      ) : (
        <Center flex={1} p="md">
          <Text c="dimmed">Select a call to preview</Text>
        </Center>
      )}
    </Box>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm exec vitest run src/routes/CallReadingPane.test.tsx`
Expected: PASS (4 tests).

- [ ] **Step 5: Typecheck + commit**

```bash
pnpm exec tsc --noEmit
git add src/routes/CallReadingPane.tsx src/routes/CallReadingPane.test.tsx
git commit -m "feat(frontend): add CallReadingPane (dashboard right-pane call detail)"
```

---

### Task 4: `CallRow` selects into the pane

**Files:**
- Modify: `frontend/src/routes/CallRow.tsx`
- Modify: `frontend/src/routes/CallRow.test.tsx` (the two title-link assertions change by design; add highlight + row-click coverage)

**Interfaces:**
- Consumes: `selectionSearch` from `./callSelection`; `useSearchParams`, `useNavigate` from `react-router-dom`.
- Produces: unchanged `CallRow({ call, profileId })` signature. The title now links to `{ search: selectionSearch(sp, call.id, profileId) }`; the `<li>` carries `aria-current="true"` + a `--app-selected` background when it is the selected call; clicking empty row area (not a link/button) selects the call; "Start drafting" is unchanged.

- [ ] **Step 1: Update the test to the new behavior (write the failing test)**

Replace the two title-link tests and add highlight + row-click tests. Final file:

```tsx
// frontend/src/routes/CallRow.test.tsx
import { MantineProvider } from "@mantine/core";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { expect, it } from "vitest";

import type { CallItem } from "../api/dashboard";
import { theme } from "../theme/theme";
import { CallRow } from "./CallRow";

function call(over: Partial<CallItem> = {}): CallItem {
  return {
    id: 3,
    title: "Grant X",
    source_name: "Vinnova",
    source_slug: "vinnova",
    deadline: null,
    published_at: null,
    aggregate_score: 82,
    ...over,
  } as CallItem;
}

function renderRow(ui: React.ReactElement, url = "/") {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <MemoryRouter initialEntries={[url]}>{ui}</MemoryRouter>
    </MantineProvider>,
  );
}

it("links the title to the reading-pane selection URL, shows source + match badge", () => {
  renderRow(<CallRow call={call()} />);
  const href = screen.getByRole("link", { name: "Grant X" }).getAttribute("href") ?? "";
  expect(href).toContain("selected=3");
  expect(href).not.toContain("profile=");
  expect(screen.getByText("Vinnova")).toBeInTheDocument();
  expect(screen.getByText("82")).toHaveAttribute("data-band", "strong");
});

it("pins the resolved profile into the selection URL", () => {
  renderRow(<CallRow call={call()} profileId={7} />);
  const href = screen.getByRole("link", { name: "Grant X" }).getAttribute("href") ?? "";
  expect(href).toContain("selected=3");
  expect(href).toContain("profile=7");
});

it("preserves existing list filters/pagination in the selection URL", () => {
  // Guards the core selectionSearch contract at the CallRow level: a row that
  // forgot to pass the live `sp` would still pass the helper unit tests.
  renderRow(<CallRow call={call()} profileId={7} />, "/?source=eu&page=2&q=dna");
  const href = screen.getByRole("link", { name: "Grant X" }).getAttribute("href") ?? "";
  expect(href).toContain("selected=3");
  expect(href).toContain("source=eu");
  expect(href).toContain("page=2");
  expect(href).toContain("q=dna");
  expect(href).toContain("profile=7");
});

it("marks the row as current when it is the selected call", () => {
  renderRow(<CallRow call={call()} />, "/?selected=3");
  expect(screen.getByRole("listitem")).toHaveAttribute("aria-current", "true");
});

it("is not current when a different call is selected", () => {
  renderRow(<CallRow call={call()} />, "/?selected=99");
  expect(screen.getByRole("listitem")).not.toHaveAttribute("aria-current");
});

it("clicking empty row area selects the call", () => {
  function Probe() {
    return <span data-testid="search">{useLocation().search}</span>;
  }
  renderRow(
    <Routes>
      <Route path="/" element={<><CallRow call={call()} profileId={7} /><Probe /></>} />
    </Routes>,
  );
  fireEvent.click(screen.getByRole("listitem"));
  expect(screen.getByTestId("search").textContent).toContain("selected=3");
  expect(screen.getByTestId("search").textContent).toContain("profile=7");
});

it("omits the match badge when the score is null", () => {
  renderRow(<CallRow call={call({ aggregate_score: null })} />);
  expect(screen.queryByText("82")).toBeNull();
});

it("Start drafting links to /cockpit/new with the call prefilled + profile", () => {
  renderRow(<CallRow call={call()} profileId={7} />);
  const href = screen.getByRole("link", { name: "Start drafting" }).getAttribute("href") ?? "";
  expect(href).toContain("/cockpit/new?");
  expect(href).toContain("call_id=3");
  expect(href).toContain("call_title=Grant+X"); // URLSearchParams encodes space as +
  expect(href).toContain("profile_id=7");
});

it("omits profile_id from the Start-drafting link when there is no resolved profile", () => {
  renderRow(<CallRow call={call()} />);
  const href = screen.getByRole("link", { name: "Start drafting" }).getAttribute("href") ?? "";
  expect(href).not.toContain("profile_id");
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm exec vitest run src/routes/CallRow.test.tsx`
Expected: FAIL — the title-link/highlight/row-click tests fail against the current `CallRow` (title still points to `/calls/3`; no `aria-current`; row click does nothing).

- [ ] **Step 3: Update `CallRow.tsx`**

```tsx
// frontend/src/routes/CallRow.tsx
import { Anchor, Box, Group, Stack, Text } from "@mantine/core";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import type { CallItem } from "../api/dashboard";
import { MatchBadge } from "../components/MatchBadge";
import { Deadline } from "./Deadline";
import { selectionSearch } from "./callSelection";

// One call in the list: the title selects the call into the right reading pane
// (URL ?selected=id, preserving filters), a muted meta line (source · deadline),
// the banded match pill, and a "Start drafting" action. The whole row is
// click-to-select for mouse users; the title + Start-drafting anchors remain the
// keyboard/interactive targets, so the row onClick ignores clicks that land on an
// <a>/<button> (no nested-interactive a11y trap, no double navigation). profileId
// is the dashboard's resolved effective profile, pinned so the pane ranks the same.
export function CallRow({ call, profileId }: { call: CallItem; profileId?: number | null }) {
  const [sp] = useSearchParams();
  const navigate = useNavigate();
  const selected = sp.get("selected") === String(call.id);
  const params = new URLSearchParams({ call_id: String(call.id), call_title: call.title });
  if (profileId != null) params.set("profile_id", String(profileId));
  const startHref = `/cockpit/new?${params.toString()}`;
  const onRowClick = (e: React.MouseEvent) => {
    if ((e.target as HTMLElement).closest("a,button")) return; // links/buttons handle themselves
    navigate({ search: selectionSearch(sp, call.id, profileId) });
  };
  return (
    <Group
      component="li"
      justify="space-between"
      align="flex-start"
      gap="sm"
      wrap="nowrap"
      px="0.25rem"
      py="xs"
      onClick={onRowClick}
      aria-current={selected ? "true" : undefined}
      bg={selected ? "var(--app-selected)" : undefined}
      style={{ borderBottom: "1px solid var(--app-border)", cursor: "pointer" }}
    >
      <Box miw={0}>
        <Anchor component={Link} fw={500} to={{ search: selectionSearch(sp, call.id, profileId) }}>
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

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm exec vitest run src/routes/CallRow.test.tsx`
Expected: PASS (9 tests).

- [ ] **Step 5: Typecheck + commit**

```bash
pnpm exec tsc --noEmit
git add src/routes/CallRow.tsx src/routes/CallRow.test.tsx
git commit -m "feat(frontend): CallRow selects into the reading pane (URL ?selected, row highlight)"
```

---

### Task 5: Wire the reading pane into the shell with route-correct labels

**Files:**
- Modify: `frontend/src/router.tsx` (index route gains `RightPane` + `rightPaneLabel`)
- Modify: `frontend/src/shell/AppShell.tsx` (read `rightPaneLabel`; label the resize handle; pass to TitleBar)
- Modify: `frontend/src/shell/TitleBar.tsx` (parameterize the toggle's `aria-label`)
- Modify: `frontend/src/shell/AppShell.test.tsx` (add a labeled-pane test; existing "Resize chat" tests stay)
- Modify: `frontend/src/shell/TitleBar.test.tsx` (add a labeled-toggle test; existing "Toggle chat panel" tests stay)
- Modify: `frontend/src/router.test.tsx` (assert the real `/` index route exposes `CallReadingPane` + `rightPaneLabel`)

**Interfaces:**
- Consumes: `CallReadingPane` from `./routes/CallReadingPane`.
- Produces: a route may declare `handle: { RightPane, rightPaneLabel?: string }`. `AppShell` resolves `rightPaneLabel` from the deepest matched handle and uses it for the resize handle (`Resize <label>`, default `"chat"`) and passes it to `TitleBar`; `TitleBar` accepts `rightPaneLabel?: string` and renders the toggle as `Toggle <label>` (default `"chat panel"`). Defaults preserve the cockpit's existing labels.

- [ ] **Step 1: Write the failing tests**

Add to `frontend/src/shell/TitleBar.test.tsx` — extend `renderWithProps`'s prop type with `rightPaneLabel?: string`, then append:

```tsx
it("labels the toggle from rightPaneLabel when provided", async () => {
  renderWithProps("/", { hasRightPane: true, onToggleChat: vi.fn(), rightPaneLabel: "reading pane" });
  expect(await screen.findByRole("button", { name: "Toggle reading pane" })).toBeInTheDocument();
});
```

(The existing `renderWithProps` signature becomes:
`function renderWithProps(path: string, props: { hasRightPane?: boolean; chatCollapsed?: boolean; onToggleChat?: () => void; rightPaneLabel?: string })`.)

Add to `frontend/src/shell/AppShell.test.tsx`:

```tsx
it("labels the right-pane toggle and resize handle from handle.rightPaneLabel", () => {
  renderShellWithRoutes("/", [
    {
      index: true,
      element: <p>BODY</p>,
      handle: { RightPane: () => <aside aria-label="Call preview" />, rightPaneLabel: "reading pane" },
    },
  ]);
  expect(screen.getByRole("separator", { name: "Resize reading pane" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Toggle reading pane" })).toBeInTheDocument();
});
```

Add to `frontend/src/router.test.tsx` — a real-router assertion that the live `/`
index route actually wires the pane (the synthetic-handle tests above can't catch
a missed or typoed edit to `router.tsx`). Add the import
`import { CallReadingPane } from "./routes/CallReadingPane";` (`RouteObject` is
already imported), then:

```tsx
it("the / index (calls) route exposes the reading pane via its handle", () => {
  const shell = (router.routes as RouteObject[]).find((r) => r.children?.some((c) => c.index));
  const index = shell?.children?.find((c) => c.index);
  const handle = index?.handle as { RightPane?: unknown; rightPaneLabel?: string } | undefined;
  expect(handle?.RightPane).toBe(CallReadingPane);
  expect(handle?.rightPaneLabel).toBe("reading pane");
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pnpm exec vitest run src/shell/TitleBar.test.tsx src/shell/AppShell.test.tsx src/router.test.tsx`
Expected: FAIL — the shell tests can't find "Toggle reading pane" / "Resize reading pane" (labels still hard-coded to chat), and the router test sees `handle.RightPane === undefined` (the index route handle is still `{ Sidebar: CallsFilters }`).

- [ ] **Step 3: Parameterize `TitleBar`'s toggle label**

In `frontend/src/shell/TitleBar.tsx`, add the prop and use it (default preserves "chat panel"):

```tsx
export function TitleBar({
  hasRightPane = false,
  chatCollapsed = false,
  onToggleChat,
  rightPaneLabel,
}: {
  hasRightPane?: boolean;
  chatCollapsed?: boolean;
  onToggleChat?: () => void;
  rightPaneLabel?: string;
} = {}) {
```

and change the toggle button's `aria-label`:

```tsx
            aria-label={`Toggle ${rightPaneLabel ?? "chat panel"}`}
```

- [ ] **Step 4: Read and pass `rightPaneLabel` in `AppShell`, label the resize handle**

In `frontend/src/shell/AppShell.tsx`:

Extend the handle type:

```tsx
type RightPaneHandle = { RightPane?: ComponentType; rightPaneLabel?: string };
```

After the `RightPaneContent` lookup, resolve the label from the deepest matched handle:

```tsx
  const rightPaneLabel = [...matches]
    .reverse()
    .map((m) => (m.handle as RightPaneHandle | undefined)?.rightPaneLabel)
    .find((l): l is string => Boolean(l));
```

Pass it to `TitleBar`:

```tsx
      <TitleBar
        hasRightPane={Boolean(RightPaneContent)}
        chatCollapsed={chatCollapsed}
        onToggleChat={toggleChat}
        rightPaneLabel={rightPaneLabel}
      />
```

Label the right-pane resize handle (default preserves "Resize chat"):

```tsx
              <PanelResizeHandle className={styles.handle} aria-label={`Resize ${rightPaneLabel ?? "chat"}`} />
```

- [ ] **Step 5: Wire the calls route in `router.tsx`**

Add the import:

```tsx
import { CallReadingPane } from "./routes/CallReadingPane";
```

Change the index route's handle:

```tsx
      { index: true, element: <Dashboard />, handle: { Sidebar: CallsFilters, RightPane: CallReadingPane, rightPaneLabel: "reading pane" } },
```

- [ ] **Step 6: Run the shell + router tests to verify they pass**

Run: `pnpm exec vitest run src/shell/TitleBar.test.tsx src/shell/AppShell.test.tsx src/router.test.tsx`
Expected: PASS — new labeled tests pass; the router test confirms the real `/` route exposes `CallReadingPane` + `rightPaneLabel: "reading pane"`; existing "Toggle chat panel" / "Resize chat" tests and the route-drift guard still pass.

- [ ] **Step 7: Typecheck + commit**

```bash
pnpm exec tsc --noEmit
git add src/router.tsx src/shell/AppShell.tsx src/shell/TitleBar.tsx src/shell/AppShell.test.tsx src/shell/TitleBar.test.tsx src/router.test.tsx
git commit -m "feat(frontend): mount the calls reading pane in the shell with route-correct labels"
```

---

### Task 6: Whole-suite verification + production build

**Files:** none (verification only).

- [ ] **Step 1: Typecheck the whole project**

Run: `pnpm exec tsc --noEmit`
Expected: no errors.

- [ ] **Step 2: Run the full frontend test suite**

Run: `pnpm exec vitest run`
Expected: all suites PASS (including `router.test`'s route-drift guard — unaffected, since no route paths or `routes.json` entries changed).

- [ ] **Step 3: Production build**

Run: `pnpm build`
Expected: `tsc` clean, `vite build` emits the bundle with no errors.

- [ ] **Step 4: Manual smoke (optional but recommended)**

With the app running, open `/`, click a call → it appears in the right pane and the row highlights; the URL shows `?selected=<id>`; refresh keeps it open; "Open full page ↗" lands on `/calls/<id>`; the TitleBar toggle (labeled "Toggle reading pane") and the drag handle collapse/resize the pane; "Start drafting" still opens the create form.

- [ ] **Step 5: Commit (only if Step 4 produced fixes; otherwise nothing to commit)**

```bash
git add -A
git commit -m "chore(frontend): reading-pane verification fixups"
```

---

## Self-Review

**Spec coverage:**
- Reading pane shows full detail via shared component → Tasks 2 (`CallDetailBody`) + 3 (`CallReadingPane`). ✓
- Selection in the URL, params preserved, profile pinned → Task 1 (`selectionSearch`) + Task 4 (CallRow uses it). ✓
- `/calls/:id` full page kept, rendered from the shared body → Task 2 (CallDetail refactor). ✓
- Row click selects + highlight + whole-row-click + Start-drafting unchanged → Task 4 (incl. a test that selection preserves live filters/pagination). ✓
- "Open full page ↗" + close/deselect + placeholder/loading/error/not-found → Task 3. ✓
- Shell `RightPane` handle + resize/toggle labels generalized, cockpit labels preserved → Task 5 (incl. a real-router assertion that `/` wires `CallReadingPane` + `rightPaneLabel`). ✓
- v1 boundaries (hide on full page only; Calls only; shared pane width; no keyboard nav) → respected; hide stays in `CallDetail` (Task 2), pane has no hide. ✓
- Testing per spec → each task ships its tests; Task 6 runs the whole suite + build. ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases"; every code step shows complete code and exact commands.

**Type consistency:** `CallDetailBody` consumes `CallDetail` (= `CallDetailOut`) and is rendered as `<CallDetailBody {...data} />` in both CallDetail and CallReadingPane; `selectionSearch(sp, call.id, profileId)`/`deselectSearch(sp)` signatures match their callers in CallRow and CallReadingPane; `rightPaneLabel` is declared on the handle (router), read in AppShell, and accepted by TitleBar with identical naming throughout.
