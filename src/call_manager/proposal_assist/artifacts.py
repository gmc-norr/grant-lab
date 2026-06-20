"""Single source of truth for the proposal-assist artifacts and their files.

`proposal.md` (the draft) and `proposal-spec.md` (the spec) live in one git
repo per workspace. `Artifact`'s string values MUST match the
`ProposedChange.target_artifact` CHECK ('draft','spec') in db/models.py and the
files seeded by phases/setup.py. PROPOSAL_FILENAME is re-exported for the
existing importers in editing/save.py and tools/proposal.py.
"""

from __future__ import annotations

from enum import StrEnum


class Artifact(StrEnum):
    DRAFT = "draft"  # values MUST match ProposedChange.target_artifact (db/models.py:958)
    SPEC = "spec"


_FILENAMES: dict[Artifact, str] = {
    Artifact.DRAFT: "proposal.md",
    Artifact.SPEC: "proposal-spec.md",
}


def filename_for(artifact: Artifact) -> str:
    return _FILENAMES[Artifact(artifact)]


PROPOSAL_FILENAME = filename_for(Artifact.DRAFT)  # back-compat re-export
SPEC_FILENAME = filename_for(Artifact.SPEC)

# The conventional starting Spec (cockpit design §8). A whitespace-only spec is
# seeded with this by POST /spec (Task 5). Leads with `## Aims`, so split_sections
# yields an empty index-0 preamble + 5 H2 sections (6 total).
SPEC_SKELETON = (
    "## Aims\n\n## Approach\n\n## Novelty & Significance\n\n## Expected Impact\n\n## Key Claims\n"
)
