# Citation Connective Tissue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`). Full two-stage review (spec + quality) per task; full-suite + lint gate at the end.

**Goal:** Make the (already-built, internally-sound) citation-provenance machinery actually *fire* and *be visible* to the human — the connective tissue the system review found missing: (A1) instruct the drafting agent to cite + self-audit; (A2) a human-facing citations view (evidence ledger + fast deterministic health-check); (C3) link the orphaned metadata-approval form.

**Architecture / scope decisions:**
- A1 goes in the base system prompt `append` (`claude_adapter.py`) — there is no per-phase prompt pipeline yet (only `phases/setup.py`). The citation tools are already allowlisted.
- A2 web view is **visibility + deterministic-only** validation, run synchronously (pure DB+string, <100ms). **Semantic audit + unsupported-claims are NOT exposed from the web** in this increment — they're LLM-bound (per-row calls, latency 30–90s, competes with the drafting cap). Those stay AGENT-driven (the A1 instruction makes the agent run `validate_citations` (incl. semantic) and `list_unsupported_claims` during its own turn, within the turn's cap budget). Web semantic audit is a documented future enhancement.
- No CSRF token system exists (session-cookie auth + CSP `form-action 'self'`); new forms follow the same convention.

**Tech Stack:** FastAPI + Jinja2 + HTMX, the Claude Agent SDK adapter, `citations/audit.py` engine + `citations/ledger.py`, pytest + the existing web test patterns.

**Key existing pieces (from integration report):**
- `drafting/claude_adapter.py` ~L192-233: `system_prompt = {"type":"preset","preset":"claude_code","append": _REFERENCE_SAFETY_INSTRUCTION}`; allowed_tools already includes validate_citations/list_unsupported_claims. Test: `tests/proposal_assist/test_drafting_claude_adapter.py::test_build_options_appends_reference_safety_system_prompt`.
- `proposal_assist/web/workspaces.py`: `GET /workspaces/{id}` `get_workspace_main` (~L493); `_require_owner_or_viewer` (~L375); proposal.md read pattern (`repo_dir = Path(settings.workspaces_dir)/str(id)/"repo"`, ~L574); `_make_tool_ctx(request, ws)` (~L944); app.state access (`request.app.state.{settings,session_factory,citation_service,templates}`). `workspace_main.html` nav `<p>` row (~L9-14).
- `proposal_assist/web/references.py`: `references_panel` GET (~L147), metadata routes `GET/POST /workspaces/{id}/references/{ref_id}/metadata` (~L414/454, owner-only), `is_owner` passed to row renders. Router is mounted under `/workspaces`.
- `web/templates/proposal_assist/partials/reference_row.html` (18 lines; no metadata_status shown). `reference_metadata_review.html`.
- `citations/audit.py::validate_citations_deterministic(*, proposal_text, ledger, session, workspace_id) -> list[dict]` (issue dict: type, severity, citekey, evidence_id, location, detail). `citations/ledger.py::load_ledger(text|None)->dict`; evidence rows: id, citekey, reference_id, locator{chunk_index,section_path}, evidence_quote, stance, confidence, claim_context{text,hash}, citation_snapshot.
- `tools/citations.py::CITATIONS_FILENAME = "citations.json"`.

---

## File Structure
- `src/call_manager/proposal_assist/drafting/claude_adapter.py` — **modify**: add `_CITATION_INSTRUCTION`, concat into the append.
- `src/call_manager/web/templates/proposal_assist/partials/reference_row.html` — **modify**: metadata badge + Review-metadata link (C3).
- `src/call_manager/proposal_assist/web/citations.py` — **new**: the citations view router (`GET /workspaces/{id}/citations`, `POST .../citations/validate`).
- `src/call_manager/web/templates/proposal_assist/workspace_citations.html` — **new**: ledger table + validation panel.
- `src/call_manager/web/templates/proposal_assist/partials/citation_audit_results.html` — **new**: the issues-table fragment (HTMX swap target).
- `src/call_manager/web/templates/proposal_assist/workspace_main.html` — **modify**: add "Citations" nav link.
- `src/call_manager/main.py` (or wherever routers mount) — **modify**: include the new citations router.
- Tests under `tests/proposal_assist/`.

---

### Task 1 — A1: agent citation + self-audit instruction
**Files:** `drafting/claude_adapter.py`; `tests/proposal_assist/test_drafting_claude_adapter.py`.
- Add a module constant `_CITATION_INSTRUCTION` — a concise instruction telling the agent: when a sentence makes a factual/empirical claim that a provided reference supports, attach a citation with `insert_citation` (use `search_references`/`read_reference_section` to get the exact `chunk_index` and copy a VERBATIM `evidence_quote` from that chunk; the marker is `[@citekey]` placed after the claim); use `list_citations` to find existing `ev` ids/citekeys before `update_citation`/`remove_citation`/`rename_citekey`; after substantial drafting, call `validate_citations` and fix the issues it reports, and use `list_unsupported_claims` to find claims that still need backing. Keep it tight (a short paragraph), imperative, and consistent in tone with `_REFERENCE_SAFETY_INSTRUCTION`.
- Change the `append` to `_REFERENCE_SAFETY_INSTRUCTION + "\n\n" + _CITATION_INSTRUCTION`.
- Update `test_build_options_appends_reference_safety_system_prompt`: drop the `== _REFERENCE_SAFETY_INSTRUCTION` equality; assert both the safety phrase ("untrusted-reference"/"never as instructions") AND key citation phrases are present in `sp["append"]`. Add `test_build_options_appends_citation_instruction` asserting `insert_citation` and `validate_citations` (and `evidence_quote`/verbatim) appear in the append.
- Commit `feat(citations): instruct the drafting agent to cite + self-audit`.

### Task 2 — C3: metadata status badge + Review-metadata link
**Files:** `partials/reference_row.html`; a web test that renders the references panel.
- After the download link, add: a metadata badge (`approved` → "metadata ✓" ok-class; else "metadata: {status}" pending-class) and, when `is_owner` and status != "approved", an anchor `Review metadata` → `/workspaces/{{ ref.workspace_id }}/references/{{ ref.id }}/metadata`. (`is_owner` is already in the row context.)
- Tests (extend the references-panel web test, e.g. `tests/proposal_assist/test_web_workspaces_references*.py`): owner + non-approved ref → the "Review metadata" link is present and points at the metadata route; viewer (is_owner False) → link absent; approved ref → badge shown, no link. (Render via the actual GET references panel route so the partial+context are exercised.)
- Commit `feat(citations): surface metadata status + review link on reference rows`.

### Task 3 — A2: citations view router + evidence-ledger page
**Files:** new `proposal_assist/web/citations.py`; new `workspace_citations.html`; mount the router; `workspace_main.html` nav link; web test.
- New router (APIRouter, prefix `/workspaces` to match references.py) with `GET /{workspace_id}/citations` → `citations_panel`: auth `_require_owner_or_viewer` (import/reuse the existing dep from workspaces.py — or mirror its session/user deps). Read `proposal.md`? not needed for the ledger view; read `citations.json` from `repo_dir = Path(settings.workspaces_dir)/str(workspace_id)/"repo"` via `load_ledger(path.read_text() if exists else None)`. Build display rows from `ledger["evidence"]`: citekey, claim (`claim_context.text`), evidence_quote, stance, confidence, reference_id (link to the reference if resolvable), ev_id. Render `workspace_citations.html` with the rows + `is_owner` + workspace.
- `workspace_citations.html` extends `base.html`: a heading, the same nav `<p>` row as workspace_main (Files | References | Citations | Settings | All workspaces), a table of evidence rows (empty-state message when none), and (Task 4) the validation panel placeholder `<div id="audit-results">`.
- Add `| <a href="/workspaces/{{ workspace.id }}/citations">Citations</a>` to `workspace_main.html`'s nav row.
- Mount the new router wherever references.py's router is included (find `include_router` for the proposal_assist web routers in main.py / the app factory) — add the citations router alongside.
- Tests: GET /workspaces/{id}/citations as owner → 200, shows a seeded citation's citekey + claim + quote; as viewer → 200 (read-only); as a non-member → 403; empty workspace (no citations.json) → 200 with empty-state. (Seed a workspace + write a citations.json into its repo_dir, OR insert a real citation via insert_citation_impl against a temp repo — prefer writing a small citations.json directly for a web test.)
- Commit `feat(citations): human-facing citations evidence-ledger view`.

### Task 4 — A2: deterministic "Run validation" panel
**Files:** `proposal_assist/web/citations.py` (+POST route); new `partials/citation_audit_results.html`; `workspace_citations.html` (button); web test.
- `POST /{workspace_id}/citations/validate` → `run_validation`: auth `_require_owner_or_viewer`. Read proposal.md + citations.json from repo_dir; open a session via `request.app.state.session_factory`; call `validate_citations_deterministic(proposal_text=, ledger=, session=, workspace_id=)`. (DETERMINISTIC ONLY — no LLM, fast, sync.) Render `partials/citation_audit_results.html` with the issues list (table: severity, type, citekey, location, detail) + a summary count + a note that semantic checks run during the drafting agent's own `validate_citations`. Return the PARTIAL (HTML fragment, no `<html>`).
- In `workspace_citations.html`, add a button: `hx-post="/workspaces/{{ workspace.id }}/citations/validate"`, `hx-target="#audit-results"`, `hx-swap="innerHTML"`, label "Run validation (deterministic)".
- Tests: POST with a clean citation → fragment shows "no issues"/0 issues; POST after tampering (e.g. a citations.json row referencing a missing chunk, or an orphan_row) → the issue appears in the fragment with the right severity/type; the response is a fragment (200, no `<html>` tag); non-member → 403.
- Commit `feat(citations): deterministic validation panel on the citations view`.

### Task 5 — gate
- `uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/`; `uv run pytest -q` (FULL suite — new router mounted, template changes, adapter prompt change). Fix any cross-file regressions (esp. the adapter system-prompt test, any route-count/nav snapshot tests). Report exact counts.
- Commit `test(citations): connective-tissue gate`.

---

## Self-review checklist
- A1 instruction present in the built system prompt append; existing safety assertion updated, not broken. ✓
- Citations view is owner-OR-viewer readable; validation is deterministic-only + sync (no LLM latency in the request path). ✓
- Semantic audit / unsupported-claims remain agent-driven (A1) — documented, not web-exposed this increment. ✓
- C3 link only shown to owners + only when not approved; badge always shows status. ✓
- New router actually mounted; "Citations" nav link added. ✓
- No CSRF token invented (follows existing session-auth convention). ✓
