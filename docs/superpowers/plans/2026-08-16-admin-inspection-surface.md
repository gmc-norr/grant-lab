# Admin Inspection Surface (Retention Slice 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an admin list soft-deleted workspaces and read their surviving proposal content and git history, with every read audited.

**Architecture:** Two new admin-only JSON routes in a new module, backed by a new bounded git-log helper, plus one new React page under the existing admin shell. Reads only — nothing is created, modified, or removed anywhere. No database migration.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, pytest (+ testcontainers Postgres); React 19 + Mantine + TanStack Query + `openapi-fetch` on the frontend.

**Spec:** `docs/superpowers/specs/2026-08-16-admin-retention-surface-design.md` (revision 3, Slice 1 only).

## Global Constraints

- **Slice 1 removes nothing and adds no migration.** No `rmtree`, no purge route, no schema change, no tombstone columns. Those belong to deferred Slices 2 and 3 (spec §9).
- **Use `acquire_commit_mutex` (`src/call_manager/proposal_assist/drafting/locking.py:205`), NEVER `acquire_commit_mutex_live` (`:267`).** The `_live` variant does a liveness re-read and raises `WorkspaceDeleted` (`:292`), so it would refuse every workspace this surface exists to serve. This is the single easiest mistake in the slice.
- **Both routes return 404 unless the workspace has `soft_deleted_at IS NOT NULL`.** A live workspace must never be reachable through them.
- **Both routes require `require_admin`** (`src/call_manager/auth/dependencies.py:50`). A non-admin gets 403.
- **No inspection response containing proposal content may be returned until its audit event is committed.**
- **List pagination:** `limit` default 50, max 200; `offset` default 0; newest `soft_deleted_at` first; response carries a total count.
- **Commit history cap:** most recent 200 commits, with an explicit truncation flag in the response.
- Inspection must create no git commit and start no collab room.
- Work on a branch. Never commit to `master` directly. All Python through `uv run`.

## Environment notes

- **Fast test runs:** `uv run pytest -n 16 --dist loadfile` (~64s for the full suite). Single files run fine without `-n`.
- **Never pipe pytest through `tail` to read its exit code** — the pipeline's status is `tail`'s. Redirect to a file, then `echo $?`.
- **`pytest` exits 4 on "no tests ran"** — a path typo reads as success. Always confirm a nonzero collected count.
- **Route docstrings become OpenAPI descriptions**, and `frontend/src/api/schema.gen.ts` is a hard CI gate. After changing any route signature or docstring, regenerate (Task 4) with `EMBEDDING_MODEL=bge-m3` set, or an unrelated startup warning corrupts the output.
- **`frontend/routes.json` carries SPA route PREFIXES, not per-route entries.** `/admin` is already listed, so a new page underneath it needs **no** manifest change. Do not add one.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/call_manager/proposal_assist/citations/gitio.py` | gains `list_commits` — bounded, read-only git log | 1 |
| `src/call_manager/web/api/admin_retention.py` | **new** — both admin routes, their response models, the audit write | 2, 3 |
| `src/call_manager/main.py` | registers the new router | 2 |
| `frontend/src/routes/AdminDeletedWorkspaces.tsx` | **new** — list page | 4 |
| `frontend/src/routes/AdminDeletedWorkspaceDetail.tsx` | **new** — detail page | 4 |
| `frontend/src/router.tsx`, `frontend/src/routes/AdminNav.tsx` | wire the pages in | 4 |

---

### Task 1: Bounded git-log helper

`gitio.py` can read `HEAD` (`current_head`) and blobs, but cannot list history. Inspection needs a commit list, bounded, with truncation detectable.

**Files:**
- Modify: `src/call_manager/proposal_assist/citations/gitio.py`
- Test: `tests/proposal_assist/citations/test_gitio_list_commits.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `CommitInfo` (a `dataclass` with `sha: str`, `authored_at: datetime`, `subject: str`) and
  `list_commits(repo_dir: Path, *, limit: int) -> tuple[list[CommitInfo], bool]` — the list newest-first, and a `truncated` flag that is `True` when more commits exist beyond `limit`. Raises `GitCommitError` when the repository is missing or has no commits.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/citations/test_gitio_list_commits.py`:

```python
"""list_commits: bounded, newest-first, truncation detectable.

Inspection (retention Slice 1) shows an admin the history of a deleted
workspace. History grows with every save, so the read must be bounded at the
source rather than trimmed after the fact.
"""

import subprocess
from pathlib import Path

import pytest

from call_manager.proposal_assist.citations.gitio import (
    GitCommitError,
    list_commits,
)


def _repo(tmp_path: Path, n_commits: int) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    for i in range(n_commits):
        (repo / "proposal.md").write_text(f"# draft {i}\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", f"commit {i}"],
            cwd=repo,
            check=True,
        )
    return repo


def test_returns_commits_newest_first(tmp_path):
    repo = _repo(tmp_path, 3)
    commits, truncated = list_commits(repo, limit=10)
    assert [c.subject for c in commits] == ["commit 2", "commit 1", "commit 0"]
    assert truncated is False


def test_truncates_at_the_limit_and_says_so(tmp_path):
    repo = _repo(tmp_path, 5)
    commits, truncated = list_commits(repo, limit=2)
    assert [c.subject for c in commits] == ["commit 4", "commit 3"]
    assert truncated is True, (
        "a caller that cannot tell a capped list from a complete one will "
        "present a partial history as the whole history"
    )


def test_exactly_at_the_limit_is_not_truncated(tmp_path):
    """The off-by-one that matters: 2 commits with limit=2 is complete, not truncated."""
    repo = _repo(tmp_path, 2)
    commits, truncated = list_commits(repo, limit=2)
    assert len(commits) == 2
    assert truncated is False


def test_missing_repository_raises(tmp_path):
    with pytest.raises(GitCommitError):
        list_commits(tmp_path / "nope", limit=10)


def test_repository_with_no_commits_raises(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    with pytest.raises(GitCommitError):
        list_commits(repo, limit=10)


def test_a_subject_containing_the_field_separator_survives(tmp_path):
    """Commit subjects are user-controlled. A naive split on a common character
    would corrupt any subject containing it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    (repo / "proposal.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "fix: a|b|c pipes and \x1f bytes"],
        cwd=repo,
        check=True,
    )
    commits, _ = list_commits(repo, limit=10)
    assert commits[0].subject == "fix: a|b|c pipes and  bytes" or "a|b|c" in commits[0].subject
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/citations/test_gitio_list_commits.py -q > /tmp/t1.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t1.log
```

Expected: FAIL — `ImportError: cannot import name 'list_commits'`.

- [ ] **Step 3: Implement `list_commits`**

Add to `src/call_manager/proposal_assist/citations/gitio.py`. Match the file's existing conventions exactly: `_git_env()` for a hermetic environment, `_GIT_TIMEOUT_SECONDS` on the subprocess, and `GitCommitError` for failures.

```python
@dataclass(frozen=True)
class CommitInfo:
    """One entry of a workspace repository's history."""

    sha: str
    authored_at: datetime
    subject: str


# Record separator: a byte git will never emit inside a subject, so splitting on
# it cannot be corrupted by user-controlled commit messages. A pipe or comma can.
_LOG_FIELD_SEP = "\x1f"
_LOG_RECORD_SEP = "\x1e"


def list_commits(repo_dir: Path, *, limit: int) -> tuple[list[CommitInfo], bool]:
    """Return (commits newest-first, truncated).

    Bounded at the source: asks git for `limit + 1` and reports `truncated`
    when that extra row comes back, so a caller can tell a capped list from a
    complete one without a second call.

    Raises GitCommitError if the repository is missing, is not a repository, or
    has no commits (an unborn HEAD).
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "log",
                f"--max-count={limit + 1}",
                f"--format=%H{_LOG_FIELD_SEP}%aI{_LOG_FIELD_SEP}%s{_LOG_RECORD_SEP}",
            ],
            cwd=repo_dir,
            env=_git_env(),
            capture_output=True,
            text=True,
            check=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        _log_if_timeout(e)
        stderr = getattr(e, "stderr", "") or ""
        raise GitCommitError(
            f"cannot list commits in {repo_dir}: {stderr.strip()}"
        ) from e

    records = [r for r in proc.stdout.split(_LOG_RECORD_SEP) if r.strip()]
    truncated = len(records) > limit
    commits: list[CommitInfo] = []
    for record in records[:limit]:
        sha, authored, subject = record.strip("\n").split(_LOG_FIELD_SEP, 2)
        commits.append(
            CommitInfo(
                sha=sha,
                authored_at=datetime.fromisoformat(authored),
                subject=subject,
            )
        )
    return commits, truncated
```

Add `from dataclasses import dataclass` and `from datetime import datetime` to the module imports if they are not already present.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/citations/test_gitio_list_commits.py -q > /tmp/t1.log 2>&1; echo "EXIT=$?"; tail -5 /tmp/t1.log
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Expected: 6 passed, `RUFF_EXIT=0`.

- [ ] **Step 5: Prove the truncation flag is not vacuous**

Change `truncated = len(records) > limit` to `truncated = False`, re-run the file, and confirm `test_truncates_at_the_limit_and_says_so` FAILS. Restore, re-run, confirm green. Report both outcomes.

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add src/call_manager/proposal_assist/citations/gitio.py tests/proposal_assist/citations/test_gitio_list_commits.py
git commit -m "feat(gitio): bounded list_commits for retention inspection

Asks git for limit+1 so truncation is detectable without a second call.
Splits on \x1f/\x1e rather than a printable separator, because commit
subjects are user-controlled and a pipe would corrupt the parse."
```

---

### Task 2: Admin list endpoint

**Files:**
- Create: `src/call_manager/web/api/admin_retention.py`
- Modify: `src/call_manager/main.py` (register the router alongside the other `include_router` calls near line 726)
- Test: `tests/test_api_admin_retention_list.py` (create)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `router` (an `APIRouter`) exported from `admin_retention.py`, mounted so the path is `/api/admin/workspaces/deleted`. Task 3 adds its route to this same module and router.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api_admin_retention_list.py`:

```python
"""GET /api/admin/workspaces/deleted — the admin list of soft-deleted workspaces.

Retention Slice 1. Reads only; this endpoint can remove nothing.
"""

import uuid

from call_manager.db.models import Workspace

from tests.helpers import make_user, make_workspace   # existing helpers


def _delete(session, workspace_id):
    from datetime import datetime, timezone

    ws = session.get(Workspace, workspace_id)
    ws.soft_deleted_at = datetime.now(timezone.utc)
    session.commit()


def test_lists_only_deleted_workspaces(admin_client, pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s, email="o@ki.se")
        live = make_workspace(s, owner=owner, title="still here")
        dead = make_workspace(s, owner=owner, title="gone")
        s.commit()
        live_id, dead_id = live.id, dead.id
        _delete(s, dead_id)

    r = admin_client.get("/api/admin/workspaces/deleted")
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()["items"]]
    assert str(dead_id) in ids
    assert str(live_id) not in ids, (
        "a live workspace reachable through the admin retention surface would "
        "make it a back door around ownership"
    )


def test_non_admin_is_403(member_client):
    r = member_client.get("/api/admin/workspaces/deleted")
    assert r.status_code == 403


def test_pagination_caps_the_limit(admin_client):
    r = admin_client.get("/api/admin/workspaces/deleted?limit=5000")
    assert r.status_code == 422, "an uncapped limit lets one request read the whole table"


def test_returns_total_count_and_honours_offset(admin_client, pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s, email="o2@ki.se")
        made = [make_workspace(s, owner=owner, title=f"w{i}") for i in range(3)]
        s.commit()
        for w in made:
            _delete(s, w.id)

    first = admin_client.get("/api/admin/workspaces/deleted?limit=2&offset=0").json()
    second = admin_client.get("/api/admin/workspaces/deleted?limit=2&offset=2").json()
    assert first["total"] >= 3
    assert len(first["items"]) == 2
    assert first["items"][0]["id"] != second["items"][0]["id"]
```

If `admin_client` / `member_client` fixtures do not already exist in `tests/conftest.py`, build the clients inside each test the way `tests/test_api_admin_users.py` does — follow that file's existing pattern rather than inventing a new one.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/test_api_admin_retention_list.py -q > /tmp/t2.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t2.log
```

Expected: FAIL — 404 on every request, because the route does not exist.

- [ ] **Step 3: Create the module and the list route**

Create `src/call_manager/web/api/admin_retention.py`:

```python
"""Admin retention surface: read-only access to soft-deleted workspaces.

Retention Slice 1 (docs/superpowers/specs/2026-08-16-admin-retention-surface-design.md).
This module REMOVES NOTHING. Purge is deferred to Slice 3 and requires the
filesystem-activity barrier of Slice 2 first.

Both routes are deliberately separate from the owner-facing workspace routes.
Those gate on a LIVE row; widening them to serve deleted workspaces would put
the deleted case on the same code path as the live case, where a later edit
could leak one into the other.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from call_manager.auth.dependencies import require_admin
from call_manager.db.models import User, Workspace

router = APIRouter()

_LIST_LIMIT_DEFAULT = 50
_LIST_LIMIT_MAX = 200


class DeletedWorkspaceRow(BaseModel):
    id: uuid.UUID
    title: str
    owner_email: str | None
    soft_deleted_at: datetime


class DeletedWorkspaceListOut(BaseModel):
    items: list[DeletedWorkspaceRow]
    total: int


@router.get("/api/admin/workspaces/deleted", response_model=DeletedWorkspaceListOut)
def list_deleted_workspaces(
    request: Request,
    limit: int = Query(_LIST_LIMIT_DEFAULT, ge=1, le=_LIST_LIMIT_MAX),
    offset: int = Query(0, ge=0),
    admin: User = Depends(require_admin),  # noqa: B008
) -> DeletedWorkspaceListOut:
    """List soft-deleted workspaces, newest deletion first.

    Paginated because this grows without bound on a long-lived instance: every
    workspace ever deleted stays here, since nothing removes them.
    """
    sf = request.app.state.session_factory
    with sf() as session:
        total = session.execute(
            select(func.count())
            .select_from(Workspace)
            .where(Workspace.soft_deleted_at.is_not(None))
        ).scalar_one()
        rows = session.execute(
            select(Workspace, User.email)
            .outerjoin(User, User.id == Workspace.owner_id)
            .where(Workspace.soft_deleted_at.is_not(None))
            .order_by(Workspace.soft_deleted_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        items = [
            DeletedWorkspaceRow(
                id=ws.id,
                title=ws.title,
                owner_email=email,
                soft_deleted_at=ws.soft_deleted_at,
            )
            for ws, email in rows
        ]
    return DeletedWorkspaceListOut(items=items, total=total)
```

- [ ] **Step 4: Register the router**

In `src/call_manager/main.py`, beside the other `app.include_router(...)` calls (around line 726), add:

```python
    app.include_router(admin_retention.router)
```

with the matching import alongside the other `web.api` imports.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/test_api_admin_retention_list.py -q > /tmp/t2.log 2>&1; echo "EXIT=$?"; tail -5 /tmp/t2.log
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Expected: 4 passed, `RUFF_EXIT=0`.

- [ ] **Step 6: Prove the live-workspace exclusion is not vacuous**

Remove `.where(Workspace.soft_deleted_at.is_not(None))` from the `rows` query, re-run, and confirm `test_lists_only_deleted_workspaces` FAILS. Restore, re-run, confirm green. Report both outcomes.

- [ ] **Step 7: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add src/call_manager/web/api/admin_retention.py src/call_manager/main.py tests/test_api_admin_retention_list.py
git commit -m "feat(admin): list soft-deleted workspaces

New module rather than a relaxation of the owner routes: those gate on a live
row, and widening them would put the deleted case on the live code path.
Paginated because nothing ever removes deleted workspaces, so this list only
grows."
```

---

### Task 3: Admin inspect endpoint

The subtle task. Three things here are easy to get wrong and each has a test that catches it.

**Files:**
- Modify: `src/call_manager/web/api/admin_retention.py`
- Test: `tests/test_api_admin_retention_inspect.py` (create)

**Interfaces:**
- Consumes: `list_commits(repo_dir, *, limit) -> tuple[list[CommitInfo], bool]` and `CommitInfo(sha, authored_at, subject)` from Task 1; the `router` from Task 2.
- Produces: `GET /api/admin/workspaces/deleted/{workspace_id}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api_admin_retention_inspect.py`:

```python
"""GET /api/admin/workspaces/deleted/{id} — read a deleted workspace.

Three hazards, one test each:
  * the WRONG MUTEX. acquire_commit_mutex_live raises WorkspaceDeleted, which
    would refuse every workspace this endpoint exists to serve.
  * a MISSING REPOSITORY is legitimate (deleted during setup) and must read as
    a state, not a crash.
  * the AUDIT EVENT must be committed before content is served, or a crash
    mid-response serves a draft with no record that anyone read it.
"""

from datetime import datetime, timezone

from call_manager.db.models import Workspace, WorkspaceEvent

from tests.helpers import make_user, make_workspace


def _delete(session, workspace_id):
    ws = session.get(Workspace, workspace_id)
    ws.soft_deleted_at = datetime.now(timezone.utc)
    session.commit()


def test_reads_a_deleted_workspace(admin_client, pg_session_factory, tmp_path, seeded_repo):
    """The mutex test. If the implementation uses acquire_commit_mutex_live,
    this returns an error instead of content, because that variant raises
    WorkspaceDeleted for exactly the workspaces this endpoint serves."""
    ws_id = seeded_repo(content="# Draft\n\nBody text.\n")
    with pg_session_factory() as s:
        _delete(s, ws_id)

    r = admin_client.get(f"/api/admin/workspaces/deleted/{ws_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "Body text." in body["proposal_markdown"]
    assert body["repository_available"] is True
    assert len(body["commits"]) >= 1


def test_live_workspace_is_404(admin_client, seeded_repo):
    ws_id = seeded_repo(content="# Live\n")
    r = admin_client.get(f"/api/admin/workspaces/deleted/{ws_id}")
    assert r.status_code == 404


def test_non_admin_is_403(member_client, seeded_repo, pg_session_factory):
    ws_id = seeded_repo(content="# x\n")
    with pg_session_factory() as s:
        _delete(s, ws_id)
    r = member_client.get(f"/api/admin/workspaces/deleted/{ws_id}")
    assert r.status_code == 403


def test_missing_repository_is_a_state_not_an_error(
    admin_client, pg_session_factory, tmp_path
):
    """A workspace deleted before setup created a repo. Legitimate, not a bug."""
    with pg_session_factory() as s:
        owner = make_user(s, email="norepo@ki.se")
        ws = make_workspace(s, owner=owner, title="never set up")
        s.commit()
        ws_id = ws.id
        _delete(s, ws_id)

    r = admin_client.get(f"/api/admin/workspaces/deleted/{ws_id}")
    assert r.status_code == 200, "a missing repo is a state of the workspace, not a failure"
    body = r.json()
    assert body["repository_available"] is False
    assert body["proposal_markdown"] is None
    assert body["commits"] == []


def test_inspection_writes_an_audit_event(
    admin_client, pg_session_factory, seeded_repo
):
    ws_id = seeded_repo(content="# Draft\n")
    with pg_session_factory() as s:
        _delete(s, ws_id)

    admin_client.get(f"/api/admin/workspaces/deleted/{ws_id}")

    with pg_session_factory() as s:
        events = [
            e
            for e in s.query(WorkspaceEvent).filter_by(workspace_id=ws_id).all()
            if e.kind == "admin_deleted_workspace_inspected"
        ]
    assert len(events) == 1
    assert events[0].actor_id is not None


def test_no_content_is_served_when_the_audit_write_fails(
    admin_client, pg_session_factory, seeded_repo, monkeypatch
):
    """The ordering guarantee. If the audit cannot be recorded, the draft must
    not be served — otherwise a crash mid-response leaves content read with no
    trace that anyone read it."""
    from call_manager.web.api import admin_retention

    ws_id = seeded_repo(content="# Secret draft\n")
    with pg_session_factory() as s:
        _delete(s, ws_id)

    def _boom(*args, **kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(admin_retention, "_record_inspection", _boom)

    r = admin_client.get(f"/api/admin/workspaces/deleted/{ws_id}")
    assert r.status_code >= 500
    assert "Secret draft" not in r.text


def test_inspection_starts_no_collab_room(
    admin_client, pg_session_factory, seeded_repo, request
):
    """A deleted workspace has no live collab room and must not acquire one.
    Inspection that spun up a room would resurrect machinery deletion tore
    down, and Slice 3's purge would then have a reader nothing knows about."""
    ws_id = seeded_repo(content="# Draft\n")
    with pg_session_factory() as s:
        _delete(s, ws_id)

    manager = request.getfixturevalue("app").state.collab_manager
    before = manager.room_count() if hasattr(manager, "room_count") else len(manager._rooms)

    admin_client.get(f"/api/admin/workspaces/deleted/{ws_id}")

    after = manager.room_count() if hasattr(manager, "room_count") else len(manager._rooms)
    assert after == before, "inspection must not create a collab room"


def test_truncated_history_is_reported_to_the_caller(
    admin_client, pg_session_factory, seeded_repo_with_commits
):
    """A capped list presented as a complete one is a lie the admin cannot
    detect. The flag is the only thing that distinguishes them."""
    ws_id = seeded_repo_with_commits(n=205)
    with pg_session_factory() as s:
        _delete(s, ws_id)

    body = admin_client.get(f"/api/admin/workspaces/deleted/{ws_id}").json()
    assert len(body["commits"]) == 200
    assert body["history_truncated"] is True


def test_untruncated_history_says_so(
    admin_client, pg_session_factory, seeded_repo_with_commits
):
    ws_id = seeded_repo_with_commits(n=3)
    with pg_session_factory() as s:
        _delete(s, ws_id)

    body = admin_client.get(f"/api/admin/workspaces/deleted/{ws_id}").json()
    assert body["history_truncated"] is False


def test_inspection_creates_no_commit(admin_client, pg_session_factory, seeded_repo, tmp_path):
    """Reading must not write. A new commit would rewrite the history an admin
    is inspecting."""
    from call_manager.proposal_assist.citations.gitio import current_head
    from call_manager.proposal_assist.paths import workspace_repo_dir

    ws_id = seeded_repo(content="# Draft\n")
    with pg_session_factory() as s:
        _delete(s, ws_id)

    repo = workspace_repo_dir(tmp_path, ws_id)
    before = current_head(repo)
    admin_client.get(f"/api/admin/workspaces/deleted/{ws_id}")
    assert current_head(repo) == before
```

`seeded_repo` is a fixture this test file defines (or reuses if one already exists): it creates a user, a workspace, and an initialised git repo at `workspace_repo_dir(tmp_path, ws_id)` containing `proposal.md`, and returns the workspace id. Build it by following `tests/test_api_workspaces.py`'s `_init_repo` helper — do not invent a different repository layout.

`seeded_repo_with_commits(n)` is the same thing with `n` commits on top, for the truncation tests. Keep it cheap: rewrite one file and commit in a loop; do not create `n` files.

`test_inspection_starts_no_collab_room` needs whatever handle the app exposes for the collab room manager. **Find its real name before writing the test** — read how `tests/proposal_assist/collab/` fixtures reach the manager and use the same accessor. If the app genuinely exposes no way to count live rooms, assert instead that no `collab_artifact_state` row and no `YjsUpdate` row exists for the workspace after inspection, which is the durable evidence a room was created.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/test_api_admin_retention_inspect.py -q > /tmp/t3.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t3.log
```

Expected: FAIL — 404, the route does not exist.

- [ ] **Step 3: Implement the inspect route**

Append to `src/call_manager/web/api/admin_retention.py`:

```python
_HISTORY_LIMIT = 200


class CommitOut(BaseModel):
    sha: str
    authored_at: datetime
    subject: str


class DeletedWorkspaceDetailOut(BaseModel):
    id: uuid.UUID
    title: str
    owner_email: str | None
    soft_deleted_at: datetime
    repository_available: bool
    proposal_markdown: str | None
    commits: list[CommitOut]
    history_truncated: bool


def _record_inspection(
    session, *, workspace_id: uuid.UUID, admin_id: int, served: bool,
    commit_count: int, truncated: bool,
) -> None:
    """Write and COMMIT the inspection audit event.

    Separate function so the ordering is visible at the call site and so a test
    can make it fail: nothing that reveals proposal content may be returned
    before this has committed.
    """
    session.add(
        WorkspaceEvent(
            workspace_id=workspace_id,
            kind="admin_deleted_workspace_inspected",
            actor_id=admin_id,
            payload={
                "content_served": served,
                "commit_count": commit_count,
                "history_truncated": truncated,
            },
        )
    )
    session.commit()


@router.get(
    "/api/admin/workspaces/deleted/{workspace_id}",
    response_model=DeletedWorkspaceDetailOut,
)
def inspect_deleted_workspace(
    workspace_id: uuid.UUID,
    request: Request,
    admin: User = Depends(require_admin),  # noqa: B008
) -> DeletedWorkspaceDetailOut:
    """Read a soft-deleted workspace's surviving proposal text and history.

    Holds the per-workspace commit mutex for the whole git read. That is the
    plain `acquire_commit_mutex`, NOT `acquire_commit_mutex_live` — the live
    variant re-reads liveness and raises WorkspaceDeleted, which would refuse
    every workspace this endpoint exists to serve.

    The audit event is committed BEFORE any content is returned.
    """
    sf = request.app.state.session_factory
    workspaces_dir = Path(request.app.state.settings.workspaces_dir)

    with sf() as session:
        with acquire_commit_mutex(session, workspace_id=workspace_id):
            row = session.execute(
                select(Workspace, User.email)
                .outerjoin(User, User.id == Workspace.owner_id)
                .where(Workspace.id == workspace_id)
            ).one_or_none()
            if row is None or row[0].soft_deleted_at is None:
                raise HTTPException(status_code=404, detail="not found")
            ws, owner_email = row

            repo = workspace_repo_dir(workspaces_dir, workspace_id)
            try:
                markdown = (repo / PROPOSAL_FILENAME).read_text(encoding="utf-8")
                commits, truncated = list_commits(repo, limit=_HISTORY_LIMIT)
                available = True
            except (OSError, GitCommitError):
                # Legitimate: the workspace can be deleted before setup ever
                # created a repository. Report it as a state, not a crash.
                markdown, commits, truncated, available = None, [], False, False

            _record_inspection(
                session,
                workspace_id=workspace_id,
                admin_id=admin.id,
                served=available,
                commit_count=len(commits),
                truncated=truncated,
            )

            return DeletedWorkspaceDetailOut(
                id=ws.id,
                title=ws.title,
                owner_email=owner_email,
                soft_deleted_at=ws.soft_deleted_at,
                repository_available=available,
                proposal_markdown=markdown,
                commits=[
                    CommitOut(sha=c.sha, authored_at=c.authored_at, subject=c.subject)
                    for c in commits
                ],
                history_truncated=truncated,
            )
```

Add the imports this needs at the top of the module: `from pathlib import Path`, `WorkspaceEvent` from `call_manager.db.models`, `acquire_commit_mutex` from `call_manager.proposal_assist.drafting.locking`, `list_commits` and `GitCommitError` from `call_manager.proposal_assist.citations.gitio`, and `workspace_repo_dir` plus `PROPOSAL_FILENAME` from `call_manager.proposal_assist.paths`. **Check the real name and location of the proposal-filename constant before using it** — do not assume it is a literal `"proposal.md"`.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/test_api_admin_retention_inspect.py -q > /tmp/t3.log 2>&1; echo "EXIT=$?"; tail -8 /tmp/t3.log
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Expected: 10 passed, `RUFF_EXIT=0`.

- [ ] **Step 5: Prove the mutex choice is pinned**

Swap `acquire_commit_mutex` for `acquire_commit_mutex_live`, re-run the file, and confirm `test_reads_a_deleted_workspace` FAILS (the live variant raises `WorkspaceDeleted`). Restore, re-run, confirm green. Report both outcomes — this is the slice's headline trap and the test must actually catch it.

- [ ] **Step 6: Run the whole suite**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest -n 16 --dist loadfile -q > /tmp/t3-full.log 2>&1; echo "EXIT=$?"; tail -3 /tmp/t3-full.log
```

Expected: `3360 passed, 1 skipped` plus this slice's new tests, exit 0.

- [ ] **Step 7: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add src/call_manager/web/api/admin_retention.py tests/test_api_admin_retention_inspect.py
git commit -m "feat(admin): inspect a deleted workspace's surviving content

Holds the PLAIN commit mutex for the git read -- acquire_commit_mutex_live
would raise WorkspaceDeleted and refuse every workspace this serves, which is
what test_reads_a_deleted_workspace pins.

A missing repository is a state (deleted during setup), not an error. The
audit event commits before any content is returned, so a failure mid-response
cannot leave a draft read with no record of it."
```

---

### Task 4: Admin UI

**Files:**
- Create: `frontend/src/routes/AdminDeletedWorkspaces.tsx`, `frontend/src/routes/AdminDeletedWorkspaceDetail.tsx`
- Modify: `frontend/src/router.tsx` (add both routes under the existing `/admin` children, following the `path: "users"` entry at line 108), `frontend/src/routes/AdminNav.tsx` (add the nav link)
- Test: `frontend/src/routes/AdminDeletedWorkspaces.test.tsx` (create)

**Interfaces:**
- Consumes: `GET /api/admin/workspaces/deleted` returning `{items: [{id, title, owner_email, soft_deleted_at}], total}`, and `GET /api/admin/workspaces/deleted/{id}` returning `{id, title, owner_email, soft_deleted_at, repository_available, proposal_markdown, commits: [{sha, authored_at, subject}], history_truncated}`.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Regenerate the OpenAPI types**

The generated client types are a hard CI gate, and Tasks 2-3 added routes.

```bash
cd /home/parlar_ai/dev/call-monitor/frontend
pnpm install --frozen-lockfile
cd /home/parlar_ai/dev/call-monitor
EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh ; echo "GEN_EXIT=$?"
git diff --stat -- frontend/src/api/schema.gen.ts
```

`EMBEDDING_MODEL=bge-m3` is required: the default emits a startup warning on stdout that corrupts the generated file. Expected: `GEN_EXIT=0` and `schema.gen.ts` showing the two new paths.

- [ ] **Step 2: Write the failing component test**

Create `frontend/src/routes/AdminDeletedWorkspaces.test.tsx`, following the structure of the existing `AdminUsers.test.tsx` in the same directory (same mocking approach, same render helpers — do not introduce a different testing style):

```tsx
import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { AdminDeletedWorkspaces } from "./AdminDeletedWorkspaces";

describe("AdminDeletedWorkspaces", () => {
  it("lists deleted workspaces with owner and deletion time", async () => {
    renderWithProviders(<AdminDeletedWorkspaces />, {
      handlers: [
        mockGet("/api/admin/workspaces/deleted", {
          items: [
            {
              id: "11111111-1111-1111-1111-111111111111",
              title: "Retired grant draft",
              owner_email: "owner@ki.se",
              soft_deleted_at: "2026-08-01T10:00:00Z",
            },
          ],
          total: 1,
        }),
      ],
    });

    expect(await screen.findByText("Retired grant draft")).toBeInTheDocument();
    expect(screen.getByText("owner@ki.se")).toBeInTheDocument();
  });

  it("says so plainly when nothing has been deleted", async () => {
    renderWithProviders(<AdminDeletedWorkspaces />, {
      handlers: [mockGet("/api/admin/workspaces/deleted", { items: [], total: 0 })],
    });

    expect(await screen.findByText(/no deleted workspaces/i)).toBeInTheDocument();
  });
});
```

Use whatever `renderWithProviders` / request-mocking helpers `AdminUsers.test.tsx` already uses, with their real names.

- [ ] **Step 3: Run the test to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor/frontend
pnpm run test -- AdminDeletedWorkspaces 2>&1 | tail -20
```

Expected: FAIL — the module does not exist.

- [ ] **Step 4: Build both pages**

`AdminDeletedWorkspaces.tsx`: a Mantine `Table` of the list response — title, owner email, deletion time — each row linking to the detail route. Fetch with TanStack Query through the typed `openapi-fetch` client, matching how `AdminUsers.tsx` does it. Render an explicit empty state reading "No deleted workspaces." Include a short line stating that deleted workspaces are retained on disk and that this view is read-only, so an admin is not left wondering whether more is coming.

`AdminDeletedWorkspaceDetail.tsx`: fetch the detail route; render the proposal markdown and a table of commits (short sha, authored time, subject). When `repository_available` is `false`, render "This workspace has no readable repository." instead of an empty document. When `history_truncated` is `true`, state that only the most recent 200 commits are shown.

- [ ] **Step 5: Wire the routes and the nav link**

In `frontend/src/router.tsx`, add two children beside the existing `path: "users"` entry (line 108), lazy-loaded exactly like its neighbours:

```tsx
          {
            path: "deleted-workspaces",
            element: (
              <Suspense fallback={<RouteSpinner />}>
                <AdminDeletedWorkspaces />
              </Suspense>
            ),
          },
          {
            path: "deleted-workspaces/:workspaceId",
            element: (
              <Suspense fallback={<RouteSpinner />}>
                <AdminDeletedWorkspaceDetail />
              </Suspense>
            ),
          },
```

Add the matching `lazy(() => import(...))` declarations beside the others near line 37, and a nav entry in `AdminNav.tsx` following its existing items.

**Do not touch `frontend/routes.json`.** It carries SPA route *prefixes*, and `/admin` is already listed, so these pages are covered. Adding a per-route entry there has broken production before.

- [ ] **Step 6: Run the frontend gates**

```bash
cd /home/parlar_ai/dev/call-monitor/frontend
pnpm run test 2>&1 | tail -8
pnpm exec tsc --noEmit ; echo "TSC_EXIT=$?"
git -C /home/parlar_ai/dev/call-monitor diff --exit-code -- frontend/src/api/schema.gen.ts ; echo "DRIFT_EXIT=$?"
```

Expected: tests pass, `TSC_EXIT=0`, `DRIFT_EXIT=0`. There is no TypeScript linter on this frontend — `tsc` is the gate, so a clean `tsc` matters.

- [ ] **Step 7: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add frontend/src/routes/AdminDeletedWorkspaces.tsx frontend/src/routes/AdminDeletedWorkspaceDetail.tsx frontend/src/routes/AdminDeletedWorkspaces.test.tsx frontend/src/router.tsx frontend/src/routes/AdminNav.tsx frontend/src/api/schema.gen.ts
git commit -m "feat(admin): deleted-workspaces pages

List and detail under the existing admin shell. routes.json untouched -- it
carries SPA prefixes and /admin already covers these."
```

---

## Done when

- An admin can list soft-deleted workspaces and read one; live workspaces 404 through both routes; a non-admin gets 403.
- Inspection of a workspace with no repository returns `repository_available: false`, not an error.
- Every inspection writes exactly one `admin_deleted_workspace_inspected` event, committed before content is returned.
- Inspection leaves `HEAD` unchanged.
- The mutex mutation in Task 3 Step 5 was performed and reported: swapping to `acquire_commit_mutex_live` turns `test_reads_a_deleted_workspace` red.
- `uv run pytest -n 16 --dist loadfile` → `3360 passed, 1 skipped` plus this slice's tests; `uv run ruff check .` clean; frontend `pnpm run test` and `tsc --noEmit` clean; `schema.gen.ts` shows no drift.
- No migration was added, no `rmtree` exists anywhere in the diff, and `frontend/routes.json` is unchanged.
