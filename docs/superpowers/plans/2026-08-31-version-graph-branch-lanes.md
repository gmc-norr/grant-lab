# Version Graph with Branch Lanes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the cockpit's stacked, unstyled branch picker plus single-lane
version list with one VS Code–style Versions pane: a real multi-branch commit
graph, branch pills you can switch from, and "branch from this commit".

**Architecture:** `gitio.list_commits` learns to report parents and to read all
branches; a new `gitio.branch_tips` reports where each branch head points. The
graph's column layout is computed by a **pure** frontend module
(`versionLanes.ts`) so it can be property-tested exhaustively, and drawn by a
per-row SVG strip. `BranchPicker` is deleted and its behaviour moves into a
`BranchBar` inside the Versions pane.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x, `uv`, `ruff`, `pytest`
(+ testcontainers Postgres). React 18 + Vite + Mantine + TanStack Query +
`openapi-fetch`, `pnpm`, `vitest`.

**Spec:** `docs/superpowers/specs/2026-08-31-version-graph-branch-lanes-design.md`
(commit `7ed12961`, after one outside review round; all 6 findings acted on).
**Settled points:** `.superpowers/review/version-graph-lanes-settled.md` — read it.
Several of the constraints below look like arbitrary fussiness and are not; the
settled file records what breaks without each one.

## Global Constraints

- **`--parents` is passed on EVERY `git log` call in `list_commits`**, with or
  without a pathspec. Under a pathspec git rewrites parent links; without
  `--parents`, `%P` reports parents that are not in the returned set. Measured.
- **`all_branches=True` emits `--branches HEAD --topo-order`.** `--branches`
  alone omits a detached-HEAD commit. Measured. `HEAD` is not optional.
- **The lane algorithm's step 3 must NOT deduplicate parent lanes.** Several
  lanes may wait for the same sha. Deduplicating makes two branches converge one
  row early — a merge the history does not contain.
- **`versionLanes` is tested for FIDELITY, not just continuity.** A
  continuity-only checker passes the buggy deduplicating version. Measured.
- **`BranchListOut.tips` has NO default value.** A `= {}` default lets the
  switch route return an empty map that no test notices.
- **The lane SVG's width is the list-wide maximum**, computed once in
  `VersionGraph`, never each row's own `columns`.
- **`HistoryPanel.tsx` is not touched by this plan.** Its restructure is a
  separate deferred item.
- **Fixed row height `22px`; `LANE_W = 14`.** Lane strips only join into
  continuous lines if every row is the same height.
- **`docs/ARCHITECTURE.md` is generated.** Never hand-edit. Regenerate with
  `uv run python scripts/gen_architecture.py` in the same commit that moves code.
- **Test suite:** `uv run pytest -n 16 --dist loadfile`. Expected tail is
  `… passed, 1 skipped`. **The `1 skipped` is the signal, not the total** — it is
  a real-SDK test gated on `ANTHROPIC_API_KEY`. `0 skipped` means a live billed
  API call just ran. Never use `-n auto` (resolves to 72 here) or
  `--dist loadgroup`.
- **Frontend gates:** `cd frontend && pnpm exec tsc --noEmit && pnpm vitest run`.
- **`pnpm add` is blocked** by a 7-day release-age cooldown. This plan adds no
  new dependency; if you think you need one, you have gone off-plan.

---

## File Structure

**Backend**

| File | Responsibility after this plan |
|---|---|
| `src/call_manager/proposal_assist/citations/gitio.py` | git primitives: `CommitInfo` carries parents; `list_commits` can read all branches; new `branch_tips`, `commit_exists`; `create_branch` takes a start point |
| `src/call_manager/proposal_assist/branches.py` | branch service: `list_workspace_branches` also returns tips; `create_workspace_branch` accepts `from_sha`; new `BranchStartUnknown` |
| `src/call_manager/proposal_assist/web/branches.py` | branch routes: `BranchIn.from_sha`, `BranchListOut.tips`, `BranchStartUnknown → 404` |
| `src/call_manager/proposal_assist/web/versions.py` | history route: rows carry `parents`; reads all branches |

**Frontend**

| File | Responsibility |
|---|---|
| `frontend/src/cockpit/versionLanes.ts` | **pure** column assignment. No React, no I/O. |
| `frontend/src/cockpit/VersionLane.tsx` | draws one row's SVG strip |
| `frontend/src/cockpit/BranchBar.tsx` | current branch + menu + the three dialogs |
| `frontend/src/cockpit/VersionGraph.tsx` | the pane: header, bar, rows, pills, row menu |
| `frontend/src/api/branches.ts` | `tips`, `from_sha` |
| `frontend/src/api/versions.ts` | `parents` |
| ~~`frontend/src/cockpit/panes/BranchPicker.tsx`~~ | **deleted** |

---

## Task 1: `CommitInfo` carries parents

**Files:**
- Modify: `src/call_manager/proposal_assist/citations/gitio.py`
- Test: `tests/proposal_assist/citations/test_gitio_list_commits.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `CommitInfo(sha: str, authored_at: datetime, subject: str, parents: tuple[str, ...])`.
  `list_commits(repo_dir, *, limit, pathspec=None) -> tuple[list[CommitInfo], bool]`
  — signature unchanged, `CommitInfo` gains a field with a default.

**Background the implementer needs.** `list_commits` streams `git log -z` output
as NUL-separated fields and parses them **three at a time**. Adding parents makes
it four at a time, and three separate constants encode that "three". Miss one and
the parser shifts every field by one, which surfaces as an unparseable date, not
as a missing parent.

There is also a byte cap: `_read_capped_fields` keeps at most `_MAX_FIELD_BYTES`
(`gitio.py:655`, `= _MAX_SUBJECT_CHARS * 4` = 2000) of **every** field. At 41
bytes per sha-1-plus-space that is 48 parents, and the cut can land mid-sha. A
partial sha would become a graph lane waiting for a commit that does not exist —
a line running off the bottom of the pane for no reason. This application never
creates a merge at all, so this is a defence for imported or damaged repos, which
is the population `list_commits` exists to inspect.

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/citations/test_gitio_list_commits.py`.

That file already has `_repo(tmp_path, n_commits)` at line 24, but it always
writes `proposal.md` and returns no shas, so it cannot express these cases. Add
two helpers beside it, matching the file's existing style — it configures the
identity with `-c user.email=…` per invocation rather than via the environment:

```python
def _empty_repo(tmp_path: Path) -> Path:
    """Like `_repo(tmp_path, 0)` but with a named initial branch, so the
    branch-aware tests below have something deterministic to check out from."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=repo, check=True)
    return repo


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *argv], cwd=repo, check=True
    )


def _commit(repo: Path, name: str, body: str, message: str) -> str:
    """Commit *body* to *name* and return the resulting full sha."""
    (repo / name).write_text(body, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
```

`subprocess`, `Path`, `pytest` and `list_commits` are already imported at the top
of that file; check the `from … gitio import (` block at line 16 and add anything
missing rather than adding a second import statement.

```python
def test_a_root_commit_has_no_parents(tmp_path):
    repo = _empty_repo(tmp_path)
    _commit(repo, "proposal.md", "v1", "c1")
    commits, _ = list_commits(repo, limit=10)
    assert commits[0].parents == ()


def test_a_child_commit_names_its_parent(tmp_path):
    repo = _empty_repo(tmp_path)
    first = _commit(repo, "proposal.md", "v1", "c1")
    _commit(repo, "proposal.md", "v2", "c2")
    commits, _ = list_commits(repo, limit=10)
    assert commits[0].parents == (first,)


def test_a_merge_commit_names_both_parents(tmp_path):
    repo = _empty_repo(tmp_path)
    _commit(repo, "proposal.md", "a\n", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    feature = _commit(repo, "proposal.md", "a\nF\n", "feature edit")
    _git(repo, "checkout", "-q", "main")
    main = _commit(repo, "proposal.md", "M\na\n", "main edit")
    _git(repo, "merge", "--no-ff", "-q", "-m", "merge", "-X", "ours", "feature")
    commits, _ = list_commits(repo, limit=10)
    merge = commits[0]
    assert merge.subject == "merge"
    assert set(merge.parents) == {main, feature}
    # ORDER matters to the lane algorithm: the first parent inherits the lane.
    assert merge.parents[0] == main


def test_pathspec_history_rewrites_the_parent_link(tmp_path):
    """THE --parents gate. Spec section 3.2.

    Oldest first: c1 touches the artifact, c2 touches something else, c3 touches
    the artifact. Under a pathspec git returns only c1 and c3, and REWRITES the
    NEWER one's parent to point at the older MATCHING commit.

    Without `--parents` in the argv, git reports c3's parent as c2 -- a commit
    that is not in the returned list, so the graph would draw an edge to a row
    that is not on screen. Delete `--parents` from `list_commits` and this test
    must go red.
    """
    repo = _empty_repo(tmp_path)
    c1 = _commit(repo, "proposal.md", "v1", "c1 artifact")
    _commit(repo, "other.md", "o1", "c2 unrelated")
    _commit(repo, "proposal.md", "v2", "c3 artifact")

    commits, _ = list_commits(repo, limit=10, pathspec="proposal.md")
    assert [c.subject for c in commits] == ["c3 artifact", "c1 artifact"]
    assert commits[0].parents == (c1,), (
        "c3's parent must be rewritten to c1; got a parent that is not in the list"
    )


def test_a_malformed_parent_token_is_dropped_not_half_parsed(tmp_path, caplog):
    """Guards the _MAX_FIELD_BYTES cut (gitio.py:655, 2000 bytes ~ 48 parents).

    A cut lands mid-sha, and a partial sha becomes a lane waiting for a commit
    that will never arrive. Exercised through the parse helper rather than by
    building a 49-parent octopus, which git makes needlessly painful.
    """
    from call_manager.proposal_assist.citations.gitio import _parse_parents

    good = "a" * 40
    assert _parse_parents(f"{good} {'b' * 17}", sha_len=40) == (good,)
    assert _parse_parents("", sha_len=40) == ()
    assert _parse_parents(f"{good} {'Z' * 40}", sha_len=40) == (good,)
```

- [ ] **Step 2: Run them and watch them fail**

```bash
uv run pytest tests/proposal_assist/citations/test_gitio_list_commits.py -x -q
```

Expected: `AttributeError: 'CommitInfo' object has no attribute 'parents'` on the
first three, and `ImportError: cannot import name '_parse_parents'` on the last.

- [ ] **Step 3: Add the field and the parser**

In `gitio.py`, extend the dataclass (it is `@dataclass(frozen=True)` at line 609):

```python
@dataclass(frozen=True)
class CommitInfo:
    """One entry of a workspace repository's history."""

    sha: str
    authored_at: datetime
    subject: str
    #: Parent shas, first-parent first. Empty for a root commit, and for the
    #: OLDEST returned commit when the history was truncated -- in both cases the
    #: graph simply has no edge to draw downward, which is honest.
    parents: tuple[str, ...] = ()
```

Add the parse helper next to the other module-private helpers, above
`list_commits`:

```python
#: A sha is hex and exactly as long as the sha the same record reported for
#: itself -- not a hard-coded 40, so a sha-256 repository needs no code change.
#: Same reasoning as ports/shas.py's 7-64 range.
_HEX_RE = re.compile(r"[0-9a-f]+", re.IGNORECASE)


def _parse_parents(field: str, *, sha_len: int) -> tuple[str, ...]:
    """The `%P` field, with anything that is not a whole sha discarded.

    `_read_capped_fields` keeps at most _MAX_FIELD_BYTES of EVERY field, so a
    parent list longer than about 48 shas is cut, and the cut can land mid-sha.
    A bare `.split()` would hand that fragment on as a real parent, and the
    version graph would open a lane for a commit that does not exist and never
    close it.

    Dropping is the conservative direction: a missing edge draws nothing, an
    invented edge draws a line to nowhere.
    """
    raw = field.split()
    kept = tuple(p for p in raw if len(p) == sha_len and _HEX_RE.fullmatch(p))
    if len(kept) != len(raw):
        logger.warning(
            "gitio.dropped_malformed_parents",
            extra={
                "event": "gitio_dropped_malformed_parents",
                "dropped": len(raw) - len(kept),
            },
        )
    return kept
```

Confirm `import re` and a module `logger` already exist at the top of `gitio.py`;
both are used elsewhere in the file. If `logger` does not exist, add
`logger = logging.getLogger(__name__)` beside the other module constants.

- [ ] **Step 4: Widen the reader from three fields to four**

Four separate places encode "three". Change all four:

```python
    argv = [
        "git",
        "log",
        f"--max-count={limit + 1}",
        # ALWAYS passed, pathspec or not. Under a pathspec git rewrites parent
        # links, and %P only reports the REWRITTEN parents when --parents is
        # given. Without it the graph draws edges to commits that were filtered
        # out. Measured; pinned by
        # test_pathspec_history_rewrites_the_parent_link.
        "--parents",
        "-z",
        "--format=%H%x00%P%x00%aI%x00%s",
    ]
```

```python
    # Four fields per record, and git emits a trailing NUL after the last one.
    max_fields = 4 * (limit + 1)
```

```python
    if len(fields) % 4 != 0:
        raise GitCommitError(
            f"malformed git log output for {repo_dir}: {len(fields)} NUL-separated "
            f"fields is not a multiple of 4"
        )
    records = [tuple(fields[i : i + 4]) for i in range(0, len(fields), 4)]
```

```python
    for sha, parents_field, authored, subject in records[:limit]:
```

and at the end of that loop body:

```python
        commits.append(
            CommitInfo(
                sha=sha,
                authored_at=authored_at,
                subject=subject,
                parents=_parse_parents(parents_field, sha_len=len(sha)),
            )
        )
```

- [ ] **Step 5: Run the new tests, then the two existing callers' suites**

```bash
uv run pytest tests/proposal_assist/citations/test_gitio_list_commits.py -q
uv run pytest tests/test_api_admin_retention_inspect.py tests/proposal_assist/web/test_versions_api.py -q
```

Expected: all pass. `admin_retention` ignores the new field and its output must
not change; if any of its assertions move, stop — the parser is shifted.

- [ ] **Step 6: Prove the `--parents` gate actually bites**

This is a mutation, not a review. Do it by line number, because formatting may
have moved the text:

```bash
grep -n '"--parents",' src/call_manager/proposal_assist/citations/gitio.py
```

Comment that one line out, run
`uv run pytest tests/proposal_assist/citations/test_gitio_list_commits.py::test_pathspec_history_rewrites_the_parent_link -q`,
and confirm it FAILS naming c3's parent. Then restore the line with
`git checkout -- src/call_manager/proposal_assist/citations/gitio.py` **only if
you have already committed** (Step 7) — otherwise you will discard your own work.
Safer: undo the comment by hand.

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/proposal_assist/citations/gitio.py \
        tests/proposal_assist/citations/test_gitio_list_commits.py
git commit -m "feat(gitio): CommitInfo carries parents; --parents always passed"
```

---

## Task 2: `list_commits` can read every branch

**Files:**
- Modify: `src/call_manager/proposal_assist/citations/gitio.py`
- Test: `tests/proposal_assist/citations/test_gitio_list_commits.py`

**Interfaces:**
- Consumes: `CommitInfo.parents` from Task 1.
- Produces: `list_commits(repo_dir, *, limit, pathspec=None, all_branches=False)`.

- [ ] **Step 1: Write the failing tests**

Append to the same test file. Reuse `_repo`, `_run`, `_commit` from Task 1.

```python
def test_all_branches_returns_commits_off_the_checked_out_branch(tmp_path):
    repo = _empty_repo(tmp_path)
    _commit(repo, "proposal.md", "v1", "base")
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "proposal.md", "v2", "only on side")
    _git(repo, "checkout", "-q", "main")

    default, _ = list_commits(repo, limit=10)
    assert "only on side" not in [c.subject for c in default]

    every, _ = list_commits(repo, limit=10, all_branches=True)
    assert "only on side" in [c.subject for c in every]


def test_all_branches_still_includes_a_detached_head(tmp_path):
    """THE `HEAD` gate. Spec section 3.3.

    `git log --branches` lists refs/heads only, so a commit made on a detached
    HEAD belongs to no branch and vanishes -- the pane would show a history
    missing its own tip. `--branches HEAD` is the union. Measured.

    Delete the bare "HEAD" argument from the all_branches argv and this must go
    red.
    """
    repo = _empty_repo(tmp_path)
    _commit(repo, "proposal.md", "v1", "base")
    _git(repo, "checkout", "-q", "--detach", "HEAD")
    _commit(repo, "proposal.md", "v2", "made while detached")

    every, _ = list_commits(repo, limit=10, all_branches=True)
    assert "made while detached" in [c.subject for c in every]


def test_all_branches_orders_children_before_parents(tmp_path):
    repo = _empty_repo(tmp_path)
    root = _commit(repo, "proposal.md", "v1", "root")
    _commit(repo, "proposal.md", "v2", "child")
    every, _ = list_commits(repo, limit=10, all_branches=True)
    assert [c.subject for c in every] == ["child", "root"]
    assert every[0].parents == (root,)
```

- [ ] **Step 2: Run them and watch them fail**

```bash
uv run pytest tests/proposal_assist/citations/test_gitio_list_commits.py -k all_branches -q
```

Expected: `TypeError: list_commits() got an unexpected keyword argument 'all_branches'`.

- [ ] **Step 3: Implement**

Change the signature and the argv in `gitio.py`:

```python
def list_commits(
    repo_dir: Path,
    *,
    limit: int,
    pathspec: str | None = None,
    all_branches: bool = False,
) -> tuple[list[CommitInfo], bool]:
```

Immediately after the `--format=…` entry and **before** the `if pathspec is not
None:` block, insert:

```python
    if all_branches:
        # `--branches` is refs/heads ONLY -- no tags, no remotes, which is what
        # the version pane wants. The bare `HEAD` is NOT redundant: measured,
        # `--branches` alone omits a commit made on a detached HEAD, so the pane
        # would show a history missing its own tip.
        #
        # `--topo-order` guarantees children before parents, which the lane
        # algorithm depends on. Applied only here so the default path stays
        # byte-identical for admin_retention.
        argv += ["--branches", "HEAD", "--topo-order"]
```

Extend the docstring with one paragraph naming `all_branches` and both reasons
above.

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/proposal_assist/citations/test_gitio_list_commits.py -q
uv run pytest tests/test_api_admin_retention_inspect.py -q
```

Expected: all pass, retention unchanged.

- [ ] **Step 5: Prove the `HEAD` gate bites**

Remove just the `"HEAD",` element from the inserted list, run
`uv run pytest tests/proposal_assist/citations/test_gitio_list_commits.py::test_all_branches_still_includes_a_detached_head -q`,
confirm FAIL, then put it back by hand.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/proposal_assist/citations/gitio.py \
        tests/proposal_assist/citations/test_gitio_list_commits.py
git commit -m "feat(gitio): list_commits(all_branches=True) reads every branch and HEAD"
```

---

## Task 3: `branch_tips` and `commit_exists`

**Files:**
- Modify: `src/call_manager/proposal_assist/citations/gitio.py`
- Test: `tests/proposal_assist/test_gitio_branches.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `branch_tips(repo_dir: Path) -> dict[str, str]` (branch name → full sha);
  `commit_exists(repo_dir: Path, sha: str) -> bool`.

**Background.** `list_branches` in the same file documents two traps you must
copy rather than rediscover: `%(refname:short)` is ambiguity-dependent and prints
`heads/feature` when a tag shares the name, and a corrupt ref makes the command
exit **zero** while silently omitting that branch.

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/test_gitio_branches.py`. That file already has
`_repo(tmp_path, *, commit=True)` at line 31, which inits a repo on `main` with
one empty commit — use it, and add two small helpers beside it:

```python
def _git(repo, *argv) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *argv], cwd=repo, check=True
    )


def _head(repo) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_branch_tips_maps_each_branch_to_its_full_sha(tmp_path):
    repo = _repo(tmp_path)
    main = _head(repo)
    _git(repo, "checkout", "-q", "-b", "side")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "on side")
    side = _head(repo)
    _git(repo, "checkout", "-q", "main")

    assert branch_tips(repo) == {"main": main, "side": side}
    assert main != side


def test_branch_tips_is_not_confused_by_a_tag_of_the_same_name(tmp_path):
    """`%(refname:short)` answers `heads/feature` here -- the trap list_branches
    documents at length. `%(refname)` is unambiguous."""
    repo = _repo(tmp_path)
    _git(repo, "branch", "feature")
    _git(repo, "tag", "feature")

    assert set(branch_tips(repo)) == {"main", "feature"}


def test_branch_tips_refuses_a_broken_ref_rather_than_omitting_it(tmp_path):
    """Measured 2026-08-31: `git for-each-ref` behaves exactly like `git branch`
    here -- it warns "ignoring broken ref" on stderr, exits ZERO, and omits the
    branch. A short map that reports success is worse than a failure, because
    the reconciler treats an unseen name as an orphan and deletes its row."""
    repo = _repo(tmp_path)
    _git(repo, "branch", "feature")
    (repo / ".git" / "refs" / "heads" / "feature").write_text("garbage\n")

    with pytest.raises(GitCommitError, match="broken ref"):
        branch_tips(repo)


def test_commit_exists(tmp_path):
    repo = _repo(tmp_path)
    assert commit_exists(repo, _head(repo)) is True
    assert commit_exists(repo, "0" * 40) is False


def test_commit_exists_is_false_for_a_tree_that_is_not_a_commit(tmp_path):
    """`^{commit}` is load-bearing: a bare `rev-parse --verify` accepts a TREE
    or a BLOB sha, and `git branch <name> <tree>` would then fail deep inside
    create_branch with a message no status code maps onto."""
    repo = _repo(tmp_path)
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()

    assert commit_exists(repo, tree) is False
```

`subprocess` and `pytest` are already imported in that file. Add `branch_tips`,
`commit_exists` and `GitCommitError` to its existing
`from call_manager.proposal_assist.citations.gitio import (` block at line 9 —
do not add a second import statement.

- [ ] **Step 2: Run them and watch them fail**

```bash
uv run pytest tests/proposal_assist/test_gitio_branches.py -k "branch_tips or commit_exists" -q
```

Expected: `ImportError: cannot import name 'branch_tips'`.

- [ ] **Step 3: Implement, directly below `list_branches`**

```python
def branch_tips(repo_dir: Path) -> dict[str, str]:
    """Every local branch mapped to the FULL sha its tip points at.

    `%(refname)` and strip, never `%(refname:short)` -- the short form shortens
    to the shortest UNAMBIGUOUS name, so a tag sharing a branch's name makes it
    report `heads/feature`. Same trap `list_branches` documents at length.

    Raises GitCommitError on a broken ref rather than returning a short map: a
    corrupt ref file makes git exit ZERO, warn on stderr, and omit the branch.
    A caller that believed the short map would mark a live branch as missing.
    """
    proc = _git(repo_dir, "for-each-ref", "--format=%(objectname)%00%(refname)", "refs/heads")
    if proc.returncode != 0:
        raise GitCommitError(f"cannot list branch tips in {repo_dir}: {proc.stderr.strip()}")
    if "broken ref" in proc.stderr:
        raise GitCommitError(f"broken ref in {repo_dir}: {proc.stderr.strip()}")
    tips: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        sha, _, refname = line.partition("\0")
        name = refname.strip().removeprefix(_HEADS_PREFIX)
        if name:
            tips[name] = sha.strip()
    return tips


def commit_exists(repo_dir: Path, sha: str) -> bool:
    """True if *sha* resolves to a COMMIT in this repository.

    `^{commit}` is not decoration: a bare `rev-parse --verify` happily accepts a
    tree or a blob sha, and `git branch <name> <tree>` then fails inside
    create_branch with a git message no status code maps onto.

    The caller MUST have validated *sha* with ports.shas.is_sha first. git parses
    `--output=...` as an option before it reaches `--`, so an unvalidated value
    reaching argv is an option-injection vector.
    """
    proc = _git(repo_dir, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    return proc.returncode == 0
```

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/proposal_assist/test_gitio_branches.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/citations/gitio.py \
        tests/proposal_assist/test_gitio_branches.py
git commit -m "feat(gitio): branch_tips and commit_exists"
```

---

## Task 4: Create a branch from a chosen commit

**Files:**
- Modify: `src/call_manager/proposal_assist/citations/gitio.py` (`create_branch`)
- Modify: `src/call_manager/proposal_assist/branches.py`
- Modify: `src/call_manager/proposal_assist/web/branches.py`
- Test: `tests/proposal_assist/test_branch_create_delete.py`, `tests/proposal_assist/test_api_branches.py`

**Interfaces:**
- Consumes: `commit_exists` from Task 3.
- Produces:
  `gitio.create_branch(repo_dir, name, start: str = "HEAD") -> None`;
  `branches.BranchStartUnknown(BranchError)`;
  `create_workspace_branch(session, *, stamp, workspace_id, name, workspaces_dir, from_sha=None)`;
  request body `{"name": str, "from_sha": str | None}`.

**Background — do not reorder anything.** `create_workspace_branch` currently
does, in this order: clear the workspace's root pointer if it names `name`,
`DELETE` any stale `Branch` row for `name`, `flush()`, **then**
`git_create_branch`, **then** `session.add(Branch(...))`. That ordering was
established by an earlier review and is load-bearing; `from_sha` changes what
`git_create_branch` is called with and nothing else.

The function's docstring justifies taking the commit mutex with "HEAD must not
move under it… would silently found the branch on a commit the user did not
choose." With an explicit `from_sha` that particular race is gone. The mutex
still stays — it protects the row work and the `HEAD` default. **Update the
docstring** so it does not leave a claim that is now only half true.

- [ ] **Step 1: Write the failing tests**

In `tests/proposal_assist/test_branch_create_delete.py`. That file's fixtures are
`pg_session_factory` and `tmp_path` (which IS the workspaces dir), its workspace
helper is `_ws(session_factory, tmp_path)` returning `(wid, repo)`, and it stamps
with `stamp_for(s, wid)` from `tests.proposal_assist.conftest`. `_ws` calls
`init_workspace_repo`, which leaves the repo with **one** commit — these cases
need two, so they add the second themselves.

Add one helper beside `_ws`:

```python
def _extra_commit(repo, body: str, message: str) -> str:
    (repo / "proposal.md").write_text(body, encoding="utf-8")
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "proposal.md"],
        cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", message],
        cwd=repo, check=True,
    )
    return current_head(repo)
```

```python
def test_create_from_an_explicit_sha_puts_the_branch_there(pg_session_factory, tmp_path):
    """The whole point of the feature: branch from the version you are looking
    at, not from wherever HEAD happens to be."""
    wid, repo = _ws(pg_session_factory, tmp_path)
    older = current_head(repo)
    newer = _extra_commit(repo, "# Aims\n\nmore\n", "newer")
    assert older != newer

    with pg_session_factory() as s:
        create_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="from-older",
            workspaces_dir=tmp_path,
            from_sha=older,
        )
        s.commit()

    assert branch_tips(repo)["from-older"] == older
    # And HEAD did not move, so this is not just "it branched at HEAD".
    assert branch_tips(repo)["main"] == newer


def test_create_with_no_sha_still_branches_at_head(pg_session_factory, tmp_path):
    """The control. Without it, an implementation that ignored from_sha entirely
    and always used HEAD would still fail only ONE test -- and an implementation
    that always used from_sha and crashed on None would pass every other test in
    this file, because they all pass a sha."""
    wid, repo = _ws(pg_session_factory, tmp_path)
    _extra_commit(repo, "# Aims\n\nmore\n", "newer")
    head = current_head(repo)

    with pg_session_factory() as s:
        create_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="at-head",
            workspaces_dir=tmp_path,
        )
        s.commit()

    assert branch_tips(repo)["at-head"] == head


def test_create_from_a_sha_that_is_not_in_this_repo_is_refused(pg_session_factory, tmp_path):
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        with pytest.raises(BranchStartUnknown):
            create_workspace_branch(
                s,
                stamp=stamp_for(s, wid),
                workspace_id=wid,
                name="ghost",
                workspaces_dir=tmp_path,
                from_sha="0" * 40,
            )
    assert "ghost" not in branch_tips(repo)


def test_create_from_a_non_sha_is_refused_before_git_sees_it(pg_session_factory, tmp_path):
    """`--output=/tmp/pwned` is not a sha. git parses options before it reaches
    any `--` separator, so an unvalidated value in argv is an option-injection
    vector -- the reason ports/shas.py exists."""
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        with pytest.raises(BranchStartUnknown):
            create_workspace_branch(
                s,
                stamp=stamp_for(s, wid),
                workspace_id=wid,
                name="evil",
                workspaces_dir=tmp_path,
                from_sha="--output=/tmp/pwned",
            )
    assert "evil" not in branch_tips(repo)
```

Add `BranchStartUnknown` to the `from call_manager.proposal_assist.branches
import (` block at line 7, and `branch_tips` to the gitio import block at line 17.
`current_head`, `subprocess` and `pytest` are already imported there.
assertion to make an unfamiliar fixture fit.

In `tests/proposal_assist/test_api_branches.py`:

```python
def test_create_from_sha_at_the_api(client_authed_owner, current_user, pg_session_factory, _app):
    from call_manager.proposal_assist.citations.gitio import branch_tips, list_commits
    from call_manager.proposal_assist.paths import workspace_repo_dir

    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    repo = workspace_repo_dir(_app.state.settings.workspaces_dir, wid)
    commits, _ = list_commits(repo, limit=10)
    target = commits[0].sha

    r = client_authed_owner.post(
        f"/api/workspaces/{wid}/branches", json={"name": "v2", "from_sha": target}
    )
    assert r.status_code == 201, r.text
    assert branch_tips(repo)["v2"] == target


def test_create_from_an_unknown_sha_is_404(client_authed_owner, current_user, pg_session_factory, _app):
    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    r = client_authed_owner.post(
        f"/api/workspaces/{wid}/branches", json={"name": "v2", "from_sha": "0" * 40}
    )
    assert r.status_code == 404, r.text
```

- [ ] **Step 2: Run them and watch them fail**

```bash
uv run pytest tests/proposal_assist/test_branch_create_delete.py -k from_ -q
```

Expected: `TypeError: create_workspace_branch() got an unexpected keyword argument 'from_sha'`.

- [ ] **Step 3: `gitio.create_branch` takes a start point**

```python
def create_branch(repo_dir: Path, name: str, start: str = "HEAD") -> None:
    """Create *name* at *start* (default: the current HEAD). Does NOT move HEAD.

    Fails on a repo with no commits -- `git branch <name> HEAD` exits 128 with
    "not a valid object name: 'HEAD'" -- which is the correct refusal for a
    workspace still in setup.

    *start* reaches git as an argument. Callers passing anything but the default
    MUST have validated it with ports.shas.is_sha and confirmed it with
    commit_exists first.
    """
    proc = _git(repo_dir, "branch", name, start)
    if proc.returncode != 0:
        raise GitCommitError(f"cannot create branch {name!r}: {proc.stderr.strip()}")
```

- [ ] **Step 4: The service layer**

In `branches.py`, add beside the other exceptions:

```python
class BranchStartUnknown(BranchError):
    """The requested start commit is not a commit in this repository.

    404, not 422: the request was well-formed, the thing it names does not
    exist -- the same distinction BranchNotFound draws for a branch name.
    """
```

Add the import: `from call_manager.proposal_assist.ports.shas import is_sha`, and
add `commit_exists` to the existing `gitio` import list.

Change the signature to append **one** keyword, leaving every existing one exactly
as it is:

```python
def create_workspace_branch(
    session: Session,
    *,
    stamp: FenceStamp,
    workspace_id: uuid.UUID,
    name: str,
    workspaces_dir: Path | str,
    from_sha: str | None = None,
) -> None:
```

Rewrite the mutex paragraph of the docstring to:

```
    It takes the mutex for two reasons. The row work (clearing a root pointer,
    deleting a stale row) must be serialised against the other branch
    operations. And when `from_sha` is None the start point is HEAD, which must
    not move underneath the read -- a checkpoint or section save committing
    between the read and the `git branch` would silently found the branch on a
    commit the user did not choose. With an explicit `from_sha` that second
    race does not apply: the caller pinned the commit.
```

Then, inside the mutex and **immediately before** the existing
`git_create_branch(repo, name)` call, insert the validation and change the call:

```python
    start = "HEAD"
    if from_sha is not None:
        # is_sha FIRST: git parses `--output=...` as an option before it reaches
        # any `--` separator, so an unvalidated value in argv is an
        # option-injection vector (ports/shas.py). commit_exists SECOND: a
        # well-formed sha for a tree, or for nothing at all, would otherwise
        # fail inside git with a message no status code maps onto.
        if not is_sha(from_sha) or not commit_exists(repo, from_sha):
            raise BranchStartUnknown(f"{from_sha!r} is not a commit in this workspace")
        start = from_sha

    git_create_branch(repo, name, start)
```

**Do not move** the de-orphan / delete / `flush()` block that sits above this.

- [ ] **Step 5: The route**

In `web/branches.py`:

```python
class BranchIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    #: Optional start point. None means HEAD, which is the behaviour every
    #: existing caller relies on.
    from_sha: str | None = None
```

Add `BranchStartUnknown` to the imports from `..branches`, and to `_STATUS`:

```python
    #: The request named a start commit that is not in this repository. 404, not
    #: 422: the body was well-formed.
    BranchStartUnknown: 404,
```

And in `create_branch_route`, add one argument to the existing call:

```python
        create_workspace_branch(
            session,
            stamp=stamp_for_workspace(ws),
            workspace_id=workspace_id,
            name=body.name,
            workspaces_dir=request.app.state.settings.workspaces_dir,
            from_sha=body.from_sha,
        )
```

- [ ] **Step 6: Run the tests**

```bash
uv run pytest tests/proposal_assist/test_branch_create_delete.py tests/proposal_assist/test_api_branches.py -q
```

Expected: PASS.

- [ ] **Step 7: Prove the validation bites**

Replace the whole `if not is_sha(from_sha) or not commit_exists(repo, from_sha):`
condition with `if False:` and run
`uv run pytest tests/proposal_assist/test_branch_create_delete.py -k "not_in_this_repo or non_sha" -q`.
Both must FAIL. Restore by hand.

- [ ] **Step 8: Commit**

```bash
git add src/call_manager/proposal_assist/citations/gitio.py \
        src/call_manager/proposal_assist/branches.py \
        src/call_manager/proposal_assist/web/branches.py \
        tests/proposal_assist/test_branch_create_delete.py \
        tests/proposal_assist/test_api_branches.py
git commit -m "feat(branches): create a branch from a chosen commit"
```

---

## Task 5: Branch tips on the branches API

**Files:**
- Modify: `src/call_manager/proposal_assist/branches.py` (`list_workspace_branches`)
- Modify: `src/call_manager/proposal_assist/web/branches.py`
- Test: `tests/proposal_assist/test_api_branches.py`

**Interfaces:**
- Consumes: `gitio.branch_tips` from Task 3.
- Produces: `list_workspace_branches(...) -> tuple[list[str], str, dict[str, str]]`;
  `BranchListOut(branches, current, tips)` with **no default on `tips`**.

**Background.** Both `GET /branches` and `POST /branches/switch` already build
their response from `list_workspace_branches`, so widening that one function
fixes both. Giving `tips` a `= {}` default would let the switch route return an
empty map that no test notices — that is exactly the gap an outside review found
in the spec's first draft, and it is a settled point.

- [ ] **Step 1: Write the failing tests**

Replace the existing exact-shape assertion in
`tests/proposal_assist/test_api_branches.py` (currently
`assert r.json() == {"branches": ["main"], "current": "main"}`) — **keep it an
exact-equality assertion**, do not loosen it to a subset check:

```python
def test_list_returns_branches_and_current(
    client_authed_owner, current_user, pg_session_factory, _app
):
    from call_manager.proposal_assist.citations.gitio import branch_tips
    from call_manager.proposal_assist.paths import workspace_repo_dir

    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    repo = workspace_repo_dir(_app.state.settings.workspaces_dir, wid)

    r = client_authed_owner.get(f"/api/workspaces/{wid}/branches")
    assert r.status_code == 200
    assert r.json() == {
        "branches": ["main"],
        "current": "main",
        "tips": {"main": branch_tips(repo)["main"]},
    }
```

And add the one that closes the switch-route gap:

```python
def test_the_switch_response_carries_real_tips_not_an_empty_map(
    client_authed_owner, current_user, pg_session_factory, _app
):
    """Without this, the switch route can return `tips: {}` forever and every
    other test still passes -- it only ever reads ["current"]."""
    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    client_authed_owner.post(f"/api/workspaces/{wid}/branches", json={"name": "v2"})

    switched = client_authed_owner.post(
        f"/api/workspaces/{wid}/branches/switch", json={"name": "v2"}
    )
    listed = client_authed_owner.get(f"/api/workspaces/{wid}/branches")

    assert switched.status_code == 200, switched.text
    assert switched.json()["tips"] == listed.json()["tips"]
    assert set(switched.json()["tips"]) == {"main", "v2"}
```

- [ ] **Step 2: Run them and watch them fail**

```bash
uv run pytest tests/proposal_assist/test_api_branches.py -k "returns_branches_and_current or real_tips" -q
```

Expected: both FAIL on a missing `tips` key.

- [ ] **Step 3: Widen the service function**

In `branches.py`, add `branch_tips` to the `gitio` imports, change the return
annotation and the final line:

```python
def list_workspace_branches(
    session: Session, *, workspace_id: uuid.UUID, workspaces_dir: Path | str
) -> tuple[list[str], str, dict[str, str]]:
```

```python
    # One listing operation for BOTH routes. A second, hand-built response body
    # for the switch route is how the two would drift apart.
    return list_branches(repo), row.current_branch, branch_tips(repo)
```

Add a line to the docstring: `Returns (every local branch, the one this
workspace is on, each branch's tip sha).`

- [ ] **Step 4: Widen the model and both call sites**

```python
class BranchListOut(BaseModel):
    branches: list[str]
    current: str
    #: Branch name -> the full sha its tip points at. NO default: with `= {}`
    #: the switch route could return an empty map and no test would see it.
    tips: dict[str, str]
```

In `list_branches_route`:

```python
        names, current, tips = list_workspace_branches(
            session,
            workspace_id=workspace_id,
            workspaces_dir=request.app.state.settings.workspaces_dir,
        )
    except Exception as exc:
        _raise_http(exc)
    return BranchListOut(branches=names, current=current, tips=tips)
```

At the end of `switch_branch_route`:

```python
    names, current, tips = list_workspace_branches(
        session, workspace_id=workspace_id, workspaces_dir=workspaces_dir
    )
    return BranchListOut(branches=names, current=current, tips=tips)
```

- [ ] **Step 5: Find every other caller**

```bash
grep -rn "list_workspace_branches" src/ tests/
```

Fix each unpacking site. A 2-tuple unpack against a 3-tuple raises `ValueError:
too many values to unpack` at runtime, not at import — so a missed one shows up
only when that path runs.

- [ ] **Step 6: Run the tests**

```bash
uv run pytest tests/proposal_assist/test_api_branches.py -q
```

Expected: PASS, including the four `MUTATIONS`-parametrised permission tests.

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/proposal_assist/branches.py \
        src/call_manager/proposal_assist/web/branches.py \
        tests/proposal_assist/test_api_branches.py
git commit -m "feat(branches): both branch responses carry each branch's tip sha"
```

---

## Task 6: The history route serves the whole graph

**Files:**
- Modify: `src/call_manager/proposal_assist/web/versions.py`
- Test: `tests/proposal_assist/web/test_versions_api.py`

**Interfaces:**
- Consumes: `list_commits(..., all_branches=True)` and `CommitInfo.parents`.
- Produces: response rows gain `parents: list[str]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/web/test_versions_api.py`. That file already has
everything needed: `_make_ws(_app, current_user, pg_session_factory, tmp_path)`
returns a workspace id whose repo is EMPTY (`git init`, no commits),
`_commit(repo, name, body, msg)` returns the new sha, and the repo lives at
`tmp_path / ws_id / "repo"`. Add a `_git` helper beside them:

```python
def _git(repo: Path, *argv: str) -> None:
    subprocess.run(["git", *argv], cwd=repo, check=True)


def test_history_rows_carry_parents(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    repo = tmp_path / ws_id / "repo"
    root = _commit(repo, "proposal.md", "# draft 0\n", "draft 0")
    _commit(repo, "proposal.md", "# draft 1\n", "draft 1")

    rows = client_authed_owner.get(
        f"/api/workspaces/{ws_id}/history?artifact=draft"
    ).json()["versions"]

    assert [r["subject"] for r in rows] == ["draft 1", "draft 0"]
    assert rows[0]["parents"] == [root]
    assert rows[1]["parents"] == []


def test_history_includes_commits_on_other_branches(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    """The pane draws lanes, and a lane needs the commits that are ON the other
    branch. Drop `all_branches=True` from the route and this goes red."""
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    repo = tmp_path / ws_id / "repo"
    _commit(repo, "proposal.md", "# draft 0\n", "draft 0")
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "proposal.md", "# on side\n", "only on side")
    _git(repo, "checkout", "-q", "-")

    subjects = [
        r["subject"]
        for r in client_authed_owner.get(
            f"/api/workspaces/{ws_id}/history?artifact=draft"
        ).json()["versions"]
    ]
    assert "only on side" in subjects


def test_history_still_scopes_to_the_artifact_across_branches(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    """`all_branches` widens WHICH BRANCHES, not which files. Without this, a
    change that dropped the pathspec entirely would still pass the test above."""
    ws_id = _make_ws(_app, current_user, pg_session_factory, tmp_path)
    repo = tmp_path / ws_id / "repo"
    _commit(repo, "proposal.md", "# draft 0\n", "draft 0")
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "proposal-spec.md", "# spec\n", "spec only, on side")
    _git(repo, "checkout", "-q", "-")

    subjects = [
        r["subject"]
        for r in client_authed_owner.get(
            f"/api/workspaces/{ws_id}/history?artifact=draft"
        ).json()["versions"]
    ]
    assert "spec only, on side" not in subjects
```

`_make_ws` runs `git init` with no `-b`, so the initial branch name is whatever
git is configured to produce. That is why these check out `-` (the previous
branch) instead of naming `main` — naming it would make the tests depend on the
runner's git config.

- [ ] **Step 2: Run them and watch them fail**

```bash
uv run pytest tests/proposal_assist/web/test_versions_api.py -k "parents or other_branches" -q
```

Expected: `KeyError: 'parents'` and a missing subject.

- [ ] **Step 3: Implement**

In `versions.py`, add to `VersionRow`:

```python
class VersionRow(BaseModel):
    commit_sha: str
    committed_at: str
    subject: str
    #: Parent shas, first-parent first, as returned by THIS query -- under a
    #: pathspec git rewrites them to the nearest matching ancestors, so they are
    #: always shas the client also received (except at a truncated tail, where
    #: the graph correctly runs off the bottom).
    parents: list[str] = []
    created_by: str | None = None
    triggering_turn_id: str | None = None
    turn_prompt: str | None = None
```

Change the `list_commits` call:

```python
        commits, truncated = list_commits(
            repo_dir,
            limit=HISTORY_LIMIT,
            pathspec=filename,
            # The pane draws a graph, so it needs every branch -- not just the
            # one HEAD is on. Which branch you are ON is shown by which tip
            # carries the current-branch pill, not by which commits are present.
            all_branches=True,
        )
```

Wherever the response rows are built, add `"parents": list(c.parents)` to each
row dict. Extend the module docstring's first paragraph to say the list now spans
all branches.

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/proposal_assist/web/test_versions_api.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/web/versions.py \
        tests/proposal_assist/web/test_versions_api.py
git commit -m "feat(versions): history rows carry parents and span every branch"
```

---

## Task 7: Regenerate API types and update the client wrappers

**Files:**
- Regenerate: `frontend/src/api/schema.d.ts` (or whatever `gen:api` writes)
- Modify: `frontend/src/api/branches.ts`, `frontend/src/api/versions.ts`

**Interfaces:**
- Consumes: the backend shapes from Tasks 4–6.
- Produces: `BranchList = { branches: string[]; current: string; tips: Record<string, string> }`;
  `VersionRow` gains `parents: string[]`;
  `useCreateBranch(id).mutate({ name, fromSha })`.

**Background.** The generator boots the FastAPI app to read its OpenAPI schema.
It fails to construct without a valid embedding model name, and because the script
captures stdout, the failure can write a truncated file rather than erroring
loudly. Always pass the env var.

- [ ] **Step 1: Regenerate**

```bash
cd /home/parlar_ai/dev/call-monitor/frontend
EMBEDDING_MODEL=bge-m3 pnpm gen:api
cd .. && git diff --stat -- frontend/src/api/
```

Expected: the generated file changes, and `tips`, `from_sha` and `parents` appear
in it:

```bash
grep -c "from_sha\|tips\|parents" frontend/src/api/*.d.ts
```

If the generated file got *shorter*, the app failed to construct — do not
continue, fix the environment first.

- [ ] **Step 2: Update `frontend/src/api/branches.ts`**

```ts
export type BranchList = {
  branches: string[];
  current: string;
  /** Branch name -> the full sha its tip points at. */
  tips: Record<string, string>;
};
```

`useCreateBranch` takes an object rather than a bare string, because it now
carries two things:

```ts
export type CreateBranchArgs = { name: string; fromSha?: string };

export function useCreateBranch(id: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ name, fromSha }: CreateBranchArgs) => {
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/branches", {
          params: { path: { workspace_id: id! } },
          body: { name, from_sha: fromSha ?? null },
        }),
      );
    },
    onSuccess: async () => {
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["branches", id] }),
        qc.invalidateQueries({ queryKey: ["draft", id] }),
        qc.invalidateQueries({ queryKey: ["spec", id] }),
        qc.invalidateQueries({ queryKey: ["versions", id] }),
      ]);
    },
  });
}
```

`useBranchMutation` stays exactly as it is for switch and delete, which still take
a bare name. Do not widen it to cover create — the four invalidations are the
thing that must not be forgotten, and they are now written twice; that is
deliberate duplication of a four-line list, not a missing abstraction.

- [ ] **Step 3: Update `frontend/src/api/versions.ts`**

```ts
export type VersionRow = {
  commit_sha: string;
  committed_at: string;
  subject: string;
  /** Parent shas, first-parent first. Empty for a root commit and for the
   *  oldest row of a truncated history. Feeds versionLanes. */
  parents: string[];
  created_by: string | null;
  triggering_turn_id: string | null;
  turn_prompt: string | null;
};
```

- [ ] **Step 4: Typecheck**

```bash
cd frontend && pnpm exec tsc --noEmit
```

Expected: errors ONLY at `BranchPicker.tsx` (it calls `createBranch.mutate(name)`
with a string) and in test files that build `VersionRow` literals. Leave
`BranchPicker` broken — Task 10 deletes it. Fix the test-file literals now by
adding `parents: []` to their row factories, e.g. the `ROW` helper at
`frontend/src/cockpit/VersionGraph.test.tsx:9`.

- [ ] **Step 5: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add frontend/src/api/ frontend/src/cockpit/VersionGraph.test.tsx
git commit -m "chore(api): regenerate types for tips, from_sha and parents"
```

---

## Task 8: `versionLanes.ts` — the pure column assignment

**Files:**
- Create: `frontend/src/cockpit/versionLanes.ts`
- Create: `frontend/src/cockpit/versionLanes.test.ts`

**Interfaces:**
- Consumes: nothing — this module imports nothing.
- Produces:
  ```ts
  export type GraphCommit = { sha: string; parents: string[] };
  export type LaneLink = { top: number | null; bottom: number | null; color: number };
  export type LaneRow = { sha: string; lane: number; links: LaneLink[]; columns: number };
  export const LANE_COLORS: number;
  export function assignLanes(commits: GraphCommit[]): LaneRow[];
  ```

**Background — read this before writing a line.** The obvious optimisation in
step 3 is wrong, and it is wrong in a way that still produces a picture where all
the lines join up. For `A→C`, `B→C`, `C→∅`:

```
  reusing an existing lane        allocating a fresh lane (correct)
    *   A                           *      A
    *   B                           |  *   B
    *   C                           *  /   C
```

With the reuse, `B`'s edge is absorbed into `A`'s lane inside **B's own row**, so
the graph claims `B` merged into `A`. It did not. This is a settled point.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/cockpit/versionLanes.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { assignLanes, type GraphCommit, type LaneRow } from "./versionLanes";

const C = (sha: string, ...parents: string[]): GraphCommit => ({ sha, parents });

/** Columns a row's lines occupy at its BOTTOM edge — the state handed to the
 *  next row. Derived from the public output, so the checkers below test the
 *  contract and not the implementation's internals. */
function bottoms(r: LaneRow): number[] {
  return r.links.filter((l) => l.bottom != null).map((l) => l.bottom as number);
}
function tops(r: LaneRow): number[] {
  return r.links.filter((l) => l.top != null).map((l) => l.top as number);
}

/** CONTINUITY: consecutive SVG strips must join into unbroken lines. */
function continuityProblems(rows: LaneRow[]): string[] {
  const out: string[] = [];
  for (let i = 1; i < rows.length; i++) {
    const above = bottoms(rows[i - 1]);
    const here = tops(rows[i]);
    for (const t of here) if (!above.includes(t)) out.push(`row ${i} enters col ${t}, nothing left there`);
    for (const b of above) if (!here.includes(b)) out.push(`row ${i - 1} left col ${b}, row ${i} receives nothing`);
  }
  return out;
}

/** FIDELITY: every real parent edge is carried by one lane all the way to the
 *  parent's dot, and every dot consumes exactly one lane per child.
 *
 *  This is the gate. Continuity alone PASSES the buggy version that reuses an
 *  existing lane for a parent — the lines join up, one row too early. */
function fidelityProblems(commits: GraphCommit[], rows: LaneRow[]): string[] {
  const out: string[] = [];
  const rowOf = new Map(commits.map((c, i) => [c.sha, i]));

  const childEdges = new Map<string, number>();
  for (const c of commits) for (const p of c.parents) childEdges.set(p, (childEdges.get(p) ?? 0) + 1);

  for (let m = 0; m < rows.length; m++) {
    const want = childEdges.get(rows[m].sha) ?? 0;
    const got = tops(rows[m]).filter((t) => !bottoms(rows[m]).includes(t)).length;
    if (want !== got) out.push(`${rows[m].sha}: ${want} child edge(s) but ${got} lane(s) end at its dot`);
  }

  for (let i = 0; i < commits.length; i++) {
    for (const p of commits[i].parents) {
      const m = rowOf.get(p);
      if (m === undefined) continue; // truncated tail: runs off the bottom, correct
      // some column must carry p from row i's bottom through to row m's top
      const carried = bottoms(rows[i]).some((j) => {
        for (let k = i + 1; k < m; k++) {
          if (!tops(rows[k]).includes(j) || !bottoms(rows[k]).includes(j)) return false;
        }
        return tops(rows[m]).includes(j);
      });
      if (!carried) out.push(`edge ${commits[i].sha}->${p}: absorbed before reaching its parent`);
    }
  }
  return out;
}

describe("assignLanes — worked cases", () => {
  it("an empty history yields no rows", () => {
    expect(assignLanes([])).toEqual([]);
  });

  it("a straight line uses one column", () => {
    const g = [C("A", "B"), C("B", "C"), C("C")];
    const rows = assignLanes(g);
    expect(rows.map((r) => r.lane)).toEqual([0, 0, 0]);
    expect(Math.max(...rows.map((r) => r.columns))).toBe(1);
    expect(continuityProblems(rows)).toEqual([]);
    expect(fidelityProblems(g, rows)).toEqual([]);
  });

  it("a fork keeps the second branch in its own column until the parent", () => {
    const g = [C("A", "C"), C("B", "C"), C("C")];
    const rows = assignLanes(g);
    expect(rows.map((r) => r.lane)).toEqual([0, 1, 0]);
    // B's line must still be alive at C's row -- this is the assertion that
    // fails if step 3 reuses A's lane for C.
    expect(tops(rows[2]).sort()).toEqual([0, 1]);
    expect(fidelityProblems(g, rows)).toEqual([]);
  });

  it("a two-parent merge opens a second column", () => {
    const g = [C("M", "X", "Y"), C("X", "Z"), C("Y", "Z"), C("Z")];
    const rows = assignLanes(g);
    expect(rows[0].lane).toBe(0);
    expect(bottoms(rows[0]).sort()).toEqual([0, 1]);
    expect(tops(rows[3]).sort()).toEqual([0, 1]);
    expect(continuityProblems(rows)).toEqual([]);
    expect(fidelityProblems(g, rows)).toEqual([]);
  });

  it("three concurrent branches use three columns", () => {
    const g = [C("A", "R"), C("B", "R"), C("C", "R"), C("R")];
    const rows = assignLanes(g);
    expect(Math.max(...rows.map((r) => r.columns))).toBe(3);
    expect(tops(rows[3]).sort()).toEqual([0, 1, 2]);
    expect(fidelityProblems(g, rows)).toEqual([]);
  });

  it("handles a merge whose second parent is an ancestor of its first", () => {
    const g = [C("M", "A", "B"), C("A", "B"), C("B")];
    const rows = assignLanes(g);
    expect(continuityProblems(rows)).toEqual([]);
    expect(fidelityProblems(g, rows)).toEqual([]);
  });

  it("handles an octopus merge", () => {
    const g = [C("M", "A", "B", "C"), C("A", "R"), C("B", "R"), C("C", "R"), C("R")];
    const rows = assignLanes(g);
    expect(bottoms(rows[0]).sort()).toEqual([0, 1, 2]);
    expect(fidelityProblems(g, rows)).toEqual([]);
  });

  it("a truncated tail leaves its lane open", () => {
    const g = [C("A", "B")]; // B is not in the set
    const rows = assignLanes(g);
    expect(rows).toHaveLength(1);
    expect(bottoms(rows[0])).toEqual([0]);
    expect(fidelityProblems(g, rows)).toEqual([]);
  });
});

describe("assignLanes — fidelity over generated graphs", () => {
  // Seeded, NOT Math.random: a failure has to be reproducible from its seed.
  function randomDag(n: number, seed: number): GraphCommit[] {
    let s = seed;
    const rnd = () => (s = (s * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
    const shas = Array.from({ length: n }, (_, i) => `c${i}`);
    return shas.map((sha, i) => {
      const available = n - i - 1;
      if (available === 0) return { sha, parents: [] };
      const want = Math.min(available, rnd() < 0.2 ? 2 : 1);
      const parents = new Set<string>();
      while (parents.size < want) parents.add(shas[i + 1 + Math.floor(rnd() * available)]);
      return { sha, parents: [...parents] };
    });
  }

  it("is continuous and faithful over 2000 generated DAGs", () => {
    const failures: string[] = [];
    for (let seed = 1; seed <= 2000; seed++) {
      const g = randomDag(14, seed);
      const rows = assignLanes(g);
      const problems = [...continuityProblems(rows), ...fidelityProblems(g, rows)];
      if (problems.length) failures.push(`seed ${seed}: ${problems[0]}`);
    }
    expect(failures).toEqual([]);
  });
});
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd frontend && pnpm vitest run src/cockpit/versionLanes.test.ts
```

Expected: `Failed to resolve import "./versionLanes"`.

- [ ] **Step 3: Implement**

Create `frontend/src/cockpit/versionLanes.ts`:

```ts
/** Column layout for the version graph. Pure: no React, no I/O, no imports.
 *
 *  Kept separate from the component on purpose. The rendering is easy and the
 *  layout is not; a pure function can be checked over thousands of generated
 *  graphs, which is the only way this logic gets real coverage — no repository
 *  in production has a fork yet.
 */

export type GraphCommit = { sha: string; parents: string[] };

/** One line crossing a row. `top`/`bottom` are column indices at the row's top
 *  and bottom edge; `null` means the line terminates at this row's dot. */
export type LaneLink = { top: number | null; bottom: number | null; color: number };

export type LaneRow = {
  sha: string;
  /** The column this commit's dot sits in. */
  lane: number;
  links: LaneLink[];
  /** Columns occupied at this row. The CALLER takes the maximum across all rows
   *  and uses that one width for every strip — sizing each row by its own value
   *  would slide the pills and subject sideways as the graph narrows. */
  columns: number;
};

/** How many distinct lane colours the theme provides (--app-lane-1..6). */
export const LANE_COLORS = 6;

function firstFree(lanes: (string | null)[]): number {
  const i = lanes.indexOf(null);
  return i === -1 ? lanes.length : i;
}

/** Assign a column to every commit and describe the lines crossing each row.
 *
 *  `commits` must be topologically ordered, children before parents — which is
 *  what `git log --topo-order` guarantees and what the history route now asks
 *  for.
 */
export function assignLanes(commits: GraphCommit[]): LaneRow[] {
  // lanes[i] = the sha column i is currently waiting for, or null if free.
  const lanes: (string | null)[] = [];
  const rows: LaneRow[] = [];

  for (const c of commits) {
    const above = lanes.slice();

    // 1. Which columns were waiting for me? Leftmost wins, so a long-lived
    //    branch keeps its column instead of drifting rightwards.
    const claimed: number[] = [];
    for (let j = 0; j < lanes.length; j++) if (lanes[j] === c.sha) claimed.push(j);
    const own = claimed.length ? claimed[0] : firstFree(lanes);

    // 2. Every claiming column's line ends at this dot.
    for (const j of claimed) lanes[j] = null;
    while (lanes.length <= own) lanes.push(null);

    // 3. Parents take columns. The first parent inherits this commit's own
    //    column so history reads as one continuous line.
    //
    //    DELIBERATELY no "reuse the column already waiting for this parent".
    //    Several columns may wait for the same sha; that is how two branches
    //    stay apart until they actually meet. Deduplicating here makes the
    //    second child's edge fold into the first child's column inside the
    //    SECOND CHILD'S OWN ROW, drawing a merge that never happened.
    const outs: number[] = [];
    c.parents.forEach((p, k) => {
      const lane = k === 0 ? own : firstFree(lanes);
      while (lanes.length <= lane) lanes.push(null);
      lanes[lane] = p;
      outs.push(lane);
    });

    // 4. Let the graph narrow again once branches close.
    while (lanes.length && lanes[lanes.length - 1] === null) lanes.pop();
    const below = lanes.slice();

    // 5. Describe the lines.
    const links: LaneLink[] = [];
    for (let j = 0; j < above.length; j++) {
      // Passing straight through: occupied above by somebody other than me.
      if (above[j] != null && above[j] !== c.sha) links.push({ top: j, bottom: j, color: j % LANE_COLORS });
    }
    for (const j of claimed) links.push({ top: j, bottom: null, color: j % LANE_COLORS });
    for (const lane of outs) links.push({ top: null, bottom: lane, color: lane % LANE_COLORS });

    rows.push({
      sha: c.sha,
      lane: own,
      links,
      columns: Math.max(above.length, below.length, own + 1),
    });
  }

  return rows;
}
```

- [ ] **Step 4: Run the tests**

```bash
cd frontend && pnpm vitest run src/cockpit/versionLanes.test.ts
```

Expected: PASS, 10 tests.

- [ ] **Step 5: Prove the fidelity check is not decorative**

Introduce the bug the comment forbids — inside `c.parents.forEach`, before
choosing a lane, add:

```ts
      const existing = lanes.indexOf(p);
      if (existing !== -1) { outs.push(existing); return; }
```

Run `pnpm vitest run src/cockpit/versionLanes.test.ts`. Expected: the **fork**
case and the **generated DAGs** case FAIL on fidelity, while
`continuityProblems` stays empty — that contrast is the whole point. Remove the
three added lines by hand.

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add frontend/src/cockpit/versionLanes.ts frontend/src/cockpit/versionLanes.test.ts
git commit -m "feat(cockpit): pure lane assignment for the version graph"
```

---

## Task 9: `VersionLane.tsx` — draw one row's strip

**Files:**
- Create: `frontend/src/cockpit/VersionLane.tsx`
- Create: `frontend/src/cockpit/VersionLane.test.tsx`
- Modify: `frontend/src/theme/theme.css`

**Interfaces:**
- Consumes: `LaneRow`, `LaneLink`, `LANE_COLORS` from Task 8.
- Produces:
  ```tsx
  export const LANE_W = 14;
  export const ROW_H = 22;
  export function VersionLane(props: {
    row: LaneRow; columns: number; role: string | null; selected: boolean;
  }): JSX.Element;
  ```

- [ ] **Step 1: Add the lane palette**

In `frontend/src/theme/theme.css`, beside the other `--app-*` tokens:

```css
  /* Version-graph lanes. Six, cycled by column index, the way VS Code's source
     control graph does. Chosen to sit on --app-editor and --app-sidebar. */
  --app-lane-0: #4c8dff;
  --app-lane-1: #3fb950;
  --app-lane-2: #d29922;
  --app-lane-3: #bc8cff;
  --app-lane-4: #4ec9b0;
  --app-lane-5: #f78166;
```

- [ ] **Step 2: Write the failing test**

Create `frontend/src/cockpit/VersionLane.test.tsx`:

```tsx
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LANE_W, ROW_H, VersionLane } from "./VersionLane";
import { assignLanes } from "./versionLanes";

describe("VersionLane", () => {
  const rows = assignLanes([
    { sha: "A", parents: ["C"] },
    { sha: "B", parents: ["C"] },
    { sha: "C", parents: [] },
  ]);

  it("sizes every strip to the LIST-WIDE column count, not its own", () => {
    // Row 0 occupies one column, row 1 occupies two. If each strip used its own
    // `columns`, the subject text beside them would slide sideways row to row.
    const { container: first } = render(<VersionLane row={rows[0]} columns={2} role={null} selected={false} />);
    const { container: second } = render(<VersionLane row={rows[1]} columns={2} role={null} selected={false} />);
    expect(first.querySelector("svg")?.getAttribute("width")).toBe(String(2 * LANE_W));
    expect(second.querySelector("svg")?.getAttribute("width")).toBe(String(2 * LANE_W));
  });

  it("is hidden from assistive technology", () => {
    const { container } = render(<VersionLane row={rows[0]} columns={2} role={null} selected={false} />);
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });

  it("draws one path per link and exactly one dot", () => {
    const { container } = render(<VersionLane row={rows[2]} columns={2} role="user" selected={false} />);
    expect(container.querySelectorAll("path")).toHaveLength(rows[2].links.length);
    expect(container.querySelectorAll("circle")).toHaveLength(1);
  });

  it("is exactly ROW_H tall so consecutive strips join up", () => {
    const { container } = render(<VersionLane row={rows[0]} columns={2} role={null} selected={false} />);
    expect(container.querySelector("svg")?.getAttribute("height")).toBe(String(ROW_H));
  });
});
```

- [ ] **Step 3: Run it and watch it fail**

```bash
cd frontend && pnpm vitest run src/cockpit/VersionLane.test.tsx
```

Expected: `Failed to resolve import "./VersionLane"`.

- [ ] **Step 4: Implement**

Create `frontend/src/cockpit/VersionLane.tsx`:

```tsx
import { LANE_COLORS, type LaneLink, type LaneRow } from "./versionLanes";

/** Column pitch, and the row height every strip must match. Fixed, not derived
 *  from content: strips only join into continuous lines when every row is the
 *  same height. The subject text ellipsises rather than wrapping for the same
 *  reason. */
export const LANE_W = 14;
export const ROW_H = 22;

const DOT_R = 4;

function x(col: number): number {
  return col * LANE_W + LANE_W / 2;
}

/** The path for one line crossing this row.
 *
 *  A link with both ends set runs edge to edge. A link with one end null starts
 *  or finishes at the dot, so it is drawn to the row's middle — which is what
 *  makes a branch appear to leave from, or arrive at, the commit itself.
 *  Non-vertical segments get a quadratic through the mid-height, the same curve
 *  VS Code's source-control graph uses. */
function pathFor(link: LaneLink, lane: number): string {
  const mid = ROW_H / 2;
  const topX = link.top != null ? x(link.top) : x(lane);
  const botX = link.bottom != null ? x(link.bottom) : x(lane);
  const startY = link.top != null ? 0 : mid;
  const endY = link.bottom != null ? ROW_H : mid;

  if (topX === botX) return `M ${topX} ${startY} L ${botX} ${endY}`;
  return `M ${topX} ${startY} L ${topX} ${mid * 0.4} Q ${topX} ${mid} ${x((link.top ?? lane) + (botX > topX ? 0.5 : -0.5))} ${mid} L ${botX} ${mid * 1.6} L ${botX} ${endY}`;
}

/** One row of the commit graph. Purely decorative: the row's meaning is carried
 *  by the sha, subject and branch pills beside it, so this is aria-hidden. */
export function VersionLane({
  row,
  columns,
  role,
  selected,
}: {
  row: LaneRow;
  /** The LIST-WIDE maximum, passed in by VersionGraph. Never row.columns. */
  columns: number;
  /** Provenance of the commit: "agent" | "user" | "reconciled" | null. */
  role: string | null;
  selected: boolean;
}) {
  const width = Math.max(columns, 1) * LANE_W;
  return (
    <svg width={width} height={ROW_H} aria-hidden="true" style={{ flex: `0 0 ${width}px`, display: "block" }}>
      {row.links.map((link, i) => (
        <path
          key={i}
          d={pathFor(link, row.lane)}
          fill="none"
          strokeWidth={2}
          stroke={`var(--app-lane-${link.color % LANE_COLORS})`}
        />
      ))}
      <circle
        cx={x(row.lane)}
        cy={ROW_H / 2}
        r={DOT_R}
        data-role={role ?? "none"}
        fill={
          role === "agent" ? "var(--app-lane-0)"
          : role === "user" ? "var(--app-lane-1)"
          : role === "reconciled" ? "var(--app-lane-2)"
          : "var(--app-muted)"
        }
        stroke={selected ? "var(--app-accent-bright)" : "var(--app-editor)"}
        strokeWidth={2}
      />
    </svg>
  );
}
```

- [ ] **Step 5: Run the tests**

```bash
cd frontend && pnpm vitest run src/cockpit/VersionLane.test.tsx
```

Expected: PASS, 4 tests.

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add frontend/src/cockpit/VersionLane.tsx frontend/src/cockpit/VersionLane.test.tsx \
        frontend/src/theme/theme.css
git commit -m "feat(cockpit): SVG lane strip for the version graph"
```

---

## Task 10: `BranchBar.tsx`, and delete `BranchPicker`

**Files:**
- Create: `frontend/src/cockpit/BranchBar.tsx`
- Create: `frontend/src/cockpit/BranchBar.test.tsx`
- Delete: `frontend/src/cockpit/panes/BranchPicker.tsx`, `frontend/src/cockpit/panes/BranchPicker.test.tsx`
- Modify: `frontend/src/cockpit/CockpitSidebar.tsx`

**Interfaces:**
- Consumes: `useBranches`, `useCreateBranch({name, fromSha})`, `useSwitchBranch`,
  `useDeleteBranch` from Task 7.
- Produces:
  ```tsx
  export function BranchBar(props: {
    id: string;
    /** Set by VersionGraph when a row's "Branch from here…" was chosen. */
    branchFrom: { sha: string; subject: string } | null;
    onBranchFromHandled: () => void;
  }): JSX.Element;
  ```

**Background.** `BranchPicker` listed every branch **twice** in one flat menu —
once to switch to it, once to delete it — with no visual separation. The fix is
labelled groups, not new machinery. It also rendered an always-visible `<ul>` of
branch names whose only purpose was to satisfy
`BranchPicker.test.tsx:28`'s `getByText("v2")`. **That assertion is deleted, not
ported.** The behaviour tests (confirm before switching, confirm before deleting,
create posts the trimmed name) move across.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/cockpit/BranchBar.test.tsx`:

```tsx
import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import * as branchesMod from "../api/branches";
import { BranchBar } from "./BranchBar";

function mockBranches(current: string, branches: string[]) {
  vi.spyOn(branchesMod, "useBranches").mockReturnValue({
    data: { current, branches, tips: Object.fromEntries(branches.map((b, i) => [b, `${i}`.repeat(40)])) },
    isError: false,
  } as never);
}

function mockMutations() {
  const create = vi.fn();
  const switchTo = vi.fn();
  const del = vi.fn();
  vi.spyOn(branchesMod, "useCreateBranch").mockReturnValue({ mutate: create, isPending: false } as never);
  vi.spyOn(branchesMod, "useSwitchBranch").mockReturnValue({ mutate: switchTo, isPending: false } as never);
  vi.spyOn(branchesMod, "useDeleteBranch").mockReturnValue({ mutate: del, isPending: false } as never);
  return { create, switchTo, del };
}

function setup(node: React.ReactNode) {
  return render(<MantineProvider>{node}</MantineProvider>);
}

describe("BranchBar", () => {
  it("shows the current branch", () => {
    mockBranches("test", ["main", "test"]);
    mockMutations();
    setup(<BranchBar id="w1" branchFrom={null} onBranchFromHandled={() => {}} />);
    expect(screen.getByRole("button", { name: /test/ })).toBeInTheDocument();
  });

  it("puts switching and deleting in separately labelled groups", async () => {
    mockBranches("test", ["main", "test"]);
    mockMutations();
    setup(<BranchBar id="w1" branchFrom={null} onBranchFromHandled={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: /test/ }));

    expect(screen.getByText("Switch to")).toBeInTheDocument();
    expect(screen.getByText("Delete")).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "main" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Delete main" })).toBeInTheDocument();
  });

  it("never offers to switch to, or delete, the branch you are on", async () => {
    mockBranches("test", ["main", "test"]);
    mockMutations();
    setup(<BranchBar id="w1" branchFrom={null} onBranchFromHandled={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: /test/ }));

    expect(screen.queryByRole("menuitem", { name: "test" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "Delete test" })).toBeNull();
  });

  it("confirms before switching, and only then mutates", async () => {
    mockBranches("test", ["main", "test"]);
    const { switchTo } = mockMutations();
    setup(<BranchBar id="w1" branchFrom={null} onBranchFromHandled={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: /test/ }));
    await userEvent.click(screen.getByRole("menuitem", { name: "main" }));

    expect(switchTo).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Switch branch" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Switch branch" }));
    expect(switchTo).toHaveBeenCalledWith("main");
  });

  it("confirms before deleting, and only then mutates", async () => {
    mockBranches("test", ["main", "test"]);
    const { del } = mockMutations();
    setup(<BranchBar id="w1" branchFrom={null} onBranchFromHandled={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: /test/ }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Delete main" }));

    expect(del).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Delete branch" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Delete branch" }));
    expect(del).toHaveBeenCalledWith("main");
  });

  it("creates from HEAD with the trimmed name when opened from the bar", async () => {
    mockBranches("test", ["main", "test"]);
    const { create } = mockMutations();
    setup(<BranchBar id="w1" branchFrom={null} onBranchFromHandled={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: /test/ }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Create new branch…" }));
    await userEvent.type(screen.getByLabelText("Branch name"), "  spike  ");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(create).toHaveBeenCalledWith({ name: "spike", fromSha: undefined }, expect.anything());
  });

  it("creates from the given sha when the pane asked to branch from a row", async () => {
    mockBranches("test", ["main", "test"]);
    const { create } = mockMutations();
    const sha = "a".repeat(40);
    setup(
      <BranchBar id="w1" branchFrom={{ sha, subject: "seed proposal" }} onBranchFromHandled={() => {}} />,
    );

    // The dialog opens by itself; the row already made the choice.
    expect(screen.getByRole("dialog", { name: "New branch" })).toBeInTheDocument();
    expect(screen.getByText(/seed proposal/)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Branch name"), "from-row");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(create).toHaveBeenCalledWith({ name: "from-row", fromSha: sha }, expect.anything());
  });

  it("stays visible and inert when the branch list cannot be read", () => {
    vi.spyOn(branchesMod, "useBranches").mockReturnValue({ data: undefined, isError: true } as never);
    mockMutations();
    setup(<BranchBar id="w1" branchFrom={null} onBranchFromHandled={() => {}} />);
    // A workspace still in setup answers 409. That is ordinary, not an error to
    // shout about -- but the bar must not vanish and leave a headless pane.
    expect(screen.getByRole("button", { name: /branch/i })).toBeDisabled();
  });
});
```

- [ ] **Step 2: Run and watch it fail**

```bash
cd frontend && pnpm vitest run src/cockpit/BranchBar.test.tsx
```

Expected: `Failed to resolve import "./BranchBar"`.

- [ ] **Step 3: Implement**

Create `frontend/src/cockpit/BranchBar.tsx`:

```tsx
import { Button, Group, Menu, Modal, Text, TextInput } from "@mantine/core";
import { useEffect, useState } from "react";

import { useBranches, useCreateBranch, useDeleteBranch, useSwitchBranch } from "../api/branches";

type Confirm = { kind: "switch" | "delete"; name: string } | null;

/** The Versions pane's branch line: which branch you are on, and everything you
 *  can do to branches.
 *
 *  Replaces panes/BranchPicker, which listed every branch TWICE in one flat menu
 *  -- once to switch to it, once to delete it, with nothing to tell them apart.
 *  The two lists are now separately labelled, and the destructive one is last
 *  and coloured.
 *
 *  The confirm dialogs are load-bearing, not politeness: a switch disconnects
 *  every open editor, and a delete drops commits. Neither may fire on a stray
 *  click.
 */
export function BranchBar({
  id,
  branchFrom,
  onBranchFromHandled,
}: {
  id: string;
  /** Set by VersionGraph when a row's "Branch from here…" was chosen. */
  branchFrom: { sha: string; subject: string } | null;
  onBranchFromHandled: () => void;
}) {
  const { data, isError } = useBranches(id);
  const createBranch = useCreateBranch(id);
  const switchBranch = useSwitchBranch(id);
  const deleteBranch = useDeleteBranch(id);

  const [confirm, setConfirm] = useState<Confirm>(null);
  const [newOpen, setNewOpen] = useState(false);
  const [newName, setNewName] = useState("");

  // A row asking to branch from a commit opens the same dialog the menu opens.
  useEffect(() => {
    if (branchFrom) setNewOpen(true);
  }, [branchFrom]);

  const current = data?.current ?? "…";
  const others = (data?.branches ?? []).filter((b) => b !== current);

  function closeNew() {
    setNewOpen(false);
    setNewName("");
    onBranchFromHandled();
  }

  function handleCreate() {
    const name = newName.trim();
    if (!name) return;
    createBranch.mutate({ name, fromSha: branchFrom?.sha }, { onSuccess: closeNew });
  }

  return (
    <>
      <Group px="sm" pb={6} gap={6} wrap="nowrap">
        <Menu position="bottom-start" width={240} withinPortal>
          <Menu.Target>
            <Button
              variant="subtle"
              size="compact-xs"
              disabled={isError}
              aria-label={`Branch: ${current}`}
              styles={{ label: { fontWeight: 500 } }}
            >
              ⑂ {current} ▾
            </Button>
          </Menu.Target>
          <Menu.Dropdown>
            {others.length > 0 && <Menu.Label>Switch to</Menu.Label>}
            {others.map((name) => (
              <Menu.Item key={name} aria-label={name} onClick={() => setConfirm({ kind: "switch", name })}>
                ⑂ {name}
              </Menu.Item>
            ))}
            <Menu.Divider />
            <Menu.Item onClick={() => setNewOpen(true)}>Create new branch…</Menu.Item>
            {others.length > 0 && (
              <>
                <Menu.Divider />
                <Menu.Label c="var(--app-bad)">Delete</Menu.Label>
                {others.map((name) => (
                  <Menu.Item
                    key={`del-${name}`}
                    color="red"
                    aria-label={`Delete ${name}`}
                    onClick={() => setConfirm({ kind: "delete", name })}
                  >
                    Delete {name}
                  </Menu.Item>
                ))}
              </>
            )}
          </Menu.Dropdown>
        </Menu>
      </Group>

      <Modal opened={confirm?.kind === "switch"} onClose={() => setConfirm(null)} title="Switch branch">
        <Text size="sm">
          Switching to <strong>{confirm?.name}</strong> will disconnect every open editor. Unsaved
          keystrokes will be lost.
        </Text>
        <Group mt="md" justify="flex-end">
          <Button variant="default" onClick={() => setConfirm(null)}>
            Cancel
          </Button>
          <Button
            disabled={switchBranch.isPending}
            onClick={() => {
              if (confirm) switchBranch.mutate(confirm.name);
              setConfirm(null);
            }}
          >
            Switch branch
          </Button>
        </Group>
      </Modal>

      <Modal opened={confirm?.kind === "delete"} onClose={() => setConfirm(null)} title="Delete branch">
        <Text size="sm">
          Delete <strong>{confirm?.name}</strong>? This cannot be undone — the commits on this branch
          will be permanently lost.
        </Text>
        <Group mt="md" justify="flex-end">
          <Button variant="default" onClick={() => setConfirm(null)}>
            Cancel
          </Button>
          <Button
            color="red"
            disabled={deleteBranch.isPending}
            onClick={() => {
              if (confirm) deleteBranch.mutate(confirm.name);
              setConfirm(null);
            }}
          >
            Delete branch
          </Button>
        </Group>
      </Modal>

      <Modal opened={newOpen} onClose={closeNew} title="New branch">
        {branchFrom && (
          <Text size="xs" c="dimmed" mb="xs">
            Branching from {branchFrom.sha.slice(0, 7)} · {branchFrom.subject}
          </Text>
        )}
        <TextInput
          label="Branch name"
          placeholder="my-branch"
          value={newName}
          onChange={(e) => setNewName(e.currentTarget.value)}
        />
        <Group mt="md" justify="flex-end">
          <Button variant="default" onClick={closeNew}>
            Cancel
          </Button>
          <Button disabled={createBranch.isPending || !newName.trim()} onClick={handleCreate}>
            Create
          </Button>
        </Group>
      </Modal>
    </>
  );
}
```

Mantine `Modal` renders `role="dialog"` with the `title` as its accessible name,
which is what the tests query.

- [ ] **Step 4: Run the tests**

```bash
cd frontend && pnpm vitest run src/cockpit/BranchBar.test.tsx
```

Expected: PASS, 8 tests. If `getByRole("button", {name: /test/})` is ambiguous
because the dialog buttons also match, tighten the query to the `aria-label`
(`{ name: "Branch: test" }`) — adjust the **test**, not the component's label.

- [ ] **Step 5: Delete `BranchPicker` and unwire it**

```bash
cd /home/parlar_ai/dev/call-monitor
git rm frontend/src/cockpit/panes/BranchPicker.tsx frontend/src/cockpit/panes/BranchPicker.test.tsx
```

In `frontend/src/cockpit/CockpitSidebar.tsx`, delete the
`import { BranchPicker } from "./panes/BranchPicker";` line and the
`<BranchPicker id={id} />` element, leaving:

```tsx
      {view === "versions" && <VersionGraph id={id} />}
```

The `<>…</>` fragment around the two children is no longer needed; remove it.

- [ ] **Step 6: Confirm nothing else referenced it**

```bash
grep -rn "BranchPicker" frontend/src || echo "clean"
cd frontend && pnpm exec tsc --noEmit
```

Expected: `clean`, and tsc passes.

- [ ] **Step 7: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add -A frontend/src/cockpit frontend/src/api
git commit -m "feat(cockpit): BranchBar replaces BranchPicker, switch and delete now separated"
```

---

## Task 11: The pane itself

**Files:**
- Modify: `frontend/src/cockpit/VersionGraph.tsx`
- Modify: `frontend/src/cockpit/VersionGraph.module.css`
- Modify: `frontend/src/cockpit/VersionGraph.test.tsx`
- Regenerate: `docs/ARCHITECTURE.md`

**Interfaces:**
- Consumes: `assignLanes` (Task 8), `VersionLane`/`LANE_W`/`ROW_H` (Task 9),
  `BranchBar` (Task 10), `useBranches().data.tips` (Task 7),
  `VersionRow.parents` (Task 7).
- Produces: the finished pane. Nothing consumes it but `CockpitSidebar`.

**Background — the row cannot be one big button.** Branch pills are buttons and
HTML forbids a button inside a button. The row is a flex `<li>` of siblings:

```
[ lane svg ] [ ⑂main ⑂test ] [ button: sha  subject  ……  2h ] [ ⋯ ]
```

The select button keeps today's behaviour exactly: `aria-pressed`, the
hover/focus `Popover` card, and **one** URL write per click via
`nav.selectAndOpen(sha)`. Two `setSearchParams` writers in one handler clobber
each other in react-router 7.18.2 — its functional updater builds from the URL
captured at render, so the second write discards the first.

- [ ] **Step 1: Write the failing tests**

Add to `frontend/src/cockpit/VersionGraph.test.tsx`. Its `ROW` helper (line 9)
needs `parents`; extend it:

```tsx
const ROW = (
  sha: string,
  subject: string,
  created_by: string | null = "user",
  parents: string[] = [],
) => ({
  commit_sha: sha,
  committed_at: "2026-08-28T10:00:00+00:00",
  subject,
  created_by,
  triggering_turn_id: null,
  turn_prompt: null,
  parents,
});
```

Add a branches mock beside the existing `mockList`:

```tsx
import * as branchesMod from "../api/branches";

function mockBranches(current: string, tips: Record<string, string>) {
  vi.spyOn(branchesMod, "useBranches").mockReturnValue({
    data: { current, branches: Object.keys(tips), tips },
    isError: false,
  } as never);
}
```

Then the new cases:

```tsx
it("puts a pill on the commit each branch tip points at", () => {
  mockList({ versions: [ROW("aaaaaaa1", "newer", "user", ["bbbbbbb2"]), ROW("bbbbbbb2", "older")] });
  mockBranches("test", { test: "aaaaaaa1", main: "bbbbbbb2" });
  wrap();
  expect(screen.getByRole("button", { name: "Switch to main" })).toBeInTheDocument();
  // The branch you are already on is labelled, not offered as a switch.
  expect(screen.getByText("test")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Switch to test" })).toBeNull();
});

it("a branch whose tip is not in the list gets no pill", () => {
  mockList({ versions: [ROW("aaaaaaa1", "newer")] });
  mockBranches("test", { test: "aaaaaaa1", elsewhere: "ccccccc3" });
  wrap();
  expect(screen.queryByRole("button", { name: "Switch to elsewhere" })).toBeNull();
});

it("every row's lane strip is the same width", () => {
  // A fork: row 2 needs two columns, row 1 needs one. Equal widths are what
  // keeps the subject text from sliding sideways.
  mockList({
    versions: [
      ROW("aaaaaaa1", "on main", "user", ["ccccccc3"]),
      ROW("bbbbbbb2", "on side", "user", ["ccccccc3"]),
      ROW("ccccccc3", "shared root"),
    ],
  });
  mockBranches("main", { main: "aaaaaaa1" });
  const { container } = wrap();
  const widths = [...container.querySelectorAll("svg")].map((s) => s.getAttribute("width"));
  expect(new Set(widths).size).toBe(1);
});

it("offers Branch from here without needing a hover first", async () => {
  mockList({ versions: [ROW("aaaaaaa1", "newer")] });
  mockBranches("main", { main: "aaaaaaa1" });
  wrap();
  // Always in the DOM: a control revealed only on mouse hover is unreachable by
  // keyboard and invisible to a screen reader.
  const kebab = screen.getByRole("button", { name: "Actions for aaaaaaa1" });
  await userEvent.click(kebab);
  expect(screen.getByRole("menuitem", { name: "Branch from here…" })).toBeInTheDocument();
});

it("Branch from here opens the new-branch dialog for THAT commit", async () => {
  mockList({ versions: [ROW("aaaaaaa1", "newer"), ROW("bbbbbbb2", "the older one")] });
  mockBranches("main", { main: "aaaaaaa1" });
  wrap();
  await userEvent.click(screen.getByRole("button", { name: "Actions for bbbbbbb2" }));
  await userEvent.click(screen.getByRole("menuitem", { name: "Branch from here…" }));
  expect(screen.getByRole("dialog", { name: "New branch" })).toBeInTheDocument();
  expect(screen.getByText(/the older one/)).toBeInTheDocument();
});
```

`wrap()` is the file's existing helper (line 32). It already renders the pane
inside `MantineProvider` + `MemoryRouter` and **already returns `render`'s
result**, so `const { container } = wrap()` works without changing it.

- [ ] **Step 2: Run and watch them fail**

```bash
cd frontend && pnpm vitest run src/cockpit/VersionGraph.test.tsx
```

Expected: the five new cases fail; the existing ones still pass.

- [ ] **Step 3: Rewrite the CSS module**

Replace the lane rules in `frontend/src/cockpit/VersionGraph.module.css`. **Delete
the file's opening comment** — "measured across all 14 workspace repos, every one
has a single branch" is stale on both counts and this work is what makes it so.

```css
/* One row of the commit graph. Height is FIXED: the SVG lane strips only join
   into continuous lines when every row is exactly ROW_H tall, which is also why
   the subject ellipsises instead of wrapping. */
.row {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  height: 22px;
  padding-right: 0.4rem;
}
.row:hover {
  background: var(--app-hover);
}

/* The clickable middle. NOT wrapping the whole row: the branch pills are
   buttons too, and a button inside a button is invalid. */
.button {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  flex: 1;
  min-width: 0;
  height: 100%;
  background: transparent;
  border: 0;
  text-align: left;
  color: var(--app-text);
  font-size: 0.85rem;
  cursor: pointer;
}
.button[aria-pressed="true"] {
  background: var(--app-selected);
  outline: 1px solid var(--app-accent);
  outline-offset: -1px;
}

.pill {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 2px;
  max-width: 90px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 0 5px;
  border-radius: 8px;
  font-size: 0.7rem;
  line-height: 15px;
  border: 1px solid var(--app-border-strong);
  background: transparent;
  color: var(--app-muted);
  cursor: pointer;
}
.pill[data-current="true"] {
  background: var(--app-accent);
  border-color: var(--app-accent);
  color: var(--app-editor);
  cursor: default;
}

.sha {
  flex: 0 0 auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.78rem;
  color: var(--app-muted);
}
/* `min-width: 0` is load-bearing: without it a flex item refuses to shrink below
   its content and the row overflows the pane instead of ellipsising. */
.subject {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.when {
  flex: 0 0 auto;
  font-size: 0.72rem;
  color: var(--app-dim);
}
.note {
  color: var(--app-muted);
  padding: 0.25rem 0.5rem;
  font-size: 0.85rem;
}
.list {
  list-style: none;
  margin: 0;
  padding: 0;
}
```

- [ ] **Step 4: Rewrite the component**

In `frontend/src/cockpit/VersionGraph.tsx`, keep the existing `VersionGraphRow`
Popover machinery (the 250ms timer, `onFocus`/`onBlur`, the card contents) — a
`HoverCard` there would bind mouse only and be invisible to keyboard users, which
an earlier review already caught. Change what the row renders and what the pane
computes.

Add at the top of the module:

```tsx
import { Menu } from "@mantine/core";

import { useBranches } from "../api/branches";
import { LANE_W, ROW_H, VersionLane } from "./VersionLane";
import { assignLanes, type LaneRow } from "./versionLanes";

/** "2h", "3d" — a coarse age, because the exact stamp is in the hover card and
 *  a row has 40px for this. */
function shortAge(iso: string, now: number): string {
  const secs = Math.max(0, (now - new Date(iso).getTime()) / 1000);
  if (secs < 60) return "now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h`;
  if (secs < 86400 * 30) return `${Math.floor(secs / 86400)}d`;
  return `${Math.floor(secs / (86400 * 30))}mo`;
}
```

Give `VersionGraphRow` these extra props and render them:

```tsx
function VersionGraphRow({
  v, selected, onSelect, laneRow, columns, pills, currentBranch, onSwitch, onBranchFrom, now,
}: {
  v: VersionRow;
  selected: boolean;
  onSelect: () => void;
  laneRow: LaneRow;
  columns: number;
  /** Branch names whose tip is this commit. */
  pills: string[];
  currentBranch: string | undefined;
  onSwitch: (name: string) => void;
  onBranchFrom: () => void;
  now: number;
}) {
```

Inside its `<li className={styles.row}>`, before the Popover target:

```tsx
      <VersionLane row={laneRow} columns={columns} role={v.created_by} selected={selected} />
      {pills.map((name) => {
        const isCurrent = name === currentBranch;
        return (
          <button
            key={name}
            type="button"
            className={styles.pill}
            data-current={isCurrent}
            disabled={isCurrent}
            aria-label={isCurrent ? name : `Switch to ${name}`}
            onClick={() => onSwitch(name)}
          >
            ⑂ {name}
          </button>
        );
      })}
```

Inside the select button, replace the old `<span className={styles.lane}>` block
(the lane is now the SVG above) and add the age:

```tsx
            <span className={styles.sha}>{sha7}</span>
            <span className={styles.subject}>{v.subject}</span>
            <span className={styles.when}>{shortAge(v.committed_at, now)}</span>
```

After the Popover, still inside the `<li>`:

```tsx
      {/* Always in the DOM, not revealed on hover: a control that only exists
          while the mouse is over it cannot be reached by keyboard. */}
      <Menu position="bottom-end" withinPortal>
        <Menu.Target>
          <button type="button" className={styles.pill} aria-label={`Actions for ${sha7}`}>
            ⋯
          </button>
        </Menu.Target>
        <Menu.Dropdown>
          <Menu.Item onClick={onBranchFrom}>Branch from here…</Menu.Item>
          <Menu.Item onClick={() => navigator.clipboard?.writeText(v.commit_sha)}>
            Copy commit id
          </Menu.Item>
        </Menu.Dropdown>
      </Menu>
```

In `VersionGraph` itself:

```tsx
export function VersionGraph({ id }: { id: string }) {
  const nav = useHistoryNav();
  const list = useVersions(id, nav.artifact);
  const branches = useBranches(id);
  const data = list.data;
  const rows = data?.versions ?? [];

  const [branchFrom, setBranchFrom] = useState<{ sha: string; subject: string } | null>(null);
  const [switchTo, setSwitchTo] = useState<string | null>(null);

  // One age baseline for the whole render, so rows cannot disagree about "now".
  const now = Date.now();

  const lanes = useMemo(
    () => assignLanes(rows.map((v) => ({ sha: v.commit_sha, parents: v.parents }))),
    [rows],
  );
  // ONE width for every strip. Sizing each row by its own `columns` slides the
  // pills and subject sideways as the graph narrows.
  const columns = Math.max(1, ...lanes.map((l) => l.columns));

  const tips = branches.data?.tips ?? {};
  const pillsFor = (sha: string) => Object.keys(tips).filter((name) => tips[name] === sha);
```

Render `<BranchBar id={id} branchFrom={branchFrom} onBranchFromHandled={() => setBranchFrom(null)} />`
immediately after `<PaneHeader …>`, and pass the new props through in the `.map`:

```tsx
          {rows.map((v, i) => (
            <VersionGraphRow
              key={v.commit_sha}
              v={v}
              laneRow={lanes[i]}
              columns={columns}
              pills={pillsFor(v.commit_sha)}
              currentBranch={branches.data?.current}
              onSwitch={setSwitchTo}
              onBranchFrom={() => setBranchFrom({ sha: v.commit_sha, subject: v.subject })}
              now={now}
              selected={v.commit_sha === nav.a || v.commit_sha === nav.b}
              onSelect={() => nav.selectAndOpen(v.commit_sha)}
            />
          ))}
```

A pill click sets `switchTo`; wire that to `BranchBar` by lifting the same
confirm the bar already owns — pass `switchTo` and `onSwitchHandled` through as
two more props on `BranchBar`, mirroring `branchFrom`, so there is **one** switch
confirm dialog in the pane rather than two implementations of the same warning.

Import `useMemo` and `useState` from React (`useState` is already imported).

- [ ] **Step 5: Run the frontend gates**

```bash
cd frontend && pnpm exec tsc --noEmit && pnpm vitest run
```

Expected: tsc clean; every test passes. Note the file/test counts in the commit
message.

- [ ] **Step 6: Look at it**

```bash
cd frontend && pnpm build
```

Expected: clean. Then check the running app in the browser at the cockpit's
Versions view — the app runs in tmux `cm-app` with `SPA_MODE=dev`, Vite on 5173.
Confirm by eye: the branch bar sits under `VERSIONS n`, pills sit on the right
commits, lane dots line up in one column, and nothing overflows the pane.

- [ ] **Step 7: Regenerate the architecture map and run everything**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run python scripts/gen_architecture.py
uv run ruff check . && uv run ruff format --check .
uv run pytest -n 16 --dist loadfile
```

Expected: `… passed, 1 skipped`. **`0 skipped` means a live billed Anthropic API
call ran** — that is the signal to check, not the total, which moves whenever
tests are added.

- [ ] **Step 8: Commit**

```bash
git add -A frontend/src/cockpit docs/ARCHITECTURE.md
git commit -m "feat(cockpit): VS Code-style version graph with branch lanes and pills"
```

---

## Self-review notes

Checked against the spec section by section.

- §5.1 parents + defensive parse → Task 1. §5.2 `all_branches` → Task 2.
  §5.3 `create_branch` start → Task 4. §5.4 `branch_tips` → Task 3.
  §5.5 service `from_sha` + `BranchStartUnknown` → Task 4. §5.6 route + `tips`
  → Tasks 4 and 5. §5.7 versions route → Task 6.
- §6.1–6.2.1 lane algorithm → Task 8, including the no-dedup constraint and the
  fidelity-vs-continuity gate. §6.3 rendering → Task 9. §6.4 row layout →
  Task 11. §6.5 branch bar → Task 10. §6.6 states → Tasks 10 and 11.
- §7 file list → every file appears in a task; `BranchPicker` deletion is Task 10,
  OpenAPI regen is Task 7, `ARCHITECTURE.md` is Task 11 Step 7.
- §8 testing → each named test has a home. The exact-shape widening is Task 5
  Step 1; the switch-tips gap is Task 5's second test.

**Known soft spots, called out rather than papered over:**

1. Every test-file helper this plan names was read and verified on 2026-08-31:
   `_repo(tmp_path, n_commits)` (test_gitio_list_commits.py:24),
   `_repo(tmp_path, *, commit=True)` (test_gitio_branches.py:31),
   `_ws(session_factory, tmp_path)` + `stamp_for` (test_branch_create_delete.py:27),
   `_make_ws` + `_commit` (test_versions_api.py:17,31), and `wrap()`
   (VersionGraph.test.tsx:32). If any of them has moved since, read the file and
   use what is actually there — but **do not weaken an assertion to fit an
   unfamiliar fixture**; ask instead.
2. Task 11 Step 4 is the largest single edit in the plan and is described as a
   set of changes to an existing component rather than a full file. That is
   deliberate: the Popover machinery in `VersionGraphRow` was hard-won across
   seven review rounds (hover timers, `onFocus`/`onBlur`, `role="region"` on the
   card) and rewriting the file wholesale is how that gets quietly lost.
3. The pill-click switch confirm (Task 11 Step 4, last paragraph) shares
   `BranchBar`'s dialog through props. If that wiring turns out awkward in
   practice, the alternative is to move the three dialogs into a small
   `useBranchDialogs()` hook that both components call — but do not do that
   speculatively.
