# Collab Slice 3b-5c — Active-Session Tail-Bounding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A periodic background job that checkpoints (bounds + compacts) the Yjs log tail of active/growing collab sessions, quietly (no version/activity spam), so a never-ending session's replay tail stays bounded.

**Architecture:** The app's `BackgroundScheduler` runs jobs in a thread, so the job must not touch the asyncio room `MANAGER`. `checkpoint_artifact` never reads the live room — it rebuilds from the `YjsUpdate` log under the commit mutex — so the job is pure DB+git: scan for `(workspace, artifact)` pairs with ≥ N uncheckpointed rows (excluding soft-deleted workspaces), then `checkpoint_artifact(record_version=False)` each in its own transaction.

**Tech Stack:** Python 3.12 / `uv` / `ruff` / `pytest` (+ pycrdt, testcontainers Postgres, git). APScheduler `BackgroundScheduler`.

Design spec: `docs/superpowers/specs/2026-07-05-collab-slice3b5c-tail-bounding-design.md`.

## Global Constraints

- Feature flag STAYS OFF; add no `enable_collab` source caller. Prod-inert: with the flag off there are no growing collab logs, so the scan is an empty no-op.
- **No DB migration, no schema change.** Reuses `YjsUpdate` + `CollabArtifactState` + `Workspace`. The two new settings are app config (pydantic `Settings`), not DB schema.
- Backend-only. Changes confined to `store.py`, `checkpoint.py`, `scheduler.py`, `config.py`. No frontend, no HTTP endpoint.
- Run backend tests grouped-dir with `EMBEDDING_MODEL=bge-m3` and `-p no:cacheprovider`; ALWAYS include `tests/test_api_spec.py` and `tests/test_scheduler.py` in the final verify. Never the whole suite (testcontainers PG drops under load).
- CI gate is `ruff check .` (not `ruff format`). Explicit-path `git add` only. Detached SDD, stop-before-merge, local merge only, not pushed.
- The `YjsUpdate.id` autoincrement is **global**, not per-room — the tail predicate MUST count rows above the watermark, never `max_id − watermark`.

---

## File Structure

- `src/call_manager/proposal_assist/collab/store.py` — ADD `find_checkpoint_candidates(session, *, min_tail)`. Owns log persistence + queries.
- `src/call_manager/proposal_assist/collab/checkpoint.py` — MODIFY `checkpoint_artifact` (add `record_version`); ADD `checkpoint_active_tails(session_factory, *, workspaces_dir, min_tail)`. Owns room→git checkpoint.
- `src/call_manager/config.py` — ADD `collab_checkpoint_scan_seconds`, `collab_checkpoint_min_tail`.
- `src/call_manager/scheduler.py` — register the interval job in `build_scheduler` when enabled.
- Tests: `tests/proposal_assist/collab/test_collab_store.py`, `tests/proposal_assist/collab/test_collab_checkpoint.py`, `tests/proposal_assist/collab/test_collab_checkpoint_scan.py` (new), `tests/test_scheduler.py`.

---

## Task 1: `find_checkpoint_candidates` — count-based scan excluding soft-deleted

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/store.py`
- Test: `tests/proposal_assist/collab/test_collab_store.py`

**Interfaces:**
- Consumes: `YjsUpdate(id, workspace_id, artifact, ...)`, `CollabArtifactState(workspace_id, artifact, last_checkpoint_update_id)`, `Workspace(id, soft_deleted_at)`; existing `store.append_update`, `state.advance_checkpoint`.
- Produces: `find_checkpoint_candidates(session, *, min_tail: int) -> list[tuple[uuid.UUID, str]]` — `(workspace_id, artifact)` pairs of live workspaces with ≥ `min_tail` rows whose `id` exceeds their checkpoint watermark.

- [ ] **Step 1: Write the failing test**

Add to `tests/proposal_assist/collab/test_collab_store.py`:

```python
from datetime import UTC, datetime

from call_manager.db.models import Workspace
from call_manager.proposal_assist.collab import state, store
from tests.proposal_assist.conftest import make_user, make_workspace


def test_find_checkpoint_candidates_counts_rows_excludes_softdeleted(pg_session_factory):
    with pg_session_factory() as s:
        o = make_user(s, email="fcc-o@ki.se")
        ws1 = make_workspace(s, owner=o)   # lightly edited (1 uncheckpointed row)
        ws2 = make_workspace(s, owner=o)   # over threshold (3 uncheckpointed rows)
        ws3 = make_workspace(s, owner=o)   # over threshold BUT soft-deleted
        s.commit()
        w1, w2, w3 = ws1.id, ws2.id, ws3.id

    with pg_session_factory() as s:
        # ws1: one row, then checkpoint watermark advanced past it
        store.append_update(s, workspace_id=w1, artifact="draft", actor_id=None, update=b"\x01")
        s.commit()
        first_id = store.load_rows(s, workspace_id=w1, artifact="draft")[-1][0]
        state.advance_checkpoint(
            s, workspace_id=w1, artifact="draft", up_to_update_id=first_id, commit_sha="x"
        )
        s.commit()
        # ws2: 3 uncheckpointed rows (interleaves global ids, inflating ws1's id-gap)
        for b in (b"\x02", b"\x03", b"\x04"):
            store.append_update(s, workspace_id=w2, artifact="draft", actor_id=None, update=b)
        s.commit()
        # ws1: ONE more row — now its max_id - watermark gap is large (ws2 bumped the
        # global sequence) but its uncheckpointed COUNT is only 1.
        store.append_update(s, workspace_id=w1, artifact="draft", actor_id=None, update=b"\x05")
        s.commit()
        # ws3: 3 uncheckpointed rows, but soft-delete the workspace
        for b in (b"\x06", b"\x07", b"\x08"):
            store.append_update(s, workspace_id=w3, artifact="draft", actor_id=None, update=b)
        s.get(Workspace, w3).soft_deleted_at = datetime.now(UTC)
        s.commit()

    with pg_session_factory() as s:
        got = set(store.find_checkpoint_candidates(s, min_tail=3))

    assert got == {(w2, "draft")}   # ws1 excluded (count=1, NOT gap); ws3 excluded (soft-deleted)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest -p no:cacheprovider tests/proposal_assist/collab/test_collab_store.py::test_find_checkpoint_candidates_counts_rows_excludes_softdeleted -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'find_checkpoint_candidates'`.

- [ ] **Step 3: Implement `find_checkpoint_candidates`**

Add to `store.py` (extend the existing imports):

```python
from sqlalchemy import func

from call_manager.db.models import CollabArtifactState, Workspace, YjsUpdate


def find_checkpoint_candidates(
    session: Session, *, min_tail: int
) -> list[tuple[uuid.UUID, str]]:
    """`(workspace_id, artifact)` pairs of LIVE (non-soft-deleted) workspaces whose
    log has at least `min_tail` rows above their checkpoint watermark.

    `YjsUpdate.id` is a GLOBAL autoincrement, so `max_id - watermark` would count
    other rooms' interleaved ids — the candidate predicate must COUNT rows whose id
    exceeds each room's own watermark (`HAVING count >= min_tail`). Soft-deleted
    workspaces are excluded (their tail must not be checkpointed — mirrors the
    eviction guard in permissions.make_checkpoint_on_evict)."""
    rows = session.execute(
        select(YjsUpdate.workspace_id, YjsUpdate.artifact)
        .join(Workspace, Workspace.id == YjsUpdate.workspace_id)
        .outerjoin(
            CollabArtifactState,
            (CollabArtifactState.workspace_id == YjsUpdate.workspace_id)
            & (CollabArtifactState.artifact == YjsUpdate.artifact),
        )
        .where(
            Workspace.soft_deleted_at.is_(None),
            YjsUpdate.id > func.coalesce(CollabArtifactState.last_checkpoint_update_id, 0),
        )
        .group_by(YjsUpdate.workspace_id, YjsUpdate.artifact)
        .having(func.count() >= min_tail)
    ).all()
    return [(r[0], r[1]) for r in rows]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest -p no:cacheprovider tests/proposal_assist/collab/test_collab_store.py::test_find_checkpoint_candidates_counts_rows_excludes_softdeleted -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

Run: `uv run ruff check src/call_manager/proposal_assist/collab/store.py tests/proposal_assist/collab/test_collab_store.py`
Then:
```bash
git add src/call_manager/proposal_assist/collab/store.py tests/proposal_assist/collab/test_collab_store.py
git commit -m "feat(collab): count-based checkpoint-candidate scan excluding soft-deleted (3b-5c)"
```

---

## Task 2: `record_version` param on `checkpoint_artifact` (quiet checkpoints)

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/checkpoint.py`
- Test: `tests/proposal_assist/collab/test_collab_checkpoint.py`

**Interfaces:**
- Consumes: existing `checkpoint_artifact(session, *, workspace_id, artifact, workspaces_dir, extra_files=None)`.
- Produces: same, plus `record_version: bool = True`. When `False`, still commits to git + advances watermark + compacts, but adds NO `ArtifactVersion` / `WorkspaceEvent`. Existing callers unchanged (default `True`).

- [ ] **Step 1: Write the failing test**

Add to `tests/proposal_assist/collab/test_collab_checkpoint.py` (reuses the module's `_init_repo` + `_seed_room_with_edit`):

```python
def test_checkpoint_record_version_false_is_quiet_and_compacts(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        o = make_user(s, email="cp-quiet@ki.se")
        ws = make_workspace(s, owner=o)
        s.commit()
        wid, oid = ws.id, o.id
    _init_repo(Path(tmp_path) / str(wid) / "repo", "## Background\noriginal.\n")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    # TWO authored edits (>= 2 rows) so the quiet path must STILL compact — proving
    # record_version=False did not accidentally skip the compaction block (the point
    # of 3b-5c is tail-bounding). Mirrors test_collab_compaction.py's 2-edit pattern.
    _seed_room_with_edit(pg_session_factory, wid, oid, "## Background\n\nedit one.\n")
    _seed_room_with_edit(
        pg_session_factory, wid, oid, "## Background\n\nedit one.\n\n## Aims\n\nedit two.\n"
    )
    with pg_session_factory() as s:
        max_id = store.load_rows(s, workspace_id=wid, artifact="draft")[-1][0]  # pre-checkpoint tail max

    with pg_session_factory() as s:
        res = checkpoint_artifact(
            s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path),
            record_version=False,
        )
        s.commit()

    assert res.committed is True                                   # git commit STILL made
    with pg_session_factory() as s:
        av = s.execute(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid)).scalars().all()
        ev = s.execute(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == wid, WorkspaceEvent.kind == "collab_checkpoint"
            )
        ).scalars().all()
        assert av == [] and ev == []                               # QUIET: no version / event
        rows = store.load_rows(s, workspace_id=wid, artifact="draft")
        assert len(rows) == 1                                      # COMPACTED 2 -> 1 on the quiet path
        assert state.get_checkpoint_watermark(s, workspace_id=wid, artifact="draft") == max_id
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest -p no:cacheprovider tests/proposal_assist/collab/test_collab_checkpoint.py::test_checkpoint_record_version_false_is_quiet_and_compacts -q`
Expected: FAIL — `checkpoint_artifact() got an unexpected keyword argument 'record_version'`.

- [ ] **Step 3: Implement the `record_version` gate**

In `checkpoint.py`, add the parameter to the signature:

```python
def checkpoint_artifact(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    artifact: str,
    workspaces_dir,
    extra_files: dict[str, str] | None = None,
    record_version: bool = True,
) -> CheckpointResult:
```

and gate the provenance rows (the existing `if changed:` block) on it:

```python
    changed = commit_sha != head
    if changed and record_version:
        session.add(
            ArtifactVersion(
                workspace_id=workspace_id,
                commit_sha=commit_sha,
                created_by="user",  # authors is non-empty on this path
                message=f"collab checkpoint: {artifact}",
            )
        )
        session.add(
            WorkspaceEvent(
                workspace_id=workspace_id,
                kind="collab_checkpoint",
                actor_id=authors[0] if len(authors) == 1 else None,
                payload={"artifact": artifact, "commit_sha": commit_sha, "authors": authors},
            )
        )
```

(Everything else — `write_and_commit_files`, `advance_checkpoint`, `compact_snapshot` — is unchanged, so a quiet checkpoint still commits, advances the watermark, and compacts.)

- [ ] **Step 4: Run the new test AND the existing checkpoint tests (regression pin)**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest -p no:cacheprovider tests/proposal_assist/collab/test_collab_checkpoint.py -q`
Expected: PASS — the new quiet test passes, and the existing `test_checkpoint_commits_serialized_doc_with_provenance` (default `record_version=True`) still records exactly one `ArtifactVersion` + one `WorkspaceEvent`, confirming the default path is unchanged.

- [ ] **Step 5: Lint + commit**

Run: `uv run ruff check src/call_manager/proposal_assist/collab/checkpoint.py tests/proposal_assist/collab/test_collab_checkpoint.py`
Then:
```bash
git add src/call_manager/proposal_assist/collab/checkpoint.py tests/proposal_assist/collab/test_collab_checkpoint.py
git commit -m "feat(collab): checkpoint_artifact record_version flag for quiet auto-checkpoints (3b-5c)"
```

---

## Task 3: `checkpoint_active_tails` job body (scan → recheck → quiet checkpoint, error-isolated)

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/checkpoint.py`
- Test: `tests/proposal_assist/collab/test_collab_checkpoint_scan.py` (new)

**Interfaces:**
- Consumes: `store.find_checkpoint_candidates` (Task 1), `checkpoint_artifact(..., record_version=False)` (Task 2), `Workspace.soft_deleted_at`.
- Produces: `checkpoint_active_tails(session_factory, *, workspaces_dir, min_tail: int) -> int` — scans candidates, re-checks each is a live workspace, quiet-checkpoints each in its own transaction; returns the count checkpointed. Per-artifact error isolation (a git error on one never aborts the others).

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/collab/test_collab_checkpoint_scan.py`:

```python
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from call_manager.db.models import ArtifactVersion, Workspace, WorkspaceEvent
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.collab import checkpoint as cp
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from tests.proposal_assist.conftest import make_user, make_workspace


def _init_repo(repo: Path, content: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=repo, check=True)
    (repo / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _seed_edit(pg_session_factory, wid, actor_id, md) -> None:
    doc = pm_json_to_ydoc(markdown_to_pm(md))
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=actor_id, update=doc.get_update()
        )
        s.commit()


def _mk(pg_session_factory, tmp_path, email, md, *, soft_deleted=False):
    with pg_session_factory() as s:
        o = make_user(s, email=email)
        ws = make_workspace(s, owner=o)
        s.commit()
        wid, oid = ws.id, o.id
    _init_repo(Path(tmp_path) / str(wid) / "repo", "## Background\noriginal.\n")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        if soft_deleted:
            s.get(Workspace, wid).soft_deleted_at = datetime.now(UTC)
        s.commit()
    _seed_edit(pg_session_factory, wid, oid, md)
    return wid


def test_checkpoint_active_tails_quietly_checkpoints_candidate(pg_session_factory, tmp_path):
    wid = _mk(pg_session_factory, tmp_path, "cat-a@ki.se", "## Background\n\nedited by A.\n")

    n = cp.checkpoint_active_tails(pg_session_factory, workspaces_dir=str(tmp_path), min_tail=1)

    assert n == 1
    disk = (Path(tmp_path) / str(wid) / "repo" / PROPOSAL_FILENAME).read_text()
    assert "edited by A." in disk                       # committed to git
    with pg_session_factory() as s:
        assert s.execute(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid)).scalars().all() == []
        assert s.execute(
            select(WorkspaceEvent).where(WorkspaceEvent.workspace_id == wid, WorkspaceEvent.kind == "collab_checkpoint")
        ).scalars().all() == []                          # QUIET
        assert state.get_checkpoint_watermark(s, workspace_id=wid, artifact="draft") == 1


def test_checkpoint_active_tails_rechecks_soft_delete(pg_session_factory, tmp_path, monkeypatch):
    # A candidate that is soft-deleted between scan and checkpoint must be skipped by
    # the per-candidate recheck (mirrors make_checkpoint_on_evict). Force the finder to
    # return a soft-deleted workspace to exercise the recheck directly.
    wid = _mk(pg_session_factory, tmp_path, "cat-sd@ki.se", "## X\n\nedited.\n", soft_deleted=True)
    monkeypatch.setattr(store, "find_checkpoint_candidates", lambda s, *, min_tail: [(wid, "draft")])

    n = cp.checkpoint_active_tails(pg_session_factory, workspaces_dir=str(tmp_path), min_tail=1)

    assert n == 0
    disk = (Path(tmp_path) / str(wid) / "repo" / PROPOSAL_FILENAME).read_text()
    assert disk == "## Background\noriginal.\n"          # NOT committed
    with pg_session_factory() as s:
        assert state.get_checkpoint_watermark(s, workspace_id=wid, artifact="draft") == 0


def test_checkpoint_active_tails_isolates_a_failing_artifact(pg_session_factory, tmp_path, monkeypatch):
    wid_bad = _mk(pg_session_factory, tmp_path, "cat-bad@ki.se", "## X\n\nbad.\n")
    wid_ok = _mk(pg_session_factory, tmp_path, "cat-ok@ki.se", "## Y\n\nok.\n")
    real = cp.checkpoint_artifact

    def _flaky(session, *, workspace_id, **kw):
        if workspace_id == wid_bad:
            raise RuntimeError("boom")
        return real(session, workspace_id=workspace_id, **kw)

    monkeypatch.setattr(cp, "checkpoint_artifact", _flaky)

    n = cp.checkpoint_active_tails(pg_session_factory, workspaces_dir=str(tmp_path), min_tail=1)

    assert n == 1                                        # the healthy one still ran
    ok_disk = (Path(tmp_path) / str(wid_ok) / "repo" / PROPOSAL_FILENAME).read_text()
    assert "ok." in ok_disk
    bad_disk = (Path(tmp_path) / str(wid_bad) / "repo" / PROPOSAL_FILENAME).read_text()
    assert bad_disk == "## Background\noriginal.\n"      # failed one rolled back, uncommitted
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest -p no:cacheprovider tests/proposal_assist/collab/test_collab_checkpoint_scan.py -q`
Expected: FAIL — `module ... has no attribute 'checkpoint_active_tails'`.

- [ ] **Step 3: Implement `checkpoint_active_tails`**

In `checkpoint.py`, add a module logger (top of file) and the job body:

```python
import logging

logger = logging.getLogger(__name__)


def checkpoint_active_tails(session_factory, *, workspaces_dir, min_tail: int) -> int:
    """Periodic tail-bound (Slice 3b-5c): scan for growing collab logs and quietly
    checkpoint each. Runs on a BackgroundScheduler thread — pure DB+git, never
    touches the asyncio room MANAGER. Each candidate is checkpointed in its OWN
    transaction so one artifact's failure never aborts the others; errors are logged
    and swallowed (best-effort maintenance)."""
    with session_factory() as s:
        candidates = store.find_checkpoint_candidates(s, min_tail=min_tail)
    done = 0
    for workspace_id, artifact in candidates:
        with session_factory() as s:
            # Re-check liveness (mirrors permissions.make_checkpoint_on_evict): a
            # workspace soft-deleted between scan and now must not be checkpointed.
            ws = s.get(Workspace, workspace_id)
            if ws is None or ws.soft_deleted_at is not None:
                continue
            try:
                checkpoint_artifact(
                    s,
                    workspace_id=workspace_id,
                    artifact=artifact,
                    workspaces_dir=workspaces_dir,
                    record_version=False,
                )
                s.commit()
                done += 1
            except Exception:  # noqa: BLE001 — best-effort; isolate per artifact
                s.rollback()
                logger.warning(
                    "collab.checkpoint_tail_failed",
                    extra={
                        "event": "collab_checkpoint_tail_failed",
                        "workspace_id": str(workspace_id),
                        "artifact": artifact,
                    },
                    exc_info=True,
                )
    return done
```

Add the needed imports at the top of `checkpoint.py`: `from call_manager.db.models import Workspace` (extend the existing `from call_manager.db.models import ArtifactVersion, WorkspaceEvent`), and `from call_manager.proposal_assist.collab import state, store` already exists.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest -p no:cacheprovider tests/proposal_assist/collab/test_collab_checkpoint_scan.py -q`
Expected: PASS (all three).

- [ ] **Step 5: Lint + commit**

Run: `uv run ruff check src/call_manager/proposal_assist/collab/checkpoint.py tests/proposal_assist/collab/test_collab_checkpoint_scan.py`
Then:
```bash
git add src/call_manager/proposal_assist/collab/checkpoint.py tests/proposal_assist/collab/test_collab_checkpoint_scan.py
git commit -m "feat(collab): checkpoint_active_tails periodic job body (scan+recheck+quiet, isolated) (3b-5c)"
```

---

## Task 4: Settings + scheduler registration

**Files:**
- Modify: `src/call_manager/config.py`
- Modify: `src/call_manager/scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `checkpoint_active_tails` (Task 3), `settings.workspaces_dir`, `session_factory`.
- Produces: two settings (`collab_checkpoint_scan_seconds: int = 60`, `collab_checkpoint_min_tail: int = 200`); a scheduler job with id `collab:checkpoint_tails` registered when `collab_checkpoint_scan_seconds > 0`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_scheduler.py` (mirrors `test_build_scheduler_includes_rank_drain_job`):

```python
def test_build_scheduler_wires_collab_checkpoint_job(_settings_env, monkeypatch):
    from unittest.mock import MagicMock

    import call_manager.proposal_assist.collab.checkpoint as cp
    from call_manager.config import Settings
    from call_manager.proposal_assist.drafting.factory import build_workspace_controller

    settings = Settings()

    def session_factory():
        return None

    workspace_controller = build_workspace_controller(settings=settings, session_factory=session_factory)
    sched = build_scheduler(
        session_factory=session_factory,
        settings=settings,
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=MagicMock(model="bge-m3"),
        workspace_controller=workspace_controller,
        start=False,
    )
    job = sched.get_job("collab:checkpoint_tails")
    assert job is not None
    # wired with the CONFIGURED interval...
    assert job.trigger.interval.total_seconds() == settings.collab_checkpoint_scan_seconds
    # ...and the job fn passes the configured session_factory / workspaces_dir / min_tail.
    captured = {}
    monkeypatch.setattr(
        cp, "checkpoint_active_tails",
        lambda sf, *, workspaces_dir, min_tail: captured.update(
            sf=sf, workspaces_dir=workspaces_dir, min_tail=min_tail
        ),
    )
    job.func()   # the closure lazy-imports checkpoint_active_tails → picks up the patch
    assert captured == {
        "sf": session_factory,
        "workspaces_dir": settings.workspaces_dir,
        "min_tail": settings.collab_checkpoint_min_tail,
    }


def test_build_scheduler_omits_collab_checkpoint_when_disabled(_settings_env, monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.setenv("COLLAB_CHECKPOINT_SCAN_SECONDS", "0")
    from call_manager.config import Settings
    from call_manager.proposal_assist.drafting.factory import build_workspace_controller

    settings = Settings()

    def session_factory():
        return None

    workspace_controller = build_workspace_controller(settings=settings, session_factory=session_factory)
    sched = build_scheduler(
        session_factory=session_factory,
        settings=settings,
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=MagicMock(model="bge-m3"),
        workspace_controller=workspace_controller,
        start=False,
    )
    assert sched.get_job("collab:checkpoint_tails") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest -p no:cacheprovider tests/test_scheduler.py::test_build_scheduler_wires_collab_checkpoint_job -q`
Expected: FAIL — `get_job("collab:checkpoint_tails")` is `None` (job not registered yet).

- [ ] **Step 3: Add the settings**

In `src/call_manager/config.py`, add to the `Settings` class (near the other tick settings, e.g. after `workspace_idle_sweep_seconds`):

```python
    collab_checkpoint_scan_seconds: int = Field(
        default=60,
        alias="COLLAB_CHECKPOINT_SCAN_SECONDS",
        description=(
            "Interval for the collab tail-bounding scan (Slice 3b-5c). 0 disables the "
            "periodic job entirely."
        ),
    )
    collab_checkpoint_min_tail: int = Field(
        default=200,
        alias="COLLAB_CHECKPOINT_MIN_TAIL",
        description="Min uncheckpointed Yjs-log rows before a collab room is auto-checkpointed.",
    )
```

- [ ] **Step 4: Register the job**

In `src/call_manager/scheduler.py` `build_scheduler`, after the existing job registrations (e.g. after the rank-drain job), add:

```python
    if settings.collab_checkpoint_scan_seconds > 0:
        def _collab_checkpoint_job() -> None:
            from call_manager.proposal_assist.collab.checkpoint import checkpoint_active_tails

            checkpoint_active_tails(
                session_factory,
                workspaces_dir=settings.workspaces_dir,
                min_tail=settings.collab_checkpoint_min_tail,
            )

        sched.add_job(
            _collab_checkpoint_job,
            trigger=IntervalTrigger(seconds=settings.collab_checkpoint_scan_seconds),
            id="collab:checkpoint_tails",
            name="Collab active-session tail-bounding checkpoint",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
```

(`IntervalTrigger` is already imported — the rank-drain job uses it. The lazy import of `checkpoint_active_tails` inside the closure mirrors the `_golden_tick` pattern and avoids any import cycle via collab → db → web at module load.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest -p no:cacheprovider tests/test_scheduler.py -q`
Expected: PASS — the job is present with default settings, absent when `COLLAB_CHECKPOINT_SCAN_SECONDS=0`, and the existing scheduler tests still pass (the new job doesn't disturb the other job-id sets).

- [ ] **Step 6: Lint + commit**

Run: `uv run ruff check src/call_manager/config.py src/call_manager/scheduler.py tests/test_scheduler.py`
Then:
```bash
git add src/call_manager/config.py src/call_manager/scheduler.py tests/test_scheduler.py
git commit -m "feat(collab): register periodic tail-bounding scheduler job + settings (3b-5c)"
```

---

## Final Verification (before stop-before-merge)

- [ ] **Backend grouped-dir run incl. the guards:**
  `EMBEDDING_MODEL=bge-m3 uv run pytest -p no:cacheprovider tests/proposal_assist/collab/ tests/test_scheduler.py tests/test_api_spec.py -q`
  Expected: all pass. (`test_api_spec.py` guards against unintended schema/OpenAPI drift — this slice must not change it; `test_scheduler.py` covers registration.)
- [ ] **Backend lint:** `uv run ruff check .` — clean.
- [ ] **No-schema / no-migration check:** `git diff --stat master...HEAD` shows only `store.py`, `checkpoint.py`, `config.py`, `scheduler.py`, and the four test files. NO Alembic migration, NO model change, NO frontend, NO endpoint, NO `enable_collab` caller.
- [ ] **STOP before merge.** Report the 4 commit SHAs + subjects, the gate result lines, and the `git diff --stat`. Do not merge or push. If any red phase does not fail as predicted or a gate fails non-obviously, STOP and report rather than improvising.

---

## Self-Review

- **Spec coverage:** scan query → Task 1; `record_version` quiet → Task 2; job body (scan+recheck+isolation) → Task 3; scheduler registration + settings → Task 4. §3.2 quiet-version gap proven by Task 2's `av == [] and ev == []`, and tail-bounding proven by Task 2's `len(rows) == 1` (compaction still runs on the quiet path). Medium#1 (count not gap) proven by Task 1's ws1-excluded assertion. Medium#2 (soft-delete) proven by Task 1 (scan filter) + Task 3 (recheck). Task 4 proves the job is not just registered but wired with the configured interval + `session_factory`/`workspaces_dir`/`min_tail` (invokes `job.func()` against a monkeypatched `checkpoint_active_tails`).
- **Placeholders:** none — every step has complete code + exact commands.
- **Type/name consistency:** `find_checkpoint_candidates(session, *, min_tail)`, `checkpoint_active_tails(session_factory, *, workspaces_dir, min_tail)`, `record_version: bool = True`, `collab_checkpoint_scan_seconds`/`collab_checkpoint_min_tail`, id `collab:checkpoint_tails` — identical across tasks, tests, and the scheduler call.
- **Regression guards:** Task 2 Step 4 re-runs the existing checkpoint tests (default path unchanged); Task 4 Step 5 re-runs the full `test_scheduler.py`; Final Verification runs the whole collab dir + `test_api_spec.py`.
