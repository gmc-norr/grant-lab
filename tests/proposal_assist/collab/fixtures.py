from call_manager.proposal_assist.citations.markers import build_marker
from call_manager.proposal_assist.artifacts import SPEC_SKELETON

_CITE = build_marker("smith2021", "ev-0a1b2c3d4e5f")   # full marker → citation node
_CITE2 = build_marker("jones2020", "ev-9f8e7d6c5b4a")

CORPUS: list[tuple[str, str]] = [
    ("preamble_only", "Intro paragraph with no heading.\n"),
    ("single_section", "## Aims\n\nWe aim to do the thing.\n"),
    ("multi_section_h3",
     "## Approach\n\n### Phase 1\n\nDo the first part.\n\n### Phase 2\n\nThen the second.\n"),
    ("marks_and_links",
     "## Novelty\n\nThis is **bold**, *italic*, ~~struck~~, `code`, and a [link](https://example.org).\n"),
    ("nested_marked_list",
     "## Plan\n\n- **bold item** with a [link](https://example.org)\n  - nested item\n- second\n\n1. one\n2. two\n"),
    ("blockquote_and_code",
     "## Notes\n\n> a quote\n\n```python\nprint('hi')\n```\n"),
    ("horizontal_rule", "## Sep\n\nabove\n\n---\n\nbelow\n"),
    ("image", "## Figure\n\n![alt text](https://example.org/fig.png)\n"),
    ("hard_break", "## Address\n\nline one\\\nline two after a hard break.\n"),
    # a "## " inside a fence must NOT split a section:
    ("fenced_h2_not_a_section", "## Methods\n\n```\n## not a heading\n```\n\nReal body.\n"),
    ("citation", f"## Key Claims\n\nThe sky is blue {_CITE} and well studied.\n"),
    ("adjacent_citations", f"## Key Claims\n\nMultiple sources agree {_CITE}{_CITE2} on this.\n"),
    # High-2 existing-document shapes — MUST hydrate without content migration:
    ("bare_marker_is_literal_text", "## Background\n\nold body [@x9] stays literal text.\n"),
    ("h1_title_preamble", "# Proposal Title\n\nIntro under an H1 title.\n\n## Aims\n\nBody.\n"),
    # byte-identical to tests/test_editing_save.py's "production layout" (tight, H1 + bare marker):
    ("production_layout", "# T\nintro\n## Background\nold body [@x9]\n## Aims\naims\n"),
    ("spec_skeleton", SPEC_SKELETON),
]
