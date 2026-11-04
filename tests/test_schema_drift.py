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
