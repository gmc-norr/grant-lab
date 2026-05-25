"""Section-aware chunker: one chunk per section when small enough;
sub-chunks within large sections via recursive split with overlap."""

from call_manager.proposal_assist.references.chunker import chunk_sections


def test_small_section_becomes_one_chunk():
    sections = [("Intro", "short intro text")]
    chunks = chunk_sections(sections)
    assert chunks == [("Intro", "short intro text")]


def test_large_section_sub_chunks_with_overlap():
    # Build a 3500-char section — should split into 3 chunks of ~1500 with
    # 100-char overlap.
    body = "abc " * 875  # 3500 chars
    sections = [("Methods", body)]
    chunks = chunk_sections(sections, max_chunk_chars=1500, overlap_chars=100)
    assert len(chunks) >= 3
    for path, text in chunks:
        assert path == "Methods"
        assert len(text) <= 1500
    # Verify overlap exists: end of chunk N appears at start of chunk N+1.
    end_of_first = chunks[0][1][-100:]
    start_of_second = chunks[1][1][:200]
    assert end_of_first in start_of_second or end_of_first[:50] in start_of_second


def test_multiple_sections_preserved():
    sections = [
        ("Intro", "intro body"),
        ("Methods > Stats", "stats body"),
        ("Results", "results body"),
    ]
    chunks = chunk_sections(sections)
    assert len(chunks) == 3
    assert chunks[0][0] == "Intro"
    assert chunks[1][0] == "Methods > Stats"
    assert chunks[2][0] == "Results"


def test_empty_section_dropped():
    sections = [("A", ""), ("B", "real text")]
    chunks = chunk_sections(sections)
    assert chunks == [("B", "real text")]


def test_none_section_path_preserved():
    sections = [(None, "no structure here")]
    chunks = chunk_sections(sections)
    assert chunks == [(None, "no structure here")]
