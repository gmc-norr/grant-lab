# Jinja Workspace Teardown — G2: Delete the Orphaned Jinja UI (Design Spec)

**Date:** 2026-06-25
**Status:** Design approved; pending spec-review gate → writing-plans.
**Project:** Full-React migration → retire the legacy Jinja `/workspaces/{id}` workspace UI.
**This is G2 of a two-slice teardown.** G1 = **sever** (merged `beff5de` — additive + relocation,
nothing deleted). G2 (this) = **delete** the now-orphaned Jinja UI.
**Builds on:** G1 merged at master `beff5de`.

---

## 1. Goal & invariant

The React cockpit (Slices A–F) fully replaced the Jinja `/workspaces/{id}` two-pane UI, and
G1 severed every dependency the React app and kept `/api` backend had on the doomed Jinja
modules. G2 removes the now-dead code: the six Jinja modules, their templates, their tests,
their router registrations, the `base.html` nav link, and the now-unreachable
citation-rendering chain in `citations/reader.py`.

**Invariant after G2:**
1. No FastAPI route resolves under the Jinja `/workspaces/…` HTML surface (the six routers are
   gone). The React SPA (`/cockpit`) and the JSON `/api/workspaces/…` surface are unaffected.
2. The kept surfaces — the 7 JSON modules, all `web/api/*`, the email digest, and the still-Jinja
   profile/admin/auth/calls pages — behave exactly as before.
3. `import call_manager.main` succeeds and the app boots with the six routers unregistered.

No DB schema change.

---

## 2. Audit result (reachability — done; SAFE TO DELETE)

A fresh reachability audit (two Explore agents + greps, cross-checked against G1's
`tests/test_jinja_severance.py`) confirmed **no live code depends on the six doomed modules**.
Their only importers are `main.py` (the six router imports + six `include_router` calls) and
their own dedicated test files; the six also import each other (intra-doomed). Zero imports
from the 7 JSON modules, `web/api/*`, the digest, the scheduler, or any service.
`render_proposal_with_citations` (the Jinja proposal renderer) is called **only** from
`workspaces.py`/`sections.py`; kept `web/api` code uses `reader.py`'s low-level helpers but
never that entry point.

---

## 3. Deletion manifest

### 3.1 The six doomed modules (delete the files)
`src/call_manager/proposal_assist/web/`: `workspaces.py`, `files.py`, `references.py`,
`sections.py`, `citations.py`, `annotations.py`.

### 3.2 `main.py` registrations (surgical — the doomed entries are INTERLEAVED with kept ones)
The doomed router imports and mounts are **not contiguous**; the seven kept JSON-module routers
(activity, coherence, evaluator, problems, proposals, review, uncited_claims) sit between them.
Remove **only** the doomed entries; leave the kept ones untouched.
- **Imports to remove** (`main.py` ~30–69): `annotations` (34–36), `citations` (37–39),
  `files` (46–48), `references` (55–57), `sections` (61–63), `workspaces` (67–69).
- **`include_router` calls to remove** (`main.py` ~549–561): `workspaces_router` (549),
  `sections_router` (550), `annotations_router` (558), `files_router` (559),
  `references_router` (560), `citations_router` (561).
- **Keep**: the activity/coherence/evaluator/problems/proposals/review/uncited_claims imports
  and mounts, and `api_router` (562). (Line numbers are anchors; the plan matches on the import
  symbol, not the literal line, since earlier removals shift them.)

### 3.3 Templates (delete — all doomed-only; ~23 files under `web/templates/proposal_assist/`)
8 full-page templates (`workspaces_index`, `workspace_main`, `workspace_new`,
`workspace_settings`, `workspace_files`, `workspace_references`, `workspace_citations`,
`reference_metadata_review`) + ~15 partials (`partials/`: `chat_turn`, `artifact_body`,
`artifact_pane`, `artifact_sections`, `section_view`, `section_edit`, `file_row`,
`reference_row`, `history`, `turn_connector`, `citation_audit_results`, `citation_reader`,
`annotation_strip`, `disclosure_modal`, `cloud_banner`). The plan derives the exact set from
the six modules' `TemplateResponse(...)` calls and confirms each is referenced by **no** kept
template (digest has its own env; profile/admin/auth/calls templates don't include these).

### 3.4 Test files (delete — each verified doomed-only first)
The dedicated test files for the doomed Jinja routes (the `tests/proposal_assist/test_web_workspaces_*.py`
family, `test_web_references_*.py` family, `test_web_sections.py`, `test_web_annotations.py`,
`test_web_citation_render_wiring.py`, and the SSE/turns/disclosure/viewers/setup-gate workspace
tests). **Per-file gate:** before deleting each, confirm it tests ONLY doomed Jinja routes — if
any file mixes in a kept-surface (`/api`) test or exports a fixture a kept test imports, **trim**
rather than delete. The `/api` equivalents (`test_api_files.py`, `test_api_references.py`,
`test_api_turns.py`, etc.) are KEPT.

### 3.5 `base.html` nav link (remove one line)
Delete `base.html:40` — `<li><a href="/workspaces" …>Workspaces</a></li>`. `base.html` itself
STAYS (shared with profile/admin/auth/calls; it is deleted only in the final auth+teardown). The
React `/cockpit` is already linked from the SPA AppShell, so no replacement link is added here.

### 3.6 Dead citation-rendering chain in the kept `citations/reader.py`
Remove `render_proposal_with_citations` and the private helpers reachable **only** from it —
notably `_inject_links` (`reader.py:131`), which builds the last hardcoded
`/workspaces/{id}/citations/{ev}/reader` URL (`reader.py:141`), plus any sentinel/link-injection
helpers used solely by that chain. **KEEP** the low-level helpers `web/api` consumes
(`build_citation_numbering`, `build_citation_stances`, `evidence_order_for_reference`,
`build_source_window`). The plan must trace `reader.py`'s within-file usage (grep each private
helper for other callers) to delete exactly the dead set, and remove/trim any KEPT test that
exercised the deleted chain (e.g. `test_citation_reader_transform.py` if it targets
`render_proposal_with_citations`/`_inject_links`; `test_web_citation_render_wiring.py` is a
doomed-test deleted in §3.4).

### 3.7 OpenAPI regen
After the route deletions, regenerate with `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`.
Expected: `frontend/src/api/schema.gen.ts` **loses** the Jinja `/workspaces/…` HTML paths (and
keeps the G1 `/api/workspaces/{id}/references/{rid}/raw` + `/files/{aid}` serves); `routes.gen.ts`
unchanged (no React route change). (Per the Slice E gotcha, the regen MUST run AFTER the
deletions.)

---

## 4. Keep set (must NOT break)

- The 7 JSON modules (`proposal_assist/web/{proposals,activity,problems,coherence,evaluator,review,uncited_claims}.py`)
  and all `web/api/*`.
- `citations/reader.py`'s low-level helpers (§3.6) and the rest of the citations engine.
- The G1 neutral helper homes (`proposal_assist/web/_deps.py`, `workspace_meta.py`,
  `_file_uploads.py`, `proposal_assist/references/uploads.py`) — unaffected.
- `base.html` (shared); the email digest (own Jinja env + templates); the still-Jinja
  profile/admin/auth/calls pages.
- `tests/test_jinja_severance.py` — stays green unchanged: it asserts the kept modules contain
  no `from …proposal_assist.web.{doomed} import`, which remains true (and is now additionally
  enforced by the doomed modules' absence). Kept as a low-cost regression guard.

---

## 5. Deferred (NOT in G2)

The dead workspace-only sections of the shared `web/static/app.css` (~lines 758–1040) and
`web/static/app.js` (~lines 188–554). These are dead after G2 but live in files shared with the
still-Jinja profile/admin/auth pages, which are deleted **wholesale** in the final auth+teardown
sub-project. Editing shared static files mid-migration for dead-but-harmless CSS/JS adds risk for
no functional gain → deferred.

---

## 6. Verification gates

- `uv run python -c "import call_manager.main"` boots clean with the six routers gone.
- Full backend suite green after the test deletions: no kept test imports from a doomed module or
  a doomed test file; `ruff` clean; `tests/test_jinja_severance.py` green.
- **Route-absence assertion**: a test inspects the built app's **route table** (`app.routes`) and
  confirms **no route is registered at the bare `/workspaces` prefix** — all workspace routes now
  live under `/api/workspaces`. (Check the route table, not an HTTP response: a `GET /workspaces`
  could otherwise be swallowed by the SPA catch-all and look like a 200, masking a stale route.)
- Frontend untouched: `tsc`/`vitest`/`build` still clean (React already calls `/api`); the
  frontend `/workspaces` grep guard (no Jinja literals) still empty.
- OpenAPI regen (§3.7): `schema.gen.ts` drops the Jinja paths; `routes.gen.ts` unchanged.

---

## 7. Risks / notes

- **Interleaved `main.py` registrations** (§3.2): the doomed and kept routers alternate — the
  plan removes by import symbol, re-reading after each edit, so a shifted line number can't drop a
  kept router. Boot + the kept JSON-module suites gate this.
- **Surgical dead-code removal in a kept file** (`reader.py`, §3.6): the only real judgment call.
  Mitigation: a within-file usage trace per private helper + the test suite + `ruff`
  (unused-import / unused-private) catch an over- or under-removal. If the dead-chain boundary is
  unclear, the conservative fallback is to remove only `render_proposal_with_citations` and the
  helpers `ruff`/grep prove unused — but the goal is to eliminate the `/workspaces` URL builder
  (`_inject_links`).
- **Large test deletion** (~29 files): the risk is a kept test importing a fixture from a doomed
  test file. The per-file gate (§3.4) + a green full suite catch it.
- **Reachability already confirmed** (§2): no email digest, redirect, scheduler, or live React
  flow targets a Jinja `/workspaces/{id}` page; only `base.html:40` did, and that line is removed.

---

## 8. Out of scope

- The deferred `app.css`/`app.js` workspace sections (§5) → final auth+teardown.
- `base.html` itself, Pico, and the rest of the Jinja chrome → final auth+teardown.
- The other migration sub-projects (calls-detail, profile/account, admin, auth).
- Any behavior change to a kept surface — G2 is deletion-only.
