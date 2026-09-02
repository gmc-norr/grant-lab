import pytest

from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import (
    SectionMapDivergenceError,
    apply_pm_section_to_fragment,
    pm_json_to_ydoc,
    ydoc_to_pm_json,
)
from call_manager.proposal_assist.editing.sectionizer import split_sections


def _blocks_of(md):
    return markdown_to_pm(md).get("content") or []


@pytest.mark.parametrize("md", [
    "Preamble.\n\n## A\n\nBody a.\n\n## B\n\nBody b.\n",
    "## A\n\nBody a.\n\n## B\n\nBody b.\n",                    # no preamble
    "Intro.\n\n##NoSpace para\n\n## A\n\nBody.\n",             # ##NoSpace phantom
    "## Methods\n\n```\n## not a heading\n```\n\nReal body.\n",  # fenced ## (not a boundary)
])
def test_attribution_no_spurious_raise_on_valid_docs(md):
    doc = pm_json_to_ydoc(markdown_to_pm(md))
    n = len(split_sections(pm_to_markdown(ydoc_to_pm_json(doc))))
    # Editing the LAST section must apply cleanly (exercises the attribution path).
    update = apply_pm_section_to_fragment(
        doc, section_index=n - 1, blocks=_blocks_of("## Z\n\nEdited last.\n"),
    )
    assert update


def test_hard_break_phantom_no_longer_diverges_and_replaces_the_right_section():
    # The 3a hard_break phantom (split_sections=3 vs block-map=2) is gone: the
    # serializer escapes "## FAKE" after a hard break (2026-09-02), so the
    # partitions agree and section 1 is replaced, not refused.
    phantom = [
        {"type": "paragraph", "content": [{"type": "text", "text": "Intro."}]},
        {"type": "paragraph", "content": [
            {"type": "text", "text": "x"}, {"type": "hard_break"},
            {"type": "text", "text": "## FAKE"},
        ]},
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "B"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "Body b."}]},
    ]
    doc = pm_json_to_ydoc({"type": "doc", "content": phantom})
    apply_pm_section_to_fragment(doc, section_index=1, blocks=_blocks_of("New.\n"))
    assert pm_to_markdown(ydoc_to_pm_json(doc)) == "Intro.\n\nx\\\n\\## FAKE\n\nNew.\n"


def test_attribution_catches_compensating_divergence(monkeypatch):
    # A matching COUNT is necessary but not sufficient: a compensating pair (one
    # phantom block-boundary + one hidden real line-boundary) nets to the same
    # count yet misaligns the partitions. Since 2026-09-02 the serializer emits
    # no hidden line-boundaries, so the misalignment is injected: the line scan
    # returns the same NUMBER of sections with the boundary shifted by one block.
    from call_manager.proposal_assist.collab import yjs_bridge

    blocks = [
        {"type": "paragraph", "content": [{"type": "text", "text": "pre"}]},
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Y"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "body y"}]},
    ]
    doc = pm_json_to_ydoc({"type": "doc", "content": blocks})
    md_before = pm_to_markdown(ydoc_to_pm_json(doc))
    # Keep the count equal (2 sections) but move the boundary: the line scan
    # thinks section 1 starts one block LATER than the block map does.
    real = yjs_bridge.split_sections
    monkeypatch.setattr(
        yjs_bridge, "split_sections", lambda md: real("pre\n\nY\n\n## body y\n")
    )
    assert len(real("pre\n\nY\n\n## body y\n")) == 2
    with pytest.raises(SectionMapDivergenceError):
        apply_pm_section_to_fragment(doc, section_index=1, blocks=_blocks_of("## Y\n\nz.\n"))
    assert pm_to_markdown(ydoc_to_pm_json(doc)) == md_before
