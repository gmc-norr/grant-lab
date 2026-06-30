from call_manager.proposal_assist.editing.sectionizer import split_sections

from .fixtures import CORPUS


def test_corpus_roundtrips_through_sectionizer():
    # Every fixture must already satisfy the byte-identical split/join invariant,
    # so any later break is the bridge's fault, not the fixture's.
    for name, md in CORPUS:
        assert "".join(s.text for s in split_sections(md)) == md, name


def test_fenced_h2_is_not_a_section_boundary():
    md = dict(CORPUS)["fenced_h2_not_a_section"]
    headings = [s.heading for s in split_sections(md) if s.heading]
    assert headings == ["Methods"]  # the fenced "## not a heading" is body, not a boundary
