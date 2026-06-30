# Collab-Editing Bridge Spike — GO/NO-GO Findings

**Date:** 2026-06-30
**Spike branch:** `collab-editing-bridge-spike`
**Author:** Task 6 capstone (Tasks 1–5 findings consolidated here)

---

## Verdict: GO

Pure-Python `pycrdt` reads a real `y-prosemirror`-encoded document byte-identically to `y-prosemirror` itself, across all 16 fixtures (16/16 fidelity + 16/16 round-trip, byte-identical even before the `_canonical` safety net; confirmed by review).

The server-authoritative design (Approach 1 in the spec) is viable with **no Node sidecar**. Markdown ↔ ProseMirror ↔ Yjs all run in pure Python.

### Read vs write path

The fidelity gate validates the **READ direction** only: `ydoc_to_pm_json` (Python) produces output that is byte-identical to what y-prosemirror itself would read from the same Yjs state. The **WRITE direction** — `pm_json_to_ydoc` (Python) produces a Yjs update that a real y-prosemirror reader in a browser decodes correctly — is **not yet validated**. `pm_json_to_ydoc` is currently exercised only in a closed Python loop (Python writes → Python reads) and in the end-to-end test; no JS-side reader verifies Python's encoded output. Byte-equality of the encoded update cannot serve as the check because Yjs updates legitimately differ by client-id and clock.

Because the server-authoritative design has the server seed the Yjs document that browsers consume, this write path is load-bearing. **Validating it is a Slice-1 task**: add a Node-side test that feeds a `pm_json_to_ydoc` update through y-prosemirror and asserts the resulting PM JSON matches the input.

The GO verdict is sound for the read/serialize path. The write path has this one named gap to close before browsers receive server-authored Yjs state.

---

## What Was Tested

16 fixtures covering the complete constrained schema plus the two existing-document shapes that must hydrate without migration:

| Fixture | Coverage |
|---|---|
| `preamble_only` | bare paragraph, no heading |
| `single_section` | H2 heading + paragraph |
| `multi_section_h3` | H2 + H3 nesting |
| `marks_and_links` | strong / em / strikethrough / code / link |
| `nested_marked_list` | bullet list incl. nesting, ordered list |
| `blockquote_and_code` | blockquote, code_block |
| `horizontal_rule` | hr node |
| `image` | image inline atom |
| `hard_break` | hard_break inline atom |
| `fenced_h2_not_a_section` | `## ` inside a fence must NOT split a section |
| `citation` | full `[@key]<!-- ev:… -->` citation atom |
| `adjacent_citations` | two citation atoms side-by-side |
| `bare_marker_is_literal_text` | `[@x9]` with no ev-comment stays literal plain text |
| `h1_title_preamble` | H1 title + H2 body (proposal title shape) |
| `production_layout` | tight H1 + bare marker (byte-identical to existing workspace layout) |
| `spec_skeleton` | multi-section spec skeleton from `artifacts.SPEC_SKELETON` |

---

## Constructs That Round-Trip Cleanly

Every constrained-schema node and mark:

**Block nodes:** heading H1–H6, paragraph, bullet_list, ordered_list, list_item, blockquote, code_block, horizontal_rule.

**Inline atoms:** image, hard_break, `citation` (full marker `[@key]<!-- ev:ev-… -->`).

**Marks:** strong, em, strikethrough, code, link.

**Existing-document shapes:** H1 title, bare `[@key]` marker staying literal plain text, tight production layout (`# T\nintro\n## Background\n…`), spec skeleton.

---

## Normalization Inventory

Markdown is re-normalized on the first checkpoint; the serializer is the single canonical writer. The following input shapes are rewritten:

1. **Non-citation HTML is dropped** — block HTML, generic HTML comments, and **fully orphan citation markers** (a `<!-- ev:… -->` with no associated `[@key]` anywhere) are dropped entirely. A **split** valid marker — `[@key]` separated from its `<!-- ev:… -->` comment (e.g. by an edit) — behaves differently: the `[@key]` text **survives** as a bare (unlinked) marker; only the orphaned `<!-- ev:… -->` comment is dropped. The result degrades to a bare marker, which the existing `find_bare_markers` scan will flag — it does not vanish silently. Inline HTML tags drop the markup but **preserve the inner text** (e.g. `<b>keep me</b>` → `keep me`). This is the **main hydration risk**: the app encodes citations as HTML comments, so an orphan or misplaced `<!-- ev:… -->` would be lost on first checkpoint. The drop is intentional and explicit in the code — the constrained schema has no raw-HTML node.

2. **Code-fence language is dropped** — ` ```python ` becomes ` ``` `. The constrained `code_block` carries no language attr.

3. **Whitespace tight→loose** — blank lines are inserted between blocks. Existing tight documents (e.g., `# T\nintro\n## Background\n…`) become loose on first checkpoint. Cosmetic git diff; idempotent thereafter.

4. **Escaped brackets are un-escaped** — `\[x\]` becomes `[x]`. This is a deliberate consequence of not escaping `[`, which is required so bare `[@x9]` markers stay literal.

5. **Autolink → link** — `<https://x>` becomes `[https://x](https://x)`.

6. **Loose list → tight list.**

Note: inline-code with backtick content was a real corruption prior to Task 3; it is now fixed via CommonMark delimiter sizing and is NOT a normalization — it round-trips cleanly.

---

## Schema Constraints to Enforce in the Editor (Slice 1)

- **No raw HTML** — the constrained schema has no raw-HTML node; the editor toolbar must not expose an HTML insert path.
- **`code_block` carries no language attribute** — add a `language`/`params` attr to the schema in Slice 1 if fence-language preservation is ever needed; until then, language tags from pasted content will be silently stripped on first save.

---

## PM→Markdown Approach Chosen

Hand-written, focused serializer over the constrained schema. `prosemirror-py-converter` is not installed; `prosemirror-py` ships only a DOM serializer. The hand-written approach is simpler and directly encodes the schema constraints as invariants.

---

## `pycrdt` ↔ ProseMirror Mapping (for the Slice-1 Sync Server)

- PM doc → the `'prosemirror'` `Y.XmlFragment`; its children are the document's top-level block nodes (the `doc` wrapper is NOT encoded as an element).
- Each non-text node → `XmlElement` (tag = PM node type; Yjs attributes = node attrs).
- Text + marks → one `XmlText` per text-containing block; each mark is a formatting-attribute key on the chunk (value = mark attrs dict, `{}` for attr-less marks).
- Inline atoms (citation, image, hard_break) → childless `XmlElement` siblings interleaved in the block.

**Yjs quirks the bridge handles:**
- Integral attrs widen to float in Yjs; bridge coerces them back to int.
- Null attrs are omitted by Yjs; bridge reconstructs absent attrs as null/None in schema order.
- Map attr order is insignificant (rebuilt in schema order by the bridge).

---

## Hydration Verdict

**Existing proposal/spec documents hydrate WITHOUT a content-migration pass.** Structure (H1 titles, H2/H3 sections, bare markers staying literal, list/blockquote/code_block shapes) is preserved and sections are intact — verified against the byte-exact `production_layout` and `spec_skeleton` fixtures.

**However, the first checkpoint is a normalizing rewrite.** It will:
- **(a)** Reflow whitespace from tight to loose (cosmetic).
- **(b)** Drop any fully-orphan `<!-- ev:… -->` comments, generic HTML comments, block HTML, and code-fence language tags; degrade split `[@key]/<!-- ev:… -->` pairs to bare markers (caught by the bare-marker scan); preserve inner text of inline HTML tags. These are the real risk, not the whitespace.

### Slice-1 Prerequisites Before Enabling the New Editor on Live Workspaces

1. **Decide on the whitespace reflow.** It is cosmetic and idempotent after the first save, but it will produce a noisy git diff for tight existing documents. Either (i) accept it as a one-time event logged in the workspace activity feed, or (ii) run a one-time normalization commit per workspace document before opening it in the new editor.

2. **Audit existing live documents for non-citation HTML and orphan/split citation markers.** A fully orphan `<!-- ev:… -->` comment (no associated `[@key]`) is dropped silently on first checkpoint — genuine content loss. A split valid marker (where `[@key]` and `<!-- ev:… -->` have been separated by an edit) degrades to a bare unlinked marker rather than disappearing, and the bare-marker scan will flag it; but the ev-comment is still lost. Generic HTML comments and block HTML are also dropped. This is the single genuine content-loss risk, and it must be addressed (via audit + repair or a migration script) before enabling the editor on any workspace that contains such content.

---

## Raw Fidelity Test Result

`test_fidelity_byte_identical_raw` passes for all 16 goldens: `ydoc_to_pm_json(ydoc)` equals `json.loads(golden["pm_json"])` with **no canonicalization** — byte-identical JSON. This is the strongest possible fidelity claim and was confirmed by review.

Full suite: **21/21 passed** (`uv run pytest tests/proposal_assist/collab/ -v`, 2026-06-30).

---

## Next Step

This findings document is the input to the Slice 1 (sync server + persistence + auth) plan.
