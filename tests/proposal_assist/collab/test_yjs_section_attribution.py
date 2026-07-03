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


def test_attribution_still_raises_on_hard_break_phantom():
    # The 3a hard_break phantom (split_sections=3 vs block-map=2) still raises —
    # now caught by attribution (and the count too).
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
    with pytest.raises(SectionMapDivergenceError):
        apply_pm_section_to_fragment(doc, section_index=1, blocks=_blocks_of("New.\n"))


def test_attribution_catches_compensating_divergence():
    # Construct empirically: counts must MATCH (old guard would pass) while the
    # partitions misalign. Verify the precondition explicitly; if it cannot be
    # constructed against this serializer, the attribution is defense-in-depth and
    # the count check already suffices for reachable inputs — record that as a
    # finding in the commit message rather than asserting a vacuous pass.
    from call_manager.proposal_assist.collab.yjs_bridge import _block_starts_section
    blocks = [
        # section 0 preamble holds a hidden real boundary (hard_break + "## X"):
        {"type": "paragraph", "content": [
            {"type": "text", "text": "pre"}, {"type": "hard_break"},
            {"type": "text", "text": "## X"},
        ]},
        # a real heading whose first line IS "## Y":
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Y"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "body y"}]},
    ]
    doc = pm_json_to_ydoc({"type": "doc", "content": blocks})
    pm_json = ydoc_to_pm_json(doc)
    blocks_json = pm_json.get("content") or []
    starts = [0] + [i for i, b in enumerate(blocks_json) if _block_starts_section(b)]
    n_line = len(split_sections(pm_to_markdown(pm_json)))
    if len(starts) == n_line:
        # Precondition met (counts match) → attribution MUST catch the misalignment.
        with pytest.raises(SectionMapDivergenceError):
            apply_pm_section_to_fragment(doc, section_index=1, blocks=_blocks_of("## Y\n\nz.\n"))
    else:
        # Counts differ here → the count check already catches it; the compensating
        # shape needs a different construction. Assert the divergence IS caught
        # (either way the guard must refuse), and note it for the reviewer.
        with pytest.raises(SectionMapDivergenceError):
            apply_pm_section_to_fragment(doc, section_index=1, blocks=_blocks_of("## Y\n\nz.\n"))
