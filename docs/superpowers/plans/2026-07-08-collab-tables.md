# Collab Tables (strict GFM) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tables become first-class collab-editor content — strict-GFM table nodes triple-sourced across the schema pair, lossless pipe-table round-trip in the server bridge, Insert-table + table-menu UI in Tiptap, and removal of the enable-on-touch pipe-table hold-back.

**Architecture:** Server-first inside the branch (spec §2.5): Python schema + bridge parse/serialize land before the frontend can create table nodes; goldens/twins bind the three schema sources; the hold-back removal is the LAST task. Spec: `docs/superpowers/specs/2026-07-08-collab-tables-design.md` (post-Codex, 5e6486b).

**Tech Stack:** Python 3.12 / markdown-it-py / prosemirror-py / pycrdt; React 19 + Tiptap 3.27.1 (`@tiptap/extension-table`) + Mantine; vitest + pytest (testcontainers PG) + Playwright.

## Global Constraints

- Worktree: `.worktrees/collab-tables`, branch `collab-tables` off `master`. Run `uv sync` in the worktree; frontend deps via `pnpm install` in `frontend/`.
- Pytest ALWAYS: `EMBEDDING_MODEL=bge-m3 uv run pytest <paths> -p no:cacheprovider -q`. NEVER the whole suite. `connection refused` / `too many clients` = testcontainers infra → re-run the group isolated.
- Frontend gates: `pnpm exec tsc --noEmit` (there is NO TS linter — tsc is the gate) and `pnpm vitest run`. Run from `frontend/`.
- New frontend package: `pnpm add @tiptap/extension-table@3.27.1 --config.minimumReleaseAge=0` (7-day cooldown blocks even old versions without the flag). Pin exactly `3.27.1` to match the installed Tiptap line.
- Schema strings must match EXACTLY across the three sources (the equality test compares content strings byte-for-byte): `table` content `"table_row+"`, `table_row` content `"(table_cell | table_header)+"`, cells content `"paragraph"`.
- Canonical parse-output attrs (spec §B): cells/headers emit exactly `{"colspan": 1, "rowspan": 1}`; the `colwidth` key is OMITTED (never `colwidth: null`).
- Always-bordered serialization (spec §C): every emitted table line starts with `"| "` — a hard sectionizer-safety invariant, not style.
- Behavioral floor: every existing test passes UNCHANGED except the hold-back tests explicitly deleted/replaced in Task 8.
- Commits: explicit-path `git add` only; every commit body ends with the trailer line `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- No DB migration, no new endpoints, no OpenAPI regen anywhere in this plan.
- Kill-switch in flip tests: arm via `state.set_kill_switch(True)` + the file's autouse reset fixture (this file's existing pattern — do NOT switch it to setenv; that pattern is for create_app wiring tests only).

---

### Task 1: Server schema — 4 table nodes

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/schema.py`
- Test: `tests/proposal_assist/collab/test_schema.py`

**Interfaces:**
- Produces: `SCHEMA` gains nodes `table` (content `table_row+`, group `block`), `table_row` (content `(table_cell | table_header)+`), `table_cell` / `table_header` (content `paragraph`, attrs `colspan=1`, `rowspan=1`, `colwidth=None`). Tasks 2–3 validate against and hand-build these node types.

- [ ] **Step 1: Write the failing tests** — append to `tests/proposal_assist/collab/test_schema.py`:

```python
def test_schema_has_table_nodes():
    for n in ["table", "table_row", "table_cell", "table_header"]:
        assert n in SCHEMA.nodes, n


def test_table_shapes_are_strict_gfm():
    assert SCHEMA.nodes["table"].spec["content"] == "table_row+"
    assert SCHEMA.nodes["table"].spec.get("group") == "block"
    assert SCHEMA.nodes["table_row"].spec["content"] == "(table_cell | table_header)+"
    for cell in ["table_cell", "table_header"]:
        spec = SCHEMA.nodes[cell].spec
        assert spec["content"] == "paragraph"
        attrs = spec["attrs"]
        assert attrs["colspan"]["default"] == 1
        assert attrs["rowspan"]["default"] == 1
        assert attrs["colwidth"]["default"] is None


def test_table_doc_validates():
    doc = SCHEMA.node_from_json({
        "type": "doc",
        "content": [{
            "type": "table",
            "content": [
                {"type": "table_row", "content": [
                    {"type": "table_header", "attrs": {"colspan": 1, "rowspan": 1},
                     "content": [{"type": "paragraph",
                                  "content": [{"type": "text", "text": "h"}]}]},
                ]},
                {"type": "table_row", "content": [
                    {"type": "table_cell", "attrs": {"colspan": 1, "rowspan": 1},
                     "content": [{"type": "paragraph"}]},
                ]},
            ],
        }],
    })
    doc.check()  # content model holds
```

- [ ] **Step 2: Run to verify failure**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_schema.py -p no:cacheprovider -q`
Expected: 3 new tests FAIL (`KeyError: 'table'`).

- [ ] **Step 3: Implement** — in `schema.py`, after the citation NodeSpec block (line 60) and before the Marks section, add:

```python
# ---------------------------------------------------------------------------
# Tables (strict GFM — 2026-07-08 collab-tables spec §A)
# ---------------------------------------------------------------------------
# Cells hold exactly ONE paragraph (inline-only in practice); colspan/rowspan/
# colwidth exist because prosemirror-tables requires them — all defaulted, all
# pinned to the defaults by the client. tableRole/isolating are inert for the
# Python side (no editor here) but keep the pair description identical to
# frontend/scripts/collab-schema.mjs.
_TABLE_CELL_ATTRS = {
    "colspan": {"default": 1},
    "rowspan": {"default": 1},
    "colwidth": {"default": None},
}

_nodes["table"] = NodeSpec(
    content="table_row+",
    group="block",
    isolating=True,
    tableRole="table",
    parseDOM=[{"tag": "table"}],
    toDOM=lambda node: ["table", ["tbody", 0]],
)
_nodes["table_row"] = NodeSpec(
    content="(table_cell | table_header)+",
    tableRole="row",
    parseDOM=[{"tag": "tr"}],
    toDOM=lambda node: ["tr", 0],
)
_nodes["table_cell"] = NodeSpec(
    content="paragraph",
    attrs=dict(_TABLE_CELL_ATTRS),
    isolating=True,
    tableRole="cell",
    parseDOM=[{"tag": "td"}],
    toDOM=lambda node: ["td", 0],
)
_nodes["table_header"] = NodeSpec(
    content="paragraph",
    attrs=dict(_TABLE_CELL_ATTRS),
    isolating=True,
    tableRole="header_cell",
    parseDOM=[{"tag": "th"}],
    toDOM=lambda node: ["th", 0],
)
```

Note: `NodeSpec` is a TypedDict — extra keys like `tableRole` are inert at runtime (no runtime key validation) and there is no mypy gate. If the constructor-call form rejects them in practice, build the spec as a plain dict literal and cast: `NodeSpec(**{...})` — verify with the test run, don't guess. Update the module docstring's node list to include the four table nodes.

- [ ] **Step 4: Run to verify pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_schema.py -p no:cacheprovider -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/collab/schema.py tests/proposal_assist/collab/test_schema.py
git commit -m "feat(collab): server schema gains strict-GFM table nodes (tables T1)"
```

---

### Task 2: Bridge parse — GFM tables + list-item paragraph synthesis

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/markdown_bridge.py:90-96` (`_make_parser`), `:180-235` (`_block_to_pm`), `:249-256` (`_list_items`)
- Test: `tests/proposal_assist/collab/test_markdown_bridge.py`

**Interfaces:**
- Consumes: Task 1's schema nodes (validation gate).
- Produces: `markdown_to_pm` parses GFM pipe tables into `table`/`table_row`/`table_header`/`table_cell` nodes with attrs exactly `{"colspan": 1, "rowspan": 1}` (no `colwidth` key); any list item whose first block is not a paragraph gains a synthesized empty leading paragraph (and an empty item gains one paragraph). Task 3 serializes these shapes; Task 5's goldens must match them byte-for-byte.

- [ ] **Step 1: Write the failing tests** — append to `tests/proposal_assist/collab/test_markdown_bridge.py`:

```python
# --- tables: parse (2026-07-08 spec §B) ------------------------------------ #

def test_table_parses_to_table_nodes():
    md = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    doc = markdown_to_pm(md)
    table = doc["content"][0]
    assert table["type"] == "table"
    rows = table["content"]
    assert [c["type"] for c in rows[0]["content"]] == ["table_header", "table_header"]
    assert [c["type"] for c in rows[1]["content"]] == ["table_cell", "table_cell"]
    cell = rows[1]["content"][0]
    # canonical parse-output attrs: explicit spans, colwidth key OMITTED
    assert cell["attrs"] == {"colspan": 1, "rowspan": 1}
    para = cell["content"][0]
    assert para["type"] == "paragraph"
    assert para["content"][0]["text"] == "1"


def test_table_cell_inline_content_parses():
    md = "| h |\n| --- |\n| *em* and `code` |\n"
    doc = markdown_to_pm(md)
    body_cell = doc["content"][0]["content"][1]["content"][0]
    inline = body_cell["content"][0]["content"]
    assert inline[0]["marks"][0]["type"] == "em"
    assert any(
        any(m["type"] == "code" for m in n.get("marks", [])) for n in inline
    )


def test_alignment_colons_are_dropped():
    md = "| a | b |\n| :--- | ---: |\n| 1 | 2 |\n"
    doc = markdown_to_pm(md)
    for row in doc["content"][0]["content"]:
        for cell in row["content"]:
            assert set(cell["attrs"]) == {"colspan", "rowspan"}


def test_escaped_pipe_in_cell_is_literal():
    md = "| a \\| b |\n| --- |\n"
    doc = markdown_to_pm(md)
    header = doc["content"][0]["content"][0]["content"][0]
    assert header["content"][0]["content"][0]["text"] == "a | b"


def test_pipe_text_without_delimiter_stays_paragraph():
    md = "a | b | c\n"
    doc = markdown_to_pm(md)
    assert doc["content"][0]["type"] == "paragraph"


# --- list-item leading-paragraph synthesis (spec §B, pre-existing crash) --- #

def test_fence_first_list_item_no_longer_crashes():
    doc = markdown_to_pm("- ```\n  x\n  ```\n")
    item = doc["content"][0]["content"][0]
    assert [b["type"] for b in item["content"]] == ["paragraph", "code_block"]


def test_quote_first_list_item_no_longer_crashes():
    doc = markdown_to_pm("- > q\n")
    item = doc["content"][0]["content"][0]
    assert [b["type"] for b in item["content"]] == ["paragraph", "blockquote"]


def test_empty_list_item_gets_a_paragraph():
    doc = markdown_to_pm("- a\n-\n- b\n")
    for item in doc["content"][0]["content"]:
        assert item["content"][0]["type"] == "paragraph"


def test_table_first_list_item_synthesizes_paragraph():
    md = "- | a | b |\n  | --- | --- |\n"
    doc = markdown_to_pm(md)
    item = doc["content"][0]["content"][0]
    types = [b["type"] for b in item["content"]]
    # if markdown-it treats the pipe text as a table inside the item, synthesis
    # kicks in; the invariant under test is "first child is a paragraph".
    assert types[0] == "paragraph"
```

- [ ] **Step 2: Run to verify failure**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_markdown_bridge.py -p no:cacheprovider -q`
Expected: new tests FAIL — table tests see `paragraph` instead of `table` (rule disabled); synthesis tests raise `ValueError: Invalid content for node list_item`.

- [ ] **Step 3: Implement.** Three edits in `markdown_bridge.py`:

(a) `_make_parser` — enable the GFM table rule:

```python
def _make_parser() -> MarkdownIt:
    md = MarkdownIt("commonmark").enable(["strikethrough", "table"])
```

(b) In `_block_to_pm`, add a `table_open` branch BEFORE the intentional-drop fallthrough (after the `hr` branch):

```python
    if ttype == "table_open":
        return _table_to_pm(tokens, i)
```

and add the new function after `_list_items`:

```python
def _table_to_pm(tokens: list[Any], i: int) -> tuple[dict, int]:
    """Consume ``table_open`` .. ``table_close`` into a strict-GFM table node.

    markdown-it emits thead/tbody wrappers with no PM counterpart — rows
    flatten directly into the table. Cells carry the CANONICAL attr form
    (explicit colspan/rowspan = 1, NO colwidth key — spec §B): the hand-built
    dict IS the output, schema defaults are never materialized into it, and
    the Yjs encoding omits nulls, so this exact shape is what ydoc_to_pm_json
    reproduces. Alignment styles on th/td are dropped on purpose (spec §2.1).
    """
    rows: list[dict] = []
    i += 1  # skip table_open
    while tokens[i].type != "table_close":
        t = tokens[i].type
        if t in ("thead_open", "thead_close", "tbody_open", "tbody_close"):
            i += 1
            continue
        assert t == "tr_open", t
        cells: list[dict] = []
        i += 1
        while tokens[i].type != "tr_close":
            cell_type = "table_header" if tokens[i].type == "th_open" else "table_cell"
            inline = tokens[i + 1]
            content = _inline_to_pm(inline.children or [])
            para: dict = {"type": "paragraph"}
            if content:
                para["content"] = content
            cells.append(
                {
                    "type": cell_type,
                    "attrs": {"colspan": 1, "rowspan": 1},
                    "content": [para],
                }
            )
            i += 3  # {th,td}_open, inline, {th,td}_close
        rows.append({"type": "table_row", "content": cells})
        i += 1  # skip tr_close
    return {"type": "table", "content": rows}, i + 1  # skip table_close
```

(c) `_list_items` — leading-paragraph synthesis (fixes the pre-existing fence/quote/heading-first crash class AND the new table-first case in one rule):

```python
def _list_items(tokens: list[Any], i: int, stop_type: str) -> tuple[list[dict], int]:
    items: list[dict] = []
    while i < len(tokens) and tokens[i].type != stop_type:
        assert tokens[i].type == "list_item_open", tokens[i].type
        children, j = _blocks_until(tokens, i + 1, "list_item_close")
        # list_item content is "paragraph block*": a non-paragraph first block
        # (fence-first, quote-first, table-first, or an empty item) fails
        # node.check(). Synthesize an empty leading paragraph; the serializer
        # DROPS it again when more blocks follow (Task 3 companion — emitting
        # it would blank-line-break the item and the block would escape the
        # list on re-parse), so the round-trip stays idempotent (spec §B).
        if not children or children[0]["type"] != "paragraph":
            children.insert(0, {"type": "paragraph"})
        items.append({"type": "list_item", "content": children})
        i = j + 1  # skip the list_item_close
    return items, i
```

Also update the module docstring's first bullet: "(CommonMark + strikethrough)" → "(CommonMark + strikethrough + GFM tables)".

- [ ] **Step 4: Run to verify pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_markdown_bridge.py -p no:cacheprovider -q`
Expected: all PASS (pre-existing tests unchanged — the parser change only ADDS table token emission; docs without delimiter rows parse exactly as before).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/collab/markdown_bridge.py tests/proposal_assist/collab/test_markdown_bridge.py
git commit -m "feat(collab): bridge parses GFM tables + list-item paragraph synthesis (tables T2)"
```

---

### Task 3: Bridge serialize — the table loose-form law

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/markdown_bridge.py:318-344` (`_serialize_inline`), `:397-421` (`_serialize_inline_atom`), `:424-444` (`_serialize_block`)
- Test: `tests/proposal_assist/collab/test_markdown_bridge.py`

**Interfaces:**
- Consumes: Task 2's parse shapes.
- Produces: `pm_to_markdown` serializes `table` nodes to always-bordered GFM (`_serialize_table(node: dict) -> str`, `_cell_text(cell: dict) -> str`); `_serialize_inline(nodes, in_cell=False)` and `_serialize_inline_atom(node, in_cell=False)` gain the keyword. Round-trip law: `pm_to_markdown(markdown_to_pm(x))` idempotent, and parse(serialize(t)) == t for default-attr tables.

- [ ] **Step 1: Write the failing tests** — append to `tests/proposal_assist/collab/test_markdown_bridge.py`:

```python
# --- tables: serialize (2026-07-08 spec §C) --------------------------------- #

def _table_doc(rows):
    """Hand-build a table doc: rows = list of (cell_type, text, colspan)."""
    def cell(ct, text, colspan=1):
        para = {"type": "paragraph"}
        if text:
            para["content"] = [{"type": "text", "text": text}]
        return {"type": ct, "attrs": {"colspan": colspan, "rowspan": 1},
                "content": [para]}
    return {"type": "doc", "content": [{
        "type": "table",
        "content": [{"type": "table_row",
                     "content": [cell(*c) for c in row]} for row in rows],
    }]}


def test_table_serializes_bordered_canonical():
    md = "Name | Value\n--- | ---\nfoo | bar\n"
    out = pm_to_markdown(markdown_to_pm(md))
    assert out == "| Name | Value |\n| --- | --- |\n| foo | bar |\n"


def test_table_roundtrip_is_idempotent():
    md = "## Budget\n\n| Item | Cost |\n| --- | --- |\n| Staff | 100 |\n"
    once = pm_to_markdown(markdown_to_pm(md))
    twice = pm_to_markdown(markdown_to_pm(once))
    assert once == twice
    assert markdown_to_pm(once) == markdown_to_pm(twice)


def test_pipe_in_cell_text_is_escaped():
    doc = _table_doc([[("table_header", "a | b")], [("table_cell", "x")]])
    out = pm_to_markdown(doc)
    assert "| a \\| b |" in out
    # and the escape round-trips back to the same literal text
    back = markdown_to_pm(out)
    header = back["content"][0]["content"][0]["content"][0]
    assert header["content"][0]["content"][0]["text"] == "a | b"


def test_pipe_inside_code_span_in_cell_is_escaped():
    doc = {"type": "doc", "content": [{"type": "table", "content": [
        {"type": "table_row", "content": [{
            "type": "table_header", "attrs": {"colspan": 1, "rowspan": 1},
            "content": [{"type": "paragraph", "content": [
                {"type": "text", "text": "x|y", "marks": [{"type": "code"}]},
            ]}],
        }]},
    ]}]}
    out = pm_to_markdown(doc)
    assert "`x\\|y`" in out
    back = markdown_to_pm(out)
    cell_text = back["content"][0]["content"][0]["content"][0]["content"][0]["content"][0]
    assert cell_text["text"] == "x|y"
    assert cell_text["marks"][0]["type"] == "code"


def test_hard_break_in_cell_degrades_to_space():
    doc = {"type": "doc", "content": [{"type": "table", "content": [
        {"type": "table_row", "content": [{
            "type": "table_header", "attrs": {"colspan": 1, "rowspan": 1},
            "content": [{"type": "paragraph", "content": [
                {"type": "text", "text": "a"},
                {"type": "hard_break"},
                {"type": "text", "text": "b"},
            ]}],
        }]},
    ]}]}
    assert "| a b |" in pm_to_markdown(doc)


def test_multi_block_cell_degrades_joined_with_space():
    # violates the schema on purpose (hostile shape via the unvalidated Yjs
    # read path) — the serializer degrades, never drops, never raises (spec §C)
    doc = {"type": "doc", "content": [{"type": "table", "content": [
        {"type": "table_row", "content": [{
            "type": "table_header", "attrs": {"colspan": 1, "rowspan": 1},
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "a"}]},
                {"type": "paragraph", "content": [{"type": "text", "text": "b"}]},
            ],
        }]},
    ]}]}
    assert "| a b |" in pm_to_markdown(doc)


def test_colspan_degrades_to_filler_cells():
    doc = _table_doc([
        [("table_header", "h1"), ("table_header", "h2")],
        [("table_cell", "wide", 2)],
    ])
    out = pm_to_markdown(doc)
    assert "| wide |  |" in out  # content once + one empty filler


def test_body_rows_padded_and_truncated_to_header_width():
    doc = _table_doc([
        [("table_header", "a"), ("table_header", "b")],
        [("table_cell", "1")],                                   # short → padded
        [("table_cell", "1"), ("table_cell", "2"), ("table_cell", "3")],  # long → truncated
    ])
    lines = pm_to_markdown(doc).splitlines()
    assert lines[2] == "| 1 |  |"
    assert lines[3] == "| 1 | 2 |"


def test_first_row_is_always_the_header():
    # td cells in row 1 / th cells in row 2 (hostile shape): serialize
    # positionally — row 1 header, row 2 body — deterministic degrade.
    doc = _table_doc([[("table_cell", "h")], [("table_header", "b")]])
    out = pm_to_markdown(doc)
    assert out == "| h |\n| --- |\n| b |\n"
    back = markdown_to_pm(out)
    assert back["content"][0]["content"][0]["content"][0]["type"] == "table_header"


def test_header_only_table_is_valid_gfm():
    doc = _table_doc([[("table_header", "only")]])
    out = pm_to_markdown(doc)
    assert out == "| only |\n| --- |\n"
    assert markdown_to_pm(out)["content"][0]["type"] == "table"


def test_no_table_line_ever_starts_with_hash():
    # sectionizer safety: bordered rows can't trip the ^## rule (spec §C)
    doc = _table_doc([[("table_header", "## not a heading")], [("table_cell", "x")]])
    out = pm_to_markdown(doc)
    for line in out.splitlines():
        assert line.startswith("| ")
    from call_manager.proposal_assist.editing.sectionizer import split_sections
    assert len(split_sections(out)) == 1  # no phantom section boundary


def test_table_in_blockquote_and_list_roundtrips():
    for md in [
        "> | a | b |\n> | --- | --- |\n> | 1 | 2 |\n",
        "## L\n\n- intro\n\n  | a | b |\n  | --- | --- |\n  | 1 | 2 |\n",
    ]:
        once = pm_to_markdown(markdown_to_pm(md))
        twice = pm_to_markdown(markdown_to_pm(once))
        assert once == twice, md
        assert markdown_to_pm(once) == markdown_to_pm(twice)


def test_synthesized_leading_paragraph_roundtrips():
    # Parse synthesizes the leading empty paragraph (Task 2); the serializer
    # drops it again so the REAL first block occupies the marker line — the
    # round-trip is idempotent for all non-paragraph-first item shapes.
    for md, marker_line in [
        ("- > q\n", "- > q"),
        ("- ```\n  x\n  ```\n", "- ```"),
        # marker line for the table case is locked by the T5 golden instead —
        # it depends on how markdown-it parses GFM-tables-in-list-items:
        ("- | a | b |\n  | --- | --- |\n", None),
    ]:
        once = pm_to_markdown(markdown_to_pm(md))
        twice = pm_to_markdown(markdown_to_pm(once))
        assert once == twice, md
        assert markdown_to_pm(once) == markdown_to_pm(twice), md
        if marker_line is not None:
            assert once.splitlines()[0] == marker_line, md


def test_empty_only_item_keeps_its_paragraph_and_roundtrips():
    once = pm_to_markdown(markdown_to_pm("- a\n-\n- b\n"))
    twice = pm_to_markdown(markdown_to_pm(once))
    assert once == twice
    assert markdown_to_pm(once) == markdown_to_pm(twice)
```

- [ ] **Step 2: Run to verify failure**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_markdown_bridge.py -p no:cacheprovider -q`
Expected: new tests FAIL — `pm_to_markdown` drops the table (`_serialize_block` unknown-type → `""`).

- [ ] **Step 3: Implement.** Edits in `markdown_bridge.py`:

(a) Thread `in_cell` through inline serialization. `_serialize_inline` signature becomes `def _serialize_inline(nodes: list[dict], *, in_cell: bool = False) -> str:` and its atom call becomes `out.append(_serialize_inline_atom(node, in_cell=in_cell))`. `_serialize_inline_atom` becomes `def _serialize_inline_atom(node: dict, *, in_cell: bool = False) -> str:` with ONE behavioral change — the hard_break branch:

```python
    if ntype == "hard_break":
        # In a table cell a "\"-newline would split the row: degrade to a
        # single space (spec §C). Outside cells, unchanged.
        return " " if in_cell else "\\\n"
```

(No other caller changes — paragraph/heading/blockquote keep the default.)

(b) Add the table case to `_serialize_block`, before the final `return ""`:

```python
    if ntype == "table":
        return _serialize_table(node)
```

(c) **Serializer companion to Task 2's synthesis** — at the TOP of `_serialize_item_blocks` (before the loop), drop a leading empty paragraph when more blocks follow:

```python
    # Companion to the parser's leading-paragraph synthesis (spec §B): an EMPTY
    # leading paragraph followed by more blocks is dropped, so the real first
    # block occupies the marker line ("- > q", "- | a | b |"). Emitting it
    # would place a blank line between the list marker and the first block —
    # CommonMark allows at most ONE blank line at an item start, so the item
    # would terminate empty and the following block would ESCAPE the list on
    # re-parse. Dropping it here + re-synthesizing on parse = idempotent.
    # (An item that is ONLY an empty paragraph keeps it: "- " re-parses as an
    # empty item.)
    if (
        len(blocks) > 1
        and blocks[0]["type"] == "paragraph"
        and not blocks[0].get("content")
    ):
        blocks = blocks[1:]
```

Extend `_serialize_item_blocks`'s docstring with one sentence naming this rule.

(d) Add the serializer functions after `_serialize_list`:

```python
def _cell_text(cell: dict) -> str:
    """Flatten one cell to single-line, pipe-safe inline markdown.

    Cells hold one paragraph by schema; anything else (hostile injection —
    the Yjs read path is unvalidated) degrades: each block's INLINE content
    serializes in cell mode and the pieces join with single spaces — degrade,
    never drop, never raise (spec §C). The trailing replaces are uniform and
    deliberate: GFM splits rows on unescaped pipes BEFORE inline parsing, so
    EVERY literal "|" — plain text, code-span bodies, link targets — must
    leave here as "\\|" (GFM unescapes it at row level on re-parse), and any
    stray newline (softbreak text) must flatten to a space.
    """
    parts: list[str] = []
    for block in cell.get("content", []):
        text = _serialize_inline(block.get("content", []), in_cell=True)
        if text:
            parts.append(text)
    return " ".join(parts).replace("\n", " ").replace("|", "\\|")


def _row_line(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _serialize_table(node: dict) -> str:
    """Strict-GFM table serialization — the table loose-form law (spec §C).

    Always bordered (every line starts "| " — the sectionizer-safety
    invariant); row 1 is the header REGARDLESS of cell types; column count is
    the header width after colspan expansion; body rows pad/truncate to it;
    colspan > 1 degrades to content + empty filler cells; rowspan is ignored
    (spanned-over cells are absent in the PM shape — padding fills the gap).
    """
    rows = node.get("content", [])
    if not rows:
        return ""

    def expand(row: dict) -> list[str]:
        cells: list[str] = []
        for cell in row.get("content", []):
            cells.append(_cell_text(cell))
            span = cell.get("attrs", {}).get("colspan", 1) or 1
            cells.extend([""] * (int(span) - 1))
        return cells

    header = expand(rows[0])
    width = max(1, len(header))

    def fit(cells: list[str]) -> list[str]:
        cells = cells[:width]
        return cells + [""] * (width - len(cells))

    lines = [_row_line(fit(header)), _row_line(["---"] * width)]
    for row in rows[1:]:
        lines.append(_row_line(fit(expand(row))))
    return "\n".join(lines)
```

Also extend the module docstring's serializer paragraph with one sentence: tables serialize always-bordered with the first row as header (spec §C).

- [ ] **Step 4: Run to verify pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_markdown_bridge.py tests/proposal_assist/collab/test_full_pipeline.py tests/proposal_assist/collab/test_schema.py -p no:cacheprovider -q`
Expected: all PASS (full_pipeline still runs the OLD corpus — unchanged until Task 5).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/collab/markdown_bridge.py tests/proposal_assist/collab/test_markdown_bridge.py
git commit -m "feat(collab): GFM table serializer — always-bordered loose form (tables T3)"
```

---

### Task 4: Frontend schema — Tiptap table extensions + twins + equality guard

**Files:**
- Modify: `frontend/package.json` (via pnpm), `frontend/src/collab/schema.ts`, `frontend/scripts/collab-schema.mjs`, `frontend/src/collab/collabSchema.equality.test.ts`
- Test: `frontend/src/collab/collabSchema.equality.test.ts` (extended), new `frontend/src/collab/schema.tables.test.tsx`

**Interfaces:**
- Consumes: nothing from earlier tasks (parallel-safe with T2/T3, but lands after them per server-first ordering).
- Produces: `collabExtensions` includes `table`/`table_row`/`table_header`/`table_cell` nodes (snake_case names, constrained); `collab-schema.mjs` `schema` gains the same 4 nodes; equality test compares `tableRole` + `isolating` on every node. Task 5 (goldens) and Task 7 (UI) build on these.

- [ ] **Step 1: Add the package**

```bash
cd frontend && pnpm add @tiptap/extension-table@3.27.1 --config.minimumReleaseAge=0
```

- [ ] **Step 2: Write the failing tests.** (a) In `collabSchema.equality.test.ts`, extend `nodeShape`'s returned object with two fields:

```ts
    isolating: !!n.isolating,
    tableRole: (n as any).tableRole ?? null,
```

(b) Create `frontend/src/collab/schema.tables.test.tsx`:

```tsx
// Headless command tests for the constrained table nodes — same pattern as
// schema.formatting.test.tsx (real Editor, no DOM mount).
import { Editor } from "@tiptap/core";
import { afterEach, describe, expect, it } from "vitest";
import { collabExtensions } from "./schema";

let editor: Editor;
afterEach(() => editor?.destroy());
const mk = (content?: string) => {
  editor = new Editor({ extensions: collabExtensions, content });
  return editor;
};

describe("strict-GFM table nodes", () => {
  it("insertTable creates rows x cols with a header row", () => {
    const e = mk();
    e.commands.insertTable({ rows: 3, cols: 3, withHeaderRow: true });
    const table = e.getJSON().content?.find((n) => n.type === "table");
    expect(table?.content).toHaveLength(3);
    expect(table?.content?.[0].content?.every((c) => c.type === "table_header")).toBe(true);
    expect(table?.content?.[1].content?.every((c) => c.type === "table_cell")).toBe(true);
  });

  it("cells hold exactly one paragraph", () => {
    const e = mk();
    e.commands.insertTable({ rows: 2, cols: 2, withHeaderRow: true });
    const cell = e.getJSON().content?.find((n) => n.type === "table")!
      .content![1].content![0];
    expect(cell.content).toHaveLength(1);
    expect(cell.content![0].type).toBe("paragraph");
  });

  it("pasted colspan/rowspan normalize to 1 (ragged rows get filled)", () => {
    const e = mk();
    e.commands.insertContent(
      '<table><tr><th colspan="2">h</th></tr><tr><td>a</td><td>b</td></tr></table>',
    );
    const table = e.getJSON().content?.find((n) => n.type === "table");
    for (const row of table?.content ?? []) {
      for (const cell of row.content ?? []) {
        expect(cell.attrs?.colspan ?? 1).toBe(1);
        expect(cell.attrs?.rowspan ?? 1).toBe(1);
        expect(cell.attrs?.colwidth ?? null).toBeNull();
      }
    }
  });

  it("row/column commands work (addRowAfter / addColumnAfter / deleteTable)", () => {
    const e = mk();
    e.commands.insertTable({ rows: 2, cols: 2, withHeaderRow: true });
    e.commands.addRowAfter();
    expect(e.getJSON().content?.find((n) => n.type === "table")?.content).toHaveLength(3);
    e.commands.addColumnAfter();
    const row0 = e.getJSON().content?.find((n) => n.type === "table")?.content?.[0];
    expect(row0?.content).toHaveLength(3);
    e.commands.deleteTable();
    expect(e.getJSON().content?.some((n) => n.type === "table")).toBe(false);
  });

  it("goToNextCell navigates cells (Tab contract)", () => {
    const e = mk();
    e.commands.insertTable({ rows: 2, cols: 2, withHeaderRow: true });
    expect(e.commands.goToNextCell()).toBe(true);
  });
});
```

- [ ] **Step 3: Run to verify failure**

Run: `cd frontend && pnpm vitest run src/collab/schema.tables.test.tsx src/collab/collabSchema.equality.test.ts`
Expected: FAIL — no table nodes in either schema source yet.

- [ ] **Step 4: Implement.** (a) In `schema.ts`, after the `ListItem` definition, add:

```ts
import {
  Table as BaseTable,
  TableCell as BaseTableCell,
  TableHeader as BaseTableHeader,
  TableRow as BaseTableRow,
} from "@tiptap/extension-table";
```

(imports go at the top of the file with the existing imports) and:

```ts
// Strict-GFM constrained tables (2026-07-08 collab-tables spec §A/§E):
// snake_case names to match the server schema; cells hold ONE paragraph;
// attr-level parseHTML pins colspan/rowspan/colwidth to their defaults so a
// pasted Word/HTML merge normalizes to 1x1 cells (prosemirror-tables'
// fixTables pads the resulting ragged rows); resizing off — colwidth can
// never be set. The server serializer's degrade law is the backstop.
const gfmCellAttributes = () => ({
  colspan: { default: 1, parseHTML: () => 1 },
  rowspan: { default: 1, parseHTML: () => 1 },
  colwidth: { default: null, parseHTML: () => null },
});
const Table = BaseTable.extend({ name: "table", content: "table_row+" }).configure({
  resizable: false,
});
const TableRow = BaseTableRow.extend({
  name: "table_row",
  content: "(table_cell | table_header)+",
});
const TableHeader = BaseTableHeader.extend({
  name: "table_header",
  content: "paragraph",
  addAttributes: gfmCellAttributes,
});
const TableCell = BaseTableCell.extend({
  name: "table_cell",
  content: "paragraph",
  addAttributes: gfmCellAttributes,
});
```

and extend the exported list:

```ts
export const collabExtensions: Extensions = [
  Doc, Text, Paragraph, Heading, Blockquote, HorizontalRule, CodeBlock, HardBreak,
  Image, BulletList, OrderedList, ListItem, Citation, Link, Em, Strong, Code, Strikethrough,
  Table, TableRow, TableHeader, TableCell,
];
```

(b) In `collab-schema.mjs`, add the four specs before the `Schema` construction and register them in the `nodes` map after `list_item`:

```js
// --- Strict-GFM tables (2026-07-08 collab-tables spec §A) -------------------
// Shape-identical to the Tiptap extensions in src/collab/schema.ts and the
// Python schema.py: content strings, attrs defaults, isolating, tableRole.

/** @type {import("prosemirror-model").NodeSpec} */
const tableSpec = {
  content: "table_row+",
  group: "block",
  isolating: true,
  tableRole: "table",
  parseDOM: [{ tag: "table" }],
  toDOM() { return ["table", ["tbody", 0]]; },
};

/** @type {import("prosemirror-model").NodeSpec} */
const tableRowSpec = {
  content: "(table_cell | table_header)+",
  tableRole: "row",
  parseDOM: [{ tag: "tr" }],
  toDOM() { return ["tr", 0]; },
};

const gfmCellAttrs = {
  colspan: { default: 1 },
  rowspan: { default: 1 },
  colwidth: { default: null },
};

/** @type {import("prosemirror-model").NodeSpec} */
const tableCellSpec = {
  content: "paragraph",
  attrs: { ...gfmCellAttrs },
  isolating: true,
  tableRole: "cell",
  parseDOM: [{ tag: "td" }],
  toDOM() { return ["td", 0]; },
};

/** @type {import("prosemirror-model").NodeSpec} */
const tableHeaderSpec = {
  content: "paragraph",
  attrs: { ...gfmCellAttrs },
  isolating: true,
  tableRole: "header_cell",
  parseDOM: [{ tag: "th" }],
  toDOM() { return ["th", 0]; },
};
```

```js
    list_item: listItemSpec,
    table: tableSpec,
    table_row: tableRowSpec,
    table_cell: tableCellSpec,
    table_header: tableHeaderSpec,
```

Update both file-header comments (schema.ts note block untouched; mjs doc comment node list gains the 4 nodes; same for `schema.py`'s docstring if Task 1 missed it).

- [ ] **Step 5: Run to verify pass**

Run: `cd frontend && pnpm vitest run src/collab/ && pnpm exec tsc --noEmit`
Expected: all green, including every `node %s matches structurally` case with the new `tableRole`/`isolating` fields. If the equality test reports a genuine engine-injected deviation on the table nodes (e.g. Tiptap normalizing a spec field), STOP and report it — do not paper over it by weakening the shape function.

- [ ] **Step 6: Commit**

```bash
git add frontend/package.json frontend/pnpm-lock.yaml frontend/src/collab/schema.ts frontend/scripts/collab-schema.mjs frontend/src/collab/collabSchema.equality.test.ts frontend/src/collab/schema.tables.test.tsx
git commit -m "feat(collab): Tiptap strict-GFM table nodes + schema-twin parity guard (tables T4)"
```

---

### Task 5: Corpus + goldens — cross-runtime table parity

**Files:**
- Modify: `tests/proposal_assist/collab/fixtures.py`, `frontend/scripts/gen_collab_goldens.mjs`
- Regenerate: `tests/proposal_assist/collab/goldens/*.json` (new fixtures), `goldens/python_writepath.json` + `goldens/python_writepath_incremental.json` (via `REGEN_WRITEPATH=1`)
- Test: existing corpus-driven suites (`test_full_pipeline.py`, `test_collab_writepath.py`, `test_markdown_bridge.py` corpus tests) + `node frontend/scripts/verify_python_writepath.mjs`

**Interfaces:**
- Consumes: Tasks 1–4 (Python parse/serialize + JS schema twins).
- Produces: 10 new corpus fixtures present BYTE-IDENTICALLY in both `fixtures.py` `CORPUS` and the gen script's `CORPUS` (16 existing + 10 = 26 goldens); goldens regenerated and committed. Gate: the two corpora may never diverge (`test_js_and_python_corpora_match`).

- [ ] **Step 1: Add fixtures to `fixtures.py` `CORPUS`** (append before the closing bracket):

```python
    # --- tables (2026-07-08 collab-tables spec §D) --- #
    ("table_basic",
     "## Budget\n\n| Item | Cost |\n| --- | --- |\n| Staff | 100 |\n| Lab | 50 |\n"),
    ("table_cell_escapes",
     "## T\n\n| a \\| b | `x\\|y` |\n| --- | --- |\n| *em* text | plain |\n"),
    ("table_borderless_normalizes",
     "## T\n\nName | Value\n--- | ---\nfoo | bar\n"),
    ("table_cite_in_cell",
     f"## Claims\n\n| Claim | Source |\n| --- | --- |\n| Sky is blue | {_CITE} |\n"),
    ("table_header_only", "| Only | Header |\n| --- | --- |\n"),
    ("table_in_blockquote", "> | a | b |\n> | --- | --- |\n> | 1 | 2 |\n"),
    ("table_in_list_item",
     "## L\n\n- intro\n\n  | a | b |\n  | --- | --- |\n  | 1 | 2 |\n"),
    # list-item leading-paragraph synthesis (spec §B) — must match JS createAndFill:
    ("table_first_list_item", "- | a | b |\n  | --- | --- |\n"),
    ("quote_first_list_item", "- > quoted\n"),
    ("empty_list_item", "- a\n-\n- b\n"),
```

- [ ] **Step 2: Teach the goldens generator tables.** In `gen_collab_goldens.mjs`:

(a) enable the rule and wrap cell inlines in paragraph tokens (our cells hold `paragraph`, but markdown-it puts inline content directly in th/td — a core rule injects the wrappers so `MarkdownParser` builds the right shape):

```js
const md = new MarkdownIt('commonmark', { html: true })
  .enable('strikethrough')
  .enable('table');
// Our table cells hold ONE paragraph (strict GFM, spec §A) but markdown-it
// emits inline content directly inside th/td. Wrap each cell's inline run in
// paragraph tokens so MarkdownParser constructs table_cell > paragraph.
md.core.ruler.push('wrap_table_cells', (state) => {
  const out = [];
  for (const tok of state.tokens) {
    if (tok.type === 'th_close' || tok.type === 'td_close') {
      out.push(new state.Token('paragraph_close', 'p', -1));
    }
    out.push(tok);
    if (tok.type === 'th_open' || tok.type === 'td_open') {
      out.push(new state.Token('paragraph_open', 'p', 1));
    }
  }
  state.tokens = out;
  return true;
});
```

(b) add token specs to the `MarkdownParser` map (after `hr`):

```js
  table:        { block: 'table' },
  tr:           { block: 'table_row' },
  th:           { block: 'table_header' },
  td:           { block: 'table_cell' },
  thead:        { ignore: true },
  tbody:        { ignore: true },
```

(c) append the same 10 fixtures to the script's `CORPUS` (byte-identical strings; JS single-quoted, so `\\|` in the JS source = backslash-pipe, same bytes as Python's `"\\|"`):

```js
  // --- tables (2026-07-08 collab-tables spec §D) --- //
  ['table_basic',               '## Budget\n\n| Item | Cost |\n| --- | --- |\n| Staff | 100 |\n| Lab | 50 |\n'],
  ['table_cell_escapes',        '## T\n\n| a \\| b | `x\\|y` |\n| --- | --- |\n| *em* text | plain |\n'],
  ['table_borderless_normalizes', '## T\n\nName | Value\n--- | ---\nfoo | bar\n'],
  ['table_cite_in_cell',        `## Claims\n\n| Claim | Source |\n| --- | --- |\n| Sky is blue | ${CITE} |\n`],
  ['table_header_only',         '| Only | Header |\n| --- | --- |\n'],
  ['table_in_blockquote',       '> | a | b |\n> | --- | --- |\n> | 1 | 2 |\n'],
  ['table_in_list_item',        '## L\n\n- intro\n\n  | a | b |\n  | --- | --- |\n  | 1 | 2 |\n'],
  ['table_first_list_item',     '- | a | b |\n  | --- | --- |\n'],
  ['quote_first_list_item',     '- > quoted\n'],
  ['empty_list_item',           '- a\n-\n- b\n'],
```

- [ ] **Step 3: Regenerate goldens**

```bash
cd frontend && node scripts/gen_collab_goldens.mjs
```

Expected: `26/26 goldens generated and self-verified.` If `table_first_list_item`, `quote_first_list_item`, or `empty_list_item` fails self-verification, prosemirror-markdown's `createAndFill` did NOT synthesize the leading paragraph the way Python does — STOP and report (the synthesis laws must match; do not adjust the Python side unilaterally). Inspect `tests/proposal_assist/collab/goldens/quote_first_list_item.json`: the list item's first child must be `{"type": "paragraph"}` with no content key.

- [ ] **Step 4: Regenerate the Python write-path artifacts + run the corpus suites**

```bash
REGEN_WRITEPATH=1 EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_writepath.py -p no:cacheprovider -q
EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_full_pipeline.py tests/proposal_assist/collab/test_collab_writepath.py tests/proposal_assist/collab/test_markdown_bridge.py -p no:cacheprovider -q
cd frontend && node scripts/verify_python_writepath.mjs && pnpm vitest run
```

Expected: all green. `test_js_and_python_corpora_match` proves byte-parity; the full-pipeline raw-fidelity test proves Python `markdown_to_pm` output deep-equals every JS golden `pm_json` — including the canonical `{"colspan": 1, "rowspan": 1}`-and-no-`colwidth` cell form.

- [ ] **Step 5: Commit** (include every regenerated golden explicitly)

```bash
git add tests/proposal_assist/collab/fixtures.py frontend/scripts/gen_collab_goldens.mjs tests/proposal_assist/collab/goldens/
git commit -m "test(collab): table corpus fixtures + regenerated cross-runtime goldens (tables T5)"
```

---

### Task 6: Backend integration — tables through checkpoint, kernel, and handover

**Files:**
- Test only: `tests/proposal_assist/collab/test_kernel_section_scoped.py`, `tests/proposal_assist/collab/test_collab_kill_switch_handover.py`, `tests/proposal_assist/collab/test_collab_apply_fragment.py`

**Interfaces:**
- Consumes: Tasks 1–5. Zero production code — if any of these tests exposes a production gap, STOP and report BLOCKED with the failure (the fix belongs in the bridge tasks, not inline here).

- [ ] **Step 1: Section-scoped kernel writes around a table.** In `test_kernel_section_scoped.py`, read the file's existing tests first and mirror their exact helper/fixture usage (do not invent new plumbing). Add two tests with this markdown and these assertions:

```python
_TABLE_SECTIONED = (
    "## Intro\n\nprose.\n\n"
    "## Budget\n\n| Item | Cost |\n| --- | --- |\n| Staff | 100 |\n\n"
    "## Outro\n\nend.\n"
)

def test_section_write_beside_a_table_preserves_it(...existing fixtures...):
    # seed a doc/fragment from _TABLE_SECTIONED exactly as the sibling test does,
    # apply a section-scoped edit to "## Intro" (new body text), then:
    out = pm_to_markdown(ydoc_to_pm_json(doc))
    assert "| Item | Cost |" in out            # table untouched
    assert "| --- | --- |" in out
    assert "new intro body" in out             # the edit landed


def test_section_write_INTO_a_table_section_roundtrips(...existing fixtures...):
    # seed from _TABLE_SECTIONED as above, then apply a section-scoped edit TO
    # the "## Budget" section itself via the sibling test's helper, with:
    new_budget = "## Budget\n\n| Item | Cost |\n| --- | --- |\n| Staff | 120 |\n"
    # assert: no SectionMapDivergenceError; then
    out = pm_to_markdown(ydoc_to_pm_json(doc))
    assert "| Staff | 120 |" in out       # edit landed, bordered form
    assert "| Staff | 100 |" not in out   # old table row replaced
    assert "prose." in out and "end." in out  # neighbors untouched
```

- [ ] **Step 2: Kill-switch handover drains tables to GFM.** In `test_collab_kill_switch_handover.py`, mirror the existing draft drain test (its helpers take `artifact="draft"` defaults) with a doc containing `_TABLE_SECTIONED`-style content applied to the room log; assert after `run_kill_switch_handover`:
  - the committed git markdown contains `"| Item | Cost |"` and `"| --- | --- |"` (bordered GFM, spec §5),
  - re-parsing the committed markdown yields a `table` node (`markdown_to_pm(committed)["content"]` contains a table) — the drain → CM6 → re-hydrate loop is lossless for tables.

- [ ] **Step 3: Whole-doc fragment apply with tables.** In `test_collab_apply_fragment.py`, add one round-trip test in the file's existing style:

```python
def test_table_doc_survives_fragment_apply_roundtrip():
    md = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    pm = markdown_to_pm(md)
    ydoc = pm_json_to_ydoc(pm)
    assert ydoc_to_pm_json(ydoc) == pm          # attrs survive the Yjs coercion
    assert pm_to_markdown(ydoc_to_pm_json(ydoc)) == pm_to_markdown(pm)
```

- [ ] **Step 4: Run**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_kernel_section_scoped.py tests/proposal_assist/collab/test_collab_kill_switch_handover.py tests/proposal_assist/collab/test_collab_apply_fragment.py -p no:cacheprovider -q`
Expected: all PASS with zero production edits.

- [ ] **Step 5: Commit**

```bash
git add tests/proposal_assist/collab/test_kernel_section_scoped.py tests/proposal_assist/collab/test_collab_kill_switch_handover.py tests/proposal_assist/collab/test_collab_apply_fragment.py
git commit -m "test(collab): tables through section kernel, handover, and fragment apply (tables T6)"
```

---

### Task 7: Frontend UI — Insert table + table menu + first editor CSS

**Files:**
- Create: `frontend/src/collab/TableControls.tsx`, `frontend/src/collab/TableControls.test.tsx`, `frontend/src/collab/CollabEditor.module.css`
- Modify: `frontend/src/collab/CollabEditor.tsx:80-102` (`CollabEditorInner` render)

**Interfaces:**
- Consumes: Task 4's extensions (`insertTable`, `addRowBefore/After`, `addColumnBefore/After`, `deleteRow`, `deleteColumn`, `deleteTable` commands).
- Produces: `InsertTableButton({ editor, canWrite })` and `TableMenu({ editor, canWrite })` React components. Design note (deliberate deviation from the spec's "floating" wording): the menu is a conditional in-flow strip that renders only while the caret is inside a table — same interaction contract, no `@floating-ui/dom` dependency (Tiptap v3's BubbleMenu requires it), robust in jsdom.

- [ ] **Step 1: Write the failing tests** — `frontend/src/collab/TableControls.test.tsx`:

```tsx
import { Editor } from "@tiptap/core";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { afterEach, describe, expect, it } from "vitest";
import { collabExtensions } from "./schema";
import { InsertTableButton, TableMenu } from "./TableControls";

let editor: Editor;
afterEach(() => editor?.destroy());
const mk = (content?: string) => {
  editor = new Editor({ extensions: collabExtensions, content });
  return editor;
};
const ui = (node: React.ReactNode) => render(<MantineProvider>{node}</MantineProvider>);

describe("InsertTableButton", () => {
  it("inserts a 3x3 table with header row", async () => {
    const e = mk();
    ui(<InsertTableButton editor={e} canWrite />);
    await userEvent.click(screen.getByTestId("insert-table"));
    const table = e.getJSON().content?.find((n) => n.type === "table");
    expect(table?.content).toHaveLength(3);
    expect(table?.content?.[0].content).toHaveLength(3);
    expect(table?.content?.[0].content?.[0].type).toBe("table_header");
  });

  it("is disabled when read-only and when already inside a table", () => {
    const e = mk();
    e.commands.insertTable({ rows: 2, cols: 2, withHeaderRow: true });
    // caret is inside the just-inserted table
    const { rerender } = ui(<InsertTableButton editor={e} canWrite />);
    expect(screen.getByTestId("insert-table")).toBeDisabled();
    rerender(<MantineProvider><InsertTableButton editor={mk()} canWrite={false} /></MantineProvider>);
    expect(screen.getByTestId("insert-table")).toBeDisabled();
  });
});

describe("TableMenu", () => {
  it("renders nothing when the caret is outside a table", () => {
    ui(<TableMenu editor={mk("<p>x</p>")} canWrite />);
    expect(screen.queryByTestId("table-menu")).toBeNull();
  });

  it("shows controls inside a table and adds a row below", async () => {
    const e = mk();
    e.commands.insertTable({ rows: 2, cols: 2, withHeaderRow: true });
    ui(<TableMenu editor={e} canWrite />);
    expect(screen.getByTestId("table-menu")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("table-add-row-below"));
    expect(e.getJSON().content?.find((n) => n.type === "table")?.content).toHaveLength(3);
  });

  it("disables Row above / Delete row while in the header row", () => {
    const e = mk();
    e.commands.insertTable({ rows: 2, cols: 2, withHeaderRow: true });
    // insertTable leaves the caret in the FIRST (header) cell
    ui(<TableMenu editor={e} canWrite />);
    expect(screen.getByTestId("table-add-row-above")).toBeDisabled();
    expect(screen.getByTestId("table-delete-row")).toBeDisabled();
  });

  it("delete table removes the table entirely", async () => {
    const e = mk();
    e.commands.insertTable({ rows: 2, cols: 2, withHeaderRow: true });
    ui(<TableMenu editor={e} canWrite />);
    await userEvent.click(screen.getByTestId("table-delete-table"));
    expect(e.getJSON().content?.some((n) => n.type === "table")).toBe(false);
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd frontend && pnpm vitest run src/collab/TableControls.test.tsx`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `TableControls.tsx`**

```tsx
// Table creation + structure controls for the collab editor (2026-07-08
// collab-tables spec §F). The menu is a conditional strip — visible only
// while the caret is inside a table — not a cursor-anchored popover: same
// appears-when-relevant contract without the @floating-ui/dom dependency
// Tiptap v3 menus require.
import { Button, Group, Paper } from "@mantine/core";
import type { Editor } from "@tiptap/core";
import { useEffect, useReducer } from "react";

/** Re-render on every editor transaction so isActive()/can() stay fresh. */
function useEditorTick(editor: Editor | null) {
  const [, bump] = useReducer((n: number) => n + 1, 0);
  useEffect(() => {
    if (!editor) return;
    editor.on("transaction", bump);
    return () => {
      editor.off("transaction", bump);
    };
  }, [editor]);
}

/** True when the selection head sits in the FIRST row of its table. */
function inHeaderRow(editor: Editor): boolean {
  const { $from } = editor.state.selection;
  for (let d = $from.depth; d > 0; d--) {
    if ($from.node(d).type.name === "table_row") {
      return $from.index(d - 1) === 0; // row index within the table
    }
  }
  return false;
}

export function InsertTableButton(props: { editor: Editor | null; canWrite: boolean }) {
  useEditorTick(props.editor);
  if (!props.editor) return null;
  const disabled = !props.canWrite || props.editor.isActive("table");
  return (
    <Button
      size="compact-xs"
      variant="default"
      data-testid="insert-table"
      disabled={disabled}
      onClick={() =>
        props.editor
          ?.chain()
          .focus()
          .insertTable({ rows: 3, cols: 3, withHeaderRow: true })
          .run()
      }
    >
      Insert table
    </Button>
  );
}

export function TableMenu(props: { editor: Editor | null; canWrite: boolean }) {
  useEditorTick(props.editor);
  const { editor } = props;
  if (!editor || !props.canWrite || !editor.isActive("table")) return null;
  const header = inHeaderRow(editor);
  const run = (fn: (c: ReturnType<Editor["chain"]>) => ReturnType<Editor["chain"]>) => () =>
    fn(editor.chain().focus()).run();
  return (
    <Paper withBorder p={4} data-testid="table-menu" role="toolbar" aria-label="Table controls">
      <Group gap={4}>
        <Button size="compact-xs" variant="subtle" data-testid="table-add-row-above" disabled={header} onClick={run((c) => c.addRowBefore())}>Row above</Button>
        <Button size="compact-xs" variant="subtle" data-testid="table-add-row-below" onClick={run((c) => c.addRowAfter())}>Row below</Button>
        <Button size="compact-xs" variant="subtle" data-testid="table-add-col-left" onClick={run((c) => c.addColumnBefore())}>Col left</Button>
        <Button size="compact-xs" variant="subtle" data-testid="table-add-col-right" onClick={run((c) => c.addColumnAfter())}>Col right</Button>
        <Button size="compact-xs" variant="subtle" color="red" data-testid="table-delete-row" disabled={header} onClick={run((c) => c.deleteRow())}>Delete row</Button>
        <Button size="compact-xs" variant="subtle" color="red" data-testid="table-delete-col" onClick={run((c) => c.deleteColumn())}>Delete col</Button>
        <Button size="compact-xs" variant="subtle" color="red" data-testid="table-delete-table" onClick={run((c) => c.deleteTable())}>Delete table</Button>
      </Group>
    </Paper>
  );
}
```

(Header-row guard rationale: GFM requires a header row, and adding a row above it or deleting it would break the first-row-is-header law — spec §F.)

- [ ] **Step 4: First editor CSS.** Create `frontend/src/collab/CollabEditor.module.css`. Before writing it, read `frontend/src/theme/theme.css` and substitute the real `--app-*` token names for the fallback literals below (keep the fallbacks):

```css
/* First real stylesheet for the collab editor surface — tables need visible
   structure (spec §F). Everything scoped under the hashed .editor class per
   repo convention (never bare element selectors in a .module.css). */
.editor :global(.ProseMirror) table {
  border-collapse: collapse;
  width: 100%;
  margin: 0.5rem 0;
  table-layout: fixed;
}
.editor :global(.ProseMirror) th,
.editor :global(.ProseMirror) td {
  border: 1px solid var(--app-border, #ced4da);
  padding: 0.3rem 0.5rem;
  text-align: left;
  vertical-align: top;
}
.editor :global(.ProseMirror) th {
  background: var(--app-surface-2, rgba(0, 0, 0, 0.04));
  font-weight: 600;
}
.editor :global(.ProseMirror) td.selectedCell,
.editor :global(.ProseMirror) th.selectedCell {
  outline: 2px solid var(--app-accent, teal);
  outline-offset: -2px;
}
```

- [ ] **Step 5: Wire into `CollabEditorInner`.** In `CollabEditor.tsx`, import the pieces:

```tsx
import styles from "./CollabEditor.module.css";
import { InsertTableButton, TableMenu } from "./TableControls";
import { Group } from "@mantine/core";
```

and change the render (root div gains the class; controls sit with the save button; menu above the content):

```tsx
  return (
    <div data-collab-status={props.status} className={styles.editor}>
      <Group gap="xs">
        <SaveVersionButton
          id={props.workspaceId}
          artifact={props.artifact}
          provider={props.provider}
          canWrite={props.canWrite}
          status={props.status}
        />
        <InsertTableButton editor={editor} canWrite={props.canWrite} />
      </Group>
      <TableMenu editor={editor} canWrite={props.canWrite} />
      <EditorContent editor={editor} data-testid="collab-editor" />
      {/* selection toolbar block unchanged */}
```

- [ ] **Step 6: Run to verify pass**

Run: `cd frontend && pnpm vitest run src/collab/ && pnpm exec tsc --noEmit`
Expected: all green, including the untouched `CollabEditor.test.tsx` (its provider mock is unaffected — the new components only need the editor instance).

- [ ] **Step 7: Commit**

```bash
git add frontend/src/collab/TableControls.tsx frontend/src/collab/TableControls.test.tsx frontend/src/collab/CollabEditor.module.css frontend/src/collab/CollabEditor.tsx
git commit -m "feat(collab): Insert-table control + table menu + first editor table CSS (tables T7)"
```

---

### Task 8: Remove the pipe-table hold-back (LAST code task)

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/flip.py` (full rewrite below), `src/call_manager/web/api/workspaces.py:282,333` (the two `maybe_auto_enable` call sites), `tests/proposal_assist/collab/test_collab_flip.py`, `tests/proposal_assist/collab/test_collab_flip_readmodel.py`, `docs/superpowers/specs/2026-07-07-collab-slice3b5d4-flip-design.md` (one amendment line)
- Delete: `scripts/collab_table_audit.py`

**Interfaces:**
- Consumes: everything before it — the bridge MUST round-trip tables before this lands (spec §G sequencing).
- Produces: `maybe_auto_enable(session, *, workspace_id, artifact) -> bool` — the `head_md` and `artifact_path` parameters are REMOVED (they existed only for the detector); `has_pipe_tables`, `_TABLE_ROW`, `_DELIMITER_ROW`, `_log_held_back` deleted. The commit mutex acquisition STAYS — it is the linearization anchor for the D4 severance-recheck family (spec §G), not detector plumbing.

- [ ] **Step 1: Rewrite `flip.py`** to exactly:

```python
"""Enable-on-touch flip seam (3b-5d.D4): the ONLY prod caller of enable_collab.

maybe_auto_enable is invoked from the draft/spec read models — the first read
of an artifact (kill-switch off) enables collab for it, stickily and
per-artifact. New workspaces converge on their first cockpit load; existing
ones on their next open. The D4 pipe-table hold-back was REMOVED by the tables
slice (docs/superpowers/specs/2026-07-08-collab-tables-design.md §G) once the
bridge gained lossless GFM table round-trip — previously held-back artifacts
now enable lazily on their next read."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from call_manager.proposal_assist.collab import state
from call_manager.proposal_assist.drafting.locking import (
    CommitMutexTimeout,
    acquire_commit_mutex,
)

logger = logging.getLogger(__name__)

# The flip is a read-path side effect and must not stall an interactive GET on a
# wedged writer. It acquires the commit mutex ONCE per artifact lifetime (the
# is_collab_enabled short-circuit fires forever after), so a bounded, modest
# timeout is safe: on contention we defer the flip and the NEXT read retries.
_FLIP_MUTEX_TIMEOUT_SECONDS = 5


def maybe_auto_enable(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    artifact: str,
) -> bool:
    """Sticky lazy flip. Returns the artifact's EFFECTIVE enabled state.

    Enables iff: kill-switch off AND not already enabled. The enable is
    committed while HOLDING the workspace commit mutex — the linearization
    anchor the D4 severance-recheck family (writers' under-mutex re-reads)
    depends on; a route blocked on the mutex re-checks the flag after us and
    sees the enable. If the mutex can't be acquired (a writer holds it), we
    defer: return False and stay legacy — the next read retries. COMMITS the
    enable itself (read models otherwise never write; the session factory is
    expire_on_commit=False so callers' loaded rows survive). The concurrent
    first-read insert race resolves via IntegrityError → the other request
    won → report enabled."""
    if state.kill_switch_engaged():
        return False
    if state.is_collab_enabled(session, workspace_id=workspace_id, artifact=artifact):
        return True
    try:
        acquire_commit_mutex(session, workspace_id, timeout_seconds=_FLIP_MUTEX_TIMEOUT_SECONDS)
    except CommitMutexTimeout:
        session.rollback()
        return False
    state.enable_collab(session, workspace_id=workspace_id, artifact=artifact)
    try:
        session.commit()  # releases the mutex
    except IntegrityError:
        session.rollback()  # concurrent first read won the insert — same outcome
    logger.info(
        "collab.flip_enabled",
        extra={
            "event": "collab_flip_enabled",
            "workspace_id": str(workspace_id),
            "artifact": artifact,
        },
    )
    return True
```

- [ ] **Step 2: Update the two call sites** in `src/call_manager/web/api/workspaces.py` (`get_draft` ~:282, `get_spec` ~:333): drop the `head_md=` and `artifact_path=` arguments — `maybe_auto_enable(session, workspace_id=..., artifact=...)`. The surrounding raw-disk read stays (it still feeds the non-collab response branch). Grep for any other caller: `grep -rn "maybe_auto_enable\|has_pipe_tables" src/ scripts/ tests/` — the ONLY remaining prod references must be the two read models.

- [ ] **Step 3: Delete the audit script**

```bash
git rm scripts/collab_table_audit.py
```

- [ ] **Step 4: Rewrite the flip tests.** In `test_collab_flip.py`: delete the 8 `has_pipe_tables` detector tests, `test_hold_back_on_pipe_tables`, and `test_hold_back_when_fresh_disk_gained_a_table`; remove the `has_pipe_tables` import, the `_NO_FILE` constant, and the `Path` import; update every remaining `maybe_auto_enable(...)` call to the new signature (drop `head_md=`/`artifact_path=`). Keep (adjusted): `test_enables_once_sticky_and_commits`, `test_kill_switch_suppresses`, `test_per_artifact_independence`, `test_concurrent_first_read_race_reports_enabled`, `test_mutex_held_by_writer_defers_flip`. Add one new test:

```python
def test_table_doc_enables_now(pg_session_factory):
    """The D4 hold-back is gone: a pipe-table artifact enables like any other
    (the bridge round-trips GFM tables losslessly — tables slice §G)."""
    with pg_session_factory() as s:
        wid = _ws(s, "flip7@ki.se").id
        s.commit()
    with pg_session_factory() as s:
        assert maybe_auto_enable(s, workspace_id=wid, artifact="draft") is True
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is True
```

In `test_collab_flip_readmodel.py`: replace `test_table_doc_is_held_back` with its inversion (keep the file's `TABLE_DOC` constant and `_seed`/`_flag_row` helpers exactly as-is):

```python
def test_table_doc_enables_and_serves_canonical_table(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    """Tables slice: pipe-table docs are no longer held back — they enable on
    first read and the response serves table content."""
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, _ = _seed(s, tmp_path, current_user, doc=TABLE_DOC)
        s.commit()
        wid = ws.id
    r = client_authed_owner.get(f"/api/workspaces/{wid}/draft")
    assert r.status_code == 200
    assert r.json()["collab_enabled"] is True
    assert "|" in r.json()["head"]  # pipe-table content served, not dropped
    with pg_session_factory() as s:
        assert _flag_row(s, wid, "draft").enabled_at is not None
```

(If the response model's field for the artifact body is not `head`, use the field the file's OTHER readmodel tests assert on — mirror them, don't guess.)

- [ ] **Step 5: D4 spec amendment.** In `docs/superpowers/specs/2026-07-07-collab-slice3b5d4-flip-design.md`, find locked decision 3 ("Tables = audit script + hold-back", line ~16) and append one sentence to that bullet: `(REMOVED by the tables slice — see 2026-07-08-collab-tables-design.md §G: the bridge now round-trips GFM tables losslessly; the audit script is deleted.)`

- [ ] **Step 6: Run**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_flip.py tests/proposal_assist/collab/test_collab_flip_readmodel.py tests/proposal_assist/collab/test_collab_save_severance.py tests/test_api_spec.py tests/test_api_spec_lock.py -p no:cacheprovider -q ; uv run ruff check .`
Expected: all PASS (severance + spec-lock suites prove the mutex-anchor semantics survived the signature change).

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/proposal_assist/collab/flip.py src/call_manager/web/api/workspaces.py tests/proposal_assist/collab/test_collab_flip.py tests/proposal_assist/collab/test_collab_flip_readmodel.py docs/superpowers/specs/2026-07-07-collab-slice3b5d4-flip-design.md
git rm scripts/collab_table_audit.py 2>/dev/null || true
git commit -m "feat(collab)!: remove the pipe-table hold-back — tables round-trip now (tables T8)"
```

---

### Task 9: e2e + full gates

**Files:**
- Modify: `frontend/e2e/collab.spec.ts` (one new test)
- Run-only: full gate sweep

**Interfaces:**
- Consumes: everything. Read `collab.spec.ts`'s existing two-browser tests FIRST and reuse their setup/seed/login helpers verbatim — do not invent new harness plumbing.

- [ ] **Step 1: Add the two-browser table test** to `collab.spec.ts`, modeled exactly on `"owner and member converge..."`:

```ts
test("owner inserts a table; member sees it live and edits a cell", async ({ browser }) => {
  // ...same two-context setup + navigation as the sibling tests...
  // OWNER: insert a table and type into the first header cell
  await ownerPage.getByTestId("insert-table").click();
  await ownerPage.locator('[data-testid="collab-editor"] th').first().click();
  await ownerPage.keyboard.type("Cost");
  // MEMBER: the table converges
  const memberTable = memberPage.locator('[data-testid="collab-editor"] table');
  await expect(memberTable).toBeVisible();
  await expect(memberPage.locator('[data-testid="collab-editor"] th').first()).toContainText("Cost");
  // MEMBER: edit a body cell; OWNER sees it
  await memberPage.locator('[data-testid="collab-editor"] td').first().click();
  await memberPage.keyboard.type("42");
  await expect(ownerPage.locator('[data-testid="collab-editor"] td').first()).toContainText("42");
  // OWNER: table menu is visible while the caret is in the table
  await expect(ownerPage.getByTestId("table-menu")).toBeVisible();
  // persist: save a version and assert the checkpoint POST succeeds (reuse the
  // file's existing checkpoint-response helper)
});
```

- [ ] **Step 2: Run e2e both modes** (from the worktree root; if Vite hits inotify ENOSPC, prefix `CHOKIDAR_USEPOLLING=1`):

```bash
bash scripts/collab_e2e_run.sh
E2E_SPA_MODE=prod bash scripts/collab_e2e_run.sh
```

Expected: 6/6 in both modes (5 existing + the new table test).

- [ ] **Step 3: Full gate sweep**

```bash
EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab tests/test_api_spec.py tests/test_api_spec_lock.py tests/test_api_workspaces.py -p no:cacheprovider -q
uv run ruff check .
cd frontend && pnpm exec tsc --noEmit && pnpm vitest run && node scripts/gen_collab_goldens.mjs && git diff --exit-code ../tests/proposal_assist/collab/goldens && node scripts/verify_python_writepath.mjs
```

Expected: everything green; the goldens regen is byte-idempotent (`git diff --exit-code` clean).

- [ ] **Step 4: Commit**

```bash
git add frontend/e2e/collab.spec.ts
git commit -m "test(collab): two-browser table editing e2e (tables T9)"
```

Then STOP — final whole-branch review + merge decision belong to the controller/user, not this task.
