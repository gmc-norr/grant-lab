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
