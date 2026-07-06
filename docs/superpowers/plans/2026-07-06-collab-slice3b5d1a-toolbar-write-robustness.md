# Collab Slice 3b-5d·D1a — Selection-Toolbar Write-Action Robustness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: this plan is executed **inline** with superpowers:executing-plans. The acceptance gate is an interactive two-browser e2e (like D0), and Task 6 (C#5) is applied **only if** that run surfaces it — so a headless autonomous build cannot make the reactive call. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make the live collab Cite/Edit selection actions actually work — the write-actions re-enable once the WS buffer drains (fixing the D0 settled-latch), Ask/Edit surface disclosure + generic errors instead of failing silently, and the full live Cite flow is proven end-to-end by restoring D0's two-browser scenario.

**Architecture:** Client-only. A shared `useSettled` poll hook makes the toolbar's `settled` gate reactive (mirroring `SaveVersionButton`, which is already immune). The CM6 disclosure ack/retry + generic-error pattern is ported into the Tiptap toolbar. D0 e2e scenario (c) is restored to the full select→compose→insert→converge flow as the acceptance gate; C#5 (composer unmount on `claim → null`) is fixed reactively (freeze a `ClaimContext` snapshot) only if the run surfaces it.

**Tech Stack:** React 19 + TypeScript + Mantine 9 + TanStack Query + Tiptap v3 + custom Yjs WS provider; `vitest` unit; `@playwright/test` two-browser e2e via `scripts/collab_e2e_run.sh`.

## Global Constraints

- Feature flag STAYS OFF in prod. No `enable_collab` source caller. **Client-only.**
- **No backend, no endpoint, no DB migration, no schema change.** Source edits confined to `frontend/src/collab/*` + `frontend/e2e/collab.spec.ts`.
- **Do not regress `SaveVersionButton`** — identical poll behaviour (200ms, `isSettled()`, initial synchronous sample) after the refactor.
- Frontend gate: `pnpm exec tsc --noEmit` (there is NO TS linter — tsc is the gate) + `pnpm exec vitest run <file>`. The e2e stays opt-in / skip-gated on the D0 seed vars.
- `git add` explicit paths only. Work on branch `collab-slice3b5d1a` cut from `master` (`fbd1f39`); merge `--no-ff`, **local only**, is **user-gated** (STOP before merge).
- `provider.isSettled()` ([collabProvider.ts:82](../../../frontend/src/collab/collabProvider.ts#L82)) already includes the `status === "connected"` term, so the whole settle predicate is just `isSettled()`.

## File Structure

| File | Change | Responsibility |
|------|--------|----------------|
| `frontend/src/collab/useSettled.ts` | **create** | 200ms poll of `provider.isSettled()` → reactive boolean. |
| `frontend/src/collab/useSettled.test.ts` | **create** | Hook re-samples on the interval (fake timers, controllable stub). |
| `frontend/src/collab/SaveVersionButton.tsx` | **modify** | Consume `useSettled` (delete inline poll; behaviour identical). |
| `frontend/src/collab/TiptapSelectionToolbar.tsx` | **modify** | `settled` via `useSettled` (reactive); disclosure ack/retry + generic error. |
| `frontend/src/collab/TiptapSelectionToolbar.test.tsx` | **modify** | Reactive-settle regression test; disclosure + generic-error tests. |
| `frontend/e2e/collab.spec.ts` | **modify** | Restore scenario (c) to the full live Cite flow (acceptance gate). |
| `frontend/src/collab/TiptapSelectionToolbar.tsx` (again) | **modify (conditional)** | Task 6 — freeze `ClaimContext` snapshot + lift composer, ONLY if the e2e surfaces C#5. |

---

## Task 0: Branch

- [ ] **Step 1: Cut the D1a branch from master**

```bash
cd /home/parlar_ai/dev/call-monitor
git checkout -b collab-slice3b5d1a
git branch --show-current   # -> collab-slice3b5d1a
```

---

## Task 1: `useSettled` shared poll hook (+ refactor SaveVersionButton)

**Files:**
- Create: `frontend/src/collab/useSettled.ts`, `frontend/src/collab/useSettled.test.ts`
- Modify: `frontend/src/collab/SaveVersionButton.tsx`

**Interfaces:**
- Produces: `useSettled(provider: CollabProvider): boolean` — a reactive mirror of `provider.isSettled()`, re-sampled every 200ms.

- [ ] **Step 1: Write the failing hook test**

Create `frontend/src/collab/useSettled.test.ts`:

```ts
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CollabProvider } from "./collabProvider";
import { useSettled } from "./useSettled";

// Controllable stub: a mutable flag, NOT a fixed call-sequence — useSettled samples
// isSettled() eagerly in BOTH the useState initializer and the immediate effect, so a
// "false then true" sequence would flip before any timer advance and prove nothing.
function stubProvider(initial: boolean) {
  const ref = { val: initial };
  const provider = { isSettled: () => ref.val } as unknown as CollabProvider;
  return { provider, ref };
}

describe("useSettled", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("starts at the provider's current settle state and re-samples on the interval", () => {
    const { provider, ref } = stubProvider(false);
    const { result } = renderHook(() => useSettled(provider));
    expect(result.current).toBe(false); // initializer + immediate effect both see false

    ref.val = true;
    act(() => vi.advanceTimersByTime(200));
    expect(result.current).toBe(true); // the poll picked up the drain

    ref.val = false;
    act(() => vi.advanceTimersByTime(200));
    expect(result.current).toBe(false);
  });
});
```

- [ ] **Step 2: Run it red**

Run: `cd frontend && pnpm exec vitest run src/collab/useSettled.test.ts`
Expected: FAIL — `Cannot find module './useSettled'`.

- [ ] **Step 3: Implement the hook**

Create `frontend/src/collab/useSettled.ts`:

```ts
import { useEffect, useState } from "react";

import type { CollabProvider } from "./collabProvider";

/**
 * Reactive mirror of `provider.isSettled()` (socket open + drained + idle since the
 * last local edit). The value is time-varying — it flips true a tick after an
 * awareness send drains the WS buffer — so consumers that gate on it (SaveVersionButton,
 * the selection toolbar's Edit/Cite) MUST re-render when it changes, not sample it once
 * at render (the D0 settled-latch). Polls every 200ms, matching SaveVersionButton's
 * original inline poll.
 */
export function useSettled(provider: CollabProvider): boolean {
  const [settled, setSettled] = useState(() => provider.isSettled());
  useEffect(() => {
    setSettled(provider.isSettled()); // re-sample immediately on provider change
    const t = setInterval(() => setSettled(provider.isSettled()), 200);
    return () => clearInterval(t);
  }, [provider]);
  return settled;
}
```

- [ ] **Step 4: Run it green**

Run: `cd frontend && pnpm exec vitest run src/collab/useSettled.test.ts`
Expected: PASS.

- [ ] **Step 5: Refactor `SaveVersionButton` onto the hook**

In `frontend/src/collab/SaveVersionButton.tsx`, replace the inline poll. Remove the `useState`/`useEffect` settle poll and the now-unused `useEffect` import if it becomes unused (it is still used for the `setSave` effect — keep it). Concretely:

Delete:
```tsx
  const [settled, setSettled] = useState(false);
  useEffect(() => {
    // Poll the settled predicate (socket open + drained + idle since last edit).
    const t = setInterval(() => setSettled(props.provider.isSettled()), 200);
    return () => clearInterval(t);
  }, [props.provider]);
```
Add (after the `useSaveVersion` line) an import and the hook call:
```tsx
import { useSettled } from "./useSettled";
```
```tsx
  const settled = useSettled(props.provider);
```
Remove `useState` from the React import if it is no longer used elsewhere in the file (it is not — only `useEffect` remains, for `setSave`). Final imports line:
```tsx
import { useEffect } from "react";
```

- [ ] **Step 6: Verify SaveVersionButton still typechecks**

There is **no `SaveVersionButton.test.tsx`**, so the refactor is covered by tsc here + the two-browser e2e scenario (a) (which clicks "Save a version") in Task 5.

Run:
```bash
cd frontend
pnpm exec tsc --noEmit
```
Expected: tsc clean (behaviour unchanged — same 200ms `isSettled()` poll, now via the hook).

- [ ] **Step 7: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add frontend/src/collab/useSettled.ts frontend/src/collab/useSettled.test.ts frontend/src/collab/SaveVersionButton.tsx
git commit -m "$(cat <<'EOF'
feat(collab): reactive useSettled poll hook + adopt in SaveVersionButton (3b-5d.D1a)

Extract SaveVersionButton's 200ms isSettled() poll into a shared useSettled hook so
the value is reactive (re-renders when the WS buffer drains). SaveVersionButton is
byte-for-behaviour unchanged; the hook is the fix vehicle for the toolbar settled-latch.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Fix the toolbar settled-latch (reactive `settled`)

**Files:**
- Modify: `frontend/src/collab/TiptapSelectionToolbar.tsx`, `frontend/src/collab/TiptapSelectionToolbar.test.tsx`

**Interfaces:**
- Consumes: `useSettled` (Task 1).

- [ ] **Step 1: Write the failing reactive-settle test**

Add to `frontend/src/collab/TiptapSelectionToolbar.test.tsx`. First extend the harness to allow a controllable provider + a render without auto fake-timer teardown. Add this test (and the `act`/`vi` timer imports — `act` from `@testing-library/react`, `vi` already imported):

```tsx
  it("reactive settle: Cite/Edit enable on the poll tick after the buffer drains", () => {
    vi.useFakeTimers();
    try {
      const ref = { val: false };
      const provider = {
        get status() {
          return "connected";
        },
        isSettled: () => ref.val, // controllable — false until we drain
      } as never;
      const qc = new QueryClient();
      render(
        <MantineProvider theme={theme} defaultColorScheme="dark">
          <QueryClientProvider client={qc}>
            <TiptapSelectionToolbar
              editor={fakeEditor()}
              provider={provider}
              workspaceId="ws-1"
              head={HEAD}
              sections={SECTIONS}
              canEdit={true}
              canWrite={true}
            />
          </QueryClientProvider>
        </MantineProvider>,
      );
      // Unsettled → Ask enabled (anchorable) but Cite/Edit disabled (the latch symptom).
      expect(screen.getByRole("button", { name: /ask/i })).toBeEnabled();
      expect(screen.getByRole("button", { name: /cite/i })).toBeDisabled();
      expect(screen.getByRole("button", { name: /edit/i })).toBeDisabled();
      // Buffer drains → the poll re-enables the write-actions (no reselect needed).
      ref.val = true;
      act(() => vi.advanceTimersByTime(200));
      expect(screen.getByRole("button", { name: /cite/i })).toBeEnabled();
      expect(screen.getByRole("button", { name: /edit/i })).toBeEnabled();
    } finally {
      vi.useRealTimers();
    }
  });
```

- [ ] **Step 2: Run it red**

Run: `cd frontend && pnpm exec vitest run src/collab/TiptapSelectionToolbar.test.tsx -t "reactive settle"`
Expected: FAIL — after `advanceTimersByTime`, Cite/Edit still disabled (the toolbar samples `settled` at render, and no re-render is triggered by the drain).

- [ ] **Step 3: Make `settled` reactive**

In `frontend/src/collab/TiptapSelectionToolbar.tsx`:
1. Add the import:
```tsx
import { useSettled } from "./useSettled";
```
2. Call the hook with the other hooks, ABOVE the `if (!claim) return null` guard (Rules of Hooks). After the `const [citeOpen, setCiteOpen] = useState(false);` line add:
```tsx
  const settled = useSettled(provider);
```
3. Delete the old render-time computation lower down:
```tsx
  // Transport-settled guard: Edit and Cite require a settled connection
  const settled = provider.status === "connected" && provider.isSettled();
```
(The `writeReady = settled && anchorable && !claim.headingOnly;` line is unchanged and now reads the reactive `settled`.)

- [ ] **Step 4: Run it green (+ the existing gate tests)**

Run: `cd frontend && pnpm exec vitest run src/collab/TiptapSelectionToolbar.test.tsx`
Expected: all pass — the reactive-settle test now flips enabled after the tick, and the existing gate tests (which use a fixed-value stub) still pass because `useSettled` samples eagerly in the initializer + immediate effect.

- [ ] **Step 5: Typecheck + commit**

```bash
cd frontend && pnpm exec tsc --noEmit
cd /home/parlar_ai/dev/call-monitor
git add frontend/src/collab/TiptapSelectionToolbar.tsx frontend/src/collab/TiptapSelectionToolbar.test.tsx
git commit -m "$(cat <<'EOF'
fix(collab): reactive settled gate unlatches Edit/Cite in the live toolbar (3b-5d.D1a)

The toolbar sampled provider.isSettled() only at its selectionUpdate render, but the
same selection's CollaborationCaret awareness send dirties ws.bufferedAmount at that
instant, latching Edit/Cite disabled with no re-render on drain (D0 finding). Use the
reactive useSettled poll so the write-actions re-enable when the buffer drains.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Disclosure ack+retry + generic error surface (C#2)

**Files:**
- Modify: `frontend/src/collab/TiptapSelectionToolbar.tsx`, `frontend/src/collab/TiptapSelectionToolbar.test.tsx`

**Interfaces:**
- Consumes: `useAckDisclosure`, `useSubmitTurn` (`../api/turns`), `ApiError` (`../api/client`).

- [ ] **Step 1: Write the failing disclosure + generic-error tests**

The existing test mocks `useSubmitTurn` as `{ mutate: vi.fn(), isPending: false }`. Replace that mock with a controllable one that invokes the mutate options, and add `useAckDisclosure`. Update the `vi.mock("../api/turns", ...)` block and add a mock for `../api/client`'s `ApiError`:

```tsx
// Controllable submit/ack mocks — capture the options so tests can drive onError/onSuccess.
const submitMock = vi.fn();
const ackMock = vi.fn();
vi.mock("../api/turns", () => ({
  useSubmitTurn: () => ({ mutate: submitMock, isPending: false }),
  useAckDisclosure: () => ({ mutate: ackMock, isPending: false }),
}));
```

`ApiError` is a real class from `../api/client`; do NOT mock it — import it in the test so `instanceof` matches:
```tsx
import { ApiError } from "../api/client";
```

Add tests (after the gates `describe`). `ApiError`'s constructor shape: confirm it is `new ApiError(code, message, ...)` or similar by reading `../api/client`; the test builds one whose `.message` contains "disclosure":

```tsx
import { fireEvent } from "@testing-library/react";

describe("TiptapSelectionToolbar Ask errors", () => {
  beforeEach(() => {
    submitMock.mockReset();
    ackMock.mockReset();
  });

  it("disclosure 403 → inline ack banner → ack + retry re-submits", () => {
    // First submit rejects with a disclosure error via the onError option.
    // ApiError(code: string, message: string); .message is `${code}: ${message}`.
    submitMock.mockImplementationOnce((_vars, opts) => {
      opts.onError(new ApiError("disclosure_required", "hosted-model disclosure not acknowledged"));
    });
    renderToolbar({ settled: true });
    fireEvent.click(screen.getByRole("button", { name: /ask/i }));
    fireEvent.click(screen.getByText(/explain this/i)); // an Ask preset → runTurn("ask", ...)
    expect(screen.getByRole("alert")).toHaveTextContent(/hosted model/i);

    // Clicking ack calls ack.mutate and, on its onSuccess, re-runs the held turn.
    ackMock.mockImplementationOnce((_v, opts) => opts.onSuccess());
    submitMock.mockImplementationOnce((_vars, opts) => opts.onSuccess?.());
    fireEvent.click(screen.getByRole("button", { name: /i understand/i }));
    expect(ackMock).toHaveBeenCalled();
    expect(submitMock).toHaveBeenCalledTimes(2); // original + retry
  });

  it("non-disclosure error → generic alert, no ack banner", () => {
    submitMock.mockImplementationOnce((_vars, opts) => {
      opts.onError(new ApiError("server_error", "boom"));
    });
    renderToolbar({ settled: true });
    fireEvent.click(screen.getByRole("button", { name: /ask/i }));
    fireEvent.click(screen.getByText(/explain this/i));
    expect(screen.getByRole("alert")).toHaveTextContent(/couldn.t do that/i);
    expect(screen.queryByText(/hosted model/i)).toBeNull();
  });
});
```

> `ApiError` (`frontend/src/api/client.ts`) is `constructor(code: string, message: string)` and sets `.message = ` `` `${code}: ${message}` ``; the behaviour under test is `e instanceof ApiError && /disclosure/i.test(e.message)`, which matches when the code OR message contains "disclosure" (hence `"disclosure_required"` above). `useAckDisclosure(id: string)` returns a mutation (`.mutate`, `.isPending`).

- [ ] **Step 2: Run it red**

Run: `cd frontend && pnpm exec vitest run src/collab/TiptapSelectionToolbar.test.tsx -t "Ask errors"`
Expected: FAIL — no ack banner / generic alert rendered (runTurn has no onError yet).

- [ ] **Step 3: Implement disclosure ack/retry + generic error**

In `frontend/src/collab/TiptapSelectionToolbar.tsx`:
1. Extend imports:
```tsx
import { Button, Group, Menu, Paper, Text } from "@mantine/core";
```
```tsx
import { useSubmitTurn, useAckDisclosure } from "../api/turns";
import { ApiError } from "../api/client";
```
2. Add hooks + state with the others (above the early return):
```tsx
  const ack = useAckDisclosure(workspaceId);
  const [needsAck, setNeedsAck] = useState<{ rt: "ask" | "modify"; text: string } | null>(null);
  const [turnError, setTurnError] = useState<string | null>(null);
```
3. Replace `runTurn` body's `submit.mutate(...)` call:
```tsx
  function runTurn(requestType: "ask" | "modify", instruction: string) {
    if (!instruction.trim() || submit.isPending) return;
    const selection_context: SelectionContext = {
      request_type: requestType,
      target_artifact: "draft",
      section_index: claim!.sectionIndex,
      base_commit_sha: head,
      base_section_sha: sectionSha,
      anchor: claim!.anchor,
      from_offset: fromOffset,
      to_offset: toOffset,
    };
    submit.mutate(
      { content: instruction, selection_context },
      {
        onSuccess: () => {
          setNeedsAck(null);
          setTurnError(null);
        },
        onError: (e) => {
          if (e instanceof ApiError && /disclosure/i.test(e.message)) {
            setNeedsAck({ rt: requestType, text: instruction });
          } else {
            setTurnError("Couldn't do that — please try again.");
          }
        },
      },
    );
  }

  function ackAndRetry() {
    const pending = needsAck;
    ack.mutate(undefined, {
      onSuccess: () => {
        setNeedsAck(null);
        if (pending) runTurn(pending.rt, pending.text);
      },
    });
  }
```
4. In the returned JSX, inside the `<Paper …>` after the `</Group>` of the button row, add the banners:
```tsx
        {needsAck && (
          <Group gap="xs" mt={4} wrap="nowrap" role="alert">
            <Text size="xs">
              Answers use a hosted model. Don&apos;t paste patient-identifiable data.
            </Text>
            <Button size="xs" onClick={ackAndRetry} disabled={ack.isPending}>
              I understand — ask
            </Button>
          </Group>
        )}
        {turnError && (
          <Text size="xs" c="red" mt={4} role="alert">
            {turnError}
          </Text>
        )}
```

- [ ] **Step 4: Run it green**

Run: `cd frontend && pnpm exec vitest run src/collab/TiptapSelectionToolbar.test.tsx`
Expected: all pass (gates + reactive-settle + Ask-errors).

- [ ] **Step 5: Typecheck + commit**

```bash
cd frontend && pnpm exec tsc --noEmit
cd /home/parlar_ai/dev/call-monitor
git add frontend/src/collab/TiptapSelectionToolbar.tsx frontend/src/collab/TiptapSelectionToolbar.test.tsx
git commit -m "$(cat <<'EOF'
feat(collab): toolbar disclosure ack/retry + generic error surface (3b-5d.D1a, C#2)

Ask/Edit runTurn had no onError — a member who hasn't acked the hosted-model disclosure
clicked Ask and nothing happened (403 swallowed). Port the CM6 needsAck ack+retry and add
a generic error surface. Cite is unaffected (not an LLM turn; the composer surfaces its own).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Restore the full-Cite two-browser e2e (acceptance gate)

**Files:**
- Modify: `frontend/e2e/collab.spec.ts`

- [ ] **Step 1: Replace scenario (c) with the full live Cite flow**

Replace the D0 latch-symptom test (`select-to-cite: write-actions latch disabled in the live editor (D1 gap)`) with the restored full flow. Keep the `saveVersion` helper (it now waits on the checkpoint POST). New scenario (c):

```typescript
  // (c) Cite-via-room on a fresh section; the citation node converges to BOTH clients.
  //     D1a acceptance gate — proves the settled-latch fix lets the live Cite flow
  //     complete end-to-end. Checkpoint + reload to a git==live base first so the
  //     D1b stale-snapshot 409 (a SEPARATE slice) does not confound this proof.
  test("owner cites a claim in the live editor; the pill converges to the member", async ({
    browser,
    baseURL,
  }) => {
    const draft = `/cockpit/${WS}`;
    const A = await openAs(browser, COOKIE_OWNER!, baseURL!, draft);
    const B = await openAs(browser, COOKIE_MEMBER!, baseURL!, draft);

    const edA = A.page.getByTestId("collab-editor");
    const edB = B.page.getByTestId("collab-editor");
    await expect(edA).toBeVisible({ timeout: 15_000 });
    await expect(edB).toBeVisible({ timeout: 15_000 });

    // git==live base so base_section_sha matches the live room (D1b handles co-edit drift).
    await saveVersion(A.page);
    await A.page.reload();
    await B.page.reload();
    await expect(edA).toBeVisible({ timeout: 15_000 });
    await expect(edB).toBeVisible({ timeout: 15_000 });

    // Select a unique body word → Cite now ENABLES (the D1a fix) once settled.
    await edA.getByText("diagnostic", { exact: false }).first().dblclick();
    const citeBtn = A.page.getByRole("button", { name: "Cite" });
    await expect(citeBtn).toBeEnabled({ timeout: 8_000 });
    await citeBtn.click();

    const composer = A.page.getByRole("complementary", { name: /add a citation/i });
    await expect(composer).toBeVisible({ timeout: 5_000 });
    await composer.getByRole("button", { name: /Seed Reference on Precision Medicine/i }).click();
    const chunk = composer.locator("pre").first();
    await expect(chunk).toBeVisible({ timeout: 5_000 });
    await chunk.click({ clickCount: 3 }); // select the line; fires the mouseup captureSelection needs
    await composer.getByRole("button", { name: /add citation/i }).click();

    // The pill appears for the owner and converges to the member.
    await expect(edA.getByTestId("citation-pill").first()).toBeVisible({ timeout: 10_000 });
    await expect(edB.getByTestId("citation-pill").first()).toBeVisible({ timeout: 10_000 });

    await A.ctx.close();
    await B.ctx.close();
  });
```

- [ ] **Step 2: Parse-check the spec**

Run: `cd frontend && pnpm exec playwright test e2e/collab.spec.ts --list`
Expected: 4 tests listed (scenario (c) now titled "owner cites a claim in the live editor…").

- [ ] **Step 3: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add frontend/e2e/collab.spec.ts
git commit -m "$(cat <<'EOF'
test(collab): restore full-Cite two-browser e2e as the D1a acceptance gate (3b-5d.D1a)

Flip D0 scenario (c) from the latch-symptom assertion back to the full live flow
(select -> Cite -> composer -> pick reference -> add citation -> pill converges to BOTH),
with a checkpoint+reload git==live base so D1b's co-edit 409 stays out of scope.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Acceptance run (vitest + two-browser e2e)

- [ ] **Step 1: Full frontend unit + typecheck**

```bash
cd frontend
pnpm exec tsc --noEmit
pnpm exec vitest run src/collab/
```
Expected: tsc clean; all collab vitest green.

- [ ] **Step 2: Preflight ports, then run the two-browser e2e**

```bash
cd /home/parlar_ai/dev/call-monitor
for p in 8000 5173; do ss -ltn "sport = :$p" 2>/dev/null | grep -q LISTEN && echo ":$p BUSY" || echo ":$p free"; done
scripts/collab_e2e_run.sh --workers=1 --reporter=list
```
Expected: **4 passed**, including the restored scenario (c) — the pill converges to both browsers. The runner's preflight/teardown frees the ports.

- [ ] **Step 3: Decide C#5**

- If scenario (c) **passes** → C#5 did not surface. Skip Task 6. Record in the findings note: "C#5 (composer unmount) not reproduced — the open composer survived the cite flow."
- If scenario (c) **fails specifically because the composer disappears / loses its claim mid-compose** (e.g. the `complementary` composer or the seeded-reference button vanishes after a selection change, or the pill never inserts because `claimCtx` went null) → apply **Task 6**, then re-run Step 2.
- If it fails for another reason (selector drift, timing) → fix that directly (not a C#5 concern) and re-run.

---

## Task 6 (CONDITIONAL — only if Task 5 Step 3 surfaces C#5): Freeze the ClaimContext snapshot

**Files:**
- Modify: `frontend/src/collab/TiptapSelectionToolbar.tsx` (+ a vitest for the snapshot)

**Rationale:** `claimCtx` is derived from the live `claim` each render, and the composer renders inside the `if (!claim) return null` subtree. A selection change mid-compose sets `claim → null` → the toolbar (and the open composer) unmount and the in-progress cite is lost. Fix: freeze a `ClaimContext` snapshot when Cite is clicked, render the composer from the snapshot, and hoist it out of the claim-guarded subtree.

- [ ] **Step 1: Write the failing snapshot test**

Add to `TiptapSelectionToolbar.test.tsx`: open Cite, then drive a `selectionUpdate` that makes `deriveClaimAnchor` return `null`, and assert the composer is still present.

```tsx
  it("C#5: open composer survives the selection emptying (frozen claim snapshot)", () => {
    const derive = vi.mocked((await import("./selectionAnchor")).deriveClaimAnchor);
    // Render with a claim, open Cite, then flip deriveClaimAnchor to null + fire selectionUpdate.
    // (Use the fakeEditor's captured "selectionUpdate" listener to trigger a re-derive.)
    // ... assert screen.getByTestId("citation-composer") is still in the document.
  });
```
> The implementer wires the `selectionUpdate` listener capture (the `fakeEditor` already records listeners) to invoke it after `derive.mockReturnValue(null)`, then asserts the mocked `citation-composer` testid persists.

- [ ] **Step 2: Run it red** — `pnpm exec vitest run src/collab/TiptapSelectionToolbar.test.tsx -t "survives the selection emptying"` → FAIL (composer gone).

- [ ] **Step 3: Implement the snapshot + hoist**

1. Add snapshot state (with the other hooks):
```tsx
  const [citeClaim, setCiteClaim] = useState<ClaimContext | null>(null);
```
(`ClaimContext` is already imported from `../cockpit/CitationComposer`.)
2. The Cite button `onClick` captures the snapshot:
```tsx
              onClick={() => {
                setCiteClaim(claimCtx);
                setCiteOpen(true);
              }}
```
3. Render the composer from the SNAPSHOT, and move it so it does not depend on the live `claim`. Since the whole component returns `null` when `!claim`, wrap the return so the composer renders even then. Simplest: compute an early `composerEl` and return it alongside. Replace the final `return ( <> …toolbar… {citeOpen && <CitationComposer claim={claimCtx} …/>} </> )` with a structure where the composer uses `citeClaim`:
```tsx
  const composerEl =
    citeOpen && citeClaim ? (
      <CitationComposer
        workspaceId={workspaceId}
        claim={citeClaim}
        onDone={() => {
          setCiteOpen(false);
          setCiteClaim(null);
        }}
        onCancel={() => {
          setCiteOpen(false);
          setCiteClaim(null);
        }}
      />
    ) : null;

  if (!claim) return composerEl; // keep an open composer alive even if the selection empties

  // …existing anchorable/settled/writeReady computation…

  return (
    <>
      <Paper …>… {/* no composer here now */} …</Paper>
      {composerEl}
    </>
  );
```
> This hoists `composerEl` above the `if (!claim) return null` guard and returns it in the empty-claim branch, so an open composer survives `claim → null`; it renders from the frozen `citeClaim` snapshot, never the live `claim`.

- [ ] **Step 4: Run green + typecheck + re-run e2e (Task 5 Step 2)**

```bash
cd frontend && pnpm exec vitest run src/collab/TiptapSelectionToolbar.test.tsx && pnpm exec tsc --noEmit
cd /home/parlar_ai/dev/call-monitor && scripts/collab_e2e_run.sh --workers=1 --reporter=list
```
Expected: vitest green; e2e 4 passed (scenario (c) now completes).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/collab/TiptapSelectionToolbar.tsx frontend/src/collab/TiptapSelectionToolbar.test.tsx
git commit -m "$(cat <<'EOF'
fix(collab): freeze ClaimContext snapshot so an open Cite composer survives selection changes (3b-5d.D1a, C#5)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Findings note + STOP (user-gated merge)

- [ ] **Step 1: Append a short D1a outcome to the D0 findings note** (or a new note) recording: the settled-latch fix verified (unit + e2e), disclosure/error ported, the full-Cite two-browser flow now GREEN, and whether C#5 was reproduced (Task 6 applied or not).

- [ ] **Step 2: STOP.** Present the branch diff (`master...HEAD`, `frontend/src/collab/*` + e2e only — no backend/schema) + the e2e result, and await the explicit merge decision. On "please merge": `git checkout master && git merge --no-ff collab-slice3b5d1a` (local only), then record D1a to memory.

---

## Self-Review

**Spec coverage:** §3.1 settled-latch → Tasks 1–2 (shared hook + reactive toolbar + regression test). §3.2 disclosure/error → Task 3. §3.3 restore e2e + reactive C#5 → Tasks 4–6. §4 tests → Tasks 1/2/3 vitest (controllable stub per Codex Low) + Task 5 e2e. §5 acceptance → Task 5. ✅

**Placeholder scan:** none — every code step has concrete content. Two implementer NOTEs (confirm `ApiError` constructor shape; wire the `selectionUpdate` listener for the C#5 test) are deliberate: they point at a small fact to verify against real code, not a gap in the plan.

**Type/name consistency:** `useSettled(provider): boolean` created in Task 1, consumed in Task 2 (toolbar) + Task 1 Step 5 (SaveVersionButton). `ClaimContext` (from `../cockpit/CitationComposer`) reused for the Task 6 snapshot. `needsAck`/`turnError`/`citeClaim` state names consistent across Tasks 3/6. The `saveVersion` helper (from D0) is reused unchanged in Task 4. Selectors match D0-verified ones (`collab-editor`, `citation-pill`, `complementary`/"add a citation", "Cite", "Seed Reference on Precision Medicine").

**Ordering:** Task 6 is explicitly conditional on Task 5 Step 3 — the reactive C#5 call the spec requires. The git==live base-reset in Task 4 keeps D1b's 409 out of scope.
