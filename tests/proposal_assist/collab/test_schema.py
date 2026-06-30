from call_manager.proposal_assist.collab.schema import SCHEMA, CITATION_NODE


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
