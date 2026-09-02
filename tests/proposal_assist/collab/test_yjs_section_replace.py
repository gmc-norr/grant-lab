import pycrdt
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


def test_hard_break_before_heading_text_is_no_longer_a_phantom_boundary():
    # Until 2026-09-02 a hard_break serialized "## FAKE" onto its own line, which
    # split_sections counted as a boundary while the block map did not; the
    # divergence guard was the only thing standing between that and a wrong-
    # section splice. The serializer now escapes a line-start construct after a
    # hard break (`x\\\n\\## FAKE`), so the two partitions agree and the
    # document round-trips as the same four blocks.
    doc = pm_json_to_ydoc(_pm_doc(_PHANTOM))
    pm_json = ydoc_to_pm_json(doc)
    md = pm_to_markdown(pm_json)
    from call_manager.proposal_assist.collab.yjs_bridge import _block_starts_section

    starts = [0] + [i for i, b in enumerate(pm_json["content"]) if _block_starts_section(b)]
    assert len(split_sections(md)) == len(starts) == 2
    assert [b["type"] for b in markdown_to_pm(md)["content"]] == [
        "paragraph", "paragraph", "heading", "paragraph",
    ]
    apply_pm_section_to_fragment(doc, section_index=1, blocks=_blocks_of("New.\n"))
    assert pm_to_markdown(ydoc_to_pm_json(doc)) == "Intro.\n\nx\\\n\\## FAKE\n\nNew.\n"


def test_divergence_guard_raises_before_mutation(monkeypatch):
    # No editor-producible shape diverges any more, so the guard is exercised
    # with an injected disagreement: the line scan reports one section more
    # than the block map. It must raise BEFORE touching the doc.
    from call_manager.proposal_assist.collab import yjs_bridge

    doc = pm_json_to_ydoc(_pm_doc(_PHANTOM))
    md_before = pm_to_markdown(ydoc_to_pm_json(doc))
    real = yjs_bridge.split_sections
    monkeypatch.setattr(yjs_bridge, "split_sections", lambda md: real(md + "\n## EXTRA\n"))
    with pytest.raises(SectionMapDivergenceError):
        apply_pm_section_to_fragment(doc, section_index=1, blocks=_blocks_of("New.\n"))
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


# ---------------------------------------------------------------------------
# Regression: the REPLAYED-room shape.
#
# Every test above builds the doc directly with pm_json_to_ydoc, so the
# fragment's children were created by the same Doc that then edits them. A
# production room is never that: hydrate.py seeds a doc, and the room replays
# that update into a DIFFERENT Doc with its own client id.
#
# In pycrdt 0.14.0, children.insert(0, x) APPENDS instead of prepending on such
# a replayed fragment whenever the seed's client id is LOWER than the room's.
# Client ids are random, so this scrambled ~48% of real section-1 edits on a
# document opening with "## " — and the checkpoint committed the result to git.
# A fresh-doc test cannot fail on it; only a replayed one can.
# ---------------------------------------------------------------------------

_HEADING_FIRST = "## Aims\n\nOriginal aims.\n\n## Approach\n\nOriginal approach.\n"


def _replayed_room(markdown: str, *, seed_client_id: int, room_client_id: int):
    """Mirror hydrate.py + canonical.build_doc: seed one Doc, replay into another.

    The client ids are pinned so the ordering that triggers the pycrdt bug is
    exercised on purpose rather than half the time.
    """
    seed = pm_json_to_ydoc(markdown_to_pm(markdown), client_id=seed_client_id)
    room = pycrdt.Doc(client_id=room_client_id)
    room.apply_update(seed.get_update())
    return room


@pytest.mark.parametrize("seed_cid,room_cid", [(1, 2), (2, 1), (7, 7)])
def test_replace_first_section_of_a_replayed_room_keeps_order(seed_cid, room_cid):
    # seed_cid < room_cid is the ordering that used to append instead of
    # prepend; the other two are included so a fix that only works one way
    # cannot pass.
    room = _replayed_room(_HEADING_FIRST, seed_client_id=seed_cid, room_client_id=room_cid)
    apply_pm_section_to_fragment(
        room, section_index=1, blocks=_blocks_of("## Aims\n\nEDITED aims.\n")
    )
    out = pm_to_markdown(ydoc_to_pm_json(room))
    assert out == "## Aims\n\nEDITED aims.\n\n## Approach\n\nOriginal approach.\n", out
    secs = split_sections(out)
    assert [s.heading for s in secs] == ["", "Aims", "Approach"]


def test_replayed_room_preamble_edit_keeps_order():
    # start=0 but end>0 (the document HAS a preamble), so the replacement is
    # inserted after the old range and the guard does not apply.
    room = _replayed_room(_DOC, seed_client_id=1, room_client_id=9)
    apply_pm_section_to_fragment(room, section_index=0, blocks=_blocks_of("New preamble.\n"))
    secs = split_sections(pm_to_markdown(ydoc_to_pm_json(room)))
    assert "New preamble." in secs[0].text
    assert [s.heading for s in secs] == ["", "A", "B"]


def test_front_insert_into_empty_preamble_raises_rather_than_scrambling():
    # start==0 AND end==0: a pure prepend, which pycrdt cannot do reliably on a
    # replayed fragment. The kernel never asks for this (it writes the section
    # containing the edited text, and an empty preamble contains none), but the
    # old code answered it correctly only ~48% of the time and silently
    # scrambled the document otherwise. Failing loudly is the contract.
    room = _replayed_room(_HEADING_FIRST, seed_client_id=1, room_client_id=2)
    with pytest.raises(SectionMapDivergenceError, match="front of a non-empty document"):
        apply_pm_section_to_fragment(room, section_index=0, blocks=_blocks_of("Sneaky.\n"))
