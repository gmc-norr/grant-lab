# App-Wide Mantine Sweep — Slice 2 (Account/Settings) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-skin the Account **Settings** route (display-name, Google-link, change-password, and weekly-digest-subscription forms) to Mantine v9 primitives, and retire `Settings.module.css` — reusing the form vocabulary established in Slice 1 (Auth).

**Architecture:** Pure frontend re-skin of a single file. `frontend/src/routes/Settings.tsx` currently renders raw `<main>/<section>/<h1>/<h2>/<form>/<input>/<button>/<fieldset>` styled by `Settings.module.css`. We swap to Mantine `Stack`/`Paper`/`Title`/`Text`/`Code`/`TextInput`/`PasswordInput`/`Checkbox`/`Button`/`Group`/`Anchor`. The data layer (`api/account.ts` hooks) is untouched. No behaviour, data-flow, API, route, or copy changes. No schema, no OpenAPI regen.

**Tech Stack:** React 19.2.7, Mantine v9.4.0, react-router-dom, TanStack Query, Vite, Vitest + Testing Library (jsdom), pnpm.

## Global Constraints

_Every task's requirements implicitly include this section._

- **Re-skin only.** No behaviour / data-flow / API / route / copy changes. Pure frontend; no backend, no schema, no OpenAPI regen. `api/account.ts` is NOT modified.
- **Mantine v9.4.0 primitive for every UI element.** Dark teal theme inherited from the existing `theme` — no per-component colour overrides unless irreducible (the only irreducible override here is `bg="var(--app-sidebar)"` on the section cards, to keep the existing surface colour).
- **`tsc` is the type gate.** No TS ESLint. `noUnusedLocals` is on → import only what you use; drop the `import styles from "./Settings.module.css"` line.
- **Test preservation is the gate.** `Settings.test.tsx` must stay green; adjust queries to the new tree only if forced, and never weaken them. **Expectation: NO test edits are needed** (see the note below).
- **PasswordInput query is SAFE here (unlike Slice 1).** Mantine `PasswordInput` renders a visibility-toggle button with `aria-label="Toggle password visibility"`. Slice 1 had to disambiguate because it queried the generic `/password/i`. This slice's queries are scoped — `getByLabelText(/current password/i)` and `getByLabelText(/new password/i)` — and "Toggle password visibility" contains neither "current password" nor "new password", so each query resolves exactly one input. Do **not** add `{ selector: "input" }` unless a run actually shows a multi-match (it won't).
- **Mantine `Button` does not force `type`.** Keep an explicit `type="submit"` on every form-submit button (Save name, Connect Google account, Change password, Save subscriptions), exactly as the raw markup did, so the native form submit still fires.
- **Native Google link form stays a full-page POST.** Keep `<form method="post" action="/auth/google/start">` with the hidden `<input type="hidden" name="link" value="1" />`; its button is a `type="submit"` Mantine Button inside that form.
- **Status/error message roles preserved.** Success messages keep `role="status"` ("Saved.", "Password changed."); the password error keeps `role="alert"` and renders the same message expression verbatim: `(change.error as Error | null)?.message ?? "Couldn't change password."`. Render them as Mantine `Text` with the explicit `role` prop (no `title`/`icon`).
- **Links use `Anchor component={Link} to="…"`** — client-side nav + real `href`. (Only the empty-subscriptions "create one" link.)
- **`e.currentTarget.value` / `e.currentTarget.checked`** (the repo idiom) in Mantine handlers, not `e.target.*`.
- **Branch / worktree:** work in an isolated worktree `.worktrees/mantine-sweep-account` on branch `feat/mantine-sweep-account` (created at execution time via superpowers:using-git-worktrees). The controller merges to LOCAL master `--no-ff` (never push) only at the user's review gate, then rebuilds + restages the gitignored SPA bundle.
- **Whole-suite `vitest` is reliable here** — only backend testcontainers flakes, and this slice changes no backend.

---

## File Structure

**Modified:** `frontend/src/routes/Settings.tsx` (full rewrite of the JSX to Mantine; logic/handlers/state unchanged).
**Deleted:** `frontend/src/routes/Settings.module.css` (sole consumer is `Settings.tsx`; after the rewrite drops the import it is orphaned).
**Unchanged (must stay green):** `frontend/src/routes/Settings.test.tsx`.
**Untouched:** `frontend/src/api/account.ts` (data layer), `frontend/src/router.tsx` (route wiring).

---

## Task 1: Convert `Settings.tsx` to Mantine and retire `Settings.module.css`

Single cohesive deliverable: the Settings route renders entirely in Mantine, the existing test passes unchanged, and the orphaned CSS module is deleted in the same commit.

**Files:**
- Modify: `frontend/src/routes/Settings.tsx`
- Delete: `frontend/src/routes/Settings.module.css`
- Test (unchanged, must stay green): `frontend/src/routes/Settings.test.tsx`

**Interfaces:**
- Consumes: `AccountSettings` type + `useAccountSettings`/`useUpdateName`/`useChangePassword`/`useSaveSubscriptions` from `../api/account` (all unchanged); `Link` from react-router-dom.
- Produces: nothing downstream (terminal route). Reuses the Slice-1 Mantine form idiom.

- [ ] **Step 1: Replace `Settings.tsx` with the Mantine version**

Full file:
```tsx
import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  Anchor,
  Button,
  Checkbox,
  Code,
  Group,
  PasswordInput,
  Paper,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";

import type { AccountSettings } from "../api/account";
import {
  useAccountSettings,
  useChangePassword,
  useSaveSubscriptions,
  useUpdateName,
} from "../api/account";

export function Settings() {
  const { data, isLoading, isError } = useAccountSettings();
  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data)
    return (
      <Text p="md" c="red" role="alert">
        Couldn't load settings.
      </Text>
    );
  return <SettingsForm data={data} />;
}

function SettingsForm({ data }: { data: AccountSettings }) {
  return (
    <Stack component="main" maw="40rem" mx="auto" px="xs" pt="md" pb="xl" gap="lg">
      <div>
        <Title order={1}>Settings</Title>
        <Text c="dimmed" size="sm">
          Signed in as <Code>{data.email}</Code>
          {data.is_admin && (
            <>
              {" · "}
              <Text span c="dimmed">
                admin
              </Text>
            </>
          )}
          .
        </Text>
      </div>
      <NameSection initial={data.name ?? ""} />
      {data.google_oidc_enabled && <GoogleSection linked={data.google_linked} />}
      {data.has_password && <PasswordSection />}
      <SubscriptionsSection profiles={data.profiles} />
    </Stack>
  );
}

function SectionCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Paper component="section" withBorder radius="md" p="md" bg="var(--app-sidebar)">
      <Title order={2} size="h4" mb="sm">
        {title}
      </Title>
      {children}
    </Paper>
  );
}

function NameSection({ initial }: { initial: string }) {
  const [name, setName] = useState(initial);
  const update = useUpdateName();
  return (
    <SectionCard title="Display name">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          update.mutate(name);
        }}
      >
        <Stack gap="sm">
          <TextInput
            label="How should we address you?"
            type="text"
            value={name}
            maxLength={200}
            placeholder="e.g. Pär Larsson"
            onChange={(e) => setName(e.currentTarget.value)}
          />
          <Group gap="sm" align="center">
            <Button type="submit" disabled={update.isPending}>
              Save name
            </Button>
            {update.isSuccess && (
              <Text role="status" c="teal" size="sm">
                Saved.
              </Text>
            )}
          </Group>
        </Stack>
      </form>
    </SectionCard>
  );
}

function GoogleSection({ linked }: { linked: boolean }) {
  return (
    <SectionCard title="Google sign-in">
      {linked ? (
        <Text>
          Google sign-in:{" "}
          <Text span fw={700}>
            connected
          </Text>
          .
        </Text>
      ) : (
        // Native server-side OAuth start — full-page POST, not /api. Owned by the auth slice.
        <form method="post" action="/auth/google/start">
          <input type="hidden" name="link" value="1" />
          <Button type="submit">Connect Google account</Button>
        </form>
      )}
    </SectionCard>
  );
}

function PasswordSection() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const change = useChangePassword();
  return (
    <SectionCard title="Change password">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          change.mutate(
            { current_password: current, new_password: next },
            {
              onSuccess: () => {
                setCurrent("");
                setNext("");
              },
            },
          );
        }}
      >
        <Stack gap="sm">
          <PasswordInput
            label="Current password"
            autoComplete="current-password"
            required
            value={current}
            onChange={(e) => setCurrent(e.currentTarget.value)}
          />
          <PasswordInput
            label="New password"
            autoComplete="new-password"
            required
            value={next}
            onChange={(e) => setNext(e.currentTarget.value)}
          />
          <Group gap="sm" align="center">
            <Button type="submit" disabled={change.isPending}>
              Change password
            </Button>
            {change.isSuccess && (
              <Text role="status" c="teal" size="sm">
                Password changed.
              </Text>
            )}
            {change.isError && (
              <Text role="alert" c="red" size="sm">
                {(change.error as Error | null)?.message ?? "Couldn't change password."}
              </Text>
            )}
          </Group>
        </Stack>
      </form>
    </SectionCard>
  );
}

function SubscriptionsSection({ profiles }: { profiles: AccountSettings["profiles"] }) {
  const [checked, setChecked] = useState<Set<number>>(
    () => new Set(profiles.filter((p) => p.subscribed).map((p) => p.id)),
  );
  const save = useSaveSubscriptions();
  if (profiles.length === 0) {
    return (
      <SectionCard title="Weekly digest subscriptions">
        <Text c="dimmed" size="sm">
          No profiles to subscribe to yet —{" "}
          <Anchor component={Link} to="/profiles/new">
            create one
          </Anchor>
          .
        </Text>
      </SectionCard>
    );
  }
  return (
    <SectionCard title="Weekly digest subscriptions">
      <Text c="dimmed" size="sm" mb="sm">
        Check the profiles whose Monday-morning digest you want emailed to you.
      </Text>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate([...checked], {
            // Reconcile from the server's authoritative response so any profiles
            // that were deactivated between load and save are immediately reflected
            // (rather than leaving the checkbox state diverged from server reality).
            onSuccess: (res) => setChecked(new Set(res.subscribed)),
          });
        }}
      >
        <Stack gap="sm">
          <Stack gap="xs">
            {profiles.map((p) => (
              <Checkbox
                key={p.id}
                label={p.name}
                checked={checked.has(p.id)}
                onChange={(e) =>
                  setChecked((prev) => {
                    const nextSet = new Set(prev);
                    if (e.currentTarget.checked) nextSet.add(p.id);
                    else nextSet.delete(p.id);
                    return nextSet;
                  })
                }
              />
            ))}
          </Stack>
          <Group gap="sm" align="center">
            <Button type="submit" disabled={save.isPending}>
              Save subscriptions
            </Button>
            {save.isSuccess && (
              <Text role="status" c="teal" size="sm">
                Saved.
              </Text>
            )}
          </Group>
        </Stack>
      </form>
    </SectionCard>
  );
}
```

- [ ] **Step 2: Run the Settings test (expect green, no edits)**

Run (from `frontend/`): `pnpm exec vitest run src/routes/Settings.test.tsx`
Expected: PASS (all 8 tests) with NO edits to the test file. Confirms: sections render from the read-model; `findByDisplayValue("Pär")` resolves the name `TextInput`; `getByText("p@ki.se")` resolves the `<Code>`; the two section headings resolve via `getByRole("heading", …)` (Mantine `Title order={2}` → `<h2>`); the checkboxes resolve via `getByRole("checkbox", { name: /Group A|B/ })` (Mantine `Checkbox` label association); the name/password/subscriptions mutations POST/PUT the same bodies; `findByText(/password changed/i)` and `findByText(/current password is incorrect/i)` resolve the `role="status"`/`role="alert"` `Text`. The scoped `/current password/i` and `/new password/i` label queries each match exactly one `PasswordInput` (the toggle's `aria-label="Toggle password visibility"` matches neither).

If — contrary to the analysis — a password query reports a multi-match, the only sanctioned fix is appending `{ selector: "input" }` to that query (as in Slice 1); do not weaken anything else.

- [ ] **Step 3: Confirm `Settings.module.css` is now unreferenced, then delete it**

Run (from `frontend/`):
```bash
grep -rln "Settings.module.css" src
```
Expected: no output (the rewrite dropped the only import). Then:
```bash
git rm src/routes/Settings.module.css
```

- [ ] **Step 4: Type-check + full suite + build**

Run (from `frontend/`):
```bash
pnpm exec tsc --noEmit
pnpm exec vitest run
pnpm build
```
Expected: tsc clean (no orphaned `styles` import; `ReactNode` is imported as a type per Step 1); full vitest green; `tsc && vite build` succeeds.

- [ ] **Step 5: Teardown grep (gate)**

Run (from `frontend/`):
```bash
grep -rln "Settings.module.css" src || echo "module gone — clean"
```
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(frontend): re-skin Account Settings to Mantine; drop Settings.module.css"
```

---

## Verification (whole slice, before merge)

1. `pnpm exec tsc --noEmit` clean.
2. `pnpm exec vitest run` full green (`Settings.test.tsx` unchanged).
3. `pnpm build` succeeds.
4. Teardown grep: `Settings.module.css` unreferenced (Task 1 Step 5).
5. **Visual:** the backendless Playwright route-mock harness ([[reference_spa_visual_check]]) over `/me` (the Settings route — `router.tsx` mounts `Settings` at `path: "me"`, NOT `/settings`). Mock `/api/me/settings` returning the `AccountSettings` shape `{ name, email, is_admin, google_oidc_enabled, google_linked, has_password, profiles: [{id,name,subscribed}] }` (mirror `Settings.test.tsx`'s `SETTINGS`). Settings is rendered inside the authed AppShell, so also mock `/api/me` as `{ user: {…}, capabilities: { admin: false } }` and provide benign fallbacks so the shell doesn't crash. Confirm the four section cards, the display-name field, the two `PasswordInput` reveal eyes, the subscription checkboxes, and the calm-teal styling.
6. Independent diff-vs-parity review before merge: confirm no copy drift (section titles, labels, placeholder "e.g. Pär Larsson", "Monday-morning digest" copy, button labels, status/error strings), POST/PUT bodies + the subscriptions reconcile-from-server logic intact, status/error roles preserved, native Google POST `action`/`method`/hidden-input intact, and no Mantine affordance change (`disabled` not `loading`; `Checkbox` not `Checkbox.Group` — the Set<number> state model is preserved).

## Finishing

Controller-only, at the user's review gate (not a task): merge `feat/mantine-sweep-account` to LOCAL `master` with `--no-ff` (never push), then `pnpm build` and copy `dist/*` into the gitignored `src/call_manager/web/spa/` to restage the bundle, then clean up the worktree/branch (superpowers:finishing-a-development-branch).

---

## Self-Review

**Spec coverage** (against `2026-06-28-app-mantine-sweep-design.md`, Slice 2 "Account/Settings — the name/password/digest forms → Mantine"):
- Name form → `TextInput` + `Button` ✓.
- Password form → `PasswordInput` ×2 + `Button` ✓.
- Digest subscriptions form → `Checkbox` ×N + `Button` ✓.
- Google-link section + header + status/error messaging → `Title`/`Text`/`Code`/`Anchor`/`Group` ✓.
- Retire the CSS module ✓ (Task 1 Step 3, same commit as the rewrite).

**Placeholder scan:** No "TBD/TODO". The full target file is given; every run step states the command and expected result.

**Type/name consistency:** All four `api/account` hooks consumed with unchanged signatures; mutation payloads unchanged (`update.mutate(name)`; `change.mutate({ current_password, new_password }, { onSuccess })`; `save.mutate([...checked], { onSuccess: (res) => setChecked(new Set(res.subscribed)) })`). `e.currentTarget.value`/`.checked` used consistently. `type="submit"` on all four submit buttons. The `SectionCard` helper is the only new local (DRY for the four identical card shells) and is used four times; its `children` is typed `ReactNode`, imported as `import { useState, type ReactNode } from "react"` — matching the repo idiom (`test-utils.tsx`, `api/*.test.tsx`), not the global `React.ReactNode` namespace (which zero files in this repo use).

**Behaviour parity:** loading/error early-returns preserved (now Mantine `Text`); conditional sections (`google_oidc_enabled`, `has_password`, `profiles.length === 0`) preserved; the subscriptions `Set<number>` state model and server-reconcile `onSuccess` preserved (individual `Checkbox`es, not `Checkbox.Group`, to avoid changing the state shape); the password error message expression copied verbatim.
