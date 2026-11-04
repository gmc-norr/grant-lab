"""The proposal workspace's 31 tables, parked.

No live code reads or writes these tables. The proposal workspace left master on
2026-11-03 (docs/superpowers/specs/2026-11-03-workspace-removal-design.md); the
code that used them is frozen on the `workspace` branch and the `workspace-v1`
tag. The tables, and the rows in them, stay in the database: the removal had no
migration.

The definitions stay so that the ORM still matches the database.
`tests/test_schema_drift.py` compares `Base.metadata` with the migrated schema,
and without these `alembic revision --autogenerate` would emit a DROP TABLE for
every one of them. `llm_spend` also has a foreign key to `workspaces`.

`grant_lab.db.models` imports this package for that reason alone, and does not
re-export its classes. Nothing else under `src/` should import it. The files were
moved here unchanged, so comments in them still name code that is now only on the
`workspace` branch.
"""

from grant_lab.db.models.workspace.call_extracts import CallRubric, CallSectionExtract
from grant_lab.db.models.workspace.documents import (
    ArtifactVersion,
    Attachment,
    ChatSummary,
    ChatTurn,
    CollabArtifactState,
    ProposedChange,
    YjsUpdate,
)
from grant_lab.db.models.workspace.jobs import (
    AttachmentJob,
    ClaimAnalysisJob,
    CoherenceJob,
    EvaluatorJob,
    TurnJob,
    UncitedClaimsJob,
)
from grant_lab.db.models.workspace.references import (
    CitationAuditVerdict,
    CitationLlmUsage,
    ClaimClassification,
    Reference,
    ReferenceChunk,
    ReviewRun,
    ReviewRunItem,
    UserClaimAnnotation,
)
from grant_lab.db.models.workspace.spend import LlmSpend
from grant_lab.db.models.workspace.workspaces import (
    Branch,
    PhaseApproval,
    SpecLockEvent,
    Workspace,
    WorkspaceEvent,
    WorkspaceSetupJob,
    WorkspaceViewer,
)

__all__ = [
    "ArtifactVersion",
    "Attachment",
    "AttachmentJob",
    "Branch",
    "CallRubric",
    "CallSectionExtract",
    "ChatSummary",
    "ChatTurn",
    "CitationAuditVerdict",
    "CitationLlmUsage",
    "ClaimAnalysisJob",
    "ClaimClassification",
    "CoherenceJob",
    "CollabArtifactState",
    "EvaluatorJob",
    "LlmSpend",
    "PhaseApproval",
    "ProposedChange",
    "Reference",
    "ReferenceChunk",
    "ReviewRun",
    "ReviewRunItem",
    "SpecLockEvent",
    "TurnJob",
    "UncitedClaimsJob",
    "UserClaimAnnotation",
    "Workspace",
    "WorkspaceEvent",
    "WorkspaceSetupJob",
    "WorkspaceViewer",
    "YjsUpdate",
]
