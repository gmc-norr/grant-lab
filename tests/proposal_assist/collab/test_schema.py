from grant_lab.proposal_assist.collab.schema import CITATION_NODE, SCHEMA


def test_schema_has_constrained_nodes():
    for n in [
        "doc",
        "paragraph",
        "heading",
        "bullet_list",
        "ordered_list",
        "list_item",
        "blockquote",
        "code_block",
        "horizontal_rule",
        "image",
        "hard_break",
        "citation",
    ]:
        assert n in SCHEMA.nodes, n


def test_schema_has_constrained_marks():
    for m in ["strong", "em", "strikethrough", "code", "link"]:
        assert m in SCHEMA.marks, m


def test_citation_is_inline_atom():
    cite = SCHEMA.nodes[CITATION_NODE]
    assert cite.spec.get("inline") is True
    assert cite.spec.get("atom") is True
    assert set(cite.spec["attrs"]) == {"citekey", "ev_id"}


def test_heading_carries_a_level_attr():
    # H1-H6 must be representable (real docs use H1 titles).
    assert "level" in SCHEMA.nodes["heading"].spec.get("attrs", {})


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
