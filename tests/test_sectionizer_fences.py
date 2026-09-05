"""Fence-length rule in split_sections (review 2026-09-02 §2.3).

A code block containing a ``` line is serialized inside a LONGER fence by the
collab bridge. The sectionizer used to toggle on every ```-prefixed line, so the
inner ``` "closed" the fence, the closing ```` "opened" one, and every heading
after the block was swallowed into it."""

from grant_lab.proposal_assist.editing.sectionizer import split_sections


def _headings(md: str) -> list[str]:
    return [s.heading for s in split_sections(md) if s.heading]


def test_a_longer_fence_is_not_closed_by_a_shorter_backtick_line():
    md = "## A\n\n````\nx\n```\ny\n````\n\n## Later\n\nbody\n"
    assert _headings(md) == ["A", "Later"]
    assert "".join(s.text for s in split_sections(md)) == md


def test_a_closing_fence_carries_no_info_string():
    # "```py" inside an open ``` fence is code, not a closer.
    md = "```\n```py\n## not a heading\n```\n\n## Real\n"
    assert _headings(md) == ["Real"]


def test_plain_fences_still_hide_headings():
    md = "## M\n\n```\n## not a heading\n```\n\nReal body.\n"
    assert _headings(md) == ["M"]


def test_an_unclosed_fence_hides_everything_after_it():
    md = "## M\n\n````\n```\n## still inside\n"
    assert _headings(md) == ["M"]
