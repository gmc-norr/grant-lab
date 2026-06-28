# App-Wide Mantine Sweep — Slice 3 (Profiles) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-skin the five Profiles surfaces — `ProfilesList`, `ProfileDetail`, the shared `StructuredFields` form, `ProfileNew`, and `ProfileEdit` — to Mantine v9 primitives, and retire their three CSS modules (`ProfilesList.module.css`, `ProfileDetail.module.css`, `ProfileForm.module.css`).

**Architecture:** Pure frontend re-skin. The display routes (`ProfilesList`, `ProfileDetail`) become Mantine `Card`/`Stack`/`Title`/`Text`/`Badge`/`Anchor`/`Button`. The forms (`ProfileNew`, `ProfileEdit`) and the shared `StructuredFields` become Mantine `TextInput`/`Textarea`/`NativeSelect`/`Fieldset`/`Button` (with `Paper` for ProfileEdit's passive status banners — NOT `Alert`, which would add a live-region role). The data layer (`api/profiles.ts`) and the router are untouched. No behaviour, data-flow, API, route, or copy changes. No schema, no OpenAPI regen.

**Tech Stack:** React 19.2.7, Mantine v9.4.0, react-router-dom, TanStack Query, Vite, Vitest + Testing Library (jsdom), pnpm.

## Global Constraints

_Every task's requirements implicitly include this section._

- **Re-skin only.** No behaviour / data-flow / API / route / copy changes. Pure frontend; no backend, no schema, no OpenAPI regen. `api/profiles.ts` and `router.tsx` are NOT modified.
- **Mantine v9.4.0 primitive for every UI element.** Dark teal theme inherited from `theme`. The only irreducible colour override is `bg="var(--app-sidebar)"` on the section/list `Card`s (preserve the existing surface colour).
- **`tsc` is the type gate** (`noUnusedLocals` — import only what you use; drop each `import styles from "./*.module.css"`). Keep `React.FormEvent` / `React.ChangeEvent` exactly as the originals use them — the repo's tsconfig resolves the global `React` namespace for type positions (the current files already compile this way).
- **Test preservation is the gate.** All five profile test files must stay green; never weaken a query. The ONLY sanctioned test change in the whole slice is wrapping `StructuredFields.test.tsx`'s two renders in `<MantineProvider>` (Task 3 Step 2) — assertions byte-identical — because Mantine v9 throws without a provider. The other four test files are byte-unchanged.
- **`Select` is a custom combobox, NOT a native `<select>`. Use `NativeSelect` for EVERY select in these forms.** This is the slice's headline risk. In particular `ProfileNew.test` drives the Kind select with `fireEvent.change(getByLabelText(/^kind/i), { target: { value: "individual" } })`, which only works on a real `<select>`. `NativeSelect` renders `<select>` (verified). Apply `NativeSelect` to: **Kind, Level, Currency** (ProfileNew/Edit) and **Organization type, Career stage** (StructuredFields). Never `Select` here.
- **Disabled cascade via `Fieldset`.** `ProfileEdit` locks the form by wrapping it in `<fieldset disabled={locked}>`; `StructuredFields` wraps its kind-specific group in `<fieldset disabled={disabled}>`. Replace each with Mantine `<Fieldset disabled={…}>` — Mantine spreads `disabled` onto the native `<fieldset>` (verified), so the native cascade still disables every descendant control and jest-dom `toBeDisabled()` (which walks up to a disabled `<fieldset>` ancestor) keeps passing. Do NOT additionally pass `disabled` to each control; the Fieldset is the single source.
- **`Textarea` keeps a fixed height via `rows={N}`** (forwarded to the native `<textarea>`); do NOT switch to `autosize` (that would change resize behaviour). Preserve each original `rows` value.
- **`Badge` for chips with `tt="none"`** — Mantine `Badge` uppercases via CSS by default; `tt="none"` preserves the original lowercase chip text (the DOM text is unchanged either way, but keep the visual faithful). Use `variant="default"` for the neutral grey pill.
- **Mantine `Button` does not force `type`.** Keep explicit `type="submit"` on the two form submit buttons (Bootstrap profile, Save and re-rank).
- **Links use `Anchor component={Link} to="…"` (inline links) or `Button component={Link} to="…"` (button-styled links).** The original button-styled links (`.newBtn`, `.empty a`, `.actions a.primary`, `.actions a`) become `Button component={Link}`; the inline links (`.back`, card title, `.links a`, Cancel) become `Anchor component={Link}`. All keep their `to`/`href`.
- **`e.currentTarget.value`** (repo idiom) in all Mantine change handlers, not `e.target.value`.
- **Status/error roles preserved:** the create/update error stays `role="alert"` (render as Mantine `Text role="alert" c="red"`); the not-found message stays `role="alert"`.
- **ProfileEdit status banners stay non-live.** The drafting / done / failed banners are plain `<div>`s with NO ARIA role in the original — passive status, not announcements. Do NOT use Mantine `Alert` for them: `Alert` defaults `role="alert"` (an assertive live region), which would turn passive status into screen-reader announcements — a semantic behaviour change that violates the re-skin bar. Render them as Mantine `Paper` (a non-live surface), preserving the original `var(--app-sidebar)` background and the `var(--app-warn)` border on the failed banner.
- **Branch / worktree:** isolated worktree `.worktrees/mantine-sweep-profiles` on branch `feat/mantine-sweep-profiles`. Controller merges to LOCAL master `--no-ff` (never push) at the user's review gate, then rebuilds + restages the gitignored bundle.
- **Whole-suite `vitest` is reliable here** — only backend testcontainers flakes; this slice changes no backend.

---

## File Structure

**Modified (Task 1):** `frontend/src/routes/ProfilesList.tsx`. **Deleted:** `ProfilesList.module.css`.
**Modified (Task 2):** `frontend/src/routes/ProfileDetail.tsx`. **Deleted:** `ProfileDetail.module.css`.
**Modified (Task 3):** `frontend/src/routes/StructuredFields.tsx`, `ProfileNew.tsx`, `ProfileEdit.tsx`, and `StructuredFields.test.tsx` (harness-only: add a `MantineProvider` wrapper — see below). **Deleted:** `ProfileForm.module.css` (shared by New + Edit; deletable only after both convert).
**Unchanged (must stay green):** `ProfilesList.test.tsx`, `ProfileDetail.test.tsx`, `ProfileNew.test.tsx`, `ProfileEdit.test.tsx`.

**Why `StructuredFields.test.tsx` must change:** it currently renders `<StructuredFields>` bare. Mantine v9 components **throw** `[@mantine/core] MantineProvider was not found in tree` without a provider (verified in the installed source). So the test must wrap the renders in `<MantineProvider>` — exactly as `ProfileNew.test`/`ProfileEdit.test`/`ProfileDetail.test` already do. This is a harness adjustment, NOT a weakening: the assertions are byte-identical; only a provider wrapper is added.
**Untouched:** `frontend/src/api/profiles.ts`, `frontend/src/router.tsx`.

Decomposition is smallest-first: the two display routes (independent, simple), then the coupled forms cluster (`StructuredFields` is rendered by both `ProfileNew` and `ProfileEdit`, and `ProfileForm.module.css` is shared by both, so they convert together).

---

## Task 1: Re-skin `ProfilesList` to Mantine; delete `ProfilesList.module.css`

**Files:**
- Modify: `frontend/src/routes/ProfilesList.tsx`
- Delete: `frontend/src/routes/ProfilesList.module.css`
- Test (unchanged, must stay green): `frontend/src/routes/ProfilesList.test.tsx`

**Interfaces:** Consumes `useProfilesList` from `../api/profiles` (unchanged) and `Link`.

- [ ] **Step 1: Replace `ProfilesList.tsx`**

```tsx
import { Anchor, Box, Button, Card, Group, Stack, Text, Title } from "@mantine/core";
import { Link } from "react-router-dom";

import { useProfilesList } from "../api/profiles";

export function ProfilesList() {
  const { data, isLoading, isError } = useProfilesList();
  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data)
    return (
      <Text p="md" c="red" role="alert">
        Couldn't load profiles.
      </Text>
    );
  const profiles = data.profiles;
  return (
    <Box component="main" maw="56rem" mx="auto" px="xs" pt="md" pb="xl">
      <Group justify="space-between" align="flex-start" wrap="nowrap" gap="md">
        <div>
          <Title order={1}>Ranking profiles</Title>
          <Text c="dimmed" size="sm">
            Each profile defines who you are and what you fund-hunt for. The LLM ranks calls against the
            description and rubric.
          </Text>
        </div>
        {profiles.length > 0 && (
          <Button component={Link} to="/profiles/new" style={{ whiteSpace: "nowrap" }}>
            + New profile
          </Button>
        )}
      </Group>
      {profiles.length === 0 ? (
        <Stack align="center" gap="xs" py="xl" c="dimmed">
          <Title order={3}>No profiles yet</Title>
          <Text>Create one to see ranked calls on the dashboard.</Text>
          <Button component={Link} to="/profiles/new" mt="xs">
            Create your first profile
          </Button>
        </Stack>
      ) : (
        <Stack gap="sm" mt="lg">
          {profiles.map((p) => (
            <Card key={p.id} withBorder radius="md" p="md" bg="var(--app-sidebar)">
              <Title order={3} size="h5">
                <Anchor component={Link} to={`/profiles/${p.id}`}>
                  {p.name}
                </Anchor>
              </Title>
              <Text c="dimmed" size="xs">
                {p.kind}
                {p.areas_count > 0 && ` · ${p.areas_count} areas`}
                {p.budget_currency && ` · ${p.budget_currency}`}
              </Text>
              {p.description && (
                <Text size="sm" mt="xs">
                  {p.description.slice(0, 160)}
                  {p.description.length > 160 ? "…" : ""}
                </Text>
              )}
              <Text size="sm" mt="xs">
                <Anchor component={Link} to={`/?profile=${p.id}`}>
                  Ranked feed →
                </Anchor>
                {" · "}
                <Anchor component={Link} to={`/profiles/${p.id}/edit`}>
                  Edit
                </Anchor>
              </Text>
            </Card>
          ))}
        </Stack>
      )}
    </Box>
  );
}
```

- [ ] **Step 2: Run the test green (from `frontend/`)**

Run: `pnpm exec vitest run src/routes/ProfilesList.test.tsx`
Expected: PASS. `findByRole("link", { name: "Lab A" })` resolves the card-title `Anchor` (`href="/profiles/1"`); `getByText(/2 areas/)` resolves the meta `Text`; the empty-state `findByRole("link", { name: /create your first profile/i })` resolves the `Button component={Link}` (`href="/profiles/new"`). No test edits expected.

- [ ] **Step 3: Delete the orphaned module + grep**

Run (from `frontend/`): `grep -rln "ProfilesList.module.css" src` → expect no output. Then `git rm src/routes/ProfilesList.module.css`.

- [ ] **Step 4: Gates (from `frontend/`)**

```bash
pnpm exec tsc --noEmit
pnpm exec vitest run
pnpm build
```
Expected: tsc clean; full vitest green; build ok.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(frontend): re-skin ProfilesList to Mantine; drop ProfilesList.module.css"
```

---

## Task 2: Re-skin `ProfileDetail` to Mantine; delete `ProfileDetail.module.css`

**Files:**
- Modify: `frontend/src/routes/ProfileDetail.tsx`
- Delete: `frontend/src/routes/ProfileDetail.module.css`
- Test (unchanged, must stay green): `frontend/src/routes/ProfileDetail.test.tsx`

**Interfaces:** Consumes `useProfile` + `ProfileDetail` type from `../api/profiles` (unchanged), `Link`, `useParams`.

- [ ] **Step 1: Replace `ProfileDetail.tsx`**

```tsx
import { Anchor, Badge, Box, Button, Card, Group, Text, Title } from "@mantine/core";
import { Link, useParams } from "react-router-dom";

import type { ProfileDetail as Detail } from "../api/profiles";
import { useProfile } from "../api/profiles";

function Chips({ items }: { items: string[] }) {
  if (items.length === 0) return <>—</>;
  return (
    <>
      {items.map((c, i) => (
        <Badge key={i} variant="default" tt="none" radius="xl" size="sm" mr={4} mt={4}>
          {c}
        </Badge>
      ))}
    </>
  );
}

function range(min: number | null, max: number | null) {
  if (min == null && max == null) return "—";
  return `${min ?? "—"} – ${max ?? "—"}`;
}

export function ProfileDetail() {
  const { id } = useParams();
  const { data, isLoading, isError } = useProfile(Number(id));
  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data)
    return (
      <Box component="main" maw="48rem" mx="auto" px="xs" pt="md" pb="xl">
        <Text role="alert">Profile not found.</Text>
      </Box>
    );
  return <DetailView p={data} />;
}

function DetailView({ p }: { p: Detail }) {
  const hasProfileFields =
    p.org_type || p.capabilities.length || p.patient_populations.length || p.scale ||
    p.career_stage || p.applicant_types.length || p.role || p.years_since_phd != null ||
    p.funding_instruments.length || p.funder_exclusions.length || p.country || p.host_institution;
  return (
    <Box component="main" maw="48rem" mx="auto" px="xs" pt="md" pb="xl">
      <Anchor component={Link} to="/profiles" size="sm">
        ← All profiles
      </Anchor>
      <Group justify="space-between" align="flex-start" wrap="nowrap" gap="md" my="md">
        <div>
          <Title order={1}>{p.name}</Title>
          <Text c="dimmed" size="sm">
            {p.kind}
            {p.level ? ` · ${p.level}` : ""}
          </Text>
        </div>
        <Group gap="xs" wrap="nowrap">
          <Button component={Link} to={`/?profile=${p.id}`}>
            Ranked feed →
          </Button>
          <Button component={Link} variant="default" to={`/profiles/${p.id}/edit`}>
            Edit
          </Button>
        </Group>
      </Group>

      <Card withBorder radius="md" p="md" bg="var(--app-sidebar)" mb="md">
        <Title order={2} size="h5" mb="sm">
          Description
        </Title>
        <Text>{p.description || "— (no description yet)"}</Text>
      </Card>

      <Card withBorder radius="md" p="md" bg="var(--app-sidebar)" mb="md">
        <Title order={2} size="h5" mb="sm">
          Ranking rubric
        </Title>
        <Box component="dl" m={0}>
          <Text component="dt" size="xs" c="dimmed" mt="xs">Areas</Text>
          <Text component="dd" size="sm" m={0}><Chips items={p.areas} /></Text>
          <Text component="dt" size="xs" c="dimmed" mt="xs">Budget</Text>
          <Text component="dd" size="sm" m={0}>
            {p.budget_min == null && p.budget_max == null
              ? "—"
              : `${p.budget_min?.toLocaleString() ?? "—"} – ${p.budget_max?.toLocaleString() ?? "—"}${p.budget_currency ? ` ${p.budget_currency}` : ""}`}
          </Text>
          <Text component="dt" size="xs" c="dimmed" mt="xs">Technology Readiness Level</Text>
          <Text component="dd" size="sm" m={0}>{range(p.trl_min, p.trl_max)}</Text>
          <Text component="dt" size="xs" c="dimmed" mt="xs">Must include</Text>
          <Text component="dd" size="sm" m={0}><Chips items={p.must_include} /></Text>
          <Text component="dt" size="xs" c="dimmed" mt="xs">Hard exclusions</Text>
          <Text component="dd" size="sm" m={0}><Chips items={p.hard_exclusions} /></Text>
        </Box>
      </Card>

      {hasProfileFields && (
        <Card withBorder radius="md" p="md" bg="var(--app-sidebar)" mb="md">
          <Title order={2} size="h5" mb="sm">
            Profile
          </Title>
          <Box component="dl" m={0}>
            {p.org_type && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Organization type</Text><Text component="dd" size="sm" m={0}>{p.org_type}</Text></>)}
            {p.capabilities.length > 0 && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Capabilities</Text><Text component="dd" size="sm" m={0}><Chips items={p.capabilities} /></Text></>)}
            {p.patient_populations.length > 0 && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Patient populations</Text><Text component="dd" size="sm" m={0}><Chips items={p.patient_populations} /></Text></>)}
            {p.scale && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Scale</Text><Text component="dd" size="sm" m={0}>{p.scale}</Text></>)}
            {p.career_stage && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Career stage</Text><Text component="dd" size="sm" m={0}>{p.career_stage}</Text></>)}
            {p.role && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Role</Text><Text component="dd" size="sm" m={0}>{p.role}</Text></>)}
            {p.years_since_phd != null && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Years since PhD</Text><Text component="dd" size="sm" m={0}>{p.years_since_phd}</Text></>)}
            {p.applicant_types.length > 0 && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Applicant types</Text><Text component="dd" size="sm" m={0}><Chips items={p.applicant_types} /></Text></>)}
            {p.funding_instruments.length > 0 && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Funding instruments</Text><Text component="dd" size="sm" m={0}><Chips items={p.funding_instruments} /></Text></>)}
            {p.funder_exclusions.length > 0 && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Funder exclusions</Text><Text component="dd" size="sm" m={0}><Chips items={p.funder_exclusions} /></Text></>)}
            {p.host_institution && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Host institution</Text><Text component="dd" size="sm" m={0}>{p.host_institution}</Text></>)}
            {p.country && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Country</Text><Text component="dd" size="sm" m={0}>{p.country}</Text></>)}
          </Box>
        </Card>
      )}
    </Box>
  );
}
```

- [ ] **Step 2: Run the test green (from `frontend/`)**

Run: `pnpm exec vitest run src/routes/ProfileDetail.test.tsx`
Expected: PASS. `findByRole("heading", { name: /Digital Pathology Lab/ })` resolves the `Title order={1}` (`<h1>`); `getByText("Imaging research.")`, `getByText("oncology")`, `getByText("biobank")`, `getByText("project_grant")` resolve the description `Text` and the chip `Badge`s (DOM text unchanged by `tt="none"`); `findByRole("link", { name: /edit/i })` resolves the Edit `Button component={Link}` (`href="/profiles/5/edit"`); the 404 path renders the `role="alert"` "Profile not found." `Text` matched by `findByText(/not found/i)`. No test edits expected.

- [ ] **Step 3: Delete the orphaned module + grep**

Run (from `frontend/`): `grep -rln "ProfileDetail.module.css" src` → expect no output. Then `git rm src/routes/ProfileDetail.module.css`.

- [ ] **Step 4: Gates (from `frontend/`)**

```bash
pnpm exec tsc --noEmit
pnpm exec vitest run
pnpm build
```
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(frontend): re-skin ProfileDetail to Mantine; drop ProfileDetail.module.css"
```

---

## Task 3: Re-skin the forms cluster (`StructuredFields` + `ProfileNew` + `ProfileEdit`); delete `ProfileForm.module.css`

The three files are coupled: `StructuredFields` is rendered by both forms, and `ProfileForm.module.css` is imported by both `ProfileNew` and `ProfileEdit`. Convert all three and delete the shared module in one commit.

**Files:**
- Modify: `frontend/src/routes/StructuredFields.tsx`, `frontend/src/routes/ProfileNew.tsx`, `frontend/src/routes/ProfileEdit.tsx`
- Modify (harness-only, provider wrapper): `frontend/src/routes/StructuredFields.test.tsx`
- Delete: `frontend/src/routes/ProfileForm.module.css`
- Tests (unchanged, must stay green): `ProfileNew.test.tsx`, `ProfileEdit.test.tsx`

**Interfaces:**
- Consumes: `useCreateProfile`/`useProfile`/`useUpdateProfile` + the `ProfileCreateBody`/`ProfileUpdateBody`/`ProfileDetail` types from `../api/profiles` (unchanged); `Link`/`useNavigate`/`useParams`.
- `StructuredFields` keeps its exported `StructuredValues` type and its `{ kind, values, onChange, disabled }` props signature verbatim — both consumers depend on it.

- [ ] **Step 1: Replace `StructuredFields.tsx`**

```tsx
import { Fieldset, NativeSelect, Stack, Textarea, TextInput } from "@mantine/core";

export type StructuredValues = {
  areas: string;
  career_stage: string;
  applicant_types: string;
  role: string;
  years_since_phd: string;
  org_type: string;
  capabilities: string;
  patient_populations: string;
  scale: string;
  funding_instruments: string;
  funder_exclusions: string;
  country: string;
  host_institution: string;
};

const ORG_TYPES = [
  ["", "(unspecified)"], ["research_organisation", "Research organisation"],
  ["university_hospital", "University hospital"], ["public_body", "Public body"],
  ["sme", "SME"], ["other", "Other"],
];
const CAREER_STAGES = [
  ["", "(unspecified)"], ["phd_student", "PhD student"], ["postdoc", "Postdoc"],
  ["junior_pi", "Junior PI"], ["senior_pi", "Senior PI"], ["group_leader", "Group leader"],
];

const toData = (pairs: string[][]) => pairs.map(([value, label]) => ({ value, label }));

export function StructuredFields({
  kind, values, onChange, disabled,
}: {
  kind: string;
  values: StructuredValues;
  onChange: (k: keyof StructuredValues, v: string) => void;
  disabled?: boolean;
}) {
  const set = (k: keyof StructuredValues) => (e: { currentTarget: { value: string } }) => onChange(k, e.currentTarget.value);
  return (
    <>
      {kind === "organizational_unit" && (
        <Fieldset disabled={disabled} mb="sm">
          <Stack gap="sm">
            <NativeSelect label="Organization type" data={toData(ORG_TYPES)} value={values.org_type} onChange={set("org_type")} />
            <Textarea label="Capabilities" description="(one per line)" rows={3} value={values.capabilities} onChange={set("capabilities")} />
            <Textarea label="Patient populations" description="(one per line)" rows={2} value={values.patient_populations} onChange={set("patient_populations")} />
            <TextInput label="Scale" description="(free text)" maxLength={200} value={values.scale} onChange={set("scale")} />
          </Stack>
        </Fieldset>
      )}
      {kind === "individual" && (
        <Fieldset disabled={disabled} mb="sm">
          <Stack gap="sm">
            <NativeSelect label="Career stage" data={toData(CAREER_STAGES)} value={values.career_stage} onChange={set("career_stage")} />
            <TextInput label="Role" description="(free text)" maxLength={200} value={values.role} onChange={set("role")} />
            <TextInput label="Years since PhD" description="(0–80)" value={values.years_since_phd} onChange={set("years_since_phd")} />
          </Stack>
        </Fieldset>
      )}
      <Stack gap="sm">
        <TextInput label="Host institution" description="(free text)" maxLength={200} value={values.host_institution} onChange={set("host_institution")} />
        <TextInput label="Country (e.g. SE)" maxLength={200} value={values.country} onChange={set("country")} />
        <Textarea label="Applicant types" description="(one per line: solo_pi, consortium_coordinator, consortium_partner, institution)" rows={2} value={values.applicant_types} onChange={set("applicant_types")} />
        <Textarea label="Funding instruments" description="(one per line: project_grant, fellowship, mobility, infrastructure, programme)" rows={2} value={values.funding_instruments} onChange={set("funding_instruments")} />
        <Textarea label="Funder exclusions" description="(one per line)" rows={2} value={values.funder_exclusions} onChange={set("funder_exclusions")} />
        <Textarea label="Research areas" description="(one per line)" rows={3} value={values.areas} onChange={set("areas")} />
      </Stack>
    </>
  );
}
```

- [ ] **Step 2: Wrap `StructuredFields.test.tsx` in `MantineProvider`, then run it green**

The converted component uses Mantine, which throws without a provider. Replace the test file with this version — assertions unchanged, only a `MantineProvider` wrapper (and a `renderSF` helper) added:

```tsx
import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { theme } from "../theme/theme";
import { StructuredFields, type StructuredValues } from "./StructuredFields";

const EMPTY: StructuredValues = {
  areas: "", career_stage: "", applicant_types: "", role: "", years_since_phd: "",
  org_type: "", capabilities: "", patient_populations: "", scale: "",
  funding_instruments: "", funder_exclusions: "", country: "", host_institution: "",
};

function renderSF(kind: string) {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <StructuredFields kind={kind} values={EMPTY} onChange={vi.fn()} />
    </MantineProvider>,
  );
}

it("shows org fields for an organizational_unit, hides individual fields", () => {
  renderSF("organizational_unit");
  expect(screen.getByLabelText(/organization type/i)).toBeInTheDocument();
  expect(screen.queryByLabelText(/career stage/i)).toBeNull();
});

it("shows individual fields for an individual, hides org fields", () => {
  renderSF("individual");
  expect(screen.getByLabelText(/career stage/i)).toBeInTheDocument();
  expect(screen.queryByLabelText(/organization type/i)).toBeNull();
});
```

Run (from `frontend/`): `pnpm exec vitest run src/routes/StructuredFields.test.tsx`
Expected: PASS — `getByLabelText(/organization type/i)` / `getByLabelText(/career stage/i)` resolve the `NativeSelect`s (rendered for the matching `kind`); the opposite-kind query returns null.

- [ ] **Step 3: Replace `ProfileNew.tsx`**

```tsx
import { Anchor, Box, Button, Group, NativeSelect, Stack, Text, Textarea, TextInput, Title } from "@mantine/core";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useCreateProfile, type ProfileCreateBody } from "../api/profiles";
import { StructuredFields, type StructuredValues } from "./StructuredFields";

const LEVELS = [
  ["", "(unspecified)"], ["lab", "Lab"], ["group", "Research group"], ["department", "Department"],
  ["clinic", "Clinic"], ["institution", "Institution"], ["other", "Other"],
];

const EMPTY_STRUCT: StructuredValues = {
  areas: "", career_stage: "", applicant_types: "", role: "", years_since_phd: "",
  org_type: "", capabilities: "", patient_populations: "", scale: "",
  funding_instruments: "", funder_exclusions: "", country: "", host_institution: "",
};

export function ProfileNew() {
  const navigate = useNavigate();
  const create = useCreateProfile();
  const [name, setName] = useState("");
  const [kind, setKind] = useState("organizational_unit");
  const [level, setLevel] = useState("");
  const [missionText, setMissionText] = useState("");
  const [orcids, setOrcids] = useState("");
  const [namesAffiliations, setNamesAffiliations] = useState("");
  const [pastedGrants, setPastedGrants] = useState("");
  const [struct, setStruct] = useState<StructuredValues>(EMPTY_STRUCT);
  const setS = (k: keyof StructuredValues, v: string) => setStruct((p) => ({ ...p, [k]: v }));

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const body: ProfileCreateBody = {
      name, kind, level, orcids, names_affiliations: namesAffiliations,
      pasted_grants: pastedGrants, mission_text: missionText, ...struct,
    };
    create.mutate(body, { onSuccess: (out) => navigate(`/profiles/${out.id}/edit`) });
  }

  return (
    <Box component="main" maw="44rem" mx="auto" px="xs" pt="md" pb="xl">
      <Anchor component={Link} to="/profiles" size="sm">
        ← All profiles
      </Anchor>
      <Title order={1} mt="xs">Create a profile</Title>
      <Text c="dimmed" size="sm" mb="md">
        Pick a kind, add seed data, and the local LLM drafts a description plus rubric. You can edit before it
        goes live.
      </Text>
      <form onSubmit={submit}>
        <Stack gap="sm">
          <TextInput label="Profile name" required maxLength={200} value={name}
                     placeholder="e.g. Digital Pathology Lab" onChange={(e) => setName(e.currentTarget.value)} />
          <NativeSelect
            label="Kind"
            data={[
              { value: "organizational_unit", label: "Organizational unit" },
              { value: "individual", label: "Individual researcher" },
            ]}
            value={kind}
            onChange={(e) => setKind(e.currentTarget.value)}
          />
          {kind === "organizational_unit" && (
            <>
              <NativeSelect label="Level" data={LEVELS.map(([value, label]) => ({ value, label }))}
                            value={level} onChange={(e) => setLevel(e.currentTarget.value)} />
              <Textarea label="Mission / strategy / capabilities" description="(free text)" rows={4}
                        value={missionText} onChange={(e) => setMissionText(e.currentTarget.value)} />
            </>
          )}
          <StructuredFields kind={kind} values={struct} onChange={setS} />
          <Textarea label="Member ORCIDs" description="(one per line; for an individual, your own)" rows={2}
                    value={orcids} placeholder="0000-0001-2345-6789" onChange={(e) => setOrcids(e.currentTarget.value)} />
          <Textarea label="Members by name" description="(one per line: Last F | Affiliation)" rows={2}
                    value={namesAffiliations} onChange={(e) => setNamesAffiliations(e.currentTarget.value)} />
          <Textarea label="Pasted past grants" description="(optional, titles + abstracts)" rows={4}
                    value={pastedGrants} onChange={(e) => setPastedGrants(e.currentTarget.value)} />
          <Group gap="sm" align="center">
            <Button type="submit" disabled={create.isPending}>Bootstrap profile</Button>
            {create.isError && (
              <Text role="alert" c="red" size="sm">
                {(create.error as Error | null)?.message ?? "Couldn't create the profile."}
              </Text>
            )}
          </Group>
        </Stack>
      </form>
    </Box>
  );
}
```

- [ ] **Step 4: Run `ProfileNew.test.tsx` green**

Run (from `frontend/`): `pnpm exec vitest run src/routes/ProfileNew.test.tsx`
Expected: PASS. `getByLabelText(/organization type/i)` present initially; `fireEvent.change(getByLabelText(/^kind/i), { target: { value: "individual" } })` flips the `NativeSelect` `<select>`, after which `getByLabelText(/career stage/i)` is present and `/organization type/i` is gone; `getByLabelText(/profile name/i)` accepts "New Lab"; the submit posts `objectContaining({ name: "New Lab", kind: "organizational_unit" })` to `/api/profiles` and navigates to `/profiles/42/edit`. No test edits expected.

- [ ] **Step 5: Replace `ProfileEdit.tsx`**

```tsx
import { Anchor, Box, Button, Fieldset, Group, NativeSelect, Paper, Stack, Text, Textarea, TextInput, Title } from "@mantine/core";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { useProfile, useUpdateProfile, type ProfileDetail, type ProfileUpdateBody } from "../api/profiles";
import { StructuredFields, type StructuredValues } from "./StructuredFields";

const LEVELS = [
  ["", "(unspecified)"], ["lab", "Lab"], ["group", "Research group"], ["department", "Department"],
  ["clinic", "Clinic"], ["institution", "Institution"], ["other", "Other"],
];
const CURRENCIES = ["", "SEK", "EUR", "USD", "NOK", "DKK"];

function joinLines(xs: string[]) { return xs.join("\n"); }

export function ProfileEdit() {
  const { id } = useParams();
  const pid = Number(id);
  const { data, isLoading, isError } = useProfile(pid);
  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data)
    return (
      <Box component="main" maw="44rem" mx="auto" px="xs" pt="md" pb="xl">
        <Text role="alert">Profile not found.</Text>
      </Box>
    );
  return <EditForm key={data.bootstrap_status ?? "ready"} p={data} pid={pid} />;
}

function EditForm({ p, pid }: { p: ProfileDetail; pid: number }) {
  const navigate = useNavigate();
  const update = useUpdateProfile(pid);
  const drafting = p.bootstrap_status === "queued" || p.bootstrap_status === "running";
  const locked = drafting || !p.can_edit;

  const [description, setDescription] = useState(p.description);
  const [level, setLevel] = useState(p.level ?? "");
  const [budgetMin, setBudgetMin] = useState(p.budget_min?.toString() ?? "");
  const [budgetMax, setBudgetMax] = useState(p.budget_max?.toString() ?? "");
  const [currency, setCurrency] = useState(p.budget_currency ?? "");
  const [trlMin, setTrlMin] = useState(p.trl_min?.toString() ?? "");
  const [trlMax, setTrlMax] = useState(p.trl_max?.toString() ?? "");
  const [mustInclude, setMustInclude] = useState(joinLines(p.must_include));
  const [hardExclusions, setHardExclusions] = useState(joinLines(p.hard_exclusions));
  const [struct, setStruct] = useState<StructuredValues>({
    areas: joinLines(p.areas), career_stage: p.career_stage ?? "", applicant_types: joinLines(p.applicant_types),
    role: p.role ?? "", years_since_phd: p.years_since_phd?.toString() ?? "", org_type: p.org_type ?? "",
    capabilities: joinLines(p.capabilities), patient_populations: joinLines(p.patient_populations),
    scale: p.scale ?? "", funding_instruments: joinLines(p.funding_instruments),
    funder_exclusions: joinLines(p.funder_exclusions), country: p.country ?? "", host_institution: p.host_institution ?? "",
  });
  const setS = (k: keyof StructuredValues, v: string) => setStruct((s) => ({ ...s, [k]: v }));

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const body: ProfileUpdateBody = {
      description, level, budget_min: budgetMin, budget_max: budgetMax, budget_currency: currency,
      trl_min: trlMin, trl_max: trlMax, must_include: mustInclude, hard_exclusions: hardExclusions, ...struct,
    };
    update.mutate(body, { onSuccess: () => navigate(`/profiles/${pid}`) });
  }

  return (
    <Box component="main" maw="44rem" mx="auto" px="xs" pt="md" pb="xl">
      <Anchor component={Link} to={`/profiles/${pid}`} size="sm">
        ← Back to profile
      </Anchor>
      <Title order={1} mt="xs">Edit {p.name}</Title>
      <Text c="dimmed" size="sm" mb="md">
        Description feeds the semantic similarity gate. Areas, budgets, TRL, must-include and exclusions feed the
        deterministic rubric gates. Saving kicks off a backfill of the last 90 days against this profile.
      </Text>
      {drafting && (
        <Paper withBorder radius="md" p="sm" mb="md" bg="var(--app-sidebar)">
          <Text size="sm">⏳ Drafting your profile from PubMed and past grants — this can take a minute on the local model.</Text>
        </Paper>
      )}
      {p.bootstrap_status === "done" && (
        <Paper withBorder radius="md" p="sm" mb="md" bg="var(--app-sidebar)">
          <Text size="sm">
            Draft ready.
            {p.bootstrap_pubmed_status === "ok" && ` Found ${p.publications_fetched} publications for your members.`}
            {p.bootstrap_pubmed_status === "empty" && " No publications matched your members on PubMed — the draft rests on your mission text and pasted grants."}
            {p.bootstrap_pubmed_status === "unavailable" && " PubMed was unavailable, so the draft rests on your mission text and pasted grants."}
          </Text>
        </Paper>
      )}
      {p.bootstrap_status === "failed" && (
        <Paper withBorder radius="md" p="sm" mb="md" bg="var(--app-sidebar)" style={{ borderColor: "var(--app-warn, var(--app-bad))" }}>
          <Text size="sm">We couldn't auto-draft a description (the local model was unavailable). Write one below and save.</Text>
        </Paper>
      )}
      <form onSubmit={submit}>
        <Fieldset disabled={locked}>
          <Stack gap="sm">
            <Textarea label="Description" description="Changing this triggers a background re-embed." rows={6}
                      value={description} onChange={(e) => setDescription(e.currentTarget.value)} />
            {p.kind === "organizational_unit" && (
              <NativeSelect label="Level" data={LEVELS.map(([value, label]) => ({ value, label }))}
                            value={level} onChange={(e) => setLevel(e.currentTarget.value)} />
            )}
            <StructuredFields kind={p.kind} values={struct} onChange={setS} disabled={locked} />
            <TextInput label="Budget min" value={budgetMin} onChange={(e) => setBudgetMin(e.currentTarget.value)} />
            <TextInput label="Budget max" value={budgetMax} onChange={(e) => setBudgetMax(e.currentTarget.value)} />
            <NativeSelect label="Currency" data={CURRENCIES.map((c) => ({ value: c, label: c || "(unknown)" }))}
                          value={currency} onChange={(e) => setCurrency(e.currentTarget.value)} />
            <TextInput label="Technology Readiness Level — min" description="(1–9)" value={trlMin} onChange={(e) => setTrlMin(e.currentTarget.value)} />
            <TextInput label="Technology Readiness Level — max" description="(1–9)" value={trlMax} onChange={(e) => setTrlMax(e.currentTarget.value)} />
            <Textarea label="Must-include keywords" description="(one per line; bypass the timeline gate)" rows={3}
                      value={mustInclude} onChange={(e) => setMustInclude(e.currentTarget.value)} />
            <Textarea label="Hard exclusions" description="(one per line)" rows={3}
                      value={hardExclusions} onChange={(e) => setHardExclusions(e.currentTarget.value)} />
            <Group gap="sm" align="center">
              <Button type="submit" disabled={locked || update.isPending}>Save and re-rank</Button>
              <Anchor component={Link} to={`/profiles/${pid}`} size="sm">Cancel</Anchor>
              {update.isError && (
                <Text role="alert" c="red" size="sm">
                  {(update.error as Error | null)?.message ?? "Couldn't save."}
                </Text>
              )}
            </Group>
          </Stack>
        </Fieldset>
      </form>
    </Box>
  );
}
```

- [ ] **Step 6: Run `ProfileEdit.test.tsx` green**

Run (from `frontend/`): `pnpm exec vitest run src/routes/ProfileEdit.test.tsx`
Expected: PASS. `findByLabelText(/description/i)` resolves the Description `Textarea` (`toHaveValue("Old desc.")`); editing + submit PUTs `objectContaining({ description: "New desc." })` to `/api/profiles/{profile_id}` with `params.path.profile_id === 5` and navigates to `/profiles/5`; with `bootstrap_status:"running"` the Description is disabled (via the `Fieldset disabled` cascade — `toBeDisabled()`) and the drafting banner (`Paper`) is matched by `getByText(/drafting/i)`; with `can_edit:false` the Description is disabled. No test edits expected.

- [ ] **Step 7: Delete the orphaned shared module + grep**

Run (from `frontend/`): `grep -rln "ProfileForm.module.css" src` → expect no output. Then `git rm src/routes/ProfileForm.module.css`.

- [ ] **Step 8: Gates (from `frontend/`)**

```bash
pnpm exec tsc --noEmit
pnpm exec vitest run
pnpm build
```
Expected: tsc clean; full vitest green; build ok.

- [ ] **Step 9: Teardown grep (gate)**

Run (from `frontend/`): `grep -rln "ProfileForm.module.css\|ProfilesList.module.css\|ProfileDetail.module.css" src || echo "all profile modules gone — clean"` → expect clean.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "feat(frontend): re-skin StructuredFields + ProfileNew + ProfileEdit to Mantine; drop ProfileForm.module.css"
```

---

## Verification (whole slice, before merge)

1. `pnpm exec tsc --noEmit` clean.
2. `pnpm exec vitest run` full green (four profile test files byte-unchanged; `StructuredFields.test.tsx` has only the sanctioned `MantineProvider` harness wrapper).
3. `pnpm build` succeeds.
4. Teardown grep: all three profile CSS modules unreferenced (Task 3 Step 9).
5. **Visual:** the backendless Playwright route-mock harness ([[reference_spa_visual_check]]) over `/profiles`, `/profiles/:id`, `/profiles/new`, `/profiles/:id/edit`. These render inside the authed AppShell, so mock `/api/me` as `{ user: {…}, capabilities: { admin: false } }` plus the per-route endpoint: `/api/profiles` (list — `{ profiles: [...] }`), `/api/profiles/{profile_id}` (detail/edit — the `ProfileDetailOut`/`ProfileListItemOut` shapes from the route tests' fixtures). Confirm: list cards + "+ New profile" button; detail cards/`<dl>`/chips; the New form's Kind→Level/Mission toggle + `NativeSelect`s; the Edit form pre-fill and (with `bootstrap_status:"running"`) the disabled/dimmed `Fieldset` + drafting banner (`Paper`).
6. Independent diff-vs-parity review before merge: confirm no copy drift (all field labels + their hint text, option labels in ORG_TYPES/CAREER_STAGES/LEVELS/CURRENCIES, lede paragraphs, banner copy, button labels, error strings), the create/update mutation bodies + navigation targets unchanged, `NativeSelect` (not `Select`) on every select, the `Fieldset disabled` cascade preserved, and `React.FormEvent` kept verbatim.

## Finishing

Controller-only, at the user's review gate (not a task): merge `feat/mantine-sweep-profiles` to LOCAL `master` with `--no-ff` (never push), then `pnpm build` and copy `dist/*` into the gitignored `src/call_manager/web/spa/` to restage the bundle, then clean up the worktree/branch (superpowers:finishing-a-development-branch).

---

## Self-Review

**Spec coverage** (against `2026-06-28-app-mantine-sweep-design.md`, Slice 3): `StructuredFields` → Mantine `NativeSelect`/`Textarea`/`TextInput` ✓; `ProfilesList`/`ProfileDetail` → `Card`/`Stack` ✓; `ProfileNew`/`ProfileEdit` wire the Mantine `StructuredFields` ✓; the `Select`≠`NativeSelect` risk addressed head-on (Kind select's `fireEvent.change` requires native `<select>`) ✓; all three CSS modules retired ✓.

**Placeholder scan:** No "TBD/TODO". Full target file for every changed file; every run step has its command + expected result.

**Type/name consistency:** `StructuredValues` type + the `{ kind, values, onChange, disabled }` props preserved so both consumers keep compiling; `toData`/`LEVELS.map`/`CURRENCIES.map` all produce Mantine `{ value, label }[]`; mutation bodies (`ProfileCreateBody`/`ProfileUpdateBody`) and `create.mutate`/`update.mutate` calls + `onSuccess` navigation unchanged; `e.currentTarget.value` throughout; `type="submit"` on both submit buttons; `React.FormEvent` kept (compiles via the repo's global React namespace, as the originals do).

**Behaviour parity:** loading/error/not-found early-returns preserved; `key={data.bootstrap_status ?? "ready"}` remount preserved; `locked = drafting || !p.can_edit` and the `Fieldset disabled` cascade preserved (verified Mantine spreads `disabled` to the native `<fieldset>`); the kind-toggle conditional rendering preserved; chip rendering via `Badge tt="none"` keeps DOM text and case; the ProfileEdit status banners use `Paper` (no `role="alert"`), so they stay passive — no new live region.

**Test harness:** the only test change is the `StructuredFields.test.tsx` `MantineProvider` wrapper (Mantine v9 throws without it — verified); the four other profile test files stay byte-unchanged. This is the same provider-wrap the other profile tests already use, so it adds no new assertion and cannot mask a regression.
