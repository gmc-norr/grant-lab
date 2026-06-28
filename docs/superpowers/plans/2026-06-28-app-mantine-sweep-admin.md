# App-Wide Mantine Sweep — Slice 4 (Admin) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-skin the five Admin content routes — `AdminDrafting`, `AdminGoldenSet`, `AdminUsers`, `AdminPendingUsers`, `AdminDashboard` — to Mantine v9 primitives (all **seven** raw `<table>`s → Mantine `Table`, plus admin forms/actions/banners), and retire their five CSS modules.

**Architecture:** Pure frontend re-skin. Each route's raw `<main>/<section>/<table>/<form>/<input>/<button>` markup becomes Mantine `Box`/`Card`/`Table`/`TextInput`/`PasswordInput`/`Button`/`Group`/`Alert`/`Anchor`/`Text`/`Code`. The data layer (`api/admin.ts`, `api/adminDashboard.ts`, `api/adminGoldenSet.ts`) and the router are untouched. No behaviour, data-flow, API, route, or copy changes. No schema, no OpenAPI regen.

**Tech Stack:** React 19.2.7, Mantine v9.4.0, react-router-dom, TanStack Query, Vite, Vitest + Testing Library (jsdom), pnpm.

## Global Constraints

_Every task's requirements implicitly include this section._

- **Re-skin only.** No behaviour / data-flow / API / route / copy changes. Pure frontend; no backend, no schema, no OpenAPI regen. The three admin `api/*.ts` modules and `router.tsx` are NOT modified.
- **Mantine v9.4.0 primitive for every UI element.** Dark teal theme inherited from `theme`. No per-component colour override unless irreducible.
- **`tsc` is the type gate** (`noUnusedLocals` — import only what you use; drop each `import styles from "./*.module.css"`). Keep `React.FormEvent` exactly as the originals use it (compiles via the repo's global `React` namespace).
- **Test preservation is the gate.** All six admin route test files (`AdminDashboard`, `AdminDrafting`, `AdminGoldenSet`, `AdminUsers`, `AdminPendingUsers`, `AdminNav`) must stay **byte-unchanged** and green. The tests query by role/text/placeholder, which survive the Mantine DOM; adjust nothing.
- **`Table` → assert roles/text, not classes.** Mantine `Table`/`Table.Thead`/`Table.Tbody`/`Table.Tr`/`Table.Th`/`Table.Td` render real `<table>/<thead>/<tbody>/<tr>/<th>/<td>`, so `getByText`/`getByRole("link"|"button")` inside cells keep working. Use `<Table horizontalSpacing="sm" verticalSpacing="xs" fz="sm">` for the compact admin density (row borders are on by default).
- **No `<select>` anywhere in admin** (verified) → `NativeSelect` is NOT needed in this slice.
- **Number inputs → `TextInput type="number"`, NOT `NumberInput`.** The forms keep string state + `Number(...)` coercion at submit and event-based `onChange`. `TextInput type="number"` renders `<input type="number">`, preserving `getByPlaceholderText`, `userEvent.type`, and `e.currentTarget.value`. `NumberInput` would change the value model (numeric onChange) — do not use it.
- **Password inputs → `PasswordInput`.** Its visibility-toggle is a button with NO `placeholder`, so `getAllByPlaceholderText(/initial password/i)).toHaveLength(1)` (AdminPendingUsers.test) stays 1, and the toggle's `aria-label="Toggle password visibility"` does not match the scoped button-name queries (`/reset password/i`, `/approve as admin/i`).
- **Banners — PRESERVE the EXISTING roles.** Unlike Slice 3, the AdminDashboard action banner is intentionally a live region in the original: `role={kind === "ok" ? "status" : "alert"}`. Convert it to Mantine `Alert` with that SAME explicit `role` (`status` for ok, `alert` for warn) and `color` (`teal` for ok, `red`/`yellow` for warn). The `calibration_warning` banner keeps `role="alert"` (Mantine `Alert` default). The error/loading states keep their `role="alert"` (`<… role="alert">`) and exact copy.
- **Secondary buttons → `Button variant="default"`** (the neutral bordered `.secondary`); primary actions → default filled `Button` (teal). Use `size="xs"` for the compact admin density on buttons and inline inputs.
- **Links:** `Anchor component={Link} to="…"` for inline links (preserves `<a href>` + client-side nav — the "Admin home"/"Pending"/cockpit/golden-set links are asserted as real `<a href>`).
- **`e.currentTarget.value`** in all Mantine change handlers.
- **AdminNav + AdminLayout stay as-is (scope note).** `AdminLayout` has no styled UI (just `RequireAdmin` + `AdminNav` + `Outlet`) — untouched. `AdminNav` is a router-`NavLink` active-state subnav (nav chrome, like the deliberately-out-of-scope IDE shell): converting it fights Mantine's controlled-`className` vs NavLink's active-`className`-function, for no test or UX gain. It stays a scoped `--app-*` module, consistent with how the shell nav is handled. `AdminNav.module.css` is NOT deleted. (If the user later wants it Mantine-ified, that's a small follow-up.)
- **Branch / worktree:** isolated worktree `.worktrees/mantine-sweep-admin` on branch `feat/mantine-sweep-admin`. Controller merges to LOCAL master `--no-ff` (never push) at the user's review gate, then rebuilds + restages the gitignored bundle.
- **Whole-suite `vitest` is reliable here** — only backend testcontainers flakes; this slice changes no backend.

---

## File Structure

**Modified + module deleted, one per task:**
1. `AdminDrafting.tsx` (3 tables) — delete `AdminDrafting.module.css`.
2. `AdminGoldenSet.tsx` (2 tables + 2 forms) — delete `AdminGoldenSet.module.css`.
3. `AdminUsers.tsx` (1 table + row actions + reset form + pager) — delete `AdminUsers.module.css`.
4. `AdminPendingUsers.tsx` (cards + password + actions) — delete `AdminPendingUsers.module.css`.
5. `AdminDashboard.tsx` (1 table + dl's + banners + buttons) — delete `AdminDashboard.module.css`.

**Unchanged (must stay green, byte-identical):** `AdminDrafting.test.tsx`, `AdminGoldenSet.test.tsx`, `AdminUsers.test.tsx`, `AdminPendingUsers.test.tsx`, `AdminDashboard.test.tsx`, `AdminNav.test.tsx`.
**Untouched:** `AdminLayout.tsx`, `AdminNav.tsx` + `AdminNav.module.css`, `RequireAdmin.tsx`, the three admin `api/*.ts`, `router.tsx`.

Decomposition: one route per task (each route is an independent file + module + test). Smallest-/simplest-first establishes the Mantine `Table` and admin-action patterns the later tasks reuse.

---

## Task 1: Re-skin `AdminDrafting` (3 read-only tables)

Establishes the Mantine `Table` + admin `Card` pattern. Read-only (no forms).

**Files:** Modify `frontend/src/routes/AdminDrafting.tsx`; Delete `frontend/src/routes/AdminDrafting.module.css`. Test (unchanged): `AdminDrafting.test.tsx`.
**Interfaces:** Consumes `useAdminDrafting` from `../api/adminDashboard` (unchanged); `Link`.

- [ ] **Step 1: Replace `AdminDrafting.tsx`**

```tsx
import { Anchor, Box, Card, Code, Table, Text, Title } from "@mantine/core";
import { Link } from "react-router-dom";

import { useAdminDrafting } from "../api/adminDashboard";

const fmtDt = (s: string) => new Date(s).toLocaleString();

export function AdminDrafting() {
  const { data, isLoading, isError } = useAdminDrafting();
  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data)
    return (
      <Box component="main" role="alert" maw="60rem" mx="auto" px="xs" pt="md" pb="xl">
        <Text>Couldn't load the drafting panel.</Text>
      </Box>
    );
  const d = data;
  const cap = d.global_cap;
  return (
    <Box component="main" maw="60rem" mx="auto" px="xs" pt="md" pb="xl">
      <Title order={1}>Drafting (B7/B8) panel</Title>

      <Card withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Kill switch</Title>
        <Text>{d.drafting_enabled ? <Text span fw={700}>Drafting enabled</Text> : <Text span fw={700}>Drafting disabled</Text>}</Text>
        <Text c="dimmed" size="sm">Toggle via env <Code>DRAFTING_ENABLED=true|false</Code> and restart. UI toggling is a backlog item.</Text>
      </Card>

      <Card withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Global daily cap (today, Europe/Stockholm)</Title>
        <Table horizontalSpacing="sm" verticalSpacing="xs" fz="sm">
          <Table.Thead><Table.Tr><Table.Th>Dimension</Table.Th><Table.Th>Used / Cap</Table.Th><Table.Th>Status</Table.Th></Table.Tr></Table.Thead>
          <Table.Tbody>
            <Table.Tr><Table.Td>Input tokens</Table.Td><Table.Td>{cap.used_in} / {cap.cap_in}</Table.Td>
              <Table.Td>{cap.reason === "input_cap_exceeded" ? <Text span fw={700}>EXCEEDED</Text> : "OK"}</Table.Td></Table.Tr>
            <Table.Tr><Table.Td>Output tokens</Table.Td><Table.Td>{cap.used_out} / {cap.cap_out}</Table.Td>
              <Table.Td>{cap.reason === "output_cap_exceeded" ? <Text span fw={700}>EXCEEDED</Text> : "OK"}</Table.Td></Table.Tr>
          </Table.Tbody>
        </Table>
        <Text mt="sm">{cap.allowed ? "Overall status: OK" : <Text span fw={700}>Overall status: EXCEEDED ({cap.reason})</Text>}</Text>
      </Card>

      <Card withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Per-workspace usage today</Title>
        <Table horizontalSpacing="sm" verticalSpacing="xs" fz="sm">
          <Table.Thead><Table.Tr><Table.Th>Title</Table.Th><Table.Th>Owner</Table.Th><Table.Th>Input</Table.Th><Table.Th>Output</Table.Th></Table.Tr></Table.Thead>
          <Table.Tbody>
            {d.per_workspace.length === 0 ? (
              <Table.Tr><Table.Td colSpan={4} c="dimmed">No workspaces.</Table.Td></Table.Tr>
            ) : d.per_workspace.map((w) => (
              <Table.Tr key={w.id}>
                <Table.Td><Anchor component={Link} to={`/cockpit/${w.id}`}>{w.title}</Anchor></Table.Td>
                <Table.Td>{w.owner_email}</Table.Td><Table.Td>{w.used_in}</Table.Td><Table.Td>{w.used_out}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Card>

      <Card withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Recent failed / cancelled turns (last 50)</Title>
        {d.recent_events.length === 0 ? (
          <Text c="dimmed" size="sm">No recent failures.</Text>
        ) : (
          <Table horizontalSpacing="sm" verticalSpacing="xs" fz="sm">
            <Table.Thead><Table.Tr><Table.Th>When</Table.Th><Table.Th>Workspace</Table.Th><Table.Th>Kind</Table.Th><Table.Th>Detail</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              {d.recent_events.map((e, i) => (
                <Table.Tr key={i}>
                  <Table.Td>{fmtDt(e.created_at)}</Table.Td><Table.Td>{e.workspace_id}</Table.Td>
                  <Table.Td><Code>{e.kind}</Code></Table.Td>
                  <Table.Td><Code block fz="xs">{JSON.stringify(e.payload, null, 2)}</Code></Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
      </Card>
    </Box>
  );
}
```

- [ ] **Step 2: Run the test green (from `frontend/`)**

Run: `pnpm exec vitest run src/routes/AdminDrafting.test.tsx`
Expected: PASS. `findByText(/drafting enabled/i)` → the kill-switch `Text`; `getByText("EXCEEDED")` (exact) → the input-row cell's isolated `<Text span fw={700}>EXCEEDED</Text>` (the "Overall status: EXCEEDED (…)" line is a different element, so no double-match); `getByText(/Overall status: EXCEEDED/)` → the overall line; `getByRole("link", { name: "Big" })` → the workspace `Anchor` (`href="/cockpit/…"`); `getByText(/boom_xyz/)` → the payload `Code block`; the error path renders the `role="alert"` "Couldn't load the drafting panel." No test edits.

- [ ] **Step 3: Delete module + grep**

Run (from `frontend/`): `grep -rln "AdminDrafting.module.css" src` → expect none. Then `git rm src/routes/AdminDrafting.module.css`.

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
git commit -m "feat(frontend): re-skin AdminDrafting to Mantine; drop AdminDrafting.module.css"
```

---

## Task 2: Re-skin `AdminGoldenSet` (2 tables + 2 forms)

**Files:** Modify `frontend/src/routes/AdminGoldenSet.tsx`; Delete `frontend/src/routes/AdminGoldenSet.module.css`. Test (unchanged): `AdminGoldenSet.test.tsx`.
**Interfaces:** Consumes `useAdminGoldenSet`/`useAddTriple`/`useDeleteTriple` + the `AdminGoldenSet` type from `../api/adminGoldenSet` (unchanged).

- [ ] **Step 1: Replace `AdminGoldenSet.tsx`**

```tsx
import { Box, Button, Card, Code, Group, Table, Text, TextInput, Title } from "@mantine/core";
import { useState } from "react";

import type { AdminGoldenSet as GoldenSet } from "../api/adminGoldenSet";
import { useAddTriple, useAdminGoldenSet, useDeleteTriple } from "../api/adminGoldenSet";

type Candidate = GoldenSet["candidates"][number];

function CandidateRow({ c }: { c: Candidate }) {
  const [score, setScore] = useState("");
  const add = useAddTriple();
  return (
    <Table.Tr>
      <Table.Td>{c.call_title}</Table.Td>
      <Table.Td>{c.profile_name}</Table.Td>
      <Table.Td>{c.aggregate_score}</Table.Td>
      <Table.Td>{c.embed_similarity !== null ? c.embed_similarity.toFixed(3) : "-"}</Table.Td>
      <Table.Td><Code>{c.reason}</Code></Table.Td>
      <Table.Td>
        <form onSubmit={(e) => {
          e.preventDefault();
          add.mutate({ call_id: c.call_id, profile_id: c.profile_id, expected_score: Number(score) },
            { onSuccess: () => setScore("") });
        }}>
          <Group gap="xs" align="center" wrap="nowrap">
            <TextInput size="xs" type="number" min={0} max={100} placeholder="0–100" required
                       value={score} onChange={(e) => setScore(e.currentTarget.value)} />
            <Button size="xs" type="submit" disabled={add.isPending}>Add candidate</Button>
          </Group>
        </form>
      </Table.Td>
    </Table.Tr>
  );
}

export function AdminGoldenSet() {
  const { data, isLoading, isError } = useAdminGoldenSet();
  const del = useDeleteTriple();
  const add = useAddTriple();
  const [callId, setCallId] = useState("");
  const [profileId, setProfileId] = useState("");
  const [score, setScore] = useState("");
  const [note, setNote] = useState("");

  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data)
    return (
      <Box component="main" role="alert" maw="60rem" mx="auto" px="xs" pt="md" pb="xl">
        <Text>Couldn't load the golden set.</Text>
      </Box>
    );

  const submitManual = (e: React.FormEvent) => {
    e.preventDefault();
    add.mutate(
      { call_id: Number(callId), profile_id: Number(profileId), expected_score: Number(score), note: note.trim() || undefined },
      { onSuccess: () => { setCallId(""); setProfileId(""); setScore(""); setNote(""); } },
    );
  };

  return (
    <Box component="main" maw="60rem" mx="auto" px="xs" pt="md" pb="xl">
      <Title order={1}>Golden set</Title>

      <Card withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Curated triples ({data.triples.length})</Title>
        {data.triples.length === 0 ? <Text c="dimmed" size="sm">No triples yet.</Text> : (
          <Table horizontalSpacing="sm" verticalSpacing="xs" fz="sm">
            <Table.Thead><Table.Tr><Table.Th>Call</Table.Th><Table.Th>Profile</Table.Th><Table.Th>Expected score</Table.Th><Table.Th>Note</Table.Th><Table.Th></Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              {data.triples.map((t) => (
                <Table.Tr key={t.id}>
                  <Table.Td>{t.call_title}</Table.Td><Table.Td>{t.profile_name}</Table.Td><Table.Td>{t.expected_score}</Table.Td><Table.Td>{t.note ?? ""}</Table.Td>
                  <Table.Td><Button size="xs" variant="default" disabled={del.isPending}
                                    onClick={() => del.mutate({ triple_id: t.id })}>Delete</Button></Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
      </Card>

      <Card withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Add triple manually</Title>
        <form onSubmit={submitManual}>
          <Group gap="xs" align="center">
            <TextInput size="xs" type="number" placeholder="call_id" required value={callId} onChange={(e) => setCallId(e.currentTarget.value)} />
            <TextInput size="xs" type="number" placeholder="profile_id" required value={profileId} onChange={(e) => setProfileId(e.currentTarget.value)} />
            <TextInput size="xs" type="number" min={0} max={100} placeholder="Expected score (0–100)" required value={score} onChange={(e) => setScore(e.currentTarget.value)} />
            <TextInput size="xs" type="text" placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.currentTarget.value)} />
            <Button size="xs" type="submit" disabled={add.isPending}>Add</Button>
          </Group>
        </form>
      </Card>

      <Card withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Candidates ({data.candidates.length})</Title>
        {data.candidates.length === 0 ? (
          <Text c="dimmed" size="sm">No candidates — no dismissed-high-score, cosine-disagreement, or escalation-disagreement pairs found.</Text>
        ) : (
          <Table horizontalSpacing="sm" verticalSpacing="xs" fz="sm">
            <Table.Thead><Table.Tr><Table.Th>Call</Table.Th><Table.Th>Profile</Table.Th><Table.Th>Score</Table.Th><Table.Th>Cosine</Table.Th><Table.Th>Signal</Table.Th><Table.Th>Add</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              {data.candidates.map((c) => <CandidateRow key={`${c.call_id}-${c.profile_id}`} c={c} />)}
            </Table.Tbody>
          </Table>
        )}
      </Card>
    </Box>
  );
}
```

- [ ] **Step 2: Run the test green (from `frontend/`)**

Run: `pnpm exec vitest run src/routes/AdminGoldenSet.test.tsx`
Expected: PASS. `findByText("C1")`/`getByText("Cand")`/`getByText("escalation_disagreement")` resolve table cells; `findByRole("button", { name: /delete/i })` deletes triple 7; the manual form's `getByPlaceholderText("call_id"|"profile_id"|/expected score/i)` `TextInput`s + `getByRole("button", { name: /^add$/i })` POST `{ call_id: 3, profile_id: 4, expected_score: 65, note: undefined }`; the candidate inline `getByPlaceholderText("0–100")` + `/add candidate/i` POSTs the candidate. (`/^add$/i` matches only "Add", not "Add candidate".) No test edits.

- [ ] **Step 3: Delete module + grep**

Run (from `frontend/`): `grep -rln "AdminGoldenSet.module.css" src` → expect none. Then `git rm src/routes/AdminGoldenSet.module.css`.

- [ ] **Step 4: Gates (from `frontend/`):** `pnpm exec tsc --noEmit` · `pnpm exec vitest run` · `pnpm build` — all green.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(frontend): re-skin AdminGoldenSet to Mantine; drop AdminGoldenSet.module.css"
```

---

## Task 3: Re-skin `AdminUsers` (1 table + row actions + reset form + pager)

**Files:** Modify `frontend/src/routes/AdminUsers.tsx`; Delete `frontend/src/routes/AdminUsers.module.css`. Test (unchanged): `AdminUsers.test.tsx` (also asserts the AdminPendingUsers "Admin home" link — independent, unaffected).
**Interfaces:** Consumes `useAdminUsers`/`useChangeRole`/`useDisableUser`/`useEnableUser`/`useResetPassword` + `AdminUser` type from `../api/admin` (unchanged); `Link`.

- [ ] **Step 1: Replace `AdminUsers.tsx`**

```tsx
import { Anchor, Box, Button, Group, PasswordInput, Table, Text, Title } from "@mantine/core";
import { useState } from "react";
import { Link } from "react-router-dom";

import type { AdminUser } from "../api/admin";
import {
  useAdminUsers,
  useChangeRole,
  useDisableUser,
  useEnableUser,
  useResetPassword,
} from "../api/admin";

function UserRow({ u }: { u: AdminUser }) {
  const [pw, setPw] = useState("");
  const disable = useDisableUser();
  const enable = useEnableUser();
  const role = useChangeRole();
  const reset = useResetPassword();
  const busy = disable.isPending || enable.isPending || role.isPending || reset.isPending;
  const err = disable.error || enable.error || role.error || reset.error;
  return (
    <Table.Tr>
      <Table.Td>{u.email}</Table.Td>
      <Table.Td>{u.status}</Table.Td>
      <Table.Td>{u.is_admin ? "admin" : "member"}</Table.Td>
      <Table.Td>
        <Group gap="xs" align="center" wrap="wrap">
          {u.status === "active" && (
            <>
              <Button size="xs" variant="default" disabled={busy} onClick={() => disable.mutate({ id: u.id })}>
                Disable
              </Button>
              <Button size="xs" disabled={busy}
                      onClick={() => role.mutate({ id: u.id, role: u.is_admin ? "member" : "admin" })}>
                {u.is_admin ? "Make member" : "Make admin"}
              </Button>
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  reset.mutate({ id: u.id, new_password: pw }, { onSuccess: () => setPw("") });
                }}
              >
                <Group gap="xs" align="center" wrap="nowrap">
                  <PasswordInput size="xs" value={pw} onChange={(e) => setPw(e.currentTarget.value)}
                                 placeholder="new password" required />
                  <Button size="xs" variant="default" type="submit" disabled={busy}>
                    Reset password
                  </Button>
                </Group>
              </form>
            </>
          )}
          {u.status === "disabled" && (
            <Button size="xs" disabled={busy} onClick={() => enable.mutate({ id: u.id })}>
              Enable
            </Button>
          )}
          {err && (
            <Text role="alert" c="red" size="xs">
              {(err as Error).message}
            </Text>
          )}
        </Group>
      </Table.Td>
    </Table.Tr>
  );
}

export function AdminUsers() {
  const [page, setPage] = useState(1);
  const { data, isLoading, isError } = useAdminUsers(page);
  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data)
    return (
      <Text p="md" role="alert">
        Couldn't load users.
      </Text>
    );
  const { users, per_page, total } = data;
  return (
    <Box component="main" maw="60rem" mx="auto" px="xs" pt="md" pb="xl">
      <Title order={1}>Users</Title>
      <Table horizontalSpacing="sm" verticalSpacing="xs" fz="sm">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Email</Table.Th>
            <Table.Th>Status</Table.Th>
            <Table.Th>Role</Table.Th>
            <Table.Th>Actions</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {users.map((u) => (
            <UserRow key={u.id} u={u} />
          ))}
        </Table.Tbody>
      </Table>
      <Group gap="xs" align="center" mt="md">
        <Text c="dimmed" size="sm">Showing page {page} ({total} total).</Text>
        {page > 1 && (
          <Button size="xs" variant="default" onClick={() => setPage(page - 1)}>
            Prev
          </Button>
        )}
        {total > page * per_page && (
          <Button size="xs" variant="default" onClick={() => setPage(page + 1)}>
            Next
          </Button>
        )}
      </Group>
      <Text size="sm" mt="md">
        <Anchor component={Link} to="/admin/users/pending">Pending approvals</Anchor>
        {" · "}
        <Anchor component={Link} to="/admin">Admin home</Anchor>
      </Text>
    </Box>
  );
}
```

- [ ] **Step 2: Run the test green (from `frontend/`)**

Run: `pnpm exec vitest run src/routes/AdminUsers.test.tsx`
Expected: PASS. The active row shows Disable / Make admin / Reset password buttons and the disabled row shows Enable (all `getByRole("button", { name })`); Make-admin POSTs `role=admin`, Disable POSTs disable, Enable POSTs enable; the reset form's `findByPlaceholderText(/new password/i)` (the `PasswordInput` — its toggle has no placeholder) + `getByRole("button", { name: /reset password/i })` POSTs `{ new_password: "fresh-strong-pw" }`; the "Admin home" `Anchor` is an `<a href="/admin">`; the AdminPendingUsers "Admin home" assertion in this file is unaffected (converted in Task 4 with the same `Anchor`). No test edits.

- [ ] **Step 3: Delete module + grep**

Run (from `frontend/`): `grep -rln "AdminUsers.module.css" src` → expect none. Then `git rm src/routes/AdminUsers.module.css`.

- [ ] **Step 4: Gates (from `frontend/`):** `pnpm exec tsc --noEmit` · `pnpm exec vitest run` · `pnpm build` — all green.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(frontend): re-skin AdminUsers to Mantine; drop AdminUsers.module.css"
```

---

## Task 4: Re-skin `AdminPendingUsers` (cards + password + actions)

**Files:** Modify `frontend/src/routes/AdminPendingUsers.tsx`; Delete `frontend/src/routes/AdminPendingUsers.module.css`. Tests (unchanged): `AdminPendingUsers.test.tsx` (and the cross-reference in `AdminUsers.test.tsx`).
**Interfaces:** Consumes `useAdminPendingUsers`/`useApproveUser`/`useDenyUser` + `AdminPendingUser` type from `../api/admin` (unchanged); `Link`.

- [ ] **Step 1: Replace `AdminPendingUsers.tsx`**

```tsx
import { Anchor, Box, Button, Card, Group, PasswordInput, Text, Title } from "@mantine/core";
import { useState } from "react";
import { Link } from "react-router-dom";

import type { AdminPendingUser } from "../api/admin";
import { useAdminPendingUsers, useApproveUser, useDenyUser } from "../api/admin";

function PendingRow({ u }: { u: AdminPendingUser }) {
  const [pw, setPw] = useState("");
  const approve = useApproveUser();
  const deny = useDenyUser();
  const busy = approve.isPending || deny.isPending;
  const err = approve.error || deny.error;
  // Local (non-Google) accounts need an admin-issued password to log in.
  const needsPw = !u.google_linked;
  const doApprove = (role: "member" | "admin") =>
    approve.mutate({ id: u.id, role, initial_password: needsPw ? pw : undefined });
  return (
    <Card withBorder radius="md" p="md" my="sm" bg="var(--app-sidebar)">
      <Text mb="sm">
        <Text span fw={700}>{u.email}</Text>
        {u.name ? ` — ${u.name}` : ""}
        {u.google_linked && <Text span c="dimmed" size="sm"> (Google)</Text>}
        <Text span c="dimmed" size="sm"> · registered {new Date(u.created_at).toLocaleDateString()}</Text>
      </Text>
      {needsPw && (
        <PasswordInput
          size="xs"
          value={pw}
          onChange={(e) => setPw(e.currentTarget.value)}
          placeholder="initial password"
          required
          mb="sm"
        />
      )}
      <Group gap="xs" wrap="wrap">
        <Button size="xs" disabled={busy || (needsPw && !pw)} onClick={() => doApprove("member")}>
          Approve as member
        </Button>
        <Button size="xs" disabled={busy || (needsPw && !pw)} onClick={() => doApprove("admin")}>
          Approve as admin
        </Button>
        <Button size="xs" variant="default" disabled={busy} onClick={() => deny.mutate({ id: u.id })}>
          Deny
        </Button>
      </Group>
      {err && (
        <Text role="alert" c="red" size="xs" mt="xs">
          {(err as Error).message}
        </Text>
      )}
    </Card>
  );
}

export function AdminPendingUsers() {
  const { data, isLoading, isError } = useAdminPendingUsers(1);
  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data)
    return (
      <Text p="md" role="alert">
        Couldn't load pending accounts.
      </Text>
    );
  const users = data.users;
  return (
    <Box component="main" maw="48rem" mx="auto" px="xs" pt="md" pb="xl">
      <Title order={1}>Pending accounts</Title>
      {users.length === 0 && <Text>No accounts awaiting approval.</Text>}
      {users.map((u) => (
        <PendingRow key={u.id} u={u} />
      ))}
      <Text size="sm" mt="md">
        <Anchor component={Link} to="/admin/users">All users</Anchor>
        {" · "}
        <Anchor component={Link} to="/admin">Admin home</Anchor>
      </Text>
    </Box>
  );
}
```

- [ ] **Step 2: Run the tests green (from `frontend/`)**

Run: `pnpm exec vitest run src/routes/AdminPendingUsers.test.tsx src/routes/AdminUsers.test.tsx`
Expected: PASS. `findByText("local@ki.se")` → the isolated `<Text span fw={700}>` email; `getAllByPlaceholderText(/initial password/i)).toHaveLength(1)` → exactly one (the local account's `PasswordInput`; the Google account renders none; the toggle button has no placeholder); `getByText(/google/i)` → the "(Google)" tag; approve-as-admin POSTs `{ role: "admin", initial_password: "issued-strong-pw" }`; the empty state and the "Admin home" `Anchor` (`<a href="/admin">`) resolve. No test edits.

- [ ] **Step 3: Delete module + grep**

Run (from `frontend/`): `grep -rln "AdminPendingUsers.module.css" src` → expect none. Then `git rm src/routes/AdminPendingUsers.module.css`.

- [ ] **Step 4: Gates (from `frontend/`):** `pnpm exec tsc --noEmit` · `pnpm exec vitest run` · `pnpm build` — all green.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(frontend): re-skin AdminPendingUsers to Mantine; drop AdminPendingUsers.module.css"
```

---

## Task 5: Re-skin `AdminDashboard` (1 table + dl's + banners + buttons)

The banner-heavy route. **Preserve the existing `role="status"`/`role="alert"` on the action banner** (Mantine `Alert` with explicit `role`), and the `role="alert"` on the calibration warning + error state.

**Files:** Modify `frontend/src/routes/AdminDashboard.tsx`; Delete `frontend/src/routes/AdminDashboard.module.css`. Test (unchanged): `AdminDashboard.test.tsx`.
**Interfaces:** Consumes `useAdminDashboard`/`useFetchSource`/`useReenableDigestUser`/`useRerank`/`useSendDigest` from `../api/adminDashboard` (unchanged); `Link`.

- [ ] **Step 1: Replace `AdminDashboard.tsx`**

```tsx
import { Alert, Anchor, Box, Button, Card, Code, Group, Table, Text, Title } from "@mantine/core";
import { useState } from "react";
import { Link } from "react-router-dom";

import {
  useAdminDashboard,
  useFetchSource,
  useReenableDigestUser,
  useRerank,
  useSendDigest,
} from "../api/adminDashboard";

// Ported verbatim from admin.html _ok_msgs / _err_msgs (admin.html:23-31).
const OK_FETCH = "Fetch triggered — the source will refresh shortly.";
const OK_DIGEST = "Weekly digest triggered — it will send shortly.";
const OK_REENABLE = "User re-enabled for the weekly digest.";
const OK_RERANK = "Re-ranking enqueued for all active profiles.";
const ERR_FETCH = "Couldn't trigger the fetch — the scheduler or this source's job is unavailable.";
const ERR_DIGEST = "Digest isn't configured (no email backend), so it can't be sent.";

type Banner = { kind: "ok" | "warn"; text: string } | null;

const fmtDt = (s: string | null) => (s ? new Date(s).toLocaleString() : null);

export function AdminDashboard() {
  const { data, isLoading, isError } = useAdminDashboard();
  const [banner, setBanner] = useState<Banner>(null);
  const rerank = useRerank();
  const sendDigest = useSendDigest();
  const reenable = useReenableDigestUser();
  const fetchSource = useFetchSource();

  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data)
    return (
      <Box component="main" role="alert" maw="60rem" mx="auto" px="xs" pt="md" pb="xl">
        <Text>Couldn't load the admin dashboard.</Text>
      </Box>
    );

  const d = data;

  const onFetch = (slug: string, name: string) =>
    fetchSource.mutate({ slug }, {
      onSuccess: (r) =>
        setBanner(r.triggered
          ? { kind: "ok", text: `${OK_FETCH} (${name})` }
          : { kind: "warn", text: `${ERR_FETCH} (${name})` }),
    });

  const onRerank = () => {
    if (!window.confirm(
      `This enqueues up to ~${d.rerank_estimate} ranking jobs and may take a while. Continue?`
    )) return;
    rerank.mutate(undefined, { onSuccess: () => setBanner({ kind: "ok", text: OK_RERANK }) });
  };

  const onSendDigest = () =>
    sendDigest.mutate(undefined, {
      onSuccess: (r) =>
        setBanner(r.triggered ? { kind: "ok", text: OK_DIGEST } : { kind: "warn", text: ERR_DIGEST }),
    });

  const onReenable = (userId: number) =>
    reenable.mutate({ user_id: userId },
      { onSuccess: () => setBanner({ kind: "ok", text: OK_REENABLE }) });

  return (
    <Box component="main" maw="60rem" mx="auto" px="xs" pt="md" pb="xl">
      <Title order={1}>Operational health</Title>
      {banner && (
        <Alert
          role={banner.kind === "ok" ? "status" : "alert"}
          color={banner.kind === "ok" ? "teal" : "red"}
          mb="md"
        >
          {banner.text}
        </Alert>
      )}

      <Card withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Source health</Title>
        <Table horizontalSpacing="sm" verticalSpacing="xs" fz="sm">
          <Table.Thead><Table.Tr><Table.Th>Source</Table.Th><Table.Th>Last fetched</Table.Th><Table.Th>Calls</Table.Th><Table.Th>Actions</Table.Th></Table.Tr></Table.Thead>
          <Table.Tbody>
            {d.sources.map((s) => (
              <Table.Tr key={s.slug}>
                <Table.Td>{s.name} <Code>{s.slug}</Code></Table.Td>
                <Table.Td>{fmtDt(s.last_fetched_at) ?? <Text span c="dimmed">never</Text>}</Table.Td>
                <Table.Td><Text span fw={700}>{s.call_count}</Text></Table.Td>
                <Table.Td>
                  <Button size="xs" variant="default" disabled={fetchSource.isPending}
                          onClick={() => onFetch(s.slug, s.name)}>
                    Fetch now
                  </Button>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Card>

      <Card withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Ranking queue</Title>
        <Group gap="lg" mb="sm">
          <Box><Text c="dimmed" size="xs">Pending</Text><Text fz="1.4rem" fw={700}>{d.queue_counts.pending}</Text></Box>
          <Box><Text c="dimmed" size="xs">Running</Text><Text fz="1.4rem" fw={700}>{d.queue_counts.running}</Text></Box>
          <Box><Text c="dimmed" size="xs">Done</Text><Text fz="1.4rem" fw={700}>{d.queue_counts.done}</Text></Box>
          <Box><Text c="dimmed" size="xs">Failed</Text><Text fz="1.4rem" fw={700}>{d.queue_counts.failed}</Text></Box>
        </Group>
        <Button size="xs" variant="default" disabled={rerank.isPending} onClick={onRerank}>
          Re-rank everything (last {d.backfill_days} days × active profiles)
        </Button>
        <Text c="dimmed" size="sm" mt="xs">
          Enqueues up to ~{d.rerank_estimate} ranking jobs ({d.active_profiles_count} active
          profiles × {d.recent_calls_count} recent calls; upper bound).
        </Text>
      </Card>

      {d.calibration_warning && (
        <Alert role="alert" color="yellow" mb="md">
          <Text span fw={700}>Embedding thresholds uncalibrated.</Text> {d.calibration_warning}
        </Alert>
      )}

      <Card withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Embedding coverage</Title>
        <Box component="dl" m={0}>
          <Text component="dt" size="xs" c="dimmed" mt="xs">Calls</Text>
          <Text component="dd" size="sm" m={0}><Text span fw={700}>{d.embedding_coverage.call_embedded}/{d.embedding_coverage.call_total} embedded</Text>{" "}
            <Text span c="dimmed">({d.embedding_coverage.call_stale} stale, model <Code>{d.embedding_coverage.current_model}</Code>)</Text></Text>
          <Text component="dt" size="xs" c="dimmed" mt="xs">Profiles</Text>
          <Text component="dd" size="sm" m={0}><Text span fw={700}>{d.embedding_coverage.profile_embedded}/{d.embedding_coverage.profile_total} embedded</Text>{" "}
            <Text span c="dimmed">({d.embedding_coverage.profile_stale} stale, model <Code>{d.embedding_coverage.current_model}</Code>)</Text></Text>
        </Box>
      </Card>

      <Card withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Digest (weekly)</Title>
        <Box component="dl" m={0}>
          <Text component="dt" size="xs" c="dimmed" mt="xs">Last digest sent</Text>
          <Text component="dd" size="sm" m={0}>{fmtDt(d.digest.last_sent_at) ?? <Text span c="dimmed">No digests sent</Text>}</Text>
          <Text component="dt" size="xs" c="dimmed" mt="xs">Runs this week</Text><Text component="dd" size="sm" m={0}>{d.digest.runs_this_week}</Text>
          <Text component="dt" size="xs" c="dimmed" mt="xs">Disabled users (3+ bounces)</Text><Text component="dd" size="sm" m={0}>{d.digest.disabled_users}</Text>
        </Box>
        {d.digest.disabled_user_rows.length > 0 ? (
          <Box component="ul" style={{ listStyle: "none", padding: 0 }}>
            {d.digest.disabled_user_rows.map((u) => (
              <Group component="li" key={u.id} gap="xs" align="center" mt="xs">
                {u.email} <Text span c="dimmed">disabled {fmtDt(u.disabled_at)}</Text>
                <Button size="xs" variant="default" disabled={reenable.isPending}
                        onClick={() => onReenable(u.id)}>Re-enable</Button>
              </Group>
            ))}
          </Box>
        ) : <Text c="dimmed" size="sm">No disabled users.</Text>}
        <Button size="xs" variant="default" mt="sm" disabled={sendDigest.isPending} onClick={onSendDigest}>
          Send digest now
        </Button>
      </Card>

      <Card withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Ranking quality — Kendall's τ (last run)</Title>
        <Text mb="sm"><Anchor component={Link} to="/admin/golden-set">Manage golden set →</Anchor></Text>
        {d.golden_set_report ? (
          <Box component="dl" m={0}>
            <Text component="dt" size="xs" c="dimmed" mt="xs">Curated triples</Text><Text component="dd" size="sm" m={0}>{d.golden_set_report.n_fixture}</Text>
            <Text component="dt" size="xs" c="dimmed" mt="xs">τ (n={d.golden_set_report.n_compared})</Text>
            <Text component="dd" size="sm" m={0}>{d.golden_set_report.tau !== null ? <Text span fw={700}>{d.golden_set_report.tau.toFixed(3)}</Text> : "—"}</Text>
          </Box>
        ) : (
          <Text c="dimmed" size="sm">Golden-set evaluation hasn't run yet — happens Mondays at 06:00 Europe/Stockholm.</Text>
        )}
      </Card>
    </Box>
  );
}
```

- [ ] **Step 2: Run the test green (from `frontend/`)**

Run: `pnpm exec vitest run src/routes/AdminDashboard.test.tsx`
Expected: PASS. `findByText("Vinnova")` (table cell), `getByText("bounced@ki.se")` (disabled-user `li`), `getByText(/1\/3 embedded/)` (the isolated `<Text span fw={700}>` in the coverage `dd`), `getByText(/up to ~6/)` (the muted rerank caption) all resolve; the error path's `findByRole("alert")` has `/couldn't load/i`; Fetch-now POSTs the slug and the `role="status"` ok `Alert` shows `/fetch triggered/i`; the not-triggered path shows the `role="alert"` warn `Alert` `/couldn't trigger the fetch/i`; rerank calls `window.confirm` and does NOT POST when declined; Re-enable POSTs `{ user_id: 9 }`. No test edits.

- [ ] **Step 3: Delete module + grep**

Run (from `frontend/`): `grep -rln "AdminDashboard.module.css" src` → expect none. Then `git rm src/routes/AdminDashboard.module.css`.

- [ ] **Step 4: Gates (from `frontend/`):** `pnpm exec tsc --noEmit` · `pnpm exec vitest run` · `pnpm build` — all green.

- [ ] **Step 5: Teardown grep (gate)**

Run (from `frontend/`):
```bash
grep -rln "AdminDashboard.module.css\|AdminDrafting.module.css\|AdminGoldenSet.module.css\|AdminUsers.module.css\|AdminPendingUsers.module.css" src || echo "5 admin content modules gone — clean"
```
Expected: clean. (`AdminNav.module.css` deliberately remains.)

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(frontend): re-skin AdminDashboard to Mantine; drop AdminDashboard.module.css"
```

---

## Verification (whole slice, before merge)

1. `pnpm exec tsc --noEmit` clean.
2. `pnpm exec vitest run` full green (all six admin test files byte-unchanged).
3. `pnpm build` succeeds.
4. Teardown grep: the five admin content modules unreferenced; `AdminNav.module.css` deliberately kept (Task 5 Step 5).
5. **Visual:** the backendless Playwright route-mock harness ([[reference_spa_visual_check]]) over `/admin`, `/admin/drafting`, `/admin/golden-set`, `/admin/users`, `/admin/users/pending`. These render inside the authed AppShell under `RequireAdmin`, so mock `/api/me` as `{ user: {…}, capabilities: { admin: true } }` (admin TRUE — RequireAdmin gates on it) plus each route's endpoint (mirror the fixtures in each route test: `DASH`, `DRAFT`, `GS`, the users/pending payloads). Confirm: Mantine `Table`s render with borders/headers; the action banners (`/admin` Fetch-now → teal `status` Alert); the disabled/dimmed nothing here; the compact `size="xs"` buttons + `PasswordInput`s.
6. Independent diff-vs-parity review before merge: confirm no copy drift (all headings, table headers, banner message constants `OK_*`/`ERR_*`, button labels, pager text, "(Google)"/"registered" tags), all mutation calls + confirm()-gate + navigation/links unchanged, the AdminDashboard banner roles preserved (`status`/`alert`), `TextInput type="number"` (not `NumberInput`), `PasswordInput` placeholders intact, and `AdminNav`/`AdminLayout` untouched.

## Finishing

Controller-only, at the user's review gate (not a task): merge `feat/mantine-sweep-admin` to LOCAL `master` with `--no-ff` (never push), then `pnpm build` and copy `dist/*` into the gitignored `src/call_manager/web/spa/` to restage the bundle, then clean up the worktree/branch (superpowers:finishing-a-development-branch).

---

## Self-Review

**Spec coverage** (against `2026-06-28-app-mantine-sweep-design.md`, Slice 4 "All seven raw `<table>`s → Mantine Table … admin forms/actions/banners → Mantine"): AdminDashboard ×1 table (Task 5), AdminDrafting ×3 (Task 1), AdminGoldenSet ×2 (Task 2), AdminUsers ×1 (Task 3) = **7 tables** ✓; forms (golden-set add ×2, user reset, pending approve) → Mantine `TextInput`/`PasswordInput`/`Button` ✓; actions/banners → `Button`/`Alert` ✓; five content modules retired ✓. **Scope note:** `AdminNav`/`AdminLayout` kept as-is (nav chrome) — a deliberate, flagged deviation from the literal in-scope list, with rationale in Global Constraints.

**Placeholder scan:** No "TBD/TODO". Full target file for every changed file; every run step has command + expected result.

**Type/name consistency:** All admin hooks consumed with unchanged signatures; every mutation payload + `onSuccess` reset/navigation unchanged; banner message constants copied verbatim; `e.currentTarget.value` throughout; `React.FormEvent` kept; `size="xs"` density applied consistently; `TextInput type="number"` (string state + `Number()` coercion preserved), `PasswordInput` for password fields.

**Behaviour parity:** loading/error early-returns preserve `role="alert"` + copy; the AdminDashboard action banner preserves its `role={ok?"status":"alert"}` live-region semantics (existing, NOT new); the rerank `window.confirm` gate, the per-row busy/disabled wiring, the pending-user `needsPw` gating, and the pager bounds are all preserved. Mantine `Table` renders real table elements so every cell role/text query survives.
