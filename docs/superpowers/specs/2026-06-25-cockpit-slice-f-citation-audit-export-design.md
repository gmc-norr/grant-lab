# Cockpit Cutover Slice F — Citation Audit + Export (Design Spec)

**Date:** 2026-06-25
**Status:** Design approved; pending spec-review gate → writing-plans.
**Sub-project:** Full-React migration → Cockpit cutover (Slices A–G). Slice F is the
last *functional* cockpit slice; only Jinja teardown sweeps remain after it.
**Builds on:** Slice A front-door (`fb43638`), B chat (`ddc6e3a`), C refs+media
(`c4edafe`/`15e5ff2`), D settings+collaborators (`7adbb44`), E rubric lifecycle
(`09d2f03`).

---

## 1. Goal

Bring two surfaces from the legacy Jinja workspace into the React cockpit, reaching
parity (and modestly exceeding it):

1. **Citation audit** — verify a proposal draft's citations against their sources.
2. **Proposal export** — download the proposal as Markdown (raw, and composed with
   inline citations + bibliography).

Then **tear down the Jinja export routes** — the only citation/export surface cleanly
excisable now (per the 2026-06-25 "Jinja need not be preserved" directive). The Jinja
citations page + validate + reader stay until the later `workspace_main` sweep because
they remain wired into live Jinja proposal rendering (see §6).

**No DB schema change.** The audit engine, verdict cache (`citation_audit_verdicts`,
migration 0039), usage ledger (`citation_llm_usage`, migration 0036), and the
Markdown/bibliography builders already exist. Slice F *surfaces* them through additive
`/api` endpoints and React UI.

---

## 2. Settled decisions (scope)

These three choices were made before design and bound the whole slice:

1. **Export formats: Markdown parity only.** Raw `proposal.md` and composed
   (markers rendered + bibliography). **No DOCX/PDF** — they were explicitly deferred
   in-code ("would require pandoc"); adding them is a separate future effort with a new
   dependency. No new dependency in Slice F.
2. **Audit depth: deterministic now, semantic on-demand.** The deterministic checker
   (offline, free, ~14 issue types) runs automatically (matches the Jinja "Validate"
   button). The **semantic LLM-judge** layer — which fully exists in the engine but was
   never surfaced in any UI — runs only on an explicit **owner-triggered "Deep check"**,
   respecting the existing token caps. This is parity-plus without surprise LLM cost.
3. **Cockpit IA: Citations bottom-panel tab + Export menu on the draft toolbar.**
   Audit lives in a new bottom-panel tab (Inbox · Problems · Activity · Library ·
   **Citations**). Export is an "Export ▾" menu on the draft artifact toolbar.

---

## 3. Current state (grounding)

### 3.1 Audit engine (exists, reuse — do NOT modify)
- `proposal_assist/citations/audit.py`:
  - `validate_citations_deterministic(proposal_text, ledger, session, workspace_id) -> list[dict]`
    (`:135–223`) — pure DB/string re-checks, no LLM, offline-safe. Issue dict shape:
    `{type, severity: "error"|"warning", citekey: str|None, evidence_id: str|None,
    location: str|None, detail: str}`. ~14 deterministic types (orphan_marker,
    reference_missing, metadata_unapproved, claim_drift, …).
  - `validate_citations_semantic(proposal_text, ledger, session, workspace_id, *,
    citation_service, session_factory, deterministic_issues, validator_version) -> list[dict]`
    (`:505–719`) — per-citation LLM judge; same issue shape plus semantic types
    (semantic_contradicts/unsupported/irrelevant, audit_unavailable). Writes the verdict
    cache; cap-checked per call; stops on cap-exceeded.
- MCP orchestration the agent already uses: `proposal_assist/tools/audit.py`
  `validate_citations_impl(ctx)` (`:66–104`) → `{issues, semantic_available, checked}`.
  This is the reference for the det→semantic run + merge order; the service itself is
  **reused** from `app.state.citation_service` (§4.2), not reconstructed.
- Service/LLM/caps: `proposal_assist/citations/service.py` (`CitationLLMService.complete_json`
  cap-checks then logs usage), `citations/llm.py` (`build_citation_llm()` wraps
  Anthropic/Ollama), `citations/verdict_cache.py` (`VALIDATOR_VERSION`, get/store).
  Config + caps: `config.py:392–413` (`CITATION_LLM_PROVIDER` default `ollama`,
  per-workspace + global daily token caps).
- Models (exist): `CitationAuditVerdict` (`db/models.py:1704–1736`), `CitationLlmUsage`
  (`:1673–1701`). Migrations 0036, 0039. **Untouched by Slice F.**

### 3.2 Export builders (exist, mostly reusable)
- Raw: Jinja route `proposal_assist/web/workspaces.py:1348–1378` — logic inline (reads
  `repo/proposal.md`, sanitizes title, `FileResponse`). Sanitizer inline.
- Composed: Jinja route `proposal_assist/web/citations.py:174–256` — thin wrapper over
  non-web helpers: `load_ledger()` (`citations/ledger.py:20`), `build_bibliography()`
  (`citations/bibliography.py:545`), `compose_export()` (`:665`),
  `BuildBibliographyBlocked` (`:410`). Has a private `_safe_title()` (`citations.py:169`).
- **Relocation needed:** only `safe_title` (duplicated/trapped) and the raw-export
  file-IO need to move to a shared module; the composed pipeline is already in
  `citations/`.

### 3.3 Perms / session (exist, reuse)
- `proposal_assist/web/_perms.py`: `require_member(*, workspace_id, user, session) -> Workspace`
  (`:29`, owner OR viewer); `require_owner(...)` (same module). Already imported by
  existing `/api` modules — importable from `/api`.
- `_session_dep` (`proposal_assist/web/references.py:93`) — session dependency used by
  existing routes; existing `/api` modules have their own session dependency pattern —
  follow the established `/api` module pattern, not the Jinja `_session_dep` import.

### 3.4 React cockpit (exists, extend)
- Bottom panel: tabs union `cockpit/nav.ts:2`; tab buttons `cockpit/BottomPanel.tsx:32–65`;
  body conditional render `:75–82`. Active bottom tab is **local state** in
  `routes/Cockpit.tsx:21` (NOT URL-persisted); `onNavigate` callback threads deep-links.
- Draft view: `cockpit/DraftPanel.tsx` wraps `editor/ArtifactSectionEditor.tsx`.
- Peek: `ArtifactSectionEditor.tsx` owns `peekEv` (`:67`), `useCitationSource` (`:73`),
  `onPillClick = (citekey, evId) => setPeekEv(evId)` (`:180`, wired to CodeMirror `:231`).
  Hook: `api/citations.ts:8–19` (`useCitationSource(id, evId)` → GET
  `/api/workspaces/{id}/citations/{ev}/source`). **Reused as-is.**
- API client: `api/client.ts:34–44` (`api`, `unwrap`). Generated types:
  `components["schemas"]["X"]` from `api/schema.gen.ts`.
- Owner signal: `api/workspace.ts:7–17` `useCockpitMeta(id)` → `CockpitMetaOut.role`
  ("owner" | …) + `current_phase`. Example gate: `CockpitSidebar.tsx:66,118–122`.
- Existing `/api` citation endpoints stay: source peek
  (`web/api/workspaces.py:561–672`), add-citation (`:759–820`).

---

## 4. Backend design

### 4.1 New service module `proposal_assist/export.py`
Shared by the new `/api` routes and (temporarily) the Jinja routes until teardown.
Pure orchestration over existing `citations/` helpers; no Jinja/web imports so teardown
of `web/` is clean.

```python
def safe_title(title: str) -> str:
    """Sanitize a workspace title for a Content-Disposition filename."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in title)[:80] or "proposal"

def get_markdown_export(workspace, workspaces_dir: str) -> bytes | None:
    """Raw proposal.md bytes from the workspace repo; None if absent (pre-draft)."""

def get_composed_export(*, workspace, workspaces_dir: str, session) -> str | None:
    """Markdown with inline citations rendered + bibliography appended.
    Returns None if proposal.md is absent (pre-draft) — mirrors get_markdown_export.
    Raises BuildBibliographyBlocked when any cited reference is unapproved/unwaived/
    unresolved/tombstoned. Returns the composed text on success.
    Loads citations.json via load_ledger, uses workspace.citation_style (default
    'vancouver'), calls build_bibliography(commit=False) then compose_export()."""
```
Implementer reads the two Jinja routes to transcribe exact builder kwargs (`repo_dir`,
`commit=False`, `bibliography_text=…`). The `_safe_title` in `citations.py` and the
inline sanitizer in `workspaces.py` are both replaced by `export.safe_title`.

### 4.2 Citation service (reuse — no new module)
Deep-check reuses the citation service already built at startup:
`request.app.state.citation_service` — constructed via the **supported**
`build_citation_service(*, settings, session_factory)` (`citations/service.py:105`,
exported from `citations/__init__.py`), wired at `main.py:485` (lazy/per-call, so present
but cheap at startup). **Do not add a parallel builder** — a second helper would risk
diverging None-on-misconfig semantics from the supported path.

`validate_citations_semantic` also needs a `session_factory` for its verdict-cache writes
— use `request.app.state.session_factory` (set at `main.py:460`). `semantic_available` is
`True` whenever `app.state.citation_service` is present (always, in prod) and `False`
only when it is `None` — tests/offline overrides set it to `None` to exercise the
deterministic-only path. Cap-exceeded and transient-provider degradation are handled
inside the service/engine (`cap_exceeded`, `audit_unavailable` rows), not by the endpoint.

### 4.3 New `web/api/export.py` (APIRouter, mounted in `web/api/__init__.py`)
- `GET /api/workspaces/{workspace_id}/export.md`
  - `require_member`. `get_markdown_export`. 200 → `Response(bytes, media_type="text/markdown",
    headers={"Content-Disposition": f'attachment; filename="{safe_title(ws.title)}.md"'})`.
    404 (envelope `not_found`) if no draft.
- `GET /api/workspaces/{workspace_id}/export-composed.md`
  - `require_member`. `get_composed_export`. 200 → composed markdown,
    `…filename="{safe_title}-composed.md"`. 404 if no draft. On corrupt `citations.json`
    (`load_ledger` raises `ValueError`) → **409 `citation_ledger_invalid`**. On
    `BuildBibliographyBlocked` → **409** built with
    `api_error(409, "export_blocked", <human message>, detail={"blocked":
    [{"citekey": str, "reason": str}]})` — the blocked list rides in the envelope's
    existing optional `error.detail` field (see `web/api/errors.py:8,43`), **NOT** a new
    top-level `error.blocked`.
- **Binary/file 200 responses are exempt from the JSON envelope** (consistent with the
  C-media `/raw` and `/files/{aid}` serves). The error paths (404/409) ARE standard
  `ApiError` envelopes.

### 4.4 New `web/api/citation_audit.py` (APIRouter, mounted in `web/api/__init__.py`)
Response models (pydantic, emitted to `schema.gen.ts`):
```python
class AuditIssueOut(BaseModel):
    type: str
    severity: str            # "error" | "warning"
    citekey: str | None
    evidence_id: str | None
    location: str | None
    detail: str

class CitationAuditOut(BaseModel):
    draft_present: bool
    issues: list[AuditIssueOut]
    error_count: int
    warning_count: int

class DeepCheckOut(BaseModel):
    semantic_available: bool   # false only when app.state.citation_service is None (tests/offline)
    checked: int               # candidate citations the deep check considered; 0 when semantic unavailable
    cap_exceeded: bool         # the daily LLM cap is exceeded (read via check_citation_caps, NOT audit_unavailable presence)
    error_count: int           # over the merged (deterministic + semantic) issue list
    warning_count: int
    issues: list[AuditIssueOut]  # deterministic + semantic, merged
```
- `GET /api/workspaces/{workspace_id}/citation-audit`
  - `require_member`. Read-only (no LLM, no writes). Loads proposal.md + citations.json;
    if no draft → `{draft_present: false, issues: [], error_count: 0, warning_count: 0}`.
    On corrupt `citations.json` → **409 `citation_ledger_invalid`** (do NOT 500, and do
    NOT silently degrade like the Jinja path — a JSON client gets a clear code). Else runs
    `validate_citations_deterministic` and returns the issue list + counts.
- `POST /api/workspaces/{workspace_id}/citation-audit/deep`
  - **`require_owner`** (consumes LLM budget). On corrupt `citations.json` →
    **409 `citation_ledger_invalid`**. Runs deterministic first (for the
    `deterministic_issues` skip-set). If `request.app.state.citation_service is None`
    (tests/offline) → `{semantic_available: false, checked: 0, cap_exceeded: false,
    error_count, warning_count, issues: <deterministic only>}`. Else runs
    `validate_citations_semantic(… citation_service=app.state.citation_service,
    session_factory=app.state.session_factory, deterministic_issues=…)` (writes verdict
    cache; respects caps), returns the merged issues + counts + `checked` + `cap_exceeded`.
    `checked` = candidate citations the pass considered (`len` of evidence rows with an
    `id`), or `0` when `semantic_available` is false. `cap_exceeded` is read **precisely**
    from `check_citation_caps(session, workspace_id, settings=settings).exceeded` — NOT
    derived from the presence of an `audit_unavailable` issue, which the engine also emits
    for transient errors and invalid verdicts (`audit.py:626,660`), not only cap hits
    (`audit.py:638`). **Synchronous.**

**Synchronous tradeoff (accepted):** the engine judges per-citation synchronously; an
explicit owner action with a loading state is acceptable. Cap-exceeded degrades to
`audit_unavailable` issues + `cap_exceeded: true` (a UI banner), never a hard failure.
No background-job infra is introduced.

---

## 5. Frontend design

### 5.1 Citations bottom-panel tab
- `cockpit/nav.ts`: add `"citations"` to the `BottomTab` union.
- `cockpit/BottomPanel.tsx`: add the tab button (with an **error-count badge**) and the
  body mount `{activeTab === "citations" && <CitationsPanel id={id} onNavigate={onNavigate} />}`.
- New `cockpit/CitationsPanel.tsx`:
  - On mount, `useCitationAudit(id)` (GET) auto-runs the deterministic audit (cheap,
    read-only). Renders issues grouped by severity (error first), each row showing
    type · citekey · location · detail. Empty/`draft_present:false` → friendly empty state.
  - A **"Re-run"** button (refetch). A **"Deep check"** button shown only when
    `useCockpitMeta(id).data?.role === "owner"`; clicking calls `useDeepCheck(id)`
    (POST), shows a loading state, then merges semantic issues + a "checked N · flagged M"
    summary; `cap_exceeded` → a small banner. Viewers never see Deep check.
  - **Tab badge = deterministic `error_count`** (from the GET query). It is NOT updated by
    deep-check (deep-check is transient and not re-run on mount). After a deep-check the
    panel body shows the merged issues + the deep-check `error_count`/`warning_count`
    summary inline; the tab badge stays deterministic-only by design.

### 5.2 Export menu on the draft toolbar
- `cockpit/DraftPanel.tsx`: add a small toolbar row above `<ArtifactSectionEditor>` with
  an "Export ▾" Mantine `Menu` (Mantine v9 already in use). Items: **Markdown**,
  **Markdown + citations & bibliography**.
- New `api/export.ts` `downloadExport(workspaceId, kind)` helper — raw `fetch` with
  `credentials: "include"` to the `/api` export URL (openapi-fetch is awkward for binary):
  - 200 → `res.blob()` → save via a transient `<a download>` + `URL.createObjectURL`
    (native; no new dependency). Filename parsed from the response `Content-Disposition`
    header (readable on same-origin fetch; the backend already owns sanitization via
    `safe_title`), falling back to `proposal.md` / `proposal-composed.md` when the header
    is absent. `downloadExport(workspaceId, kind)` needs no title input.
  - 409 `export_blocked` → read `error.detail.blocked` and surface an inline message
    listing the blocked citekeys + reasons ("approve these references, then export").
  - 409 `citation_ledger_invalid` → "citations.json is corrupt — re-save citations."
  - 404 → "No draft to export yet."

### 5.3 Peek deep-link (lowest-priority task — trimmable if time-boxed)
- Audit rows carry `evidence_id`. "View source" navigates to `?tab=draft&ev=<id>`.
- `ArtifactSectionEditor.tsx` gains a small effect syncing its existing `peekEv` from the
  `ev` URL param (additive; pill-click behavior unchanged). Reuses the existing
  `useCitationSource` peek entirely — no new peek UI.
- If trimmed, audit rows still display citekey/location/detail; only the one-click jump
  is deferred. This task is independent and last.

### 5.4 New API hooks (`openapi-fetch` + `unwrap` pattern)
- `api/audit.ts`: `useCitationAudit(id)` (query), `useDeepCheck(id)` (mutation). Types
  from `components["schemas"]["CitationAuditOut" | "DeepCheckOut" | "AuditIssueOut"]`.
- `api/export.ts`: `downloadExport(...)` (plain fetch helper, not a hook).

---

## 6. Teardown (per-slice — EXPORT routes only)

Slice F's teardown is deliberately **narrow**: only the Jinja **export** routes are
deleted, because they are cleanly replaced by the React Export menu and are NOT invoked by
any live Jinja renderer. Delete (route functions only; prune imports they alone used):
- `GET /workspaces/{id}/export.md` in `proposal_assist/web/workspaces.py` (~:1348-1378).
- `GET /workspaces/{id}/export-composed.md` in `proposal_assist/web/citations.py` (~:174-256).
- The "Export markdown" / "Export with citations" links in `partials/artifact_pane.html`,
  and the now-dead export-composed link in `workspace_citations.html`.

**DEFERRED to the later `workspace_main` teardown sweep — do NOT delete in Slice F:** the
Jinja **citations page** (`GET /{id}/citations`), the **validate** route, the **citation
reader** route + `partials/citation_reader.html`, `workspace_citations.html`,
`partials/citation_audit_results.html`, and the `/citations` links in the four sub-nav
templates (`workspace_main.html:19`, `workspace_files.html:13`, `workspace_settings.html:13`,
`workspace_references.html:13`) + `partials/annotation_strip.html:38`.

Reason (verified): the citation-reader renderer
`citations/reader.py:render_proposal_with_citations` injects `/{id}/citations/{ev}/reader`
links and is still called by LIVE Jinja proposal rendering in `web/sections.py:110` and
`web/workspaces.py:169,179`; the citations page is also linked from the live
`annotation_strip.html`. Excising any of these now would break those renderers or leave
dangling links — that is the sweep's job. The React Citations tab and the (nav-dark) Jinja
citations page coexist harmlessly until then (strangler pattern).

Keep: the entire `citations/*` engine + bibliography + reader helpers, `_perms`,
`_session_dep`, the new `export.py` and the `citation_audit`/`export` `/api` modules.

**Re-run the OpenAPI regen AFTER deleting the two export routes** (Slice E gotcha): the
Jinja `export.md`/`export-composed.md` operationIds must drop out of `schema.gen.ts`,
leaving only the new `/api` export paths. A single pre-teardown regen would leave stale
paths. Regen: `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`; confirm
`frontend/src/routes.gen.ts` has an empty diff.

---

## 7. Testing

**Backend (targeted files only — never the whole suite):**
- `tests/.../test_api_export.py`: raw export 200 + filename header; composed 200 over a
  seeded approved-reference ledger; composed **409 `export_blocked`** with the blocked list
  under `error.detail.blocked` when a cited ref is unapproved; composed **409
  `citation_ledger_invalid`** on corrupt citations.json; member can read, viewer can read,
  non-member 403/404; no-draft 404.
- `tests/.../test_api_citation_audit.py`: deterministic GET returns expected issues over a
  seeded drifted/orphan ledger + correct counts; `draft_present:false` pre-draft; corrupt
  citations.json → **409 `citation_ledger_invalid`** (GET and deep-check); deep-check
  **403 for a viewer** (owner-gate); deep-check with `app.state.citation_service` set to a
  stub returns merged semantic issues + counts; cap-exceeded path → seed `citation_llm_usage`
  rows over the daily input cap so `check_citation_caps` reports exceeded → `cap_exceeded:true`;
  `semantic_available:false` + `checked:0` when `app.state.citation_service` is set to `None`.

**Frontend (vitest, `tsc` is the gate):**
- `CitationsPanel.test.tsx`: renders deterministic issues + severity grouping + badge;
  Deep-check button present for owner, absent for viewer (mock `useCockpitMeta`);
  deep-check merges semantic issues + summary; cap banner on `cap_exceeded`.
- `export.test.ts`: `downloadExport` triggers a blob save on 200; on 409 surfaces the
  blocked-refs list; on 404 the no-draft message. (Transport spies; mock `fetch`.)
- Peek deep-link: `ArtifactSectionEditor` opens peek when `?ev=` is set (if the task ships).

**OpenAPI regen** after backend (and again after teardown, per §6).

---

## 8. Out of scope (YAGNI)

- DOCX/PDF export (separate effort; new dependency).
- A standalone "all citations" table — the audit list + existing peek + select-to-cite
  composer already cover browse/inspect/add.
- Always-on semantic audit (deep check is explicit, owner-gated).
- Background-job audit (synchronous is acceptable for an explicit owner action).
- Deep query-invalidation wiring on every turn/edit — refetch-on-focus + a manual
  "Re-run" button suffice for v1.
- Any DB schema change.

---

## 9. Risks / notes

- **Long deep-check on large drafts.** Mitigation: explicit owner action + loading state;
  caps bound cost; cap-exceeded degrades gracefully. Acceptable for v1.
- **409 blocked list rides in `error.detail`.** The blocked-refs list uses the envelope's
  existing optional `detail` field (`error.detail.blocked`) via `api_error(...)` — no new
  envelope field is introduced. The UI reads it via a raw fetch (not `unwrap`), so the
  standard `ApiError` path is unaffected.
- **Corrupt `citations.json` is pinned to 409 `citation_ledger_invalid`** across all three
  new endpoints (deterministic GET, deep-check, composed export) — `load_ledger` raises
  `ValueError`; the endpoints catch it and return the envelope rather than 500ing.
- **Teardown is export-only.** Slice F deletes only the two Jinja export routes + their
  links and re-regens OpenAPI; the citations page/validate/reader are deferred to the
  `workspace_main` sweep (§6) because the reader renderer + page links are still invoked by
  live Jinja proposal rendering. This is a Codex-review correction to the original
  "delete the citations page + reader" scope.
- **No new citation-service builder.** Deep-check reuses `app.state.citation_service` (the
  supported `build_citation_service`) and `app.state.session_factory`; the MCP tool's own
  construction is untouched, so its behavior cannot regress.
