"""The artifacts a tool can name, and their filenames — stdlib only.

A converted tool may import the standard library and this package, and nothing
else. It still has to say "proposal.md", so the names live here. The old home,
`proposal_assist/artifacts.py`, re-exports every one of these, so the 22 existing
importers under src/ are unchanged.

`Artifact`'s string values MUST match the `ProposedChange.target_artifact` CHECK
('draft','spec') in db/models.py and the files seeded by phases/setup.py.
"""

from __future__ import annotations

from enum import StrEnum


class Artifact(StrEnum):
    DRAFT = "draft"  # MUST match ck_proposed_changes_target_artifact (db/models.py:1181)
    SPEC = "spec"


_FILENAMES: dict[Artifact, str] = {
    Artifact.DRAFT: "proposal.md",
    Artifact.SPEC: "proposal-spec.md",
}


def filename_for(artifact: Artifact) -> str:
    return _FILENAMES[Artifact(artifact)]


PROPOSAL_FILENAME = filename_for(Artifact.DRAFT)
SPEC_FILENAME = filename_for(Artifact.SPEC)

#: The per-workspace citation ledger. Written out as a literal in FOUR places
#: before F3d1 — references/deletion.py, reconcile.py, editing/save.py and
#: tools/citations.py. The first three now import it. tools/citations.py keeps its
#: own copy until F3d3 converts that module.
CITATIONS_FILENAME = "citations.json"
