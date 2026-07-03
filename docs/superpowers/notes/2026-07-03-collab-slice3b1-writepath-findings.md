# Collab Slice 3b-1: Write-Path Harness Incremental Fixtures — Findings

**Date:** 2026-07-03
**Branch:** collab-slice3b1
**Task:** Task 5 — extend write-path harness with incremental `apply_pm_section_to_fragment` fixtures

## Summary

The existing Python↔y-prosemirror write-path validation harness was extended with
five incremental fixtures covering the most likely failure modes of
`apply_pm_section_to_fragment`: section content replacement, growing/shrinking block
counts, and clearing sections to empty (including the preamble, section 0).

## Incremental Corpus

Five cases in `goldens/python_writepath_incremental.json`, each as
`{name, pm_json (expected post-edit), base_b64 (full-state of pre-edit doc),
incr_b64 (apply_pm_section_to_fragment output)}`:

| name | base markdown | section_index | new content |
|------|--------------|---------------|-------------|
| `replace_middle` | Pre + §A (Body a.) + §B (Body b.) | 1 | §A replaced with "A EDITED." |
| `grow_blocks` | Pre + §A (Body a.) + §B (Body b.) | 1 | §A replaced with 2 paragraphs (P1 + P2) |
| `shrink_blocks` | Pre + §A (P1 + P2) + §B (Body b.) | 1 | §A collapsed to 1 paragraph (Only.) |
| `clear_blocks_empty` | Pre + §A (Body a.) + §B (Body b.) | 1 | §A cleared to empty (`blocks=[]`) |
| `clear_preamble` | Pre + §A (Body a.) | 0 | preamble (section 0) cleared to empty |

## Python Regen + Stable-Run Results

```
REGEN run (REGEN_WRITEPATH=1):  5 passed, 2 warnings in 0.07s
Stable run (no REGEN):          5 passed, 2 warnings in 0.05s
ruff check:                     All checks passed!
```

## Node Harness Gate — VERDICT: GO

All 16 full-state fixtures AND all 5 incremental fixtures read back identically
in real `y-prosemirror`. No write-path interop bug found. `apply_pm_section_to_fragment`
output, including the tricky empty-blocks cases (`clear_blocks_empty`, `clear_preamble`),
converges correctly in a real `Y.Doc`.

### Full CLI output (`node scripts/verify_python_writepath.mjs`)

```
=== verify_python_writepath (Node reads Python-produced ydocs) ===
  ✓ preamble_only
  ✓ single_section
  ✓ multi_section_h3
  ✓ marks_and_links
  ✓ nested_marked_list
  ✓ blockquote_and_code
  ✓ horizontal_rule
  ✓ image
  ✓ hard_break
  ✓ fenced_h2_not_a_section
  ✓ citation
  ✓ adjacent_citations
  ✓ bare_marker_is_literal_text
  ✓ h1_title_preamble
  ✓ production_layout
  ✓ spec_skeleton

GO: 16/16 fixtures read back identical (Node y-prosemirror == Python pm_json_to_ydoc).

=== verify_python_writepath incremental (base + apply_pm_section_to_fragment) ===
  ✓ replace_middle
  ✓ grow_blocks
  ✓ shrink_blocks
  ✓ clear_blocks_empty
  ✓ clear_preamble

GO: 5/5 incremental fixtures read back identical (Node y-prosemirror == Python apply_pm_section_to_fragment).
```

### Vitest wrapper (`pnpm exec vitest run src/writepath.test.ts`)

```
✓ src/writepath.test.ts  (2 tests) 17ms
Test Files  1 passed (1)
     Tests  2 passed (2)
```

## Write-Path Fix Required

**None.** The Python write path was clean — no fix to `apply_pm_section_to_fragment`
or any other production module was required.

## Files Changed

- `tests/proposal_assist/collab/test_collab_writepath.py` — added `INCREMENTAL`, `_INCR_CASES`, `_build_incremental_entries`, `_maybe_regenerate_incremental`, `test_incremental_committed_bytes_reconstruct_pm`
- `tests/proposal_assist/collab/goldens/python_writepath_incremental.json` — committed incremental corpus (5 entries)
- `frontend/scripts/verify_python_writepath.mjs` — added `INCREMENTAL` path constant, `loadIncremental`, `reconstructIncremental`, `compareAllIncremental`; extended `invokedDirectly` CLI block to run and report incremental fixtures
- `frontend/src/writepath.test.ts` — added second vitest test asserting `compareAllIncremental()` returns no mismatches; updated import to include `compareAllIncremental`
- `docs/superpowers/notes/2026-07-03-collab-slice3b1-writepath-findings.md` — this file
