"""Sectionizer: byte-round-trip split/join of proposal.md on ## headings."""

from grant_lab.proposal_assist.editing.sectionizer import Section, split_sections

DOC = (
    "# Title\n"
    "Intro before any section.\n"
    "\n"
    "## Background\n"
    "Some text.\n"
    "```\n"
    "## not a heading (in fence)\n"
    "```\n"
    "## Aims\n"
    "Aim text.\n"
)


def _join(sections: list[Section]) -> str:
    return "".join(s.text for s in sections)


def test_round_trip_byte_identical():
    assert _join(split_sections(DOC)) == DOC


def test_preamble_is_section_zero():
    sections = split_sections(DOC)
    assert sections[0].index == 0
    assert sections[0].heading == ""
    assert sections[0].text.startswith("# Title\n")


def test_headings_and_fence_respected():
    sections = split_sections(DOC)
    assert [s.heading for s in sections] == ["", "Background", "Aims"]
    # the fenced "## not a heading" stays inside Background
    assert "## not a heading" in sections[1].text


def test_no_heading_doc_is_single_preamble():
    sections = split_sections("just prose\nno headings\n")
    assert len(sections) == 1
    assert sections[0].heading == ""


def test_empty_doc():
    sections = split_sections("")
    assert len(sections) == 1
    assert sections[0].text == ""


def test_doc_starting_with_heading_has_empty_preamble():
    doc = "## A\nbody\n## B\nbody2"
    sections = split_sections(doc)
    assert sections[0].text == ""
    assert [s.heading for s in sections] == ["", "A", "B"]
    assert _join(sections) == doc


def test_level3_heading_is_not_a_boundary():
    doc = "## A\n### sub\nbody\n"
    sections = split_sections(doc)
    assert [s.heading for s in sections] == ["", "A"]


def test_sha_changes_with_content():
    a = split_sections("## A\nx\n")[1]
    b = split_sections("## A\ny\n")[1]
    assert a.sha != b.sha and len(a.sha) == 64


def test_crlf_round_trip():
    doc = "# T\r\nintro\r\n## A\r\nbody\r\n"
    sections = split_sections(doc)
    assert _join(sections) == doc
    assert [s.heading for s in sections] == ["", "A"]
