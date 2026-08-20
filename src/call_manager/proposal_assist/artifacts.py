"""Single source of truth for the proposal-assist artifacts and their files.

`proposal.md` (the draft) and `proposal-spec.md` (the spec) live in one git repo
per workspace. The names themselves moved to `ports/artifacts.py` in F3d1, because
a converted tool may import only stdlib and `ports` and still has to name a file;
they are re-exported here so every existing importer is unchanged. SPEC_SKELETON
stays: it is seeded page content, not a name a tool needs.
"""

from __future__ import annotations

from call_manager.proposal_assist.ports.artifacts import (
    PROPOSAL_FILENAME as PROPOSAL_FILENAME,
)
from call_manager.proposal_assist.ports.artifacts import SPEC_FILENAME as SPEC_FILENAME
from call_manager.proposal_assist.ports.artifacts import Artifact as Artifact
from call_manager.proposal_assist.ports.artifacts import filename_for as filename_for

# The conventional starting Spec (cockpit design §8). A whitespace-only spec is
# seeded with this by POST /spec (Task 5). Leads with `## Aims`, so split_sections
# yields an empty index-0 preamble + 5 H2 sections (6 total).
SPEC_SKELETON = (
    "## Aims\n\n## Approach\n\n## Novelty & Significance\n\n## Expected Impact\n\n## Key Claims\n"
)
