# Collab tables — strict-GFM tables in the collab editor (design)

**Date:** 2026-07-08 · **Status:** approved design, pre-plan
**Predecessors:** 3b-5d.D4 flip (`2026-07-07-collab-slice3b5d4-flip-design.md`, merged 0c53a72) + post-flip hardening (merged d72565c). Backlog driver: word-processor backlog §5.9 (grant budget tables), sequenced per §3.1.
**Supersedes:** the D4 pipe-table hold-back (D4 spec §1 locked decision 3, Component A detector, Component H audit script) — this slice removes it.

## 1. Goal

Tables become first-class content in the live collab editor: users insert and edit GFM-shaped tables in Tiptap, tables round-trip losslessly through the markdown-in-git checkpoint stream, and the enable-on-touch pipe-table hold-back is removed — previously held-back artifacts auto-enable on next read and their pipe tables hydrate as real table nodes.

## 2. Locked decisions (user-approved 2026-07-08)

1. **Strict GFM shape, no alignment.** The schema expresses exactly what pipe tables can store: first row is the header, cells hold a single paragraph of inline content, no merged cells, no column widths, no per-column alignment. The editor cannot create anything git cannot store; round-trip is lossless by construction. Alignment colons in existing docs are dropped on canonicalization (documented loose-form loss, same class as fence info strings).
2. **UI = Insert-table control + floating table menu.** A small "Insert table" affordance in the editor chrome (draft AND spec — `CollabEditor` is shared), plus a compact floating menu shown only when the caret is inside a table: add row above/below, add column left/right, delete row (disabled on the header row), delete column, delete table. Tab/Shift-Tab cell navigation comes from the table plugin. No persistent formatting toolbar (D4's no-toolbar philosophy stands).
3. **No text-to-table conversion.** Insert button + Word/HTML paste + server-side hydration are the creation paths. Raw pipe-table text typed or pasted into the live editor stays literal text (v1; a paste transform can come later).
4. **Frontend = `@tiptap/extension-table` family, constrained.** Official Table/TableRow/TableHeader/TableCell (prosemirror-tables underneath) with cells constrained to `content: 'paragraph'`, resizing off, no merge UI. Battle-tested navigation/commands/fixTables for free.
5. **Hold-back removed in this slice, as the LAST task.** Server-first ordering inside the branch: at no commit can a client create nodes the server would silently drop.

## 3. Current state (verified 2026-07-08 @ d72565c)

- Server bridge `src/call_manager/proposal_assist/collab/markdown_bridge.py`: markdown-it-py `MarkdownIt("commonmark").enable("strikethrough")` + custom citation inline rule (:90-99). GFM `table` rule NOT enabled — a pipe table parses as ONE paragraph with softbreak `\n` text nodes (verified empirically). Serializer is hand-written; **unknown node/mark law = silent empty-string drop** (`_serialize_block` :444, `_serialize_inline_atom` :421, `_open_mark`/`_close_mark` :353/:367).
- Validation exists ONLY on the markdown→PM direction (`SCHEMA.node_from_json` + `node.check()`, markdown_bridge.py:277-279). The Yjs→PM read path (`ydoc_to_pm_json`, yjs_bridge.py:388-418) surfaces whatever XmlElement tags exist, unvalidated. A client-injected table node TODAY: applied + persisted verbatim, silently omitted from git checkpoints (leaving doubled blank lines), physically deleted by kernel section writes, permanently destroyed by kill-switch handover. No path raises.
- Schema `src/call_manager/proposal_assist/collab/schema.py`: 13 nodes / 5 marks. JS twin `frontend/scripts/collab-schema.mjs` (+ `.d.mts`); Tiptap side `frontend/src/collab/schema.ts` (18 hand-rolled extensions, no table). `frontend/src/collab/collabSchema.equality.test.ts` enforces Tiptap↔twin shape equality (attrs compared via `v.default ?? null` — the documented escape hatch for Tiptap's no-required-attrs limitation). Goldens: `frontend/scripts/gen_collab_goldens.mjs` + `verify_python_writepath.mjs`.
- Yjs bridge `yjs_bridge.py`: PM node → XmlElement (tag = PM type), attrs as Yjs attributes with **nulls omitted** and floats coerced to int when integral (`_coerce_scalar` :81-87); attr plumbing has only ever carried scalars (`_node_attrs_for_yjs` types `str | int | float`, :90).
- Sectionizer `src/call_manager/proposal_assist/editing/sectionizer.py`: `split_sections` cuts at `^##(?!#)` outside backtick fences, line-based, NOT table-aware. `apply_pm_section_to_fragment` (yjs_bridge.py:282-339) verifies block-partition/line-partition coincidence → `SectionMapDivergenceError` before any mutation. Kernel derives room sections as `split_sections(pm_to_markdown(ydoc_to_pm_json(doc)))` (kernel.py:95) — i.e. in LOOSE space.
- Hold-back `src/call_manager/proposal_assist/collab/flip.py`: `_TABLE_ROW` (:39) + `_DELIMITER_ROW` (:47) + `has_pipe_tables` (:50-67) gate `maybe_auto_enable` (:107-131; decision order kill-switch → sticky → detector on head_md → 5s commit mutex → fresh-disk re-read → detector again → enable + commit). Callers: exactly `get_draft` + `get_spec` (web/api/workspaces.py:282/:333). Audit script `scripts/collab_table_audit.py` imports the same detector. Enable is sticky (`is_collab_enabled` short-circuits forever; survives kill-switch handover, state.py:99-103).
- Frontend has NO paste handler (flatten-on-paste is pure schema rejection), NO editor CSS (no `.ProseMirror` rules anywhere), NO formatting toolbar (Mod-key shortcuts + input rules only). `TiptapSelectionToolbar` is the Ask/Edit/Cite surface (draft-only, below the editor). The thin provider (`collabProvider.ts`) is schema-agnostic — zero changes.
- Backlog spec text (word-processor backlog): §5.9 verbatim — "Tables as first-class content. GFM tables in the bridge + Tiptap table extension + insert UI. Do after the §3.1 decision; grant budget tables are the driver." §3.1 documents the degradation mechanics this slice fixes.

## 4. Components

### A. Schema — 4 new nodes, triple-sourced

Add to `schema.py`, `collab-schema.mjs` (+ `.d.mts` typings), and `schema.ts` (via `@tiptap/extension-table` extensions), keeping all three shape-identical:

| node | content | attrs | notes |
|---|---|---|---|
| `table` | `table_row+` | — | group `block`, `isolating: true`, `tableRole: "table"` |
| `table_row` | `(table_cell \| table_header)+` | — | `tableRole: "row"` |
| `table_cell` | `paragraph` | `colspan: 1`, `rowspan: 1`, `colwidth: null` | `isolating: true`, `tableRole: "cell"` |
| `table_header` | `paragraph` | `colspan: 1`, `rowspan: 1`, `colwidth: null` | `isolating: true`, `tableRole: "header_cell"` |

- The colspan/rowspan/colwidth attrs exist because prosemirror-tables requires them; **all defaulted**, so the equality test's `v.default ?? null` normalization holds and Tiptap's no-required-attrs limitation never bites. `colwidth` stays `null` forever (resizing off); nulls are omitted from the Yjs encoding, so the scalar-only attr plumbing in `yjs_bridge.py` is never exercised by an array in normal operation.
- `tableRole` (and any other spec field) must appear consistently across the three sources to whatever depth the equality test compares — the implementer verifies against `collabSchema.equality.test.ts`'s actual comparison, not this table.
- Marks unchanged. No new marks.
- Cells hold exactly one `paragraph` — inline-only in practice. Citations (inline atoms) and all existing marks work inside cells with no extra work.
- Tables are group `block`, so they are legal inside `blockquote` and `list_item` (`paragraph block*`) — supported by construction; the per-line prefixing in the existing serializer covers them (goldens lock it, §D).
- This deliberately breaks D4 Component G's "no new nodes/marks" freeze — that constraint was scoped to the formatting-floor slice. Bridge + goldens + twin all change here, on purpose.

### B. Bridge parse (markdown → PM)

- `MarkdownIt("commonmark").enable(["strikethrough", "table"])` (markdown_bridge.py:91).
- New token handlers in `_block_to_pm`'s dispatch for: `table_open/close`, `thead_open/close`, `tbody_open/close`, `tr_open/close`, `th_open/close`, `td_open/close`. Mapping: `th` → `table_header`, `td` → `table_cell`; `thead`/`tbody` are structural wrappers with no PM node (rows flatten into `table`).
- Alignment: markdown-it exposes per-cell `style="text-align:…"` — **dropped** (locked decision 1).
- Cell inline content parses through the normal inline pipeline (citation rule, marks, code_inline) — GFM unescapes `\|` before inline parsing, so cell text arrives clean.
- Output still passes the existing `SCHEMA.node_from_json` + `node.check()` gate — the ONLY validation direction, unchanged.
- Non-GFM pipe text (no delimiter row) still parses as a paragraph, exactly as today — and round-trips as literal text because `|` is not in the paragraph-text escape set and a re-parse still finds no delimiter row.

### C. Bridge serialize (PM → markdown) — the table loose-form law

`_serialize_block` gains a `table` case. The canonical (loose) form:

- **Always bordered**: every emitted row is `| cell | … | cell |` (leading and trailing pipe). This guarantees no emitted line ever starts with `##`, which keeps `split_sections` and the `SectionMapDivergenceError` coincidence check safe for multi-line table blocks — a hard invariant, not a style choice.
- Row format: `"| " + " | ".join(cells) + " |"`; delimiter row after the header: `"| " + " | ".join("---" per column) + " |"`. No alignment colons ever emitted.
- **Header = the FIRST row**, whatever its cell types claim. A `table_header` cell appearing in a later row serializes as a plain cell; a `table_cell` in row one serializes as a header cell. (Deterministic degrade for hostile/imported content; the UI can't produce it.)
- **Column count = header-row width** (after colspan expansion). Body rows are padded with empty cells or truncated to match — deterministic.
- **colspan > 1 degrades** to the cell's content once + (n−1) empty filler cells. **rowspan is ignored** (prosemirror-tables represents spanned-over cells by omission; the padding rule fills the gap).
- **In-cell inline serialization** runs in a cell-aware mode: the existing escape set (`* _ ` ~ \`) plus `|` → `\|`; `hard_break` degrades to a single space (a `\`-newline would split the row). The `|` escape applies to ALL inline content in the cell **including code-span (`code` mark) text** — GFM splits rows on unescaped pipes before inline parsing, so `_wrap_inline_code` output inside a cell must also have its pipes escaped (GFM honors `\|` inside code spans in table cells; outside tables, code-span content stays unescaped as today). If a cell somehow contains more than one paragraph or a non-paragraph block (hostile injection — the schema check never sees the Yjs read path), its blocks' inline serializations are joined with a single space: degrade, never drop, never raise.
- Empty cell → empty string between the pipe separators (`|  |`). Header-only tables (no body rows) are valid GFM and serialize as header + delimiter.
- Tables inside blockquotes/list items inherit the existing per-line `> ` prefix / marker-width indentation — markdown-it re-parses prefixed tables, and goldens lock the nesting cases.
- **Round-trip law**: `markdown_to_pm(pm_to_markdown(t)) == t` for every schema-valid table with default attrs, and `pm_to_markdown` is idempotent over its own output. Golden-tested (§H).

### D. Goldens + schema twins

- Regenerate the Python-writepath goldens (`frontend/scripts/gen_collab_goldens.mjs`, `verify_python_writepath.mjs`) with table cases: basic 2×2, escaped `|` in cell text, citation-in-cell, marks-in-cell, ragged body row, header-only table, table-in-list-item, table-in-blockquote, colspan-degrade input.
- Update `collab-schema.mjs` + `.d.mts` and `collabSchema.equality.test.ts` expectations in the same commit as `schema.ts` — the equality test is the drift guard, it must never be green across a half-updated pair.

### E. Frontend nodes + editing behavior

- Packages: `@tiptap/extension-table`, `-table-row`, `-table-header`, `-table-cell` pinned to the workspace's existing Tiptap 3 version (`pnpm add --config.minimumReleaseAge=0` — 7-day cooldown gotcha).
- `schema.ts`: `Table.configure({ resizable: false })`; `TableHeader.extend` / `TableCell.extend` with `content: 'paragraph'` and `parseHTML` overrides that force `colspan`/`rowspan` to 1 — a pasted Word/HTML merge parses as a single 1×1 cell, producing ragged rows that prosemirror-tables' `fixTables` pads with empty cells. The server degrade law (§C) remains as backstop for anything that slips through (or is injected directly over WS).
- Register the new extensions so they flow through the `collabEditorExtensions` post-NodeView list (the `CitationNode.tsx` re-export at frontend/src/collab/CitationNode.tsx:24-27) — the equality test guards that list, not bare `collabExtensions`.
- No merge/split commands exposed anywhere; no resize plugin; Tab/Shift-Tab from `tableEditing`.
- Provider untouched (schema-agnostic). No backend API changes → no OpenAPI regen.

### F. Frontend UI

- **Insert table**: a small control in `CollabEditorInner`'s chrome above `EditorContent` (both artifacts get it — `SpecPanel` mounts the same component). Runs `insertTable({ rows: 3, cols: 3, withHeaderRow: true })`. Disabled while the selection is inside a table (no nesting via UI; the schema forbids it anyway since cells hold only a paragraph) and while `editor` is null (first render).
- **Floating table menu**: rendered via Tiptap's bubble-menu utility, visible only when `editor.isActive('table')`. Buttons: add row above / below, add column left / right, delete row, delete column, delete table — thin wrappers over the extension's commands. **Delete row is disabled when the caret is in the header row** (GFM requires a header; `toggleHeaderRow` is not exposed). Mantine components, `data-*` hooks per cockpit convention.
- **CSS**: the collab editor's first real stylesheet — a scoped CSS module (no bare element selectors; scope under a hashed wrapper class per repo convention) with `--app-*` tokens: table borders, cell padding, header-row emphasis, `.selectedCell` state. Nothing global.

### G. Hold-back removal + flip simplification (LAST task)

- `maybe_auto_enable` decision order becomes: kill-switch → sticky short-circuit → acquire 5s commit mutex → enable + commit. **The mutex stays** — enabling while holding the commit mutex is the linearization anchor the D4 severance-recheck family depends on. The detector steps (pre-mutex head_md check, under-mutex fresh-disk re-read) are deleted along with `_TABLE_ROW`, `_DELIMITER_ROW`, `has_pipe_tables`, and the held-back log line; `CommitMutexTimeout` → defer (unchanged); the IntegrityError concurrent-first-read race handling (unchanged).
- Delete `scripts/collab_table_audit.py` (its operational signal — "the hold-back is removable" — is fulfilled by this slice). Update flip.py's module docstring.
- Tests: delete the 8 detector tests + `test_hold_back_on_pipe_tables` + `test_hold_back_when_fresh_disk_gained_a_table` (test_collab_flip.py) + `test_table_doc_is_held_back` (test_collab_flip_readmodel.py). Replace with: an integration test that a pipe-table doc **enables on read and serves the table through the canonical projection** (GET → `collab_enabled: true`, response body contains the canonicalized bordered table), and a hydration unit test (pipe-table markdown → PM contains a `table` node).
- One-line amendment to the D4 spec §1 decision 3: hold-back removed by this slice (pointer to this doc).
- Sequencing (hard): this task lands only after §§B–F are committed and green. Server-first inside the branch: bridge/schema (server) → goldens/twins → frontend nodes/UI → hold-back removal.

## 5. Cross-cutting invariants

- **Tight/loose law**: table canonicalization (bordering, padding, escape normalization, alignment-colon dropping) joins the loose form. The `has_authored_updates` tight/loose switch and all its consumers are unchanged. Pre-enable tight `section_sha`s over table-containing sections re-canonicalize on first authored edit exactly as documented for existing content — the existing anchor-relocation/self-heal machinery applies; no sha migration.
- **Sectionizer**: safe in loose space by the always-bordered invariant (§C). Documented residual edge: a seed-only (tight-anchored) room whose borderless table row's first cell starts `## ` can mismatch section anchors until the first authored edit — existing guards produce clean 409s / anchor relocation, not corruption. Accepted; markdown-it's block-rule order (table before heading) and `split_sections` disagree on such lines only in tight space.
- **Kill-switch handover**: room tables serialize to GFM pipe markdown → land in git → CM6 world edits them as raw text → re-hydrate as real table nodes on lift (the bridge now parses GFM). This CLOSES the pre-existing trap where a table entering via CM6 during a kill-switch window flattened on lift.
- **Injection posture unchanged**: the Yjs→PM read path stays unvalidated; the silent-drop law for unknown nodes stays. Tables merely move from "unknown/dropped" to "known/serialized-with-deterministic-degrades". No new validation pass (out of scope; posture documented).
- **Enable-on-touch race family**: untouched except for the detector deletion — the flip still commits enables while holding the commit mutex; all severance rechecks and fresh under-mutex reads in the D4 family remain valid.
- **Downstream markdown consumers** (export, coherence keeper, evaluator, mock panel, digests): consume markdown text; pipe tables pass through as text. No changes.
- **No DB migration, no new endpoints, no OpenAPI regen.** `DraftOut`/`SpecOut` unchanged.

## 6. Accepted losses / non-goals (documented)

- Per-column alignment colons normalize away (loose-form loss; same class as fence info strings, which are already dropped).
- Merged cells, column widths, multi-block cells: not representable; deterministic degrade rules at parse/paste/serialize boundaries.
- Raw pipe-table TEXT typed/pasted into the live editor stays literal text (no input rule / paste transform in v1). It also does not later "become" a table: multi-line pastes become separate paragraphs whose blank-line-separated serialization never re-parses as GFM.
- Out of scope: DOCX/PDF export fidelity (backlog §5.7), Word-paste hardening beyond what the schema gives for free (§5.8), rendered-markdown preview (§5.10), per-column alignment UI, CM6 teardown.

## 7. Testing & gates

- **Backend**: bridge round-trip + idempotency goldens (all §D cases); hydration test (pipe-table markdown → table node; borderless table; table + citation in cell); checkpoint round-trip (insert table via applied update → checkpoint → git contains bordered GFM → re-hydrate equals doc); kernel section write on a table-containing section (no `SectionMapDivergenceError`, table preserved); kill-switch handover with a table (drained markdown parses back to the same table); flip tests updated per §G. Pytest: `EMBEDDING_MODEL=bge-m3 -p no:cacheprovider`, grouped never whole-suite.
- **Frontend**: `collabSchema.equality.test.ts` green across all three sources; headless Editor command tests (insert table, add/delete row/col, Tab navigation, header-row delete-guard) in the `schema.formatting.test.tsx` pattern; bubble-menu/insert-button component tests (jsdom layout shims per `test-setup.ts`); `tsc` (the frontend gate — no linter).
- **e2e**: extend `frontend/e2e/collab.spec.ts` — user A inserts a table and types in a cell; user B sees it live and edits another cell; save version; reload shows the table. Run dev (`bash scripts/collab_e2e_run.sh`) + prod (`E2E_SPA_MODE=prod …`).
- **Standard gates**: grouped backend + ruff + tsc + vitest + e2e both modes.

## 8. Rollout

Single branch/slice, standard SDD. Server-first task ordering (§G). Merge = tables live everywhere collab is live: existing enabled rooms gain table editing on next load; held-back artifacts auto-enable on next read and hydrate their pipe tables as real tables (lazy, per-read — no migration, no notification, matching the D4 enable-on-touch model). Rollback = the collab kill-switch as before; drained tables persist as GFM markdown and survive the round-trip.
