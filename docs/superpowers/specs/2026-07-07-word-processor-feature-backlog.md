# Word-processor feature backlog for the proposal cockpit

- **Date:** 2026-07-07
- **Status:** DEFERRED — parked until the collab-editing track is done (3b-5d: D3 analysis re-fire, then D4 flip). Two items are carved out of this backlog and folded into the D4 definition of done (§3).
- **Origin:** "Which MS Word features would be very useful here?" Gap analysis grounded in a 4-agent read-only inventory of the codebase (editor surface, document lifecycle, review/collaboration, grant-domain constraints). Evidence paths below reflect the tree at commit `43712a4`.

## 1. Summary

The cockpit is strong where Word is weak (evidence-anchored citations, CSL bibliography, AI review/scorecard, propose-only agent edits with redline accept/reject) and weak where Word is table stakes (spellcheck, find & replace, word count, export formats, version browsing, human-to-human review). Several of the most valuable gaps are cheap because the plumbing already exists but was never surfaced to humans.

## 2. Decision: defer until collab is done

Nothing in this backlog blocks the collab track, and the two most design-sensitive features (margin comments, human suggestion-mode) are **better** built after it: their text anchors must survive concurrent live edits, so designing them against the final editing model (server-authoritative Yjs doc) instead of today's CM6 save-cycle avoids building anchoring twice. Everything else (word count, find/replace, export, version history, length budgets) is orthogonal and loses nothing by waiting.

**Do not propose these as next slices while collab is in flight.**

## 3. Carve-outs: two items belong to the D4 flip, not this backlog

These are properties of the flip itself — flipping without them regresses the writing experience:

### 3.1 Table-degradation decision

Pipe tables typed as raw markdown in the CM6 editor survive save/export as literal text. Post-flip, the draft hydrates through the CommonMark-only bridge (`src/call_manager/proposal_assist/collab/markdown_bridge.py` — no GFM `table` extension) and the Tiptap schema has no table node (`frontend/src/collab/schema.ts`), so a pipe table becomes plain paragraph text — borderline data loss on round-trip. Before flipping, decide one of:

1. Accept + document as a known limitation (cheapest; acceptable if no real drafts contain tables).
2. Pre-flip scan of existing drafts for pipe-table lines; warn/skip-flip affected workspaces.
3. Full GFM-table support (bridge + Tiptap table extension + insert UI) — real work; see §5.9, probably post-flip.

A pre-flip scan (option 2) is cheap insurance regardless.

### 3.2 Minimal formatting authoring in the Tiptap editor

The WYSIWYG editor can *display* bold/headings/lists hydrated from markdown but offers **no way to author them**: no toolbar, no Mod-B/Mod-I keyboard shortcuts, no input rules (`frontend/src/collab/schema.ts` — bare `Mark.create`, no `addKeyboardShortcuts`/`addInputRules`; `TiptapSelectionToolbar` is Ask/Edit/Cite only). The backend serializer escapes `*_`​`~` typed by hand. Post-flip this is the primary writing surface, so a formatting floor — Mod-B/Mod-I/Mod-Shift-S at minimum, ideally heading/list input rules (`## `, `- `) — is part of "collab is done".

## 4. Current-state inventory (condensed)

What exists, what half-exists, what's absent. Full detail lives in the per-feature sections below.

| Area | Exists | Gaps |
|---|---|---|
| Editor | CM6 markdown source editor (per-section save, 409 conflict banner, citation pills, Ask/Edit/Cite LLM toolbar, per-section undo, Ln/Col status bar); flag-OFF Tiptap/Yjs collab editor with presence carets + collaborative undo | No spellcheck in CM6 (accidentally disabled), no find/replace, no word count, no formatting toolbar/shortcuts in either editor, no rendered preview (Full-draft tab is read-only *source*) |
| Lifecycle | Markdown export ×2 (raw + composed with citations/bibliography), git-backed versions (list-only UI), explicit save + dirty guards (CM6) / continuous Yjs persistence + checkpoint (collab), workspace soft-delete, rubric snapshot at setup | No DOCX/PDF export, no version view/diff/restore for humans, no template scaffold (spec parked), no draft import, no print |
| Review/collab | AI propose→redline→accept/reject (member-gated), Claims tab (machine-generated only), activity feed, flat owner/member roles, spec lock, presence + live co-editing (flag OFF) | No human margin comments, no human suggestion-mode (`origin_kind='human'` reserved in the `ProposedChange` check constraint but never creatable), no mentions/notifications, no view-only links, no role tiers |
| Domain | Rubric extraction + evaluator scorecard, CSL-JSON reference library with external verification, 2-style bibliography (vancouver/author-year), citation audit + uncited-claims signals | No page/word-limit modeling anywhere on `Call`/workspace, only a global 6000-char Problems heuristic, content-quality-only evaluator, no multi-language support (Swedish!), 2 citation styles only |

## 5. Ranked backlog (for after the flip)

### Tier 1 — quick wins (days, not weeks)

**5.1 Spellcheck.**
CodeMirror defaults to `spellcheck="false"` and `frontend/src/editor/CodeMirrorEditor.tsx` never overrides it — manual writers get no red squiggles in the production editor. The Tiptap editor has browser spellcheck ON by contenteditable default. Fix: one-line `EditorView.contentAttributes` override (CM6); make Tiptap's explicit. Later option: LanguageTool-style grammar checking with Swedish support (today's only aid is the LLM "Fix grammar" preset).

**5.2 Find & replace.**
Completely absent — `@codemirror/search` is not installed; no search within a section, none across the proposal. Fix: add the package + `searchKeymap` (Mod-F panel) for CM6; ProseMirror equivalent for Tiptap post-flip. Cross-section search is a small follow-up (client-side over section buffers).

**5.3 Word / character count.**
Absent everywhere; the StatusBar (`frontend/src/shell/StatusBar.tsx`) shows only save state, "Markdown", Ln/Col. Fix: live word/char count for the active section + full draft in the StatusBar (feed from the existing `editorStore` cursor/doc listener; `@tiptap/extension-character-count` post-flip). This is the natural substrate for 5.4.

### Tier 2 — high value (each a real slice)

**5.4 Length budgets vs funder limits.**
Grant calls have hard page/word limits, and today they are unmodeled: no limit column on `Call`/`CallSectionExtract`/`Workspace` (`src/call_manager/db/models.py`), extraction prompts never ask for limits, and the only signal is the global `PROBLEM_SECTION_CHAR_BUDGET` heuristic (6000 chars, `src/call_manager/proposal_assist/problems/builders.py`) — "a loose guardrail, not a hard funder limit". This slice = **unpark the ProposalTemplate v1 spec** (`docs/superpowers/specs/2026-06-28-proposal-template-design.md`: required sections, per-section guidance + length budgets, scaffold at activation, soft-validation into the Problems panel) and pair it with the 5.3 counter for live over/under display.

**5.5 Version history: view / diff / restore.**
The Versions sidebar rows are inert labels over `GET /api/workspaces/{id}/versions` (list-only). Meanwhile the *agent* already has the needed git plumbing — `get_artifact_history`, `read_artifact_at`, `diff_artifact` in `src/call_manager/proposal_assist/tools/history.py` (sha-injection-guarded) — that humans cannot reach. Slice: expose read-at-commit + diff as user-facing endpoints; render with the existing redline component (`frontend/src/cockpit/diff.ts`); add restore-as-new-commit (never rewrite history). Word equivalents: Version History + Compare Documents. Small plumbing, large trust payoff.

**5.6 Human comments + suggestion-mode (track changes).**
The biggest genuine hole, and the one that must wait for collab (§2). Today: flag-off members cannot edit at all (save is owner-only); flag-on members become full live co-authors; there is no "member suggests, owner accepts" middle mode, no margin comments on arbitrary ranges, no threads/resolve, no @-mentions, no notifications (email module serves only the weekly digest + auth). Foundations already in place: the propose→redline→accept/reject machinery with staleness guards (`src/call_manager/proposal_assist/web/proposals.py`), and the `ProposedChange` check constraint already reserving `origin_kind='human'` — no endpoint ever creates one. Slice shape: (a) human-origin proposals reusing the existing review surfaces; (b) threaded margin comments anchored to the live doc (Yjs relative positions post-flip); (c) mentions/notifications as a follow-up.

**5.7 DOCX / PDF export.**
Export is markdown-only (`src/call_manager/web/api/export.py`: `export.md`, `export-composed.md`) — but funders take PDFs and Word files, so the last mile happens outside the app and discards the composed citation/bibliography formatting. `python-docx` is already a dependency (reference extraction); pandoc would buy both formats + reference-doc templating. PDF export also unlocks a page-count check against limits (5.4) and de-facto print (no `window.print`/print stylesheet exists today).

### Tier 3 — later

**5.8 Import / paste-from-Word.** No path seeds `proposal.md` from an existing file (workspace creation takes only call/title/deadline/profile), and CM6 paste flattens rich text to plain text. DOCX parsing already exists for references (`src/call_manager/proposal_assist/references/extract.py`) — a "start from my Word draft" import (docx → markdown → sections) is cheap goodwill. Tiptap paste already maps HTML through the schema (untested against real Word output — verify at flip time).

**5.9 Tables as first-class content.** GFM tables in the bridge + Tiptap table extension + insert UI. Do after the §3.1 decision; grant budget tables are the driver.

**5.10 Assorted.** Rendered-markdown preview of the full draft (no renderer exists in the SPA); focus/zoom modes; snippet/boilerplate insertion (org descriptions, ethics statements — "quick parts"); more citation styles via a CSL processor (currently 2 hand-rolled styles despite CSL-JSON metadata); proposal-language setting (Swedish/English) driving spellcheck language + prompts.

## 6. Non-goals

Deliberately not chasing, low value against the markdown-canonical architecture: footnotes/endnotes (the citation system is strictly better), cross-reference/figure auto-numbering, styles/themes, mail merge, macros, full WYSIWYG pagination view.

## 7. Sequencing sketch (post-flip)

1. Writing-basics bundle: 5.1 + 5.2 + 5.3 (one small slice).
2. Then the highest-pain Tier-2 item — 5.4 (compliance pain) or 5.6 (collaboration pain), user to pick; 5.5 and 5.7 slot in as independent slices in either order.
3. Tier 3 opportunistically.
