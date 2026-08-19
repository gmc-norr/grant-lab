"""The locator arriving as a PARAMETER, not as an attribute.

Spec 4.1. The imports are clean AND no attribute is named, so an attribute-only
locator rule reports nothing at all — and this module overwrites the artifact a
human is editing. The composition root wires it with `ctx.repo_dir`, and
`registry.py` is permanently skipped by the gate, so there is no second line of
defence. `citations.py:135` is this shape in production today.

PARSED by the import gate, never imported and never collected.
"""

from __future__ import annotations


def overwrite(repo_dir, new_text: str) -> None:
    (repo_dir / "proposal.md").write_text(new_text)
