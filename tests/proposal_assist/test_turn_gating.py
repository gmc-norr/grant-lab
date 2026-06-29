from call_manager.proposal_assist.drafting.turn_gating import (
    MODIFY_TOOLS,
    READ_ONLY_TOOLS,
    allowed_tools_for_content,
    selection_from_content,
)


def test_read_only_excludes_all_mutators():
    for t in ("propose_edit", "propose_spec_edit", "propose_citation",
              "fetch_reference", "build_bibliography"):
        assert t not in READ_ONLY_TOOLS


def test_modify_adds_only_propose_edit():
    assert READ_ONLY_TOOLS | {"propose_edit"} == MODIFY_TOOLS
    assert "propose_spec_edit" not in MODIFY_TOOLS
    assert "propose_citation" not in MODIFY_TOOLS


def test_allowed_tools_for_content():
    ask = [{"type": "text", "text": "q"}, {"type": "selection", "request_type": "ask"}]
    mod = [{"type": "text", "text": "q"}, {"type": "selection", "request_type": "modify"}]
    plain = {"type": "text", "text": "hi"}
    assert allowed_tools_for_content(ask) == set(READ_ONLY_TOOLS)
    assert allowed_tools_for_content(mod) == set(MODIFY_TOOLS)
    assert allowed_tools_for_content(plain) is None  # normal chat -> full list


def test_allowed_tools_unknown_request_type_fails_closed():
    blk = [{"type": "text", "text": "q"}, {"type": "selection", "request_type": "execute"}]
    assert allowed_tools_for_content(blk) == set()   # deny all, not READ_ONLY
    missing = [{"type": "text", "text": "q"}, {"type": "selection"}]  # no request_type
    assert allowed_tools_for_content(missing) == set()


def test_selection_from_content():
    mod = [{"type": "text", "text": "q"},
           {"type": "selection", "request_type": "modify", "anchor": "a",
            "from_offset": 1, "to_offset": 2, "section_index": 3}]
    sel = selection_from_content(mod)
    assert sel["anchor"] == "a" and sel["section_index"] == 3
    assert selection_from_content({"type": "text", "text": "x"}) is None
