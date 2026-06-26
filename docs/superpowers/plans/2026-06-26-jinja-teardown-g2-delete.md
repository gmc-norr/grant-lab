# Jinja Teardown G2 — Delete the Orphaned Jinja `/workspaces` UI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the now-orphaned Jinja `/workspaces/{id}` two-pane workspace UI — the 6 doomed modules, their ~23 templates, their dedicated tests, the 6 `main.py` router registrations, the dead `reader.py` citation-render chain — and repoint the handful of kept surfaces that still hardcode a doomed `/workspaces/…` URL to their React `/cockpit` equivalents.

**Architecture:** Pure deletion + a few one-line link repoints. The React cockpit (Slices A–F) + the 3 gap-closer slices fully replaced the Jinja workspace UI; a fresh reachability audit (this plan's Task 0 evidence) confirms no kept module imports a doomed one and no kept Python renders a doomed template. Ordering keeps the test suite green at every task boundary: repoint kept links → prune doomed tests → unmount+delete modules → delete templates → remove the dead reader chain → regen OpenAPI.

**Tech Stack:** Python 3.12 / FastAPI / Jinja2 (being removed) / `uv` / `ruff` / pytest (+ testcontainers Postgres); React 19 SPA (unchanged); `pnpm`.

## Global Constraints

- **Deletion-only — no behavior change to any kept surface.** The 7 JSON modules (`proposal_assist/web/{proposals,activity,problems,coherence,evaluator,review,uncited_claims}.py`), all `web/api/*`, the email digest, and the still-Jinja profile/admin/auth/calls pages must behave exactly as before.
- **No DB schema change. No new dependency.**
- **The 6 doomed modules** are `src/call_manager/proposal_assist/web/{workspaces,files,references,sections,citations,annotations}.py`. Their ONLY importers are `main.py` and each other (verified) — nothing kept imports them.
- **`base.html` itself STAYS** (shared with profile/admin/auth/calls — deleted only in the final auth+teardown). Only its line-40 Workspaces nav `<li>` is removed.
- **`reader.py` low-level helpers STAY:** `build_citation_numbering`, `evidence_order_for_reference`, `build_citation_stances`, `build_source_window` (consumed by `web/api`). Only `render_proposal_with_citations` + the helpers reachable ONLY from it (notably `_inject_links`, the last `/workspaces/{id}/citations/{ev}/reader` URL builder) are removed.
- **`tests/test_jinja_severance.py` STAYS unchanged** — it greps source *text* (`path.read_text()`), never imports a doomed module, so it survives deletion and remains a regression guard.
- **`tests/js/citation_drawer.test.js` STAYS** — it tests `app.js`'s `citeReaderUrl` *string parsing* (pure logic; `app.js` is deferred to the final teardown, not deleted here).
- **Deferred (NOT in G2, per spec §5/§8):** the dead workspace-only sections of the shared `web/static/app.css` / `app.js` (incl `app.js:327`'s now-trigger-less `/workspaces/{id}/artifact` ajax) — removed wholesale in the final auth+teardown.
- **OpenAPI regen command:** `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`. `frontend/src/routes.gen.ts` MUST stay unchanged (no React route change).
- Source spec: `docs/superpowers/specs/2026-06-25-jinja-teardown-g2-delete-design.md`. **This plan supersedes the spec's manifest where the fresh audit (2026-06-26, post-gap-closers) extended it** — see the Audit Addendum below.

## Audit Addendum (fresh reachability audit, 2026-06-26 — beyond the spec's §2/§3)

The spec's audit predated the 3 gap-closer slices. Re-running it against master `79af1a4` confirmed the orphan-removal premise still holds AND surfaced items the spec's manifest omitted:

1. **Two kept templates hardcode doomed `/workspaces/…` URLs** (not just `base.html:40`) — deleting the routes without repointing these is a live regression:
   - `call_detail.html:19` — the **"Start drafting"** link → `/workspaces/new?call_id=…&profile_id=…`. Repoint to **`/cockpit/new?…`** (React `WorkspaceCreate.tsx` reads `call_id`+`profile_id` — verified). Update `test_web_calls_start_drafting.py` (asserts the URL string).
   - `admin_drafting.html:65` — each workspace row → `/workspaces/{id}`. Repoint to **`/cockpit/{id}`** (the React cockpit editor). No test asserts this link. Also `admin_drafting.html:17,20` — kill-switch status **copy** literally names `POST /workspaces/<id>/turns` (not a link, so URL/href greps miss it); after G2 the kept endpoint is `/api/workspaces/{id}/turns` → update the copy.
2. **Mixed test files need trim/retarget, not blanket delete** (resolved per-file in Task 2 / Task 5).
3. `app.js:327` (`/workspaces/{id}/artifact` ajax) is dead-but-harmless on the kept pages (its trigger element lives only on the deleted workspace editor) → deferred with the rest of `app.js`.

---

### Task 1: Repoint the kept surfaces off the doomed routes (do FIRST — links must never point at a route that's about to be deleted)

Additive-safe: the doomed routes still exist at this point, and the React `/cockpit` targets already work, so nothing breaks. This task removes every kept reference to a doomed `/workspaces/…` URL.

**Files:**
- Modify: `src/call_manager/web/templates/base.html` (line 40 — remove Workspaces nav)
- Modify: `src/call_manager/web/templates/call_detail.html` (line 19 — Start-drafting link)
- Modify: `src/call_manager/web/templates/admin_drafting.html` (line 65 — row link; lines 17/20 — kill-switch copy)
- Modify: `tests/proposal_assist/test_web_calls_start_drafting.py`

- [ ] **Step 1: Update the failing test first (TDD) — assert the React URL**

In `tests/proposal_assist/test_web_calls_start_drafting.py`, change the 3 asserted URL literals from `/workspaces/new?…` to `/cockpit/new?…`:

```python
    assert f"/cockpit/new?call_id={cid}&profile_id={pid}".encode() in response.content
```
```python
    assert f"/cockpit/new?call_id={cid}".encode() in response.content
    assert f"/cockpit/new?call_id={cid}&profile_id=".encode() not in response.content
```

- [ ] **Step 2: Run the test → it FAILS (template still emits `/workspaces/new`)**

Run: `uv run pytest tests/proposal_assist/test_web_calls_start_drafting.py -q`
Expected: FAIL (asserts `/cockpit/new`, template still renders `/workspaces/new`).

- [ ] **Step 3: Repoint `call_detail.html:19`**

Change `href="/workspaces/new?call_id=…"` to `href="/cockpit/new?call_id=…"` (keep the `{{ call.id }}` / `best_profile_id` Jinja exactly as-is):

```html
    <a href="/cockpit/new?call_id={{ call.id }}{% if best_profile_id %}&profile_id={{ best_profile_id }}{% endif %}"
```

- [ ] **Step 4: Run the test → PASSES**

Run: `uv run pytest tests/proposal_assist/test_web_calls_start_drafting.py -q`
Expected: PASS.

- [ ] **Step 5: Repoint `admin_drafting.html` — the row link AND the stale kill-switch copy**

(a) Line 65 — change the workspace-row link from `/workspaces/{{ row.id }}` to `/cockpit/{{ row.id }}`:

```html
          <td><a href="/cockpit/{{ row.id }}">{{ row.title | e }}</a></td>
```

(b) Lines 17 and 20 — the kill-switch status copy literally names the now-doomed endpoint (`POST /workspaces/&lt;id&gt;/turns`). After G2 that bare route is gone; the kept equivalent is `/api/workspaces/{id}/turns`. Update both occurrences:

```html
      <strong>Drafting enabled</strong> — POST /api/workspaces/&lt;id&gt;/turns
```
```html
      <strong>Drafting disabled</strong> — POST /api/workspaces/&lt;id&gt;/turns
```

- [ ] **Step 6: Remove the `base.html:40` Workspaces nav link**

Delete the entire line 40 `<li>…Workspaces…</li>` from `src/call_manager/web/templates/base.html`. (The React `/cockpit` is linked from the SPA AppShell; no replacement nav link is added.)

- [ ] **Step 7: Verify no kept template references a bare doomed `/workspaces/…` URL — in links OR in copy**

Run (broad match — `/workspaces/` anywhere, not just quoted hrefs, so admin copy is caught too; exclude the `/api/` paths and the doomed `proposal_assist/` templates):
```bash
grep -rnE '/workspaces/?[<{a-z]' src/call_manager/web/templates/ --include='*.html' \
  | grep -v 'proposal_assist/' | grep -v '/api/workspaces'
```
Expected: **no output** (every kept-template `/workspaces` reference — `base.html` nav, `call_detail` link, `admin_drafting` link + kill-switch copy — is now repointed or removed; the only remaining `/workspaces` literals live inside the doomed `proposal_assist/` templates, deleted in Task 4). `app.js`/`app.css` are deferred (Global Constraints).

- [ ] **Step 8: Commit**

```bash
git add src/call_manager/web/templates/base.html src/call_manager/web/templates/call_detail.html src/call_manager/web/templates/admin_drafting.html tests/proposal_assist/test_web_calls_start_drafting.py
git commit -m "feat(spa): repoint kept calls/admin/base links off doomed Jinja /workspaces → /cockpit"
```

---

### Task 2: Prune the doomed test surface (delete doomed-only files; trim the mixed ones)

Remove every test that exercises a doomed Jinja route, BEFORE the routes are deleted (Task 3) — so the suite is never red. Each file is gated: confirm it tests ONLY doomed routes (no `/api/` test, no fixture a kept test imports) before deleting; otherwise trim.

**Files:**
- Delete: the 34 doomed-route-only test files (listed in Step 2)
- Modify (trim): `test_global_cap.py`, `test_kill_switch.py`, `test_references_admission.py`

- [ ] **Step 1: Per-file gate — confirm the delete set is doomed-only**

Run (note the glob is `test_web_workspace*` — singular-aware — so it catches `test_web_workspace_setup_gate.py`, which the plural `test_web_workspaces_*` glob would MISS):
```bash
# which delete-set files contain an /api/ request? (candidates for trim, not delete)
grep -rln '"/api/\|/api/workspaces' tests/proposal_assist/test_web_workspace*.py tests/proposal_assist/test_web_references_*.py tests/proposal_assist/test_web_citation_reader_route.py tests/proposal_assist/test_web_citation_render_wiring.py tests/test_web_sections.py tests/test_web_annotations.py tests/proposal_assist/test_error_handler_htmx.py tests/proposal_assist/test_b7_e2e.py || echo "OK: no /api tests mixed in"
# is any delete-set file imported by another (kept) test? (fixture reuse)
grep -rln "from tests.proposal_assist.test_web_workspace\|from tests.test_web_sections\|from tests.test_web_annotations\|import test_b7_e2e" tests/ || echo "OK: no kept test imports a delete-set file"
```
Expected: the second grep prints `OK:` (no fixture reuse). The first grep prints **exactly one** file: `tests/proposal_assist/test_web_workspace_setup_gate.py` — the **one documented exception** (see below). Any OTHER file it prints is a genuine mixed file → move it to the trim set instead of deleting.

**Documented exception — `test_web_workspace_setup_gate.py` is DELETED despite an `/api/` hit.** Its `test_create_confirm_composer_closed_loop` calls `/api/workspaces/{id}/activate` as ONE step, but every assertion in the file is coupled to the Jinja `/workspaces/{id}` VIEW (`client.get(f"/workspaces/{wsid}")` — the doomed `workspace_main` page) plus the Jinja `/workspaces/{id}/turns` route; there is no cleanly-extractable `/api` test. The `/api/activate` closed-loop behavior is **independently covered by the kept `tests/proposal_assist/test_phase0_e2e.py:174`** (`create → POST /api/.../activate → drafting live`, pure `/api`, no Jinja view). So deleting this file loses no unique `/api` coverage. Confirm before deleting:
```bash
grep -n "/api/workspaces/{.*}/activate\|/api/workspaces/.*activate" tests/proposal_assist/test_phase0_e2e.py
```
Expected: a hit at ~line 174 (the kept `/api` activation coverage). If this grep is EMPTY, STOP — trim `test_web_workspace_setup_gate.py` (keep an `/api/activate` test) instead of deleting it.

- [ ] **Step 2: Delete the 34 doomed-route-only test files**

```bash
git rm \
  tests/proposal_assist/test_web_workspaces_artifact.py \
  tests/proposal_assist/test_web_workspaces_cancel.py \
  tests/proposal_assist/test_web_workspaces_citations.py \
  tests/proposal_assist/test_web_workspaces_delete.py \
  tests/proposal_assist/test_web_workspaces_disclosure_gate.py \
  tests/proposal_assist/test_web_workspaces_disclosure.py \
  tests/proposal_assist/test_web_workspace_setup_gate.py \
  tests/proposal_assist/test_web_workspaces_files.py \
  tests/proposal_assist/test_web_workspaces_list.py \
  tests/proposal_assist/test_web_workspaces_main.py \
  tests/proposal_assist/test_web_workspaces_new.py \
  tests/proposal_assist/test_web_workspaces_references.py \
  tests/proposal_assist/test_web_workspaces_references_fetch.py \
  tests/proposal_assist/test_web_workspaces_reference_waiver.py \
  tests/proposal_assist/test_web_workspaces_settings.py \
  tests/proposal_assist/test_web_workspaces_sse.py \
  tests/proposal_assist/test_web_workspaces_turn_errors.py \
  tests/proposal_assist/test_web_workspaces_turns.py \
  tests/proposal_assist/test_web_workspaces_viewer_enforcement.py \
  tests/proposal_assist/test_web_workspaces_viewers.py \
  tests/proposal_assist/test_web_workspaces_xss.py \
  tests/proposal_assist/test_web_references_delete.py \
  tests/proposal_assist/test_web_references_guards.py \
  tests/proposal_assist/test_web_references_kind.py \
  tests/proposal_assist/test_web_references_member_access.py \
  tests/proposal_assist/test_web_references_reprocess.py \
  tests/proposal_assist/test_web_references_templates.py \
  tests/proposal_assist/test_web_references_upload_pdf.py \
  tests/proposal_assist/test_web_citation_reader_route.py \
  tests/proposal_assist/test_web_citation_render_wiring.py \
  tests/proposal_assist/test_error_handler_htmx.py \
  tests/proposal_assist/test_b7_e2e.py \
  tests/test_web_sections.py \
  tests/test_web_annotations.py
```

(Note: `test_b7_e2e.py` is an upload→turn→drain e2e via the doomed Jinja routes; its pieces are covered individually by `test_api_files.py` / `test_api_turns_*.py` / the drain tests. `test_error_handler_htmx.py` tests the doomed HTMX/app.js error-rendering path; the `/api` error envelope has its own tests.)

- [ ] **Step 3: Trim `test_global_cap.py` — keep the 2 service tests, remove the 2 Jinja-route tests**

Delete the two functions that POST to `/workspaces/{id}/turns`: `test_post_turn_returns_429_with_global_header` and `test_global_cap_wins_when_per_workspace_also_exceeded`. KEEP `test_check_global_cap_sums_across_workspaces` and `test_submit_raises_global_cap_exceeded` (service-level: `check_global_daily_token_cap` / `submit_user_turn`). Remove any now-unused imports (`TestClient`, `create_app`, signer) flagged by ruff.

- [ ] **Step 4: Trim `test_kill_switch.py` — keep the 3 service/drain tests, remove the 2 Jinja-route tests**

Delete `test_post_turn_returns_503_when_kill_switch_off` and `test_post_turn_works_when_kill_switch_on` (both POST `/workspaces/{id}/turns`). KEEP `test_drain_tick_noops_when_kill_switch_off`, `test_phase0_and_compaction_skip_when_disabled`, `test_re_enable_drains_queued_backlog` (all drive `drain_turn_queue_tick` at the service level). Remove now-unused imports (ruff).

- [ ] **Step 5: Trim `test_references_admission.py` — keep the `IngestLimiter` service tests, remove the 2 Jinja-route tests**

Delete `test_upload_returns_429_when_no_slot` (POSTs `/workspaces/{id}/references`) and `test_fetch_route_returns_429_when_no_slot` (POSTs `/workspaces/{id}/references/fetch`). KEEP the `IngestLimiter` / `OverCapacity` service-level tests `test_limiter_admits_up_to_capacity` and `test_limiter_releases_slot`. Remove now-unused imports (ruff).

- [ ] **Step 6: Run the trimmed files + ruff to confirm green and import-clean**

Run:
```bash
uv run pytest tests/proposal_assist/test_global_cap.py tests/proposal_assist/test_kill_switch.py tests/proposal_assist/test_references_admission.py -q
uv run ruff check tests/proposal_assist/test_global_cap.py tests/proposal_assist/test_kill_switch.py tests/proposal_assist/test_references_admission.py
```
Expected: all PASS; ruff clean (no unused imports).

- [ ] **Step 7: Commit**

```bash
git add -A tests/
git commit -m "test(teardown): delete doomed Jinja-route tests; trim service tests off doomed routes"
```

---

### Task 3: Remove the 6 `main.py` registrations, delete the 6 doomed modules, add the route-absence guard

The doomed router imports/mounts in `main.py` are **interleaved** with the 7 kept JSON-module routers — remove ONLY the doomed entries, matching by import symbol (re-read after each edit; line numbers shift).

**Files:**
- Modify: `src/call_manager/main.py`
- Delete: the 6 doomed modules
- Create: `tests/test_jinja_workspace_surface_absent.py`

- [ ] **Step 1: Write the route-absence guard test**

Create `tests/test_jinja_workspace_surface_absent.py`:

```python
"""G2 teardown guard: no FastAPI route is registered under the bare Jinja
`/workspaces` HTML prefix. All workspace routes now live under `/api/workspaces`.

Inspect the route TABLE, not an HTTP response: a GET /workspaces could be
swallowed by the SPA catch-all and look like 200, masking a stale route."""

from __future__ import annotations

from call_manager.main import create_app


def test_no_bare_workspaces_route_registered(monkeypatch, pg_url):
    # Use pg_url (the real testcontainer URL, psycopg3 driver) — a bare
    # "postgresql://" selects the psycopg2 dialect, which is NOT installed, so
    # create_app() would fail at engine creation before the route assertion.
    # pg_url is defined in tests/conftest.py and available to this root test.
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()
    bare = [
        r.path
        for r in app.routes
        if getattr(r, "path", "").startswith("/workspaces")
    ]
    assert bare == [], f"stale Jinja /workspaces routes still registered: {bare}"
```

- [ ] **Step 2: Run it → FAILS (the 6 doomed routers are still mounted)**

Run: `uv run pytest tests/test_jinja_workspace_surface_absent.py -q`
Expected: FAIL (lists the live Jinja `/workspaces/…` paths).

- [ ] **Step 3: Remove the 6 doomed router imports from `main.py`**

Delete the import blocks for these 6 symbols (each is a `from call_manager.proposal_assist.web.<mod> import (... as <x>_router)` block, ~lines 34–69): `annotations`, `citations`, `files`, `references`, `sections`, `workspaces`. **KEEP** the `activity`, `coherence`, `evaluator`, `problems`, `proposals`, `review`, `uncited_claims` import blocks (the 7 kept JSON modules) and the `web/api` import.

- [ ] **Step 4: Remove the 6 doomed `include_router` calls from `main.py`**

Delete these mounts (~lines 549–561): `workspaces_router`, `sections_router`, `annotations_router`, `files_router`, `references_router`, `citations_router`. **KEEP** `proposals_router`, `coherence_router`, `evaluator_router`, `review_router`, `problems_router`, `uncited_claims_router`, `activity_router`, and `api_router`.

- [ ] **Step 5: Delete the 6 doomed module files**

```bash
git rm \
  src/call_manager/proposal_assist/web/workspaces.py \
  src/call_manager/proposal_assist/web/files.py \
  src/call_manager/proposal_assist/web/references.py \
  src/call_manager/proposal_assist/web/sections.py \
  src/call_manager/proposal_assist/web/citations.py \
  src/call_manager/proposal_assist/web/annotations.py
```

- [ ] **Step 6: App boots + route-absence test passes + severance guard green**

Run:
```bash
EMBEDDING_MODEL=bge-m3 uv run python -c "import call_manager.main; call_manager.main.create_app(); print('boot ok')"
uv run pytest tests/test_jinja_workspace_surface_absent.py tests/test_jinja_severance.py -q
uv run ruff check src/call_manager/main.py
```
Expected: `boot ok`; both tests PASS; ruff clean (no unused import left behind in `main.py`).

- [ ] **Step 7: Commit**

```bash
git add -A src/call_manager/main.py src/call_manager/proposal_assist/web/ tests/test_jinja_workspace_surface_absent.py
git commit -m "feat(teardown): unmount + delete the 6 doomed Jinja workspace modules; add route-absence guard"
```

---

### Task 4: Delete the ~23 doomed Jinja templates

All `proposal_assist/*.html` templates were rendered only by the (now-deleted) doomed modules — no kept Python renders any of them (verified). Three include-only partials (`artifact_pane`, `cloud_banner`, `disclosure_modal`) are reached only via `{% include %}` from doomed templates; confirm none is included by a kept template, then delete the whole directory.

**Files:**
- Delete: `src/call_manager/web/templates/proposal_assist/` (all ~23 `.html`)

- [ ] **Step 1: Safety check — no KEPT template includes any doomed partial**

Run:
```bash
# Any {% include %}/{% extends %}/{% import %} of a proposal_assist template from OUTSIDE proposal_assist/?
grep -rnE '(include|extends|import|from)\s+"proposal_assist/' src/call_manager/web/templates/ --include='*.html' | grep -v '/proposal_assist/'
```
Expected: **no output** (only doomed templates reference each other). If a kept template (base/calls/admin/profile/auth) shows up, STOP and trim that reference first.

- [ ] **Step 2: Delete the doomed template directory**

```bash
git rm -r src/call_manager/web/templates/proposal_assist/
```

- [ ] **Step 3: App still boots (template lookups are lazy; confirm nothing kept referenced them)**

Run:
```bash
EMBEDDING_MODEL=bge-m3 uv run python -c "import call_manager.main; call_manager.main.create_app(); print('boot ok')"
ls src/call_manager/web/templates/proposal_assist 2>&1 | tail -1
```
Expected: `boot ok`; the directory no longer exists.

- [ ] **Step 4: Commit**

```bash
git add -A src/call_manager/web/templates/
git commit -m "feat(teardown): delete the doomed Jinja workspace templates"
```

---

### Task 5: Remove the dead `reader.py` citation-render chain + trim its test

`render_proposal_with_citations` was called only from the (now-deleted) `workspaces.py`/`sections.py`. Remove it and the private helpers reachable ONLY from it (notably `_inject_links`, the last `/workspaces/{id}/citations/{ev}/reader` URL builder). KEEP the low-level helpers `web/api` consumes.

**Files:**
- Modify: `src/call_manager/proposal_assist/citations/reader.py`
- Modify (trim): `tests/proposal_assist/test_citation_reader_transform.py`

- [ ] **Step 1: Trace the dead set — find the exact helpers reachable only from the dead chain**

Run (for each private helper, count callers OTHER than the dead-chain functions):
```bash
cd /home/parlar_ai/dev/call-monitor
for h in _inject_links _to_sentinels _code_span_ranges _all_indices locate_evidence; do
  echo "=== $h callers (across src/) ==="
  grep -rn "$h" src/call_manager/proposal_assist/citations/reader.py src/ --include='*.py' | grep -v "def $h"
done
```
Decision rule: a helper used ONLY by `render_proposal_with_citations` / `_inject_links` (or by another dead helper) is dead → remove. A helper used by a KEPT function → KEEP. **Expected dead set (from the call-graph trace): `render_proposal_with_citations` (renderer), `_inject_links` (+ its nested `_link_html`/`_expand`), `_to_sentinels` (called only by the renderer, line ~277), and `_code_span_ranges` (called only by `_to_sentinels`, line ~114).** **Expected KEEP: `build_citation_numbering`, `build_citation_stances`, `evidence_order_for_reference`, `build_source_window`** (all consumed by the kept `web/api/workspaces.py`), plus **`locate_evidence` + `_all_indices`** (used by the kept `build_source_window`, lines ~249/206). `ruff` is the safety net — if it flags a kept helper as unused after removal, the trace was wrong.

- [ ] **Step 2: Remove `render_proposal_with_citations` + the traced dead helpers from `reader.py`**

Delete the `def render_proposal_with_citations(...)` block, the `def _inject_links(...)` block, and any helper the Step-1 trace proved dead. Leave the kept helpers untouched.

- [ ] **Step 3: `ruff` confirms no over-/under-removal (no unused private, no broken import)**

Run: `uv run ruff check src/call_manager/proposal_assist/citations/reader.py`
Expected: clean. If ruff flags a now-unused private helper, it was part of the dead chain — remove it. If it flags an undefined name, you removed a helper a KEPT function still needs — restore it.

- [ ] **Step 4: Trim `test_citation_reader_transform.py` — drop the render/inject tests, keep the helper tests**

Remove the `render_proposal_with_citations` import and every test that calls it / asserts the `/workspaces/{id}/citations/{ev}/reader` link HTML (the functions using `render_proposal_with_citations` at the file's body). KEEP the tests for `build_citation_numbering`, `build_citation_stances`, `build_source_window`, `evidence_order_for_reference`. Remove the now-unused import (ruff).

- [ ] **Step 5: Run the citations engine tests + ruff**

Run:
```bash
uv run pytest tests/proposal_assist/test_citation_reader_transform.py tests/proposal_assist/citations/ tests/proposal_assist/tools/ -q
uv run ruff check tests/proposal_assist/test_citation_reader_transform.py
```
Expected: PASS; ruff clean. (The `web/api` citation surfaces — peek/audit — still pass because they use only the kept helpers.)

- [ ] **Step 6: Commit**

```bash
git add -A src/call_manager/proposal_assist/citations/reader.py tests/proposal_assist/test_citation_reader_transform.py
git commit -m "feat(teardown): remove dead render_proposal_with_citations chain from reader.py"
```

---

### Task 6: OpenAPI regen

The route deletions change the documented OpenAPI surface. Regen AFTER the deletions (per the Slice E gotcha).

**Files:**
- Modify: `frontend/src/api/schema.gen.ts` (generated)

- [ ] **Step 1: Regenerate**

Run: `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`
Expected: completes without error.

- [ ] **Step 2: Verify the Jinja `/workspaces/…` paths are GONE and the `/api` + G1 serves remain**

Run:
```bash
# the bare Jinja workspace HTML paths must be removed from the schema
grep -cE '"/workspaces/\{workspace_id\}/(sections|references|files|citations|annotations)' frontend/src/api/schema.gen.ts
# the kept /api workspace surface + G1 binary serves must remain
grep -c '"/api/workspaces/{workspace_id}/references/{reference_id}/raw"' frontend/src/api/schema.gen.ts
git diff --quiet frontend/src/routes.gen.ts && echo "routes.gen.ts UNCHANGED ✓" || echo "routes.gen.ts CHANGED ✗"
```
Expected: first grep `0` (Jinja paths dropped); second grep `1` (G1 raw-serve kept); `routes.gen.ts UNCHANGED ✓`.

- [ ] **Step 3: Frontend still clean (React already calls `/api` only)**

Run (from `frontend/`): `export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; pnpm tsc -b && pnpm build`
Expected: tsc clean; build clean. (No `vitest` change expected — no React source changed.)

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/schema.gen.ts
git commit -m "chore(openapi): regen types after deleting Jinja /workspaces routes"
```

---

## Final Verification (before handing off to review / merge)

- [ ] Backend boots with the 6 routers gone: `EMBEDDING_MODEL=bge-m3 uv run python -c "import call_manager.main; call_manager.main.create_app(); print('ok')"`
- [ ] Route-absence + severance guards green: `uv run pytest tests/test_jinja_workspace_surface_absent.py tests/test_jinja_severance.py -q`
- [ ] **Full backend suite green** (no kept test imports a deleted module/test/fixture): `uv run pytest -q` (if testcontainers flakes under load — connection-refused / DeadlockDetected on TRUNCATE — re-run the flagged file in isolation; that is infra).
- [ ] `uv run ruff check src/ tests/` clean.
- [ ] Frontend: `cd frontend && pnpm tsc -b && pnpm build` clean; `routes.gen.ts` unchanged; OpenAPI dropped the Jinja `/workspaces` paths, kept `/api/workspaces/…`.
- [ ] No kept surface references a doomed `/workspaces` URL: `grep -rnE 'href="/workspaces|"/workspaces/new|"/workspaces/\{' src/call_manager/web/templates/ --include='*.html' | grep -v proposal_assist` → empty (`app.js`/`app.css` deferred).
- [ ] Deletion-only: `git diff --stat <merge-base>` shows only deletions + the Task-1 link repoints + the route-absence test + the OpenAPI regen; no DB/migration change.

---

## Self-Review (completed during planning)

**Spec coverage:** §3.1 modules → Task 3; §3.2 main.py → Task 3; §3.3 templates → Task 4; §3.4 tests → Task 2 (+ Task 5 trims the reader test); §3.5 base.html nav → Task 1; §3.6 reader.py chain → Task 5; §3.7 OpenAPI → Task 6; §6 route-absence guard → Task 3. **Beyond the spec (audit addendum):** the `call_detail.html` + `admin_drafting.html` repoints → Task 1 (the spec's audit missed these live links to doomed routes).

**Ordering safety (suite green at every boundary):** Task 1 repoints links while routes still exist (no break). Task 2 removes doomed-route tests BEFORE Task 3 removes the routes (suite never red). Task 3 unmounts+deletes modules (route-absence test green, boot OK). Tasks 4–5 delete templates / dead code (nothing references them). Task 6 regen.

**Placeholder scan:** every deletion step names exact files; every trim step names exact test functions to keep/remove; every verification step has an exact command + expected output. The two judgment-bearing steps (test per-file gate in Task 2 Step 1; reader.py dead-chain trace in Task 5 Step 1) carry explicit decision rules + `ruff`/suite as the safety net.
