import pytest

from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import (
    SectionMapDivergenceError,
    apply_pm_section_to_fragment,
    pm_json_to_ydoc,
    ydoc_to_pm_json,
)
from call_manager.proposal_assist.editing.sectionizer import split_sections

_DOC = "Preamble.\n\n## A\n\nBody a.\n\n## B\n\nBody b.\n"


def _blocks_of(section_md):
    return markdown_to_pm(section_md).get("content") or []


def test_replace_middle_section_only():
    doc = pm_json_to_ydoc(markdown_to_pm(_DOC))
    # Replace section 1 ("## A ...") with new content.
    new_md = "## A\n\nBody a EDITED.\n"
    update = apply_pm_section_to_fragment(doc, section_index=1, blocks=_blocks_of(new_md))
    assert update  # non-empty incremental update
    out = pm_to_markdown(ydoc_to_pm_json(doc))
    secs = split_sections(out)
    assert "Body a EDITED." in secs[1].text
    assert "Preamble." in secs[0].text          # preamble untouched
    assert "Body b." in secs[2].text            # section B untouched


def test_replace_preamble_section_zero():
    doc = pm_json_to_ydoc(markdown_to_pm(_DOC))
    update = apply_pm_section_to_fragment(
        doc, section_index=0, blocks=_blocks_of("New preamble.\n")
    )
    assert update
    secs = split_sections(pm_to_markdown(ydoc_to_pm_json(doc)))
    assert "New preamble." in secs[0].text
    assert secs[1].heading == "A" and secs[2].heading == "B"


def test_replace_last_section():
    doc = pm_json_to_ydoc(markdown_to_pm(_DOC))
    update = apply_pm_section_to_fragment(doc, section_index=2, blocks=_blocks_of("## B\n\nB2.\n"))
    assert update
    secs = split_sections(pm_to_markdown(ydoc_to_pm_json(doc)))
    assert "B2." in secs[2].text
    assert "Body a." in secs[1].text


def test_replace_with_different_block_count():
    doc = pm_json_to_ydoc(markdown_to_pm(_DOC))
    # Section A grows from 1 body paragraph to 2.
    update = apply_pm_section_to_fragment(
        doc, section_index=1, blocks=_blocks_of("## A\n\nP1.\n\nP2.\n")
    )
    assert update
    secs = split_sections(pm_to_markdown(ydoc_to_pm_json(doc)))
    assert "P1." in secs[1].text and "P2." in secs[1].text
    assert "Body b." in secs[2].text            # B still intact after a size change


# --- Regression tests for the boundary rule (these caught real defects) ---

_NO_PREAMBLE = "## A\n\nBody a.\n\n## B\n\nBody b.\n"


def test_no_preamble_doc_section_index_alignment():
    # split_sections ALWAYS emits Section 0 = preamble (possibly empty), so for a
    # doc starting with '## A', section 1 = A. The boundary map MUST match: editing
    # section_index=1 replaces A, NOT B. (Regression: a preamble-collapse would
    # silently splice B — data loss.)
    doc = pm_json_to_ydoc(markdown_to_pm(_NO_PREAMBLE))
    update = apply_pm_section_to_fragment(
        doc, section_index=1, blocks=_blocks_of("## A\n\nA EDITED.\n")
    )
    assert update
    out = pm_to_markdown(ydoc_to_pm_json(doc))
    secs = split_sections(out)
    assert secs[1].heading == "A" and "A EDITED." in secs[1].text
    assert secs[2].heading == "B" and "Body b." in secs[2].text   # B untouched
    # And section_index=2 (valid last section) does not raise + targets B.
    doc2 = pm_json_to_ydoc(markdown_to_pm(_NO_PREAMBLE))
    apply_pm_section_to_fragment(doc2, section_index=2, blocks=_blocks_of("## B\n\nB EDITED.\n"))
    assert "B EDITED." in split_sections(pm_to_markdown(ydoc_to_pm_json(doc2)))[2].text


def test_hash_hash_nospace_paragraph_is_a_boundary_like_split_sections():
    # '##NoSpace' is a split_sections boundary (_H2_RE) but a PARAGRAPH in PM, and
    # pm_to_markdown round-trips it unescaped — so the boundary rule must count it
    # (via is_h2_heading_line on the serialized line), matching split_sections.
    md = "Intro.\n\n##NoSpace para\n\n## A\n\nBody.\n"
    doc = pm_json_to_ydoc(markdown_to_pm(md))
    secs_before = split_sections(pm_to_markdown(ydoc_to_pm_json(doc)))
    assert len(secs_before) == 3   # preamble, ##NoSpace, A
    # Editing section 2 (the real '## A') must target A, not run off the end.
    update = apply_pm_section_to_fragment(
        doc, section_index=2, blocks=_blocks_of("## A\n\nA EDITED.\n")
    )
    assert update
    secs = split_sections(pm_to_markdown(ydoc_to_pm_json(doc)))
    assert "A EDITED." in secs[2].text
    assert "##NoSpace para" in secs[1].text   # the phantom-boundary section preserved


def test_clear_section_to_empty_blocks():
    # Empty blocks (clearing a section) delete the range and insert nothing; the
    # kernel maps an empty new_text to blocks=[] (see Task 6). Non-final preamble
    # clear keeps the doc valid because other sections remain.
    doc = pm_json_to_ydoc(markdown_to_pm(_DOC))
    update = apply_pm_section_to_fragment(doc, section_index=0, blocks=[])
    assert update
    out = pm_to_markdown(ydoc_to_pm_json(doc))
    assert "Preamble." not in out               # the cleared text is actually gone
    secs = split_sections(out)
    assert secs[0].text == ""                   # section 0 = now-empty preamble
    assert secs[1].heading == "A"
    assert "Body b." in secs[-1].text


# ---------------------------------------------------------------------------
# Section-map divergence detection (Item 1 — pre-mutation guards)
# ---------------------------------------------------------------------------


def _pm_doc(blocks):
    return {"type": "doc", "content": blocks}


_PHANTOM = [
    {"type": "paragraph", "content": [{"type": "text", "text": "Intro."}]},
    {"type": "paragraph", "content": [
        {"type": "text", "text": "x"}, {"type": "hard_break"},
        {"type": "text", "text": "## FAKE"},
    ]},
    {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "B"}]},
    {"type": "paragraph", "content": [{"type": "text", "text": "Body b."}]},
]


def test_hard_break_phantom_boundary_raises():
    # Verify the divergence is real: hard_break serializes as backslash-newline
    # so "## FAKE" becomes a new line that split_sections counts as a boundary,
    # but the block-map's _block_starts_section only checks each block's FIRST line.
    doc = pm_json_to_ydoc(_pm_doc(_PHANTOM))
    pm_json = ydoc_to_pm_json(doc)
    md = pm_to_markdown(pm_json)
    n_line = len(split_sections(md))
    blocks_json = pm_json.get("content") or []

    from call_manager.proposal_assist.collab.yjs_bridge import _block_starts_section
    starts = [0] + [i for i, b in enumerate(blocks_json) if _block_starts_section(b)]
    # The divergence must be present in this construction; if not, the guard is
    # not testable and we should report BLOCKED rather than a vacuous pass.
    assert n_line != len(starts), (
        f"Expected divergence: split_sections={n_line} vs block-map={len(starts)}; "
        "hard_break may not serialize to a newline — stop and report BLOCKED"
    )

    # The guard raises BEFORE any mutation.
    md_before = pm_to_markdown(ydoc_to_pm_json(doc))
    with pytest.raises(SectionMapDivergenceError):
        apply_pm_section_to_fragment(doc, section_index=1, blocks=_blocks_of("New.\n"))
    # Doc is unchanged after the raise (no partial mutation).
    assert pm_to_markdown(ydoc_to_pm_json(doc)) == md_before


def test_fence_flip_divergence_raises():
    # In this repo the markdown serializer escapes backticks in inline text
    # (pm_to_markdown renders inline ``` as \`\`\`), so the fence-flip direction
    # of the divergence does NOT reproduce — split_sections sees the escaped form
    # and does NOT flip its fence state. Confirmed by empirical check below.
    _FENCE_ATTEMPT = [
        {"type": "paragraph", "content": [{"type": "text", "text": "Intro."}]},
        {"type": "paragraph", "content": [
            {"type": "text", "text": "x"}, {"type": "hard_break"},
            {"type": "text", "text": "```"},
        ]},
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "B"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "Body b."}]},
    ]
    doc = pm_json_to_ydoc(_pm_doc(_FENCE_ATTEMPT))
    pm_json = ydoc_to_pm_json(doc)
    md = pm_to_markdown(pm_json)
    n_line = len(split_sections(md))
    blocks_json = pm_json.get("content") or []

    from call_manager.proposal_assist.collab.yjs_bridge import _block_starts_section
    starts = [0] + [i for i, b in enumerate(blocks_json) if _block_starts_section(b)]
    if n_line == len(starts):
        # No divergence in this repo: backtick escaping prevents fence-flip.
        # The SectionMapDivergenceError guard still fires correctly for the
        # hard_break case above (see test_hard_break_phantom_boundary_raises).
        # This branch confirms no spurious raise on a non-divergent doc.
        doc2 = pm_json_to_ydoc(markdown_to_pm(_DOC))
        update = apply_pm_section_to_fragment(
            doc2, section_index=1, blocks=_blocks_of("## A\n\nNo raise.\n")
        )
        assert update
        return
    # If the divergence DOES appear in a future build, the guard must fire.
    with pytest.raises(SectionMapDivergenceError):
        apply_pm_section_to_fragment(doc, section_index=1, blocks=_blocks_of("## B\n\nX.\n"))
