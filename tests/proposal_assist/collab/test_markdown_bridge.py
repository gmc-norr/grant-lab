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


def test_hostile_colspan_degrades_never_raises():
    # colspan arrives via the unvalidated Yjs read path (spec §5); a hostile
    # value must degrade, never raise, never amplify (spec §C).
    def _doc(cs):
        return {"type": "doc", "content": [{"type": "table", "content": [
            {"type": "table_row", "content": [
                {"type": "table_header", "attrs": {"colspan": cs, "rowspan": 1},
                 "content": [{"type": "paragraph",
                              "content": [{"type": "text", "text": "h"}]}]},
            ]},
        ]}]}
    for hostile in ["abc", {}, None, -5, 10**9, 2.9]:
        out = pm_to_markdown(_doc(hostile))
        assert out.splitlines()[0].startswith("| ")
        assert len(out) < 10_000  # clamped — no amplification


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


# --- Round-trip fidelity for real grant-proposal prose -------------------------
#
# The 2026-06-17 editor round-trip spike (docs/superpowers/notes/) recorded these as
# Break 1 and Break 3 and they were never fixed. Both are reachable from a LIVE path:
# apply_citation_to_room (collab/kernel.py:99) serialises the room to markdown, mutates
# it, and re-parses a section back INTO the live CRDT, so whatever the projection loses
# becomes permanent in the document the researcher is looking at.


def test_gene_names_in_angle_brackets_survive():
    """`<TP53>` parsed as inline HTML and dropped silently deletes the gene name."""
    md = "Variants in <TP53> and <BRCA1> were enriched in the cohort."
    assert pm_to_markdown(markdown_to_pm(md)).strip() == md


def test_snake_case_identifiers_are_not_backslash_escaped():
    """`sample_id` -> `sample\\_id` on disk makes `grep -c sample_id` return 0.

    markdown-it does not treat an INTRAWORD underscore as emphasis (CommonMark
    left/right-flanking), so escaping it was never necessary.
    """
    md = "We join on sample_id, patient_age and n_reads across cohorts."
    out = pm_to_markdown(markdown_to_pm(md))
    assert "\\_" not in out, out
    assert out.strip() == md


def test_snake_case_headings_keep_their_identity():
    """A section heading IS the section's identity; escaping it renames the section."""
    md = "## Aims_and_Objectives\n\nBody text.\n"
    out = pm_to_markdown(markdown_to_pm(md))
    assert "Aims_and_Objectives" in out, out
    assert "\\_" not in out, out


def test_angle_bracket_placeholders_survive():
    md = "Replace <COHORT_NAME> before submission."
    assert pm_to_markdown(markdown_to_pm(md)).strip() == md


def test_a_literal_asterisk_in_TEXT_is_still_escaped():
    """Control: `*` IS intraword emphasis in CommonMark (`a*b*c` parses as em), so a
    literal `*` inside a text node must keep its backslash or the next parse invents
    emphasis the researcher never wrote. Relaxing the UNDERSCORE rule must not be
    over-applied to the asterisk.

    Driven from a PM doc rather than markdown, because `5*3*2` in markdown is already
    emphasis — the only way to hold a literal `*` is to put it in a text node.
    """
    doc = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [
            {"type": "text", "text": "The formula 5*3*2 equals 30."}
        ]}],
    }
    out = pm_to_markdown(doc)
    assert "\\*" in out, out
    # and it must survive: re-parsing gives back the literal asterisks, not emphasis
    back = pm_to_markdown(markdown_to_pm(out))
    assert back.strip() == out.strip()
    assert "5*3*2" in markdown_to_pm(out)["content"][0]["content"][0]["text"]


def test_the_full_citation_marker_still_becomes_one_atom():
    """Control: the marker must keep winning over the generic parse."""
    md = "As shown [@smith2021]<!-- ev:ev-abc123 --> the effect holds."
    doc = markdown_to_pm(md)
    kinds = [n["type"] for n in doc["content"][0]["content"]]
    assert "citation" in kinds, kinds
    assert pm_to_markdown(doc).strip() == md


# --------------------------------------------------------------------------- #
# Whitespace at an emphasis boundary. CommonMark will not pair a `**` that is
# followed by whitespace (opening) or preceded by it (closing), so emitting
# `**bold **tail` produces a document whose asterisks re-parse as LITERAL TEXT
# and are then escaped permanently: the bold is destroyed and the researcher is
# left looking at raw `**`. Silent, and it survives every checkpoint.
#
# This is also how a bold citation corrupts itself. y-prosemirror drops marks on
# inline atoms (createTypeFromElementNode writes node.attrs only) and our bridge
# matches it deliberately, so the atom splits its run and leaves `**bold **`
# behind — the same defect, arrived at from the CRDT side.
# --------------------------------------------------------------------------- #


def _para(*nodes):
    return {"type": "doc", "content": [{"type": "paragraph", "content": list(nodes)}]}


def _t(text, *marks):
    node = {"type": "text", "text": text}
    if marks:
        node["marks"] = [{"type": m, "attrs": {}} for m in marks]
    return node


def _survives(doc):
    """Serialize, re-parse, re-serialize. Returns (markdown, stable?)."""
    once = pm_to_markdown(doc)
    twice = pm_to_markdown(markdown_to_pm(once))
    return once, once == twice


def test_whitespace_never_lands_inside_an_emphasis_delimiter():
    cases = {
        "trailing space in strong": _para(_t("bold ", "strong"), _t("tail")),
        "leading space in strong": _para(_t("head"), _t(" bold", "strong")),
        # Mid-paragraph: a leading space at the very START of a paragraph is
        # stripped by CommonMark itself and is not representable either way.
        "both sides": _para(_t("head"), _t(" bold ", "strong"), _t("tail")),
        "trailing space in em": _para(_t("it ", "em"), _t("tail")),
        "wholly-whitespace strong": _para(_t("a"), _t(" ", "strong"), _t("b")),
        "strikethrough": _para(_t("gone ", "strikethrough"), _t("tail")),
    }
    broken = {}
    for name, doc in cases.items():
        md, stable = _survives(doc)
        if "\\*" in md or "\\~" in md or not stable:
            broken[name] = md
    assert broken == {}, broken


def test_a_bold_citation_does_not_corrupt_its_own_paragraph():
    """The end-to-end shape. A round trip through the CRDT strips the atom's mark;
    serializing must still emit markdown that means what it says."""
    from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc, ydoc_to_pm_json

    md = "**bold [@jones2020]<!-- ev:ev-111111111111 --> tail**\n"
    once = pm_to_markdown(ydoc_to_pm_json(pm_json_to_ydoc(markdown_to_pm(md))))
    # It must re-parse at all (this used to raise "Invalid collection of marks
    # for node citation: ['strong', 'strong']") and then hold still.
    twice = pm_to_markdown(ydoc_to_pm_json(pm_json_to_ydoc(markdown_to_pm(once))))
    assert once == twice, (once, twice)
    assert "\\*" not in once, once
    assert "[@jones2020]<!-- ev:ev-111111111111 -->" in once, once


def test_the_expelled_space_keeps_the_marks_it_is_still_inside():
    """`**a *b***` — the space is inside strong and outside em, so it must stay
    bold. Expelling it all the way out would silently unbold it."""
    doc = _para(_t("a", "strong"), _t(" b", "strong", "em"))
    md, stable = _survives(doc)
    assert stable, md
    assert md.strip() == "**a *b***", md


def test_emphasis_without_boundary_whitespace_is_untouched():
    """Control. The fix must not churn documents that were already correct —
    every one of these is a byte-for-byte no-op."""
    for md in ["**bold** tail\n", "*it* tail\n", "***both*** x\n", "a **b** c\n"]:
        assert pm_to_markdown(markdown_to_pm(md)) == md, md


def test_whitespace_inside_a_code_span_is_left_alone():
    """Control. Code spans are verbatim — their whitespace is content, not padding.

    It must carry `strong` TOO. A `code`-only run has no fixed-delimiter mark, so it
    is already skipped by the delimiter check and cannot exercise the code guard at
    all; `strong` + `code` is legal in this schema (code.excludes is None) and is the
    only shape that reaches it.
    """
    doc = _para(_t("x "), _t("a  b ", "strong", "code"), _t(" y"))
    md, stable = _survives(doc)
    assert stable, md
    body = markdown_to_pm(md)["content"][0]["content"]
    kept = [n["text"] for n in body if "code" in [m["type"] for m in (n.get("marks") or [])]]
    assert kept == ["a  b "], kept
