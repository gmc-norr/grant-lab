# Collab Slice 3b-5d·D0 — Two-Browser E2E De-Risk Findings

**Date:** 2026-07-05
**Slice:** 3b-5d·D0 (first sub-slice of the flip). Parent plan: `docs/superpowers/plans/2026-07-05-collab-slice3b5d0-e2e-derisk.md`.
**Verdict:** **GO** — proceed to D1. Core transport is proven with two real browsers as distinct users; the one failure is the *expected* D1 item (Cite write-action gate), now precisely root-caused.

---

## Environment (what actually ran)

Everything ran live on this box — no blocker, no manual-recipe fallback needed:
- `scripts/collab_e2e_run.sh` booted the extended seed (testcontainers PG `pgvector/pgvector:pg16` + Alembic to head 0060 + 2-user seed) + uvicorn:8000 (SPA_MODE=dev) + Vite:5173, and drove **two real Chromium contexts** (cached `chromium-1223`; OS libs present — headless launch smoke passed).
- Serial (`--workers=1`); the four tests share one server-side room per run.
- Seed vars flowed correctly end-to-end (owner + member cookies + labels), confirming the Task-1 `sys.stdout.flush()` + `uv run python -u` fix.

## Per-scenario results

| # | Scenario | Result | Evidence |
|---|----------|--------|----------|
| (a) | Converge (both directions) + distinct-name presence + checkpoint + reload-persist | **PASS** (GO gate) | Owner→member and member→owner text converge; B sees the owner's caret label, A sees the *member's* distinct label (not one shared name); Save-a-version re-enables; reload keeps the text. Passed 4/5 runs; 3/3 in isolation (~3.1–3.3 s). |
| (b) | Reconnect / re-sync after dropped socket | **PASS** (GO gate) | `context.setOffline(true)` → owner edits offline → `setOffline(false)` → member re-syncs the offline edit and its caret reappears for the owner. ~1.6 s. |
| (c) | Select-to-cite in the live editor | **Documents known D1 gap** (not a GO gate) | The `Cite`/`Edit` write-actions never enable. Root-caused below. The test asserts the SPECIFIC symptom — `Ask` enabled (anchoring works) but `Cite` disabled (the settle latch) — so it passes today, unrelated regressions still fail loudly, and it flips the moment D1 fixes the race. |
| (d) | Read-only member on the LOCKED SPEC | **PASS** (strong signal) | Member's editor is `contenteditable=false`; member sees the owner's live spec edits + caret; the member's own keystrokes do **not** mutate the locked spec (no write leak). ~1.6 s. |

Net: **3 passed + 1 expected-fail (green)** on the clean run; the GO gate (a, b) is fully green.

## Root-caused finding → D1 (Cite/Edit write-action latch)

**Symptom:** in the live editor, `Cite` (and `Edit`) stay **disabled** even though the connection is healthy and the selection is valid, so the citation composer never opens.

**Root cause (verified live with temporary in-page probes, since reverted):**
- `TiptapSelectionToolbar` computes `writeReady = settled && anchorable && !claim.headingOnly`, where `settled = provider.status === "connected" && provider.isSettled()`.
- `settled` is read **only during the toolbar's render**, and the toolbar re-renders **only on `editor.on("selectionUpdate")`**.
- `provider.isSettled()` (`collabProvider.ts:82`) requires `ws.bufferedAmount === 0` among its terms. But the **same** selection change that triggers the render also makes `CollaborationCaret` broadcast an awareness update → `provider.send()` → `ws.bufferedAmount > 0` **at that exact instant**.
- So the render samples `isSettled() === false`; `writeReady` latches false; the buffer drains microseconds later but **nothing re-renders the toolbar**, so `Cite`/`Edit` stay disabled until… the next selection change, which dirties the buffer again. Net: the write-actions are effectively never enabled after a selection.

**Proof (temporary probes, reverted):**
- `askDisabled=false` (Ask enabled ⇒ `anchorable=true`), `status=connected`, PM selection was a clean body word (`pmText="outcomes"`, node = `paragraph` ⇒ `headingOnly=false`), and `provider.isSettled()` sampled **true** a tick later — yet `Cite` stayed disabled across 8 s and across a forced re-select. Every non-`settled` explanation is eliminated except the render-vs-awareness-send race on `bufferedAmount`.

**Why unit tests missed it:** the vitest `TiptapSelectionToolbar` tests mock `provider.isSettled()` to return `true`, so the render-timing race is invisible off-browser. This is exactly the class of defect the two-browser de-risk exists to surface.

**Suggested D1 fix direction (not implemented here):** make the toolbar re-evaluate `settled` on a provider status/drain event (subscribe + re-render, or a short interval) instead of sampling only on `selectionUpdate`; and/or make `isSettled()` ignore the awareness frame it just enqueued (awareness is presence, not a persisted edit, so it should not gate a write). This sits alongside the already-planned D1 items (3b-5a C#3 stale-snapshot 409; 3b-5a C#2 disclosure-ack/silent-failure). It arguably **raises D1's priority**: as-is, citation-insert is unusable in the live collab editor.

## Punch-list

| Item | Target | Severity | Notes |
|------|--------|----------|-------|
| Cite/Edit write-actions latch disabled (toolbar `settled` sampled at render vs the awareness-send that dirties `bufferedAmount`) | **D1** | High | New, root-caused above. Asserted as the current symptom in `collab.spec.ts` (c) (Ask enabled / Cite disabled). Makes citation-insert unusable live today. |
| Cite-after-co-edit stale-snapshot **409** ("save and reselect") | **D1** | (expected) | The already-known 3b-5a C#3 deferral. NOT re-exercised automatically — blocked upstream by the latch above (can't reach the composer to even attempt a cite). Re-validate once the latch is fixed. |
| Relay-spoof (active high-clock awareness spoof to hide/fake a peer's caret) | **D2** | (expected) | Not benignly reproducible from a browser; server-side payload-filter still required per 3b-5b. Unchanged by D0. |
| Convergence occasionally exceeds the 8 s assertion window under full-stack single-box contention | D4 harness | Low | Timing, not transport — (a) passed 4/5 and 3/3 isolated (~3 s). For D4's final re-validation, use generous timeouts and/or per-scenario runs; consider `--retries` for the flip gate. |

## Notes for D4 (final re-validation)

- The harness (`collab_e2e_seed.py` 2-user + `collab_e2e_run.sh`) is reusable as-is for D4's post-flip re-validation.
- D4 must additionally cover **prod serving mode** (SPA_MODE=prod, `E2E_MODE=prod`): the prod CSP is `connect-src 'self'` with **no `ws:`/`wss:` token**, so the Yjs socket would be blocked — the narrow same-origin `wss:` CSP allowance is a D4 deliverable and must be re-validated with this harness in prod mode.
- Once D1 lands, scenario (c)'s `Cite` `toBeDisabled` assertion will start failing (Cite now enables) — that is the cue to extend (c) back to the full cite flow (checkpoint + reload to a git==live base, then Cite → composer → pick reference → add citation → assert the pill converges to both clients). The seed already provisions the citeable reference.

---

## D1a outcome (2026-07-06) — settled-latch fixed; full-Cite flow now GREEN

Slice 3b-5d·D1a (client-only, flag OFF) closed the top D0 finding. Branch `collab-slice3b5d1a`.

- **Settled-latch FIXED.** Extracted `SaveVersionButton`'s 200 ms `isSettled()` poll into a shared `useSettled` hook and used it in `TiptapSelectionToolbar`, so `Edit`/`Cite` re-enable when the WS buffer drains (no re-select). vitest regression test uses a controllable `isSettled()` stub + fake timers (the old tests mocked `isSettled()=true` and couldn't catch it).
- **Disclosure ack/retry + generic error (C#2).** Ported the CM6 pattern; a disclosure 403 now shows an inline ack banner + retry, other errors a generic alert. The retry re-submits the **frozen** turn payload (content + `selection_context` captured at submit time), so a selection change between the 403 and the ack can't retarget it (regression-tested).
- **Full-Cite two-browser e2e RESTORED and GREEN.** D0 scenario (c) is back to the complete flow (select → Cite → composer → pick reference → select chunk passage → Add citation → the `citation-pill` converges to **both** owner and member). **All 4 e2e scenarios pass** (~10 s); teardown leaves ports free + 0 PG containers.
- **C#5 did NOT surface** — the `CitationComposer` survived the entire cite flow, so no snapshot/hoist refactor was needed (YAGNI; the conditional Task 6 was skipped). If a future selection-clearing interaction breaks it, the fix is the frozen-`ClaimContext` snapshot described in the D1a plan §Task 6.
- **e2e passage selection:** synthetic triple-click/drag did not register a chunk selection headless; the working approach is `locator.selectText()` + a dispatched `mouseup` (mirrors the composer's unit test).

Remaining before the flip: **D1b** (stale-snapshot 409 — cite/edit *after a co-edit*, the `base_section_sha`-vs-live-room concurrency change), then D2 (relay-spoof filter), D3 (analysis re-fire), D4 (the flip + prod `wss:` CSP).

---

## D1b outcome (2026-07-07) — stale-snapshot 409 fixed; co-edit cite/edit now GREEN

Slice 3b-5d·D1b (backend-only, flag OFF) closed the deferred 3b-5a **C#3**. Branch `collab-slice3b5d1b`. Root cause was sharper than "co-edit drift": the toolbar baselines `base_section_sha` on **git HEAD** (`useDraft`), but the live Yjs room is almost always ahead of git (git only advances on checkpoint), so the two toolbar-input gates 409'd on *any* unsaved live edit — **including the writer's own**.

- **Two backend gates → anchor-relocation against the live section.** (1) `apply_citation_to_room` gained `enforce_section_sha=True`; `direct_cite_via_kernel` passes `False`, so the direct Cite rests on the guards it already had (anchor present-in-section + whole-doc ambiguity→422 / ref→404 / anchor-moved→409). The inbox pc-accept path (`kernel_accept_citation`) keeps `True` → terminal-stale parity unchanged. (2) `build_selection_block` gained `allow_anchor_relocation=False`; the collab-draft branch of `turns.py` passes `True`, so an Edit `modify` re-locates the anchor in the LIVE section (via the shared marker-aware `anchor_occurrences`) instead of rejecting on the stale git sha.
- **Accept-time gate untouched.** `kernel_accept_prose` is already live-vs-live (the agent reads `canonical_artifact_markdown`), and the inbox `refresh_staleness` is already room-canonical — both are genuine conflict detection and stay.
- **Flag-off byte-for-byte.** Both new params default to the old behavior; the only behavior change is on the flag-gated collab path. No schema, no migration.
- **Tests.** Unit (`build_selection_block`: unique-short-span verify + gone/ambiguous 409 + ask leniency + flag-off parity); integration (direct cite: stale-sha-allowed regression + gone-anchor clean-stale-no-evict re-point); a **route wiring test** that drives a live room *genuinely ahead of git* (anchor absent from git HEAD, present in the live log) → 200 with the recomputed live offset, pinning both the `turns.py` wire and the live baseline.
- **e2e scenario (e) — cite after a co-edit with NO save-first (the D1b acceptance gate).** Both users edit the Aims section; the owner reloads (member stays connected → no last-leave checkpoint, so git stays behind), selects a clean uncited word via a precise DOM Range (a center-dblclick on the long co-edited paragraph lands on the existing pill — the reliable path is a `Range` + `mouseup`), cites it without saving, and the `citation-pill` converges to both clients. **All 5 e2e scenarios pass** (~13 s); teardown leaves ports free + 0 PG containers.

**Approach-3 FE polish deferred** (toolbar enable-gate still uses git-section uniqueness → a rare live-ambiguous-but-git-unique anchor can enable a button the backend then 422/409s; fails safe). Remaining before the flip: **D2** (relay-spoof filter), **D3** (analysis re-fire), **D4** (the flip + prod `wss:` CSP).
