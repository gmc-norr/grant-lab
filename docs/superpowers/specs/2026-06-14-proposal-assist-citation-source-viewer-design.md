# Citation Source Viewer (proposal-assist) — Design

**Status:** approved design (brainstorm 2026-06-14), ready for an implementation plan.
**Date:** 2026-06-14
**Stack:** current — FastAPI + Jinja2 + HTMX, server-rendered, no SPA. Additive only.

## Goal

In the rendered proposal, make each citation clickable so the user can open the
cited reference and jump straight to the **exact passage** the claim came from —
with the surrounding context readable and scrollable. This completes the payoff
of the verbatim-evidence citation system already in place.

This is **Tier 1 (extracted text)**. The PDF-with-highlight viewer is an
explicit later tier (see Non-goals).

It is the first concrete step toward the proposal-workspace north-star:
"work interactively with a proposal, all relevant information close at hand —
simple, intuitive, powerful, flexible" (memory: `proposal-workspace-north-star`).

## Decisions (from the brainstorm)

1. **What opens:** the reference's **extracted text** (not the PDF). Reliable
   verbatim highlight, lightweight, ships first.
2. **Where it opens:** a **responsive reference drawer**.
   - Desktop (≥1024px): the drawer **takes the chat column** (you are reading a
     source, not chatting), giving proposal + reference side-by-side at the
     existing ~50/50 — proposal stays visible for claim↔evidence glancing.
   - Narrow (<1024px, already single-column): the drawer becomes a **full
     overlay**. One responsive design, no separate UI.
3. **What the reader shows:** **info-rich**, all from data we already store:
   - header: source line (authors/year/title), the **claim** this supports,
     a **stance** chip (supports/contradicts), and **‹ prev · evidence i of N ·
     next ›** to step through this reference's other cited passages;
   - body: the reference's **full extracted text, scrollable**, with the
     evidence passage **highlighted and auto-scrolled into view**.
4. **Citation marker style:** numbered `[n]` (per-reference numbering, stable in
   order of first appearance).
5. **Read-only.** No writes, no editing of references from the reader.

## What already exists (build on, don't rebuild)

- **Evidence ledger** `citations.json` → `{evidence: [...], version}`. Each entry:
  `id` (`ev-…`), `citekey`/`citation_snapshot` (`ref-…`), `reference_id` (UUID),
  `locator{chunk_index, section_path}`, `evidence_quote` (verbatim, may contain
  `\n` from PDF extraction), `evidence_quote_offsets` (chunk-relative `[start,end]`),
  `claim_context{text, hash}`, `stance`, `confidence`, `*_content_hash` (staleness).
- **Inline markers** in `proposal.md`: `[@citekey]<!-- ev:id -->`.
- **Renderer** `_render_proposal_markdown()`
  ([`web/workspaces.py`](../../../src/call_manager/proposal_assist/web/workspaces.py))
  — markdown → bleach allowlist. Today it renders `[@citekey]` as inert literal
  text and **strips the `<!-- ev:id -->` comment**, so the ev-id is currently lost
  in the rendered artifact. Allowlist: `_ALLOWED_TAGS`, `_ALLOWED_ATTRS = {"a":
  ["href","title"]}`.
- **Export transform precedent**
  ([`web/citations.py`](../../../src/call_manager/proposal_assist/web/citations.py))
  already rewrites `[@citekey]<!-- ev:id -->` markers into rendered citations +
  bibliography for export — reuse its marker-parsing/numbering logic.
- **Citations ledger view** `GET /workspaces/{wid}/citations` — read-only view of
  the same ledger; the new reader route lives beside it.
- **Reference storage:** `text.txt` (full extracted text) via
  `ReferenceStorage.text_path_for(...)`; `ReferenceChunk` rows
  (`chunk_index, section_path, text`); the `Reference` row carries bib metadata
  (`csl_json`/`title`/`citekey`) and `text_status`.
- **Existing JS harness:** `tests/js/` with a `vm`-based `app.js` wiring test +
  `node --test` (used by the dedup/auto-scroll work).

## Architecture & components (all additive)

### A. Citation render transform
A server-side step that converts each `[@citekey]<!-- ev:id -->` marker into a
clickable, numbered link **before** bleach runs, so the ev-id survives:

```
[@ref-31e62…]<!-- ev:ev-d7da… -->   →   <a class="cite" href="/workspaces/{wid}/citations/ev-d7da…/reader">[3]</a>
```

- **Numbering:** assign `[n]` per distinct `reference_id` in order of first
  appearance in the document (reuse the export numbering logic). Repeat citations
  of the same reference share the number.
- **Validation:** only transform markers whose ev-id exists in the ledger. An
  unknown / malformed ev-id is left as **inert escaped text** (no link). The
  agent controls only the ev-id string; the URL is built server-side from a
  validated id, so there is no injection surface.
- **Bleach:** add `class` to the allowed attributes for `<a>` (`href`/`title`
  already allowed). Encoding the ev-id in the **href path** means no other new
  attributes are needed.
- Runs wherever the artifact is rendered for display (the artifact pane and its
  HTMX refresh path). The raw `export.md` path is unchanged.

### B. Evidence locator (pure, independently testable)
`locate_evidence(full_text, evidence) -> (start, end) | None`

Finds the verbatim evidence span within the reference's full extracted text.
**Fallback ladder:**
1. Exact substring match of `evidence_quote` in `full_text`.
2. Whitespace-normalized match (collapse runs of whitespace/newlines on both
   sides; map back to original offsets) — handles PDF newline noise.
3. Narrow by `locator.section_path` / `chunk_index` to disambiguate when the
   quote appears more than once.
4. **Not found →** return `None`; the route then shows the located **chunk
   text** (`ReferenceChunk` for `chunk_index`) with a "couldn't pinpoint the
   exact sentence — showing the cited section" note. Never a dead end.

No new persistence: offsets are recomputed against `text.txt` at request time.

### C. Reader route
`GET /workspaces/{workspace_id}/citations/{evidence_id}/reader` →
`HTMLResponse` **fragment** (not a full page), in
[`web/citations.py`](../../../src/call_manager/proposal_assist/web/citations.py).

- **Auth:** workspace owner **and** viewers (same dependency as the citations
  view).
- Loads: evidence entry (ledger) + `Reference` (bib) + `text.txt`.
- Builds header context (source line, claim, stance, `prev_ev_id` / `next_ev_id`
  / `index` / `total` computed by filtering the ledger to this `reference_id`,
  ordered by document position) and the body: HTML-escaped full text with the
  located span wrapped in `<mark id="ev-target">…</mark>`.
- prev/next links point at the reader route for the sibling evidence ids.
- Template: `proposal_assist/partials/citation_reader.html`.

### D. Drawer + client wiring
- **Container:** a `#reference-drawer` element in `workspace_main.html` (empty
  until loaded).
- **CSS** (`app.css`): drawer occupies the chat column on desktop; `@media
  (max-width:1024px)` → fixed full overlay with a backdrop. Highlight `mark`
  styling (reuse the warn/amber tokens).
- **JS** (`app.js`, CSP-safe, delegated): intercept clicks on `a.cite` →
  `preventDefault` → load the href fragment into `#reference-drawer` (htmx.ajax)
  → open the drawer → scroll `#ev-target` into view (centered). Esc and the ✕
  close the drawer and restore the chat column. prev/next swap the fragment
  in place and re-scroll.

## Data flow

```
click a.cite[href=/…/citations/{ev}/reader]
  → JS: load fragment into #reference-drawer, open drawer
  → route: ledger[ev] + Reference(bib) + text.txt
           locate_evidence(full_text, ev)  →  <mark id=ev-target>
           header: claim, stance, source line, prev/next of this reference
  → JS: scroll #ev-target into view
```

All reads. Source of truth unchanged (git `citations.json` + DB + `text.txt`).

## Security
- ev-id validated against the ledger; unknown → inert text + route 404.
- Reference text (arbitrary PDF content) is **HTML-escaped** before the `<mark>`
  is inserted; the fragment is otherwise our own trusted markup.
- Reader URL constructed server-side from the validated id — agent input never
  forms a URL or markup directly.
- Viewer-role parity with the existing citations view (no privilege widening).

## Error / edge handling
- `text_status != "done"` (no extracted text yet) → reader shows a clean
  "source text not available yet" notice (no highlight attempt).
- Evidence quote not locatable → chunk-level fallback with a note (locator B.4).
- Stale evidence (`*_content_hash` mismatch vs current chunk) → render normally
  with a subtle "source may have changed since this was cited" hint.
- Reference deleted but ledger entry remains → 404 with a friendly fragment.
- A reference with a single evidence span → prev/next hidden (no "1 of 1" nav).

## Testing
- **Transform** (pytest): valid marker → numbered `[n]` link with correct href;
  unknown ev-id → inert escaped text; numbering stable & per-reference; repeat
  citations share a number; export path untouched.
- **Locator** (pytest, pure): exact match; whitespace-normalized match across
  newlines; duplicate-quote disambiguation via section/chunk; not-found → None.
- **Route** (pytest + TestClient): owner & viewer auth; fragment structure
  (header fields, `#ev-target` present); prev/next targets correct at ends;
  `text_status != done` notice; HTML-escaping of reference text; unknown ev →
  404.
- **JS** (`tests/js/`, node --test, `vm` harness): `a.cite` click loads + opens
  drawer; scroll-to-target invoked; Esc/✕ closes and restores chat; responsive
  overlay vs column branch.

## Scope boundaries / non-goals
- **No PDF viewer** (Tier 2, later): open the real PDF via pdf.js with verbatim
  find-highlight. Out of scope here.
- **No new DB tables or migrations**; no writes; no editing references from the
  reader.
- **No "all citations" index/panel** beyond the existing citations ledger view.
- No change to `export.md` or the citation-export bibliography behavior.

## File-by-file (for the plan)
- `src/call_manager/proposal_assist/web/workspaces.py` — call the citation
  transform in the display-render path; allow `class` on `<a>` in the bleach
  config.
- `src/call_manager/proposal_assist/citations/` — new `reader.py` (or extend the
  citations module): marker→link transform (shared with export numbering) +
  `locate_evidence()` pure locator.
- `src/call_manager/proposal_assist/web/citations.py` — new reader route.
- `src/call_manager/web/templates/proposal_assist/partials/citation_reader.html`
  — reader fragment.
- `src/call_manager/web/templates/proposal_assist/workspace_main.html` —
  `#reference-drawer` container.
- `src/call_manager/web/static/app.js` — delegated `a.cite` handling, drawer
  open/close/scroll, Esc.
- `src/call_manager/web/static/app.css` — responsive drawer + `mark` highlight.
- `tests/…` — as in Testing.
