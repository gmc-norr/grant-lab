# Collab Slice 3b-5d·D0 — Two-Browser E2E De-Risk (Design)

**Date:** 2026-07-05
**Status:** Design — pending user review
**Parent:** 3b-5d (the flip), decomposed into D0 (this) → D1 toolbar/stale-snapshot → D2 relay-spoof filter → D3 analysis re-fire → D4 the flip.
**Feature flag:** collaborative editing STAYS OFF in prod. D0 changes nothing in prod — it enables collab only for an ephemeral testcontainers workspace and drives real browsers against a locally-run dev server.

---

## 1. Context & Goal

The collaborative-editing stack (server-authoritative Yjs log + Tiptap + custom WS provider + presence + citation-via-room + periodic/eviction checkpoints) has extensive unit/integration coverage and a Node-ws handshake de-risk (3b-3), but has **never been run with two real browsers**. `frontend/e2e/collab.spec.ts` already contains two authored-but-`test.skip`-gated two-browser tests (convergence+checkpoint+persist; remote-caret presence), and `scripts/collab_e2e_seed.py` boots a testcontainers PG + migrates + seeds one collab-enabled workspace + starts uvicorn (SPA_MODE=dev). Both are **deferred / never executed**, and both use a **single owner in two tabs**, not distinct users.

**D0 goal:** produce the first *real* two-browser signal — a **GO/NO-GO verdict + a punch-list** — by executing this harness (extended to distinct users and the core scenarios) before investing in D1–D3 and the flip. This de-risks the single largest unvalidated unknown and may reshape the downstream hardening.

## 2. Global Constraints

- Feature flag STAYS OFF in prod; add no `enable_collab` source caller. The seed enables collab only on the ephemeral testcontainers workspace.
- No DB migration, no schema change. The e2e suite stays **opt-in / skip-gated** (`test.skip` when `E2E_COLLAB_WS_ID` unset) so the default CI Playwright run stays green.
- Runs in **dev serving mode** (Vite :5173 proxying uvicorn :8000, SPA_MODE=dev). The dev CSP already emits `connect-src 'self' ws: http://localhost:5173`, so the Yjs WS is allowed. **Do NOT touch the prod CSP** here (that is D4).
- No prod-facing code change: the only source edits are the e2e seed script, the runner, and the e2e spec (test-harness surface). No app/backend logic changes.
- Python 3.12 / `uv`; frontend `pnpm` + `@playwright/test` (already deps). Chromium is cached at `~/.cache/ms-playwright`.

## 3. Harness (reuse + extend)

### 3.1 Seed — three users, one workspace (`scripts/collab_e2e_seed.py`)

Extend the existing seed from one owner to **three users on one collab-enabled workspace**:
- **owner** (writer), **member** (writer — added via the existing collaborator/membership model), **viewer** (read-only — `WorkspaceViewer`).
- Print a signed session cookie per user plus the workspace id, e.g. `E2E_COLLAB_WS_ID`, `E2E_COOKIE_OWNER`, `E2E_COOKIE_MEMBER`, `E2E_COOKIE_VIEWER`. Keep the existing single-owner env names (`E2E_SESSION_COOKIE` = owner) for back-compat with the un-extended tests.
- For the Cite scenario (§3.3c) the seed also creates **one reference with a citeable chunk** in the workspace library, so a select→Cite flow has a source to cite. (Highest-setup-cost item — if reference seeding proves heavy, Cite degrades to "record the gap" per §4.)
- Everything else (testcontainers PG, Alembic upgrade, `_init_repo`, uvicorn:8000, dev CSP) is unchanged.

### 3.2 Runner — one command (`scripts/collab_e2e_run.sh`)

A single orchestration script (reusable for D4's final re-validation):
1. Start the seed in the background; parse the printed env vars (ws id + cookies).
2. Start Vite dev (`pnpm dev`, :5173) in the background.
3. Poll until both `:8000` (uvicorn) and `:5173` (Vite) answer.
4. Run `pnpm exec playwright test e2e/collab.spec.ts` with the env vars exported.
5. Tear both down on exit (trap), and the testcontainers PG stops with the seed.

### 3.3 Spec — distinct users + core scenarios (`frontend/e2e/collab.spec.ts`)

Switch the two existing tests to **distinct-user cookies** (owner vs member — real two-user presence, distinct caret names) and add:
- **(a) Convergence + presence, two distinct writers:** A (owner) types → B (member) converges within the timeout; each sees the *other's distinct name* on the remote caret (not one shared owner name). Retains the existing checkpoint (Save-a-version) + reload-persist assertions.
- **(b) Reconnect / re-sync:** drop B's socket (Playwright `context.setOffline(true)` then `false`, or a provider close), assert B reconnects, re-syncs the latest doc, and its caret reappears for A (presence self-heal, ≤ ~15s per 3b-4/3b-5b).
- **(c) Cite-via-room (fresh section):** on a freshly-loaded section (no prior co-edit), A selects a claim → the selection toolbar's **Cite** → CitationComposer → pick the seeded reference/quote → the `citation` node appears in **both** A and B. Then, as a *documented* check, cite again *after* a co-edit changed that section and observe the **stale-snapshot 409** ("save and reselect") — recording the known 3b-5a C#3 blocker for D1 (expected, not a failure).
- **(d) Read-only viewer:** V (viewer) loads the same draft — its `collab-editor` is present and shows A's live edits + A/B carets, but the editor is **non-editable** (`contenteditable=false`; typing changes nothing). Confirms the ungated read path + the `editable: canWrite` gate.

## 4. The Run + Findings

Attempt the live run in this environment via the runner. Capture, per scenario, **PASS / FAIL / BLOCKED**, into a findings doc `docs/superpowers/notes/2026-07-05-collab-e2e-derisk-findings.md` containing:
- The **GO/NO-GO verdict** (criteria in §5).
- A **punch-list**: any *new* defect (a real failure), plus the *expected* known deferrals it confirmed (stale-snapshot 409 → D1; relay-spoof not benignly reproducible → D2) — each tagged with its target sub-slice.
- If the sandbox blocks the run (port conflicts, missing browser OS libs needing `sudo playwright install --with-deps`, resource limits): the doc records the blocker, the exact manual run recipe, and whatever partial signal was obtained (e.g. seed + servers up, or a single scenario run).

## 5. GO/NO-GO Criteria

- **GO** — the core transport all passes: two-distinct-user convergence, distinct-name presence, checkpoint + reload-persistence, reconnect/re-sync, and the read-only viewer. The only gaps are the *expected* deferrals (Cite-after-co-edit 409 → D1; relay-spoof → D2). ⇒ proceed to D1.
- **NO-GO** — any **core-transport** break (text doesn't sync, presence absent/wrong-identity, checkpoint loses content, reconnect fails to re-sync, or the read-only gate leaks write access). ⇒ stop and reshape before D1; the punch-list defines the fix.
- **Cite fresh-section** is a strong signal but not a GO gate (its 409-after-co-edit variant is a known D1 item); a fresh-section Cite failure that is *not* the 409 is a punch-list defect.

## 6. Execution Model

**Inline** in the working session (not a detached SDD build): the code surface is small (seed + runner + spec) and the value is the *interactive* orchestration + observation of a live run, which a headless autonomous agent cannot report on well. The harness edits are made + committed inline; the run is performed inline; the findings doc is written + committed inline.

## 7. Risks & Rejected Alternatives

- **Sandbox can't run the full stack** (2 Chromium + uvicorn + Vite + PG): mitigated by the documented-recipe fallback (§4) and by degrading to per-scenario partial runs. The de-risk value is preserved as "how far did it get + exact repro."
- **Cite reference-seeding cost:** if seeding a citeable reference is disproportionate, Cite degrades to a recorded gap (D1/D4 re-validate it after the stale-snapshot fix). Not a GO gate.
- **Prod-CSP wss** deliberately **out of scope** (dev mode only) — it is a D4 concern; noted so D0 does not conflate a dev-mode PASS with prod readiness.
- **Rejected:** authoring a brand-new e2e from scratch — the harness already exists; D0 extends and *runs* it. Rejected: a detached SDD build — the deliverable is an interactive run, not autonomous code.

## 8. Non-Goals

No prod flip, no `enable_collab` caller, no prod CSP change, no D1–D3 fixes (those are separate slices; D0 only *observes/records* them). No new blocking CI gate (the e2e stays opt-in/skip-gated).

## 9. Deliverables Summary

| Item | Kind | Files |
|------|------|-------|
| 3-user + reference seed | script | `scripts/collab_e2e_seed.py` |
| One-command runner | script | `scripts/collab_e2e_run.sh` |
| Distinct-user + core-scenario e2e | test | `frontend/e2e/collab.spec.ts` |
| GO/NO-GO + punch-list | doc | `docs/superpowers/notes/2026-07-05-collab-e2e-derisk-findings.md` |
