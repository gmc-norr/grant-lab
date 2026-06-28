# App-Wide Mantine Sweep — Slice 1 (Auth) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-skin every auth surface (Login, Register, PreAuthLayout, AccountPending, AccountNoAccess, AuthError) to Mantine v9 primitives, retire `PreAuth.module.css`, and delete the three dead wrapper components (`Button`/`Card`/`Select`) — establishing the Mantine form vocabulary the later sweep slices reuse.

**Architecture:** Pure frontend re-skin. The six auth pages currently render raw `<h1>/<p>/<form>/<input>/<button>` styled by the shared `PreAuth.module.css`. We swap them for Mantine `Title`/`Text`/`TextInput`/`PasswordInput`/`Button`/`Alert`/`Anchor` (dark-teal theme inherited from the existing `theme`), and convert the `PreAuthLayout` card shell to `Center`/`Paper`/`Stack`. No behaviour, data-flow, API, route, or copy changes. No schema, no OpenAPI regen.

**Tech Stack:** React 19.2.7, Mantine v9.4.0, react-router-dom, TanStack Query, Vite, Vitest + Testing Library (jsdom), pnpm.

## Global Constraints

_Every task's requirements implicitly include this section._

- **Re-skin only.** No behaviour / data-flow / API / route / copy changes. Pure frontend; no backend, no schema, no OpenAPI regen.
- **Mantine v9.4.0 primitive for every UI element.** Dark teal theme inherited from the existing `theme` — no per-component colour overrides unless irreducible.
- **`tsc` is the type gate.** No TS ESLint. `noUnusedLocals` is on → import only what you use; drop the `import styles from "./PreAuth.module.css"` line from every converted file.
- **Test preservation is the gate.** Every existing route test must still pass; adjust queries to the new tree **without weakening them**. Add a structural assertion only where it strengthens coverage.
- **Mantine `PasswordInput` renders a visibility-toggle button with `aria-label="Toggle password visibility"`.** This collides with `getByLabelText(/password/i)` (matches the input AND the toggle → "found multiple elements"). Disambiguate with `getByLabelText(/password/i, { selector: "input" })`.
- **Mantine `Button` does not force `type`.** Keep an explicit `type="submit"` on every form-submit button (Login sign-in, Login Google, Register), exactly as the raw markup did, so the native form submit still fires.
- **Mantine `Alert` defaults to `role="alert"`** (`role || "alert"`), so the Login error stays reachable by `findByRole("alert")`. Pass no `title`/`icon` so the alert's `textContent` is exactly the message string.
- **`Anchor component={Link} to="…"`** for every in-app link — keeps client-side navigation AND the asserted `href` attribute. Never a bare Mantine `Anchor href` (that would full-page navigate).
- **No top-level bare element selectors** in any `.module.css` (n/a here once `PreAuth.module.css` is gone, but holds for any module you touch).
- **Mantine event handlers use `e.currentTarget.value`** (the repo idiom — see `RubricEditor`/`CallsFilters`), not `e.target.value`.
- **Branch / worktree:** work in an isolated worktree `.worktrees/mantine-sweep-auth` on branch `feat/mantine-sweep-auth` (created at execution time via superpowers:using-git-worktrees). Commit per task. The controller merges to LOCAL master `--no-ff` (never push) only at the user's review gate, then rebuilds + restages the gitignored SPA bundle.
- **Whole-suite `vitest` is reliable here** — only backend testcontainers flakes, and this slice changes no backend.

---

## File Structure

**Deleted (Task 1 — dead code, zero consumers):**
- `frontend/src/components/Button.tsx`, `Button.module.css`, `Button.test.tsx`
- `frontend/src/components/Card.tsx`, `Card.module.css`
- `frontend/src/components/Select.tsx`, `Select.module.css`

**Modified (Tasks 2–3 — auth pages → Mantine):**
- `frontend/src/routes/Login.tsx` + `Login.test.tsx` (Task 2)
- `frontend/src/routes/Register.tsx` (Task 2 — test unchanged)
- `frontend/src/routes/PreAuthLayout.tsx` (Task 3 — test unchanged)
- `frontend/src/routes/AccountPending.tsx`, `AccountNoAccess.tsx`, `AuthError.tsx` (Task 3)

**Deleted (Task 3 — after the last importer converts):**
- `frontend/src/routes/PreAuth.module.css`

**Untouched:** `frontend/src/components/MatchBadge.*`, `Pagination.*` (Pagination is converted later in Slice 5), `frontend/src/shell/BrandMark.tsx`, `frontend/src/router.tsx` (route wiring is unchanged), `frontend/src/api/auth.ts`.

---

## Task 1: Delete the three dead wrapper components

The thin `Button`/`Card`/`Select` element-wrappers have **zero consumers** (verified: `grep -rn "components/\(Button\|Card\|Select\)" src` returns only the files themselves). Deleting them up front clears the wrappers the sweep is meant to eliminate and proves the teardown-grep gate early.

**Files:**
- Delete: `frontend/src/components/Button.tsx`, `frontend/src/components/Button.module.css`, `frontend/src/components/Button.test.tsx`
- Delete: `frontend/src/components/Card.tsx`, `frontend/src/components/Card.module.css`
- Delete: `frontend/src/components/Select.tsx`, `frontend/src/components/Select.module.css`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing (removal only). `frontend/src/components/` retains `MatchBadge.*` and `Pagination.*`, so the directory is not empty.

- [ ] **Step 1: Confirm zero consumers (teardown gate, pre-delete)**

Run (from `frontend/`):
```bash
grep -rn "components/Button\|components/Card\|components/Select" src --include=*.tsx --include=*.ts | grep -v "src/components/Button" | grep -v "src/components/Card" | grep -v "src/components/Select"
```
Expected: no output (no external importer).

- [ ] **Step 2: Delete the seven files**

```bash
git rm src/components/Button.tsx src/components/Button.module.css src/components/Button.test.tsx \
       src/components/Card.tsx src/components/Card.module.css \
       src/components/Select.tsx src/components/Select.module.css
```

- [ ] **Step 3: Type-check**

Run: `pnpm exec tsc --noEmit`
Expected: clean (no dangling import errors).

- [ ] **Step 4: Run the full suite**

Run: `pnpm test -- --run`
Expected: green; the only delta is the removed `Button.test.tsx` (one fewer test file). No suite references the deleted modules.

- [ ] **Step 5: Build**

Run: `pnpm build`
Expected: `tsc && vite build` succeeds.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(frontend): delete dead Button/Card/Select wrappers"
```

---

## Task 2: Convert Login + Register to Mantine (the form vocabulary)

Convert the two auth **forms** to Mantine `TextInput`/`PasswordInput`/`Button`/`Alert`/`Anchor` + `Title`/`Text`, establishing the form idiom the later slices reuse. `PreAuthLayout` still provides the old card shell until Task 3 — its descendant `.card h1` / `.card p` selectors harmlessly style the Mantine `<h1>`/`<p>` in the interim, so the intermediate state stays visually coherent.

**Files:**
- Modify: `frontend/src/routes/Login.tsx`
- Modify: `frontend/src/routes/Login.test.tsx` (password-query disambiguation only)
- Modify: `frontend/src/routes/Register.tsx`
- Test (unchanged, must stay green): `frontend/src/routes/Register.test.tsx`

**Interfaces:**
- Consumes: `useAuthConfig`, `useLogin`, `useRegister` from `../api/auth`; `ApiError` from `../api/client` (all unchanged). `errorMessage(err)` helper stays verbatim in `Login.tsx`.
- Produces: the Mantine form pattern — `<form onSubmit>` wrapping `<Stack gap="sm">` of `TextInput`/`PasswordInput` + a full-width `Button type="submit"`; errors via `<Alert color="red">`; links via `<Anchor component={Link}>`. Tasks 3+ reuse this.

- [ ] **Step 1: Replace `Login.tsx` body with the Mantine form**

Full file:
```tsx
import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Alert, Anchor, Button, PasswordInput, Stack, Text, TextInput, Title } from "@mantine/core";

import { useAuthConfig, useLogin } from "../api/auth";
import { ApiError } from "../api/client";

function errorMessage(err: unknown): string {
  // unwrap throws ApiError(code, "code: message"); strip the code prefix so the
  // form shows the bare server message (always the generic one for a 400).
  if (err instanceof ApiError) return err.message.slice(err.code.length + 2);
  return "Something went wrong. Please try again.";
}

export function Login() {
  const cfg = useAuthConfig().data;
  const [sp] = useSearchParams();
  const next = sp.get("next");
  const navigate = useNavigate();
  const qc = useQueryClient();
  const login = useLogin();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  return (
    <>
      <Title order={1} size="h4" ta="center">
        Sign in
      </Title>
      <Text ta="center" c="dimmed" size="sm">
        Enter your email and password.
      </Text>
      {login.isError && <Alert color="red">{errorMessage(login.error)}</Alert>}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          login.mutate(
            { email, password, next },
            {
              onSuccess: (data) => {
                qc.invalidateQueries({ queryKey: ["me"] });
                navigate(data.redirect);
              },
            },
          );
        }}
      >
        <Stack gap="sm">
          <TextInput
            label="Email"
            type="email"
            required
            autoFocus
            autoComplete="email"
            value={email}
            placeholder={`you@${cfg?.allowed_domains[0] ?? "example.org"}`}
            onChange={(e) => setEmail(e.currentTarget.value)}
          />
          <PasswordInput
            label="Password"
            required
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.currentTarget.value)}
          />
          <Button type="submit" fullWidth disabled={login.isPending}>
            Sign in
          </Button>
        </Stack>
      </form>
      {cfg?.google_oidc_enabled && (
        // Native server-side OAuth start: a full-page POST carries Origin/CSRF and
        // 303s to Google (cannot be a fetch). safe_next is applied server-side.
        <form method="post" action="/auth/google/start">
          {next && <input type="hidden" name="next" value={next} />}
          <Button type="submit" variant="default" fullWidth>
            Continue with Google
          </Button>
        </form>
      )}
      <Text ta="center" size="sm">
        <Anchor component={Link} to="/register">
          Request access
        </Anchor>
      </Text>
    </>
  );
}
```

- [ ] **Step 2: Run the Login test to see the password-query collision fail**

Run: `pnpm test -- --run src/routes/Login.test.tsx`
Expected: FAIL — `getByLabelText(/password/i)` now matches two elements (the password `<input>` and the `PasswordInput` visibility-toggle button labelled "Toggle password visibility"). This is the expected red that motivates Step 3.

- [ ] **Step 3: Disambiguate the three password queries in `Login.test.tsx`**

Add `{ selector: "input" }` to each `getByLabelText(/password/i)` (lines ~22, ~44, ~62) so the query resolves the input, not the toggle button. The three edits:

```tsx
expect(screen.getByLabelText(/password/i, { selector: "input" })).toBeInTheDocument();
```
```tsx
await userEvent.type(screen.getByLabelText(/password/i, { selector: "input" }), "correct-horse-1");
```
```tsx
await userEvent.type(screen.getByLabelText(/password/i, { selector: "input" }), "whatever-long");
```

Leave every other query unchanged: `getByLabelText(/email/i)` has no collision; `getByRole("button", { name: /^sign in$/i })` and `/continue with google/i` do not match the toggle's "Toggle password visibility"; `findByRole("alert")` resolves the Mantine `Alert`.

- [ ] **Step 4: Run the Login test green**

Run: `pnpm test -- --run src/routes/Login.test.tsx`
Expected: PASS (all 5 tests). Confirms: email/password fields present, Google POST form gated on config, `?next` threaded into the POST body, one generic error in an `Alert` with the `bad_request` prefix stripped, and the strip-helper pin.

- [ ] **Step 5: Replace `Register.tsx` body with the Mantine form**

Full file:
```tsx
import { useState } from "react";
import { Link } from "react-router-dom";
import { Anchor, Button, Stack, Text, TextInput, Title } from "@mantine/core";

import { useAuthConfig, useRegister } from "../api/auth";

export function Register() {
  const cfg = useAuthConfig().data;
  const domains = cfg?.allowed_domains ?? [];
  const register = useRegister();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");

  if (register.isSuccess) {
    return (
      <>
        <Title order={1} size="h4" ta="center">
          Request received
        </Title>
        <Text ta="center" c="dimmed" size="sm">
          Thanks — your access request has been recorded. An administrator will
          review it and set up your account. You'll be able to sign in once it's
          approved.
        </Text>
        <Text ta="center" size="sm">
          <Anchor component={Link} to="/login">
            Back to sign in
          </Anchor>
        </Text>
      </>
    );
  }

  return (
    <>
      <Title order={1} size="h4" ta="center">
        Request access
      </Title>
      <Text ta="center" c="dimmed" size="sm">
        Enter your hospital email. An administrator will review your request and set
        up your account.
      </Text>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          register.mutate({ email, name: name.trim() || null });
        }}
      >
        <Stack gap="sm">
          <TextInput
            label="Email"
            type="email"
            required
            autoFocus
            autoComplete="email"
            value={email}
            placeholder={`you@${domains[0] ?? "example.org"}`}
            onChange={(e) => setEmail(e.currentTarget.value)}
          />
          <TextInput
            label="Name (optional)"
            type="text"
            maxLength={200}
            autoComplete="name"
            value={name}
            onChange={(e) => setName(e.currentTarget.value)}
          />
          {domains.length > 0 && (
            <Text size="xs" c="dimmed">
              Use your hospital email ({domains.join(" or ")}).
            </Text>
          )}
          <Button type="submit" fullWidth disabled={register.isPending}>
            Request access
          </Button>
        </Stack>
      </form>
      <Text ta="center" size="sm">
        <Anchor component={Link} to="/login">
          Back to sign in
        </Anchor>
      </Text>
    </>
  );
}
```

- [ ] **Step 6: Run the Register test (unchanged) green**

Run: `pnpm test -- --run src/routes/Register.test.tsx`
Expected: PASS. `getByLabelText(/email/i)` resolves the email input (the "Name (optional)" label has no "email"; there is no `PasswordInput`), the submit `Button` matches `/request access/i`, the success state's `Title` text matches `getByText(/request received/i)`, and the POST body is `{ email: "new@ki.se", name: null }`.

- [ ] **Step 7: Type-check + full suite + build**

Run:
```bash
pnpm exec tsc --noEmit
pnpm test -- --run
pnpm build
```
Expected: tsc clean; full vitest green; build succeeds. (`Login.tsx`/`Register.tsx` no longer import `PreAuth.module.css`; the four remaining auth files still do, so the module stays.)

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(frontend): re-skin Login + Register to Mantine"
```

---

## Task 3: Convert PreAuthLayout + the three status pages, then retire `PreAuth.module.css`

Convert the remaining four auth files (the `PreAuthLayout` card shell + the three static status pages) to Mantine, dropping the last `PreAuth.module.css` importers, then delete the now-unreferenced module. This closes the auth slice.

**Files:**
- Modify: `frontend/src/routes/PreAuthLayout.tsx`
- Modify: `frontend/src/routes/AccountPending.tsx`, `frontend/src/routes/AccountNoAccess.tsx`, `frontend/src/routes/AuthError.tsx`
- Delete: `frontend/src/routes/PreAuth.module.css`
- Test (unchanged, must stay green): `frontend/src/routes/PreAuthLayout.test.tsx`

**Interfaces:**
- Consumes: the Mantine link/text idiom from Task 2 (`Anchor component={Link}`, `Title`, `Text`). `BrandMark` from `../shell/BrandMark` (unchanged; it hardcodes its own CSS vars, so no colour wrapper is needed — the old inert `.brand { color }` is dropped).
- Produces: nothing downstream; the auth slice ends here.

- [ ] **Step 1: Convert `PreAuthLayout.tsx` to `Center` / `Paper` / `Stack`**

Full file (keep the anti-loop invariant comment verbatim):
```tsx
import { Center, Paper, Stack } from "@mantine/core";
import { Outlet } from "react-router-dom";

import { BrandMark } from "../shell/BrandMark";

// SECOND top-level pathless layout (sibling to AppShell). Centered card on the
// app background, NO ActivityBar / Sidebar / StatusBar. INVARIANT: this layout
// and its pages make NO authenticated /api call on mount — a useSession here
// would 401 -> unwrap -> redirectToLogin -> infinite loop. The public
// /api/auth/config call lives in the pages (Login/Register), not here.
export function PreAuthLayout() {
  return (
    <Center mih="100vh" bg="var(--app-editor)" p="md">
      <Paper
        component="main"
        w="100%"
        maw="22rem"
        p="xl"
        radius="md"
        shadow="sm"
        withBorder
        bg="var(--app-sidebar)"
      >
        <Stack gap="sm">
          <Center>
            <BrandMark size={36} />
          </Center>
          <Outlet />
        </Stack>
      </Paper>
    </Center>
  );
}
```

- [ ] **Step 2: Convert `AccountPending.tsx`**

Full file:
```tsx
import { Anchor, Text, Title } from "@mantine/core";
import { Link } from "react-router-dom";

export function AccountPending() {
  return (
    <>
      <Title order={1} size="h4" ta="center">
        Account awaiting approval
      </Title>
      <Text ta="center" c="dimmed" size="sm">
        Your sign-in worked. An administrator needs to approve your account before
        you can use Call Manager. You'll be able to sign in once approved.
      </Text>
      <Text ta="center" size="sm">
        <Anchor component={Link} to="/login">
          Back to sign in
        </Anchor>
      </Text>
    </>
  );
}
```

- [ ] **Step 3: Convert `AccountNoAccess.tsx`**

Full file:
```tsx
import { Anchor, Text, Title } from "@mantine/core";
import { Link } from "react-router-dom";

export function AccountNoAccess() {
  return (
    <>
      <Title order={1} size="h4" ta="center">
        No access
      </Title>
      <Text ta="center" c="dimmed" size="sm">
        This account does not have access. If you think this is a mistake, contact
        an administrator.
      </Text>
      <Text ta="center" size="sm">
        <Anchor component={Link} to="/login">
          Back to sign in
        </Anchor>
      </Text>
    </>
  );
}
```

- [ ] **Step 4: Convert `AuthError.tsx`** (two body paragraphs → two `Text`)

Full file:
```tsx
import { Anchor, Text, Title } from "@mantine/core";
import { Link } from "react-router-dom";

export function AuthError() {
  return (
    <>
      <Title order={1} size="h4" ta="center">
        Sign-in could not be completed
      </Title>
      <Text ta="center" c="dimmed" size="sm">
        We couldn't sign you in. Please try again.
      </Text>
      <Text ta="center" c="dimmed" size="sm">
        If you already have an account with this email, sign in your usual way, then
        connect Google from your account settings.
      </Text>
      <Text ta="center" size="sm">
        <Anchor component={Link} to="/login">
          Back to sign in
        </Anchor>
      </Text>
    </>
  );
}
```

- [ ] **Step 5: Confirm `PreAuth.module.css` is now unreferenced, then delete it**

Run (from `frontend/`):
```bash
grep -rln "PreAuth.module.css" src
```
Expected: no output. Then:
```bash
git rm src/routes/PreAuth.module.css
```

- [ ] **Step 6: Run the PreAuthLayout test (unchanged) green**

Run: `pnpm test -- --run src/routes/PreAuthLayout.test.tsx`
Expected: PASS — the child still renders through `<Outlet/>`; `queryByLabelText("Primary")` is absent (no ActivityBar; `BrandMark`'s `aria-label` is "Call Manager", not "Primary"); the layout fires no `/api/me` on mount.

- [ ] **Step 7: Type-check + full suite + build**

Run:
```bash
pnpm exec tsc --noEmit
pnpm test -- --run
pnpm build
```
Expected: tsc clean (no orphaned `styles` import anywhere); full vitest green; build succeeds.

- [ ] **Step 8: Teardown grep (final gate)**

Run (from `frontend/`):
```bash
grep -rln "PreAuth.module.css" src || echo "module gone — clean"
grep -rn "components/Button\|components/Card\|components/Select" src --include=*.tsx --include=*.ts | grep -vE "src/components/(MatchBadge|Pagination)" || echo "dead wrappers gone — clean"
```
Expected: both report clean.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat(frontend): re-skin PreAuthLayout + auth status pages to Mantine; drop PreAuth.module.css"
```

---

## Verification (whole slice, before merge)

1. `pnpm exec tsc --noEmit` clean.
2. `pnpm test -- --run` full green (suite delta: `Button.test.tsx` removed).
3. `pnpm build` succeeds.
4. Teardown grep: `PreAuth.module.css` and the three dead wrappers are unreferenced (Task 3 Step 8).
5. **Visual:** the backendless Playwright route-mock harness ([[reference_spa_visual_check]]) over `/login` (with `useAuthConfig` mocked both with and without `google_oidc_enabled`), `/register` (form + "request received" state), and `/auth/pending|no-access|error` — confirm the centered Paper card, the brand mark, the show/hide password affordance, and the calm-teal styling. Auth pages make no authenticated call, so the harness needs only `/api/auth/config` mocked.
6. Independent diff-vs-parity review before merge: confirm no copy/behaviour drift (button labels, error message, POST bodies, the `?next` thread, the native Google POST `action`/`method`), and that no Mantine `loading`/extra-affordance behaviour change slipped in beyond the intended re-skin.

## Finishing

Controller-only, at the user's review gate (not a task): merge `feat/mantine-sweep-auth` to LOCAL `master` with `--no-ff` (never push), then `pnpm build` and copy `dist/*` into the gitignored `src/call_manager/web/spa/` to restage the bundle, then clean up the worktree/branch (superpowers:finishing-a-development-branch).

---

## Self-Review

**Spec coverage** (against `2026-06-28-app-mantine-sweep-design.md`, Slice 1):
- Login/Register forms → `TextInput`/`PasswordInput`/`Button` (+ Mantine form error/`Alert`) ✓ Task 2.
- Static status pages → `Text`/`Alert`/`Card` ✓ Task 3 (`Title`/`Text` for copy; `Paper` card shell in `PreAuthLayout`; no error state on the static pages, so no `Alert` needed there — `Alert` is exercised on Login).
- Deletes dead `Button`/`Card`/`Select` + CSS + `Button.test.tsx` ✓ Task 1.
- Establishes the Mantine form vocabulary ✓ (Task 2 "Produces").
- Self-contained (PreAuth layout, no shell) ✓ — `PreAuthLayout` owns the shell; no `AppShell` interaction.
- Retire the CSS module ✓ Task 3 Step 5 (after the last importer converts).

**Placeholder scan:** No "TBD/TODO". Every code step shows the complete file or the exact edit; every run step states the command and expected result.

**Type/name consistency:** `errorMessage` helper preserved verbatim in `Login.tsx`. Hooks (`useAuthConfig`/`useLogin`/`useRegister`) and `ApiError` unchanged. `onChange` uses `e.currentTarget.value` (repo idiom) consistently. `Button type="submit"` on all three submit buttons. `Anchor component={Link} to=…` on all five links. `Title order={1} size="h4" ta="center"` consistent across all six headings.

**Ordering safety:** `PreAuth.module.css` is deleted only in Task 3 Step 5, after all six files stop importing it; Tasks 1 and 2 each leave the suite/build green, and the Task-2 interim (Mantine pages inside the still-CSS-module card) renders coherently via the card's descendant selectors.
