"""THE DECISIVE NEGATIVE (spec 9).

`editing.sectionizer` contains no SQLAlchemy in its own module, so a
forbidden-name implementation of the gate would wave this through. It must be
rejected: a tool that can reach arbitrary project helpers can reach effectful
ones, and this is the shape that proves the allowlist is an allowlist.

PARSED by the import gate, never imported and never collected.
"""

from __future__ import annotations

from call_manager.proposal_assist.editing.sectionizer import split_sections


def count_sections(markdown: str) -> int:
    return len(split_sections(markdown))
