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


# ---------------------------------------------------------------------------
# Review 2026-09-02 §2.2–2.3: shapes the EDITOR produces (not markdown-it output)
# that the checkpoint serializer used to corrupt or emit unreadably. Every case
# goes through the Yjs reader, because Yjs stores marks as an unordered attr
# dict and the reader re-sorts them into schema order — the order the parser
# produced is gone by the time the checkpoint serializes.
# ---------------------------------------------------------------------------
import itertools  # noqa: E402
import re  # noqa: E402

import pytest  # noqa: E402

from call_manager.proposal_assist.collab.markdown_bridge import _mark_key  # noqa: E402
from call_manager.proposal_assist.collab.yjs_bridge import (  # noqa: E402
    pm_json_to_ydoc,
    ydoc_to_pm_json,
)


def _via_yjs(md: str) -> str:
    """md -> PM -> Yjs -> PM -> md: exactly hydrate + checkpoint."""
    return pm_to_markdown(ydoc_to_pm_json(pm_json_to_ydoc(markdown_to_pm(md))))


@pytest.mark.parametrize(
    ("md", "expected"),
    [
        # em ends before strong: ': text' used to gain italic (`***Aim 1: text***`)
        ("**_Aim 1_: text**\n", "***Aim 1*: text**\n"),
        # link ends before strong: the link used to widen over ': text'
        ("**[Aim 1](https://x.se): text**\n", "**[Aim 1](https://x.se): text**\n"),
        ("**bold** then *italic*\n", "**bold** then *italic*\n"),
        ("*a **b** c*\n", "*a **b** c*\n"),
        ("**a *b*** *c*\n", "**a *b*** *c*\n"),
    ],
)
def test_nested_marks_do_not_widen_through_yjs(md, expected):
    # The EXACT bytes matter, not only the marks: the longer-lived mark must be
    # the outer one, or every checkpoint after an upgrade rewrites the document
    # into an equivalent-but-different form (a spurious diff on every doc with
    # nested marks). The Yjs path must also agree with the direct parser path.
    out = _via_yjs(md)
    assert out == expected
    assert out == pm_to_markdown(markdown_to_pm(md))
    assert _runs(markdown_to_pm(out)) == _runs(markdown_to_pm(md))
    assert _via_yjs(out) == out


def _runs(doc: dict) -> list[list[tuple[str, frozenset]]]:
    """Per block: adjacent same-mark text merged into (text, mark-key set) runs.
    This is the SEMANTIC content — what the user sees — independent of how
    many text nodes carry it or in which order the marks are listed. Whitespace
    is compared unmarked: a bold space is invisible, and markdown cannot hold
    one next to a delimiter anyway (the serializer moves it outside)."""
    blocks = []
    for block in doc["content"]:
        if block["type"] not in ("paragraph", "heading"):
            continue
        runs: list[tuple[str, frozenset]] = []
        for node in block.get("content", []):
            assert node["type"] == "text", node
            keys = frozenset(_mark_key(m) for m in node.get("marks", []))
            for piece in re.findall(r"\s+|\S+", node["text"]):
                piece_keys = frozenset() if piece.isspace() else keys
                if runs and runs[-1][1] == piece_keys:
                    runs[-1] = (runs[-1][0] + piece, piece_keys)
                else:
                    runs.append((piece, piece_keys))
        blocks.append(runs)
    return blocks


_MARK_PAIRS = [
    ({"type": "strong"}, {"type": "em"}),
    ({"type": "strong"}, {"type": "link", "attrs": {"href": "https://x.se", "title": None}}),
    ({"type": "em"}, {"type": "link", "attrs": {"href": "https://x.se", "title": None}}),
    ({"type": "strong"}, {"type": "strikethrough"}),
    ({"type": "em"}, {"type": "strikethrough"}),
]


def _layouts():
    # Three words; each word carries any subset of the two marks. 4^3 = 64 layouts
    # per pair, which covers nested, adjacent, crossing and identical ranges.
    for a, b in _MARK_PAIRS:
        for combo in itertools.product(range(4), repeat=3):
            marks = [
                ([a] if bits & 1 else []) + ([b] if bits & 2 else []) for bits in combo
            ]
            yield a["type"], b["type"], marks


@pytest.mark.parametrize(("a", "b", "marks"), list(_layouts()))
def test_every_two_mark_layout_survives_the_checkpoint_round_trip(a, b, marks):
    # The words are separated by spaces that carry the marks BOTH neighbours
    # share (the form the whitespace-expulsion rule produces anyway).
    words = ["alpha", "beta", "gamma"]
    content = []
    for i, (word, m) in enumerate(zip(words, marks, strict=True)):
        if i:
            shared = [x for x in marks[i - 1] if x in m]
            content.append({"type": "text", "text": " ", **({"marks": shared} if shared else {})})
        content.append({"type": "text", "text": word, **({"marks": m} if m else {})})
    doc = {"type": "doc", "content": [{"type": "paragraph", "content": content}]}
    yjs_view = ydoc_to_pm_json(pm_json_to_ydoc(doc))
    md = pm_to_markdown(yjs_view)
    assert _runs(markdown_to_pm(md)) == _runs(yjs_view), md
    assert pm_to_markdown(markdown_to_pm(md)) == md, "not idempotent"


def test_empty_blockquote_is_readable_again():
    # Tiptap's blockquote is block+; typing "> " and moving on leaves
    # blockquote[paragraph()], which serializes as a bare ">" line. That line
    # used to fail node.check() on rehydration: the artifact 500ed on every GET
    # and the git-first save path answered 409 — unreadable and unsaveable.
    doc = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "intro"}]},
            {"type": "blockquote", "content": [{"type": "paragraph"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "after"}]},
        ],
    }
    md = pm_to_markdown(doc)
    assert md == "intro\n\n>\n\nafter\n"
    assert markdown_to_pm(md) == doc
    # And the exact bytes an already-committed document carries parse too.
    assert markdown_to_pm(">\n") == {
        "type": "doc",
        "content": [{"type": "blockquote", "content": [{"type": "paragraph"}]}],
    }


def test_code_block_containing_a_fence_line_does_not_swallow_the_document():
    doc = {
        "type": "doc",
        "content": [
            {"type": "code_block", "content": [{"type": "text", "text": "x\n```\ny"}]},
            {
                "type": "heading",
                "attrs": {"level": 2},
                "content": [{"type": "text", "text": "Later"}],
            },
            {"type": "paragraph", "content": [{"type": "text", "text": "body"}]},
        ],
    }
    md = pm_to_markdown(doc)
    assert md == "````\nx\n```\ny\n````\n\n## Later\n\nbody\n"
    assert markdown_to_pm(md) == doc
    assert [s.heading for s in split_sections(md) if s.heading] == ["Later"]


def test_block_final_hard_break_is_dropped_not_a_literal_backslash():
    hb = {"type": "hard_break"}
    t = lambda s: {"type": "text", "text": s}  # noqa: E731
    doc = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [t("line"), hb]},
            {"type": "paragraph", "content": [t("next")]},
            {
                "type": "bullet_list",
                "content": [
                    {
                        "type": "list_item",
                        "content": [{"type": "paragraph", "content": [t("item"), hb, hb]}],
                    },
                ],
            },
        ],
    }
    md = pm_to_markdown(doc)
    assert md == "line\n\nnext\n\n- item\n"
    assert "\\" not in md
    assert _runs(markdown_to_pm(md))[0] == [("line", frozenset())]


def test_hard_break_inside_a_heading_becomes_a_space_and_keeps_the_section_name():
    hb = {"type": "hard_break"}
    t = lambda s: {"type": "text", "text": s}  # noqa: E731
    doc = {
        "type": "doc",
        "content": [
            {"type": "heading", "attrs": {"level": 2}, "content": [t("Aims"), hb, t("more")]},
            {"type": "paragraph", "content": [t("body")]},
        ],
    }
    md = pm_to_markdown(doc)
    assert md == "## Aims more\n\nbody\n"
    assert [s.heading for s in split_sections(md) if s.heading] == ["Aims more"]
    assert markdown_to_pm(md)["content"][0]["content"] == [t("Aims more")]


@pytest.mark.parametrize(
    ("md", "text"),
    [
        ("*a *b* c*\n", "a b c"),
        ("**a **b** c**\n", "a b c"),
        # What the serializer emits for intraword mixed emphasis; CommonMark's
        # flanking rules leave some delimiters literal here, so only "parses,
        # one mark per type" is asserted, not the text.
        ("*al***ph*a***\n", None),
        ("**~~al~~**ph**a**\n", None),
    ],
)
def test_nested_same_type_emphasis_parses_with_one_mark(md, text):
    # CommonMark nests em inside em (and markdown-it emits both opens); a PM
    # node carries a mark once. This used to raise "Invalid collection of
    # marks" from node.check() — an artifact holding such text 500ed on every
    # hydration.
    doc = markdown_to_pm(md)
    for node in doc["content"][0]["content"]:
        keys = [_mark_key(m) for m in node.get("marks", [])]
        assert len(keys) == len(set(keys)), node
    if text is not None:
        assert "".join(n["text"] for n in doc["content"][0]["content"]) == text
        assert all(
            {m["type"] for m in n.get("marks", [])} == {"em" if md[1] != "*" else "strong"}
            for n in doc["content"][0]["content"]
        )


# ---------------------------------------------------------------------------
# Second-opinion round on the 2026-09-02 fix: text that LOOKS like markdown
# must come back as the same text. None of these raise, so the checkpoint's
# dry-run reader cannot see them.
# ---------------------------------------------------------------------------
_HBN = {"type": "hard_break"}


def _tn(s: str, **marks) -> dict:
    node = {"type": "text", "text": s}
    if marks.get("marks"):
        node["marks"] = marks["marks"]
    return node


def _pn(*content) -> dict:
    return {"type": "doc", "content": [{"type": "paragraph", "content": list(content)}]}


def _text_of_n(doc: dict) -> str:
    return "".join(
        n.get("text", "") if n["type"] == "text" else f"<{n['type']}>"
        for block in doc["content"]
        for n in block.get("content", [])
    )


@pytest.mark.parametrize(
    "s",
    [
        "see [x](y) here",
        "an ![img](s.png) too",
        "<https://x.se> and <a.b@x.se>",
        "ref [x][y] and [z] alone",
        "bare [@x9] marker",
        "<TP53> is a gene",
    ],
)
def test_literal_inline_syntax_in_plain_text_stays_text(s):
    md = pm_to_markdown(_pn(_tn(s)))
    back = markdown_to_pm(md)
    assert back == _pn(_tn(s)), md
    assert pm_to_markdown(back) == md
    if "[@x9]" in s:
        assert "[@x9]" in md  # the bare marker stays unescaped: the scan must match it
    if "<TP53>" in s:
        assert "<TP53>" in md  # not an autolink, not escaped


@pytest.mark.parametrize(
    "s",
    [
        "# not a heading",
        "###### six",
        "- not a bullet",
        "+ plus",
        "1. not a list",
        "2024. Results were",
        "7) seven",
        "> not a quote",
        "---",
        "***",
        "===",
        "| --- |",
        "|---|---|",
        "[x]: y",
    ],
)
def test_line_start_block_syntax_in_plain_text_stays_text(s):
    for content in ([_tn(s)], [_tn("line"), _HBN, _tn(s)], [_tn("line\n" + s)]):
        doc = _pn(*content)
        md = pm_to_markdown(doc)
        back = markdown_to_pm(md)
        assert [b["type"] for b in back["content"]] == ["paragraph"], md
        assert _text_of_n(back) == _text_of_n(doc), md
        assert pm_to_markdown(back) == md


def test_block_syntax_after_a_delimiter_is_not_escaped():
    # `**1. Aim**` is not a list; escaping it would rewrite every such document.
    strong = [{"type": "strong"}]
    assert pm_to_markdown(_pn(_tn("1. Aim", marks=strong))) == "**1. Aim**\n"
    # ...but a bullet-looking run INSIDE a paragraph after a newline is escaped.
    assert pm_to_markdown(_pn(_tn("a\n- b"))) == "a\n\\- b\n"


def test_hard_break_before_trailing_whitespace_is_block_final():
    # Second opinion (Sonnet): "line", Shift+Enter, trailing spaces — the
    # break's backslash landed right before the blank line and stayed literal.
    for tail in ([_tn("   ")], [_tn("  ", marks=[{"type": "strong"}])], [_tn(" "), _HBN]):
        doc = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [_tn("line"), _HBN, *tail]},
                {"type": "paragraph", "content": [_tn("next")]},
            ],
        }
        md = pm_to_markdown(doc)
        assert md == "line\n\nnext\n", md
        assert "\\" not in md


def test_block_edge_whitespace_is_dropped_canonically():
    strong = [{"type": "strong"}]
    assert pm_to_markdown(_pn(_tn("  lead"))) == "lead\n"
    assert pm_to_markdown(_pn(_tn("trail  "))) == "trail\n"
    assert pm_to_markdown(_pn(_tn(" ", marks=strong), _tn("a", marks=strong))) == "**a**\n"
    assert pm_to_markdown(_pn(_tn("a", marks=strong), _tn(" ", marks=strong))) == "**a**\n"
    # a heading's inner hard break becomes a space but a trailing one vanishes
    doc = {
        "type": "doc",
        "content": [{"type": "heading", "attrs": {"level": 2}, "content": [_tn("Aims"), _HBN]}],
    }
    assert pm_to_markdown(doc) == "## Aims\n"


@pytest.mark.parametrize("body", [" a ", "  a  ", " `a ", " ", "a", " a", "a "])
def test_code_span_with_edge_spaces_round_trips(body):
    # Second opinion (Sol): CommonMark strips one space from each end of a code
    # span whose body starts AND ends with a space, so `` ` a ` `` re-read as ``a``.
    doc = _pn(_tn("x "), _tn(body, marks=[{"type": "code"}]), _tn(" y"))
    md = pm_to_markdown(doc)
    back = markdown_to_pm(md)
    code = [n["text"] for n in back["content"][0]["content"] if n.get("marks")]
    assert code == [body], md
    assert pm_to_markdown(back) == md


def test_numbered_and_hash_headings_stay_byte_identical():
    # A heading's text does not start a line — "## " does — so "1. " or "#"
    # there is not a list or heading and must not be escaped: the heading
    # text is the section's identity (split_sections keys on it).
    for md in ["## 1. Aims\n", "## # not nested\n", "## - dash first\n", "## > quoted\n"]:
        assert pm_to_markdown(markdown_to_pm(md)) == md
    # ...while inside a list item the same text WOULD nest a list, so it is escaped.
    li = {"type": "doc", "content": [{"type": "bullet_list", "content": [
        {"type": "list_item", "content": [{"type": "paragraph", "content": [_tn("1. x")]}]},
    ]}]}
    md = pm_to_markdown(li)
    assert md == "- 1\\. x\n"
    assert markdown_to_pm(md) == li


def test_newlines_inside_code_spans_and_headings_become_spaces():
    # CommonMark parses blocks before inline code: a code span whose next line
    # starts "## " ENDS the span and starts a heading (a phantom section). The
    # reader turns a line ending inside a code span into a space anyway.
    doc = _pn(_tn("x "), _tn("a\n## FAKE", marks=[{"type": "code"}]))
    md = pm_to_markdown(doc)
    assert md == "x `a ## FAKE`\n"
    assert [b["type"] for b in markdown_to_pm(md)["content"]] == ["paragraph"]
    assert [s.heading for s in split_sections(md) if s.heading] == []
    heading = {"type": "doc", "content": [
        {"type": "heading", "attrs": {"level": 2}, "content": [_tn("Aims\n## not")]},
    ]}
    assert pm_to_markdown(heading) == "## Aims ## not\n"
    # Second opinion (Grok): a newline-ONLY text node and a newline inside a run
    # both ended the heading; the rest became a paragraph and the section was
    # renamed. Both are one space now, and the heading is idempotent.
    for content in (
        [_tn("Aims"), _tn("\n"), _tn("more")],
        [_tn("Aim\ns", marks=[{"type": "em"}])],
    ):
        h = {
            "type": "doc",
            "content": [{"type": "heading", "attrs": {"level": 2}, "content": content}],
        }
        md = pm_to_markdown(h)
        assert "\n" not in md.rstrip("\n"), md
        assert [b["type"] for b in markdown_to_pm(md)["content"]] == ["heading"], md
        assert pm_to_markdown(markdown_to_pm(md)) == md
    image = _pn({"type": "image", "attrs": {"src": "s.png", "alt": "a\n## b", "title": None}})
    assert pm_to_markdown(image) == "![a ## b](s.png)\n"
