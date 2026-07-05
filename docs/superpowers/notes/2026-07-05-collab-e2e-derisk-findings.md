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
| (c) | Cite-via-room on a fresh section | **FAIL → known D1 gap** (not a GO gate) | The `Cite`/`Edit` write-actions never enable. Root-caused below. Encoded as a Playwright `test.fail` so the harness stays green and flips to an unexpected-pass the moment D1 fixes it. |
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
| Cite/Edit write-actions latch disabled (toolbar `settled` sampled at render vs the awareness-send that dirties `bufferedAmount`) | **D1** | High | New, root-caused above. Encoded as `test.fail` in `collab.spec.ts` (c). Makes citation-insert unusable live today. |
| Cite-after-co-edit stale-snapshot **409** ("save and reselect") | **D1** | (expected) | The already-known 3b-5a C#3 deferral. NOT re-exercised automatically — blocked upstream by the latch above (can't reach the composer to even attempt a cite). Re-validate once the latch is fixed. |
| Relay-spoof (active high-clock awareness spoof to hide/fake a peer's caret) | **D2** | (expected) | Not benignly reproducible from a browser; server-side payload-filter still required per 3b-5b. Unchanged by D0. |
| Convergence occasionally exceeds the 8 s assertion window under full-stack single-box contention | D4 harness | Low | Timing, not transport — (a) passed 4/5 and 3/3 isolated (~3 s). For D4's final re-validation, use generous timeouts and/or per-scenario runs; consider `--retries` for the flip gate. |

## Notes for D4 (final re-validation)

- The harness (`collab_e2e_seed.py` 2-user + `collab_e2e_run.sh`) is reusable as-is for D4's post-flip re-validation.
- D4 must additionally cover **prod serving mode** (SPA_MODE=prod, `E2E_MODE=prod`): the prod CSP is `connect-src 'self'` with **no `ws:`/`wss:` token**, so the Yjs socket would be blocked — the narrow same-origin `wss:` CSP allowance is a D4 deliverable and must be re-validated with this harness in prod mode.
- Once D1 lands, flip scenario (c) from `test.fail` back to a normal assertion (it will already be flagging as an unexpected pass).
