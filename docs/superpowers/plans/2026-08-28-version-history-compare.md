# Version History — read a version, compare two versions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a cockpit user a History tab that lists every saved version of the draft or spec, opens any one of them read-only, and shows a word-level diff between any two.

**Architecture:** Two new read-only, member-gated FastAPI routes (`GET .../versions`, `GET .../versions/{sha}`) source the list from **git** (`gitio.list_commits`, scoped by pathspec) with `artifact_versions` as a nullable provenance overlay joined on sha. The browser fetches two version payloads and diffs them client-side with the existing dependency-free LCS in `frontend/src/cockpit/diff.ts`, extended with a word-level refinement pass. The diff renderer, currently duplicated verbatim in two components, is extracted to one shared component first.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x (no migration — no schema change). React 18 + Vite, TanStack Query, `openapi-fetch` against generated types, Mantine, vitest.

**Source spec:** `docs/superpowers/specs/2026-08-27-version-history-compare-design.md` rev 3 (`45ab224e`). Two review rounds (Sol / gpt-5.6-sol), 14 findings, all closed. §9 (restore) is **phase 2 and out of scope for this plan**.

## Global Constraints

- **No new npm package.** `frontend/pnpm-workspace.yaml` sets a 7-day `minimumReleaseAge`; `pnpm add` is refused outright. Only `pnpm install --frozen-lockfile` is permitted.
- **No database migration.** This slice reads existing tables only. If you find yourself writing an Alembic revision, stop — you have left the slice.
- **Every new route is a sync `def`, not `async`.** FastAPI then runs the git subprocess in its threadpool instead of blocking the event loop. This matches `proposal_assist/web/review.py`.
- **Every new route takes NO commit mutex.** These are reads. `review_file` takes none.
- **Any exception escaping a route handler becomes a 500, not a 4xx.** `main.py:713-716` registers an app-wide `@app.exception_handler(Exception)` returning `api_error(500, "internal_error", …)` for every `/api` request. Catch `GitCommitError` and `OSError` at the boundary.
- **The sha bound is `{7,64}`**, not `{7,40}`. sha-256-ready. One shared validator; do not add a third copy.
- **The diff ceiling is 2,000 lines per side, inclusive.** 2,000 lines still diffs; 2,001 falls back.
- **The history limit is 200**, matching `_HISTORY_LIMIT` at `web/api/admin_retention.py:105`. Not a client-tunable parameter.
- **Backend test command:** `uv run --python 3.12 pytest <path> -v`. Full suite: `uv run --python 3.12 pytest -n 16 --dist loadfile`. **Never `-n auto`** (resolves to 72 workers here) and **never `--dist loadgroup`**.
- **Do NOT `source .env` before running the suite.** It adds 4 failures that are not real (one live-SDK test that the `ANTHROPIC_API_KEY` ungates, plus three email-transport tests). Judge from a clean environment.
- **The pytest false-pass signature:** a broken import shows as mass teardown **ERRORs with zero FAILED**. Judge a mutation on FAILED, never ERROR.
- **The vitest false-pass signature:** `include` is `src/**/*.{test,spec}.{ts,tsx}` — a misnamed or misplaced test file is silently uncollected and the run is green. Judge on the printed file and test counts.
- **Frontend test commands, run from `frontend/`:** `pnpm exec tsc --noEmit` and `pnpm vitest run <path>`.
- **`ruff check` is the lint gate; `ruff format` is not.** Run `uv run ruff check src tests`.
- **Slice 1 ships with NO collab-normalisation boundary detection.** Re-verified 2026-08-28: **0 of 13** repos under `var/workspaces/*/repo` carry a `collab checkpoint:` commit. There is nothing to detect yet. Do not build the detector in this slice (spec §7.4).

---

## File Structure

**Backend — created:**

- `src/call_manager/proposal_assist/ports/shas.py` — the single hex-sha validator. Lives in `ports/` because `tools/history.py` is one of its two existing callers and the fencing import gate allows the whole `ports` package by prefix (`tests/fencing/import_gate.py:30`, `PORTS_PREFIX`). Stdlib only (`re`).
- `src/call_manager/proposal_assist/web/versions.py` — the two routes, their Pydantic response models, and the provenance overlay join. One responsibility: serving version history over HTTP.

**Backend — modified:**

- `src/call_manager/proposal_assist/citations/gitio.py` — `list_commits` gains an optional `pathspec`.
- `src/call_manager/proposal_assist/tools/history.py` — drops its private `_SHA_PATTERN`, uses the shared validator.
- `src/call_manager/proposal_assist/web/review.py` — drops its private `_SHA_RE`, uses the shared validator.
- `src/call_manager/main.py` — registers the new router.

**Frontend — created:**

- `frontend/src/api/versions.ts` — the two TanStack Query hooks.
- `frontend/src/cockpit/DiffView.tsx` — the one shared diff renderer.
- `frontend/src/cockpit/historyNav.ts` — the History tab's URL state (`artifact`, `a`, `b`).
- `frontend/src/cockpit/HistoryPanel.tsx` — the tab itself: list, states, selection, read, compare.

**Frontend — modified:**

- `frontend/src/cockpit/diff.ts` — word-level refinement beside the untouched `lineDiff`.
- `frontend/src/cockpit/CockpitPanels.module.css` — inline-span rules for the word diff.
- `frontend/src/cockpit/ProposalReview.tsx`, `frontend/src/editor/InlineProposalReview.tsx` — converted to `DiffView`.
- `frontend/src/cockpit/nav.ts`, `frontend/src/cockpit/cockpitNav.ts` — `"history"` joins `TopTab`.
- `frontend/src/routes/Cockpit.tsx` — the tab and its panel.
- `frontend/src/api/schema.gen.ts` — regenerated, never hand-edited.

---

### Task 1: One shared sha validator

Two private copies exist and they **disagree**: `tools/history.py:35` is `{7,40}`, `web/review.py:37` is `{7,64}`. A third copy is about to be needed. Extract one, use `{7,64}`, convert both.

**Files:**
- Create: `src/call_manager/proposal_assist/ports/shas.py`
- Create: `tests/proposal_assist/ports/test_shas.py`
- Modify: `src/call_manager/proposal_assist/tools/history.py` (delete `_SHA_PATTERN` at line 35, rewrite `_validate_sha` at lines 38-46)
- Modify: `src/call_manager/proposal_assist/web/review.py` (delete `_SHA_RE` at line 37, change its use at line 227)

**Interfaces:**
- Consumes: nothing.
- Produces: `is_sha(value: object) -> bool` and `sha_error(sha: object, *, arg_name: str = "commit_sha") -> str | None` from `call_manager.proposal_assist.ports.shas`. Task 3 uses `is_sha`.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/ports/test_shas.py`:

```python
"""The one hex-sha validator. Bound is {7,64}: sha-256-ready.

Git-option injection is the reason this exists — `tools/history.py`'s module
docstring names `--output=proposal.md` as the value it must refuse.
"""

import pytest

from call_manager.proposal_assist.ports.shas import is_sha, sha_error


@pytest.mark.parametrize(
    "value",
    [
        "abc1234",  # 7, the minimum (git's --abbrev-commit default)
        "a" * 40,  # sha-1 full length
        "A" * 40,  # upper case hex
        "f" * 64,  # sha-256 full length
    ],
)
def test_accepts_hex_shas(value):
    assert is_sha(value) is True
    assert sha_error(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "abc123",  # 6 — one short
        "f" * 65,  # 65 — one long
        "--output=proposal.md",  # the injection this guard exists for
        "-h",
        "HEAD",
        "zzzzzzz",  # right length, not hex
        "abc1234\n",  # trailing newline must not slip past $
        "",
        None,  # not a str at all
        1234567,
    ],
)
def test_rejects_everything_else(value):
    assert is_sha(value) is False
    msg = sha_error(value)
    assert msg is not None
    assert "hex commit sha" in msg


def test_error_message_names_the_argument():
    assert "commit_a must be" in (sha_error("nope", arg_name="commit_a") or "")
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --python 3.12 pytest tests/proposal_assist/ports/test_shas.py -v
```

Expected: collection ERROR — `ModuleNotFoundError: No module named 'call_manager.proposal_assist.ports.shas'`.

- [ ] **Step 3: Write the module**

Create `src/call_manager/proposal_assist/ports/shas.py`:

```python
"""The one hex-sha validator — stdlib only.

Two private copies used to exist and they disagreed: `tools/history.py` was
`{7,40}` and `web/review.py` was `{7,64}`. This is the merge, at `{7,64}`.

WHY it exists, from `tools/history.py`'s original docstring: every sha that
reaches git as an argument must be validated first. Git parses `--output=...`
as an option before it reaches the `--` path separator, so an unvalidated
"sha" is an option-injection vector. Separately and more mundanely,
`gitio.blob_sha` FAILS OPEN — a ref it cannot resolve is echoed back as if it
were a sha — so the boundary is the only place a bad value gets a clean answer.

Lives in `ports/` because `tools/` may import only the standard library and
this package (`tests/fencing/import_gate.py`), and `tools/history.py` is a
caller.

`$` in a Python regex also matches immediately before a trailing newline, so
the pattern ends with `\\Z` — otherwise "abc1234\\n" validates.
"""

from __future__ import annotations

import re

#: 7 is git's `--abbrev-commit` default and the shortest this codebase produces.
#: 64 is sha-256 full length; 40 is sha-1's. Accept both so a future repo-format
#: migration does not need a code change.
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}\Z")


def is_sha(value: object) -> bool:
    """True if `value` is a string safe to pass to git as a commit-ish."""
    return isinstance(value, str) and SHA_RE.match(value) is not None


def sha_error(sha: object, *, arg_name: str = "commit_sha") -> str | None:
    """Return an error message if `sha` is unsafe, else None.

    The message form is for the agent-tool callers, which return errors as
    dicts rather than raising.
    """
    if is_sha(sha):
        return None
    return f"{arg_name} must be a hex commit sha (7-64 chars, 0-9a-f); got: {sha!r}"
```

- [ ] **Step 4: Run the test and watch it pass**

```bash
uv run --python 3.12 pytest tests/proposal_assist/ports/test_shas.py -v
```

Expected: `13 passed`.

- [ ] **Step 5: Convert `tools/history.py`**

Delete lines 35 and 38-46 (the `_SHA_PATTERN` constant and the body of `_validate_sha`) and replace the whole block with a re-export, so the two existing call sites at lines 94 and 117 keep working unchanged:

```python
from call_manager.proposal_assist.ports.shas import sha_error as _validate_sha
```

Place that import beside the existing `from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME`. Then delete the now-unused `import re` at the top of the file **only if no other use of `re` remains** — check with `grep -n 're\.' src/call_manager/proposal_assist/tools/history.py` first and leave the import if anything else uses it.

Also update the module docstring: the line reading

```
validated to match ^[0-9a-fA-F]{7,40}$ BEFORE being passed to git.
```

becomes

```
validated by ports/shas.py (^[0-9a-fA-F]{7,64}$) BEFORE being passed to git.
```

- [ ] **Step 6: Convert `web/review.py`**

Delete line 37 (`_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")`). Add to the import block:

```python
from call_manager.proposal_assist.ports.shas import is_sha
```

Change the guard inside `review_file` (line 227) from:

```python
    if not sha or not _SHA_RE.match(sha):
```

to:

```python
    if not is_sha(sha):
```

`is_sha` already rejects `None` and `""`, so the `not sha` half is subsumed — do not keep it. Then remove `import re` from `review.py` **only if** `grep -n 're\.' src/call_manager/proposal_assist/web/review.py` shows no other use.

- [ ] **Step 7: Run every affected test file**

```bash
uv run --python 3.12 pytest tests/proposal_assist/ports/test_shas.py \
  tests/proposal_assist/review/test_web_review.py \
  tests/proposal_assist/tools -v 2>&1 | tail -20
uv run ruff check src tests
```

Expected: all PASS, ruff clean. If you see ERRORs rather than FAILUREs, you have broken an import — fix that before reading anything else.

- [ ] **Step 8: Prove the gate bites**

Temporarily change `SHA_RE` in `ports/shas.py` to `r"^.*\Z"` and re-run `tests/proposal_assist/ports/test_shas.py`. Expected: **FAILED** (not ERROR) on `test_rejects_everything_else`. Revert the change.

- [ ] **Step 9: Commit**

```bash
git add src/call_manager/proposal_assist/ports/shas.py \
        tests/proposal_assist/ports/test_shas.py \
        src/call_manager/proposal_assist/tools/history.py \
        src/call_manager/proposal_assist/web/review.py
git commit -m "refactor(shas): one hex-sha validator at {7,64}, two copies retired"
```

---

### Task 2: `list_commits` gains a pathspec

The list must be scoped to one artifact. Scoping the **result** is wrong: `list_commits` asks git for `--max-count={limit + 1}` (`gitio.py:478-487`), so unrelated commits consume the limit and hide older versions of the artifact the user asked for. The scope belongs inside the git command.

Measured first, so nobody mistakes this for urgency: across the 13 live repos, **zero** commits touch neither artifact. This is correctness by construction, not a live bug.

**Files:**
- Modify: `src/call_manager/proposal_assist/citations/gitio.py` (the `list_commits` signature at line 462 and its `argv` at lines 477-487)
- Test: `tests/proposal_assist/citations/test_gitio_list_commits.py` (append)

**Interfaces:**
- Consumes: nothing.
- Produces: `list_commits(repo_dir: Path, *, limit: int, pathspec: str | None = None) -> tuple[list[CommitInfo], bool]`. Task 3 calls it with `pathspec=filename`.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/citations/test_gitio_list_commits.py`:

```python
def _repo_two_files(tmp_path: Path) -> Path:
    """A repo whose NEWEST commits touch only proposal-spec.md.

    This shape is the whole point: with the pathspec applied AFTER git has
    already capped at --max-count, a small limit returns only spec commits and
    the draft's history vanishes. A fixture whose commits all touch both files
    cannot fail on that bug.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)

    def commit(name: str, body: str, msg: str) -> None:
        (repo / name).write_text(body, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", msg],
            cwd=repo,
            check=True,
        )

    commit("proposal.md", "# draft 0\n", "draft 0")
    commit("proposal.md", "# draft 1\n", "draft 1")
    for i in range(5):
        commit("proposal-spec.md", f"# spec {i}\n", f"spec {i}")
    return repo


def test_pathspec_scopes_to_one_file(tmp_path):
    repo = _repo_two_files(tmp_path)
    commits, truncated = list_commits(repo, limit=10, pathspec="proposal.md")
    assert [c.subject for c in commits] == ["draft 1", "draft 0"]
    assert truncated is False


def test_pathspec_is_applied_before_the_limit(tmp_path):
    """THE gate. The 5 newest commits touch only the spec. With a limit of 3 and
    the scope applied inside git log, the draft's two commits still come back.
    Filtering list_commits' RESULT instead would return an empty list."""
    repo = _repo_two_files(tmp_path)
    commits, truncated = list_commits(repo, limit=3, pathspec="proposal.md")
    assert [c.subject for c in commits] == ["draft 1", "draft 0"]
    assert truncated is False


def test_pathspec_matching_nothing_is_empty_not_an_error(tmp_path):
    repo = _repo_two_files(tmp_path)
    commits, truncated = list_commits(repo, limit=10, pathspec="does-not-exist.md")
    assert commits == []
    assert truncated is False


def test_omitting_pathspec_keeps_the_old_unscoped_behaviour(tmp_path):
    repo = _repo_two_files(tmp_path)
    commits, _ = list_commits(repo, limit=10)
    assert len(commits) == 7


def test_pathspec_still_reports_truncation(tmp_path):
    repo = _repo_two_files(tmp_path)
    commits, truncated = list_commits(repo, limit=1, pathspec="proposal-spec.md")
    assert [c.subject for c in commits] == ["spec 4"]
    assert truncated is True
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --python 3.12 pytest tests/proposal_assist/citations/test_gitio_list_commits.py -v
```

Expected: the five new tests FAIL with `TypeError: list_commits() got an unexpected keyword argument 'pathspec'`.

- [ ] **Step 3: Implement**

In `src/call_manager/proposal_assist/citations/gitio.py`, change the signature at line 462 from:

```python
def list_commits(repo_dir: Path, *, limit: int) -> tuple[list[CommitInfo], bool]:
```

to:

```python
def list_commits(
    repo_dir: Path, *, limit: int, pathspec: str | None = None
) -> tuple[list[CommitInfo], bool]:
```

Add to the docstring, after the existing "Raises GitCommitError…" paragraph:

```
    `pathspec` scopes the history to commits touching that path. It is passed
    after a literal `--` so a filename can never be read as an option, and it is
    applied INSIDE `git log` — scoping the returned list instead would let
    unrelated commits consume `--max-count` and hide older matching commits.
    Omitted, the history is unscoped, exactly as before.
```

Then, immediately after the `argv = [...]` literal (which ends at line 487 with the closing `]`), add:

```python
    if pathspec is not None:
        # After `--`, so a filename can never be parsed as an option. git also
        # enables history simplification here, which is what makes the count
        # honest: `--max-count` now counts MATCHING commits.
        argv += ["--", pathspec]
```

Nothing else changes. `truncated` still comes from `len(records) > limit`, which now counts matching records.

- [ ] **Step 4: Run the tests and watch them pass**

```bash
uv run --python 3.12 pytest tests/proposal_assist/citations/test_gitio_list_commits.py -v
```

Expected: all PASS, including the pre-existing tests (the `pathspec=None` default must leave them untouched).

- [ ] **Step 5: Prove the gate bites**

Change the implementation to apply the scope after the fact instead — replace the `argv += [...]` block with nothing, and instead filter at the end. The clean way to prove it without writing throwaway code: temporarily move the `argv += ["--", pathspec]` line so the pathspec is appended **before** `--max-count` is honoured is not possible, so instead simply delete the `argv +=` block and re-run.

Expected: `test_pathspec_is_applied_before_the_limit` and `test_pathspec_scopes_to_one_file` **FAILED** (not ERROR). Restore the block.

- [ ] **Step 6: Check the one existing caller still compiles**

```bash
grep -n "list_commits(" src/call_manager/web/api/admin_retention.py
uv run --python 3.12 pytest tests/test_api_admin_retention_inspect.py -v 2>&1 | tail -5
uv run ruff check src tests
```

Expected: the admin call is unchanged (`limit=_HISTORY_LIMIT`, no pathspec) and its tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/proposal_assist/citations/gitio.py \
        tests/proposal_assist/citations/test_gitio_list_commits.py
git commit -m "feat(gitio): optional pathspec on list_commits, applied inside git log"
```

---

### Task 3: The two version endpoints

**Files:**
- Create: `src/call_manager/proposal_assist/web/versions.py`
- Create: `tests/proposal_assist/web/test_versions_api.py`
- Modify: `src/call_manager/main.py` (import beside line 58, `include_router` beside line 797)

**Interfaces:**
- Consumes: `is_sha` (Task 1); `list_commits(..., pathspec=...)` (Task 2).
- Produces: two routes and their wire shapes, which Task 4 generates a client for:
  - `GET /api/workspaces/{workspace_id}/versions?artifact=draft|spec` → `{"versions": [{"commit_sha": str, "committed_at": str, "subject": str, "created_by": str | null, "triggering_turn_id": str | null, "turn_prompt": str | null}], "truncated": bool, "history_available": bool}`
  - `GET /api/workspaces/{workspace_id}/versions/{commit_sha}?artifact=draft|spec` → `{"commit_sha": str, "artifact": str, "markdown": str}`

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/web/test_versions_api.py`:

```python
"""Version-history endpoints: list from git, read one blob.

Modelled on tests/proposal_assist/review/test_web_review.py — TestClient with an
authed-owner cookie and a workspace whose repo lives under tmp_path.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

from call_manager.db.models import ArtifactVersion
from tests.proposal_assist.conftest import make_workspace


def _make_ws(_app, current_user, pg_session_factory, tmp_path) -> str:
    """Workspace + an EMPTY git repo (git init, no commits)."""
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True, current_phase="active")
        s.commit()
        ws_id = ws.id
    repo = tmp_path / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    return str(ws_id)


def _commit(repo: Path, name: str, body: str, msg: str) -> str:
    (repo / name).write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=repo, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


# --- the list -------------------------------------------------------------


def test_list_is_newest_first_and_scoped_to_the_artifact(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    repo = tmp_path / ws_id / "repo"
    _commit(repo, "proposal.md", "# draft 0\n", "draft 0")
    _commit(repo, "proposal.md", "# draft 1\n", "draft 1")
    _commit(repo, "proposal-spec.md", "# spec 0\n", "spec 0")

    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/versions?artifact=draft")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["history_available"] is True
    assert body["truncated"] is False
    assert [v["subject"] for v in body["versions"]] == ["draft 1", "draft 0"]

    r2 = client_authed_owner.get(f"/api/workspaces/{ws_id}/versions?artifact=spec")
    assert [v["subject"] for v in r2.json()["versions"]] == ["spec 0"]


def test_unborn_head_is_a_state_not_a_500(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    """THE gate for the commonest new-workspace state. `git log` on a repo with
    no commits exits 128, which list_commits converts to GitCommitError. An
    endpoint that assumes an empty list turns this into the app-wide 500 from
    main.py:713-716."""
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/versions?artifact=draft")
    assert r.status_code == 200, r.text
    assert r.json() == {"versions": [], "truncated": False, "history_available": False}


def test_missing_repo_directory_is_also_a_state(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    import shutil

    shutil.rmtree(tmp_path / ws_id / "repo")
    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/versions?artifact=draft")
    assert r.status_code == 200, r.text
    assert r.json()["history_available"] is False


def test_a_commit_with_no_artifact_versions_row_still_lists(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    """The whole reason the list is sourced from git (spec §12). Four routine
    commit paths write no artifact_versions row; those versions must still be
    reachable, with the provenance fields absent rather than erroring."""
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    repo = tmp_path / ws_id / "repo"
    sha_with = _commit(repo, "proposal.md", "# one\n", "one")
    _commit(repo, "proposal.md", "# two\n", "two")

    with pg_session_factory() as s:
        s.add(
            ArtifactVersion(
                workspace_id=uuid.UUID(ws_id),
                commit_sha=sha_with,
                created_by="user",
                message="one",
            )
        )
        s.commit()

    body = client_authed_owner.get(f"/api/workspaces/{ws_id}/versions?artifact=draft").json()
    rows = {v["subject"]: v for v in body["versions"]}
    assert rows["two"]["created_by"] is None
    assert rows["two"]["triggering_turn_id"] is None
    assert rows["one"]["created_by"] == "user"


def test_a_version_carries_the_prompt_that_caused_it(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    """Goal 1 is "who made each one AND WHY". `triggering_turn_id` points at the
    USER turn (turns/processor.py:359), and that turn's `content` is JSONB, so
    the prompt has to be extracted, not read off a column."""
    from call_manager.db.models import ChatTurn

    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    repo = tmp_path / ws_id / "repo"
    sha = _commit(repo, "proposal.md", "# one\n", "one")

    with pg_session_factory() as s:
        turn = ChatTurn(
            workspace_id=uuid.UUID(ws_id),
            role="user",
            content={"type": "text", "text": "Tighten the aims section"},
        )
        s.add(turn)
        s.flush()
        s.add(
            ArtifactVersion(
                workspace_id=uuid.UUID(ws_id),
                commit_sha=sha,
                created_by="agent",
                message="one",
                triggering_turn_id=turn.id,
            )
        )
        s.commit()

    row = client_authed_owner.get(f"/api/workspaces/{ws_id}/versions?artifact=draft").json()[
        "versions"
    ][0]
    assert row["turn_prompt"] == "Tighten the aims section"


def test_a_block_list_turn_still_yields_its_text(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    """A submit carrying an attachment, a reference or a selection emits a LIST
    of blocks whose FIRST block is the text (turns/submit.py:109). A reader that
    handles only the dict shape silently loses the reason on exactly the turns
    that had the most context."""
    from call_manager.db.models import ChatTurn

    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    repo = tmp_path / ws_id / "repo"
    sha = _commit(repo, "proposal.md", "# one\n", "one")

    with pg_session_factory() as s:
        turn = ChatTurn(
            workspace_id=uuid.UUID(ws_id),
            role="user",
            content=[
                {"type": "text", "text": "Use this figure"},
                {"type": "image", "attachment_id": str(uuid.uuid4())},
            ],
        )
        s.add(turn)
        s.flush()
        s.add(
            ArtifactVersion(
                workspace_id=uuid.UUID(ws_id),
                commit_sha=sha,
                created_by="agent",
                message="one",
                triggering_turn_id=turn.id,
            )
        )
        s.commit()

    row = client_authed_owner.get(f"/api/workspaces/{ws_id}/versions?artifact=draft").json()[
        "versions"
    ][0]
    assert row["turn_prompt"] == "Use this figure"


def test_a_version_with_no_turn_has_no_prompt(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    """Absence is NORMAL — every `reconciled` row and every commit with no DB row
    at all. It must render as ordinary, never as an error."""
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    repo = tmp_path / ws_id / "repo"
    sha = _commit(repo, "proposal.md", "# one\n", "one")
    with pg_session_factory() as s:
        s.add(
            ArtifactVersion(
                workspace_id=uuid.UUID(ws_id),
                commit_sha=sha,
                created_by="reconciled",
                message="one",
            )
        )
        s.commit()
    row = client_authed_owner.get(f"/api/workspaces/{ws_id}/versions?artifact=draft").json()[
        "versions"
    ][0]
    assert row["turn_prompt"] is None
    assert row["created_by"] == "reconciled"


def test_unknown_artifact_is_422_not_500(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/versions?artifact=bogus")
    assert r.status_code == 422, r.text


# --- reading one version --------------------------------------------------


def test_read_returns_the_markdown_at_that_commit(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    repo = tmp_path / ws_id / "repo"
    old = _commit(repo, "proposal.md", "# draft 0\n", "draft 0")
    _commit(repo, "proposal.md", "# draft 1\n", "draft 1")

    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/versions/{old}?artifact=draft")
    assert r.status_code == 200, r.text
    assert r.json() == {"commit_sha": old, "artifact": "draft", "markdown": "# draft 0\n"}


def test_read_rejects_an_option_shaped_sha_before_git(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    """422 at the boundary, NOT the app-wide 500. blob_sha fails open — it echoes
    an unresolvable ref back as if it were a sha — so this is the only place a
    bad value gets a clean answer."""
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    repo = tmp_path / ws_id / "repo"
    _commit(repo, "proposal.md", "# draft\n", "draft")
    r = client_authed_owner.get(
        f"/api/workspaces/{ws_id}/versions/--output=proposal.md?artifact=draft"
    )
    assert r.status_code == 422, r.text


def test_read_of_an_unknown_sha_is_404(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    repo = tmp_path / ws_id / "repo"
    _commit(repo, "proposal.md", "# draft\n", "draft")
    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/versions/{'a' * 40}?artifact=draft")
    assert r.status_code == 404, r.text


def test_read_of_a_commit_that_never_touched_the_artifact_is_404(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    repo = tmp_path / ws_id / "repo"
    only_spec = _commit(repo, "proposal-spec.md", "# spec\n", "spec")
    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/versions/{only_spec}?artifact=draft")
    assert r.status_code == 404, r.text


# --- membership -----------------------------------------------------------


def test_non_member_gets_403_not_a_document(
    client_authed_other, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    repo = tmp_path / ws_id / "repo"
    sha = _commit(repo, "proposal.md", "# draft\n", "draft")
    assert client_authed_other.get(f"/api/workspaces/{ws_id}/versions").status_code == 403
    assert (
        client_authed_other.get(f"/api/workspaces/{ws_id}/versions/{sha}").status_code == 403
    )
```

**Before running:** confirm the two fixtures exist. Run

```bash
grep -rn "def client_authed_owner\|def client_authed_other\|def pg_session_factory\|def _app\b" tests/conftest.py tests/proposal_assist/conftest.py
```

If `client_authed_other` does not exist under that name, find the non-member client fixture the existing endpoint tests use (`grep -rn "403" tests/proposal_assist/review/test_web_review.py`) and use that name instead. Do not invent a fixture.

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --python 3.12 pytest tests/proposal_assist/web/test_versions_api.py -v
```

Expected: every test FAILS with 404 (the routes do not exist). If instead you see collection ERRORs, a fixture name is wrong — fix that first.

- [ ] **Step 3: Write the module**

Create `src/call_manager/proposal_assist/web/versions.py`:

```python
"""Version history — list a workspace's versions, read one version's markdown.

Read-only and member-gated, modelled on `review.py`'s `review_file`: a sync
`def` so FastAPI runs the git subprocess in its threadpool, and NO commit mutex
(this is a read; taking one would queue reads behind writers for no benefit).

The list is sourced from GIT, not from `artifact_versions`. Four routine commit
paths write no row, and `reconcile` fires only on agent launch, so a collab-only
workspace's DB list can lag git indefinitely. `artifact_versions` is a
provenance OVERLAY joined on sha, every field optional (spec §12).
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import ArtifactVersion, ChatTurn
from call_manager.proposal_assist.citations.gitio import (
    GitCommitError,
    blob_sha,
    list_commits,
    read_blob,
)
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.ports.artifacts import Artifact, filename_for
from call_manager.proposal_assist.ports.shas import is_sha
from call_manager.proposal_assist.web._deps import _session_dep
from call_manager.proposal_assist.web._perms import require_member

router = APIRouter(prefix="/api/workspaces", tags=["versions"])

#: Matches `_HISTORY_LIMIT` in web/api/admin_retention.py:105, the only other
#: `list_commits` caller. Deliberately NOT a query parameter: measured on the 13
#: live repos, twelve hold one commit and the largest holds 15.
HISTORY_LIMIT = 200

#: A Literal query parameter, so FastAPI itself answers 422 for an unknown
#: value. The alternative — `Artifact(artifact)` inside the body — raises
#: ValueError, which the app-wide handler at main.py:713-716 turns into a 500.
ArtifactParam = Literal["draft", "spec"]


class VersionRow(BaseModel):
    commit_sha: str
    committed_at: str
    subject: str
    #: Overlay fields. Absent means "no artifact_versions row for this commit",
    #: which is ORDINARY — render "not recorded", never an error.
    created_by: str | None = None
    triggering_turn_id: str | None = None
    #: The user's own words from the turn that caused this version, so a row can
    #: answer "why". None wherever there is no turn — which is every
    #: `reconciled` row and every commit with no DB row at all.
    turn_prompt: str | None = None


class VersionListOut(BaseModel):
    versions: list[VersionRow]
    truncated: bool
    #: False when `list_commits` raised: no repo directory, or an unborn HEAD
    #: (a repo with `git init` and no commit). Distinct from an empty list,
    #: which means "a readable history with no commits touching this artifact".
    history_available: bool


class VersionReadOut(BaseModel):
    commit_sha: str
    artifact: str
    markdown: str


#: A prompt is free text with no length bound on the way in. A row shows a
#: reason, not an essay; the marker makes a shortened prompt distinguishable
#: from a short one, mirroring gitio's subject truncation.
_MAX_PROMPT_CHARS = 200
_PROMPT_TRUNCATION_MARKER = "…"


def _prompt_text(content: object) -> str | None:
    """The user's own words out of a `ChatTurn.content` payload.

    `_build_user_content` (turns/submit.py:92-117) emits ONE of two shapes: the
    legacy dict `{"type": "text", "text": …}` for a plain submit, or a LIST of
    blocks whose FIRST block is that same text block when an attachment,
    reference or selection is present. A reader that handles only the dict shape
    silently loses the reason on exactly the turns that carried the most
    context.

    Anything else yields None — no reason shown, never an error.
    """
    if isinstance(content, dict):
        block: object = content
    elif isinstance(content, list) and content:
        block = content[0]
    else:
        return None
    if not isinstance(block, dict) or block.get("type") != "text":
        return None
    text = block.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()
    if len(text) > _MAX_PROMPT_CHARS:
        text = text[:_MAX_PROMPT_CHARS] + _PROMPT_TRUNCATION_MARKER
    return text


@router.get("/{workspace_id}/versions", response_model=VersionListOut)
def list_versions(
    workspace_id: uuid.UUID,
    request: Request,
    artifact: ArtifactParam = "draft",
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    require_member(workspace_id=workspace_id, user=user, session=session)
    repo_dir = workspace_repo_dir(request.app.state.settings.workspaces_dir, workspace_id)
    filename = filename_for(Artifact(artifact))

    try:
        commits, truncated = list_commits(repo_dir, limit=HISTORY_LIMIT, pathspec=filename)
    except (OSError, GitCommitError):
        # Legitimate, and the commonest new-workspace state: the repo may not
        # exist yet, or exist with no commits. `git log` on an unborn HEAD exits
        # 128. Report it as a state, exactly as admin_retention.py:291-297 does.
        return {"versions": [], "truncated": False, "history_available": False}

    # sha -> (ArtifactVersion, that turn's content payload or None). An OUTER
    # join: `triggering_turn_id` is nullable and a turn can be deleted
    # (ON DELETE SET NULL), so an inner join would drop provenance rows.
    overlay: dict[str, tuple[ArtifactVersion, object]] = {}
    if commits:
        rows = session.execute(
            select(ArtifactVersion, ChatTurn.content)
            .outerjoin(ChatTurn, ChatTurn.id == ArtifactVersion.triggering_turn_id)
            .where(
                ArtifactVersion.workspace_id == workspace_id,
                ArtifactVersion.commit_sha.in_([c.sha for c in commits]),
            )
        ).all()
        overlay = {av.commit_sha: (av, content) for av, content in rows}

    versions = []
    for c in commits:
        entry = overlay.get(c.sha)
        av = entry[0] if entry else None
        versions.append(
            {
                "commit_sha": c.sha,
                "committed_at": c.authored_at.isoformat(),
                "subject": c.subject,
                "created_by": av.created_by if av else None,
                "triggering_turn_id": (
                    str(av.triggering_turn_id) if av and av.triggering_turn_id else None
                ),
                "turn_prompt": _prompt_text(entry[1]) if entry else None,
            }
        )
    return {"versions": versions, "truncated": truncated, "history_available": True}


@router.get("/{workspace_id}/versions/{commit_sha}", response_model=VersionReadOut)
def read_version(
    workspace_id: uuid.UUID,
    commit_sha: str,
    request: Request,
    artifact: ArtifactParam = "draft",
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    require_member(workspace_id=workspace_id, user=user, session=session)
    if not is_sha(commit_sha):
        # 422 at the boundary. `blob_sha` FAILS OPEN — an unresolvable ref is
        # echoed back as if it were a sha — so without this the value reaches
        # `read_blob` and dies with a confusing cat-file message.
        raise HTTPException(status_code=422, detail="commit_sha must be a hex commit sha")

    repo_dir = workspace_repo_dir(request.app.state.settings.workspaces_dir, workspace_id)
    filename = filename_for(Artifact(artifact))
    try:
        markdown = read_blob(repo_dir, blob_sha(repo_dir, commit_sha, filename))
    except (OSError, GitCommitError):
        # Unknown sha, or a real commit that never touched this artifact.
        raise HTTPException(status_code=404, detail="version content unavailable") from None
    return {"commit_sha": commit_sha, "artifact": artifact, "markdown": markdown}
```

- [ ] **Step 4: Register the router**

In `src/call_manager/main.py`, add beside the other `proposal_assist.web` imports (they sit alphabetically around lines 52-62):

```python
from call_manager.proposal_assist.web.versions import (
    router as versions_router,
)
```

and beside line 797 (`app.include_router(review_router)`):

```python
    app.include_router(versions_router)
```

- [ ] **Step 5: Run the tests and watch them pass**

```bash
uv run --python 3.12 pytest tests/proposal_assist/web/test_versions_api.py -v
uv run ruff check src tests
```

Expected: all PASS, ruff clean.

- [ ] **Step 6: Prove the two most important gates bite**

Gate A — the unborn-HEAD state. Delete the `except (OSError, GitCommitError):` block in `list_versions` (let the exception escape) and re-run. Expected: `test_unborn_head_is_a_state_not_a_500` and `test_missing_repo_directory_is_also_a_state` **FAILED with 500** (not ERROR). Restore.

Gate B — scope-before-limit reaching the wire. Temporarily change the `list_commits` call to drop `pathspec=filename` and re-run. Expected: `test_list_is_newest_first_and_scoped_to_the_artifact` **FAILED**. Restore.

Gate C — the block-list turn shape. Temporarily simplify `_prompt_text` to handle only the dict shape (delete the `elif isinstance(content, list)` branch, returning None instead) and re-run. Expected: `test_a_block_list_turn_still_yields_its_text` **FAILED**, while `test_a_version_carries_the_prompt_that_caused_it` still passes — which is why the plain-text test alone does not count. Restore.

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/proposal_assist/web/versions.py \
        tests/proposal_assist/web/test_versions_api.py \
        src/call_manager/main.py
git commit -m "feat(api): version list and single-version read, sourced from git"
```

---

### Task 4: Generated types and the frontend query hooks

**Files:**
- Modify: `frontend/src/api/schema.gen.ts` (regenerated — never hand-edited)
- Create: `frontend/src/api/versions.ts`
- Create: `frontend/src/api/versions.test.tsx`

**Interfaces:**
- Consumes: the two routes from Task 3.
- Produces, from `frontend/src/api/versions.ts`:
  - `type VersionRow = { commit_sha: string; committed_at: string; subject: string; created_by: string | null; triggering_turn_id: string | null; turn_prompt: string | null }`
  - `type VersionList = { versions: VersionRow[]; truncated: boolean; history_available: boolean }`
  - `useVersions(id: string, artifact: Artifact)` → TanStack query of `VersionList`
  - `useVersion(id: string, artifact: Artifact, sha: string | null)` → TanStack query of `{ commit_sha: string; artifact: string; markdown: string } | null`, disabled when `sha` is null.

- [ ] **Step 1: Regenerate the types**

```bash
cd frontend && pnpm gen:api
git diff --stat src/api/schema.gen.ts
```

Expected: `schema.gen.ts` gains the two `/api/workspaces/{workspace_id}/versions` paths. The script pins `EMBEDDING_MODEL=bge-m3`, `SPA_MODE=disabled` and `--python 3.12` itself, so run it exactly as above and do **not** hand-edit the output. If the diff is thousands of lines, you ran a different generator — discard and re-run `pnpm gen:api`.

- [ ] **Step 2: Write the failing test**

Create `frontend/src/api/versions.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as clientMod from "./client";
import { useVersion, useVersions } from "./versions";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("useVersions", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("returns the list payload", async () => {
    vi.spyOn(clientMod.api, "GET").mockResolvedValue({
      data: {
        versions: [
          {
            commit_sha: "abc1234",
            committed_at: "2026-08-28T10:00:00+00:00",
            subject: "draft 1",
            created_by: "user",
            triggering_turn_id: null,
            turn_prompt: null,
          },
        ],
        truncated: false,
        history_available: true,
      },
      error: undefined,
      response: new Response(),
    } as never);

    const { result } = renderHook(() => useVersions("ws1", "draft"), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.versions[0].subject).toBe("draft 1");
    expect(result.current.data?.history_available).toBe(true);
  });

  it("never returns undefined when the payload is empty", async () => {
    vi.spyOn(clientMod.api, "GET").mockResolvedValue({
      data: undefined,
      error: undefined,
      response: new Response(),
    } as never);

    const { result } = renderHook(() => useVersions("ws1", "draft"), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({
      versions: [],
      truncated: false,
      history_available: false,
    });
  });
});

describe("useVersion", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("does not fetch when no sha is selected", async () => {
    const spy = vi.spyOn(clientMod.api, "GET");
    const { result } = renderHook(() => useVersion("ws1", "draft", null), { wrapper });
    await waitFor(() => expect(result.current.fetchStatus).toBe("idle"));
    expect(spy).not.toHaveBeenCalled();
  });

  it("fetches the markdown when a sha is selected", async () => {
    vi.spyOn(clientMod.api, "GET").mockResolvedValue({
      data: { commit_sha: "abc1234", artifact: "draft", markdown: "# hi\n" },
      error: undefined,
      response: new Response(),
    } as never);

    const { result } = renderHook(() => useVersion("ws1", "draft", "abc1234"), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.markdown).toBe("# hi\n");
  });
});
```

- [ ] **Step 3: Run it and watch it fail**

```bash
cd frontend && pnpm vitest run src/api/versions.test.tsx
```

Expected: FAIL — `Failed to resolve import "./versions"`. Confirm the printed count says **1 test file**; if it says 0 files, the filename is wrong and vitest silently collected nothing.

- [ ] **Step 4: Write the hooks**

Create `frontend/src/api/versions.ts`:

```ts
import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { Artifact } from "./proposals";

export type VersionRow = {
  commit_sha: string;
  committed_at: string;
  subject: string;
  created_by: string | null;
  triggering_turn_id: string | null;
  /** The user's words from the turn that caused this version. Null wherever
   *  there is no turn — every `reconciled` row, and every commit with no DB row
   *  at all. Absence is ordinary. */
  turn_prompt: string | null;
};

export type VersionList = {
  versions: VersionRow[];
  truncated: boolean;
  history_available: boolean;
};

export type VersionContent = {
  commit_sha: string;
  artifact: string;
  markdown: string;
};

/** GET /api/workspaces/{id}/versions?artifact=… — the git-sourced version list. */
export function useVersions(id: string, artifact: Artifact) {
  return useQuery({
    queryKey: ["versions", id, artifact],
    queryFn: async (): Promise<VersionList> => {
      const data = unwrap(
        await api.GET("/api/workspaces/{workspace_id}/versions", {
          params: { path: { workspace_id: id }, query: { artifact } },
        }),
      ) as VersionList | undefined;
      // Never return undefined — React Query rejects it. `history_available:
      // false` is the honest default for "we got nothing back".
      return data ?? { versions: [], truncated: false, history_available: false };
    },
  });
}

/** GET /api/workspaces/{id}/versions/{sha}?artifact=… — one version's markdown.
 *  Disabled while no sha is selected, so selecting nothing costs no request. */
export function useVersion(id: string, artifact: Artifact, sha: string | null) {
  return useQuery({
    enabled: sha != null,
    queryKey: ["version", id, artifact, sha],
    queryFn: async (): Promise<VersionContent | null> => {
      if (sha == null) return null;
      const data = unwrap(
        await api.GET("/api/workspaces/{workspace_id}/versions/{commit_sha}", {
          params: {
            path: { workspace_id: id, commit_sha: sha },
            query: { artifact },
          },
        }),
      ) as VersionContent | undefined;
      return data ?? null;
    },
  });
}
```

- [ ] **Step 5: Run the tests and the type check**

```bash
cd frontend && pnpm vitest run src/api/versions.test.tsx && pnpm exec tsc --noEmit
```

Expected: `1 passed (1)` file, 4 tests, and tsc silent (exit 0).

- [ ] **Step 6: Confirm the codegen drift gate is satisfied**

```bash
cd frontend && pnpm gen:api && cd .. && git diff --exit-code frontend/src/api/schema.gen.ts
```

Expected: exit 0 (no diff) — regeneration is idempotent, so CI's drift check will agree.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/api/schema.gen.ts frontend/src/api/versions.ts frontend/src/api/versions.test.tsx
git commit -m "feat(api-client): version list and version read hooks"
```

---

### Task 5: Word-level diff refinement

The live draft is paragraph-per-line markdown: **37 lines, longest line 1172 characters, 14 lines over 200**. Under a pure line diff, changing `40 patients` to `120 patients` strikes through the entire 1172-character paragraph and re-adds it. Every real edit would look like a rewrite.

**Files:**
- Modify: `frontend/src/cockpit/diff.ts` (append; `lineDiff` and its signature are untouched)
- Create: `frontend/src/cockpit/diff.refine.test.ts`

**Interfaces:**
- Consumes: the existing `lineDiff(current: string, proposed: string): DiffRow[]`.
- Produces, from `frontend/src/cockpit/diff.ts`:
  - `type Span = { tag: "same" | "add" | "del"; text: string }`
  - `DiffRow` gains an optional `spans?: Span[]` — additive, so both existing callers keep compiling.
  - `wordDiff(before: string, after: string): { del: Span[]; add: Span[] }`
  - `refineRuns(rows: DiffRow[]): DiffRow[]`
  - `MAX_DIFF_LINES = 2000` and `tooLargeToDiff(a: string, b: string): boolean`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/cockpit/diff.refine.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { MAX_DIFF_LINES, lineDiff, refineRuns, tooLargeToDiff, wordDiff } from "./diff";

describe("wordDiff", () => {
  it("marks only the changed word", () => {
    const { del, add } = wordDiff(
      "We will recruit 40 patients from the biobank.",
      "We will recruit 120 patients from the biobank.",
    );
    expect(del.filter((s) => s.tag === "del").map((s) => s.text)).toEqual(["40"]);
    expect(add.filter((s) => s.tag === "add").map((s) => s.text)).toEqual(["120"]);
  });

  it("round-trips: the spans rejoin to the original strings", () => {
    const before = "alpha beta gamma delta";
    const after = "alpha BETA gamma epsilon delta";
    const { del, add } = wordDiff(before, after);
    expect(del.map((s) => s.text).join("")).toBe(before);
    expect(add.map((s) => s.text).join("")).toBe(after);
  });

  it("refines a change inside a 1172-character paragraph", () => {
    // The gate as spec §11 states it. The live draft is paragraph-per-line
    // markdown whose longest line is 1172 characters; the failure this prevents
    // is a two-character edit rendering as a whole-paragraph rewrite.
    const filler = "lorem ipsum dolor sit amet ".repeat(43); // ~1161 chars
    const before = `We will recruit 40 patients. ${filler}`;
    const after = `We will recruit 120 patients. ${filler}`;
    expect(before.length).toBeGreaterThan(1100);

    const { del, add } = wordDiff(before, after);
    // BOTH unchanged and changed spans in the same row — not one whole-line row.
    expect(del.some((s) => s.tag === "same")).toBe(true);
    expect(del.filter((s) => s.tag === "del").map((s) => s.text)).toEqual(["40"]);
    expect(add.filter((s) => s.tag === "add").map((s) => s.text)).toEqual(["120"]);
  });

  it("merges neighbouring spans of the same tag", () => {
    const { del } = wordDiff("one two three", "one three");
    // "two" and the space beside it are one del span, not two.
    expect(del.filter((s) => s.tag === "del")).toHaveLength(1);
  });
});

describe("refineRuns", () => {
  it("pairs contiguous RUNS by index, not adjacent rows", () => {
    // THE gate. lineDiff prefers deletion on an LCS tie, so a two-line
    // replacement emits del,del,add,add. A rule that paired "a del immediately
    // followed by an add" would pair `bravo` with `ALPHA` — the wrong pair.
    const rows = lineDiff("alpha\nbravo\nzulu", "ALPHA\nBRAVO\nzulu");
    expect(rows.map((r) => r.tag)).toEqual(["del", "del", "add", "add", "same"]);

    const out = refineRuns(rows);
    expect(out.map((r) => r.tag)).toEqual(["del", "del", "add", "add", "same"]);
    // del[0] "alpha" refined against add[0] "ALPHA": entirely changed.
    expect(out[0].spans?.map((s) => s.tag)).toEqual(["del"]);
    expect(out[0].spans?.map((s) => s.text).join("")).toBe("alpha");
    // del[1] "bravo" refined against add[1] "BRAVO" — NOT against "ALPHA".
    expect(out[1].spans?.map((s) => s.text).join("")).toBe("bravo");
    expect(out[3].spans?.map((s) => s.text).join("")).toBe("BRAVO");
  });

  it("refines the one-line case (the degenerate del,add)", () => {
    const out = refineRuns(lineDiff("recruit 40 patients", "recruit 120 patients"));
    const del = out.find((r) => r.tag === "del");
    expect(del?.spans?.filter((s) => s.tag === "del").map((s) => s.text)).toEqual(["40"]);
  });

  it("leaves an unpaired row whole", () => {
    // Three deletions, one addition: dels[0] pairs, dels[1] and dels[2] do not.
    const rows: ReturnType<typeof lineDiff> = [
      { tag: "del", text: "one" },
      { tag: "del", text: "two" },
      { tag: "del", text: "three" },
      { tag: "add", text: "ONE" },
    ];
    const out = refineRuns(rows);
    expect(out[0].spans).toBeDefined();
    expect(out[1].spans).toBeUndefined();
    expect(out[2].spans).toBeUndefined();
  });

  it("leaves same rows untouched", () => {
    const out = refineRuns(lineDiff("a\nb", "a\nb"));
    expect(out.every((r) => r.tag === "same" && r.spans === undefined)).toBe(true);
  });

  it("preserves row order and count", () => {
    const rows = lineDiff("a\nb\nc\nd", "a\nB\nC\nd");
    expect(refineRuns(rows).map((r) => r.text)).toEqual(rows.map((r) => r.text));
  });
});

describe("tooLargeToDiff", () => {
  it("2,000 lines still diffs and 2,001 does not — the boundary is inclusive", () => {
    const ok = Array(MAX_DIFF_LINES).fill("x").join("\n");
    const tooBig = Array(MAX_DIFF_LINES + 1).fill("x").join("\n");
    expect(tooLargeToDiff(ok, ok)).toBe(false);
    expect(tooLargeToDiff(tooBig, ok)).toBe(true);
    expect(tooLargeToDiff(ok, tooBig)).toBe(true);
  });
});
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd frontend && pnpm vitest run src/cockpit/diff.refine.test.ts
```

Expected: FAIL — `refineRuns is not exported`. Confirm the header says **1 test file**, not 0.

- [ ] **Step 3: Implement**

Append to `frontend/src/cockpit/diff.ts` (do not touch `lineDiff`):

```ts
// ---------------------------------------------------------------------------
// Word-level refinement.
//
// The corpus this serves is paragraph-per-line markdown: measured on the live
// draft, 37 lines with a longest line of 1172 characters. A pure line diff
// strikes through a whole paragraph to show a two-character edit.
// ---------------------------------------------------------------------------

export type Span = { tag: "same" | "add" | "del"; text: string };

/** Split into words AND the whitespace between them, so the spans rejoin to
 *  the original string exactly. `split` with a capturing group keeps the
 *  separators; the filter drops the empty strings it produces at the edges. */
function tokens(s: string): string[] {
  return s.split(/(\s+)/).filter((t) => t !== "");
}

function pushSpan(out: Span[], tag: Span["tag"], text: string): void {
  const last = out[out.length - 1];
  if (last && last.tag === tag) last.text += text;
  else out.push({ tag, text });
}

/** The same LCS as lineDiff, over word tokens instead of lines. Returns one
 *  span list for each side: the `del` list reconstructs `before`, the `add`
 *  list reconstructs `after`. */
export function wordDiff(before: string, after: string): { del: Span[]; add: Span[] } {
  const a = tokens(before);
  const b = tokens(after);
  const n = a.length;
  const m = b.length;
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const del: Span[] = [];
  const add: Span[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      pushSpan(del, "same", a[i]);
      pushSpan(add, "same", b[j]);
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      pushSpan(del, "del", a[i++]);
    } else {
      pushSpan(add, "add", b[j++]);
    }
  }
  while (i < n) pushSpan(del, "del", a[i++]);
  while (j < m) pushSpan(add, "add", b[j++]);
  return { del, add };
}

/** Attach word-level spans to replaced lines.
 *
 *  Pairs contiguous RUNS by index, NOT adjacent rows. lineDiff prefers deletion
 *  on an LCS tie, so a two-line replacement emits del,del,add,add — a rule that
 *  paired "a del immediately followed by an add" would pair the SECOND deletion
 *  with the FIRST addition. Measured, not assumed:
 *
 *    lineDiff("alpha\nbravo\nzulu", "ALPHA\nBRAVO\nzulu")
 *      -> del:alpha del:bravo add:ALPHA add:BRAVO same:zulu
 *
 *  Rows are returned in the same order and count; only `spans` is added. A
 *  leftover row, where the runs are unequal in length, stays whole. */
export function refineRuns(rows: DiffRow[]): DiffRow[] {
  const out: DiffRow[] = [];
  let i = 0;
  while (i < rows.length) {
    if (rows[i].tag !== "del") {
      out.push(rows[i]);
      i++;
      continue;
    }
    const delStart = i;
    while (i < rows.length && rows[i].tag === "del") i++;
    const addStart = i;
    while (i < rows.length && rows[i].tag === "add") i++;
    const dels = rows.slice(delStart, addStart);
    const adds = rows.slice(addStart, i);
    const paired = Math.min(dels.length, adds.length);
    for (let k = 0; k < dels.length; k++) {
      out.push(k < paired ? { ...dels[k], spans: wordDiff(dels[k].text, adds[k].text).del } : dels[k]);
    }
    for (let k = 0; k < adds.length; k++) {
      out.push(k < paired ? { ...adds[k], spans: wordDiff(dels[k].text, adds[k].text).add } : adds[k]);
    }
  }
  return out;
}

/** lineDiff allocates a full (n+1)x(m+1) number matrix, quadratic in the line
 *  counts. Two 2,000-line documents is a 4M-entry matrix — the largest this
 *  approach should attempt. Today's largest document is 37 lines, so this is
 *  unreachable from real data and exists for the pathological case. Inclusive:
 *  exactly 2,000 still diffs. */
export const MAX_DIFF_LINES = 2000;

export function tooLargeToDiff(a: string, b: string): boolean {
  return a.split("\n").length > MAX_DIFF_LINES || b.split("\n").length > MAX_DIFF_LINES;
}
```

Also change the `DiffRow` type at the top of the file from:

```ts
export type DiffRow = { tag: "same" | "add" | "del"; text: string };
```

to:

```ts
// `spans` is OPTIONAL and added by refineRuns, so lineDiff's own output and
// both existing callers are unchanged.
export type DiffRow = { tag: "same" | "add" | "del"; text: string; spans?: Span[] };
```

`Span` is declared below `DiffRow` in the file; TypeScript hoists type declarations, so the forward reference is fine.

- [ ] **Step 4: Run the tests and the type check**

```bash
cd frontend && pnpm vitest run src/cockpit/diff.refine.test.ts && pnpm exec tsc --noEmit
```

Expected: `1 passed (1)` file, 10 tests. tsc silent.

- [ ] **Step 5: Confirm the existing diff tests still pass untouched**

```bash
cd frontend && pnpm vitest run src/cockpit/diff.test.ts 2>&1 | tail -5
```

Expected: PASS with the same test count as before this task. `lineDiff`'s signature and behaviour did not change.

- [ ] **Step 6: Prove the run-pairing gate bites**

Temporarily replace the run-pairing body of `refineRuns` with the naive adjacent-row rule — pair `rows[k]` with `rows[k+1]` whenever they are `del` then `add`. Re-run:

```bash
cd frontend && pnpm vitest run src/cockpit/diff.refine.test.ts
```

Expected: `pairs contiguous RUNS by index, not adjacent rows` **FAILS** (it will report `out[1].spans` reconstructing something other than `"bravo"`), while `refines the one-line case` still passes — which is exactly why the one-line test alone does not count. Restore.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/cockpit/diff.ts frontend/src/cockpit/diff.refine.test.ts
git commit -m "feat(diff): word-level refinement, runs paired by index"
```

---

### Task 6: One shared diff renderer

The renderer is an inline `<pre>` block duplicated **verbatim** in `cockpit/ProposalReview.tsx:72-78` and `editor/InlineProposalReview.tsx:73-79`. A version-compare surface would be the third copy. Extract first, convert both, then build on it.

`InlineProposalReview.tsx:39-47` carries content-aware React keys (tag + text + occurrence) that the other copy lacks — keep the better one.

**Files:**
- Create: `frontend/src/cockpit/DiffView.tsx`
- Create: `frontend/src/cockpit/DiffView.test.tsx`
- Modify: `frontend/src/cockpit/CockpitPanels.module.css` (append beside the rules at lines 99-105)
- Modify: `frontend/src/cockpit/ProposalReview.tsx` (replace the `<pre>` block at lines 72-78)
- Modify: `frontend/src/editor/InlineProposalReview.tsx` (replace the key-building block at lines 39-47 and the `<pre>` block at lines 73-79)

**Interfaces:**
- Consumes: `DiffRow`, `Span` (Task 5).
- Produces: `<DiffView rows={DiffRow[]} />` from `frontend/src/cockpit/DiffView.tsx`. Task 8 renders it.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/cockpit/DiffView.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DiffView } from "./DiffView";
import { lineDiff, refineRuns } from "./diff";

describe("DiffView", () => {
  it("tags each row so the CSS can colour it", () => {
    render(<DiffView rows={lineDiff("a\nb", "a\nB")} />);
    const rows = document.querySelectorAll("[data-diff]");
    expect(Array.from(rows).map((r) => r.getAttribute("data-diff"))).toEqual([
      "same",
      "del",
      "add",
    ]);
  });

  it("renders word spans when a row carries them", () => {
    render(<DiffView rows={refineRuns(lineDiff("recruit 40 patients", "recruit 120 patients"))} />);
    const dels = document.querySelectorAll('[data-diff="del"] span[data-span="del"]');
    expect(Array.from(dels).map((s) => s.textContent)).toEqual(["40"]);
    const adds = document.querySelectorAll('[data-diff="add"] span[data-span="add"]');
    expect(Array.from(adds).map((s) => s.textContent)).toEqual(["120"]);
  });

  it("renders a row with no spans as plain text", () => {
    render(<DiffView rows={[{ tag: "del", text: "gone" }]} />);
    expect(screen.getByText(/gone/)).toBeInTheDocument();
    expect(document.querySelectorAll("span[data-span]")).toHaveLength(0);
  });

  it("survives repeated identical lines", () => {
    // Content-aware keys disambiguate by occurrence; duplicate keys would make
    // React drop rows.
    render(<DiffView rows={[
      { tag: "del", text: "same text" },
      { tag: "del", text: "same text" },
    ]} />);
    expect(document.querySelectorAll('[data-diff="del"]')).toHaveLength(2);
  });
});
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd frontend && pnpm vitest run src/cockpit/DiffView.test.tsx
```

Expected: FAIL — `Failed to resolve import "./DiffView"`. Header must say **1 test file**.

- [ ] **Step 3: Write the component**

Create `frontend/src/cockpit/DiffView.tsx`:

```tsx
import styles from "./CockpitPanels.module.css";
import type { DiffRow } from "./diff";

/** The one diff renderer. Was duplicated verbatim in ProposalReview.tsx and
 *  InlineProposalReview.tsx; a third copy was about to appear in the version
 *  compare view.
 *
 *  Keys are content-aware (tag + text + occurrence), taken from the better of
 *  the two originals: diff rows are stateless and recomputed each render, so an
 *  array index would be a foot-gun if the diff ever reorders, and repeated
 *  identical lines are ordinary in prose. */
export function DiffView({ rows }: { rows: DiffRow[] }) {
  const seen: Record<string, number> = {};
  return (
    <pre className={styles.proposalDiff}>
      {rows.map((r) => {
        const base = `${r.tag}:${r.text}`;
        const n = (seen[base] = (seen[base] ?? 0) + 1);
        const prefix = r.tag === "add" ? "+ " : r.tag === "del" ? "- " : "  ";
        return (
          <div key={`${base}#${n}`} data-diff={r.tag}>
            {prefix}
            {r.spans
              ? r.spans.map((s, k) => (
                  <span key={`${s.tag}:${k}`} data-span={s.tag}>
                    {s.text}
                  </span>
                ))
              : r.text}
          </div>
        );
      })}
    </pre>
  );
}
```

- [ ] **Step 4: Add the span CSS**

Append to `frontend/src/cockpit/CockpitPanels.module.css`, directly after the two `.proposalDiff div[data-diff=...]` rules at lines 104-105:

```css
/* Word-level spans sit INSIDE an already-tinted row, so they are stronger than
   the row tint (0.15) to read as "this is the part that changed". A row with no
   spans keeps the old whole-line appearance. */
.proposalDiff span[data-span="add"] { background: rgba(46, 160, 67, 0.4); }
.proposalDiff span[data-span="del"] {
  background: rgba(248, 81, 73, 0.4);
  text-decoration: line-through;
}
```

- [ ] **Step 5: Run the tests and watch them pass**

```bash
cd frontend && pnpm vitest run src/cockpit/DiffView.test.tsx && pnpm exec tsc --noEmit
```

Expected: `1 passed (1)` file, 4 tests.

- [ ] **Step 6: Convert `ProposalReview.tsx`**

Replace the `<pre>` block (lines 72-78):

```tsx
              <pre className={styles.proposalDiff}>
                {lineDiff(currentFor(p.section_index), p.new_text ?? "").map((r, k) => (
                  <div key={k} data-diff={r.tag}>
                    {(r.tag === "add" ? "+ " : r.tag === "del" ? "- " : "  ") + r.text}
                  </div>
                ))}
              </pre>
```

with:

```tsx
              <DiffView rows={refineRuns(lineDiff(currentFor(p.section_index), p.new_text ?? ""))} />
```

Change the import line `import { lineDiff } from "./diff";` to:

```tsx
import { DiffView } from "./DiffView";
import { lineDiff, refineRuns } from "./diff";
```

If `styles` becomes unused in this file, remove its import — `pnpm exec tsc --noEmit` will tell you (it is configured to error on unused locals; a previous task in this repo hit exactly that).

- [ ] **Step 7: Convert `InlineProposalReview.tsx`**

Delete the key-building block at lines 39-47 (the `seen` record and the `rows` map) — `DiffView` now owns that logic. Replace the `<pre>` block (lines 73-79) with:

```tsx
      <DiffView rows={refineRuns(lineDiff(currentText, pc.new_text ?? ""))} />
```

Update the imports the same way, adding `import { DiffView } from "../cockpit/DiffView";` (note the `../cockpit/` prefix — this file lives in `frontend/src/editor/`).

- [ ] **Step 8: Run every affected test file**

```bash
cd frontend && pnpm vitest run src/cockpit src/editor 2>&1 | tail -15 && pnpm exec tsc --noEmit
```

Expected: all PASS. Note the printed **file count** — if it dropped versus a run before this task, something failed to collect.

- [ ] **Step 9: Commit**

```bash
git add frontend/src/cockpit/DiffView.tsx frontend/src/cockpit/DiffView.test.tsx \
        frontend/src/cockpit/CockpitPanels.module.css \
        frontend/src/cockpit/ProposalReview.tsx frontend/src/editor/InlineProposalReview.tsx
git commit -m "refactor(diff): one DiffView, both call sites converted, word spans styled"
```

---

### Task 7: The History tab — list, states, and reading one version

**Files:**
- Modify: `frontend/src/cockpit/nav.ts` (the `TopTab` union)
- Modify: `frontend/src/cockpit/cockpitNav.ts` (`TOP_TABS`)
- Create: `frontend/src/cockpit/historyNav.ts`
- Create: `frontend/src/cockpit/historyNav.test.ts`
- Create: `frontend/src/cockpit/HistoryPanel.tsx`
- Create: `frontend/src/cockpit/HistoryPanel.test.tsx`
- Modify: `frontend/src/routes/Cockpit.tsx` (the `Tabs.List` at lines 80-85 and the panel switch at lines 88-91)

**Interfaces:**
- Consumes: `useVersions`, `useVersion` (Task 4).
- Produces:
  - `useHistoryNav(): { artifact: Artifact; a: string | null; b: string | null; setArtifact(x: Artifact): void; toggle(sha: string): void }` from `frontend/src/cockpit/historyNav.ts`
  - `<HistoryPanel id={string} />` from `frontend/src/cockpit/HistoryPanel.tsx`
  - `"history"` as a member of `TopTab`

- [ ] **Step 1: Write the failing URL-state test**

Create `frontend/src/cockpit/historyNav.test.ts`:

```ts
import { renderHook, act } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { useHistoryNav } from "./historyNav";

function wrapperFor(initial: string) {
  return ({ children }: { children: ReactNode }) =>
    createElement(MemoryRouter, { initialEntries: [initial] }, children);
}

describe("useHistoryNav", () => {
  it("defaults to the draft with nothing selected", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history"),
    });
    expect(result.current.artifact).toBe("draft");
    expect(result.current.a).toBeNull();
    expect(result.current.b).toBeNull();
  });

  it("reads both shas from the URL", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history&artifact=spec&a=abc1234&b=def5678"),
    });
    expect(result.current.artifact).toBe("spec");
    expect(result.current.a).toBe("abc1234");
    expect(result.current.b).toBe("def5678");
  });

  it("drops a sha that is not sha-shaped rather than fetching it", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history&a=--output=x&b=def5678"),
    });
    expect(result.current.a).toBeNull();
    expect(result.current.b).toBe("def5678");
  });

  it("ignores an unknown artifact and falls back to draft", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history&artifact=bogus"),
    });
    expect(result.current.artifact).toBe("draft");
  });

  it("toggle fills a then b, and toggling a selected sha removes it", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history"),
    });
    act(() => result.current.toggle("aaaaaaa"));
    expect(result.current.a).toBe("aaaaaaa");
    act(() => result.current.toggle("bbbbbbb"));
    expect(result.current.b).toBe("bbbbbbb");
    act(() => result.current.toggle("aaaaaaa"));
    expect(result.current.a).toBe("bbbbbbb");
    expect(result.current.b).toBeNull();
  });

  it("a third selection replaces the older of the two", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb"),
    });
    act(() => result.current.toggle("ccccccc"));
    expect(result.current.a).toBe("bbbbbbb");
    expect(result.current.b).toBe("ccccccc");
  });

  it("changing artifact clears the selection", () => {
    const { result } = renderHook(() => useHistoryNav(), {
      wrapper: wrapperFor("/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb"),
    });
    act(() => result.current.setArtifact("spec"));
    expect(result.current.artifact).toBe("spec");
    expect(result.current.a).toBeNull();
    expect(result.current.b).toBeNull();
  });
});
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd frontend && pnpm vitest run src/cockpit/historyNav.test.ts
```

Expected: FAIL — `Failed to resolve import "./historyNav"`.

- [ ] **Step 3: Write the URL-state hook**

Create `frontend/src/cockpit/historyNav.ts`:

```ts
import { useCallback } from "react";
import { useSearchParams } from "react-router-dom";

import type { Artifact } from "../api/proposals";

/** Mirrors ports/shas.py's SHA_RE. A sha arriving from the URL is user input:
 *  the backend rejects a bad one with 422, but there is no reason to spend the
 *  request. */
const SHA_RE = /^[0-9a-fA-F]{7,64}$/;

function shaParam(sp: URLSearchParams, key: string): string | null {
  const v = sp.get(key);
  return v != null && SHA_RE.test(v) ? v : null;
}

function artifactParam(sp: URLSearchParams): Artifact {
  const v = sp.get("artifact");
  return v === "spec" ? "spec" : "draft";
}

export type HistoryNav = {
  artifact: Artifact;
  /** The OLDER selection slot. */
  a: string | null;
  /** The NEWER selection slot. */
  b: string | null;
  setArtifact: (x: Artifact) => void;
  toggle: (sha: string) => void;
};

/** The History tab's URL state: `?tab=history&artifact=…&a=…&b=…`.
 *
 *  Both shas live in the URL, not just the artifact, because a link to a
 *  comparison must reopen that comparison. Kept beside useCockpitNav rather
 *  than inside it: `?tab` and `?sec` are shared across the AppShell boundary,
 *  these three are the History tab's own. */
export function useHistoryNav(): HistoryNav {
  const [sp, setSp] = useSearchParams();
  const artifact = artifactParam(sp);
  const a = shaParam(sp, "a");
  const b = shaParam(sp, "b");

  const write = useCallback(
    (next: { artifact?: Artifact; a?: string | null; b?: string | null }) =>
      setSp((prev) => {
        const out = new URLSearchParams(prev);
        if (next.artifact !== undefined) out.set("artifact", next.artifact);
        for (const key of ["a", "b"] as const) {
          if (next[key] === undefined) continue;
          const v = next[key];
          if (v == null) out.delete(key);
          else out.set(key, v);
        }
        return out;
      }),
    [setSp],
  );

  const setArtifact = useCallback(
    // The selection is cleared: a sha from the draft's history is not a version
    // of the spec, and leaving it would fetch a 404.
    (x: Artifact) => write({ artifact: x, a: null, b: null }),
    [write],
  );

  const toggle = useCallback(
    (sha: string) => {
      if (sha === a) write({ a: b, b: null });
      else if (sha === b) write({ b: null });
      else if (a == null) write({ a: sha });
      else if (b == null) write({ b: sha });
      // Both full: drop the older slot and slide along, so a third click always
      // means "compare this with the one I picked most recently".
      else write({ a: b, b: sha });
    },
    [a, b, write],
  );

  return { artifact, a, b, setArtifact, toggle };
}
```

- [ ] **Step 4: Run and watch it pass**

```bash
cd frontend && pnpm vitest run src/cockpit/historyNav.test.ts && pnpm exec tsc --noEmit
```

Expected: `1 passed (1)` file, 7 tests.

- [ ] **Step 5: Write the failing panel test**

Create `frontend/src/cockpit/HistoryPanel.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as versionsMod from "../api/versions";
import { HistoryPanel } from "./HistoryPanel";

function wrap(ui: ReactNode, initial = "/cockpit/ws1?tab=history") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
    </MemoryRouter>,
  );
}

const ROW = (
  sha: string,
  subject: string,
  created_by: string | null = "user",
  turn_prompt: string | null = null,
) => ({
  commit_sha: sha,
  committed_at: "2026-08-28T10:00:00+00:00",
  subject,
  created_by,
  triggering_turn_id: turn_prompt == null ? null : "turn-1",
  turn_prompt,
});

function mockList(data: Partial<versionsMod.VersionList>) {
  vi.spyOn(versionsMod, "useVersions").mockReturnValue({
    data: { versions: [], truncated: false, history_available: true, ...data },
    isLoading: false,
    isError: false,
  } as never);
}

function mockVersion(markdown: string | null) {
  vi.spyOn(versionsMod, "useVersion").mockReturnValue({
    data: markdown == null ? null : { commit_sha: "x", artifact: "draft", markdown },
    isLoading: false,
    isError: false,
  } as never);
}

describe("HistoryPanel", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("shows the zero-version invitation, not an empty list", () => {
    mockList({ versions: [], history_available: true });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />);
    expect(screen.getByText(/No versions yet/i)).toBeInTheDocument();
  });

  it("distinguishes an unreadable history from an empty one", () => {
    mockList({ versions: [], history_available: false });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />);
    expect(screen.getByText(/not available for this workspace yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/No versions yet/i)).not.toBeInTheDocument();
  });

  it("says so when the list is truncated", () => {
    mockList({ versions: [ROW("aaaaaaa", "one")], truncated: true });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />);
    expect(screen.getByText(/200 most recent versions/i)).toBeInTheDocument();
  });

  it("renders a row's role badge and its subject verbatim", () => {
    mockList({ versions: [ROW("aaaaaaa", "user draft edit: Aims (user:7)")] });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />);
    expect(screen.getByText("user draft edit: Aims (user:7)")).toBeInTheDocument();
    expect(screen.getByText("user")).toBeInTheDocument();
  });

  it("shows WHY a version exists when a prompt caused it", () => {
    mockList({
      versions: [ROW("aaaaaaa", "agent draft edit", "agent", "Tighten the aims section")],
    });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />);
    expect(screen.getByText(/because you asked: Tighten the aims section/i)).toBeInTheDocument();
  });

  it("shows no reason line at all when there is no prompt", () => {
    mockList({ versions: [ROW("aaaaaaa", "collab checkpoint: draft", null)] });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />);
    expect(screen.queryByText(/because you asked/i)).not.toBeInTheDocument();
  });

  it("renders a row with no provenance as ordinary, not as an error", () => {
    mockList({ versions: [ROW("aaaaaaa", "collab checkpoint: draft", null)] });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />);
    expect(screen.getByText("not recorded")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("opens a version read-only when one row is selected", async () => {
    mockList({ versions: [ROW("aaaaaaa", "one"), ROW("bbbbbbb", "two")] });
    mockVersion("# an old draft\n");
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa");
    expect(screen.getByText(/# an old draft/)).toBeInTheDocument();
    // Read-only: no textbox anywhere in the panel.
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("selecting a row puts its sha in the URL", async () => {
    mockList({ versions: [ROW("aaaaaaa", "one")] });
    mockVersion(null);
    wrap(<HistoryPanel id="ws1" />);
    await userEvent.click(screen.getByRole("button", { name: /one/ }));
    await waitFor(() => expect(window.location.search).toContain("a=aaaaaaa"));
  });
});
```

**Note on the last test:** `MemoryRouter` does not touch `window.location`. If that assertion fails for that reason, assert on the rendered selected-state instead — e.g. `expect(screen.getByRole("button", { name: /one/ })).toHaveAttribute("aria-pressed", "true")` — and keep the URL round-trip covered by `historyNav.test.ts`, which tests it directly. Do not delete the test; convert it.

- [ ] **Step 6: Run it and watch it fail**

```bash
cd frontend && pnpm vitest run src/cockpit/HistoryPanel.test.tsx
```

Expected: FAIL — `Failed to resolve import "./HistoryPanel"`.

- [ ] **Step 7: Write the panel**

Create `frontend/src/cockpit/HistoryPanel.tsx`:

```tsx
import { Badge, Group, SegmentedControl, Stack, Text, Title } from "@mantine/core";

import type { Artifact } from "../api/proposals";
import { useVersion, useVersions } from "../api/versions";
import styles from "./CockpitPanels.module.css";
import { useHistoryNav } from "./historyNav";

function VersionRowButton({
  subject,
  committedAt,
  createdBy,
  turnPrompt,
  selected,
  onClick,
}: {
  subject: string;
  committedAt: string;
  createdBy: string | null;
  turnPrompt: string | null;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onClick}
      style={{
        display: "block",
        width: "100%",
        textAlign: "left",
        background: selected ? "var(--app-sidebar)" : "transparent",
        border: "1px solid var(--app-border)",
        borderRadius: 6,
        padding: "0.4rem 0.6rem",
        cursor: "pointer",
        color: "inherit",
      }}
    >
      <Group gap="xs" wrap="nowrap">
        <Badge size="sm" variant="light">
          {createdBy ?? "not recorded"}
        </Badge>
        <Text size="sm" style={{ flex: 1 }}>
          {subject}
        </Text>
        <Text size="xs" c="dimmed">
          {new Date(committedAt).toLocaleString()}
        </Text>
      </Group>
      {/* The "why" half of goal 1. Rendered only when a turn caused this
          version; absence is ordinary, so there is no placeholder. */}
      {turnPrompt && (
        <Text size="xs" c="dimmed" mt={2}>
          because you asked: {turnPrompt}
        </Text>
      )}
    </button>
  );
}

export function HistoryPanel({ id }: { id: string }) {
  const nav = useHistoryNav();
  const list = useVersions(id, nav.artifact);
  const one = useVersion(id, nav.artifact, nav.b == null ? nav.a : null);

  const data = list.data;

  return (
    <Stack gap="sm" p="sm">
      <Group justify="space-between">
        <Title order={3}>Version history</Title>
        <SegmentedControl
          size="xs"
          value={nav.artifact}
          onChange={(v) => nav.setArtifact(v as Artifact)}
          data={[
            { label: "Draft", value: "draft" },
            { label: "Spec", value: "spec" },
          ]}
        />
      </Group>

      {list.isLoading && (
        <Text size="sm" c="dimmed">
          Loading versions…
        </Text>
      )}

      {data && !data.history_available && (
        <Text size="sm" c="dimmed">
          Version history is not available for this workspace yet.
        </Text>
      )}

      {data && data.history_available && data.versions.length === 0 && (
        <Text size="sm" c="dimmed">
          No versions yet. Versions appear when you save one, or when the assistant edits the
          draft.
        </Text>
      )}

      {data && data.versions.length > 0 && (
        <Stack gap="xs">
          {data.versions.map((v) => (
            <VersionRowButton
              key={v.commit_sha}
              subject={v.subject}
              committedAt={v.committed_at}
              createdBy={v.created_by}
              turnPrompt={v.turn_prompt}
              selected={v.commit_sha === nav.a || v.commit_sha === nav.b}
              onClick={() => nav.toggle(v.commit_sha)}
            />
          ))}
          {data.truncated && (
            <Text size="xs" c="dimmed">
              Showing the 200 most recent versions.
            </Text>
          )}
        </Stack>
      )}

      {data && data.versions.length === 1 && (
        <Text size="xs" c="dimmed">
          Comparing needs two versions.
        </Text>
      )}

      {/* One selected: read it. An old version must never look editable, so it
          renders as text the way ReviewPanel renders a review file — not the
          Tiptap editor. */}
      {one.data && (
        <pre className={styles.reviewDoc}>{one.data.markdown}</pre>
      )}
      {nav.b == null && nav.a != null && one.isError && (
        <Text size="sm" c="dimmed">
          That version could not be read.
        </Text>
      )}
    </Stack>
  );
}
```

- [ ] **Step 8: Add the tab**

In `frontend/src/cockpit/nav.ts`, change:

```ts
export type TopTab = "draft" | "spec" | "scorecard" | "review";
```

to:

```ts
export type TopTab = "draft" | "spec" | "scorecard" | "review" | "history";
```

In `frontend/src/cockpit/cockpitNav.ts`, change:

```ts
export const TOP_TABS: TopTab[] = ["draft", "spec", "scorecard", "review"];
```

to:

```ts
export const TOP_TABS: TopTab[] = ["draft", "spec", "scorecard", "review", "history"];
```

In `frontend/src/routes/Cockpit.tsx`, add after line 84 (`<Tabs.Tab value="review">Panel review</Tabs.Tab>`):

```tsx
            <Tabs.Tab value="history">History</Tabs.Tab>
```

and after line 91 (`{tab === "review" && <ReviewPanel id={id} />}`):

```tsx
          {tab === "history" && <HistoryPanel id={id} />}
```

with the import `import { HistoryPanel } from "../cockpit/HistoryPanel";` beside the other panel imports.

- [ ] **Step 9: Run everything and type-check**

```bash
cd frontend && pnpm vitest run src/cockpit src/routes 2>&1 | tail -15 && pnpm exec tsc --noEmit
```

Expected: all PASS. `Cockpit.test.tsx` may assert the tab list — if it fails on a count, update that assertion to include History; that is a real expected change, not a workaround.

- [ ] **Step 10: Prove the zero-state gate bites**

Temporarily collapse the two empty states into one — delete the `!data.history_available` block. Re-run `pnpm vitest run src/cockpit/HistoryPanel.test.tsx`. Expected: `distinguishes an unreadable history from an empty one` **FAILS**. Restore.

- [ ] **Step 11: Commit**

```bash
git add frontend/src/cockpit/historyNav.ts frontend/src/cockpit/historyNav.test.ts \
        frontend/src/cockpit/HistoryPanel.tsx frontend/src/cockpit/HistoryPanel.test.tsx \
        frontend/src/cockpit/nav.ts frontend/src/cockpit/cockpitNav.ts \
        frontend/src/routes/Cockpit.tsx
git commit -m "feat(cockpit): History tab — git-sourced version list, read one version"
```

---

### Task 8: Comparing two versions

**Files:**
- Modify: `frontend/src/cockpit/HistoryPanel.tsx`
- Modify: `frontend/src/cockpit/HistoryPanel.test.tsx` (append)

**Interfaces:**
- Consumes: `useVersion` (Task 4), `lineDiff`/`refineRuns`/`tooLargeToDiff` (Task 5), `DiffView` (Task 6), `useHistoryNav` (Task 7).
- Produces: nothing new — this completes `HistoryPanel`.

- [ ] **Step 1: Write the failing tests**

Append to `frontend/src/cockpit/HistoryPanel.test.tsx`:

```tsx
describe("HistoryPanel comparison", () => {
  beforeEach(() => vi.restoreAllMocks());

  /** Two versions: the OLDER one is second in the list (newest-first). */
  function mockPair(olderMd: string, newerMd: string) {
    mockList({
      versions: [ROW("bbbbbbb", "newer"), ROW("aaaaaaa", "older")],
    });
    vi.spyOn(versionsMod, "useVersion").mockImplementation(
      ((_id: string, _artifact: string, sha: string | null) => ({
        data:
          sha === "aaaaaaa"
            ? { commit_sha: "aaaaaaa", artifact: "draft", markdown: olderMd }
            : sha === "bbbbbbb"
              ? { commit_sha: "bbbbbbb", artifact: "draft", markdown: newerMd }
              : null,
        isLoading: false,
        isError: false,
      })) as never,
    );
  }

  it("diffs older against newer, refined to word level", () => {
    mockPair("We recruit 40 patients.\n", "We recruit 120 patients.\n");
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb");
    expect(
      Array.from(document.querySelectorAll('[data-diff="del"] span[data-span="del"]')).map(
        (s) => s.textContent,
      ),
    ).toEqual(["40"]);
    expect(
      Array.from(document.querySelectorAll('[data-diff="add"] span[data-span="add"]')).map(
        (s) => s.textContent,
      ),
    ).toEqual(["120"]);
  });

  it("selection ORDER does not change the view — direction is a contract", () => {
    // THE gate. lineDiff assigns `del` to its FIRST argument, so the panel must
    // sort by the list's own order (newest-first) and always pass older, newer.
    mockPair("We recruit 40 patients.\n", "We recruit 120 patients.\n");
    const forward = wrap(
      <HistoryPanel id="ws1" />,
      "/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb",
    );
    const forwardHtml = document.querySelector("[data-testid='version-diff']")?.innerHTML;
    forward.unmount();

    mockPair("We recruit 40 patients.\n", "We recruit 120 patients.\n");
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=bbbbbbb&b=aaaaaaa");
    const reverseHtml = document.querySelector("[data-testid='version-diff']")?.innerHTML;

    expect(reverseHtml).toBe(forwardHtml);
  });

  it("falls back with a reason above the 2,000-line ceiling", () => {
    const huge = Array(2001).fill("x").join("\n");
    mockPair(huge, huge + "\ny");
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb");
    expect(screen.getByText(/too large to compare/i)).toBeInTheDocument();
    expect(document.querySelector("[data-testid='version-diff']")).toBeNull();
  });

  it("exactly 2,000 lines still diffs — the boundary is inclusive", () => {
    const atLimit = Array(2000).fill("x").join("\n");
    mockPair(atLimit, atLimit.replace("x", "y"));
    wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb");
    expect(screen.queryByText(/too large to compare/i)).toBeNull();
    expect(document.querySelector("[data-testid='version-diff']")).not.toBeNull();
  });
});
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd frontend && pnpm vitest run src/cockpit/HistoryPanel.test.tsx
```

Expected: the four new tests FAIL (no `version-diff` element exists). The Task 7 tests must still PASS.

- [ ] **Step 3: Implement the comparison**

In `frontend/src/cockpit/HistoryPanel.tsx`, add the imports:

```tsx
import { DiffView } from "./DiffView";
import { lineDiff, refineRuns, tooLargeToDiff } from "./diff";
```

Replace the single `const one = useVersion(...)` line with both fetches, and add the ordering logic. The `useVersion` hook is disabled when its sha is null, so the second call costs nothing while only one row is selected:

```tsx
  const nav = useHistoryNav();
  const list = useVersions(id, nav.artifact);
  const data = list.data;

  // Direction is part of the contract, not an accident of selection order.
  // lineDiff assigns `del` to its FIRST argument, so compare must always pass
  // older, newer. The list is newest-first, so the LATER index is the older
  // version. Selecting the two rows in the opposite order must produce an
  // identical view — that is a test, not a convention.
  const order = data?.versions.map((v) => v.commit_sha) ?? [];
  const both = nav.a != null && nav.b != null;
  const [olderSha, newerSha] =
    both && order.indexOf(nav.a as string) < order.indexOf(nav.b as string)
      ? [nav.b, nav.a]
      : [nav.a, nav.b];

  const older = useVersion(id, nav.artifact, both ? olderSha : nav.a);
  const newer = useVersion(id, nav.artifact, both ? newerSha : null);
```

Then replace the single-version render block at the end of the component with:

```tsx
      {/* One selected: read it. An old version must never look editable, so it
          renders as text the way ReviewPanel renders a review file — not the
          Tiptap editor. */}
      {!both && older.data && <pre className={styles.reviewDoc}>{older.data.markdown}</pre>}
      {!both && nav.a != null && older.isError && (
        <Text size="sm" c="dimmed">
          That version could not be read.
        </Text>
      )}

      {both && (older.isLoading || newer.isLoading) && (
        <Text size="sm" c="dimmed">
          Loading both versions…
        </Text>
      )}
      {both && (older.isError || newer.isError) && (
        <Text size="sm" c="dimmed">
          One of those versions could not be read.
        </Text>
      )}
      {both && older.data && newer.data && (
        tooLargeToDiff(older.data.markdown, newer.data.markdown) ? (
          <Stack gap="xs">
            <Text size="sm" c="dimmed">
              These versions are too large to compare (over 2,000 lines). Open each one on its
              own instead.
            </Text>
          </Stack>
        ) : (
          <div data-testid="version-diff">
            <DiffView
              rows={refineRuns(lineDiff(older.data.markdown, newer.data.markdown))}
            />
          </div>
        )
      )}
```

- [ ] **Step 4: Run the tests and type-check**

```bash
cd frontend && pnpm vitest run src/cockpit/HistoryPanel.test.tsx && pnpm exec tsc --noEmit
```

Expected: all PASS — the Task 7 tests and the four new ones.

- [ ] **Step 5: Prove the direction gate bites**

Temporarily replace the ordering logic with the naive `const [olderSha, newerSha] = [nav.a, nav.b];`. Re-run. Expected: `selection ORDER does not change the view` **FAILS**. Restore.

- [ ] **Step 6: Prove the ceiling gate bites**

Temporarily change `MAX_DIFF_LINES` in `frontend/src/cockpit/diff.ts` to `1999`. Re-run `pnpm vitest run src/cockpit`. Expected: `exactly 2,000 lines still diffs` **FAILS**. Restore to `2000`.

- [ ] **Step 7: Run the whole frontend suite and the whole backend suite**

```bash
cd frontend && pnpm vitest run 2>&1 | tail -8 && pnpm exec tsc --noEmit && pnpm build 2>&1 | tail -5
cd .. && uv run --python 3.12 pytest -n 16 --dist loadfile 2>&1 | tail -8
uv run ruff check src tests
```

Expected: frontend green with a file count **at least 3 higher** than before this plan (versions, DiffView, historyNav, HistoryPanel, diff.refine — five new files); `pnpm build` succeeds; backend shows **`1 skipped`** (the `ANTHROPIC_API_KEY`-gated live-SDK test) and zero FAILED. The pass total moves with every slice — read `1 skipped` as the signal, not the total. If you see ERRORs with zero FAILED, an import is broken.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/cockpit/HistoryPanel.tsx frontend/src/cockpit/HistoryPanel.test.tsx
git commit -m "feat(cockpit): compare two versions, older-first, word-level, 2000-line ceiling"
```

---

## Out of scope for this plan — do not build

- **§9 Restore / going back to a version.** Phase 2. It is a write, it must land in the live collab room as well as git, and it must thread ONE session and ONE transaction through the whole operation (advisory xact locks are re-entrant on the same session; the hazard is opening a second Session while the first holds the mutex). It needs its own plan and a concurrent-writer test.
- **The collab-normalisation boundary detector (§7.4).** Re-verified 2026-08-28: **0 of 13** repos carry a `collab checkpoint:` commit. Building against zero instances means building against a guess. It becomes required before the first one exists.
- **Branches, merging, a branch graph.** Ruled out with measured reasons in spec §3.
- **Any change to `lineDiff`'s signature or its existing tests.** The word pass is additive.
