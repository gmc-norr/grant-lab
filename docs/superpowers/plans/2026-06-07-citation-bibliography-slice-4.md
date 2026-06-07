# Citation Bibliography & Export (Slice 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`). Full two-stage review per task; holistic + full-suite gate at the end.

**Goal:** Turn a draft full of `[@citekey]` markers into a presentable document: a **fail-closed** `build_bibliography` that renders cited references' CSL-JSON into a formatted `bibliography.md`, a per-workspace citation-style setting, an owner waiver for unapproved metadata, and a **composed export** that transforms markers into inline citations + appends the bibliography (no leaked `<!-- ev:... -->`).

**Architecture / decisions (from spec + integration report):**
- **Render from `csl_json` (approved canonical) ONLY** — never `csl_json_extracted` (D10). The fail-closed gate ensures `csl_json` was verified before we render from it.
- **citekey→reference resolves via the LEDGER** (canonical: `find_by_citekey(ledger, citekey) -> row["reference_id"]` → `session.get(Reference, …)`), NOT the `references.citekey` DB cache (which can be stale) (D16).
- **Fail-closed (D11):** `build_bibliography` REFUSES if any cited reference's `metadata_status` ∉ {approved, verified_external} AND has no owner waiver. No partial render.
- **Formatter: pure-Python** for the two v1 styles (Vancouver numbered default; author-year). No `citeproc-py`/pandoc dependency (spec defers custom CSL styles; "citeproc" = render-from-CSL-JSON directive). Interface stays swappable.
- **`bibliography.md` is a separate git artifact** — NEVER appended into proposal.md (D13). Composition is a separate export action.
- Migration head is **0039** → Slice 4 adds **0040**.

**Tech Stack:** SQLAlchemy/Alembic, pure-Python formatter, `citations/markers.py` (MARKER_RE/find_markers), `citations/ledger.py` (find_by_citekey/load_ledger), `citations/gitio.py` (write_and_commit_files), FastAPI/Jinja/HTMX, the MCP tool registry. pytest.

**Key existing pieces:**
- Render/export: `web/workspaces.py` `_render_proposal_markdown` (~L131; markdown+bleach, strips the HTML comment in the rendered VIEW but the **raw `/export.md` at ~L1229 leaks it**). `export_markdown` serves raw `proposal.md` bytes.
- `citations/markers.py`: `MARKER_RE` matches `[@<citekey>]<!-- ev:<id> -->`; `find_markers(text) -> [{citekey, ev_id, start, end}]`.
- `citations/ledger.py`: `load_ledger`, `find_by_citekey`.
- `citations/audit.py`: `_APPROVED_METADATA = {"approved","verified_external"}` (reuse).
- `citations/gitio.py`: `write_and_commit_files(repo_dir, {path: content}, message)`.
- `db/models.py`: `Reference{csl_json, csl_json_extracted, citekey, metadata_status, tombstoned_at}`; `Workspace` uses discrete setting columns (no JSON blob).
- `references/csl.py`: ingestion + `generate_citekey` (alpha-suffix disambiguation: smith2021/smith2021a). NO formatting yet.
- `web/citations.py`: the Slice-A citations router (citations_panel + run_validation) — add bibliography/export routes here.
- `tools/audit.py` + registry: where the `build_bibliography` MCP tool wrapper goes (allowlist → 23).
- Settings: `web/templates/proposal_assist/workspace_settings.html` + `post_settings` (~workspaces.py L1130, allowlist-validates `status`).

---

## File Structure
- `alembic/versions/0040_bibliography_settings.py` — **new**: `workspaces.citation_style`, `references.metadata_waiver` + `metadata_waiver_reason`.
- `src/call_manager/db/models.py` — **modify**: the three columns.
- `src/call_manager/proposal_assist/citations/bibliography.py` — **new**: pure formatter (`format_vancouver`, `format_author_year`, `render_bibliography`) + `build_bibliography(...)` fail-closed orchestration + `compose_export(...)` marker→inline transform. `BuildBibliographyBlocked`.
- `src/call_manager/proposal_assist/tools/audit.py` — **modify**: `build_bibliography_impl(ctx, *, style=None)` MCP tool.
- `src/call_manager/proposal_assist/tools/registry.py` + `drafting/claude_adapter.py` — **modify**: register `build_bibliography` (+ allowlist).
- `src/call_manager/proposal_assist/web/workspaces.py` — **modify**: `citation_style` in settings route.
- `src/call_manager/web/templates/proposal_assist/workspace_settings.html` — **modify**: style `<select>`.
- `src/call_manager/proposal_assist/web/citations.py` — **modify**: `POST .../citations/bibliography` (build) + `GET .../export-composed.md` (compose). Waiver `POST .../references/{id}/waiver`.
- Templates: `workspace_citations.html` (build button + bibliography preview), reference row (waiver control), composed-export link.
- Tests under `tests/proposal_assist/`.

---

### Task 1 — migration 0040 + model columns
**Files:** `db/models.py`; new `alembic/versions/0040_bibliography_settings.py`; new migration test.
- `Workspace.citation_style`: Text NOT NULL server_default `'vancouver'`, CHECK `citation_style IN ('vancouver','author-year')` (name `ck_workspaces_citation_style`).
- `Reference.metadata_waiver`: Boolean NOT NULL server_default `false`. `Reference.metadata_waiver_reason`: Text nullable.
- Migration 0040 (down_revision="0039") adds all three; downgrade drops them. Mirror the 0038/0039 style.
- Tests: migration round-trip (head→0039→head); model round-trip (set/read the columns); CHECK rejects an invalid style.
- Commit `feat(bib): citation_style + metadata_waiver columns (migration 0040)`.

### Task 2 — citation_style settings UI + route
**Files:** `workspace_settings.html`; `web/workspaces.py` `post_settings`; web test.
- Add a `<select name="citation_style">` (options vancouver / author-year) defaulting to the workspace's current value, in the settings form (owner-only, as the rest of the form is).
- `post_settings`: add `citation_style: str = Form("vancouver")`; allowlist-validate against {vancouver, author-year} (reject → 400 / re-render with error, mirroring the `status` validation); persist on the workspace.
- Tests: owner POST persists the style + GET reflects it; invalid style rejected; non-owner can't change it.
- Commit `feat(bib): per-workspace citation style setting`.

### Task 3 — pure-Python CSL formatter
**Files:** new `citations/bibliography.py` (formatter part); new `tests/proposal_assist/citations/test_bibliography_format.py`.
- `format_author_list(authors: list[dict], *, style) -> str` — handle `family`/`given`/`literal`, et-al for many authors (e.g. >6 → first 6 + "et al." for Vancouver), missing → "".
- `format_vancouver(csl: dict, number: int) -> str` → `"[N] Smith J, Jones A. Title. Container. 2021;27(4):511-520. doi:10.x"` (omit absent fields gracefully; no trailing-punctuation mess).
- `format_author_year(csl: dict, citekey: str) -> str` → `"Smith, J., & Jones, A. (2021a). Title. Container, 27(4), 511-520. https://doi.org/10.x"` — carry the disambiguation SUFFIX from the citekey (parse trailing `[a-z]` after the year, e.g. `smith2021a` → "2021a").
- `render_bibliography(entries: list[tuple[citekey, csl]], style: str) -> str` → the full `bibliography.md` text: Vancouver = numbered in the given (document) order; author-year = alphabetical by formatted entry. A `# References` heading.
- No external deps. Pure functions. Tests: vancouver numbering; author-year alpha + suffix carry; >6 authors et-al; missing title/container/doi degrade; unicode names; empty → just heading or "".
- Commit `feat(bib): pure-Python Vancouver + author-year CSL formatter`.

### Task 4 — `build_bibliography` fail-closed orchestration
**Files:** `citations/bibliography.py` (orchestration); new `tests/proposal_assist/citations/test_build_bibliography.py`.
- `class BuildBibliographyBlocked(Exception)` carrying `blocked: list[dict]` (citekey, reference_id, metadata_status).
- `build_bibliography(*, proposal_text, ledger, session, workspace_id, style, repo_dir, commit=True) -> dict`:
  1. `markers = find_markers(proposal_text)`; unique citekeys in DOCUMENT ORDER (first-occurrence order — needed for Vancouver numbering).
  2. For each citekey: resolve `reference_id` via `find_by_citekey(ledger, citekey)` (canonical). If no ledger row → blocked entry `unresolved_citekey`. Else `ref = session.get(Reference, uuid)` workspace-scoped; if missing/tombstoned → blocked. 
  3. **Fail-closed gate:** if `ref.metadata_status not in _APPROVED_METADATA` AND not `ref.metadata_waiver` → add to `blocked`.
  4. If `blocked` non-empty → raise `BuildBibliographyBlocked(blocked)` (NO partial write).
  5. Else collect `(citekey, ref.csl_json or {})` in document order, `render_bibliography(...)`, and if `commit`: `write_and_commit_files(repo_dir, {"bibliography.md": text}, "cite: build bibliography")`. Return `{"bibliography_md": text, "citekey_count": n, "commit_sha": sha|None}`.
  - Never appended to proposal.md. Defensive on bad rows (`.get()`).
- Tests: all-approved → writes bibliography.md (one commit) with all cited keys, only marker-present keys (an uncited approved ref is NOT included); one `extracted` ref → BuildBibliographyBlocked listing it, NO write; waiver on that ref → succeeds; tombstoned cited ref → blocked; unresolved citekey → blocked; style arg drives format; `commit=False` returns text without writing.
- Commit `feat(bib): fail-closed build_bibliography (ledger-resolved, waiver-aware)`.

### Task 5 — `build_bibliography` MCP tool
**Files:** `tools/audit.py`; `tools/registry.py`; `claude_adapter.py`; web/test.
- `async def build_bibliography_impl(ctx, *, style=None) -> dict`: read proposal.md + ledger (reuse `_read_proposal_and_ledger`); resolve effective style = `style` or the workspace's `citation_style` (load the Workspace row); `async with`? no lock needed beyond reads + one git commit — but it WRITES bibliography.md via gitio, so guard with the same `ctx.write_lock` the mutation tools use (intra-turn serialization). Call `build_bibliography(...)`; on `BuildBibliographyBlocked` → `{"error": "bibliography blocked: unapproved/unresolved references", "blocked": [...]}`; success → `{"bibliography_md", "citekey_count", "commit_sha"}`.
- Register `build_bibliography` in `_collect_tools` (schema: optional `style` enum) + `allowed_tools` (→ 23). Update wiring-test counts.
- Add to `_CITATION_INSTRUCTION`? Optionally mention "when the draft is ready, call build_bibliography to render the reference list." (small append; update its test.)
- Tests: blocked path → error+blocked; approved path → writes + returns; style override; registered + allowlisted.
- Commit `feat(bib): build_bibliography MCP tool`.

### Task 6 — composed export (marker→inline transform + append bibliography)
**Files:** `citations/bibliography.py` (`compose_export`); `web/citations.py` (route); export-link template; tests.
- `compose_export(*, proposal_text, ledger, session, workspace_id, style, bibliography_text|None) -> str`:
  - Build the same ordered unique-citekey list + resolve to refs (reuse build_bibliography's resolution; for export, transform even if some refs unapproved? NO — composed export should reflect the SAME fail-closed contract; if blocked, the route surfaces the block. Decision: compose_export requires a successfully-built bibliography — the route builds first, then composes).
  - Vancouver: assign each unique citekey a number in document order; replace each `[@citekey]<!-- ev:id -->` (via MARKER_RE, two-pass: number map then sub) with `[N]`. Author-year: replace with `(Family Year[suffix])` derived from the ref's csl_json + citekey suffix.
  - Strip ALL ev-comments (the MARKER_RE sub removes the whole marker span, replacing with the inline form — so no comment leaks).
  - Append `\n\n` + bibliography_text (if provided).
  - Return the composed markdown string.
- `GET /{workspace_id}/export-composed.md` in web/citations.py (owner-or-viewer): read proposal.md + ledger + workspace style; run `build_bibliography(commit=False)` → on Blocked return a 409/HTML error listing blocked refs (do NOT serve a half-composed doc); else `compose_export(...)` + serve as `text/markdown` attachment `{safe_title}-composed.md`. (No ev-comment leak; markers become inline citations.)
- Add an "Export composed" link (next to the existing export.md link — find `artifact_pane.html`/the export link) AND on the citations page.
- Tests: composed output has NO `[@`/`<!-- ev:` (markers transformed), Vancouver numbers in document order (repeated citekey → same number), author-year inline form + suffix, bibliography appended, blocked refs → error not a leaked doc.
- Commit `feat(bib): composed export (inline citations + bibliography, no marker leak)`.

### Task 7 — per-reference waiver UI + route
**Files:** `web/references.py` (or citations.py) waiver route; reference row template; tests.
- `POST /workspaces/{workspace_id}/references/{reference_id}/waiver` (owner-only): toggle/set `metadata_waiver` (+ optional `metadata_waiver_reason` from form); record a `WorkspaceEvent` (audit trail, mirror existing event usage). Return the updated reference row partial (HTMX) or redirect.
- Reference row: for an owner viewing a non-approved ref, show a "Waive metadata gate" control + the current waiver state; approved refs don't need it.
- Tests: owner sets waiver → persisted + event recorded + build_bibliography then succeeds for that ref; viewer cannot set; clearing waiver re-blocks.
- Commit `feat(bib): per-reference metadata waiver`.

### Task 8 — holistic review + full-suite gate
- Holistic pass over Slice 4 (does the build→compose→export flow cohere; fail-closed honored in BOTH the tool and the export route; markers never leak; style setting flows through). Fix findings.
- `uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/`; `uv run pytest -q`. Fix cross-file regressions (tool counts, nav, migration chain). Report counts.
- Commit `test(bib): Slice 4 gate`.

---

## Trickiest parts (call out to implementers)
1. **Marker→inline transform (T6)** must be two-pass: first assign each unique citekey a number in DOCUMENT ORDER (repeated citekey → same number), then `MARKER_RE.sub` replacing the WHOLE marker span with the inline form — so no `[@`/comment leaks and surrounding markdown isn't corrupted (the marker may abut text with no space). Author-year carries the citekey's trailing `[a-z]` suffix into the rendered `(Smith 2021a)`.
2. **Fail-closed is enforced in TWO places** (the `build_bibliography` tool AND the `export-composed` route) using the SAME gate (metadata_status ∈ approved/verified_external OR waiver). Export must NEVER serve a half-composed document when blocked — it returns an error listing the blocked references. Reuse the orchestration, don't duplicate the gate logic.
3. **Resolve via the ledger, not the DB citekey cache** — the cache can be stale; the ledger row's `reference_id` is canonical and consistent with the markers at the current commit.
4. **csl_json only** (approved canonical), never csl_json_extracted, for rendering.

## Self-review checklist
- bibliography.md is separate; never appended into proposal.md. ✓
- Only marker-present citekeys rendered (uncited refs excluded). ✓
- Fail-closed + waiver in both tool and export route, same gate. ✓
- Composed export: zero `[@`/`<!-- ev:` leakage; numbering/author-year correct. ✓
- Migration 0040 reversible + round-trip test. ✓
- No new heavy dependency (pure-Python formatter). ✓
