"""Every ORM model, re-exported.

Split out of a single 2313-line `models.py` on 2026-09-05. The split is by
SUBSYSTEM: `identity`, `calls`, `profiles`, `ranking` and `boards` are the app's
tables; `workspaces`, `documents`, `jobs`, `references` and `spend` belonged to
the proposal workspace, which left master on 2026-11-03. Its tables stay in the
database and stay mapped here; nothing on master reads or writes them.

**Importing this package imports all of them, and that is required, not
incidental.** `grant_lab/db/__init__.py` imports this package so that
`Base.metadata` is fully populated before Alembic resolves `target_metadata`;
without it `alembic revision --autogenerate` produces empty diffs. So do NOT
make these imports lazy to trim a boot -- the tables would go missing from the
next migration instead, silently.

The submodules deliberately do not import each other. Only three cross-class
Python references exist (Source<->Call, RankingJob->RankingJobStatus) and each
pair lives in one file; every other link is a ForeignKey("table.column")
string that SQLAlchemy resolves through the shared metadata registry.
"""

from grant_lab.db.base import Base
from grant_lab.db.models.boards import (
    ORIENTATION_ITEMS_PER_PAGE,
    Board,
    BoardItem,
    BoardSuppression,
)
from grant_lab.db.models.calls import (
    Call,
    CallRubric,
    CallSectionExtract,
    Source,
)
from grant_lab.db.models.documents import (
    ArtifactVersion,
    Attachment,
    ChatSummary,
    ChatTurn,
    CollabArtifactState,
    ProposedChange,
    YjsUpdate,
)
from grant_lab.db.models.identity import (
    User,
)
from grant_lab.db.models.jobs import (
    AttachmentJob,
    ClaimAnalysisJob,
    CoherenceJob,
    EvaluatorJob,
    TurnJob,
    UncitedClaimsJob,
)
from grant_lab.db.models.profiles import (
    Profile,
    ProfileBootstrapJob,
    ProfileKind,
    ProfileLevel,
    ProfileViewer,
    Subscription,
)
from grant_lab.db.models.ranking import (
    CallDecision,
    CallRanking,
    CallRankingCriterion,
    DigestRun,
    GoldenSetTriple,
    RankingJob,
    RankingJobStatus,
    RankingRemoteAttempt,
)
from grant_lab.db.models.references import (
    CitationAuditVerdict,
    CitationLlmUsage,
    ClaimClassification,
    Reference,
    ReferenceChunk,
    ReviewRun,
    ReviewRunItem,
    UserClaimAnnotation,
)
from grant_lab.db.models.spend import (
    LlmSpend,
)
from grant_lab.db.models.workspaces import (
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
    "Base",
    "Board",
    "BoardItem",
    "BoardSuppression",
    "Branch",
    "Call",
    "CallDecision",
    "CallRanking",
    "CallRankingCriterion",
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
    "DigestRun",
    "EvaluatorJob",
    "GoldenSetTriple",
    "LlmSpend",
    "ORIENTATION_ITEMS_PER_PAGE",
    "PhaseApproval",
    "Profile",
    "ProfileBootstrapJob",
    "ProfileKind",
    "ProfileLevel",
    "ProfileViewer",
    "ProposedChange",
    "RankingJob",
    "RankingJobStatus",
    "RankingRemoteAttempt",
    "Reference",
    "ReferenceChunk",
    "ReviewRun",
    "ReviewRunItem",
    "Source",
    "SpecLockEvent",
    "Subscription",
    "TurnJob",
    "UncitedClaimsJob",
    "User",
    "UserClaimAnnotation",
    "Workspace",
    "WorkspaceEvent",
    "WorkspaceSetupJob",
    "WorkspaceViewer",
    "YjsUpdate",
]
