# Citation Source Viewer (proposal-assist) — Design

**Status:** approved design (brainstorm 2026-06-14), revised after Codex review
(2026-06-14). Ready for an implementation plan.
**Stack:** current — FastAPI + Jinja2 + HTMX, server-rendered, no SPA. Additive only.

## Goal

In the rendered proposal, make each citation clickable so the user can open the
cited reference and jump straight to the **exact passage** the claim came from —
with the surrounding context readable and scrollable. This completes the payoff
of the verbatim-evidence citation system already in place.

This is **Tier 1 (extracted text)**. The PDF-with-highlight viewer is an explicit
later tier (see Non-goals). It is the first concrete step toward the
proposal-workspace north-star: "work interactively with a proposal, all relevant
information close at hand — simple, intuitive, powerful, flexible" (memory:
`proposal-workspace-north-star`).

## Decisions (from the brainstorm)

1. **What opens:** the reference's **extracted text** (not the PDF). Reliable
   verbatim highlight, lightweight, ships first.
2. **Where it opens:** a **responsive reference drawer**. Desktop (≥1024px): the
   drawer **takes the chat column**, giving proposal + reference side-by-side at
   the existing ~50/50. Narrow (<1024px): a **full overlay** (modal dialog).
3. **What the reader shows:** **info-rich** header (source line, the **claim**
   this supports, a **stance** chip, **‹ prev · evidence i of N · next ›** across
   the reference's cited passages) + the reference's **full extracted text,
   scrollable**, with the evidence passage **highlighted and auto-scrolled to**.
4. **Citation marker style:** numbered `[n]`, per-reference, stable in order of
   first appearance across the whole proposal.
5. **Read-only.** No writes, no editing references from the reader.

## What already exists (build on, don't rebuild)

- **Evidence ledger** `citations.json` → `{evidence: [...], version}`. Each entry:
  `id` (`ev-…`), `citekey`/`citation_snapshot` (`ref-…`), `reference_id` (UUID),
  `locator{chunk_index, section_path}`, `evidence_quote` (verbatim, may contain
  `\n`), `evidence_quote_offsets` (chunk-relative `[start,end]`),
  `claim_context{text, hash}`, `stance`, `confidence`, `*_content_hash`.
- **Inline markers** in `proposal.md`: `[@citekey]<!-- ev:id -->`.
- **Renderer** `_render_proposal_markdown()`
  ([`web/workspaces.py:150`](../../../src/call_manager/proposal_assist/web/workspaces.py))
  — markdown → bleach allowlist. Renders `[@citekey]` as inert text and **strips
  the ev-comment**. Allowlist `_ALLOWED_ATTRS = {"a": ["href","title"]}`.
- **Per-section render** `_section_ctx()`
  ([`web/sections.py:86`](../../../src/call_manager/proposal_assist/web/sections.py))
  calls `_render_proposal_markdown(section.text)` **independently per section**
  (also on save/cancel fragments). ⇒ numbering must be computed proposal-wide and
  passed in, or it resets per section.
- **Export transform precedent** ([`web/citations.py`](../../../src/call_manager/proposal_assist/web/citations.py))
  already parses these markers + numbers a bibliography for export — reuse the
  marker-parsing/numbering logic; the `export.md` path itself is unchanged.
- **Reference storage:** `text.txt` (full extracted text) via
  `ReferenceStorage.text_path_for(...)`; `ReferenceChunk` rows
  (`reference_id, chunk_index, section_path, text`); `Reference` carries bib
  (`csl_json`/`title`/`citekey`), `text_status`, and **`tombstoned_at`**.
- **Tombstone policy** ([`references/deletion.py:82`](../../../src/call_manager/proposal_assist/references/deletion.py)):
  deleting a **cited** reference sets `tombstoned_at` and **retains bytes/chunks/
  metadata** so citations keep resolving. ⇒ the reader must render tombstoned
  sources (with a hint), never 404 them.
- **HTMX error wiring** ([`static/app.js`](../../../src/call_manager/web/static/app.js)):
  `htmx:responseError` turns any 4xx into a generic inline notice and does **not**
  swap the body into the target. ⇒ expected "missing/degraded" reader states must
  be **HTTP 200** fragments; 4xx is reserved for genuine errors.
- **JS test harness:** `tests/js/` (`vm`-based `app.js` wiring + `node --test`).

## Architecture & components (all additive)

### A. Citation render transform — proposal-wide numbering, post-sanitization injection

**Numbering map (computed once per request, proposal-wide).** Scan the live
markers in **`proposal.md`** in document order; assign `[n]` per distinct
`reference_id` on first appearance (repeat citations of a reference share `n`).
The map (`ev-id → n`, plus the ordered ev-id list per `reference_id` for
prev/next) is built once and **threaded into every render path** — the artifact
pane full render and each per-section / save-cancel fragment render
([`sections.py`](../../../src/call_manager/proposal_assist/web/sections.py)).
Markers whose ev-id is absent from the ledger, or ledger rows with no marker in
`proposal.md` (orphans), are excluded from the map.

**Injection AFTER sanitization (security-critical).** We do **not** add `class`
to the bleach allowlist (that would let agent-authored raw HTML forge its own
`a.cite`). Instead:
1. Pre-markdown: replace each *validated* marker with a collision-resistant
   plain-text sentinel (private-use codepoints, e.g. `CITE:{n}:{ev}`).
   Strip any pre-existing sentinel codepoints from the raw input first (defense
   in depth).
2. markdown + bleach run as today — the sentinel is inert text; any agent raw
   `<a class="cite" …>` loses its `class` (still not in the allowlist) and so can
   never be matched by the drawer JS.
3. Post-bleach: replace each sentinel with **trusted** markup
   `<a class="cite" href="/workspaces/{wid}/citations/{ev}/reader">[n]</a>`.

Result: only server-injected links carry `class="cite"`; agent input never forms
a URL or functioning marker. (Regression test: forged `<a class="cite">` in agent
markdown is neutralized.)

### B. Evidence locator (pure, independently testable)

`locate_evidence(full_text, cited_chunk, evidence) -> (start, end) | None`

Takes the workspace-scoped **cited `ReferenceChunk`** (looked up by
`reference_id` + `locator.chunk_index`) — the chunk text is the disambiguator.
Ladder:
1. Locate `cited_chunk.text` within `full_text` (exact, then whitespace-
   normalized) → the chunk's region. Apply the chunk-relative
   `evidence_quote_offsets` inside that region → the exact span. (Most precise;
   resolves duplicate quotes because it is anchored to *this* chunk.)
2. If the chunk can't be placed: search `evidence_quote` in `full_text` (exact →
   ws-normalized); accept **only if exactly one** match. **Collect all matches;
   if >1 remain ambiguous, return `None`.**
3. `None` is not a dead end: the route falls back to rendering the **chunk text
   itself** (always available), highlighted whole, with a "showing the cited
   section — couldn't pinpoint the exact sentence in the full document" note.

No persistence; offsets recomputed against `text.txt`/`cited_chunk` per request.

### C. Reader route

`GET /workspaces/{workspace_id}/citations/{evidence_id}/reader` → **HTML
fragment** (in [`web/citations.py`](../../../src/call_manager/proposal_assist/web/citations.py)),
beside the existing citations view.

- **Auth:** workspace owner **and** viewers (same dependency as the citations view).
- Loads: evidence entry (ledger) + `Reference` (bib + `tombstoned_at` +
  `text_status`) + cited `ReferenceChunk` + `text.txt`.
- **prev/next ordering** is derived from the **proposal.md marker order** for this
  `reference_id` (same scan as the numbering map), **not** the ledger. An orphan
  ev-id (in the ledger, no live marker) renders standalone with prev/next hidden.
  A reference with one cited passage hides prev/next.
- Header context: source line (`csl_json`/title), `claim_context.text`, `stance`,
  `index`/`total`, `prev_ev_id`/`next_ev_id`, plus a tombstone / stale hint when
  applicable.
- Body: HTML-escaped text with the located span wrapped in
  `<mark id="ev-target">…</mark>` (see oversized policy).
- Template: `proposal_assist/partials/citation_reader.html`.

**Status codes (must match the HTMX wiring):** **200** for every expected state
that should render in the drawer — found, locator-fallback, tombstoned,
`text_status != done`, oversized-windowed. **404 only** for an `evidence_id` not
in the ledger (forged/stale link) — it surfaces via the existing generic notice.

### D. Drawer + client wiring (with dialog semantics)

- **Container:** `#reference-drawer` in `workspace_main.html`, empty until loaded.
- **CSS** (`app.css`): occupies the chat column ≥1024px; `@media (max-width:1024px)`
  → fixed full overlay with backdrop. `mark` highlight uses the warn/amber tokens.
- **JS** (`app.js`, CSP-safe, delegated): intercept clicks on `a.cite` →
  `preventDefault` → load the href fragment into `#reference-drawer` → open →
  scroll `#ev-target` into view (centered). prev/next swap the fragment in place
  and re-scroll.
- **Accessibility (esp. the narrow overlay):** `role="dialog"`, `aria-modal="true"`,
  labelled by the source line; **move focus into the drawer on open** and **restore
  focus to the triggering `[n]` on close**; **trap focus** while the overlay is
  open; **lock background scroll** while the overlay is open; Esc and ✕ close.

## Data flow

```
click a.cite[href=/…/citations/{ev}/reader]
  → JS: load fragment into #reference-drawer, open, manage focus
  → route: ledger[ev] + Reference(bib,tombstone,status) + cited ReferenceChunk + text.txt
           locate_evidence(full_text, cited_chunk, ev)  →  <mark id=ev-target>  (or chunk fallback)
           prev/next + numbering from proposal.md marker order
           200 for found / fallback / tombstone / no-text / oversized; 404 only for unknown ev
  → JS: scroll #ev-target into view
```

All reads. Source of truth unchanged (git `citations.json` + DB + `text.txt`).

## Error / edge handling (all HTTP 200 fragments unless noted)

- **Unknown `evidence_id`** (not in ledger) → **404** (generic notice; forged/stale link).
- **`text_status != "done"`** → "source text not available yet" fragment.
- **Locator returns None** → chunk-text fallback + "couldn't pinpoint" note (B.3).
- **Tombstoned reference** (`tombstoned_at` set; bytes/chunks retained) → render
  normally with a subtle "removed from the library" hint.
- **Stale evidence** (`*_content_hash` mismatch vs current chunk) → render with a
  "source may have changed since this was cited" hint.
- **Oversized source:** if `text.txt` exceeds a cap (**`_MAX_READER_CHARS`, e.g.
  500 000 chars**), render a **window** around the evidence (the cited chunk ± a
  bounded neighborhood of `full_text`) instead of the entire document, with a
  "showing the cited section of a large document" note. Normal sources render in
  full.
- **Corrupt/unparseable `citations.json`** → degraded fragment ("citations
  unavailable"), logged; never a 500 in the drawer.
- **Missing `text.txt` / missing chunk** (`chunk_index` out of range) → treated as
  the no-text / locator-None fallbacks above (200), logged.

## Security

- ev-id validated against the ledger; unknown → inert text in the proposal, route 404.
- **`class` is NOT added to the bleach allowlist;** cite links are injected as
  trusted markup *after* sanitization (§A). Agent raw `<a class="cite">` is
  neutralized — forged-link regression test required.
- Reference text (arbitrary PDF content) is **HTML-escaped** before the `<mark>`.
- Reader URL built server-side from a validated id; workspace-scoped chunk lookup
  prevents cross-workspace reads. Viewer-role parity with the citations view.

## Testing

- **Numbering/transform** (pytest): proposal-wide numbering stable across multiple
  sections and the save/cancel fragment path; repeat citations share `n`; unknown
  ev-id → inert escaped text; **forged `<a class="cite">` in agent markdown is
  neutralized**; export path untouched.
- **Locator** (pytest, pure): chunk-anchored exact placement; ws-normalized chunk
  placement; duplicate-quote disambiguation via the chunk; ambiguous → None;
  None → chunk fallback.
- **Route** (pytest + TestClient): owner & viewer auth; fragment structure
  (`#ev-target`, header fields); prev/next from proposal.md order incl. ends and
  orphan/standalone; **200 for tombstone / no-text / oversized / locator-fallback;
  404 only for unknown ev**; HTML-escaping; **corrupt-ledger, missing text.txt,
  missing chunk, oversized-window** cases.
- **JS** (`tests/js/`, node --test): `a.cite` click loads + opens; scroll-to-
  target; prev/next swap; Esc/✕ close; **focus moves in on open, restores to the
  trigger on close, focus trap, background scroll lock**; responsive overlay vs
  column branch.

## Scope boundaries / non-goals

- **No PDF viewer** (Tier 2, later): real PDF via pdf.js + verbatim find-highlight.
- **No new DB tables/migrations; no writes; no editing references** from the reader.
- **No "all citations" index** beyond the existing citations ledger view.
- No change to `export.md` or the export bibliography behavior.

## File-by-file (for the plan)

- `src/call_manager/proposal_assist/citations/` — new `reader.py`: proposal-wide
  **numbering map** builder (scan `proposal.md` markers, shared with export
  numbering), sentinel transform helpers, and the pure `locate_evidence()`.
- `src/call_manager/proposal_assist/web/workspaces.py` — build the numbering map
  once; apply the post-sanitization citation transform in the artifact display
  render. (No `class` added to bleach.)
- `src/call_manager/proposal_assist/web/sections.py` — thread the same numbering
  map into `_section_ctx` / save-cancel fragment renders so numbers don't reset.
- `src/call_manager/proposal_assist/web/citations.py` — new reader route.
- `src/call_manager/web/templates/proposal_assist/partials/citation_reader.html`
  — reader fragment (header + `mark` body + hints), `role="dialog"`.
- `src/call_manager/web/templates/proposal_assist/workspace_main.html` —
  `#reference-drawer` container.
- `src/call_manager/web/static/app.js` — delegated `a.cite`, drawer open/close,
  focus management + trap + scroll lock, scroll-to-target, Esc.
- `src/call_manager/web/static/app.css` — responsive drawer + `mark` highlight.
- `tests/…` — as in Testing.
