# Admin Inspection Surface (Retention Slice B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an admin list soft-deleted workspaces and read their surviving proposal content and git history, with every read audited.

**Architecture:** Two new admin-only JSON routes in a new module, backed by a new bounded git-log helper, plus one new React page under the existing admin shell. Reads only — nothing is created, modified, or removed anywhere. No database migration.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, pytest (+ testcontainers Postgres); React 19 + Mantine + TanStack Query + `openapi-fetch` on the frontend.

**Spec:** `docs/superpowers/specs/2026-08-16-admin-retention-surface-design.md` revision 4, Slice **B** only.

**Plan revision 2** (2026-08-16), after a review found eight defects — one P0. The spec renamed its slices A-E in revision 4; this plan's old "Slice 1/2/3" are B/D/E. Slice A (freeze-only delete) has since shipped (`ef8ccb3`), which is why deletion now destroys nothing for this surface to inspect.

## Global Constraints

- **Slice B removes nothing and adds no migration.** No `rmtree`, no purge route, no schema change, no tombstone columns. Those belong to deferred Slices D and E (spec §9).
- **Use `acquire_commit_mutex` (`src/call_manager/proposal_assist/drafting/locking.py:205`), NEVER `acquire_commit_mutex_live` (`:267`).** The `_live` variant does a liveness re-read and raises `WorkspaceDeleted` (`:292`), so it would refuse every workspace this surface exists to serve. This is the single easiest mistake in the slice.
- **`acquire_commit_mutex` returns `None` — it is NOT a context manager.** Call it as a plain statement inside an open transaction; the advisory lock releases at COMMIT or ROLLBACK. `with acquire_commit_mutex(...)` raises `AttributeError` on every request.
- **Map `CommitMutexTimeout` to 429 + `Retry-After: 2` with `BUSY_ERROR_MESSAGE`**, matching every other mutex caller (`web/api/workspaces.py:444`). Unhandled it is a 500 the SPA's busy handling does not recognise.
- **Serve the proposal at `HEAD`, not the working tree** (spec 3.2). Use `current_head` + `blob_sha` + `read_blob`; a workspace can be deleted with uncommitted bytes on disk.
- **Routes are registered on `web/api/__init__.py`'s router, not `main.py`.** That router carries the `/api` prefix AND the shared error-envelope `responses`; bypassing it advertises the wrong 422 shape and breaks the TS codegen. Route paths are therefore written relative (`/admin/...`).
- **Audit payload is exactly the five keys spec 3.3 fixes:** `workspace_id`, `admin_user_id`, `content_served`, `commit_count`, `history_truncated`.
- **User-written content renders as escaped text, never as markup.** No `dangerouslySetInnerHTML` (there is none in this frontend today) and no markdown-to-HTML renderer on the admin pages.
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

Inspection (retention Slice B) shows an admin the history of a deleted
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


HOSTILE = "fix: a|b|c pipes \x1f US \x1e RS \t tab and \\n literal"


def test_a_hostile_subject_round_trips_exactly(tmp_path):
    """Commit subjects are user-controlled and git stores control bytes verbatim
    — verified: `git commit -m $'a\\x1eb'` succeeds and `%s` emits the 0x1e back.
    So ASCII RS/US framing is forgeable: a subject containing the record
    separator splits one commit into two rows, and one containing the field
    separator shifts every field.

    NUL is the one byte git refuses ("error: a NUL byte in commit log message
    not allowed"), which is why the implementation frames on it.

    Exact equality, and no `or` — an assertion with an alternative branch passes
    for whichever branch happens to hold and can never fail."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    (repo / "proposal.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", HOSTILE],
        cwd=repo,
        check=True,
    )
    commits, truncated = list_commits(repo, limit=10)
    assert len(commits) == 1, "a forged separator split one commit into several rows"
    assert commits[0].subject == HOSTILE
    assert truncated is False


def test_a_hostile_subject_does_not_corrupt_its_neighbours(tmp_path):
    """The failure mode framing exists to prevent: injection in commit 2 of 3
    must not shift commit 1's or commit 3's fields."""
    repo = _repo(tmp_path, 1)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", HOSTILE],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "after"],
        cwd=repo,
        check=True,
    )
    commits, _ = list_commits(repo, limit=10)
    assert [c.subject for c in commits] == ["after", HOSTILE, "commit 0"]
    assert all(len(c.sha) == 40 for c in commits), "a shifted field lands in sha"


def test_a_plain_directory_does_not_inherit_an_enclosing_repository(tmp_path):
    """`repo_dir` may exist without being a repository — a workspace deleted
    before setup ever ran `git init`. Without GIT_CEILING_DIRECTORIES git walks
    up and reports the ENCLOSING repository's history, so an admin would be
    shown this project's commits as if they were the workspace's.

    Mirrors the ceiling that `current_head` already sets (gitio.py:290)."""
    outer = tmp_path / "outer"
    (outer / "inner").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "."], cwd=outer, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "enclosing repo commit"],
        cwd=outer,
        check=True,
    )
    with pytest.raises(GitCommitError):
        list_commits(outer / "inner", limit=10)
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


# NUL framing. Commit subjects are user-controlled, and git stores ASCII control
# bytes in them verbatim — RS (0x1e) and US (0x1f) included, verified against a
# real `git commit`. Framing on either lets a crafted subject split one commit
# into two rows or shift every field one place left. NUL is the ONLY byte git
# refuses ("error: a NUL byte in commit log message not allowed"), so it is the
# only safe separator. `%x00` emits it from the format string, so no module
# constant is needed — do not reintroduce one for a byte used inline once.


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
                # -z terminates each commit with NUL; %x00 separates the three
                # fields within one. Both are the same byte, so the stream is a
                # flat run of NUL-separated fields, parsed three at a time below.
                "-z",
                "--format=%H%x00%aI%x00%s",
            ],
            cwd=repo_dir,
            env=_list_commits_env(repo_dir),
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

    # git -z emits a trailing NUL, so the final split yields an empty tail.
    # Drop ONLY that tail — do not filter on truthiness, which would silently
    # swallow an empty subject and shift every field after it.
    fields = proc.stdout.split("\x00")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 3 != 0:
        raise GitCommitError(
            f"malformed git log output for {repo_dir}: {len(fields)} NUL-separated "
            f"fields is not a multiple of 3"
        )
    records = [tuple(fields[i : i + 3]) for i in range(0, len(fields), 3)]
    truncated = len(records) > limit
    commits: list[CommitInfo] = []
    for sha, authored, subject in records[:limit]:
        commits.append(
            CommitInfo(
                sha=sha,
                # %aI is strict ISO-8601 with an offset; fromisoformat handles it
                # on 3.12. A parse failure means the framing shifted — fail loudly
                # rather than storing a garbage timestamp.
                authored_at=datetime.fromisoformat(authored),
                subject=subject,
            )
        )
    return commits, truncated


def _list_commits_env(repo_dir: Path) -> dict[str, str]:
    """`_git_env()` plus a discovery ceiling.

    `_git_env()` (gitio.py:34) sets only author/committer identity. It does NOT
    pin repository discovery, so `git log` in a plain directory walks up and
    reports an enclosing repository's history. `current_head` (gitio.py:290)
    already guards this; `list_commits` reads the same directories and needs the
    same guard, or a workspace whose repo was never created shows an admin the
    project's own commits.
    """
    return dict(_git_env(), GIT_CEILING_DIRECTORIES=str(Path(repo_dir).resolve().parent))
```

Add `from dataclasses import dataclass` and `from datetime import datetime` to the module imports if they are not already present.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/citations/test_gitio_list_commits.py -q > /tmp/t1.log 2>&1; echo "EXIT=$?"; tail -5 /tmp/t1.log
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Expected: 8 passed, `RUFF_EXIT=0`.

- [ ] **Step 5: Prove the truncation flag is not vacuous**

Change `truncated = len(records) > limit` to `truncated = False`, re-run the file, and confirm `test_truncates_at_the_limit_and_says_so` FAILS. Restore, re-run, confirm green. Report both outcomes.

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add src/call_manager/proposal_assist/citations/gitio.py tests/proposal_assist/citations/test_gitio_list_commits.py
git commit -m "feat(gitio): bounded list_commits for retention inspection

Asks git for limit+1 so truncation is detectable without a second call.
Frames on NUL (git log -z + %x00), the one byte git refuses inside a commit
message. ASCII RS/US are NOT safe: git stores them verbatim, so a crafted
subject could split one commit into two rows. Sets GIT_CEILING_DIRECTORIES
like current_head, so a plain directory cannot report an enclosing repo."
```

---

### Task 2: Admin list endpoint

**Files:**
- Create: `src/call_manager/web/api/admin_retention.py`
- Modify: `src/call_manager/web/api/__init__.py` (add to the central `/api` router — **not** `main.py`)
- Test: `tests/test_api_admin_retention_list.py` (create)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `router` (an `APIRouter` with **no prefix of its own** and paths written relative, e.g. `/admin/workspaces/deleted`) exported from `admin_retention.py`. `web/api/__init__.py`'s router supplies the `/api` prefix, so the final path is `/api/admin/workspaces/deleted`. Task 3 adds its route to this same module and router.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api_admin_retention_list.py`:

```python
"""GET /api/admin/workspaces/deleted — the admin list of soft-deleted workspaces.

Retention Slice B. Reads only; this endpoint can remove nothing.
"""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from call_manager.db.models import User, Workspace
from call_manager.main import create_app
from tests.proposal_assist.conftest import make_user, make_workspace


def _setup(monkeypatch, pg_url, tmp_path):
    """Same shape as tests/test_api_admin_users.py:_setup, plus a workspaces_dir.

    Task 3 reads files under it; setting it here too keeps both test modules
    identical rather than having one silently point at the real configured
    directory."""
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    app = create_app()
    app.state.settings.workspaces_dir = str(tmp_path)
    return app


def _client_for(app, sf, *, email, is_admin):
    """Cookie-authenticated TestClient, matching test_api_admin_users.py:_admin."""
    with sf() as s:
        u = User(email=email, status="active", is_admin=is_admin)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, uid


def _delete(session, workspace_id):
    ws = session.get(Workspace, workspace_id)
    ws.soft_deleted_at = datetime.now(UTC)
    session.commit()


def test_lists_only_deleted_workspaces(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    with pg_session_factory() as s:
        owner = make_user(s, email="o@ki.se")
        live = make_workspace(s, owner=owner, title="still here")
        dead = make_workspace(s, owner=owner, title="gone")
        s.commit()
        live_id, dead_id = live.id, dead.id
        _delete(s, dead_id)

    r = admin.get("/api/admin/workspaces/deleted")
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()["items"]]
    assert str(dead_id) in ids
    assert str(live_id) not in ids, (
        "a live workspace reachable through the admin retention surface would "
        "make it a back door around ownership"
    )


def test_non_admin_is_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _setup(monkeypatch, pg_url, tmp_path)
    member, _ = _client_for(app, pg_session_factory, email="member@ki.se", is_admin=False)
    r = member.get("/api/admin/workspaces/deleted")
    assert r.status_code == 403


def test_pagination_caps_the_limit(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    r = admin.get("/api/admin/workspaces/deleted?limit=5000")
    assert r.status_code == 422, "an uncapped limit lets one request read the whole table"


def test_returns_total_count_and_honours_offset(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    with pg_session_factory() as s:
        owner = make_user(s, email="o2@ki.se")
        made = [make_workspace(s, owner=owner, title=f"w{i}") for i in range(3)]
        s.commit()
        ids = [w.id for w in made]
        for wid in ids:
            _delete(s, wid)

    first = admin.get("/api/admin/workspaces/deleted?limit=2&offset=0").json()
    second = admin.get("/api/admin/workspaces/deleted?limit=2&offset=2").json()
    assert first["total"] == 3
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    # Disjoint pages, not merely different first rows: an off-by-one in the
    # offset would still satisfy "first id differs".
    assert not ({r["id"] for r in first["items"]} & {r["id"] for r in second["items"]})


def test_the_route_is_documented_with_the_shared_error_envelope(
    monkeypatch, pg_url, tmp_path
):
    """These routes must go through web/api/__init__.py's router, which declares
    responses={code: ApiError} for every status the /api surface emits. Mounting
    them directly on the app instead advertises FastAPI's stock
    HTTPValidationError for the 422 and breaks the OpenAPI -> TS codegen.

    This asserts the SCHEMA, not a runtime body, and that distinction matters:
    the runtime envelope comes from app-level exception handlers gated on
    is_api_path() (main.py:637), so a directly-mounted route still RETURNS the
    envelope. Only the generated document tells the two mountings apart —
    verified: /api/admin/users documents $ref ApiError and has 11 documented
    statuses, while a directly-mounted route documents HTTPValidationError and
    has 2."""
    app = _setup(monkeypatch, pg_url, tmp_path)
    responses = app.openapi()["paths"]["/api/admin/workspaces/deleted"]["get"]["responses"]
    ref = responses["422"]["content"]["application/json"]["schema"]["$ref"]
    assert ref == "#/components/schemas/ApiError", (
        f"422 documents {ref} — the router is mounted outside web/api/__init__.py"
    )
    assert {"401", "403", "404", "429"} <= set(responses)
```

**Scaffolding note — read this before writing the file.** There are no
`admin_client` / `member_client` / `seeded_repo` fixtures and no `tests.helpers`
module; do not import them. `make_user` and `make_workspace` are plain functions
in `tests/proposal_assist/conftest.py` and are imported by top-level tests today
(see `tests/test_workspaces_service.py`). `pg_url` and `pg_session_factory` are
real session-scoped fixtures. Setting `app.state.settings.workspaces_dir` on the
app object is the established pattern (`tests/test_api_export.py:44`).

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

Retention Slice B (docs/superpowers/specs/2026-08-16-admin-retention-surface-design.md).
This module REMOVES NOTHING. Purge is deferred to Slice E and requires the
filesystem-activity barrier of Slice D first.

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

# NO prefix: web/api/__init__.py's router adds /api and the shared error-envelope
# responses. Paths below are written relative to that.
router = APIRouter(tags=["admin-retention"])

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


# Path is RELATIVE. web/api/__init__.py's router supplies the /api prefix along
# with the shared error-envelope responses; an absolute path mounted straight on
# the app would skip those and advertise the wrong 422 shape to the TS codegen.
@router.get("/admin/workspaces/deleted", response_model=DeletedWorkspaceListOut)
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

- [ ] **Step 4: Register the router on the central API router**

**Not in `main.py`.** `main.py` mounts `api_router` (line 737), and that router —
defined in `src/call_manager/web/api/__init__.py:25` — carries both the `/api`
prefix and `responses={code: {"model": ApiError} for code in STATUS_CODES}`.
Every `/api` module goes through it. A router mounted directly on the app would
document FastAPI's stock `HTTPValidationError` for its 422 while the runtime
returns the `{error: {...}}` envelope — the exact drift that block exists to
prevent, and it breaks the OpenAPI -> TS codegen.

In `src/call_manager/web/api/__init__.py`, add `admin_retention` to the existing
`from call_manager.web.api import (...)` block (alphabetical: after
`admin_golden_set`, before `auth`), then add beside the other includes:

```python
router.include_router(admin_retention.router)
```

The router in `admin_retention.py` therefore takes **no prefix** — it is already declared that way in Step 3.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/test_api_admin_retention_list.py -q > /tmp/t2.log 2>&1; echo "EXIT=$?"; tail -5 /tmp/t2.log
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Expected: 5 passed, `RUFF_EXIT=0`.

- [ ] **Step 6: Prove the live-workspace exclusion is not vacuous**

Remove `.where(Workspace.soft_deleted_at.is_not(None))` from the `rows` query, re-run, and confirm `test_lists_only_deleted_workspaces` FAILS. Restore, re-run, confirm green. Report both outcomes.

- [ ] **Step 7: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add src/call_manager/web/api/admin_retention.py src/call_manager/web/api/__init__.py tests/test_api_admin_retention_list.py
git status --short   # main.py must NOT appear -- registration goes on the central api router
git commit -m "feat(admin): list soft-deleted workspaces

New module rather than a relaxation of the owner routes: those gate on a live
row, and widening them would put the deleted case on the live code path.
Paginated because nothing ever removes deleted workspaces, so this list only
grows.

Registered on web/api/__init__.py's router, not main.py, so it inherits the
shared error-envelope responses the TS codegen depends on."
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

Five hazards, one test each:
  * the WRONG MUTEX. acquire_commit_mutex_live raises WorkspaceDeleted, which
    would refuse every workspace this endpoint exists to serve. And it is a
    plain call, not a context manager — `with acquire_commit_mutex(...)` raises
    AttributeError on every request.
  * a CONTENDED mutex must surface as the established 429 + Retry-After, not a
    500.
  * the endpoint must read HEAD, not the working tree. A workspace can be
    deleted with uncommitted edits on disk; the spec's contract is the
    committed draft.
  * a MISSING REPOSITORY is legitimate (deleted during setup) and must read as
    a state, not a crash.
  * the AUDIT EVENT must be committed before content is served, or a crash
    mid-response serves a draft with no record that anyone read it.
"""

import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from call_manager.db.models import User, Workspace, WorkspaceEvent
from call_manager.main import create_app
from call_manager.proposal_assist.paths import workspace_repo_dir
from tests.proposal_assist.conftest import make_user, make_workspace


def _setup(monkeypatch, pg_url, tmp_path):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    app = create_app()
    # The route resolves repositories under this directory. Without it the test
    # reads the real configured workspaces_dir.
    app.state.settings.workspaces_dir = str(tmp_path)
    return app


def _client_for(app, sf, *, email, is_admin):
    with sf() as s:
        u = User(email=email, status="active", is_admin=is_admin)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, uid


def _delete(session, workspace_id):
    ws = session.get(Workspace, workspace_id)
    ws.soft_deleted_at = datetime.now(UTC)
    session.commit()


def _commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", message],
        cwd=repo,
        check=True,
    )


def _seed(sf, tmp_path, *, content="# Draft\n", n_commits=1, email=None):
    """Create a workspace row plus an initialised repo under tmp_path.

    Layout comes from `workspace_repo_dir` — do not hand-build a different one.
    Returns the workspace id.
    """
    with sf() as s:
        owner = make_user(s, email=email or f"o-{uuid.uuid4().hex[:8]}@ki.se")
        ws = make_workspace(s, owner=owner, title="Seeded")
        s.commit()
        ws_id = ws.id
    repo = workspace_repo_dir(Path(tmp_path), ws_id)
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    for i in range(n_commits):
        (repo / "proposal.md").write_text(content if i == n_commits - 1 else f"rev {i}\n")
        _commit(repo, f"commit {i}")
    return ws_id


def test_reads_a_deleted_workspace(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """The mutex test, two ways. `acquire_commit_mutex` returns None, so calling
    it in a `with` statement raises AttributeError; and the `_live` variant
    re-reads liveness and raises WorkspaceDeleted for exactly the workspaces
    this endpoint serves. Either mistake turns this 200 into a 500."""
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    ws_id = _seed(pg_session_factory, tmp_path, content="# Draft\n\nBody text.\n")
    with pg_session_factory() as s:
        _delete(s, ws_id)

    r = admin.get(f"/api/admin/workspaces/deleted/{ws_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "Body text." in body["proposal_markdown"]
    assert body["repository_available"] is True
    assert len(body["commits"]) >= 1


def test_a_contended_mutex_is_429_with_retry_after(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """CommitMutexTimeout is raised by acquire_commit_mutex when lock_timeout
    fires. Unhandled it becomes a 500. Every other mutex caller in this codebase
    maps it to 429 + Retry-After: 2 with BUSY_ERROR_MESSAGE (see
    web/api/workspaces.py:444) and this must match, or the SPA's busy handling
    does not apply to admin routes."""
    from call_manager.proposal_assist.drafting.locking import CommitMutexTimeout
    from call_manager.web.api import admin_retention

    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    ws_id = _seed(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        _delete(s, ws_id)

    def _timeout(*a, **k):
        raise CommitMutexTimeout("held by another writer")

    monkeypatch.setattr(admin_retention, "acquire_commit_mutex", _timeout)

    r = admin.get(f"/api/admin/workspaces/deleted/{ws_id}")
    assert r.status_code == 429, r.text
    assert r.headers["Retry-After"] == "2"


def test_serves_the_committed_draft_not_the_working_tree(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Spec 3.2: "proposal markdown at HEAD". A workspace can be deleted while a
    collab session had written proposal.md without committing, so the working
    tree and HEAD legitimately differ. `read_text()` on the file would serve
    whichever bytes happened to be on disk — content that was never part of the
    proposal's history and that the commit list does not explain."""
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    ws_id = _seed(pg_session_factory, tmp_path, content="# Committed\n")
    repo = workspace_repo_dir(Path(tmp_path), ws_id)
    (repo / "proposal.md").write_text("# UNCOMMITTED SCRATCH\n")
    with pg_session_factory() as s:
        _delete(s, ws_id)

    body = admin.get(f"/api/admin/workspaces/deleted/{ws_id}").json()
    assert body["proposal_markdown"] == "# Committed\n"
    assert "UNCOMMITTED" not in body["proposal_markdown"]


def test_live_workspace_is_404(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    ws_id = _seed(pg_session_factory, tmp_path, content="# Live\n")
    r = admin.get(f"/api/admin/workspaces/deleted/{ws_id}")
    assert r.status_code == 404


def test_non_admin_is_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _setup(monkeypatch, pg_url, tmp_path)
    member, _ = _client_for(app, pg_session_factory, email="member@ki.se", is_admin=False)
    ws_id = _seed(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        _delete(s, ws_id)
    r = member.get(f"/api/admin/workspaces/deleted/{ws_id}")
    assert r.status_code == 403


def test_missing_repository_is_a_state_not_an_error(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """A workspace deleted before setup created a repo. Legitimate, not a bug."""
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, admin_id = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    with pg_session_factory() as s:
        owner = make_user(s, email="norepo@ki.se")
        ws = make_workspace(s, owner=owner, title="never set up")
        s.commit()
        ws_id = ws.id
        _delete(s, ws_id)

    r = admin.get(f"/api/admin/workspaces/deleted/{ws_id}")
    assert r.status_code == 200, "a missing repo is a state of the workspace, not a failure"
    body = r.json()
    assert body["repository_available"] is False
    assert body["proposal_markdown"] is None
    assert body["commits"] == []

    # The unavailable case is audited too: "an admin opened this record" is the
    # fact being traced, and it is true whether or not bytes came back.
    with pg_session_factory() as s:
        events = (
            s.query(WorkspaceEvent)
            .filter_by(workspace_id=ws_id, kind="admin_deleted_workspace_inspected")
            .all()
        )
    assert len(events) == 1
    assert events[0].payload["content_served"] is False
    assert events[0].payload["commit_count"] == 0


def test_inspection_writes_exactly_one_audit_event_naming_the_admin(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Spec 3.3 fixes the payload: workspace_id, admin_user_id, whether content
    was served, commit count, truncation flag. `actor_id is not None` would pass
    with the wrong admin, a duplicate event, or an empty payload."""
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, admin_id = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    other, other_id = _client_for(app, pg_session_factory, email="other@ki.se", is_admin=True)
    ws_id = _seed(pg_session_factory, tmp_path, n_commits=2)
    with pg_session_factory() as s:
        _delete(s, ws_id)

    assert admin.get(f"/api/admin/workspaces/deleted/{ws_id}").status_code == 200

    with pg_session_factory() as s:
        events = (
            s.query(WorkspaceEvent)
            .filter_by(workspace_id=ws_id, kind="admin_deleted_workspace_inspected")
            .all()
        )
    assert len(events) == 1, "one read must leave one row, not zero and not two"
    ev = events[0]
    assert ev.actor_id == admin_id, "the trace names the wrong admin"
    assert ev.actor_id != other_id
    assert ev.payload == {
        "workspace_id": str(ws_id),
        "admin_user_id": admin_id,
        "content_served": True,
        "commit_count": 2,
        "history_truncated": False,
    }


def test_a_second_read_appends_a_second_event(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """The trace is per-read, not per-workspace. An implementation that
    de-duplicated would hide repeat access to someone's draft."""
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    ws_id = _seed(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        _delete(s, ws_id)

    admin.get(f"/api/admin/workspaces/deleted/{ws_id}")
    admin.get(f"/api/admin/workspaces/deleted/{ws_id}")

    with pg_session_factory() as s:
        n = (
            s.query(WorkspaceEvent)
            .filter_by(workspace_id=ws_id, kind="admin_deleted_workspace_inspected")
            .count()
        )
    assert n == 2


def test_no_content_is_served_when_the_audit_write_fails(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """The ordering guarantee. If the audit cannot be recorded, the draft must
    not be served — otherwise a crash mid-response leaves content read with no
    trace that anyone read it."""
    from call_manager.web.api import admin_retention

    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    ws_id = _seed(pg_session_factory, tmp_path, content="# Secret draft\n")
    with pg_session_factory() as s:
        _delete(s, ws_id)

    def _boom(*args, **kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(admin_retention, "_record_inspection", _boom)

    with pytest.raises(RuntimeError, match="audit write failed"):
        # TestClient re-raises server exceptions by default; that IS the
        # assertion — no response body was produced, so no content escaped.
        admin.get(f"/api/admin/workspaces/deleted/{ws_id}")


def test_inspection_starts_no_collab_room(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """A deleted workspace has no live collab room and must not acquire one.
    Inspection that spun up a room would resurrect machinery deletion tore
    down, and Slice E's purge would then have a reader nothing knows about."""
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    ws_id = _seed(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        _delete(s, ws_id)

    # The room manager is a module-level singleton (collab/room.py:776), NOT an
    # app.state attribute — `app.state.collab_manager` does not exist. This is
    # how tests/proposal_assist/test_workspaces_delete_route.py:58 reaches it.
    from call_manager.proposal_assist.collab.room import MANAGER

    before = len(MANAGER._rooms)
    admin.get(f"/api/admin/workspaces/deleted/{ws_id}")
    assert len(MANAGER._rooms) == before, "inspection must not create a collab room"


def test_truncated_history_is_reported_to_the_caller(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """A capped list presented as a complete one is a lie the admin cannot
    detect. The flag is the only thing that distinguishes them."""
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    ws_id = _seed(pg_session_factory, tmp_path, n_commits=205)
    with pg_session_factory() as s:
        _delete(s, ws_id)

    body = admin.get(f"/api/admin/workspaces/deleted/{ws_id}").json()
    assert len(body["commits"]) == 200
    assert body["history_truncated"] is True


def test_untruncated_history_says_so(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    ws_id = _seed(pg_session_factory, tmp_path, n_commits=3)
    with pg_session_factory() as s:
        _delete(s, ws_id)

    body = admin.get(f"/api/admin/workspaces/deleted/{ws_id}").json()
    assert body["history_truncated"] is False


def test_inspection_creates_no_commit(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """Reading must not write. A new commit would rewrite the history an admin
    is inspecting."""
    from call_manager.proposal_assist.citations.gitio import current_head

    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    ws_id = _seed(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        _delete(s, ws_id)

    repo = workspace_repo_dir(Path(tmp_path), ws_id)
    before = current_head(repo)
    admin.get(f"/api/admin/workspaces/deleted/{ws_id}")
    assert current_head(repo) == before
```

**Scaffolding note.** As in Task 2: no `admin_client` / `member_client` /
`seeded_repo` fixtures exist and there is no `tests.helpers` module. `_seed`
above is the repository builder, and it uses `workspace_repo_dir` rather than
hand-building a path. The collab room manager is the module-level `MANAGER`
singleton in `proposal_assist/collab/room.py`, not an `app.state` attribute.
Never write a `hasattr(...) if ... else ...` fallback in a test — it passes
whichever way the attribute happens to exist, so it asserts nothing.

`_seed(..., n_commits=205)` covers the truncation tests. It rewrites one file and
commits in a loop rather than creating 205 files, so the 205-commit case stays
cheap. If that test is slow enough to notice, lower `_HISTORY_LIMIT` via
`monkeypatch.setattr` instead of seeding fewer commits — never by weakening the
assertion.

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
            # Spec 3.3 fixes these five keys. workspace_id and admin_user_id are
            # redundant with the columns ON PURPOSE: the payload is what an
            # exported or forwarded audit trail carries, and a row that says
            # only "content_served: true" names neither the draft read nor who
            # read it. str() on the UUID because the column is JSONB.
            payload={
                "workspace_id": str(workspace_id),
                "admin_user_id": admin_id,
                "content_served": served,
                "commit_count": commit_count,
                "history_truncated": truncated,
            },
        )
    )
    session.commit()


@router.get(
    "/admin/workspaces/deleted/{workspace_id}",
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
        # NOT a context manager. acquire_commit_mutex returns None
        # (drafting/locking.py:205); the advisory lock is held for the rest of
        # the transaction and released by COMMIT or ROLLBACK. `with acquire_...`
        # raises AttributeError on every request.
        #
        # The plain variant, not acquire_commit_mutex_live: the _live variant
        # re-reads liveness and raises WorkspaceDeleted, which would refuse every
        # workspace this endpoint exists to serve.
        try:
            acquire_commit_mutex(session, workspace_id)
        except CommitMutexTimeout as e:
            session.rollback()
            # Same mapping as every other mutex caller (web/api/workspaces.py:444).
            raise HTTPException(
                status_code=429,
                detail=BUSY_ERROR_MESSAGE,
                headers={"Retry-After": "2"},
            ) from e

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
            # At HEAD, not the working tree. Spec 3.2 says "proposal markdown at
            # HEAD", and a workspace can be deleted with uncommitted bytes on
            # disk from a collab session that never checkpointed. read_text()
            # would serve content the commit list beside it does not explain.
            head = current_head(repo)
            markdown = read_blob(repo, blob_sha(repo, head, PROPOSAL_FILENAME))
            commits, truncated = list_commits(repo, limit=_HISTORY_LIMIT)
            available = True
        except (OSError, GitCommitError):
            # Legitimate: the workspace can be deleted before setup ever
            # created a repository, or before proposal.md was first committed.
            # Report it as a state, not a crash.
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

Add the imports this needs at the top of the module. These paths are verified — use them as written:

```python
from pathlib import Path

from call_manager.db.models import WorkspaceEvent
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.citations.gitio import (
    GitCommitError,
    blob_sha,
    current_head,
    list_commits,
    read_blob,
)
from call_manager.proposal_assist.drafting.locking import (
    BUSY_ERROR_MESSAGE,
    CommitMutexTimeout,
    acquire_commit_mutex,
)
from call_manager.proposal_assist.paths import workspace_repo_dir
```

`PROPOSAL_FILENAME` is in `proposal_assist.artifacts`, **not** `proposal_assist.paths` — `paths` exports only `workspace_repo_dir`. Signatures you are calling: `current_head(repo_dir) -> str`, `blob_sha(repo_dir, ref, path) -> str` (ref is positional), `read_blob(repo_dir, sha) -> str`. All three raise `GitCommitError`, which the `except` already covers.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/test_api_admin_retention_inspect.py -q > /tmp/t3.log 2>&1; echo "EXIT=$?"; tail -8 /tmp/t3.log
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Expected: 13 passed, `RUFF_EXIT=0`.

- [ ] **Step 5: Prove the mutex choice is pinned**

Swap `acquire_commit_mutex` for `acquire_commit_mutex_live`, re-run the file, and confirm `test_reads_a_deleted_workspace` FAILS (the live variant raises `WorkspaceDeleted`). Restore, re-run, confirm green. Report both outcomes — this is the slice's headline trap and the test must actually catch it.

- [ ] **Step 6: Run the whole suite**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest -n 16 --dist loadfile -q > /tmp/t3-full.log 2>&1; echo "EXIT=$?"; tail -3 /tmp/t3-full.log
```

Expected: `3361 passed, 1 skipped` plus this slice's new tests, exit 0.

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
- Modify: `frontend/src/api/admin.ts` (add the two query hooks beside `useAdminUsers`)
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
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { AdminDeletedWorkspaces } from "./AdminDeletedWorkspaces";
import { AdminDeletedWorkspaceDetail } from "./AdminDeletedWorkspaceDetail";

afterEach(() => {
  vi.restoreAllMocks();
});

function row(i: number) {
  return {
    id: `1111111${i}-1111-1111-1111-111111111111`,
    title: `Retired grant draft ${i}`,
    owner_email: `owner${i}@ki.se`,
    soft_deleted_at: "2026-08-01T10:00:00Z",
  };
}

function mockList(items: unknown[], total = items.length) {
  return vi
    .spyOn(clientMod.api, "GET")
    .mockResolvedValue({ data: { items, total }, error: undefined } as never);
}

it("lists deleted workspaces with owner and deletion time", async () => {
  mockList([row(0)]);
  renderWithProviders(<AdminDeletedWorkspaces />);
  expect(await screen.findByText("Retired grant draft 0")).toBeInTheDocument();
  expect(screen.getByText("owner0@ki.se")).toBeInTheDocument();
});

it("says so plainly when nothing has been deleted", async () => {
  mockList([], 0);
  renderWithProviders(<AdminDeletedWorkspaces />);
  expect(await screen.findByText(/no deleted workspaces/i)).toBeInTheDocument();
});

it("requests the first page with an explicit offset", async () => {
  const get = mockList([row(0)], 120);
  renderWithProviders(<AdminDeletedWorkspaces />);
  await screen.findByText("Retired grant draft 0");
  // Assert the REQUEST, not just the render: a component that renders page 1
  // correctly while never sending offset looks identical until you click Next.
  const params = get.mock.calls[0][1]?.params?.query;
  expect(params).toMatchObject({ offset: 0 });
  expect(params.limit).toBeGreaterThan(0);
});

it("advances the offset when the admin goes to the next page", async () => {
  const get = mockList([row(0)], 120);
  renderWithProviders(<AdminDeletedWorkspaces />);
  await screen.findByText("Retired grant draft 0");
  const limit = get.mock.calls[0][1].params.query.limit;

  await userEvent.click(screen.getByRole("button", { name: /next/i }));

  await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(1));
  const latest = get.mock.calls.at(-1)[1].params.query;
  expect(latest.offset).toBe(limit);
});

it("does not offer a next page when everything already fits", async () => {
  mockList([row(0)], 1);
  renderWithProviders(<AdminDeletedWorkspaces />);
  await screen.findByText("Retired grant draft 0");
  expect(screen.queryByRole("button", { name: /next/i })).toBeNull();
});

// ---- detail page ----

function mockDetail(overrides: Record<string, unknown> = {}) {
  return vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: {
      id: "11111111-1111-1111-1111-111111111111",
      title: "Retired grant draft",
      owner_email: "owner@ki.se",
      soft_deleted_at: "2026-08-01T10:00:00Z",
      repository_available: true,
      proposal_markdown: "# Draft\n\nBody text.\n",
      commits: [
        { sha: "a".repeat(40), authored_at: "2026-07-30T09:00:00Z", subject: "first" },
      ],
      history_truncated: false,
      ...overrides,
    },
    error: undefined,
  } as never);
}

it("renders the proposal markdown and the commit list", async () => {
  mockDetail();
  renderWithProviders(<AdminDeletedWorkspaceDetail />);
  expect(await screen.findByText(/Body text\./)).toBeInTheDocument();
  expect(screen.getByText("first")).toBeInTheDocument();
});

it("renders hostile markdown as text, never as markup", async () => {
  // The draft is written by ordinary users and this page is viewed by an admin.
  // Rendering it as HTML would turn any workspace into stored XSS against the
  // one account that can see every deleted workspace.
  const hostile = '# Title\n\n<img src=x onerror="alert(1)">\n<script>alert(2)</script>\n';
  mockDetail({ proposal_markdown: hostile });
  const { container } = renderWithProviders(<AdminDeletedWorkspaceDetail />);

  // The literal source is visible...
  expect(await screen.findByText(/onerror/)).toBeInTheDocument();
  // ...and no element was created from it.
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("script")).toBeNull();
  expect(container.innerHTML).not.toContain("<img");
});

it("states that no repository is readable instead of showing an empty document", async () => {
  mockDetail({ repository_available: false, proposal_markdown: null, commits: [] });
  renderWithProviders(<AdminDeletedWorkspaceDetail />);
  expect(await screen.findByText(/no readable repository/i)).toBeInTheDocument();
});

it("says when the history was truncated", async () => {
  mockDetail({ history_truncated: true });
  renderWithProviders(<AdminDeletedWorkspaceDetail />);
  expect(await screen.findByText(/most recent 200/i)).toBeInTheDocument();
});
```

**Mocking style.** This mirrors `AdminUsers.test.tsx`: `vi.spyOn(clientMod.api, "GET")`
plus `renderWithProviders` from `../test-utils`. There is no `mockGet` helper and
`renderWithProviders` takes no `handlers` option — do not invent either.

`AdminDeletedWorkspaceDetail` reads its id from the route params. Render it
inside whatever router wrapper `renderWithProviders` provides, following how
existing param-reading route tests in this directory do it; if none exists,
accept the id as an optional prop defaulting to the param so the test can pass
it directly. Do not add a `data-testid` to make a query work — query the visible
text, or the test passes for reasons unrelated to what it claims.

- [ ] **Step 3: Run the test to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor/frontend
pnpm run test > /tmp/t4.log 2>&1; echo "TEST_EXIT=$?"; tail -20 /tmp/t4.log
```

Expected: `TEST_EXIT=1` — the modules do not exist.

**Never pipe a test command into `tail`.** The pipeline's exit status is the
pager's, so a failing suite reports success. Redirect, capture `$?`, then read
the file. (`pnpm run test -- <pattern>` also does not reliably filter here —
this project's runs execute every file regardless — so just run the suite.)

- [ ] **Step 4: Build both pages**

`AdminDeletedWorkspaces.tsx`: a Mantine `Table` of the list response — title, owner
email, deletion time — each row linking to the detail route. Fetch with TanStack
Query through the typed `openapi-fetch` client, matching how `AdminUsers.tsx` does
it. Render an explicit empty state reading "No deleted workspaces." Include a short
line stating that deleted workspaces are retained on disk and that this view is
read-only, so an admin is not left wondering whether more is coming.

**Pagination is required, not optional** (spec 3.4: "a paginated table"; the wire
contract is `limit`/`offset` per spec 3.1.1). The list grows without bound because
nothing removes deleted workspaces, so a single unpaginated fetch degrades forever.

Two conventions meet here, so be deliberate. `/api/admin/users` is **page-based**
(`{page, per_page, total}`) and `AdminUsers.tsx:72-108` is built around it. This
route is **offset-based**, because the spec fixed it that way. Keep the wire as the
spec has it and convert in the hook, so the component still looks like its
neighbour.

Add the hook to `frontend/src/api/admin.ts` beside `useAdminUsers` — that is where
admin queries live; do not inline `useQuery` in the component:

```ts
export const DELETED_PER_PAGE = 50;

export function useAdminDeletedWorkspaces(page: number) {
  return useQuery({
    // page is in the key, or page 2 renders page 1's cache
    queryKey: ["admin-deleted-workspaces", page],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/admin/workspaces/deleted", {
          params: {
            query: { limit: DELETED_PER_PAGE, offset: (page - 1) * DELETED_PER_PAGE },
          },
        }),
      ),
    retry: false,
  });
}

export function useAdminDeletedWorkspace(workspaceId: string) {
  return useQuery({
    queryKey: ["admin-deleted-workspace", workspaceId],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/admin/workspaces/deleted/{workspace_id}", {
          params: { path: { workspace_id: workspaceId } },
        }),
      ),
    retry: false,
  });
}
```

`unwrap` (`frontend/src/api/client.ts:34`) handles the 401 redirect and the error
envelope — use it rather than checking `error` by hand.

In the component, mirror `AdminUsers.tsx:101-110` exactly: `const [page, setPage] =
useState(1)`, a "Showing page N (T total)" line, a Previous button rendered only
when `page > 1`, and a Next button rendered only when `total > page * DELETED_PER_PAGE`.
The tests above assert the emitted `offset`, so a component that renders page 1
correctly but never advances the request will fail.

`AdminDeletedWorkspaceDetail.tsx`: fetch the detail route; render the proposal
markdown and a table of commits (short sha, authored time, subject). When
`repository_available` is `false`, render "This workspace has no readable
repository." instead of an empty document. When `history_truncated` is `true`,
state that only the most recent 200 commits are shown.

**Render the markdown as escaped text — never as markup.** `proposal_markdown` is
written by ordinary users; this page is read by the one account that can see every
deleted workspace. Rendering it as HTML makes any workspace stored XSS against
that account.

There is **no `dangerouslySetInnerHTML` anywhere in this frontend today** — keep it
that way. Do not add a markdown renderer for this page either: `markdown-it` is
present as a ProseMirror dependency, not as a sanitising view layer, and there is
no DOMPurify in the tree. Use the existing preformatted-text approach, the same one
`AdminDrafting.tsx:72` uses for untrusted payloads:

```tsx
<Code block fz="xs" style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
  {detail.proposal_markdown}
</Code>
```

A React text child is escaped by construction, so the hostile-markdown test above
passes for the right reason. An admin inspecting a deleted draft wants to see what
was written, and the source is a faithful answer; rendered prose is not worth an
XSS surface here.

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
pnpm run test > /tmp/t4-gate.log 2>&1; echo "TEST_EXIT=$?"; tail -8 /tmp/t4-gate.log
pnpm exec tsc --noEmit ; echo "TSC_EXIT=$?"
```

Expected: `TEST_EXIT=0` and `TSC_EXIT=0`. Redirect rather than pipe — `| tail`
replaces the test command's exit status with the pager's, so a failing suite
reports success. There is no TypeScript linter on this frontend; `tsc` is the gate.

**The drift check belongs AFTER the commit, not here.** Step 1 regenerated
`schema.gen.ts` and it is still unstaged, so `git diff --exit-code` against it now
returns 1 by construction — it would report drift on a correct branch every time.
What CI actually checks is that regenerating produces no *further* change once the
generated file is committed. Run it as the last action of Step 7.

- [ ] **Step 7: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add frontend/src/routes/AdminDeletedWorkspaces.tsx frontend/src/routes/AdminDeletedWorkspaceDetail.tsx frontend/src/routes/AdminDeletedWorkspaces.test.tsx frontend/src/api/admin.ts frontend/src/router.tsx frontend/src/routes/AdminNav.tsx frontend/src/api/schema.gen.ts
git status --short   # frontend/routes.json must NOT appear
git commit -m "feat(admin): deleted-workspaces pages

List and detail under the existing admin shell. Paginated, because nothing ever
removes deleted workspaces so this list only grows. Proposal markdown renders as
escaped preformatted text, never as markup: it is user-written and this page is
read by the one account that can see every deleted workspace.

routes.json untouched -- it carries SPA prefixes and /admin already covers these."
```

Now verify there is no OpenAPI drift, with the generated file committed:

```bash
cd /home/parlar_ai/dev/call-monitor
EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh ; echo "GEN_EXIT=$?"
git diff --exit-code -- frontend/src/api/schema.gen.ts ; echo "DRIFT_EXIT=$?"
```

Expected: `GEN_EXIT=0`, `DRIFT_EXIT=0` — regenerating the committed schema changes
nothing. A non-zero `DRIFT_EXIT` here means Step 1's regeneration was stale (a
route docstring changed after it) — re-stage and amend.

---

## Done when

- An admin can list soft-deleted workspaces and read one; live workspaces 404 through both routes; a non-admin gets 403.
- Inspection of a workspace with no repository returns `repository_available: false`, not an error — and is still audited.
- Inspection serves the draft **at `HEAD`**: a test with a working tree that differs from the commit proves it.
- A contended mutex returns 429 with `Retry-After: 2`, not 500.
- Every inspection writes exactly one `admin_deleted_workspace_inspected` event naming the acting admin, with the exact five-key payload, committed before content is returned. Two reads leave two rows.
- Inspection leaves `HEAD` unchanged.
- The routes appear under `/api/...` **and** carry the shared error envelope — the 422 body is `{error: {code, message}}`.
- `list_commits` round-trips a subject containing RS/US bytes exactly, and a plain directory inside an enclosing repository raises rather than reporting that repository's history.
- The admin list is paginated: the request carries `offset`, Next advances it, and Next is absent when everything fits.
- Hostile markdown renders as visible source with no element created from it.
- The mutex mutation in Task 3 Step 5 was performed and reported: swapping to `acquire_commit_mutex_live` turns `test_reads_a_deleted_workspace` red.
- `uv run pytest -n 16 --dist loadfile` → `3361 passed, 1 skipped` plus this slice's tests; `uv run ruff check .` clean; frontend `pnpm run test` and `tsc --noEmit` clean; regenerating `schema.gen.ts` after committing it shows no drift. Re-derive the baseline from an actual run before comparing — it moves every slice.
- No migration was added, no `rmtree` exists anywhere in the diff, and `frontend/routes.json` is unchanged.
