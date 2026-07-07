# Collab Slice 3b-5d·D3 — Post-Checkpoint Tier-2 Prompt Coverage: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-07-07-collab-slice3b5d3-post-checkpoint-analysis-refire-design.md` (read it first; §2 documents the verified landscape, §3 the three components).

**Goal:** When a collab checkpoint advances a workspace's **draft** git HEAD, immediately enqueue per-section Tier-2 claim-analysis coverage (enqueue-only; the existing drain tick does the LLM work) — closing the up-to-one-reconcile-interval latency gap versus the legacy `save_section` path.

**Architecture:** One new shared helper `enqueue_uncovered_sections` in `analysis/queue.py` holds the "which HEAD sections need a job" loop. Two callers: a 6-line `changed and artifact == "draft"` hook in the checkpoint funnel (new behavior, prod-inert until the D4 flip), and `reconcile_analysis_coverage` refactored to delegate its inner loop (**must be byte-for-byte behavior-preserving — this is the flag-off parity crux**, because reconcile runs in prod today).

**Tech Stack:** Python 3.12 / SQLAlchemy 2.x sync sessions / Postgres partial unique index (`ON CONFLICT DO NOTHING`) / pytest + testcontainers PG.

## Global Constraints

- Feature flag **stays OFF**; all checkpoint paths are collab-gated → the hook is prod-inert until D4. The reconcile refactor **runs in prod today** — it must be a pure, behavior-preserving extraction, gated by the existing reconcile tests passing **unchanged**.
- **No migration, no schema change** — reuses `ClaimAnalysisJob` + `enqueue_section_job`. Backend-only; frontend untouched.
- No new locks; no lock-ordering changes (the enqueue runs inside the existing checkpoint transaction/commit-mutex).
- Work in an isolated worktree: `.worktrees/collab-slice3b5d3`, branch `collab-slice3b5d3` off `master` (create via superpowers:using-git-worktrees at execution time). Subagents: always `git -C <abs-worktree-path>` and verify the branch before committing.
- **Explicit-path `git add` only** (never `git add -A`/`.`). Merges are **local `--no-ff` only**, not pushed without an explicit ask. Do NOT invoke `/code-review` / `/ultrareview`.
- Commit trailer (last line of every commit body): `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` (attribution-accurate; supersedes the spec §7 Opus 4.8 line — precedent 111ebb4).
- Pytest: always `EMBEDDING_MODEL=bge-m3` and `-p no:cacheprovider`; **never run the whole suite** (testcontainers PG drops connections under load — a `connection refused` failure is infra, re-run that file isolated; an `UndefinedTable` failure is migration-ordering, not infra).
- Python commands via `uv run` from the repo root.

## File Structure

| File | Role in this slice |
|---|---|
| `src/call_manager/proposal_assist/analysis/queue.py` | Modify — add `_COVERED_STATUSES` (moved from reconcile.py, comment travels verbatim) + new `enqueue_uncovered_sections` (Component A) |
| `src/call_manager/proposal_assist/analysis/reconcile.py` | Modify — inner loop delegates to the helper; local constant + now-unused imports removed (Component C) |
| `src/call_manager/proposal_assist/collab/checkpoint.py` | Modify — post-provenance hook in `checkpoint_artifact` (Component B) |
| `tests/test_analysis_queue.py` | Modify — append helper unit tests (this file already tests `analysis/queue.py`; note the spec §6 said `tests/proposal_assist/analysis/` but the queue/reconcile tests actually live at top-level `tests/`) |
| `tests/test_analysis_reconcile.py` | **Untouched** — passing unchanged is Task 2's regression gate |
| `tests/proposal_assist/collab/test_collab_checkpoint_analysis.py` | Create — checkpoint-hook integration tests |

---

### Task 1: `enqueue_uncovered_sections` helper (Component A)

**Files:**
- Modify: `src/call_manager/proposal_assist/analysis/queue.py` (insert after `enqueue_section_job`, i.e. after line 68, before `enqueue_containing_section`)
- Test: `tests/test_analysis_queue.py` (append)

**Interfaces:**
- Consumes: existing `enqueue_section_job(session, *, workspace_id, section_index, section_heading, section_sha, commit_sha) -> bool` (True only when **this call inserted**; False on conflict-skip) and `split_sections(doc) -> list[Section]` (fields: `.index`, `.heading`, `.sha`, `.text`; section 0 is the preamble, empty `""` when the doc starts with a heading).
- Produces: `enqueue_uncovered_sections(session: Session, *, workspace_id: uuid.UUID, doc: str, commit_sha: str) -> int` and module-level `_COVERED_STATUSES = ("queued", "running", "done")` — both in `analysis/queue.py`. Tasks 2 and 3 import the function by exactly this name.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_analysis_queue.py`. Two import changes at the top of the file: add `import pytest` (not currently imported), extend the queue import with `enqueue_uncovered_sections`, and add the sectionizer import:

```python
import pytest
```

```python
from call_manager.proposal_assist.analysis.queue import (
    claim_queued_jobs,
    enqueue_section_job,
    enqueue_uncovered_sections,
    mark_done,
    mark_failed,
    mark_superseded,
    requeue_stuck_running,
    touch_running_job,
)
from call_manager.proposal_assist.editing.sectionizer import split_sections
```

Then append at the end of the file:

```python
# --- enqueue_uncovered_sections (3b-5d.D3) --------------------------------- #
# Shared coverage loop: one job per uncovered non-empty section of a supplied
# markdown doc. Callers: the collab checkpoint hook (immediacy) and the
# reconcile tick (safety net).

# Empty preamble (section 0 is "") + Alpha + Beta + a byte-identical duplicate
# of Alpha (same text → same sha → must dedup to ONE job).
UNCOVERED_DOC = (
    "## Alpha\nAlpha makes a factual claim.\n"
    "## Beta\nBeta makes another claim.\n"
    "## Alpha\nAlpha makes a factual claim.\n"
)


def _live_unique_shas(doc):
    return {s.sha for s in split_sections(doc) if s.text.strip()}


def test_uncovered_sections_enqueued_once_each(pg_session_factory):
    """One job per unique non-empty section: the empty preamble is skipped,
    the duplicated Alpha section gets ONE row, the insert count is returned,
    and every row is queued pinned to the supplied commit_sha."""
    with pg_session_factory() as s:
        ws = _ws(s)
        n = enqueue_uncovered_sections(
            s, workspace_id=ws.id, doc=UNCOVERED_DOC, commit_sha="c1"
        )
        s.commit()
        assert n == 2  # Alpha, Beta — dup Alpha deduped, preamble skipped
        jobs = s.query(ClaimAnalysisJob).filter_by(workspace_id=ws.id).all()
        assert {j.section_sha for j in jobs} == _live_unique_shas(UNCOVERED_DOC)
        assert all(j.status == "queued" and j.commit_sha == "c1" for j in jobs)


@pytest.mark.parametrize("status", ["queued", "running", "done"])
def test_covered_section_is_skipped(pg_session_factory, status):
    """covered = queued/running/done (_COVERED_STATUSES): a pre-existing job in
    any of those states keeps its section out; only Beta is enqueued."""
    alpha_sha = split_sections(UNCOVERED_DOC)[1].sha
    beta_sha = split_sections(UNCOVERED_DOC)[2].sha
    with pg_session_factory() as s:
        ws = _ws(s)
        s.add(
            ClaimAnalysisJob(
                workspace_id=ws.id,
                section_index=1,
                section_heading="Alpha",
                section_sha=alpha_sha,
                commit_sha="seed",
                status=status,
            )
        )
        s.flush()
        n = enqueue_uncovered_sections(
            s, workspace_id=ws.id, doc=UNCOVERED_DOC, commit_sha="c2"
        )
        assert n == 1
        new = s.query(ClaimAnalysisJob).filter_by(workspace_id=ws.id, commit_sha="c2").all()
        assert [j.section_sha for j in new] == [beta_sha]


def test_failed_is_not_covered(pg_session_factory):
    """DELIBERATE SEMANTIC: FAILED is absent from _COVERED_STATUSES — a
    dead-lettered job's content gets a fresh row with a fresh attempt budget."""
    alpha_sha = split_sections(UNCOVERED_DOC)[1].sha
    with pg_session_factory() as s:
        ws = _ws(s)
        s.add(
            ClaimAnalysisJob(
                workspace_id=ws.id,
                section_index=1,
                section_heading="Alpha",
                section_sha=alpha_sha,
                commit_sha="seed",
                status="failed",
            )
        )
        s.flush()
        n = enqueue_uncovered_sections(
            s, workspace_id=ws.id, doc=UNCOVERED_DOC, commit_sha="c2"
        )
        assert n == 2  # Alpha re-enqueued despite the failed row, plus Beta


def test_second_identical_call_is_idempotent(pg_session_factory):
    with pg_session_factory() as s:
        ws = _ws(s)
        assert (
            enqueue_uncovered_sections(
                s, workspace_id=ws.id, doc=UNCOVERED_DOC, commit_sha="c1"
            )
            == 2
        )
        assert (
            enqueue_uncovered_sections(
                s, workspace_id=ws.id, doc=UNCOVERED_DOC, commit_sha="c1"
            )
            == 0
        )


def test_does_not_commit(pg_session_factory):
    """The helper stages inserts on the caller's transaction and never commits:
    rows are visible in-transaction and GONE after rollback."""
    with pg_session_factory() as s:
        ws = _ws(s)
        s.commit()  # persist the workspace so the enqueue is the only pending work
        ws_id = ws.id
        n = enqueue_uncovered_sections(
            s, workspace_id=ws_id, doc=UNCOVERED_DOC, commit_sha="c1"
        )
        assert n == 2
        assert s.query(ClaimAnalysisJob).filter_by(workspace_id=ws_id).count() == 2
        s.rollback()
        assert s.query(ClaimAnalysisJob).filter_by(workspace_id=ws_id).count() == 0
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/test_analysis_queue.py -p no:cacheprovider -q`
Expected: **collection error** — `ImportError: cannot import name 'enqueue_uncovered_sections'`.

- [ ] **Step 3: Implement the helper**

In `src/call_manager/proposal_assist/analysis/queue.py`:

(a) extend the sqlalchemy import (line 15) —

```python
from sqlalchemy import select, text
```

(b) insert after `enqueue_section_job` (after its closing `return result.inserted_primary_key is not None`, before `enqueue_containing_section`). The `_COVERED_STATUSES` comment is **moved verbatim** from `reconcile.py:20-25` (only the "this tick" phrase generalized, since the loop now serves two callers):

```python
# A section is "covered" when a job for its exact content is queued, running,
# or done. FAILED is deliberately absent: a dead-lettered job's content gets
# another chance via the coverage loop (fresh row, fresh attempt budget).
# SUPERSEDED is likewise absent, but a superseded sha only reappears at HEAD
# via a content revert, which deserves a re-run anyway.
_COVERED_STATUSES = ("queued", "running", "done")


def enqueue_uncovered_sections(
    session: Session, *, workspace_id: uuid.UUID, doc: str, commit_sha: str
) -> int:
    """Enqueue a claim-analysis job for every non-empty section of *doc* whose
    content sha has no queued/running/done job. Content-keyed idempotent; dedups
    duplicate-content sections; reads nothing (caller supplies the markdown) and
    does NOT commit (caller owns the transaction). Returns the number inserted.

    Callers: the collab checkpoint hook (3b-5d.D3 immediacy) and the reconcile
    tick (the always-on safety net) — one source of truth for the coverage
    predicate."""
    covered = set(
        session.execute(
            select(ClaimAnalysisJob.section_sha).where(
                ClaimAnalysisJob.workspace_id == workspace_id,
                ClaimAnalysisJob.status.in_(_COVERED_STATUSES),
            )
        ).scalars()
    )
    inserted = 0
    for sec in split_sections(doc):
        if not sec.text.strip():
            continue  # empty preamble/section — nothing to analyze
        if sec.sha in covered:
            continue
        if enqueue_section_job(
            session,
            workspace_id=workspace_id,
            section_index=sec.index,
            section_heading=sec.heading,
            section_sha=sec.sha,
            commit_sha=commit_sha,
        ):
            inserted += 1
        # Duplicate sections share content (and a sha) — enqueue once.
        covered.add(sec.sha)
    return inserted
```

Note: `uuid`, `Session`, `ClaimAnalysisJob`, and `split_sections` are already imported in this module — only `select` is new.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/test_analysis_queue.py -p no:cacheprovider -q`
Expected: all PASS — 19 total (the 12 pre-existing tests + the 7 new ones: 5 new functions, one of them parametrized 3-way).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/call_manager/proposal_assist/analysis/queue.py tests/test_analysis_queue.py
git add src/call_manager/proposal_assist/analysis/queue.py tests/test_analysis_queue.py
git commit -m "$(cat <<'EOF'
feat(analysis): enqueue_uncovered_sections shared coverage helper (3b-5d.D3)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: reconcile refactor — delegate the inner loop (Component C)

**Files:**
- Modify: `src/call_manager/proposal_assist/analysis/reconcile.py` (whole file shown below)
- Test: `tests/test_analysis_reconcile.py` — **do not edit**; passing unchanged IS the gate

**Interfaces:**
- Consumes: `enqueue_uncovered_sections(session, *, workspace_id, doc, commit_sha) -> int` from Task 1.
- Produces: `reconcile_analysis_coverage(*, session_factory, workspaces_dir) -> int` — signature and behavior unchanged (this is what the APScheduler `_analysis_reconcile_tick` calls in prod today).

This is a **pure, behavior-preserving extraction** — the flag-off parity crux of the slice. Same covered predicate, same skip-empty, same dedup, same per-workspace covered query, same one-commit-at-the-end. If any existing reconcile test needs editing to pass, the refactor is wrong — stop and fix the refactor, never the test.

- [ ] **Step 1: Baseline — existing tests green before touching anything**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/test_analysis_reconcile.py -p no:cacheprovider -q`
Expected: 3 PASS.

- [ ] **Step 2: Replace `reconcile.py` with the delegating version**

Full new content of `src/call_manager/proposal_assist/analysis/reconcile.py`:

```python
"""Self-healing coverage tick (spec §3): every HEAD section of every
drafting workspace gets a job row eventually, even if the save-time enqueue
was lost to the post-commit DB-failure window (spec §6)."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select

from call_manager.db.models import Workspace
from call_manager.proposal_assist.analysis.queue import enqueue_uncovered_sections
from call_manager.proposal_assist.citations.gitio import GitCommitError, current_head
from call_manager.proposal_assist.paths import workspace_repo_dir

logger = logging.getLogger(__name__)


def reconcile_analysis_coverage(*, session_factory, workspaces_dir) -> int:
    """Enqueue an analysis job for every live HEAD section that has no
    queued/running/done job. Returns the number of jobs inserted.

    One session, one commit at the end. Workspaces that are soft-deleted,
    have no proposal.md, or have an unreadable HEAD are skipped without
    raising — the next tick retries. The covered/uncovered predicate lives
    in queue.enqueue_uncovered_sections (shared with the collab checkpoint
    hook, 3b-5d.D3).
    """
    inserted = 0
    with session_factory() as session:
        ws_ids = (
            session.execute(select(Workspace.id).where(Workspace.soft_deleted_at.is_(None)))
            .scalars()
            .all()
        )
        for ws_id in ws_ids:
            repo = workspace_repo_dir(Path(workspaces_dir), ws_id)
            try:
                doc = (repo / "proposal.md").read_text(encoding="utf-8")
            except OSError:
                # No repo / no proposal yet (workspace still in setup) — skip.
                continue
            try:
                head = current_head(repo)
            except GitCommitError:
                logger.debug(
                    "analysis reconcile: cannot read HEAD for workspace %s — skipped",
                    ws_id,
                    exc_info=True,
                )
                continue
            inserted += enqueue_uncovered_sections(
                session, workspace_id=ws_id, doc=doc, commit_sha=head
            )
        session.commit()
    return inserted
```

What changed vs. the old file: the covered-query + section loop is gone (now inside the helper); `_COVERED_STATUSES` and its comment are gone (moved to `queue.py` in Task 1); imports of `ClaimAnalysisJob`, `enqueue_section_job`, and `split_sections` are dropped. Everything else — docstrings, skip semantics, logging, commit placement — is verbatim.

- [ ] **Step 3: Run the untouched regression tests**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/test_analysis_reconcile.py tests/test_analysis_queue.py tests/test_analysis_pipeline.py -p no:cacheprovider -q`
Expected: all PASS with **zero test-file edits** (verify with `git status -- tests/` → clean).

- [ ] **Step 4: Lint and commit**

```bash
uv run ruff check src/call_manager/proposal_assist/analysis/reconcile.py
git add src/call_manager/proposal_assist/analysis/reconcile.py
git commit -m "$(cat <<'EOF'
refactor(analysis): reconcile delegates to enqueue_uncovered_sections (3b-5d.D3)

Behavior-preserving extraction; existing reconcile tests pass unchanged.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: checkpoint hook (Component B)

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/checkpoint.py` (one import + a 6-line block after the provenance writes)
- Create: `tests/proposal_assist/collab/test_collab_checkpoint_analysis.py`

**Interfaces:**
- Consumes: `enqueue_uncovered_sections` (Task 1); in-scope locals of `checkpoint_artifact` — `changed: bool` (line 118), `md: str` (the serialized markdown, byte-identical to the new git HEAD), `commit_sha: str`, `artifact: str` (`"draft"`/`"spec"`), `session`, `workspace_id`.
- Produces: no new API — `checkpoint_artifact`'s signature and `CheckpointResult` are unchanged. After this task, any draft checkpoint that advances git leaves `ClaimAnalysisJob` rows (status `queued`, pinned to the checkpoint `commit_sha`) for every uncovered non-empty section.

No import cycle: `analysis/queue.py` imports only `db.models`, `artifacts`, `editing.sectionizer`, `paths` — nothing from `collab`.

- [ ] **Step 1: Write the failing integration tests**

Create `tests/proposal_assist/collab/test_collab_checkpoint_analysis.py` (helpers mirror `test_collab_checkpoint.py` — house pattern is module-local copies, `fixtures.py` there is a bridge-corpus, not repo helpers):

```python
"""3b-5d.D3: the checkpoint funnel enqueues Tier-2 claim-analysis coverage for
just-committed DRAFT content (spec §3.2). Gate is `changed and artifact ==
"draft"` — NOT record_version, so the silent tail-scan path (3b-5c) is covered
too. Enqueue-only: the drain tick does the LLM work later."""

import subprocess
from pathlib import Path

from pycrdt import Doc
from sqlalchemy import select

from call_manager.db.models import ArtifactVersion, ClaimAnalysisJob
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.checkpoint import (
    checkpoint_artifact,
    serialize_doc_to_markdown,
)
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from call_manager.proposal_assist.editing.sectionizer import split_sections
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


def _seed_room_edit(pg_session_factory, wid, actor_id, md, *, artifact="draft") -> Doc:
    doc = pm_json_to_ydoc(markdown_to_pm(md))
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact=artifact, actor_id=actor_id, update=doc.get_update()
        )
        s.commit()
    return doc


def _setup_ws(pg_session_factory, tmp_path, *, email, artifact="draft"):
    with pg_session_factory() as s:
        o = make_user(s, email=email)
        ws = make_workspace(s, owner=o)
        s.commit()
        wid, oid = ws.id, o.id
    _init_repo(Path(tmp_path) / str(wid) / "repo", "## Background\noriginal.\n")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact=artifact)
        s.commit()
    return wid, oid


def _jobs(session, wid):
    return (
        session.execute(select(ClaimAnalysisJob).where(ClaimAnalysisJob.workspace_id == wid))
        .scalars()
        .all()
    )


def _live_shas(md: str) -> set[str]:
    return {sec.sha for sec in split_sections(md) if sec.text.strip()}


def test_draft_checkpoint_enqueues_coverage_for_new_sections(pg_session_factory, tmp_path):
    """A draft checkpoint that advances git enqueues one queued job per
    non-empty section of the serialized doc, pinned to the checkpoint's
    commit_sha (spec §6 case 1)."""
    wid, oid = _setup_ws(pg_session_factory, tmp_path, email="d3-draft@ki.se")
    doc = _seed_room_edit(
        pg_session_factory,
        wid,
        oid,
        "## Background\n\nrewritten claim.\n\n## Aims\n\nsecond claim.\n",
    )
    expected_shas = _live_shas(serialize_doc_to_markdown(doc))
    assert len(expected_shas) == 2  # sanity: two distinct non-empty sections

    with pg_session_factory() as s:
        res = checkpoint_artifact(
            s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path)
        )
        s.commit()
    assert res.committed is True

    with pg_session_factory() as s:
        jobs = _jobs(s, wid)
        assert {j.section_sha for j in jobs} == expected_shas
        assert all(j.status == "queued" for j in jobs)
        assert all(j.commit_sha == res.commit_sha for j in jobs)


def test_spec_checkpoint_enqueues_nothing(pg_session_factory, tmp_path):
    """A SPEC checkpoint commits (changed=True) but enqueues no claim analysis
    (mirrors save_section: spec §3 has no claim analysis) — spec §6 case 2.
    Non-vacuous: the spec content's sections are uncovered, so without the
    artifact gate the hook WOULD insert."""
    wid, oid = _setup_ws(pg_session_factory, tmp_path, email="d3-spec@ki.se", artifact="spec")
    _seed_room_edit(
        pg_session_factory, wid, oid, "## Approach\n\nspec content.\n", artifact="spec"
    )
    with pg_session_factory() as s:
        res = checkpoint_artifact(
            s, workspace_id=wid, artifact="spec", workspaces_dir=str(tmp_path)
        )
        s.commit()
    assert res.committed is True  # a REAL spec commit — the guard is the artifact, not vacuity
    with pg_session_factory() as s:
        assert _jobs(s, wid) == []


def test_seed_only_checkpoint_enqueues_nothing(pg_session_factory, tmp_path):
    """The unauthored-seed early-return (checkpoint.py:91-106) never reaches the
    hook — no git commit, no jobs (spec §6 case 3)."""
    wid, _ = _setup_ws(pg_session_factory, tmp_path, email="d3-seed@ki.se")
    _seed_room_edit(pg_session_factory, wid, None, "## Background\n\noriginal.\n")
    with pg_session_factory() as s:
        res = checkpoint_artifact(
            s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path)
        )
        s.commit()
    assert res.committed is False
    with pg_session_factory() as s:
        assert _jobs(s, wid) == []


def test_unchanged_content_checkpoint_enqueues_nothing(pg_session_factory, tmp_path):
    """The gate is `changed`: a checkpoint whose serialized content equals HEAD
    (changed=False) must NOT enqueue (spec §6 case 4). Non-vacuous: coverage
    rows from the first checkpoint are DELETED, so if the hook ran on the
    second checkpoint the shas would be uncovered and rows WOULD appear.
    Also covers the max_id<=watermark no-op (spec §6 case 3, first variant)."""
    wid, oid = _setup_ws(pg_session_factory, tmp_path, email="d3-nochange@ki.se")
    doc = _seed_room_edit(pg_session_factory, wid, oid, "## Background\n\nedited once.\n")
    with pg_session_factory() as s:
        checkpoint_artifact(s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path))
        s.commit()
    with pg_session_factory() as s:
        assert len(_jobs(s, wid)) > 0  # checkpoint 1 enqueued
        s.query(ClaimAnalysisJob).filter_by(workspace_id=wid).delete()
        s.commit()

    # A new AUTHORED log row carrying the SAME full state: Yjs merge is
    # idempotent (same client ids), so the rebuilt doc serializes to content
    # identical to HEAD -> write_and_commit_files returns head -> changed=False.
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=oid, update=doc.get_update()
        )
        s.commit()
    with pg_session_factory() as s:
        res2 = checkpoint_artifact(
            s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path)
        )
        s.commit()
    assert res2.committed is False  # changed=False: real attempt, empty git diff
    with pg_session_factory() as s:
        assert _jobs(s, wid) == []

    # And the max_id<=watermark early-return (no new rows at all): still nothing.
    with pg_session_factory() as s:
        res3 = checkpoint_artifact(
            s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path)
        )
        s.commit()
    assert res3.committed is False
    with pg_session_factory() as s:
        assert _jobs(s, wid) == []


def test_scan_path_checkpoint_enqueues_coverage(pg_session_factory, tmp_path):
    """record_version=False (the periodic tail-scan, 3b-5c) still enqueues —
    the gate is `changed`, NOT record_version (spec §6 case 5). Quiet for
    provenance, loud for coverage."""
    wid, oid = _setup_ws(pg_session_factory, tmp_path, email="d3-scan@ki.se")
    doc = _seed_room_edit(pg_session_factory, wid, oid, "## Background\n\nscan edit.\n")
    expected_shas = _live_shas(serialize_doc_to_markdown(doc))

    with pg_session_factory() as s:
        res = checkpoint_artifact(
            s,
            workspace_id=wid,
            artifact="draft",
            workspaces_dir=str(tmp_path),
            record_version=False,
        )
        s.commit()
    assert res.committed is True

    with pg_session_factory() as s:
        av = (
            s.execute(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid))
            .scalars()
            .all()
        )
        assert av == []  # quiet: no provenance row
        jobs = _jobs(s, wid)
        assert {j.section_sha for j in jobs} == expected_shas  # loud: coverage
        assert all(j.commit_sha == res.commit_sha for j in jobs)
```

- [ ] **Step 2: Run the new tests to verify they fail the right way**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_checkpoint_analysis.py -p no:cacheprovider -q`
Expected: `test_draft_checkpoint_enqueues_coverage_for_new_sections`, `test_unchanged_content_checkpoint_enqueues_nothing` (first assert `len(_jobs(...)) > 0`), and `test_scan_path_checkpoint_enqueues_coverage` FAIL on empty job sets; the spec/seed negative tests PASS vacuously (that's fine — their value is regression once the hook exists).

- [ ] **Step 3: Implement the hook**

In `src/call_manager/proposal_assist/collab/checkpoint.py`:

(a) add the import, between the `call_manager.db.models` and `call_manager.proposal_assist.artifacts` imports (isort order):

```python
from call_manager.proposal_assist.analysis.queue import enqueue_uncovered_sections
```

(b) insert the hook immediately after the provenance block — i.e. after the `session.add(WorkspaceEvent(...))` call's closing `)` (line 135), before `state.advance_checkpoint(`:

```python
    if changed and artifact == "draft":
        # Prompt Tier-2 coverage for the just-committed draft (3b-5d.D3): mirror
        # save_section step 10, funnelled. `md` is the committed HEAD content;
        # idempotent, so covered sections are free no-ops. Gated on `changed`
        # (not record_version) so the silent tail-scan checkpoints are covered too.
        enqueue_uncovered_sections(
            session, workspace_id=workspace_id, doc=md, commit_sha=commit_sha
        )
```

No try/except (spec §5): the enqueue participates in the checkpoint transaction and fails-or-commits atomically with the provenance writes, matching `save_section` step 10; the scan path's per-artifact isolation (`checkpoint.py:181-201`) already contains a failure there.

- [ ] **Step 4: Run the tests to verify they pass, plus the collab group**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab -p no:cacheprovider -q`
Expected: all PASS (the 5 new tests plus the entire pre-existing collab group — the hook must not disturb any existing checkpoint/scan/kernel/evict test).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/call_manager/proposal_assist/collab/checkpoint.py tests/proposal_assist/collab/test_collab_checkpoint_analysis.py
git add src/call_manager/proposal_assist/collab/checkpoint.py tests/proposal_assist/collab/test_collab_checkpoint_analysis.py
git commit -m "$(cat <<'EOF'
feat(collab): checkpoint enqueues Tier-2 claim-analysis coverage (3b-5d.D3)

Draft checkpoints that advance git HEAD now enqueue per-section claim-analysis
jobs (enqueue-only; the drain tick does the LLM work) — closing the
up-to-one-reconcile-interval latency gap vs the save_section path.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Acceptance gates

**Files:** none created/modified — verification only. All gates from spec §6 must pass before the branch is offered for review/merge.

**Interfaces:** consumes everything above; produces the evidence for the merge decision.

- [ ] **Step 1: Grouped backend sweep (incl. the API-spec snapshot)**

Run:
```bash
EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/test_analysis_queue.py tests/test_analysis_reconcile.py \
  tests/test_analysis_pipeline.py tests/test_editing_e2e.py \
  tests/proposal_assist/collab tests/test_api_spec.py \
  -p no:cacheprovider -q
```
Expected: all PASS. (`tests/test_api_spec.py` is included explicitly — sweeps have skipped it before and it has caught 500s; a `connection refused` failure is testcontainers infra → re-run that file isolated.)

- [ ] **Step 2: Lint, whole repo**

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 3: Frontend regression (no frontend change in this slice)**

Run from `frontend/`:
```bash
pnpm exec tsc --noEmit
pnpm exec vitest run src/collab
```
Expected: both clean — proves the backend-only slice didn't move anything the SPA types or collab client tests depend on.

- [ ] **Step 4: Two-browser e2e (checkpoint-path regression)**

Run from the repo root: `scripts/collab_e2e_run.sh`
Expected: scenarios (a)–(e) PASS (scenario (a) exercises a real checkpoint through the live room). Requirements: `pnpm i` done in `frontend/`, Playwright Chromium cached at `~/.cache/ms-playwright`. This boots its own testcontainers PG + uvicorn:8000 + Vite:5173 and tears them down.

- [ ] **Step 5: Stop for review — do NOT merge**

Report gate results and stop. Merge (local `--no-ff` into `master`) happens only after the independent diff review, per house convention (3rd gate = adversarial diff review of `master..collab-slice3b5d3`).

---

## Spec-coverage map (self-review)

- Spec §3.1 Component A → Task 1 (helper + constant move with verbatim comment).
- Spec §3.2 Component B → Task 3 (hook, `changed`-not-`record_version`, draft-only, no re-read, no try/except).
- Spec §3.3 Component C → Task 2 (delegation, one-commit-at-end preserved, regression-gated).
- Spec §6 helper-unit list → Task 1 tests (per-section / skip-empty / dedup / skip-covered ×3 statuses + failed-not-covered / insert count / no-commit / idempotent).
- Spec §6 checkpoint-integration list → Task 3 tests (draft-enqueues / spec-nothing / no-op-seed-nothing incl. watermark variant / changed=False-nothing / scan-path-enqueues).
- Spec §6 acceptance gates → Task 4.
- Spec §1 exclusions (Tier-1, manual LLM families) → no task, by design — **do not add code for them**.
- Deviation from spec noted: helper tests live in `tests/test_analysis_queue.py` (where the queue/reconcile tests actually are), not the spec's `tests/proposal_assist/analysis/`.
