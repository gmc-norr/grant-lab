# Workspace removal, pieces "3. What stays" and "4. Gates": exact code map

**Base.** HEAD is `388494c8` (READ, `git rev-parse`). The working tree has unrelated changes in `docs/research/` only.

**Labels used below:**
- **READ**: read from source.
- **MEASURED**: ran a read-only command here (ruff on stdin with `--no-cache`, `python -B` compile/AST/import). No files were written.
- **INFERRED**: reasoning from the code.
- **PREDICTED**: a gate outcome I did not run.

**One lint artefact to expect.** On stdin, ruff flags I001 for `grant_lab.db.models.workspace` and `tests.migrations.support` imports. Those paths don't exist yet, so ruff classes them as third-party. I re-linted with existing module names substituted, and every file below passed. Re-run `uv run ruff check .` once the files exist.

---

## TASK 1: Move the table definitions

### 1a. `src/grant_lab/db/models/__init__.py` today (READ, 149 lines, verbatim)

```python
"""Every ORM model, re-exported.

Split out of a single 2313-line `models.py` on 2026-09-05. The split is by
SUBSYSTEM, and the module names carry the edition boundary: `identity`,
`calls`, `profiles` and `ranking` are the MATCH edition's tables; `workspaces`,
`documents`, `jobs`, `references` and `spend` belong to proposal-assist.

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
    "ArtifactVersion", "Attachment", "AttachmentJob", "Base", "Board", "BoardItem",
    "BoardSuppression", "Branch", "Call", "CallDecision", "CallRanking",
    "CallRankingCriterion", "CallRubric", "CallSectionExtract", "ChatSummary", "ChatTurn",
    "CitationAuditVerdict", "CitationLlmUsage", "ClaimAnalysisJob", "ClaimClassification",
    "CoherenceJob", "CollabArtifactState", "DigestRun", "EvaluatorJob", "GoldenSetTriple",
    "LlmSpend", "ORIENTATION_ITEMS_PER_PAGE", "PhaseApproval", "Profile",
    "ProfileBootstrapJob", "ProfileKind", "ProfileLevel", "ProfileViewer", "ProposedChange",
    "RankingJob", "RankingJobStatus", "RankingRemoteAttempt", "Reference", "ReferenceChunk",
    "ReviewRun", "ReviewRunItem", "Source", "SpecLockEvent", "Subscription", "TurnJob",
    "UncitedClaimsJob", "User", "UserClaimAnnotation", "Workspace", "WorkspaceEvent",
    "WorkspaceSetupJob", "WorkspaceViewer", "YjsUpdate",
]
```
(In the file, `__all__` has one name per line. I only compressed it here.)

### 1b. git mv commands

`git mv` refuses a destination whose parent directory doesn't exist, so create the folder first.
```bash
mkdir -p src/grant_lab/db/models/workspace
git mv src/grant_lab/db/models/documents.py  src/grant_lab/db/models/workspace/documents.py
git mv src/grant_lab/db/models/jobs.py       src/grant_lab/db/models/workspace/jobs.py
git mv src/grant_lab/db/models/references.py src/grant_lab/db/models/workspace/references.py
git mv src/grant_lab/db/models/spend.py      src/grant_lab/db/models/workspace/spend.py
git mv src/grant_lab/db/models/workspaces.py src/grant_lab/db/models/workspace/workspaces.py
```
- The five moved files need no content change. Each imports only `grant_lab.db.base.Base`, sqlalchemy, pgvector and stdlib (READ).
- Then create the two new files below.
- Then edit `calls.py`: delete from line 145 (the blank line after `return self.raw or {}`) through line 185 (the end of the file), so the file ends at line 144 plus a newline. No import line changes (see 1f).

### 1c. New `src/grant_lab/db/models/workspace/__init__.py` (MEASURED: ruff clean)

```python
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
```

### New `src/grant_lab/db/models/workspace/call_extracts.py` (MEASURED: ruff clean; the class bodies are `calls.py` lines 147-185, verbatim)

```python
"""Text the proposal workspace derived from a call: per-section extracts and the
funder's rubric, both written by the workspace's Phase 0.

Moved unchanged from `db/models/calls.py` on 2026-11-03. Parked: see this
package's `__init__.py`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from grant_lab.db.base import Base


class CallSectionExtract(Base):
    __tablename__ = "call_section_extracts"
    __table_args__ = (
        CheckConstraint(
            "section IN ('eligibility','scope','budget',"
            "'expected_outcomes','restrictions','mandatory')",
            name="ck_call_section_extracts_section",
        ),
    )

    call_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("calls.id", ondelete="CASCADE"),
        primary_key=True,
    )
    section: Mapped[str] = mapped_column(Text, primary_key=True)
    extract_text: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    extracted_by_model: Mapped[str] = mapped_column(Text, nullable=False)
    extractor_version: Mapped[str] = mapped_column(Text, nullable=False)
    source_text_hash: Mapped[str] = mapped_column(Text, nullable=False)


class CallRubric(Base):
    __tablename__ = "call_rubrics"

    call_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("calls.id", ondelete="CASCADE"),
        primary_key=True,
    )
    criteria: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    funder_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    extracted_by_model: Mapped[str] = mapped_column(Text, nullable=False)
    extractor_version: Mapped[str] = mapped_column(Text, nullable=False)
    source_text_hash: Mapped[str] = mapped_column(Text, nullable=False)
```

### New content of `src/grant_lab/db/models/__init__.py` (MEASURED: ruff clean)

```python
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
```
- Import order still works (INFERRED). `grant_lab/db/__init__` imports `grant_lab.db.models`. That runs `grant_lab.db.base` (defines `Base`), then `grant_lab.db.models.workspace`, whose files import the already-loaded `grant_lab.db.base`.
- `tests/architecture/test_no_import_cycles.py` stays green (INFERRED). The only new edges are `models` → `models.workspace` → `workspace.*` → `db.base`, and the gate skips edges to an importer's own ancestors.

### 1d. Every import of the moving modules or classes outside `proposal_assist` (MEASURED: AST scan of all `git ls-files *.py`)

**Kept files that need a fix**

| file:line | today | fix |
|---|---|---|
| `src/grant_lab/main.py:162` (used at :166) | `from grant_lab.db.models import Reference` inside the start-up reset of `Reference.text_status=='running'` | Delete the whole reset block. Spec §2 removes it. |
| `src/grant_lab/web/api/admin.py:12` (names at :14 `ChatTurn`, :21 `Workspace`, :22 `WorkspaceEvent`) | used only by `_drafting_panel_context` (:276) and `drafting_router` (:36, :351), which the spec removes | Delete those three names from the import. |
| `src/grant_lab/db/models/__init__.py:28-93` | re-exports | Replaced by the new content in 1c. |
| `scripts/collab_e2e_seed.py:109` | `Reference, ReferenceChunk, WorkspaceViewer` | Owned by the e2e seed rewrite. The CI `e2e` job runs it. If it is kept to seed the smoke-spec user, drop these three names and the workspace/collab seeding. Otherwise it goes with collab. |
| `tests/test_migration_0037_backfill.py:22` (+ `tests.proposal_assist.conftest` at :23) | — | Replaced by the full file in 3c. |
| `tests/test_migration_0040_bibliography_settings.py:100,142` / `:111,173,192` | `from grant_lab.db.models import Workspace` / `… import Reference` | `from grant_lab.db.models.workspace import Workspace` / `… import Reference`. Line 91 (`User`) stays. |
| `tests/test_migration_0041_user_editing.py:5-11` | one import of 5 names | `from grant_lab.db.models import User` and `from grant_lab.db.models.workspace import (ClaimAnalysisJob, ClaimClassification, UserClaimAnnotation, Workspace)` |
| `tests/test_migration_0064_claimed_at_generation.py:76` | `from grant_lab.db.models import (AttachmentJob, …)` | `from grant_lab.db.models.workspace import (` (same names) |
| `tests/test_migration_0067_llm_spend.py:11` | `from grant_lab.db.models import LlmSpend, User, Workspace` | `from grant_lab.db.models import User` and `from grant_lab.db.models.workspace import LlmSpend, Workspace` |
| `tests/test_migration_0069_collab_doc_epoch.py:7` | `from grant_lab.db.models import CollabArtifactState, User, Workspace` | `from grant_lab.db.models import User` and `from grant_lab.db.models.workspace import CollabArtifactState, Workspace` |
| `tests/test_migration_0083_llm_spend_setup_kind.py:11` | `from tests.proposal_assist.conftest import make_workspace` (not a model import, but its source is deleted) | `from tests.migrations.support import make_workspace` |

**Deleted with the workspace** (READ: each imports `grant_lab.proposal_assist`, `grant_lab.workspaces` or a deleted `web/api` module, or calls `/api/workspaces`):
- **src:** `src/grant_lab/workspaces.py:28`; `src/grant_lab/web/api/{admin_retention:22, annotations:18, files:20, references:22, rubric:18, settings:16, turns:20, workspaces:21}.py`.
- **tests:**
  - `tests/architecture/test_branch_entity_invariants.py:12` (and :13-21, which import PA)
  - `tests/admin_retention_helpers.py:19`; it is used only by the retention tests and `test_workspace_restore_acceptance`
  - `tests/test_analysis_{insert,invalidation,pipeline,queue,reconcile}.py`
  - `tests/test_api_{admin_drafting, admin_retention_inspect, admin_retention_list, admin_retention_restore, citation_audit, citation_audit_defect2, export, files, references, references_defect1, rubric, settings, spec, spec_lock, turns, workspaces}.py`
  - `tests/test_citation_cloud_egress_gate.py`, `tests/test_editing_{e2e,enqueue_race,save}.py`, `tests/test_embeddings_truncation_defect.py`, `tests/test_lifecycle_generation.py`
  - `tests/test_pool_exhaustion_event_loop.py` (:125, and :144 imports `web.api.turns`)
  - `tests/test_profile_access_workspaces.py` (POST `/api/workspaces`)
  - `tests/test_workspace_restore_{acceptance,service}.py`, `tests/test_workspaces_service.py`
  - every file under `tests/proposal_assist/`
- **`tests/fencing/*`**: all 18 files name `proposal_assist`.
  - Path literals there pin the old locations: `inventory.py:142-144` (`"db/models/{jobs,references,workspaces}.py"`), `spend_guard.py:498` (`_LLM_SPEND_MODULES`), `test_spend_guard.py:352-408` and `:700-706` (expects `"grant_lab/db/models/spend.py"`).
  - **If any of `tests/fencing` survives the model move,** those become `db/models/workspace/*.py` and `grant_lab.db.models.workspace[.spend]`. Otherwise `test_fence_coverage` and `test_the_ledger_has_exactly_one_writer` go red (INFERRED).

No attribute-style uses (`models.Workspace`), no `from grant_lab.db.models import *`, no `__all__` readers, and no `grant_lab.db.models.<moving>` imports in `alembic/` (MEASURED: grep).

### 1e. Relationship and ForeignKey links, both directions (READ)

- **Live → parked: none.**
  - The only `relationship()` calls in all model files are `calls.py:51` `Source.calls = relationship(back_populates="source")` and `calls.py:137` `Call.source = relationship(back_populates="calls")`. Both live in one file and neither has a string target.
  - Live FKs point only at `users`, `profiles`, `calls`, `boards`, `sources`: `identity.py:66,96-97`; `boards.py:86,136,139,195,198,205,248,251,254`; `ranking.py:75,77,79,99,125,128,208,210,251,293,296,324,326,358,360,365`; `profiles.py:101,109,164,169,183,186,220`.
- **Parked → live: ForeignKey strings only, no `relationship()` anywhere in the parked files.**
  - `workspaces.py:62` owner_id→users RESTRICT, `:67` call_id→calls SET NULL, `:74` profile_id→profiles SET NULL, `:176` spec_locked_by→users SET NULL, `:289,:333,:377,:415`→users.
  - `references.py:239,464`→users.
  - `jobs.py:83,122,169,225`→users.
  - `documents.py:196,199,236,277`→users.
  - `CallSectionExtract`/`CallRubric`→`calls.id` CASCADE.
- **Parked ↔ parked:**
  - `workspaces.root_branch_id → branches.id`, `use_alter=True`, RESTRICT (`workspaces.py:157-162`), and `branches.workspace_id → workspaces.id` CASCADE (`:260`). Both are in the same moved file.
  - `llm_spend → workspaces` (`spend.py:65`).
- **Consequence (INFERRED):**
  - No string relationship targets and no `back_populates` cross the boundary, so mapper configuration does not depend on import order.
  - FK strings resolve lazily through the shared `Base.metadata`, which is complete once `db/models/__init__` imports `workspace`.
  - The RESTRICT FKs to `users` stay in the database. A hard-delete of a user who owns workspace rows would still be refused. No live code hard-deletes users (MEASURED: grep for `delete(User)`, `DELETE FROM users`).
  - No ORM table is defined anywhere outside `db/models` (MEASURED), so deleting `proposal_assist` does not change the metadata.

### 1f. `calls.py` after removing the two classes

**No import becomes unused** (MEASURED: `head -145 calls.py | ruff check --select F401,I -` passes). `CheckConstraint`, `BigInteger`, `ForeignKey`, `DateTime`, `Text`, `func` and `JSONB` are all still used by `Source` and `Call`. The module docstring can stay.

### 1g. `scripts/gen_architecture.py` (READ)

**How it finds model modules today.** It uses neither a fixed list nor a recursive scan. `_model_modules()` (lines 57-78) reads the ImportFrom nodes of `models/__init__.py` and maps each module's last segment to `MODELS_PKG/<tail>.py`. It then asserts that every **top-level** `MODELS_PKG.glob("*.py")` file was covered.

**What happens after the move without a change (INFERRED):**
- `from grant_lab.db.models import workspace` gives tail `models`, so `models.py` is not found and the line is skipped silently.
- The non-recursive glob doesn't see `workspace/`, so all 31 tables vanish from the map with no assert firing.
- `tests/architecture/test_architecture_map_is_current.py` would then go red after regeneration. Its pinned rows (coherence_jobs, turn_jobs, claim_analysis_jobs, `ck_turn_jobs_status`) are all parked tables.

**Two more lines break when the package is deleted:**
- Line 24 `FENCING = …/proposal_assist/fencing.py` is read without a guard, so `render()` raises FileNotFoundError.
- Line 284 names it.
- `INVENTORY` (`tests/fencing/inventory.py`) is guarded by `exists()`.

**New file** (MEASURED: ruff clean). The parked tables are parsed from `PARKED_PKG` and marked "(parked)" in every section. Any other sub-package trips an assert. The fence and allowlist sections are deleted. **That deletion must land in the commit that deletes `proposal_assist/fencing.py`.** If the model move lands earlier, keep the `FENCING`/`INVENTORY` constants, `parse_fence_primitives`, `parse_gate_allowlists` and the fence section exactly as today until then.

```python
#!/usr/bin/env python3
"""Generate docs/ARCHITECTURE.md from the code.

Reads the source with ast — never imports the app — so it runs without a database,
without settings, and without any LLM SDK.

The point of this file is that the map cannot go stale: a pytest test regenerates it
and fails on any diff, the same way schema.gen.ts is pinned in CI.

Run:  uv run python scripts/gen_architecture.py            # writes the file
      uv run python scripts/gen_architecture.py --stdout   # prints it
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
MODELS_PKG = REPO / "src/grant_lab/db/models"
#: The proposal workspace's tables (spec 2026-11-03-workspace-removal-design.md §3).
#: Still in the database and still mapped, but nothing on master reads or writes
#: them. Parsed like the live ones and marked "(parked)" wherever a table is named.
PARKED_PKG = MODELS_PKG / "workspace"
CONFIG = REPO / "src/grant_lab/config.py"
OUT = REPO / "docs/ARCHITECTURE.md"


def _const(node: ast.AST) -> str | None:
    """The literal string a node evaluates to, if it is one. Handles text("...")."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Call) and node.args:
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
        if name == "text":
            return _const(node.args[0])
    return None


def _kwarg(call: ast.Call, key: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == key:
            return kw.value
    return None


class Table:
    def __init__(self, cls: str, table: str, line: int) -> None:
        self.cls, self.table, self.line = cls, table, line
        self.parked = False
        self.columns: list[tuple[str, str]] = []
        self.checks: list[tuple[str, str]] = []
        self.indexes: list[tuple[str, list[str], bool, str | None]] = []
        self.fks: list[tuple[str, str, str]] = []

    @property
    def label(self) -> str:
        return f"`{self.table}`" + (" (parked)" if self.parked else "")


def _model_modules(pkg: pathlib.Path) -> list[pathlib.Path]:
    """The model modules of *pkg*, in the order its `__init__.py` imports them.

    Ordered by that file rather than alphabetically so the generated document
    keeps following the package's own subsystem grouping, and so a module added
    to the package is picked up here without editing this script. Every .py in
    the package must be reachable this way -- an unimported module would be
    silently missing from ARCHITECTURE.md, which is the failure this assert
    exists to prevent.
    """
    init = (pkg / "__init__.py").read_text()
    order: list[pathlib.Path] = []
    for node in ast.parse(init).body:
        if isinstance(node, ast.ImportFrom) and node.module:
            _, _, tail = node.module.rpartition(".")
            path = pkg / f"{tail}.py"
            if path.is_file() and path not in order:
                order.append(path)
    found = {p for p in pkg.glob("*.py") if p.name != "__init__.py"}
    missing = sorted(p.name for p in found - set(order))
    assert not missing, f"model modules not imported by {pkg.name}/__init__.py: {missing}"
    return order


def parse_models(pkg: pathlib.Path = MODELS_PKG) -> list[Table]:
    out: list[Table] = []
    classes: list[ast.ClassDef] = []
    for path in _model_modules(pkg):
        classes += [n for n in ast.parse(path.read_text()).body if isinstance(n, ast.ClassDef)]
    for cls in classes:
        tname = None
        for stmt in cls.body:
            if (
                isinstance(stmt, ast.AnnAssign | ast.Assign)
                and "__tablename__" in ast.dump(stmt)
                and (v := _const(getattr(stmt, "value", None) or ast.Pass()))
            ):
                tname = v
        if not tname:
            continue
        t = Table(cls.name, tname, cls.lineno)

        for stmt in cls.body:
            # columns
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                nm = stmt.target.id
                if nm.startswith("__"):
                    continue
                ann = ast.unparse(stmt.annotation) if stmt.annotation else ""
                if not ann.startswith("Mapped"):
                    continue
                t.columns.append((nm, ann.removeprefix("Mapped[").removesuffix("]")))
                if isinstance(stmt.value, ast.Call):
                    for a in stmt.value.args:
                        if isinstance(a, ast.Call):
                            fn = getattr(a.func, "id", "") or getattr(a.func, "attr", "")
                            if fn == "ForeignKey" and a.args and (tgt := _const(a.args[0])):
                                od = _kwarg(a, "ondelete")
                                t.fks.append((nm, tgt, _const(od) if od else ""))
            # __table_args__
            if isinstance(stmt, ast.Assign) and any(
                getattr(tg, "id", "") == "__table_args__" for tg in stmt.targets
            ):
                for el in getattr(stmt.value, "elts", []):
                    if not isinstance(el, ast.Call):
                        continue
                    fn = getattr(el.func, "id", "") or getattr(el.func, "attr", "")
                    nmk = _kwarg(el, "name")
                    nm = _const(nmk) if nmk else ""
                    if fn == "CheckConstraint" and el.args:
                        t.checks.append((nm or "", _const(el.args[0]) or ast.unparse(el.args[0])))
                    elif fn == "Index":
                        parts = [_const(a) for a in el.args]
                        idx_name = parts[0] if parts else ""
                        cols = [p for p in parts[1:] if p]
                        uq = _kwarg(el, "unique")
                        where = _kwarg(el, "postgresql_where")
                        t.indexes.append(
                            (
                                idx_name or "",
                                cols,
                                bool(getattr(uq, "value", False)),
                                _const(where) if where else None,
                            )
                        )
        out.append(t)
    return out


def all_tables() -> list[Table]:
    """The live tables, then the parked ones, each marked.

    `_model_modules` reads one directory, so a model SUB-package is invisible to
    it. The parked package is parsed on its own; any other sub-package would drop
    out of the map silently, which the assert refuses.
    """
    subs = sorted(p.name for p in MODELS_PKG.iterdir() if (p / "__init__.py").is_file())
    assert subs == [PARKED_PKG.name], (
        f"model sub-packages {subs}: only {PARKED_PKG.name!r} is parsed, so any other "
        "would be missing from ARCHITECTURE.md"
    )
    parked = parse_models(PARKED_PKG)
    for t in parked:
        t.parked = True
    return parse_models(MODELS_PKG) + parked


def parse_caps() -> list[tuple[str, str]]:
    tree = ast.parse(CONFIG.read_text())
    out = []
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        for stmt in cls.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                nm = stmt.target.id
                if not any(k in nm for k in ("cap", "max_", "_limit", "budget")):
                    continue
                default = ""
                if isinstance(stmt.value, ast.Call) and stmt.value.args:
                    default = ast.unparse(stmt.value.args[0])
                elif stmt.value is not None:
                    default = ast.unparse(stmt.value)
                out.append((nm, default))
    return sorted(set(out))


def _is_status_like(nm: str | None, expr: str) -> bool:
    """A CHECK on a lifecycle column: `status` anywhere, or `state` as a whole
    word or after `_`. escalation_state yes; ck_collab_artifact_state_artifact
    (a table name) and a word like `estate` no."""
    text = f"{nm or ''} {expr}".lower()
    return "status" in text or re.search(r"(?<![a-z0-9])state\b", text) is not None


def render() -> str:
    tables = all_tables()
    L: list[str] = []
    A = L.append

    A("# Architecture map")
    A("")
    A("> **GENERATED — do not edit.** Produced by `scripts/gen_architecture.py` from the")
    A("> source, via `ast` (the app is never imported). A pytest test regenerates this")
    A("> file and fails on any diff, so it cannot go stale.")
    A("> Regenerate with `uv run python scripts/gen_architecture.py`.")
    A("")
    A("This map exists because two wrong recommendations were made from unmeasured")
    A("inference about facts that were not written down anywhere lookup-able.")
    A("")
    A("A table marked **(parked)** belongs to the proposal workspace, which left master")
    A("on 2026-11-03. It and its rows are still in the database and its definition is in")
    A("`src/grant_lab/db/models/workspace/`, but no code on master reads or writes it.")
    A("")

    # --- partial unique indexes: the section that would have caught the error
    A("## Active-uniqueness: every partial UNIQUE index and its exact predicate")
    A("")
    A("Read this before claiming two tables share, or do not share, a rule.")
    A("")
    A("| table | index | columns | predicate |")
    A("|---|---|---|---|")
    rows = []
    for t in tables:
        for nm, cols, uq, where in t.indexes:
            if uq and where:
                rows.append((t.table, nm, ", ".join(cols), where, t.label))
    for _tb, nm, cols, where, label in sorted(rows):
        A(f"| {label} | `{nm}` | {cols} | `{where}` |")
    A("")
    seen: dict[str, list[str]] = {}
    for tb, _nm, _c, w, _label in rows:
        seen.setdefault(w, []).append(tb)
    A(f"**{len(rows)} partial unique indexes across {len({r[0] for r in rows})} tables, "
      f"{len(seen)} distinct predicates.**")
    A("")
    for w, tbs in sorted(seen.items(), key=lambda kv: -len(kv[1])):
        A(f"- `{w}` — {len(tbs)} table(s): {', '.join(sorted(tbs))}")
    A("")

    # --- tables
    A("## Tables")
    A("")
    A(f"{len(tables)} mapped tables, {sum(t.parked for t in tables)} of them parked.")
    A("")
    A("| table | class | cols | FKs | CHECKs | indexes |")
    A("|---|---|---|---|---|---|")
    for t in sorted(tables, key=lambda x: x.table):
        A(f"| {t.label} | `{t.cls}` | {len(t.columns)} | {len(t.fks)} "
          f"| {len(t.checks)} | {len(t.indexes)} |")
    A("")

    A("### Status vocabularies")
    A("")
    A(
        "Every CHECK constraining a `status`- or `state`-like column. "
        "Divergence here is a real defect."
    )
    A("")
    A("| table | constraint | vocabulary |")
    A("|---|---|---|")
    for t in sorted(tables, key=lambda x: x.table):
        for nm, expr in t.checks:
            if _is_status_like(nm, expr):
                A(f"| {t.label} | `{nm}` | `{expr}` |")
    A("")

    A("### Ownership: what each table is keyed on")
    A("")
    A("| table | foreign keys (column → target, ON DELETE) |")
    A("|---|---|")
    for t in sorted(tables, key=lambda x: x.table):
        if t.fks:
            A(f"| {t.label} | "
              + "<br>".join(f"`{c}` → `{tg}`{f' ({od})' if od else ''}" for c, tg, od in t.fks)
              + " |")
    A("")

    # --- caps
    caps = parse_caps()
    A("## Caps, ceilings and limits")
    A("")
    A(f"{len(caps)} settings in `config.py`. A cap that exists in only one of a pair of")
    A("sibling paths is the shape of a real defect.")
    A("")
    A("| setting | default |")
    A("|---|---|")
    for nm, d in caps:
        A(f"| `{nm}` | `{d}` |")
    A("")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    text = render()
    if "--stdout" in sys.argv:
        sys.stdout.write(text)
    else:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(text)
        print(f"wrote {OUT.relative_to(REPO)} ({len(text.splitlines())} lines)")
```

- **Counts.** MEASURED by simulating the split with today's parser: 48 tables, 31 parked, 17 live. Nine of the 12 partial-unique rows are parked (attachment_jobs, claim_analysis_jobs, coherence_jobs, evaluator_jobs, references, review_runs, turn_jobs, uncited_claims_jobs, workspace_events).
- **Pinned tests.** The pins in `test_architecture_map_is_current.py` still hold (INFERRED): each row still contains the backticked table name, `uq_` and the predicate, and `### Status vocabularies` is still followed by `### Ownership`.
- **Regenerate** with `uv run python scripts/gen_architecture.py` after config.py's settings are trimmed. The Caps section depends on them.

### 1h. `alembic/env.py` (READ)

`env.py:6` does `from grant_lab.db.base import Base`. That executes `grant_lab/db/__init__.py:7`, `from grant_lab.db import models`, which imports `workspace`. `target_metadata = Base.metadata` (`env.py:19`).
- Today: importing `grant_lab.db.models` in a fresh interpreter registers exactly 48 tables, all 31 parked ones among them (MEASURED).
- After the move: same chain, so all 48 are still registered (INFERRED). Pinned by the new subprocess test in 4b.

---

## TASK 2: Freeze the migrations

### 2a. upgrade() today (READ)

**0037** (lines 34-59):
```python
def upgrade() -> None:
    try:
        from pathlib import Path
        from sqlalchemy import orm
        from grant_lab.proposal_assist.references.backfill import backfill_original_sha256
        references_root = Path(os.environ.get("WORKSPACES_DIR", "var/workspaces"))
        bind = op.get_bind()
        with orm.Session(bind=bind) as session:
            count = backfill_original_sha256(session, references_root)
            session.flush()
        log.info("0037 backfill: updated original_sha256 for %d reference(s)", count)
    except Exception:
        log.warning("0037 backfill: best-effort data migration failed — schema is unaffected", exc_info=True)
```

**0066** (lines 19-43): `op.add_column("workspaces", Column("current_branch", Text, nullable=False, server_default='main'))`, then:
```python
    import os
    from sqlalchemy.orm import Session
    from grant_lab.proposal_assist.branch_backfill import backfill_current_branch
    workspaces_dir = os.environ.get("WORKSPACES_DIR", "var/workspaces")
    with Session(bind=op.get_bind()) as s:
        backfill_current_branch(s, workspaces_dir=workspaces_dir)
        s.commit()
```

**0068** (lines 20-77): `create_table("branches", …)`, `create_index`, `add_column workspaces.root_branch_id`, `create_foreign_key fk_workspaces_root_branch_id RESTRICT`, then:
```python
    import os
    from sqlalchemy.orm import Session
    from grant_lab.proposal_assist.branch_entity_backfill import backfill_branch_entities
    workspaces_dir = os.environ.get("WORKSPACES_DIR", "var/workspaces")
    with Session(bind=op.get_bind()) as s:
        result = backfill_branch_entities(s, workspaces_dir=workspaces_dir)
        s.commit()
    print(f"0068: founded {result.created} branch rows, removed {result.deleted} orphans, set {result.rooted} root pointers, SKIPPED {result.skipped} unreadable repos, {result.absent} workspaces with no repo directory")
```

### 2b. What each backfill depends on (READ)

- **0037 → `references/backfill.py`.** Stdlib plus a Core `table("references", id, storage_path, original_sha256)`. No other PA code. `references/__init__.py` is empty.
- **0066 → `branch_backfill.py`.**
  - ORM `Workspace`, using `id` and `current_branch`, plus the implicit `updated_at` onupdate.
  - `citations/gitio.py`: `current_branch` → `_git` → `_git_env`, `_GIT_TIMEOUT_SECONDS`, `GitCommitError`.
  - `paths.workspace_repo_dir`.
- **0068 → `branch_entity_backfill.py`.**
  - ORM `Workspace`, using id, last_sdk_session_id, root_branch_id, current_branch and updated_at.
  - ORM `Branch`, using id, workspace_id, name and last_sdk_session_id.
  - `gitio`: `head_branch`, `list_branches` (+`_HEADS_PREFIX`), `branch_exists`, `_git`, `_git_env`, `GitCommitError`.
  - `paths.workspace_repo_dir`.
- **Import-time side effect.** Importing `gitio` runs `citations/__init__.py`, which imports `citations.service` → `grant_lab.config`, `citations.llm`, `citations.usage` and `turns.caps`. That is a large import closure, but none of it feeds the backfill's behaviour. `gitio` itself imports stdlib only.
- **Behaviour the copies must keep** (MEASURED: compiled the ORM statements):
  - 0066's `update(Workspace).values(current_branch=…)` compiles to `UPDATE workspaces SET updated_at=now(), current_branch=…`, because the model's `onupdate` fires. The frozen copy writes `updated_at=sa.func.now()` explicitly.
  - 0068 names `updated_at=Workspace.updated_at` only to suppress that `onupdate`. A lightweight `sa.table()` has no `onupdate`, so leaving `updated_at` out gives the same row.
  - The Core DELETE and UPDATE statements of the frozen 0068 compile to SQL identical to the ORM originals (MEASURED).
- **ORM-free copies are practical for all three.** All three below use only stdlib, `sqlalchemy` Core and `alembic.op`.

### 2c. Frozen `alembic/versions/0037_backfill_reference_original_sha256.py` (full; MEASURED: ruff clean. `op.get_bind()` was later moved inside the `try`.)

```python
"""Backfill references.original_sha256 for rows ingested before migration 0036.

Revision ID: 0037
Revises: 0036
Create Date: 2026-06-06

Best-effort data migration: for every reference whose original_sha256 is NULL,
resolve the stored file from disk and write its SHA-256 hex digest.  Rows whose
file is absent or unreadable are skipped silently — the freeze anchor simply
remains unavailable for them.

The data pass NEVER hard-fails the upgrade: if the filesystem root is absent, the
application is running in an environment without file storage (CI, minimal
setups), or any other unexpected error occurs in the pass, it is logged and the
upgrade completes with the hashes it managed. Only the data pass is wrapped.

FROZEN COPY. `_backfill_original_sha256` below is
`grant_lab.proposal_assist.references.backfill.backfill_original_sha256` as it
stood at 388494c8, copied in on 2026-11-03 when the proposal workspace left master
(docs/superpowers/specs/2026-11-03-workspace-removal-design.md §3). The original
is on the `workspace` branch and the `workspace-v1` tag. A migration must keep
running after the code it once imported is gone, so this file imports nothing
from `grant_lab`. The one change: it takes the migration's Connection instead of
an ORM Session wrapped around that same Connection. The SQL is the same.
Tests: tests/test_migration_0037_backfill.py.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op

# revision identifiers, used by Alembic.
revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

log = logging.getLogger(__name__)

# A lightweight Core table naming only the columns this backfill touches, never
# an ORM model: selecting a full entity would emit `SELECT <every model column>`
# and fail with "column does not exist" on a database at 0037 the moment the
# model grew a later column (e.g. 0038's tombstoned_at).
_references = sa.table(
    "references",
    sa.column("id"),
    sa.column("storage_path"),
    sa.column("original_sha256"),
)


def _backfill_original_sha256(conn: Connection, references_root: Path | str) -> int:
    """Fill original_sha256 for references where it IS NULL and the stored
    original file exists under references_root. Returns the count updated.
    Does NOT commit. Never raises on a single missing/unreadable file (skips it).

    ``references_root`` is the app's ``WORKSPACES_DIR`` (default
    ``var/workspaces``). ``storage_path`` is stored relative to it as
    ``<workspace_id>/references/<reference_id>/original<suffix>``. A path that
    resolves outside the root is skipped, never read.
    """
    root_resolved = Path(references_root).resolve()
    stmt = (
        _references.select()
        .with_only_columns(_references.c.id, _references.c.storage_path)
        .where(_references.c.original_sha256.is_(None))
    )
    # Materialises all NULL rows at once — acceptable for a one-time backfill.
    rows = conn.execute(stmt).all()

    count = 0
    for ref_id, storage_path in rows:
        path = (root_resolved / storage_path).resolve()
        if not path.is_relative_to(root_resolved):
            log.warning(
                "backfill: skipping reference %s: storage_path escapes references_root",
                ref_id,
            )
            continue
        if not path.is_file():
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except Exception:
            log.warning(
                "backfill: could not read %s for reference %s — skipping",
                path,
                ref_id,
            )
            continue
        conn.execute(
            sa.update(_references).where(_references.c.id == ref_id).values(original_sha256=digest)
        )
        count += 1

    return count


def upgrade() -> None:
    # Resolve the workspaces root using the same env var the app read.
    references_root = Path(os.environ.get("WORKSPACES_DIR", "var/workspaces"))
    # Best-effort, and only the data pass is: nothing outside this block can fail
    # on an import any more, because the code it runs is in this file.
    try:
        count = _backfill_original_sha256(op.get_bind(), references_root)
    except Exception:
        log.warning(
            "0037 backfill: best-effort data migration failed — schema is unaffected",
            exc_info=True,
        )
        return
    log.info("0037 backfill: updated original_sha256 for %d reference(s)", count)


def downgrade() -> None:
    # Deliberate no-op: we do not null-out hashes that were successfully computed.
    pass
```

### Frozen `alembic/versions/0066_workspace_current_branch.py` (full; MEASURED: ruff clean)

```python
"""workspaces: current_branch, the source of truth for which branch a workspace is on

Revision ID: 0066
Revises: 0065

FROZEN COPY. `_backfill_current_branch` and its helpers below are
`grant_lab.proposal_assist.branch_backfill.backfill_current_branch` plus what it
called (`citations/gitio.py`: `current_branch`, `_git`, `_git_env`,
`GitCommitError`; `paths.py`: `workspace_repo_dir`) as they stood at 388494c8,
copied in on 2026-11-03 when the proposal workspace left master
(docs/superpowers/specs/2026-11-03-workspace-removal-design.md §3). The originals
are on the `workspace` branch and the `workspace-v1` tag. A migration must keep
running after the code it once imported is gone, so this file imports nothing
from `grant_lab`.

Changes of form only. The ORM `Workspace` became a lightweight `sa.table()`, so
no later model change can alter what this migration does. The ORM UPDATE also
set `updated_at = now()` (the model's `onupdate`); that is written out below, so
the rows come out as they did. Tests: tests/migrations/test_0066_backfill.py.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.engine import Connection

from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None

log = logging.getLogger(__name__)

# Ceiling on any single git subprocess (gitio._GIT_TIMEOUT_SECONDS).
_GIT_TIMEOUT_SECONDS = 30.0

_workspaces = sa.table(
    "workspaces",
    sa.column("id", UUID(as_uuid=True)),
    sa.column("current_branch", sa.Text()),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)


class GitCommitError(RuntimeError):
    """A git read failed (frozen copy of gitio.GitCommitError)."""


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command in *repo_dir*. Never raises on a non-zero exit.

    The environment is gitio's `_git_env(repo_dir)`: no global or system config,
    no prompt, and repository DISCOVERY fenced at the repo's parent. Without that
    ceiling, a repo whose `.git` is corrupt answers with whatever repository
    encloses it, with exit 0. The ceiling must be absolute: git ignores a relative
    entry, and WORKSPACES_DIR defaults to the relative "var/workspaces". (gitio
    also defaulted GIT_AUTHOR_*/GIT_COMMITTER_*; no read below uses them.)
    """
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CEILING_DIRECTORIES"] = str(Path(repo_dir).resolve().parent)
    return subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        env=env,
    )


def _current_branch(repo_dir: Path) -> str:
    """The checked-out branch name.

    Succeeds on an UNBORN HEAD (a repo with no commits): git still knows which
    branch the first commit will land on, and that name is the right answer.
    Raises GitCommitError on a DETACHED HEAD, where there is no branch name and
    no safe default.
    """
    proc = _git(repo_dir, "symbolic-ref", "--short", "HEAD")
    if proc.returncode != 0:
        raise GitCommitError(f"HEAD is not on a branch in {repo_dir}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _backfill_current_branch(conn: Connection, *, workspaces_dir: Path | str) -> int:
    """Set `current_branch` from `git symbolic-ref --short HEAD` per repo.

    Returns the number of rows changed. Does NOT commit.

    A workspace with no repo directory keeps the column default: there is no
    repo to read and nothing has happened in it yet, so 'main' is not a guess.
    A DETACHED HEAD raises, failing the migration: nothing in the app detached
    HEAD, so a detached repo means something outside the app did, and writing a
    plausible 'main' would discard that state.
    """
    changed = 0
    for (wid,) in conn.execute(sa.select(_workspaces.c.id)).all():
        # <WORKSPACES_DIR>/<workspace_id>/repo (paths.workspace_repo_dir)
        repo = Path(workspaces_dir) / str(wid) / "repo"
        if not (repo / ".git").exists():
            continue
        name = _current_branch(repo)  # raises GitCommitError on a detached HEAD
        conn.execute(
            sa.update(_workspaces)
            .where(_workspaces.c.id == wid)
            # updated_at: what the ORM UPDATE added from Workspace's onupdate.
            .values(current_branch=name, updated_at=sa.func.now())
        )
        changed += 1
        log.info(
            "branches.backfill",
            extra={"event": "branch_backfill", "workspace_id": str(wid), "branch": name},
        )
    return changed


def upgrade() -> None:
    # server_default so existing rows land somewhere valid before the backfill;
    # NOT NULL so no code path has to consider a null branch.
    op.add_column(
        "workspaces",
        sa.Column(
            "current_branch",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'main'"),
        ),
    )
    # Then correct it from each repo. The default is right for NEW rows only:
    # backfilling every existing row to 'main' is a guess, and a wrong guess
    # makes §4.1's reconciliation check out a branch that may not exist.
    workspaces_dir = os.environ.get("WORKSPACES_DIR", "var/workspaces")
    _backfill_current_branch(op.get_bind(), workspaces_dir=workspaces_dir)


def downgrade() -> None:
    op.drop_column("workspaces", "current_branch")
```

### Frozen `alembic/versions/0068_branch_entity.py` (full; MEASURED: ruff clean)

```python
"""branches — a branch is a row with an immutable id, and the workspace names its root

Revision ID: 0068
Revises: 0067

FROZEN COPY. `_backfill_branch_entities` and its helpers below are
`grant_lab.proposal_assist.branch_entity_backfill.backfill_branch_entities` plus
what it called (`citations/gitio.py`: `head_branch`, `list_branches`,
`branch_exists`, `_git`, `_git_env`, `GitCommitError`; `paths.py`:
`workspace_repo_dir`) as they stood at 388494c8, copied in on 2026-11-03 when the
proposal workspace left master
(docs/superpowers/specs/2026-11-03-workspace-removal-design.md §3). The originals
are on the `workspace` branch and the `workspace-v1` tag. A migration must keep
running after the code it once imported is gone, so this file imports nothing
from `grant_lab`.

Changes of form only:
  * the ORM `Workspace` and `Branch` became `sa.table()`s naming the columns the
    schema had at 0068, and `session.add(Branch(...))` became a Core insert with
    the same `uuid4()` id;
  * `updated_at` is never written. The original named it with its own value to
    suppress the model's `onupdate`; a lightweight table has no `onupdate`;
  * the `workspace_id=` narrowing is gone: only Phase 0 passed it, never this
    migration.
Tests: tests/migrations/test_0068_backfill.py.
"""

from __future__ import annotations

import logging
import os
import subprocess
import uuid
from pathlib import Path
from typing import NamedTuple

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.engine import Connection

from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None

log = logging.getLogger(__name__)

# Ceiling on any single git subprocess (gitio._GIT_TIMEOUT_SECONDS).
_GIT_TIMEOUT_SECONDS = 30.0
#: The prefix `%(refname)` puts on every local branch.
_HEADS_PREFIX = "refs/heads/"

_workspaces = sa.table(
    "workspaces",
    sa.column("id", UUID(as_uuid=True)),
    sa.column("current_branch", sa.Text()),
    sa.column("last_sdk_session_id", sa.Text()),
    sa.column("root_branch_id", UUID(as_uuid=True)),
)
_branches = sa.table(
    "branches",
    sa.column("id", UUID(as_uuid=True)),
    sa.column("workspace_id", UUID(as_uuid=True)),
    sa.column("name", sa.Text()),
    sa.column("last_sdk_session_id", sa.Text()),
)


class GitCommitError(RuntimeError):
    """A git read failed (frozen copy of gitio.GitCommitError)."""


class BackfillResult(NamedTuple):
    """Counts, so the caller can say what happened rather than assume it did."""

    created: int
    deleted: int
    rooted: int
    skipped: int
    #: Workspaces with no repo directory at all. Correct for one still in setup —
    #: but if `WORKSPACES_DIR` is unset, resolved against the wrong cwd, or the
    #: volume is not mounted, EVERY workspace looks like that and the run reports
    #: a clean zero. Counted so "0 of 0" and "0 of 500" cannot print identically.
    absent: int


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command in *repo_dir*. Never raises on a non-zero exit —
    callers decide what a failure means, because for some (`_branch_exists`) a
    non-zero exit IS the answer.

    The environment is gitio's `_git_env(repo_dir)`: no global or system config,
    no prompt, and repository DISCOVERY fenced at the repo's parent. Without that
    ceiling, a repo whose `.git` is corrupt answers with whatever repository
    encloses it, with exit 0 — and the reconciler would found rows named after
    the server's own branches and delete the workspace's real ones. The ceiling
    must be absolute: git ignores a relative entry. (gitio also defaulted
    GIT_AUTHOR_*/GIT_COMMITTER_*; no read below uses them.)
    """
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CEILING_DIRECTORIES"] = str(Path(repo_dir).resolve().parent)
    return subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        env=env,
    )


def _head_branch(repo_dir: Path) -> str | None:
    """The checked-out branch name, or None when HEAD is DETACHED.

    `-q` separates the cases (measured 2026-08-31): attached -> 0 and the name;
    detached -> 1; not a repository -> 128; unborn HEAD -> 0 and the name the
    first commit will land on. Only exit 1 means detached — collapsing 128 into
    it would report a broken repo as a normal state and clear its root pointer.
    """
    proc = _git(repo_dir, "symbolic-ref", "--short", "-q", "HEAD")
    if proc.returncode == 1:
        return None
    if proc.returncode != 0:
        raise GitCommitError(f"cannot read HEAD in {repo_dir}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _list_branches(repo_dir: Path) -> list[str]:
    """Every local branch, in git's own order. Empty only for a repo with no refs.

    `%(refname)` and strip, NOT `%(refname:short)`: the short form depends on what
    other refs exist (a tag named like a branch makes it `heads/feature`).

    A corrupt ref file makes `git branch` exit ZERO, warn "ignoring broken ref" on
    stderr and OMIT that branch. Treating that short list as complete would delete
    the omitted branch's row as an orphan, so it raises instead, and the caller
    skips the whole workspace.
    """
    proc = _git(repo_dir, "branch", "--format=%(refname)")
    if proc.returncode != 0:
        raise GitCommitError(f"cannot list branches in {repo_dir}: {proc.stderr.strip()}")
    if "broken ref" in proc.stderr:
        raise GitCommitError(f"broken ref in {repo_dir}: {proc.stderr.strip()}")
    return [ln.strip().removeprefix(_HEADS_PREFIX) for ln in proc.stdout.splitlines() if ln.strip()]


def _branch_exists(repo_dir: Path, name: str) -> bool:
    """Whether a LOCAL branch of this name exists (full refs/heads/ path, so a tag
    sharing the name cannot answer yes). Only exit 1 means "no such branch"; 128
    (not a repository, corruption, permissions) raises."""
    proc = _git(repo_dir, "show-ref", "--verify", "--quiet", f"refs/heads/{name}")
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    raise GitCommitError(
        f"cannot read refs in {repo_dir} (git exit {proc.returncode}): {proc.stderr.strip()}"
    )


def _real_refs(repo: Path) -> list[str]:
    """The local branch names, with git's non-ref output filtered out.

    On a detached HEAD `git branch` emits a pseudo-entry such as
    `(HEAD detached at 7ee4363)` with no `refs/heads/` prefix. `_branch_exists`
    answers no for it, so no row is founded from it.
    """
    return [name for name in _list_branches(repo) if _branch_exists(repo, name)]


def _backfill_branch_entities(conn: Connection, *, workspaces_dir: Path | str) -> BackfillResult:
    """Make `branches` agree with each workspace's local refs, and set the root
    pointer. Returns counts. Does NOT commit.

    * Every local ref gets a row, not just the current one.
    * Rows whose ref is gone are deleted — but only for a workspace whose refs were
      read successfully; an unreadable repo skips the whole workspace.
    * The root pointer is FOUNDED from git HEAD, never MOVED: it is assigned only
      when VACANT (never set, or naming a row whose ref just vanished). A detached
      HEAD leaves it alone.
    * `last_sdk_session_id` follows `workspaces.current_branch`, not HEAD, and
      lands on at most one branch. The workspace's own column is left untouched.
    * Idempotent. Skipped without raising: no repo directory (`absent`), or a repo
      git cannot read (`skipped`).
    """
    created = deleted = rooted = skipped = absent = 0
    rows = conn.execute(sa.select(_workspaces.c.id, _workspaces.c.last_sdk_session_id)).all()
    for wid, sdk_session_id in rows:
        # <WORKSPACES_DIR>/<workspace_id>/repo (paths.workspace_repo_dir)
        repo = Path(workspaces_dir) / str(wid) / "repo"
        try:
            has_git = (repo / ".git").exists()
        except OSError:
            # `Path.exists()` RAISES on EACCES. Unreadable is `skipped`, not
            # `absent`: the repo may well be there.
            skipped += 1
            log.warning(
                "branches.entity_reconcile_skipped",
                extra={"event": "branch_entity_reconcile_skipped", "workspace_id": str(wid)},
                exc_info=True,
            )
            continue
        if not has_git:
            absent += 1
            continue
        # BOTH git reads happen before anything is written for this workspace,
        # and a failure in either skips it. `_head_branch`, not a current-branch
        # read that raises for detached and broken alike: a broken repo must not
        # look detached, because detached CLEARS the root pointer.
        try:
            names = _real_refs(repo)
            head = _head_branch(repo)  # None ONLY for a genuinely detached HEAD
        except (GitCommitError, OSError, subprocess.TimeoutExpired):
            skipped += 1
            log.warning(
                "branches.entity_reconcile_skipped",
                extra={"event": "branch_entity_reconcile_skipped", "workspace_id": str(wid)},
                exc_info=True,
            )
            continue
        # No early return on an empty ref set: a repo whose last ref was deleted
        # under a detached HEAD still has rows and a pointer to clear.

        existing = {
            n
            for (n,) in conn.execute(
                sa.select(_branches.c.name).where(_branches.c.workspace_id == wid)
            ).all()
        }
        for name in names:
            if name in existing:
                continue
            conn.execute(sa.insert(_branches).values(id=uuid.uuid4(), workspace_id=wid, name=name))
            created += 1
        orphans = existing - set(names)

        # THE POINTER MOVES BEFORE THE DELETE, and that order is load-bearing:
        # fk_workspaces_root_branch_id is ON DELETE RESTRICT, so deleting the row
        # the workspace still points at would abort the whole batch.
        rooted_at = conn.execute(
            sa.select(_workspaces.c.root_branch_id).where(_workspaces.c.id == wid)
        ).scalar_one()
        held_name = (
            conn.execute(
                sa.select(_branches.c.name).where(_branches.c.id == rooted_at)
            ).scalar_one_or_none()
            if rooted_at is not None
            else None
        )
        vacant = held_name is None or held_name in orphans

        if vacant and head in names:
            root_id = conn.execute(
                sa.select(_branches.c.id).where(
                    _branches.c.workspace_id == wid, _branches.c.name == head
                )
            ).scalar_one()
            conn.execute(
                sa.update(_workspaces)
                .where(_workspaces.c.id == wid)
                .values(root_branch_id=root_id)
            )
            rooted += 1
        elif vacant and rooted_at is not None:
            # The old root's ref is gone and no new root is knowable (detached
            # HEAD). Clearing is the honest answer; promoting a survivor is not
            # this function's decision to make.
            conn.execute(
                sa.update(_workspaces).where(_workspaces.c.id == wid).values(root_branch_id=None)
            )

        if orphans:
            conn.execute(
                sa.delete(_branches).where(
                    _branches.c.workspace_id == wid, _branches.c.name.in_(sorted(orphans))
                )
            )
            deleted += len(orphans)

        # The SDK handle: ONE hosted conversation, so at most one row may hold it.
        # Clear the others first. `is_distinct_from`, not `!=`: with a NULL
        # current_branch, `name != NULL` is NULL and would clear nothing.
        current = conn.execute(
            sa.select(_workspaces.c.current_branch).where(_workspaces.c.id == wid)
        ).scalar_one()
        conn.execute(
            sa.update(_branches)
            .where(_branches.c.workspace_id == wid, _branches.c.name.is_distinct_from(current))
            .values(last_sdk_session_id=None)
        )
        if current in names:
            conn.execute(
                sa.update(_branches)
                .where(_branches.c.workspace_id == wid, _branches.c.name == current)
                .values(last_sdk_session_id=sdk_session_id)
            )

        log.info(
            "branches.entity_reconciled",
            extra={
                "event": "branch_entity_reconciled",
                "workspace_id": str(wid),
                "refs": len(names),
                "head": head,
            },
        )
    return BackfillResult(
        created=created, deleted=deleted, rooted=rooted, skipped=skipped, absent=absent
    )


def upgrade() -> None:
    op.create_table(
        "branches",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_sdk_session_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_branches_workspace_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("workspace_id", "name", name="uq_branches_ws_name"),
    )
    op.create_index("ix_branches_workspace_id", "branches", ["workspace_id"])

    # The other half of the cycle, added as a separate ALTER — the same shape the
    # model declares with use_alter=True.
    op.add_column("workspaces", sa.Column("root_branch_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_workspaces_root_branch_id",
        "workspaces",
        "branches",
        ["root_branch_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # Then found the branches from each repo. `branches` and `root_branch_id`
    # both exist by this point.
    workspaces_dir = os.environ.get("WORKSPACES_DIR", "var/workspaces")
    result = _backfill_branch_entities(op.get_bind(), workspaces_dir=workspaces_dir)
    # Say what happened. A workspace whose repo could not be read is left with no
    # rows and no root pointer, and the migration still succeeds — that is the
    # right trade (one bad repo must not block a deploy) but it must not be
    # silent, or "the migration succeeded" reads as "every workspace was founded".
    print(
        f"0068: founded {result.created} branch rows, removed {result.deleted} orphans, "
        f"set {result.rooted} root pointers, SKIPPED {result.skipped} unreadable repos, "
        f"{result.absent} workspaces with no repo directory"
    )
    # `absent` is the one to read on a deploy. A workspace still in setup has no
    # repo and is correctly skipped — but if WORKSPACES_DIR is unset, resolved
    # against the wrong cwd, or the volume is not mounted here, EVERY workspace
    # looks that way and every other counter prints a clean zero. `absent` equal
    # to the workspace count means this migration founded nothing and nobody was
    # told.


def downgrade() -> None:
    op.drop_constraint("fk_workspaces_root_branch_id", "workspaces", type_="foreignkey")
    op.drop_column("workspaces", "root_branch_id")
    op.drop_index("ix_branches_workspace_id", table_name="branches")
    op.drop_table("branches")
```

### 2d. Transaction semantics (INFERRED)

- **Same transaction as before.** Today's `Session(bind=op.get_bind())` joins Alembic's open transaction. Under SA 2.0's default `join_transaction_mode="conservative_savepoint"` that becomes "rollback_only", so `s.commit()` does not commit Alembic's transaction. Calling the frozen copy on `op.get_bind()` directly runs in the same transaction.
- **0037's try/except is no safer than before.** A database error inside it aborts Alembic's single transaction, and 0038 then fails with InFailedSqlTransaction. That was true before the change too. Only filesystem and Python errors are truly best-effort.

---

## TASK 3: The backfill tests

### 3a. Which test covers what (READ)

- **`tests/test_migration_0037_backfill.py` covers 0037.**
  - It imports `backfill_original_sha256` from PA and calls it with an ORM session.
  - `test_0037_round_trip` runs downgrade 0036 → upgrade head on an **empty** database, with no data.
  - Fixtures: `pg_session_factory`, `tmp_path`, `monkeypatch`, `migrated_engine`, `pg_url` (from `tests/conftest.py`); `make_user` (:204) and `make_workspace` (:234) from `tests/proposal_assist/conftest.py`.
  - Seeds User + Workspace + Reference rows with NULL hash, and files at `tmp_path/<ws>/references/<uuid>/original.pdf`.
  - **Weakness found:** the escape test's `../../etc/passwd` resolves to a file that does not exist under pytest's tmp tree, so `is_file()` fails first. Deleting the escape check would leave it green (INFERRED).
- **`tests/proposal_assist/test_branch_column_migration.py` covers 0066** (4 tests).
  - It calls PA's `backfill_current_branch`, `gitio.GitCommitError` and `paths.workspace_repo_dir` directly.
  - Fixtures: `pg_session_factory`, `tmp_path`; `make_workspace` from the PA conftest.
  - Seeds Workspace rows and git repos in `tmp_path` (committed, unborn, none, detached).
  - The unborn case uses branch `main`, which equals the default, so it can't tell a real read from a no-op.
- **`tests/proposal_assist/test_branch_entity_backfill.py` covers 0068** (1136 lines).
  - It calls PA's `backfill_branch_entities`. It also uses PA's `delete_workspace_branch`, `BranchInUse`, `branch_exists`, and `stamp_for` (PA conftest :292, which uses `ports.stamp`).
  - It has two AST tests over the 0068 file and one AST test over the PA source.
  - Fixtures: `pg_session_factory`, `tmp_path`, `monkeypatch`; `make_user`, `make_workspace`, `stamp_for`.
- **`tests/proposal_assist/test_phase0_transaction_boundaries.py`** is not a migration test. It drives PA `phases/setup._m1`, which calls `backfill_branch_entities(workspace_id=…)` (:332-430, :1358). It is deleted with the folder.
- **`tests/architecture/test_branch_entity_invariants.py`** tests S2 invariants with PA's `create_workspace_branch`. Delete it. Its one backfill-only property (a root never names another workspace's branch) is ported below.
- **`tests/test_migration_0038_reference_tombstone.py`** is a schema round trip only and needs no change.
- **Autouse fixtures in play:** `_hermetic_git_env` (conftest :51) and `_workspaces_dir_is_never_the_real_one` (:209).

### 3b. How the new tests load the frozen copies

Two ways, following the precedent in `tests/test_migration_0082_*`:
1. Load the migration file as a module with `importlib`, then call `M._backfill_*(session.connection(), …)` on rows and files under `tmp_path`.
2. One end-to-end test per migration, following `tests/test_migration_0064_*`: `downgrade` to the revision before, seed raw SQL rows there, create real files or repos, `monkeypatch.setenv("WORKSPACES_DIR", tmp_path)`, `upgrade` to the revision, read back, and restore `head` in a `finally`. This is the only thing that can catch a broken 0037, whose `upgrade()` swallows errors.

**File placement.**
- New: `tests/migrations/__init__.py` (empty; makes the module names `tests.migrations.*`, the same convention as `tests/proposal_assist/`).
- New: `tests/migrations/support.py`, `tests/migrations/test_0066_backfill.py`, `tests/migrations/test_0068_backfill.py`, `tests/migrations/test_frozen_migrations_import_no_app_code.py`.
- Rewritten: `tests/test_migration_0037_backfill.py`.
- Delete `tests/proposal_assist/test_branch_column_migration.py` and `test_branch_entity_backfill.py`; they go with the folder anyway.

**If you land these before the model move,** write `from grant_lab.db.models import …` instead of `…models.workspace import …` in four places: `support.py` (`Workspace`), the 0037 test (`Reference`), the 0066 test (`Workspace`) and the 0068 test (`Branch, Workspace`). Switch them in the move commit.

#### `tests/migrations/support.py` (MEASURED: ruff clean)
```python
"""Shared pieces for the migration backfill tests (0037, 0066, 0068, 0083).

The workspace tables are parked (src/grant_lab/db/models/workspace/), so nothing
in the app builds their rows any more. `make_user` and `make_workspace` replace
the helpers of the same names that the deleted tests/proposal_assist/conftest.py
provided, cut down to what these tests use.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

from alembic.config import Config

from grant_lab.db.models import User
from grant_lab.db.models.workspace import Workspace

REPO = Path(__file__).resolve().parents[2]
VERSIONS = REPO / "alembic" / "versions"

#: Identity for the test-side `git commit`. tests/conftest.py's
#: `_hermetic_git_env` removes the ambient one on purpose.
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": "/usr/bin:/bin",
}


def load_migration(filename: str) -> ModuleType:
    """Execute one file in alembic/versions as a module, so the frozen backfill in
    it can be called directly. Same pattern as tests/test_migration_0082_*."""
    path = VERSIONS / filename
    spec = importlib.util.spec_from_file_location(f"_migration_{path.stem}", path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def alembic_cfg(pg_url: str) -> Config:
    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def make_user(session, *, email: str = "owner@umu.se") -> User:
    """A User row. The default email is a fixed literal, so a second call in one
    session must pass its own `email` or it collides on uq_users_email."""
    user = User(status="active", email=email)
    session.add(user)
    session.flush()
    return user


def make_workspace(
    session, *, owner: User | None = None, title: str = "Test workspace"
) -> Workspace:
    """A Workspace row owned by *owner*, or by a fresh `make_user()` when none is
    given (so two calls without `owner=` in one session collide on the email)."""
    if owner is None:
        owner = make_user(session)
    ws = Workspace(title=title, owner_id=owner.id, current_phase="active")
    session.add(ws)
    session.flush()
    return ws


def repo_dir(workspaces_dir, workspace_id) -> Path:
    """`<WORKSPACES_DIR>/<workspace_id>/repo`, the layout the app used.

    Written out here, not taken from the migration under test, so a frozen copy
    that looks somewhere else goes red."""
    return Path(workspaces_dir) / str(workspace_id) / "repo"


def git_repo(workspaces_dir, workspace_id, head, *, extra=(), commit=True, detach=False) -> Path:
    """A workspace repo on branch `head`, plus one ref per name in `extra`."""
    d = repo_dir(workspaces_dir, workspace_id)
    d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", head, "."], cwd=d, check=True)
    if commit:
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "x"], cwd=d, check=True, env=GIT_ENV
        )
        for name in extra:
            subprocess.run(["git", "branch", name], cwd=d, check=True)
    if detach:
        subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=d, check=True)
    return d
```
(`alembic.ini` has `script_location = %(here)s/alembic` (READ), so the absolute ini path is safe.)

#### `tests/test_migration_0037_backfill.py` (full replacement; MEASURED: ruff clean)
```python
"""Migration 0037: best-effort backfill of references.original_sha256.

Two groups:
1. The frozen backfill, called directly against the real database with rows and
   files under tmp_path. It is loaded from the migration file, because that is
   where the code now lives: the package it was copied from left master on
   2026-11-03.
2. The migration end to end: downgrade to 0036, seed references that predate the
   backfill, write their files, upgrade to 0037, read the hashes back. This is the
   only test that can see a broken 0037. upgrade() logs and swallows any error in
   the data pass, so the suite's `alembic upgrade head` on an empty database
   passes whether the backfill works or not.

pg_url / migrated_engine are the SHARED session-scoped container; the end-to-end
test restores head in a finally block.
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import create_engine, inspect, select, text

from alembic import command
from grant_lab.db.models.workspace import Reference
from tests.migrations.support import alembic_cfg, load_migration, make_workspace

M = load_migration("0037_backfill_reference_original_sha256.py")


def _make_ref(session, workspace, *, storage_path: str | None = None) -> Reference:
    ref = Reference(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        kind="paper",
        source_type="uploaded",
        filename="original.pdf",
        storage_path=storage_path or f"{workspace.id}/references/{uuid.uuid4()}/original.pdf",
        mime_type="application/pdf",
        bytes_total=512,
        uploaded_by=workspace.owner_id,
        original_sha256=None,  # explicitly pre-0036
    )
    session.add(ref)
    session.flush()
    return ref


def _sha_of(session_factory, ref_id):
    with session_factory() as s:
        return s.scalar(select(Reference.original_sha256).where(Reference.id == ref_id))


# ---------------------------------------------------------------------------
# 1. The frozen backfill
# ---------------------------------------------------------------------------


def test_backfill_fills_present_skips_absent(pg_session_factory, tmp_path):
    """Ref A has its file on disk -> hashed. Ref B has no file -> stays NULL."""
    file_bytes = b"hello world pdf content"

    with pg_session_factory() as session:
        ws = make_workspace(session)
        ref_a = _make_ref(session, ws)
        ref_b = _make_ref(session, ws)
        session.commit()
        full_path = tmp_path / ref_a.storage_path
        full_path.parent.mkdir(parents=True)
        full_path.write_bytes(file_bytes)

        count = M._backfill_original_sha256(session.connection(), tmp_path)
        session.commit()

    assert count == 1
    assert _sha_of(pg_session_factory, ref_a.id) == hashlib.sha256(file_bytes).hexdigest()
    assert _sha_of(pg_session_factory, ref_b.id) is None


def test_each_row_gets_the_hash_of_its_own_file(pg_session_factory, tmp_path):
    """Two files, two different hashes. A copy that hashed one file for every row,
    or mixed rows up, passes the single-file test above."""
    with pg_session_factory() as session:
        ws = make_workspace(session)
        refs = [_make_ref(session, ws), _make_ref(session, ws)]
        session.commit()
        for i, ref in enumerate(refs):
            p = tmp_path / ref.storage_path
            p.parent.mkdir(parents=True)
            p.write_bytes(f"paper number {i}".encode())

        assert M._backfill_original_sha256(session.connection(), tmp_path) == 2
        session.commit()

    for i, ref in enumerate(refs):
        expected = hashlib.sha256(f"paper number {i}".encode()).hexdigest()
        assert _sha_of(pg_session_factory, ref.id) == expected


def test_backfill_idempotent(pg_session_factory, tmp_path):
    """Second call on already-filled rows returns 0 and doesn't alter the hash."""
    file_bytes = b"idempotent test content"
    expected_hash = hashlib.sha256(file_bytes).hexdigest()

    with pg_session_factory() as session:
        ws = make_workspace(session)
        ref = _make_ref(session, ws)
        session.commit()
        full_path = tmp_path / ref.storage_path
        full_path.parent.mkdir(parents=True)
        full_path.write_bytes(file_bytes)

        count1 = M._backfill_original_sha256(session.connection(), tmp_path)
        session.commit()
    assert count1 == 1
    assert _sha_of(pg_session_factory, ref.id) == expected_hash

    full_path.write_bytes(b"the file changed since")  # must not be re-read
    with pg_session_factory() as session:
        count2 = M._backfill_original_sha256(session.connection(), tmp_path)
        session.commit()
    assert count2 == 0
    assert _sha_of(pg_session_factory, ref.id) == expected_hash


def test_backfill_returns_zero_when_nothing_to_do(pg_session_factory, tmp_path):
    with pg_session_factory() as session:
        assert M._backfill_original_sha256(session.connection(), tmp_path) == 0


def test_backfill_skips_unreadable_file(pg_session_factory, tmp_path, monkeypatch):
    """A read that raises skips the row; the function still returns normally."""
    with pg_session_factory() as session:
        ws = make_workspace(session)
        ref = _make_ref(session, ws)
        session.commit()
        full_path = tmp_path / ref.storage_path
        full_path.parent.mkdir(parents=True)
        full_path.write_bytes(b"content")  # exists, so is_file() passes

        def _boom(self):
            raise OSError("simulated read error")

        monkeypatch.setattr(M.Path, "read_bytes", _boom)
        count = M._backfill_original_sha256(session.connection(), tmp_path)
        session.commit()

    assert count == 0
    assert _sha_of(pg_session_factory, ref.id) is None


def test_backfill_skips_escaping_storage_path(pg_session_factory, tmp_path):
    """A storage_path that resolves outside the root is skipped and its file is
    never read. The file it points at EXISTS, so only the escape check can stop
    it (the version of this test before 2026-11-03 pointed at a missing file, and
    would have passed with the check deleted)."""
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.pdf").write_bytes(b"not under the root")

    with pg_session_factory() as session:
        ws = make_workspace(session)
        ref = _make_ref(session, ws, storage_path="../outside.pdf")
        session.commit()
        count = M._backfill_original_sha256(session.connection(), root)
        session.commit()

    assert count == 0
    assert _sha_of(pg_session_factory, ref.id) is None


# ---------------------------------------------------------------------------
# 2. The migration end to end
# ---------------------------------------------------------------------------


def test_upgrade_0037_hashes_rows_that_predate_it(
    pg_url, pg_session_factory, tmp_path, monkeypatch
):
    """Downgrade to 0036, seed there, upgrade to 0037, read the hashes back.

    `pg_session_factory` is requested only for its teardown TRUNCATE, same as
    tests/test_migration_0064_claimed_at_generation.py.
    """
    cfg = alembic_cfg(pg_url)
    eng = create_engine(pg_url)
    root = tmp_path / "root"
    present_bytes = b"%PDF-1.4 a paper uploaded before 0036"
    try:
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0036")
        try:
            with eng.begin() as c:
                uid = c.execute(
                    text("INSERT INTO users (email) VALUES ('m0037@umu.se') RETURNING id")
                ).scalar_one()
                wid = c.execute(
                    text(
                        "INSERT INTO workspaces (id, title, owner_id) "
                        "VALUES (gen_random_uuid(), 'W0037', :u) RETURNING id"
                    ),
                    {"u": uid},
                ).scalar_one()

                def ref(storage_path):
                    return c.execute(
                        text(
                            'INSERT INTO "references" (id, workspace_id, kind, source_type, '
                            "filename, storage_path, mime_type) VALUES (gen_random_uuid(), "
                            ":w, 'paper', 'uploaded', 'original.pdf', :p, 'application/pdf') "
                            "RETURNING id"
                        ),
                        {"w": wid, "p": storage_path},
                    ).scalar_one()

                present = ref(f"{wid}/references/a/original.pdf")
                absent = ref(f"{wid}/references/b/original.pdf")
                escaping = ref("../outside.pdf")
            target = root / str(wid) / "references" / "a" / "original.pdf"
            target.parent.mkdir(parents=True)
            target.write_bytes(present_bytes)
            (tmp_path / "outside.pdf").write_bytes(b"not under the root")
            monkeypatch.setenv("WORKSPACES_DIR", str(root))

            command.upgrade(cfg, "0037")

            with eng.connect() as c:
                got = dict(
                    c.execute(
                        text(
                            'SELECT id, original_sha256 FROM "references" '
                            "WHERE workspace_id = :w"
                        ),
                        {"w": wid},
                    ).all()
                )
        finally:
            command.upgrade(cfg, "head")

        with eng.connect() as c:
            cols = {col["name"] for col in inspect(c).get_columns("references")}
        assert "original_sha256" in cols
    finally:
        eng.dispose()

    assert got == {
        present: hashlib.sha256(present_bytes).hexdigest(),
        absent: None,
        escaping: None,
    }
```
Seeding at 0036 with only `users(email)`, `workspaces(id,title,owner_id)` and the seven required `references` columns works because every other NOT NULL column has a server default (INFERRED from 0001/0005/0014/0023/0031/0033/0036).

#### `tests/migrations/test_0066_backfill.py` (MEASURED: ruff clean)
```python
"""Migration 0066: workspaces.current_branch, read from each repo, never guessed.

Moved from tests/proposal_assist/test_branch_column_migration.py on 2026-11-03,
when the proposal workspace left master. The backfill it tested is now a frozen
copy inside alembic/versions/0066_workspace_current_branch.py, and these tests
call it there. The last test runs upgrade() itself on rows that predate it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select, text

from alembic import command
from grant_lab.db.models.workspace import Workspace
from tests.migrations.support import (
    alembic_cfg,
    git_repo,
    load_migration,
    make_user,
    make_workspace,
)

M = load_migration("0066_workspace_current_branch.py")
_OLD = datetime(2020, 1, 1, tzinfo=UTC)


def _branch_of(session_factory, wid):
    with session_factory() as s:
        return s.execute(select(Workspace.current_branch).where(Workspace.id == wid)).scalar_one()


def _backfill(session_factory, root):
    with session_factory() as s:
        n = M._backfill_current_branch(s.connection(), workspaces_dir=root)
        s.commit()
    return n


def test_backfill_reads_the_repos_actual_branch(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "aims-ambitious")

    assert _backfill(pg_session_factory, tmp_path) == 1
    assert _branch_of(pg_session_factory, wid) == "aims-ambitious"


def test_each_workspace_gets_its_own_repos_branch(pg_session_factory, tmp_path):
    """Several rows at once. A copy that wrote one name to every row, or keyed the
    UPDATE on the wrong id, passes the single-row test above."""
    with pg_session_factory() as s:
        a = make_workspace(s, title="a", owner=make_user(s, email="m66-a@umu.se")).id
        b = make_workspace(s, title="b", owner=make_user(s, email="m66-b@umu.se")).id
        none = make_workspace(s, title="none", owner=make_user(s, email="m66-c@umu.se")).id
        s.commit()
    git_repo(tmp_path, a, "aims-ambitious")
    git_repo(tmp_path, b, "eu-version")

    assert _backfill(pg_session_factory, tmp_path) == 2
    assert [_branch_of(pg_session_factory, w) for w in (a, b, none)] == [
        "aims-ambitious",
        "eu-version",
        "main",
    ]


def test_backfill_records_an_unborn_repos_branch_rather_than_failing(pg_session_factory, tmp_path):
    # A workspace still in setup has a repo with no commits. Its branch NAME is
    # known and correct; failing here would block the whole migration on it.
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "draft-1", commit=False)

    _backfill(pg_session_factory, tmp_path)
    assert _branch_of(pg_session_factory, wid) == "draft-1"


def test_backfill_leaves_a_workspace_with_no_repo_at_the_default(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    # No repo directory created at all.
    assert _backfill(pg_session_factory, tmp_path) == 0
    assert _branch_of(pg_session_factory, wid) == "main"


def test_backfill_RAISES_on_a_detached_head(pg_session_factory, tmp_path):
    # Nothing in the app detached HEAD, so a detached repo means something
    # outside the app did. Writing a plausible 'main' would discard that state.
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "main", detach=True)

    with pg_session_factory() as s, pytest.raises(M.GitCommitError):
        M._backfill_current_branch(s.connection(), workspaces_dir=tmp_path)


def test_the_backfill_restamps_updated_at_as_the_orm_update_did(pg_session_factory, tmp_path):
    """The original ran `update(Workspace)`, and SQLAlchemy added the model's
    `onupdate`: `SET updated_at=now(), current_branch=...` (compiled 2026-11-03).
    The frozen copy writes that out, so a database migrated today gets the rows
    production got. A row with no repo is not updated at all."""
    with pg_session_factory() as s:
        a = make_workspace(s, title="repo", owner=make_user(s, email="m66-r@umu.se")).id
        b = make_workspace(s, title="no repo", owner=make_user(s, email="m66-n@umu.se")).id
        s.execute(
            text("UPDATE workspaces SET updated_at = :old WHERE id IN (:a, :b)"),
            {"old": _OLD, "a": a, "b": b},
        )
        s.commit()
    git_repo(tmp_path, a, "main")

    _backfill(pg_session_factory, tmp_path)

    with pg_session_factory() as s:
        stamps = dict(
            s.execute(select(Workspace.id, Workspace.updated_at).where(Workspace.id.in_([a, b])))
        )
    assert stamps[a] > _OLD
    assert stamps[b] == _OLD


def test_upgrade_0066_backfills_rows_that_predate_it(
    pg_url, pg_session_factory, tmp_path, monkeypatch
):
    """upgrade() itself: downgrade to 0065, seed workspaces and their repos there,
    upgrade to 0066 with WORKSPACES_DIR pointing at the repos, read the column.

    This is what proves upgrade() calls the frozen copy, with the env var, after
    the column exists. `pg_session_factory` is requested only for its teardown
    TRUNCATE (same as tests/test_migration_0064_claimed_at_generation.py).
    """
    cfg = alembic_cfg(pg_url)
    eng = create_engine(pg_url)
    try:
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0065")
        try:
            with eng.begin() as c:
                uid = c.execute(
                    text("INSERT INTO users (email) VALUES ('m0066@umu.se') RETURNING id")
                ).scalar_one()

                def ws(title):
                    return c.execute(
                        text(
                            "INSERT INTO workspaces (id, title, owner_id, updated_at) "
                            "VALUES (gen_random_uuid(), :t, :u, :at) RETURNING id"
                        ),
                        {"t": title, "u": uid, "at": _OLD},
                    ).scalar_one()

                feature, unborn, no_repo = ws("feature"), ws("unborn"), ws("no repo")
            git_repo(tmp_path, feature, "aims-ambitious", extra=("main",))
            git_repo(tmp_path, unborn, "draft-1", commit=False)
            monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))

            command.upgrade(cfg, "0066")

            with eng.connect() as c:
                rows = c.execute(
                    text(
                        "SELECT id, current_branch, updated_at FROM workspaces "
                        "WHERE owner_id = :u"
                    ),
                    {"u": uid},
                ).all()
        finally:
            command.upgrade(cfg, "head")
    finally:
        eng.dispose()

    got = {wid: branch for wid, branch, _ in rows}
    stamped = {wid: at for wid, _, at in rows}
    assert got == {feature: "aims-ambitious", unborn: "draft-1", no_repo: "main"}
    assert stamped[no_repo] == _OLD
    assert stamped[feature] > _OLD and stamped[unborn] > _OLD
```

#### `tests/migrations/test_0068_backfill.py` (MEASURED: ruff clean)
```python
"""Migration 0068: the branch entity, founded from each workspace's real refs.

Moved from tests/proposal_assist/test_branch_entity_backfill.py on 2026-11-03,
when the proposal workspace left master. The reconciler those tests exercised is
now a frozen copy inside alembic/versions/0068_branch_entity.py, and these tests
call it there. The last test runs upgrade() itself on rows that predate it.

Left behind in the move, with the reason:
  * the Branch schema tests (unique name per workspace, RESTRICT on the root):
    they test table constraints, not the backfill, and the table is parked;
  * the `workspace_id=` narrowing test: only Phase 0 passed it, and the frozen
    copy does not take it;
  * the two AST tests of upgrade()'s wiring: `test_upgrade_0068_*` below runs
    upgrade() for real, which shows the call is reachable and comes after the
    schema it writes;
  * the AST test that no whole-entity SELECT appears: the frozen copy has no ORM
    entities (test_frozen_migrations_import_no_app_code.py pins that it imports
    no application code at all);
  * the root-guard half of the switch test: it called the removed
    `delete_workspace_branch`.
Added: the cross-workspace root check from the deleted
tests/architecture/test_branch_entity_invariants.py, the one property in that
file that belongs to the backfill alone.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect, select, text

from alembic import command
from grant_lab.db.models.workspace import Branch, Workspace
from tests.migrations.support import (
    alembic_cfg,
    git_repo,
    load_migration,
    make_user,
    make_workspace,
    repo_dir,
)

M = load_migration("0068_branch_entity.py")


def _run(session_factory, root):
    """One reconcile in its own session, committed. Returns the counts."""
    with session_factory() as s:
        r = M._backfill_branch_entities(s.connection(), workspaces_dir=root)
        s.commit()
    return r


def _names(session, wid):
    return sorted(
        n for (n,) in session.execute(select(Branch.name).where(Branch.workspace_id == wid)).all()
    )


def _root_name(session, wid):
    root_id = session.get(Workspace, wid).root_branch_id
    return None if root_id is None else session.get(Branch, root_id).name


def test_every_local_ref_gets_a_row_not_just_the_current_one(pg_session_factory, tmp_path):
    """`create_workspace_branch` did nothing but `git branch`, so a workspace
    could already hold several refs. A ref with no row is invisible."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "main", extra=("new_ideas", "eu_version"))

    r = _run(pg_session_factory, tmp_path)
    assert (r.created, r.deleted, r.rooted, r.skipped) == (3, 0, 1, 0)
    with pg_session_factory() as s:
        assert _names(s, wid) == ["eu_version", "main", "new_ideas"]


def test_a_slashed_branch_name_round_trips(pg_session_factory, tmp_path):
    """`git check-ref-format --branch` accepts `aims/ambitious`; a slashed name
    must survive the row it is founded into."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "main", extra=("aims/ambitious",))

    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        assert _names(s, wid) == ["aims/ambitious", "main"]


def test_the_root_pointer_names_the_branch_repo_actually_holds(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "aims-ambitious", extra=("main",))

    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        assert _root_name(s, wid) == "aims-ambitious"


def test_the_root_pointer_never_names_another_workspaces_branch(pg_session_factory, tmp_path):
    """`root_branch_id` is a bare FK to `branches.id`: nothing in the schema stops
    a cross-workspace pointer, only the backfill's own scoping. Both workspaces
    use the same branch name, which is what a scoping bug would confuse; checked
    by ownership, because two SWAPPED pointers are still unequal."""
    with pg_session_factory() as s:
        a = make_workspace(s, title="a", owner=make_user(s, email="a@umu.se")).id
        b = make_workspace(s, title="b", owner=make_user(s, email="b@umu.se")).id
        s.commit()
    git_repo(tmp_path, a, "main")
    git_repo(tmp_path, b, "main")

    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        for wid in (a, b):
            root = s.get(Branch, s.get(Workspace, wid).root_branch_id)
            assert root.workspace_id == wid, f"{wid}'s root belongs to {root.workspace_id}"


def _head_and_column_disagree(session_factory, tmp_path):
    """The state a crashed switch left: `repo/` is on main, the column says
    new_ideas."""
    with session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "main", extra=("new_ideas",))
    with session_factory() as s:
        ws = s.get(Workspace, wid)
        ws.current_branch = "new_ideas"
        ws.last_sdk_session_id = "sess-abc"
        s.commit()
    return wid


def test_the_root_pointer_follows_git_not_the_column(pg_session_factory, tmp_path):
    """The root is founded on what `repo/` actually is."""
    wid = _head_and_column_disagree(pg_session_factory, tmp_path)
    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        assert _root_name(s, wid) == "main", "root followed the column, not repo/"


def test_the_sdk_handle_follows_the_column_not_git(pg_session_factory, tmp_path):
    """The mirror image, on the SAME fixture: a conversation belongs to the branch
    the USER was on, and the column is the source of truth for that."""
    wid = _head_and_column_disagree(pg_session_factory, tmp_path)
    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        rows = s.execute(select(Branch).where(Branch.workspace_id == wid)).scalars().all()
        assert {b.name: b.last_sdk_session_id for b in rows} == {
            "main": None,
            "new_ideas": "sess-abc",
        }


def test_the_sdk_handle_lands_on_one_branch_only(pg_session_factory, tmp_path):
    """Copying it to every branch would let two branches resume one session."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.get(Workspace, wid).last_sdk_session_id = "sess-abc"
        s.commit()
    git_repo(tmp_path, wid, "main", extra=("new_ideas", "eu_version"))

    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        rows = s.execute(select(Branch).where(Branch.workspace_id == wid)).scalars().all()
        assert [b.name for b in rows if b.last_sdk_session_id == "sess-abc"] == ["main"]


def test_the_workspace_column_is_left_alone(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.get(Workspace, wid).last_sdk_session_id = "sess-abc"
        s.commit()
    git_repo(tmp_path, wid, "main")

    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        assert s.get(Workspace, wid).last_sdk_session_id == "sess-abc"


def test_a_row_whose_ref_is_gone_is_deleted(pg_session_factory, tmp_path):
    """`git branch -D` is not transactional, so a delete whose commit failed
    afterwards leaves a row with no ref, and nothing else would remove it."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "main")
    with pg_session_factory() as s:
        # "main" already has a row, so this run deletes and does not also create.
        s.add(Branch(workspace_id=wid, name="main"))
        s.add(Branch(workspace_id=wid, name="deleted_behind_our_back"))
        s.commit()

    r = _run(pg_session_factory, tmp_path)
    assert (r.created, r.deleted) == (0, 1)
    with pg_session_factory() as s:
        assert _names(s, wid) == ["main"]


def test_an_unreadable_repo_deletes_nothing(pg_session_factory, tmp_path):
    """A git failure must never read as 'this workspace has no branches'."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    repo = git_repo(tmp_path, wid, "main", extra=("new_ideas",))
    _run(pg_session_factory, tmp_path)
    (repo / ".git" / "HEAD").write_text("garbage\n")

    r = _run(pg_session_factory, tmp_path)
    assert (r.deleted, r.skipped) == (0, 1)
    with pg_session_factory() as s:
        assert _names(s, wid) == ["main", "new_ideas"]


def test_running_it_twice_changes_nothing(pg_session_factory, tmp_path):
    """A migration can be re-run against a partially-migrated database, and the
    unique key would turn a non-idempotent pass into a hard failure."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "main", extra=("new_ideas",))

    r = _run(pg_session_factory, tmp_path)
    assert (r.created, r.deleted, r.rooted) == (2, 0, 1)
    r = _run(pg_session_factory, tmp_path)
    # rooted is 0, not 1: the pointer was already founded, and founding is all
    # this does.
    assert (r.created, r.deleted, r.rooted) == (0, 0, 0)
    with pg_session_factory() as s:
        assert _names(s, wid) == ["main", "new_ideas"]


def test_a_workspace_with_no_repo_is_absent_not_skipped(pg_session_factory, tmp_path):
    """Still in setup: nothing to found a branch on, and not a failure."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()

    r = _run(pg_session_factory, tmp_path)
    assert (r.created, r.rooted, r.skipped, r.absent) == (0, 0, 0, 1)
    with pg_session_factory() as s:
        assert _names(s, wid) == []
        assert s.get(Workspace, wid).root_branch_id is None


def test_a_workspace_with_no_repo_is_counted_absent_not_silently_zero(
    pg_session_factory, tmp_path
):
    """An unset WORKSPACES_DIR makes EVERY workspace look like one in setup.
    Counting them is what tells that apart from an empty instance."""
    with pg_session_factory() as s:
        a = make_workspace(s, title="a", owner=make_user(s, email="abs-a@umu.se")).id
        make_workspace(s, title="b", owner=make_user(s, email="abs-b@umu.se"))
        s.commit()
    git_repo(tmp_path, a, "main")  # one real repo, one workspace with none

    r = _run(pg_session_factory, tmp_path)
    assert (r.created, r.rooted, r.skipped, r.absent) == (1, 1, 0, 1)


def test_a_repo_with_no_commits_is_a_no_op(pg_session_factory, tmp_path):
    """`git init` with no commit has a branch name but no ref to found a row on."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "main", commit=False)

    r = _run(pg_session_factory, tmp_path)
    assert (r.created, r.deleted, r.rooted, r.skipped, r.absent) == (0, 0, 0, 0, 0), r
    with pg_session_factory() as s:
        assert _names(s, wid) == []
        assert s.get(Workspace, wid).root_branch_id is None


def test_a_detached_head_still_founds_the_refs_but_no_root(pg_session_factory, tmp_path):
    """Detaching HEAD removes no ref, so every ref still gets a row. Only the ROOT
    is unknown, so only the root is left unset."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "main", extra=("new_ideas",), detach=True)

    r = _run(pg_session_factory, tmp_path)
    assert (r.created, r.rooted) == (2, 0)
    with pg_session_factory() as s:
        assert _names(s, wid) == ["main", "new_ideas"]
        assert s.get(Workspace, wid).root_branch_id is None


def test_a_detached_head_founds_no_pseudo_branch(pg_session_factory, tmp_path):
    """On a detached HEAD `git branch` prints `(HEAD detached at <sha>)` above the
    real refs. Asserted as exact equality: a membership check would pass with the
    pseudo-row beside `main`."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "main", detach=True)

    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        assert _names(s, wid) == ["main"]


def test_one_broken_repo_does_not_halt_the_rest(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        good = make_workspace(s, title="good", owner=make_user(s, email="good@umu.se")).id
        bad = make_workspace(s, title="bad", owner=make_user(s, email="bad@umu.se")).id
        s.commit()
    git_repo(tmp_path, good, "main")
    broken = repo_dir(tmp_path, bad)
    broken.mkdir(parents=True)
    (broken / ".git").write_text("not a git dir")

    r = _run(pg_session_factory, tmp_path)
    assert (r.created, r.rooted, r.skipped) == (1, 1, 1)
    with pg_session_factory() as s:
        assert _names(s, good) == ["main"]
        assert _names(s, bad) == []


def test_reconciling_survives_the_root_branchs_ref_being_deleted(pg_session_factory, tmp_path):
    """fk_workspaces_root_branch_id is ON DELETE RESTRICT and one transaction
    covers every workspace, so deleting the row the pointer names would abort the
    batch. The second workspace gets a NEW ref in the second run, so a rolled-back
    batch would show as its missing row."""
    with pg_session_factory() as s:
        broken = make_workspace(s, title="broken", owner=make_user(s, email="br@umu.se")).id
        other = make_workspace(s, title="other", owner=make_user(s, email="ot@umu.se")).id
        s.commit()
    for wid in (broken, other):
        git_repo(tmp_path, wid, "main", extra=("new_ideas",))
    _run(pg_session_factory, tmp_path)

    d = repo_dir(tmp_path, broken)
    subprocess.run(["git", "checkout", "-q", "new_ideas"], cwd=d, check=True)
    subprocess.run(["git", "branch", "-D", "main"], cwd=d, check=True)
    subprocess.run(["git", "branch", "third"], cwd=repo_dir(tmp_path, other), check=True)

    r = _run(pg_session_factory, tmp_path)  # must not raise
    assert r.deleted == 1
    with pg_session_factory() as s:
        assert _names(s, broken) == ["new_ideas"]
        assert _names(s, other) == ["main", "new_ideas", "third"], "the batch rolled back"
        assert _root_name(s, broken) == "new_ideas"


def test_a_rootless_workspace_whose_root_ref_vanished_is_left_honestly_rootless(
    pg_session_factory, tmp_path
):
    """No new root is knowable (detached) AND the old root's ref is gone: clear the
    pointer rather than promote a survivor nobody chose."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w", owner=make_user(s, email="rootless@umu.se")).id
        s.commit()
    git_repo(tmp_path, wid, "main", extra=("new_ideas",))
    _run(pg_session_factory, tmp_path)

    d = repo_dir(tmp_path, wid)
    subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=d, check=True)
    subprocess.run(["git", "branch", "-D", "main"], cwd=d, check=True)

    r = _run(pg_session_factory, tmp_path)  # must not raise
    assert (r.deleted, r.rooted) == (1, 0)
    with pg_session_factory() as s:
        assert _names(s, wid) == ["new_ideas"]
        assert s.get(Workspace, wid).root_branch_id is None


def test_a_surviving_root_pointer_is_not_cleared_by_an_unrelated_orphan(
    pg_session_factory, tmp_path
):
    """Control for the test above: the clear fires ONLY when the doomed row is the
    one the pointer names."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w", owner=make_user(s, email="survive@umu.se")).id
        s.commit()
    git_repo(tmp_path, wid, "main")
    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        s.add(Branch(workspace_id=wid, name="stale_unrelated"))
        s.commit()
        root_before = s.get(Workspace, wid).root_branch_id

    subprocess.run(
        ["git", "checkout", "-q", "--detach", "HEAD"], cwd=repo_dir(tmp_path, wid), check=True
    )
    r = _run(pg_session_factory, tmp_path)
    assert r.deleted == 1
    with pg_session_factory() as s:
        assert s.get(Workspace, wid).root_branch_id == root_before, "unrooted a healthy workspace"


def test_reconciling_after_a_switch_does_not_move_the_root(pg_session_factory, tmp_path):
    """The root is FOUNDED, never moved: a re-run after a switch must leave it.
    (The original also proved the root stayed undeletable, through the removed
    `delete_workspace_branch`; that half went with it.)"""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w", owner=make_user(s, email="noswitch@umu.se")).id
        s.commit()
    repo = git_repo(tmp_path, wid, "main", extra=("new_ideas",))
    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        assert _root_name(s, wid) == "main"

    subprocess.run(["git", "checkout", "-q", "new_ideas"], cwd=repo, check=True)
    r = _run(pg_session_factory, tmp_path)
    assert r.rooted == 0, "a re-run re-founded a root that was already set"
    with pg_session_factory() as s:
        assert _root_name(s, wid) == "main", "the root moved to what repo/ had checked out"


def test_the_sdk_handle_lands_on_exactly_one_branch_after_a_switch(pg_session_factory, tmp_path):
    """Reconcile, switch (which moves current_branch and leaves the workspace's
    handle), reconcile: the handle must end on ONE row. Asserted over the whole
    map, because `held["feature"] == "sess-X"` passes with main still holding it."""
    with pg_session_factory() as s:
        w = make_workspace(s, title="w")
        wid = w.id
        w.current_branch = "main"
        w.last_sdk_session_id = "sess-X"
        s.commit()
    git_repo(tmp_path, wid, "main", extra=("feature",))
    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:  # what switch_branch left behind
        s.get(Workspace, wid).current_branch = "feature"
        s.commit()
    _run(pg_session_factory, tmp_path)

    with pg_session_factory() as s:
        held = dict(
            s.execute(
                select(Branch.name, Branch.last_sdk_session_id).where(Branch.workspace_id == wid)
            ).all()
        )
    assert held == {"feature": "sess-X", "main": None}, held


def test_current_branch_is_not_nullable_so_the_null_path_is_unreachable(migrated_engine):
    """Why no test sets current_branch to NULL: the column is NOT NULL, so the
    `is_distinct_from` (rather than `!=`) in the clearing UPDATE guards a path no
    row can reach. Pinned on the database, not the ORM."""
    cols = {c["name"]: c for c in inspect(migrated_engine).get_columns("workspaces")}
    assert cols["current_branch"]["nullable"] is False


def test_a_repo_whose_last_ref_vanished_is_swept(pg_session_factory, tmp_path):
    """A readable repo with ZERO refs still needs the sweep: its stale row and
    pointer would otherwise stay forever."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    d = git_repo(tmp_path, wid, "main")
    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        assert s.get(Workspace, wid).root_branch_id is not None

    subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=d, check=True)
    subprocess.run(["git", "branch", "-q", "-D", "main"], cwd=d, check=True)
    r = _run(pg_session_factory, tmp_path)
    assert r.deleted == 1, r
    with pg_session_factory() as s:
        assert _names(s, wid) == []
        assert s.get(Workspace, wid).root_branch_id is None


def test_an_unreadable_HEAD_skips_the_workspace_instead_of_clearing_its_root(
    pg_session_factory, tmp_path, monkeypatch
):
    """An operational git failure must not be read as "detached HEAD", which is
    the branch that CLEARS the root pointer. Set up on the destructive path (the
    root's ref is gone), so a run that did not skip WOULD clear it."""
    with pg_session_factory() as s:
        w = make_workspace(s, title="w")
        wid = w.id
        w.current_branch = "main"
        s.commit()
    d = git_repo(tmp_path, wid, "main", extra=("side",))
    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        root_before = s.get(Workspace, wid).root_branch_id
    assert root_before is not None

    subprocess.run(["git", "checkout", "-q", "side"], cwd=d, check=True)
    subprocess.run(["git", "branch", "-q", "-D", "main"], cwd=d, check=True)
    subprocess.run(["git", "branch", "third"], cwd=d, check=True)

    def _unreadable(_repo_dir):
        raise M.GitCommitError("cannot read HEAD")

    monkeypatch.setattr(M, "_head_branch", _unreadable)
    r = _run(pg_session_factory, tmp_path)
    assert (r.skipped, r.created, r.deleted, r.rooted) == (1, 0, 0, 0), r
    with pg_session_factory() as s:
        assert s.get(Workspace, wid).root_branch_id == root_before
        assert _names(s, wid) == ["main", "side"]


def test_an_unreadable_directory_skips_one_workspace_not_the_batch(pg_session_factory, tmp_path):
    """`Path.exists()` RAISES on EACCES. Unreadable counts as `skipped`, not
    `absent`, and the healthy workspace is still founded."""
    with pg_session_factory() as s:
        bad = make_workspace(s, title="bad", owner=make_user(s, email="bad@umu.se")).id
        good = make_workspace(s, title="good", owner=make_user(s, email="good@umu.se")).id
        s.commit()
    git_repo(tmp_path, good, "main")
    locked = repo_dir(tmp_path, bad)
    locked.mkdir(parents=True)
    (locked / ".git").mkdir()
    os.chmod(locked, 0o000)
    try:
        r = _run(pg_session_factory, tmp_path)
    finally:
        os.chmod(locked, 0o755)

    assert (r.skipped, r.created) == (1, 1), r
    with pg_session_factory() as s:
        assert s.get(Workspace, good).root_branch_id is not None


def test_founding_the_root_does_not_restamp_updated_at(pg_session_factory, tmp_path):
    """The original named `updated_at` with its own value to suppress the model's
    onupdate, because a restamp reordered every user's workspace list. Asserted on
    the exact timestamp."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "main")
    with pg_session_factory() as s:
        before = s.get(Workspace, wid).updated_at

    r = _run(pg_session_factory, tmp_path)
    assert r.rooted == 1, r
    with pg_session_factory() as s:
        ws = s.get(Workspace, wid)
        assert ws.root_branch_id is not None
        assert ws.updated_at == before, "the founding restamped updated_at"


@pytest.mark.parametrize(
    "boom",
    [
        pytest.param(OSError("disk gone"), id="OSError"),
        pytest.param(subprocess.TimeoutExpired(cmd=["git"], timeout=30.0), id="TimeoutExpired"),
    ],
)
def test_every_arm_of_the_skip_handler_is_reachable(
    pg_session_factory, tmp_path, monkeypatch, boom
):
    """`subprocess.TimeoutExpired` is NOT an OSError, so dropping it from the
    handler would let a 30 s git timeout abort the whole migration."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w", owner=make_user(s, email="arm@umu.se")).id
        s.commit()
    git_repo(tmp_path, wid, "main")

    def _raise(_repo_dir):
        raise boom

    monkeypatch.setattr(M, "_list_branches", _raise)
    r = _run(pg_session_factory, tmp_path)
    assert (r.skipped, r.created, r.rooted) == (1, 0, 0), r


def test_timeout_is_not_an_OSError_which_is_why_both_are_listed():
    assert not issubclass(subprocess.TimeoutExpired, OSError)


_SUMMARY = re.compile(
    r"0068: founded (\d+) branch rows, removed (\d+) orphans, set (\d+) root pointers, "
    r"SKIPPED (\d+) unreadable repos, (\d+) workspaces with no repo directory"
)


def test_upgrade_0068_founds_branches_for_rows_that_predate_it(
    pg_url, pg_session_factory, tmp_path, monkeypatch, capsys
):
    """upgrade() itself: downgrade to 0067, seed workspaces and their repos there,
    upgrade to 0068 with WORKSPACES_DIR pointing at the repos, read the rows back.

    This is what proves upgrade() calls the frozen copy, with the env var, after
    `branches` and `root_branch_id` exist. `pg_session_factory` is requested only
    for its teardown TRUNCATE.
    """
    cfg = alembic_cfg(pg_url)
    eng = create_engine(pg_url)
    stamp = datetime(2020, 1, 1, tzinfo=UTC)
    try:
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0067")
        try:
            with eng.begin() as c:
                uid = c.execute(
                    text("INSERT INTO users (email) VALUES ('m0068@umu.se') RETURNING id")
                ).scalar_one()

                def ws(title, current_branch="main", sdk=None):
                    return c.execute(
                        text(
                            "INSERT INTO workspaces (id, title, owner_id, current_branch, "
                            "last_sdk_session_id, updated_at) VALUES (gen_random_uuid(), "
                            ":t, :u, :cb, :sdk, :at) RETURNING id"
                        ),
                        {"t": title, "u": uid, "cb": current_branch, "sdk": sdk, "at": stamp},
                    ).scalar_one()

                several = ws("several", current_branch="new_ideas", sdk="sess-0068")
                detached = ws("detached")
                no_repo = ws("no repo")
                broken = ws("broken")
            git_repo(tmp_path, several, "main", extra=("new_ideas", "eu_version"))
            git_repo(tmp_path, detached, "main", extra=("side",), detach=True)
            bad = repo_dir(tmp_path, broken)
            bad.mkdir(parents=True)
            (bad / ".git").write_text("not a git dir")
            monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))
            capsys.readouterr()  # drop anything printed before the upgrade

            command.upgrade(cfg, "0068")

            out = capsys.readouterr().out
            with eng.connect() as c:
                branches = c.execute(
                    text(
                        "SELECT b.workspace_id, b.name, b.last_sdk_session_id FROM branches b "
                        "JOIN workspaces w ON w.id = b.workspace_id WHERE w.owner_id = :u"
                    ),
                    {"u": uid},
                ).all()
                roots = c.execute(
                    text(
                        "SELECT w.id, b.name, w.updated_at FROM workspaces w "
                        "LEFT JOIN branches b ON b.id = w.root_branch_id WHERE w.owner_id = :u"
                    ),
                    {"u": uid},
                ).all()
        finally:
            command.upgrade(cfg, "head")
    finally:
        eng.dispose()

    by_ws: dict = {}
    for wid, name, sdk in branches:
        by_ws.setdefault(wid, {})[name] = sdk
    assert by_ws == {
        several: {"main": None, "new_ideas": "sess-0068", "eu_version": None},
        detached: {"main": None, "side": None},
    }
    assert {wid: root for wid, root, _ in roots} == {
        several: "main",
        detached: None,
        no_repo: None,
        broken: None,
    }
    assert {at for _, _, at in roots} == {stamp}, "the founding restamped updated_at"
    m = _SUMMARY.search(out)
    assert m, f"0068 printed no summary: {out!r}"
    founded, _removed, rooted, skipped, absent = map(int, m.groups())
    # At least, not exactly: the database is shared by the whole suite, and a
    # workspace another test left behind would be counted too.
    assert founded >= 5 and rooted >= 1 and skipped >= 1 and absent >= 1, out
```
**Why the tests still see the backfill's writes (INFERRED).** `make_session_factory` uses `expire_on_commit=False` (READ, `db/session.py:78`). Every test reads `Workspace`/`Branch` in a session that did not load them before the Core writes, so there are no stale identity-map reads.

#### `tests/migrations/test_frozen_migrations_import_no_app_code.py` (MEASURED: ruff clean)

Run against today's tree, its logic finds the 3 offenders, so the gate does bite (MEASURED).

```python
"""Migrations 0037, 0066 and 0068 carry frozen copies of the workspace code they
used to import (2026-11-03). A migration has to keep running after the code it
once called is gone, so those three import nothing from `grant_lab` at all, and
no migration may import the removed package.

G1 (`git grep proposal_assist -- src`) cannot see this: alembic/ is outside src/.
Imports inside upgrade() count, which is why this walks the whole tree.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
FROZEN = (
    "0037_backfill_reference_original_sha256.py",
    "0066_workspace_current_branch.py",
    "0068_branch_entity.py",
)


def _imported_modules(path: Path) -> list[str]:
    out = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            out.append(node.module or "")
    return out


@pytest.mark.parametrize("name", FROZEN)
def test_a_frozen_migration_imports_no_application_code(name):
    mods = [m for m in _imported_modules(VERSIONS / name) if m.split(".")[0] == "grant_lab"]
    assert mods == [], f"{name} imports {mods}"


def test_no_migration_imports_the_removed_workspace_package():
    offenders = sorted(
        f"{path.name}: {mod}"
        for path in VERSIONS.glob("*.py")
        for mod in _imported_modules(path)
        if mod.startswith("grant_lab.proposal_assist")
    )
    assert offenders == [], offenders
```

### 3e. G2b: one mutation per frozen copy (all PREDICTED, not run)

| copy | one-line mutation | expected red |
|---|---|---|
| 0037 (logic) | `digest = hashlib.sha256(path.read_bytes()).hexdigest()` → `digest = hashlib.sha256(storage_path.encode()).hexdigest()` | `test_backfill_fills_present_skips_absent` at `assert _sha_of(…ref_a.id) == hashlib.sha256(file_bytes).hexdigest()`. Also `test_each_row_gets_the_hash_of_its_own_file`, `test_backfill_idempotent`, and the end-to-end `assert got == {…}`. |
| 0037 (wiring: the case the swallow hides) | in `upgrade()`, `_backfill_original_sha256(` → `_backfill_original_sha25(` (a NameError that the except swallows) | Only `test_upgrade_0037_hashes_rows_that_predate_it`, at `assert got == {present: <hash>, …}` (gets `None`). G2 (`migrated_engine`) stays green, which is exactly why the end-to-end test exists. Run it with pytest only; ruff F821 would flag the name. |
| 0066 | `name = _current_branch(repo)` → `name = "main"` | `test_backfill_reads_the_repos_actual_branch` (`'main' == 'aims-ambitious'`). Also `test_each_workspace…`, `…unborn…` (`'draft-1'`), `test_backfill_RAISES_on_a_detached_head` (DID NOT RAISE), and the end-to-end `got == {…}`. |
| 0066 (fidelity) | drop `, updated_at=sa.func.now()` | `test_the_backfill_restamps_updated_at_as_the_orm_update_did` (`stamps[a] > _OLD`), and the end-to-end `stamped[feature] > _OLD`. |
| 0068 | `vacant = held_name is None or held_name in orphans` → `vacant = True` | `test_reconciling_after_a_switch_does_not_move_the_root` at `assert r.rooted == 0`. Also `test_running_it_twice_changes_nothing` (`(0, 0, 1) != (0, 0, 0)`) and `test_a_surviving_root_pointer_is_not_cleared…` (pointer cleared). |
| 0068 (alt) | `orphans = existing - set(names)` → `orphans = set()` | `test_a_row_whose_ref_is_gone_is_deleted` (`(0, 0) != (0, 1)`), `test_reconciling_survives…`, `test_a_repo_whose_last_ref_vanished_is_swept`. |

---

## TASK 4: Schema drift gate

### 4a. What the gate depends on (READ)

- `tests/test_schema_drift.py:18` does `from grant_lab.db.models import Base`. It runs `compare_metadata(MigrationContext(conn, compare_type=True), Base.metadata)` against `migrated_engine`.
- `migrated_engine` is defined at `tests/conftest.py:261-271`: `Config("alembic.ini")`, `command.upgrade(cfg, "head")` on the session-scoped testcontainer.
- `pg_session_factory` (`conftest.py:286`) also TRUNCATEs `Base.metadata.sorted_tables`.
- So `db/models/__init__.py` **must** import the parked package, and it does in 1c.

**Hollowness found (INFERRED).** `Base.metadata` is per process. Under `--dist loadfile`, every worker collects every test file. Any module that imports `grant_lab.db.models.workspace` registers the 31 tables in every worker: `tests/migrations/support.py`, the re-pointed migration tests. So deleting the `workspace` import from `db/models/__init__.py` would leave the drift test **green in a full run**, and red only when it runs alone. The added subprocess test closes that hole.

### 4b. New `tests/test_schema_drift.py` (full; MEASURED: ruff clean)

```python
"""Review 2026-09-02 §3 data layer: the ORM and the migrated database must agree.

Measured at the base commit: `compare_metadata` against a migrated pgvector
container reported exactly three ops, all `remove_index` — the HNSW vector index
(raw SQL in 0031), `ix_reference_chunks_ws_model` (0032) and the partial
`ix_references_indexing_backlog` (0031) existed in the database and nowhere in
`models.py`. The next `alembic revision --autogenerate` would have DROPPED the
vector index. Nothing in `tests/` ran `compare_metadata`. This does, and it is the
gate: any future column, index or constraint that lands in one place and not the
other fails here with the diff spelled out.

Since 2026-11-03 it also keeps the 31 PARKED tables honest: the proposal
workspace left master, its tables and rows did not, and their definitions live in
src/grant_lab/db/models/workspace/ only so that this test (and autogenerate) see
them.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from grant_lab.db.models import Base

REPO = Path(__file__).resolve().parents[1]

#: src/grant_lab/db/models/workspace/, table by table.
PARKED = frozenset(
    {
        # call_extracts.py
        "call_rubrics",
        "call_section_extracts",
        # documents.py
        "artifact_versions",
        "attachments",
        "chat_summaries",
        "chat_turns",
        "collab_artifact_state",
        "proposed_changes",
        "yjs_updates",
        # jobs.py
        "attachment_jobs",
        "claim_analysis_jobs",
        "coherence_jobs",
        "evaluator_jobs",
        "turn_jobs",
        "uncited_claims_jobs",
        # references.py
        "citation_audit_verdicts",
        "citation_llm_usage",
        "claim_classifications",
        "reference_chunks",
        "references",
        "review_run_items",
        "review_runs",
        "user_claim_annotations",
        # spend.py
        "llm_spend",
        # workspaces.py
        "branches",
        "phase_approvals",
        "spec_lock_events",
        "workspace_events",
        "workspace_setup_jobs",
        "workspace_viewers",
        "workspaces",
    }
)


def _describe(op) -> str:
    kind = op[0] if isinstance(op, tuple) else type(op).__name__
    target = op[1] if isinstance(op, tuple) and len(op) > 1 else ""
    name = getattr(target, "name", None) or getattr(target, "key", None) or target
    return f"{kind}: {name}"


def test_orm_metadata_matches_the_migrated_database(migrated_engine):
    with migrated_engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diffs = compare_metadata(ctx, Base.metadata)
    flat = [d for group in diffs for d in (group if isinstance(group, list) else [group])]
    assert flat == [], "ORM and migrations disagree:\n  " + "\n  ".join(_describe(d) for d in flat)


def test_the_models_package_alone_registers_every_parked_table():
    """`Base.metadata` is per PROCESS. Any test module that imports
    `grant_lab.db.models.workspace` registers the parked tables for the whole
    run, so the test above would stay green if `db/models/__init__.py` stopped
    importing that package -- and the next autogenerate would then emit DROP
    TABLE for all 31. A fresh interpreter sees only what the package imports."""
    assert len(PARKED) == 31
    code = "from grant_lab.db.models import Base; print(' '.join(sorted(Base.metadata.tables)))"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    missing = PARKED - set(proc.stdout.split())
    assert not missing, f"importing grant_lab.db.models does not register {sorted(missing)}"
```
Today the subprocess import registers 48 tables, including all 31 names in `PARKED` (MEASURED). So the new test passes on today's tree too.

### 4c. G3 must-reject (PREDICTED, not run)

**Deletion 1.** Delete `class SpecLockEvent` (the moved `workspace/workspaces.py`, today's lines 383-424). Also remove `SpecLockEvent` from the import and `__all__` in `workspace/__init__.py`; otherwise the suite errors with an ImportError instead of producing a diff.
- `spec_lock_events` has no inbound FK, so the diff is a single op.
- Expected: `test_orm_metadata_matches_the_migrated_database` fails with
  `AssertionError: ORM and migrations disagree:\n  remove_table: spec_lock_events`
- `test_the_models_package_alone_registers_every_parked_table` also fails: `importing grant_lab.db.models does not register ['spec_lock_events']`.

**Deletion 2.** Delete the line `from grant_lab.db.models import workspace  # noqa: F401 …` from `db/models/__init__.py`.
- Expected: the subprocess test goes red with all 31 names.
- The drift test goes red **only when run alone** (31 `remove_table:` lines). In a full run it stays green, for the reason in 4a.

**Optional.** `tests/architecture/test_parked_tables_have_no_live_readers.py` (MEASURED: ruff clean) enforces the docstring's "nothing else under src/ imports it". It catches only imports; raw SQL on the tables is not covered.
```python
"""The workspace tables are parked (src/grant_lab/db/models/workspace/): nothing
on master reads or writes them. So nothing under src/ may import that package
except grant_lab/db/models/__init__.py, which imports it only so Base.metadata
matches the database. G1 cannot see this: it greps for `proposal_assist`."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "grant_lab"
PARKED_DIR = SRC / "db" / "models" / "workspace"
PARKED = "grant_lab.db.models.workspace"
ALLOWED = {SRC / "db" / "models" / "__init__.py"}


def _names_parked(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(a.name == PARKED or a.name.startswith(PARKED + ".") for a in node.names)
    if isinstance(node, ast.ImportFrom) and node.module:
        if node.module == PARKED or node.module.startswith(PARKED + "."):
            return True
        return node.module == "grant_lab.db.models" and any(
            a.name == "workspace" for a in node.names
        )
    return False


def test_no_live_module_imports_the_parked_tables():
    offenders = sorted(
        f"{path.relative_to(SRC.parent)}:{node.lineno}"
        for path in SRC.rglob("*.py")
        if path not in ALLOWED and PARKED_DIR not in path.parents
        for node in ast.walk(ast.parse(path.read_text()))
        if _names_parked(node)
    )
    assert offenders == [], offenders
```
Must-reject (PREDICTED): adding `from grant_lab.db.models.workspace import Workspace` to any live module, e.g. `web/api/boards.py`, turns it red with that file:line.

---

## Open questions and risks

1. **Commit order (my proposal).**
   - **(a) Freeze commit, before the package is deleted:** the three frozen migrations, `tests/migrations/*`, the rewritten 0037 test, and deleting the two old backfill tests. Use `from grant_lab.db.models import …` in the four places listed in 3b. Run the G2b mutations here.
   - **(b) Removal commit(s), other owners:** delete PA, the workspace tests, `tests/fencing`, and `tests/architecture/test_branch_entity_invariants.py`. Remove the gen_architecture fence and allowlist hunk in the same commit, because `FENCING.read_text()` crashes otherwise. Re-point 0083 to `tests.migrations.support`.
   - **(c) Park commit:** `git mv`, the new `__init__`s, `calls.py`, the gen_architecture parked labelling, regenerated `docs/ARCHITECTURE.md`, the new drift test, and the import switches in 0040/0041/0064/0067/0069 and the four lines from (a). Run G3 here.
   - **If (c) lands before `tests/fencing` is deleted,** fix the path literals listed in 1d, or `test_fence_coverage` and `test_the_ledger_has_exactly_one_writer` go red (INFERRED).
2. **`tests/conftest.py:344-359` autouse fixture.** `_close_helper_entered_lifespans` imports `tests.proposal_assist._refs_helpers` on every test teardown. Deleting `tests/proposal_assist/` without removing that fixture breaks every test (INFERRED). That belongs to the test-removal owner, but it gates G2b and G3.
3. **The end-to-end tests run every downgrade from head to 0065, 0067 and 0036,** like the existing 0064 and 0037 tests do. A future migration with a broken downgrade would redden them. Accepted, with precedent.
4. **0066 fidelity choice.** The frozen 0066 reproduces the original `updated_at=now()` restamp; it is pinned by a test. If you would rather not restamp, that is a behaviour change to a migration production already ran. I kept the original behaviour.
5. **0037's `try/except` cannot recover from a database error** (2d). Unchanged behaviour; a SAVEPOINT (`bind.begin_nested()`) would fix it but changes behaviour, so I left it out.
6. **The frozen 0066/0068 drop `GIT_AUTHOR_*`/`GIT_COMMITTER_*` defaults.** They are read-only git commands, so INFERRED no effect. The rest of `_git_env` (hermetic config, absolute ceiling) is kept.
7. **The moved model files keep comments** naming PA code and `tests/architecture/test_branch_entity_invariants.py` (`workspace/workspaces.py`, today's lines 112-143). The spec says "unchanged", and the new `__init__` docstring warns about it.
8. **CLAUDE.md** says ARCHITECTURE.md carries "the fence primitives … the armed-gate allowlist sizes". That changes when the fence sections go (doc owner).
9. **ruff isort.** I001 shows on stdin for the new package paths only because they don't exist yet (MEASURED). Re-run `uv run ruff check .` after the files exist.

### Critical files for implementation
- /home/parlar_ai/dev/grant-lab/src/grant_lab/db/models/__init__.py
- /home/parlar_ai/dev/grant-lab/scripts/gen_architecture.py
- /home/parlar_ai/dev/grant-lab/alembic/versions/0068_branch_entity.py (plus 0066_workspace_current_branch.py, 0037_backfill_reference_original_sha256.py)
- /home/parlar_ai/dev/grant-lab/tests/test_schema_drift.py
- /home/parlar_ai/dev/grant-lab/tests/conftest.py (migrated_engine :261, truncate :286, autouse PA import :359)
