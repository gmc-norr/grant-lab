from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.editing.sectionizer import split_sections

from .fixtures import CORPUS


def test_markdown_roundtrip_is_idempotent():
    # serialize(parse(x)) is the canonical normal form; applying it again is a no-op.
    for name, md in CORPUS:
        once = pm_to_markdown(markdown_to_pm(md))
        twice = pm_to_markdown(markdown_to_pm(once))
        assert once == twice, name


def test_split_sections_invariant_holds_on_output():
    for name, md in CORPUS:
        out = pm_to_markdown(markdown_to_pm(md))
        assert "".join(s.text for s in split_sections(out)) == out, name


def test_h2_headings_preserved():
    for name, md in CORPUS:
        before = [s.heading for s in split_sections(md) if s.heading]
        out = pm_to_markdown(markdown_to_pm(md))
        after = [s.heading for s in split_sections(out) if s.heading]
        assert before == after, name


def test_citation_marker_survives_roundtrip():
    md = dict(CORPUS)["citation"]
    out = pm_to_markdown(markdown_to_pm(md))
    assert "[@smith2021]<!-- ev:ev-0a1b2c3d4e5f -->" in out


def test_adjacent_citations_survive():
    md = dict(CORPUS)["adjacent_citations"]
    out = pm_to_markdown(markdown_to_pm(md))
    assert "[@smith2021]<!-- ev:ev-0a1b2c3d4e5f -->" in out
    assert "[@jones2020]<!-- ev:ev-9f8e7d6c5b4a -->" in out


def test_bare_marker_stays_literal_text_unescaped():
    # A citekey-only marker is NOT a citation node; it must remain literal,
    # UNESCAPED text so the existing bare-marker scan still matches it.
    out = pm_to_markdown(markdown_to_pm(dict(CORPUS)["bare_marker_is_literal_text"]))
    assert "[@x9]" in out  # not "\\[@x9\\]"


def test_h1_title_in_preamble_survives():
    out = pm_to_markdown(markdown_to_pm(dict(CORPUS)["h1_title_preamble"]))
    assert out.lstrip().startswith("# Proposal Title")


def test_inline_code_with_backtick_roundtrips():
    md = "## X\n\nuse `` a`b `` here.\n"
    once = pm_to_markdown(markdown_to_pm(md))
    twice = pm_to_markdown(markdown_to_pm(once))
    assert once == twice
    # the backtick survives inside an inline-code span:
    pm = markdown_to_pm(once)
    # find the code-marked text node and assert it contains a backtick
    import json

    assert "a`b" in json.dumps(pm)


def test_multi_paragraph_list_item_roundtrips():
    # A list item with two paragraphs is schema-valid ("paragraph block*").
    # pm_to_markdown is the canonical git-checkpoint writer, so it MUST keep the
    # paragraph break: a single newline between the blocks re-parses as ONE
    # soft-broken paragraph = silent content loss. Covers both list kinds.
    for md in ("- one\n\n  two\n", "1. one\n\n   two\n"):
        pm = markdown_to_pm(md)
        item = pm["content"][0]["content"][0]  # list > list_item
        assert [c["type"] for c in item["content"]] == ["paragraph", "paragraph"], md

        # Round-trip through the serializer preserves the two distinct paragraphs.
        pm2 = markdown_to_pm(pm_to_markdown(pm))
        item2 = pm2["content"][0]["content"][0]
        assert [c["type"] for c in item2["content"]] == ["paragraph", "paragraph"], md

        # ... and the canonical normal form stays idempotent.
        once = pm_to_markdown(pm)
        assert pm_to_markdown(markdown_to_pm(once)) == once, md


def test_production_layout_hydrates():
    # The exact tests/test_editing_save.py doc (H1 + bare marker + tight layout)
    # must round-trip idempotently and preserve its H2 sections.
    from call_manager.proposal_assist.editing.sectionizer import split_sections

    md = dict(CORPUS)["production_layout"]
    out = pm_to_markdown(markdown_to_pm(md))
    assert pm_to_markdown(markdown_to_pm(out)) == out  # idempotent
    assert "[@x9]" in out  # bare marker literal
    assert [s.heading for s in split_sections(out) if s.heading] == ["Background", "Aims"]


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
    assert any(any(m["type"] == "code" for m in n.get("marks", [])) for n in inline)


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


# --- tables: serialize (2026-07-08 spec §C) --------------------------------- #


def _table_doc(rows):
    """Hand-build a table doc: rows = list of (cell_type, text, colspan)."""

    def cell(ct, text, colspan=1):
        para = {"type": "paragraph"}
        if text:
            para["content"] = [{"type": "text", "text": text}]
        return {"type": ct, "attrs": {"colspan": colspan, "rowspan": 1}, "content": [para]}

    return {
        "type": "doc",
        "content": [
            {
                "type": "table",
                "content": [
                    {"type": "table_row", "content": [cell(*c) for c in row]} for row in rows
                ],
            }
        ],
    }


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
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "table",
                "content": [
                    {
                        "type": "table_row",
                        "content": [
                            {
                                "type": "table_header",
                                "attrs": {"colspan": 1, "rowspan": 1},
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": "x|y",
                                                "marks": [{"type": "code"}],
                                            },
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ],
    }
    out = pm_to_markdown(doc)
    assert "`x\\|y`" in out
    back = markdown_to_pm(out)
    cell_text = back["content"][0]["content"][0]["content"][0]["content"][0]["content"][0]
    assert cell_text["text"] == "x|y"
    assert cell_text["marks"][0]["type"] == "code"


def test_hard_break_in_cell_degrades_to_space():
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "table",
                "content": [
                    {
                        "type": "table_row",
                        "content": [
                            {
                                "type": "table_header",
                                "attrs": {"colspan": 1, "rowspan": 1},
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [
                                            {"type": "text", "text": "a"},
                                            {"type": "hard_break"},
                                            {"type": "text", "text": "b"},
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ],
    }
    assert "| a b |" in pm_to_markdown(doc)


def test_multi_block_cell_degrades_joined_with_space():
    # violates the schema on purpose (hostile shape via the unvalidated Yjs
    # read path) — the serializer degrades, never drops, never raises (spec §C)
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "table",
                "content": [
                    {
                        "type": "table_row",
                        "content": [
                            {
                                "type": "table_header",
                                "attrs": {"colspan": 1, "rowspan": 1},
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [{"type": "text", "text": "a"}],
                                    },
                                    {
                                        "type": "paragraph",
                                        "content": [{"type": "text", "text": "b"}],
                                    },
                                ],
                            }
                        ],
                    },
                ],
            }
        ],
    }
    assert "| a b |" in pm_to_markdown(doc)


def test_colspan_degrades_to_filler_cells():
    doc = _table_doc(
        [
            [("table_header", "h1"), ("table_header", "h2")],
            [("table_cell", "wide", 2)],
        ]
    )
    out = pm_to_markdown(doc)
    assert "| wide |  |" in out  # content once + one empty filler


def test_body_rows_padded_and_truncated_to_header_width():
    doc = _table_doc(
        [
            [("table_header", "a"), ("table_header", "b")],
            [("table_cell", "1")],  # short → padded
            [("table_cell", "1"), ("table_cell", "2"), ("table_cell", "3")],  # long → truncated
        ]
    )
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
