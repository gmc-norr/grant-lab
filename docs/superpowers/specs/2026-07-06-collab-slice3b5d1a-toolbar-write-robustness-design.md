# Collab Slice 3b-5d·D1a — Selection-Toolbar Write-Action Robustness (Design)

**Date:** 2026-07-06
**Status:** Design — pending user review
**Parent:** 3b-5d (the flip), decomposed D0 (e2e de-risk, merged `d024ea9`) → **D1a (this)** → D1b (stale-snapshot 409 concurrency) → D2 (relay-spoof filter) → D3 (analysis re-fire) → D4 (the flip).
**Feature flag:** collaborative editing STAYS OFF in prod. D1a is client-only and inert while the flag is off (the collab toolbar does not render in prod). No `enable_collab` source caller is added.

---

## 1. Context & Goal

D0 (the two-browser de-risk) root-caused a browser-only defect that makes citation-insert **unusable** in the live collab editor: the `TiptapSelectionToolbar` computes `settled = provider.isSettled()` **only during its `selectionUpdate`-triggered render**, but the same selection fires a `CollaborationCaret` awareness broadcast → `provider.send()` → `ws.bufferedAmount > 0` at that instant → `isSettled()` returns false → the write-actions (`Edit`, `Cite`) latch **disabled**, with no re-render when the buffer drains a tick later. `SaveVersionButton` is immune because it *polls* `isSettled()` every 200ms ([SaveVersionButton.tsx:17-21](../../../frontend/src/collab/SaveVersionButton.tsx#L17)); the toolbar does not. vitest missed it because those tests mock `isSettled()=true`.

D1 was split by risk (user decision): **D1a** = the cheap **client-only** toolbar fixes that unblock and harden the live Cite/Edit actions; **D1b** = the deeper server+client concurrency change for the cite/edit-after-co-edit 409 (3b-5a C#3). This spec is D1a.

**D1a goal:** make the live Cite/Edit selection actions actually work — the write-actions re-enable once the connection settles, Ask/Edit surface disclosure and generic errors instead of failing silently, and the **full live Cite flow is proven end-to-end** by restoring D0's two-browser scenario from a disabled-symptom assertion to the complete select→compose→insert→converge flow.

## 2. Global Constraints

- Feature flag STAYS OFF in prod. No `enable_collab` source caller. Client-only.
- **No backend, no endpoint, no DB migration, no schema change.** The only source edits are under `frontend/src/collab/` plus the D0 e2e spec (`frontend/e2e/collab.spec.ts`).
- **Do not regress `SaveVersionButton`.** Its existing poll behaviour must be preserved byte-for-behaviour when refactored onto the shared hook.
- Frontend gate is `tsc` (there is no TS linter) + `vitest`. The e2e stays opt-in / skip-gated on the D0 seed vars, run via `scripts/collab_e2e_run.sh`.
- pnpm fresh adds (none expected here) need `--config.minimumReleaseAge=0`.

## 3. Design

### 3.1 `useSettled` — reactive settle state (fixes the latch)

`provider.isSettled()` ([collabProvider.ts:82](../../../frontend/src/collab/collabProvider.ts#L82)) already includes the `status === "connected"` term, so the toolbar's `provider.status === "connected" && provider.isSettled()` is redundant — the whole predicate is `isSettled()`.

New hook `frontend/src/collab/useSettled.ts`:

```ts
export function useSettled(provider: CollabProvider): boolean {
  const [settled, setSettled] = useState(() => provider.isSettled());
  useEffect(() => {
    setSettled(provider.isSettled());
    const t = setInterval(() => setSettled(provider.isSettled()), 200);
    return () => clearInterval(t);
  }, [provider]);
  return settled;
}
```

- **`SaveVersionButton`** is refactored to `const settled = useSettled(props.provider);`, deleting its inline `useState`/`useEffect` poll (identical behaviour, DRY).
- **`TiptapSelectionToolbar`** replaces `const settled = provider.status === "connected" && provider.isSettled();` (computed at render, after the `if (!claim) return null` guard) with `const settled = useSettled(provider);` called **at the top with the other hooks** (before the early return, to satisfy the Rules of Hooks). `writeReady = settled && anchorable && !claim.headingOnly` is otherwise unchanged. Result: after a selection's awareness send dirties the buffer, the 200ms poll re-enables `Edit`/`Cite` as soon as the buffer drains, instead of latching.

### 3.2 Disclosure ack+retry + generic error (C#2)

`runTurn` (Ask/Edit) currently calls `submit.mutate({ content, selection_context })` fire-and-forget — a member who hasn't acked the hosted-model disclosure clicks Ask → 403 → nothing happens. Port the CM6 `SelectionToolbar` pattern ([SelectionToolbar.tsx:52-77,128-133](../../../frontend/src/cockpit/SelectionToolbar.tsx#L52)) into the Tiptap toolbar:

- Add `useAckDisclosure(workspaceId)` (from `../api/turns`) and import `ApiError` (from `../api/client`).
- Add state `needsAck: { rt: "ask" | "modify"; text: string } | null` and `turnError: string | null`.
- `runTurn` passes `onSuccess` (clear `needsAck`/`turnError`) and `onError`:
  - `e instanceof ApiError && /disclosure/i.test(e.message)` → `setNeedsAck({ rt, text: instruction })`.
  - otherwise → `setTurnError("Couldn't do that — please try again.")`.
- Render (inside the toolbar `Paper`, below the button row):
  - `needsAck` → a `role="alert"` banner: "Answers use a hosted model. Don't paste patient-identifiable data." + an "I understand — ask" button → `ack.mutate` → on success clear `needsAck` and re-`runTurn(needsAck.rt, needsAck.text)`.
  - `turnError` → a `role="alert"` generic message.
- **Cite is out of scope for this** — a direct citation is not an LLM turn (no disclosure), and `CitationComposer` already surfaces its own add-citation errors ([CitationComposer.tsx:162](../../../frontend/src/cockpit/CitationComposer.tsx#L162)).

### 3.3 Restore the full Cite e2e (the acceptance gate) + reactive C#5

Flip D0 `collab.spec.ts` scenario (c) from the current disabled-symptom assertion (`Ask` enabled / `Cite` disabled) back to the **full live flow**:

1. Open owner (A) + member (B) on the draft; both editors visible.
2. `saveVersion(A.page)` + reload both → git==live base (avoids the D1b 409, which is a *separate* slice — D1a must keep the base consistent so the 409 doesn't confound the settled-latch proof).
3. Select a unique body word → **`Cite` now enables** (the fix) → click it.
4. `CitationComposer` opens → pick the seeded reference → triple-click the chunk `<pre>` → **Add citation**.
5. Assert the `citation-pill` appears in **both** A and B.

This restored two-browser run is D1a's acceptance gate. **If it surfaces C#5** — the `CitationComposer` is rendered *inside* `TiptapSelectionToolbar`, which returns `null` when `claim` empties, so a selection change mid-compose would unmount the open composer and lose the in-progress cite — then fixing it is **in-scope**, and the fix must **freeze the claim, not just relocate the composer.** `claimCtx` is derived from the live `claim` on every render ([TiptapSelectionToolbar.tsx:81-86](../../../frontend/src/collab/TiptapSelectionToolbar.tsx#L81)); merely lifting the composer out of the `if (!claim) return null` subtree still breaks when `claim → null`, because there is no live `claim` left to build `claimCtx` from. So: on the `Cite` click, capture a **snapshot** `ClaimContext` into state (e.g. `setCiteClaim(claimCtx)` alongside `setCiteOpen(true)`), render `CitationComposer` from that stored snapshot (not the live `claim`), and hoist that render out of the claim-guarded subtree so it survives `claim → null`. If the e2e passes without surfacing C#5, leave it untouched (YAGNI); a follow-up note records it as not-reproduced.

## 4. Testing

- **vitest** (`TiptapSelectionToolbar.test.tsx`, and a small `useSettled.test.ts`):
  - **Settle latch (the regression guard the old tests couldn't catch):** use a **controllable** provider stub — a mutable flag, `let settledVal = false; isSettled: () => settledVal` — NOT a fixed call-sequence. `useSettled` calls `isSettled()` in the `useState` initializer AND again immediately in the effect (before the interval), so a naive "false then true" stub would flip the button enabled before any timer advance and prove nothing. With the mutable flag: assert `Cite`/`Edit` disabled while `settledVal===false` (both eager calls see false), then set `settledVal = true` and `act(() => vi.advanceTimersByTime(200))` and assert they **enable on the poll tick**. The existing tests that mock `isSettled()=true` stay, but this new one pins the reactive behaviour.
  - **Disclosure:** `submit` rejects with an `ApiError` message containing "disclosure" → the ack banner appears; clicking it calls `ack` then re-submits the held instruction.
  - **Generic error:** `submit` rejects with a non-disclosure error → the generic `role="alert"` appears; no ack banner.
  - `SaveVersionButton.test.tsx` (if present) stays green after the `useSettled` refactor.
- **Two-browser e2e** (`scripts/collab_e2e_run.sh`): the restored scenario (c) is the real proof; the other three D0 scenarios must stay green.

## 5. Acceptance (GO for D1b)

- The restored full-Cite e2e passes with two distinct users (pill converges to both).
- All four e2e scenarios green; `tsc` + `vitest` green.
- Ask/Edit surface disclosure (ack+retry) and generic errors.
- `SaveVersionButton` unchanged in behaviour.

## 6. Risks & Rejected Alternatives

- **`useSettled` refactor regresses `SaveVersionButton`:** mitigated by keeping the poll identical (200ms, `isSettled()`), an initial synchronous sample, and its existing test staying green.
- **Frequent awareness sends keep `bufferedAmount>0`:** awareness is sent on *state change* (cursor/selection moves), not continuously, so after the user stops moving the buffer drains within a poll tick — the poll converges. (A deeper fix — excluding awareness bytes from the settle check, or tracking pending *doc* updates separately — is **rejected** for D1a: it changes provider semantics shared with `SaveVersionButton` for no observed benefit.)
- **C#5 speculative refactor:** rejected as proactive work — driven reactively off the restored e2e (fix only if it actually unmounts).
- **The 409 (C#3/D1b) confounds the D1a e2e:** avoided by checkpoint+reload to a git==live base in step (c).2 before citing; the cite-after-co-edit 409 is explicitly D1b.

## 7. Non-Goals

No stale-snapshot 409 fix (that is D1b — cite/edit after a *co-edit*), no relay-spoof filter (D2), no analysis re-fire (D3), no flip / prod CSP (D4). No `why-disabled` hint (C#6) — deferred polish. No backend/schema change. No new blocking CI gate (the e2e stays opt-in).

## 8. Deliverables Summary

| Item | Kind | Files |
|------|------|-------|
| Reactive settle hook | new | `frontend/src/collab/useSettled.ts` (+ `useSettled.test.ts`) |
| SaveVersionButton on shared hook | refactor | `frontend/src/collab/SaveVersionButton.tsx` |
| Settled-latch fix + disclosure ack/retry + generic error | modify | `frontend/src/collab/TiptapSelectionToolbar.tsx` (+ `.test.tsx`) |
| Restore full-Cite two-browser gate (+ reactive C#5 fix if surfaced) | modify | `frontend/e2e/collab.spec.ts` (+ toolbar/composer if C#5) |
