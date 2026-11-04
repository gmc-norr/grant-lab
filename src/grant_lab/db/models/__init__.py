"""Every ORM model the live app uses, re-exported; and the parked ones, registered.

Split out of a single 2313-line `models.py` on 2026-09-05, by SUBSYSTEM:
`identity`, `calls`, `profiles`, `ranking` and `boards` hold the app's 17 live
tables.

`workspace/` holds the proposal workspace's 31 tables, PARKED when the workspace
left master on 2026-11-03: they stay in the database, nothing reads or writes
them, and they are deliberately NOT re-exported here. See that package's
docstring.

**Importing this package imports all of them, and that is required, not
incidental.** `grant_lab/db/__init__.py` imports this package so that
`Base.metadata` is fully populated before Alembic resolves `target_metadata`;
without it `alembic revision --autogenerate` produces empty diffs -- and, for the
parked package, DROP TABLE for 31 tables that still hold data.
`tests/test_schema_drift.py` checks the result. So do NOT make these imports lazy
to trim a boot, and do not drop the `workspace` import because nothing uses it.

The submodules deliberately do not import each other. Only three cross-class
Python references exist (Source<->Call, RankingJob->RankingJobStatus) and each
pair lives in one file; every other link is a ForeignKey("table.column")
string that SQLAlchemy resolves through the shared metadata registry.
"""

from grant_lab.db.base import Base
from grant_lab.db.models import workspace  # noqa: F401 -- registers the parked tables
from grant_lab.db.models.boards import (
    ORIENTATION_ITEMS_PER_PAGE,
    Board,
    BoardItem,
    BoardSuppression,
)
from grant_lab.db.models.calls import (
    Call,
    Source,
)
from grant_lab.db.models.identity import (
    User,
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

__all__ = [
    "Base",
    "Board",
    "BoardItem",
    "BoardSuppression",
    "Call",
    "CallDecision",
    "CallRanking",
    "CallRankingCriterion",
    "DigestRun",
    "GoldenSetTriple",
    "ORIENTATION_ITEMS_PER_PAGE",
    "Profile",
    "ProfileBootstrapJob",
    "ProfileKind",
    "ProfileLevel",
    "ProfileViewer",
    "RankingJob",
    "RankingJobStatus",
    "RankingRemoteAttempt",
    "Source",
    "Subscription",
    "User",
]
