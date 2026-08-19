# Fence F3d1 — The Spine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put everything a converted tool will need inside `ports/`, hand every tool
surface a live `Ports` bundle, and make F3d's eight identities impossible to lose —
without converting a single tool.

**Architecture:** F3d converts the tool sites, and it is too large for one slice: ~26
production files, a 512-line citation core to relocate, 194 tests riding on it. It is cut
into three. **This is the first: purely additive, exactly the shape F3b and F3c both
landed on.** Nothing changes behaviour, no module goes green, the exemption list stays at
nine. What lands is the spine the other two stand on — `Invocation`, the artifact
filenames and the busy/deleted errors move into `ports/`; `ToolContext` gains a `ports`
field that the five adapter factories populate; `_safe_tool` learns to thread it; and the
inventory learns to say which of the three slices owns each row.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, pytest.

**Revision 1 (2026-08-20).** Three agents executed revision 0 verbatim — Tasks 1-5 and
Tasks 6-7 in separate throwaway worktrees, plus an independent document audit. The slice
DOES execute end to end and the arithmetic held (3715 baseline, +20, +2 → 3737), but 26
findings came back, 3 of them blocking. Every fix below is folded in:

- **Task 3 named three of `Invocation`'s four members.** `provenance()` (`registry.py:236-249`)
  was in neither the move list nor the stay list, and the import block omitted
  `Provenance`. Both readings fail: move the class and `ruff` reports `F821` — the exact
  failure GC-2 is built on; move only the enumerated members and `test_f3b_stamp.py`
  raises `AttributeError`.
- **Task 3 Step 4 put the re-export where the class was** (`E402`) and left four imports
  orphaned (`Mapping`, `MappingProxyType`, `UnfencedInvocation`, `FenceStamp`/`Provenance`
  → four `F401`s). `ruff check .` at Step 7 fails.
- **Task 5's third assertion was false by construction.** `assert "error" not in str(result)`
  can never hold: the success envelope is `{"content": [...], "is_error": False}`.
- **Three mutation steps proved something other than what they claimed.** Task 2's leaves
  `test_collab_retire_mutex.py` fully green; Task 3's creates a circular import and dies
  at `PYTEST_EXIT=4` before the test runs; Task 6's fails identically with or without the
  key split, so it is invariant to the thing it justifies. All three replaced, and the
  claims they rested on (GC-3, GC-4) corrected.
- **`_DEFAULT_SLICE = "F3d"` survived the key split**, so the first regeneration after
  this slice would stamp a dead slice name.
- **A fourth `CITATIONS_FILENAME` literal** lives in `editing/save.py:41` and is the copy
  eight modules import. Task 1 now repoints it.

Both load-bearing premises were independently confirmed: GC-2's `F821` reproduces, and
Task 6 IS necessary — proved by the counterfactual the revised Step 5 now runs.

**Spec:** `docs/superpowers/specs/2026-08-18-fence-session-ownership-design.md` (revision
8) — §7's F3d paragraph is the charter for all three slices; §3 is the port design; §4 the
gate. This slice implements no §7 conversion: it is the prerequisite the spec's F3d
paragraph does not name, discovered by survey (see Global Constraints GC-2 and GC-3).

## Global Constraints

Every task's requirements implicitly include this section.

- **GC-1 — Additive. Nothing is converted.** No tool impl changes behaviour, no tool
  calls a port, no exemption is deleted, no inventory row flips `fenced`. At the end of
  this slice `TOOLS_NOT_YET_CONVERTED` still has **nine** entries and all eight F3d rows
  are still `fenced: false`. A task that makes a tool module cleaner has exceeded scope —
  say so and stop rather than "finishing the job".
- **GC-2 — `Invocation` must leave `registry.py`, and this is why the slice exists.**
  All nine exempt modules pin `from call_manager.proposal_assist.tools.registry import ...`.
  No tool module can ever reach zero violations while it names `registry`, and a converted
  impl needs `Invocation` in its signature. `from __future__ import annotations` does not
  rescue this: `ruff` (a CI gate, `select = ["E","F","I","B","UP","SIM"]`) reports **F821
  Undefined name** for a lazily-annotated parameter with no import. So the type must be
  importable from `ports/`.
- **GC-3 — `ports/` stays stdlib-only, and that is mechanically enforced.**
  `tests/proposal_assist/test_f3b_ports_boundary.py` walks the package's imports in a
  fresh interpreter. Anything moved into `ports/` must import nothing but the standard
  library and other `ports/` modules. `StrEnum`, `uuid`, `dataclasses`, `MappingProxyType`
  are all fine; `sqlalchemy`, `db.models` and any project module are not.
  Know the gate's actual reach: it greps `sys.modules` for
  `{'sqlalchemy','psycopg','alembic'}` and flags source names containing `port_impl`
  or starting `sqlalchemy`/`psycopg`. A ports module importing a stdlib-clean project
  module (`proposal_assist.paths`, say) passes both checks. The RULE is stdlib-only;
  the TEST catches the database coupling. Do not claim the test enforces the rule.
- **GC-4 — A relocation keeps ONE source of truth and re-exports from the old home.**
  Every existing import site must keep working untouched, and the moved object must remain
  the *same object* — `is`, not `==`. This is the pattern F3b used for
  `WorkspaceGenerationStale`.
  Do NOT justify this with `assert CommitMutexTimeout in clean`
  (`tests/proposal_assist/collab/test_collab_retire_mutex.py:287`). That assertion is
  blind to the relocation: the test (line 28) and the production list it checks
  (`proposals/kernel_accept.py:19`) BOTH resolve the name through `drafting.locking`,
  so forking the class rebinds every participant together and membership still holds.
  It passes with or without identity. The `is` check in Task 2 is the ONLY thing that
  catches a fork — which is precisely why it has to be written.
  `ruff` flags an unused re-export, so use the redundant-alias form
  (`from x import Y as Y`) or add the name to `__all__`.
- **GC-5 — Two `ToolContext` constructions must NOT gain `ports=`.**
  `tools/registry.py:293` (`collect_mcp_tool_names`) and
  `proposals/accepting.py:195` build a context with `session_factory=None`.
  `tests/fencing/detectors.py:92-100` keys on that literal to classify them **inert** and
  exclude them from the inventory scan. Be precise about the mechanism: the detector
  tests `any(kw.arg == "session_factory" and ... is None)` and IGNORES every other
  keyword, so adding `ports=None` beside it leaves `tests/fencing` at 82 passed — only
  REMOVING the `session_factory=None` literal makes two `tool_context` rows appear.
  The instruction stands anyway, for the real reason: there is no `session_factory` at
  either site to build a bundle from, so there is nothing to pass.
  Note the line drifts inside this slice: the registry site is `:293` on entry and
  `:228` after Task 3 deletes the `Invocation` class. Do not write `:293` into a
  docstring in Task 4.
- **GC-6 — `SLICE_STATUS` has no partial state.**
  `test_each_slice_matches_its_declared_status` (`tests/fencing/test_fence_coverage.py:103`)
  fails on any row that is `fenced: true` while its slice is `pending`. A three-way cut
  therefore needs three keys, assigned in THIS slice, before any row can flip. Forgetting
  it makes slice two un-mergeable.
- **GC-7 — Test command.** `uv run pytest -n 16 --dist loadfile`. Never `-n auto`
  (resolves to 72 here) and never `--dist loadgroup`. Baseline on master before this
  branch: **3715 passed, 1 skipped** (`71c0493`). The `1 skipped` is an
  `ANTHROPIC_API_KEY`-gated test — read it as the signal, not the total.
  `tests/fencing` alone is 82 tests in ~3.5s with no database: run it after every task.
  The paired `test_f3b_ports_boundary.py` is NOT database-free — four of its six tests
  take `pg_session_factory` and start a testcontainers Postgres, so the combined
  commands in Tasks 1-3 Step 6 are slower than "3.5s" suggests.
- **GC-7a — If you work in a worktree, `uv run` is a trap.** A fresh worktree has no
  `.venv`, so `uv run` builds one and resolves it to Python 3.14. Either
  `uv sync --python 3.12` in the worktree first, or substitute the main tree's
  interpreter with `PYTHONPATH="$(pwd)/src"` so your own `src/` is what gets imported —
  without it the editable install resolves to the main tree and your changes have no
  effect while the tests still pass.
- **GC-8 — `ruff check .` is the lint gate. `ruff format` is NOT** — do not run it and do
  not reformat files to satisfy it.
- **GC-9 — Mutation testing happens AFTER the task's commit, never before, and the restore
  is verified by a command that FAILS.** `git checkout <path>` cannot restore a file the
  task itself created — it errors, and piped through anything the non-zero exit is masked.
  So commit the green implementation first, then mutate, then:

  ```bash
  git checkout <path>
  git diff --exit-code HEAD -- <path>
  ```

  Never `git status --porcelain`: it exits 0 whether the tree is clean or filthy.

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `src/call_manager/proposal_assist/ports/artifacts.py` | The artifact identity and its filenames — the only names a tool needs to talk about files. Stdlib only. |
| `src/call_manager/proposal_assist/ports/invocation.py` | `Invocation`: one tool call's frozen identity and per-turn provenance, plus `stamp()`. Stdlib only. |
| `tests/proposal_assist/test_f3d1_spine.py` | The whole slice's own tests: the relocations keep identity, the five factories produce working ports, `_safe_tool` threads them. |

**Modified:**

| File | Change |
|---|---|
| `src/call_manager/proposal_assist/artifacts.py` | Re-exports the filenames from `ports/artifacts.py`. Keeps `SPEC_SKELETON`, which is page content, not a locator. |
| `src/call_manager/proposal_assist/ports/errors.py` | Gains `CommitMutexTimeout`, `BUSY_ERROR_MESSAGE`, `DELETED_ERROR_MESSAGE`. |
| `src/call_manager/proposal_assist/drafting/locking.py` | Re-exports those three. |
| `src/call_manager/proposal_assist/ports/effects.py` | The `Effects` Protocol docstring stops saying `CommitMutexTimeout` is outside `ports/` (Task 2 Step 5). |
| `proposal_assist/references/deletion.py`, `proposal_assist/reconcile.py`, `proposal_assist/editing/save.py` | Import `CITATIONS_FILENAME` instead of redefining it. (Note: `proposal_assist/reconcile.py`, NOT `references/reconcile.py` — and there is a second `analysis/reconcile.py` that is not this.) |
| `src/call_manager/proposal_assist/tools/registry.py` | Imports `Invocation` from `ports/`, keeps `snapshot_invocation(ctx)`; `ToolContext` gains `ports`; `_safe_tool` gains `pass_ports`. |
| `coherence/agent.py`, `evaluator/agent.py`, `review/agent.py`, `uncited_claims/agent.py`, `drafting/factory.py` | Each passes `ports=build_ports(session_factory)`. |
| `tests/fencing/inventory.py` | `SLICE_STATUS` splits `F3d` into `F3d1/F3d2/F3d3`; gains `F3D_IDENTITIES`. |
| `tests/fencing/fence_inventory.json` | The eight F3d rows get their new slice names. |
| `tests/fencing/test_fence_coverage.py` | Gains the anti-vacuity test over `F3D_IDENTITIES`. |
| `tests/proposal_assist/test_f3a_invocation_snapshot.py`, `test_f3b_stamp.py` | Call `snapshot_invocation(ctx)` instead of `Invocation.snapshot(ctx)`. |

**Deliberately NOT done here** (named so a reviewer does not read them as gaps): no tool
imports a port; `tools/citations.py:80` keeps its own `CITATIONS_FILENAME` literal (that
module is F3d3's, and touching it now would be a conversion); `WorkspaceDeleted` stays in
`drafting/locking.py` (only the citation path catches it, which is F3d3); and no
`agent_view_*` decision is taken (F3d3 owns it, because `proposal.py` and `citations.py`
share that snapshot and cannot convert independently).

---

### Task 1: The artifact filenames move into `ports/`

A converted tool still has to name `proposal.md`. Today those constants live in
`proposal_assist/artifacts.py`, which is stdlib-clean already but is NOT on the gate's
allowlist — only `call_manager.proposal_assist.ports` is. So the names a tool needs move
into `ports/`, and the old module keeps working by re-export.

`SPEC_SKELETON` stays behind: it is seeded page content, not something a tool names.

**Files:**
- Create: `src/call_manager/proposal_assist/ports/artifacts.py`
- Create: `tests/proposal_assist/test_f3d1_spine.py`
- Modify: `src/call_manager/proposal_assist/artifacts.py`
- Modify: `src/call_manager/proposal_assist/references/deletion.py:38`
- Modify: `src/call_manager/proposal_assist/reconcile.py:36`
- Modify: `src/call_manager/proposal_assist/editing/save.py:41`

**Interfaces:**
- Consumes: nothing.
- Produces, in `call_manager.proposal_assist.ports.artifacts`:
  - `Artifact` (StrEnum: `DRAFT = "draft"`, `SPEC = "spec"`)
  - `filename_for(artifact: Artifact) -> str`
  - `PROPOSAL_FILENAME: str`, `SPEC_FILENAME: str`, `CITATIONS_FILENAME: str`

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_f3d1_spine.py`:

```python
"""F3d1 — the spine. Everything a converted tool will need, in place, with nothing
converted.

Spec 2026-08-18 revision 8 §7. This slice is additive by construction: if any test
here starts needing a tool module to have changed, the slice has exceeded its scope.
"""

from __future__ import annotations


def test_the_artifact_filenames_are_reachable_from_ports():
    """A converted tool may import only stdlib and ports, and it still has to name
    proposal.md. These constants are the reason ports/artifacts.py exists."""
    from call_manager.proposal_assist.ports.artifacts import (
        CITATIONS_FILENAME,
        PROPOSAL_FILENAME,
        SPEC_FILENAME,
    )

    assert PROPOSAL_FILENAME == "proposal.md"
    assert SPEC_FILENAME == "proposal-spec.md"
    assert CITATIONS_FILENAME == "citations.json"


def test_the_old_artifacts_module_re_exports_the_same_objects():
    """GC-4: one source of truth. Every existing importer keeps working, and the
    value it gets is the value ports defines — not a second copy that can drift."""
    from call_manager.proposal_assist import artifacts as old
    from call_manager.proposal_assist.ports import artifacts as new

    assert old.PROPOSAL_FILENAME is new.PROPOSAL_FILENAME
    assert old.SPEC_FILENAME is new.SPEC_FILENAME
    assert old.Artifact is new.Artifact
    assert old.filename_for is new.filename_for


def test_spec_skeleton_stays_out_of_ports():
    """Seeded page content is not a capability. Keeping it behind is what stops
    ports/ from becoming the place things go when nobody knows where they belong."""
    from call_manager.proposal_assist import artifacts as old
    from call_manager.proposal_assist.ports import artifacts as new

    assert hasattr(old, "SPEC_SKELETON")
    assert not hasattr(new, "SPEC_SKELETON")


def test_no_module_redefines_the_citations_filename_except_the_unconverted_tool():
    """CITATIONS_FILENAME was written out FOUR times: references/deletion.py:38,
    reconcile.py:36, editing/save.py:41 and tools/citations.py:80. Three now import
    it. editing/save.py is included even though the plan's narrative is about tools
    — it is the copy eight other modules import (export.py, human_cite.py,
    collab/kernel.py, analysis/insert.py, web/api/workspaces.py,
    web/api/citation_audit.py, problems/aggregate.py), so leaving it out would leave
    the widest-blast-radius literal unguarded. The fourth is tools/citations.py,
    which F3d3 converts — until then this test is what stops the copies drifting."""
    from call_manager.proposal_assist import reconcile
    from call_manager.proposal_assist.editing import save
    from call_manager.proposal_assist.ports.artifacts import CITATIONS_FILENAME
    from call_manager.proposal_assist.references import deletion
    from call_manager.proposal_assist.tools import citations

    assert reconcile.CITATIONS_FILENAME is CITATIONS_FILENAME
    assert deletion.CITATIONS_FILENAME is CITATIONS_FILENAME
    assert save.CITATIONS_FILENAME is CITATIONS_FILENAME
    assert citations.CITATIONS_FILENAME == CITATIONS_FILENAME
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d1_spine.py -q > /tmp/f3d1-1.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d1-1.txt
```
Expected: `PYTEST_EXIT=1`, with `ModuleNotFoundError: No module named 'call_manager.proposal_assist.ports.artifacts'`.

- [ ] **Step 3: Create the ports module**

Create `src/call_manager/proposal_assist/ports/artifacts.py`:

```python
"""The artifacts a tool can name, and their filenames — stdlib only.

A converted tool may import the standard library and this package, and nothing
else. It still has to say "proposal.md", so the names live here. The old home,
`proposal_assist/artifacts.py`, re-exports every one of these, so the 22 existing
importers under src/ are unchanged.

`Artifact`'s string values MUST match the `ProposedChange.target_artifact` CHECK
('draft','spec') in db/models.py and the files seeded by phases/setup.py.
"""

from __future__ import annotations

from enum import StrEnum


class Artifact(StrEnum):
    DRAFT = "draft"  # MUST match ck_proposed_changes_target_artifact (db/models.py:1181)
    SPEC = "spec"


_FILENAMES: dict[Artifact, str] = {
    Artifact.DRAFT: "proposal.md",
    Artifact.SPEC: "proposal-spec.md",
}


def filename_for(artifact: Artifact) -> str:
    return _FILENAMES[Artifact(artifact)]


PROPOSAL_FILENAME = filename_for(Artifact.DRAFT)
SPEC_FILENAME = filename_for(Artifact.SPEC)

#: The per-workspace citation ledger. Written out as a literal in FOUR places
#: before F3d1 — references/deletion.py, reconcile.py, editing/save.py and
#: tools/citations.py. The first three now import it. tools/citations.py keeps its
#: own copy until F3d3 converts that module.
CITATIONS_FILENAME = "citations.json"
```

- [ ] **Step 4: Re-export from the old home**

Replace the body of `src/call_manager/proposal_assist/artifacts.py` above
`SPEC_SKELETON` with a re-export, keeping the module docstring's first paragraph and
`SPEC_SKELETON` exactly as they are:

```python
"""Single source of truth for the proposal-assist artifacts and their files.

`proposal.md` (the draft) and `proposal-spec.md` (the spec) live in one git repo
per workspace. The names themselves moved to `ports/artifacts.py` in F3d1, because
a converted tool may import only stdlib and `ports` and still has to name a file;
they are re-exported here so every existing importer is unchanged. SPEC_SKELETON
stays: it is seeded page content, not a name a tool needs.
"""

from __future__ import annotations

from call_manager.proposal_assist.ports.artifacts import (
    PROPOSAL_FILENAME as PROPOSAL_FILENAME,
)
from call_manager.proposal_assist.ports.artifacts import SPEC_FILENAME as SPEC_FILENAME
from call_manager.proposal_assist.ports.artifacts import Artifact as Artifact
from call_manager.proposal_assist.ports.artifacts import filename_for as filename_for
```

The redundant-alias form is deliberate: `ruff`'s F401 flags a plain unused import,
and this file's whole job is now to re-export.

- [ ] **Step 5: Point the three other definitions at it**

In `src/call_manager/proposal_assist/references/deletion.py`, delete the
`CITATIONS_FILENAME = "citations.json"` literal at line 38 and add to the imports:

```python
from call_manager.proposal_assist.ports.artifacts import CITATIONS_FILENAME
```

Do exactly the same in `src/call_manager/proposal_assist/reconcile.py` (line 36) and
in `src/call_manager/proposal_assist/editing/save.py` (line 41). `save.py` is the one
easiest to overlook and the one that matters most: eight modules import
`CITATIONS_FILENAME` from it (`export.py`, `citations/human_cite.py`,
`collab/kernel.py`, `analysis/insert.py`, `web/api/workspaces.py`,
`web/api/citation_audit.py`, `problems/aggregate.py` and two test files), so leaving
its literal in place would mean the busiest path is the one with no drift guard.
`ports/artifacts.py` is stdlib-only, so importing it introduces no cycle.

Leave `tools/citations.py:80` alone. It is a tool module and F3d3 owns it; changing
it now is a conversion, which GC-1 forbids.

- [ ] **Step 6: Run the tests**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d1_spine.py -q > /tmp/f3d1-1.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d1-1.txt
uv run pytest tests/proposal_assist/test_f3b_ports_boundary.py tests/fencing -q > /tmp/f3d1-1b.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -5 /tmp/f3d1-1b.txt
```
Expected: `PYTEST_EXIT=0` and 4 passed for the first; `PYTEST_EXIT=0` and 88 passed
for the second (82 fencing + 6 boundary). The boundary test is the one that proves
`ports/` is still stdlib-only after the move.

- [ ] **Step 7: Commit**

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/ports/artifacts.py src/call_manager/proposal_assist/artifacts.py src/call_manager/proposal_assist/references/deletion.py src/call_manager/proposal_assist/reconcile.py src/call_manager/proposal_assist/editing/save.py tests/proposal_assist/test_f3d1_spine.py
git commit -m "feat(ports): the artifact filenames a converted tool can name (F3d1 Task 1)"
```

- [ ] **Step 8: Prove the re-export is real**

In `src/call_manager/proposal_assist/artifacts.py`, replace the re-export of
`PROPOSAL_FILENAME` with a second definition: `PROPOSAL_FILENAME = "proposal.md"`.

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d1_spine.py -q -k re_exports > /tmp/f3d1-1m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d1-1m.txt
```
Expected: FAIL on `test_the_old_artifacts_module_re_exports_the_same_objects`. The
values are equal, so only the `is` check catches it — which is the point: two equal
copies are exactly what drifts.

```bash
git checkout src/call_manager/proposal_assist/artifacts.py
git diff --exit-code HEAD -- src/call_manager/proposal_assist/artifacts.py
```

---

### Task 2: The busy and deleted errors move into `ports/`

A converted tool has to answer "workspace is busy" the same way the browser does.
`CommitMutexTimeout` and `BUSY_ERROR_MESSAGE` live in `drafting/locking.py`, which a tool
may not import — and the `Effects` Protocol already documents that this exception escapes
every one of its methods. F3b recorded the problem and deliberately left it; this closes it.

The class must remain the SAME object: `tests/proposal_assist/collab/test_collab_retire_mutex.py:287`
asserts `CommitMutexTimeout in clean`, an identity test over a set of exception classes.

**Files:**
- Modify: `src/call_manager/proposal_assist/ports/errors.py`
- Modify: `src/call_manager/proposal_assist/drafting/locking.py:135-186`
- Modify: `src/call_manager/proposal_assist/ports/effects.py` (the Protocol docstring)
- Modify: `tests/proposal_assist/test_f3d1_spine.py`

**Interfaces:**
- Consumes: nothing.
- Produces, in `call_manager.proposal_assist.ports.errors`: `CommitMutexTimeout`,
  `BUSY_ERROR_MESSAGE`, `DELETED_ERROR_MESSAGE`.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_f3d1_spine.py`:

```python
def test_the_busy_error_is_reachable_from_ports():
    """Every Effects method can raise this — the shared lease has a 10s cap. A
    converted tool that cannot NAME it has to let it reach _safe_tool's catch-all,
    which turns "busy, retry" into "internal error"."""
    from call_manager.proposal_assist.ports.errors import (
        BUSY_ERROR_MESSAGE,
        CommitMutexTimeout,
    )

    assert issubclass(CommitMutexTimeout, RuntimeError)
    assert BUSY_ERROR_MESSAGE == "workspace is busy — try again in a moment"


def test_locking_re_exports_the_identical_error_class():
    """GC-4, and it is load-bearing here: collab/test_collab_retire_mutex.py asserts
    membership of this class in a set of clean errors. Two classes that merely look
    alike would pass every equality test in the tree and fail that one."""
    from call_manager.proposal_assist.drafting import locking
    from call_manager.proposal_assist.ports import errors

    assert locking.CommitMutexTimeout is errors.CommitMutexTimeout
    assert locking.BUSY_ERROR_MESSAGE is errors.BUSY_ERROR_MESSAGE
    assert locking.DELETED_ERROR_MESSAGE is errors.DELETED_ERROR_MESSAGE


def test_an_existing_raise_site_is_still_caught_by_the_ports_class():
    """The relocation must not fork the taxonomy: something raised through the old
    import path is catchable through the new one."""
    from call_manager.proposal_assist.drafting.locking import CommitMutexTimeout as Old
    from call_manager.proposal_assist.ports.errors import CommitMutexTimeout as New

    try:
        raise Old("busy")
    except New as exc:
        assert str(exc) == "busy"
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d1_spine.py -q -k "busy or re_exports_the_identical or raise_site" > /tmp/f3d1-2.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d1-2.txt
```
Expected: `PYTEST_EXIT=1`, `ImportError: cannot import name 'BUSY_ERROR_MESSAGE'`.

- [ ] **Step 3: Move the class and the two strings**

Append to `src/call_manager/proposal_assist/ports/errors.py`:

```python
class CommitMutexTimeout(RuntimeError):
    """The per-workspace commit mutex could not be acquired within the timeout —
    another writer is wedged or unusually slow.

    Lives here rather than in drafting/locking.py because EVERY `Effects` method can
    raise it (the shared lease caps its wait at 10s) and a tool must be able to NAME
    what it catches. `drafting/locking.py` re-exports it, so every pre-F3d1 import
    site is unchanged.

    It means "busy, retry", not "failed": web routes answer 429 with a
    `Retry-After: 2` header and BUSY_ERROR_MESSAGE (the collab checkpoint route in
    proposal_assist/web/collab_checkpoint.py is the lone holdout, still answering
    503). A converted tool that lets it escape hands the agent "internal error"
    instead, because tools/registry.py::_safe_tool catches everything it does not
    name.
    """


#: Served with 429 + Retry-After: 2, and returned verbatim by the tools. Here so the
#: message the agent sees and the message the browser sees cannot drift apart.
BUSY_ERROR_MESSAGE = "workspace is busy — try again in a moment"

#: The workspace was deleted while the actor was mid-turn.
DELETED_ERROR_MESSAGE = "workspace no longer exists"
```

- [ ] **Step 4: Re-export from `drafting/locking.py`**

In `src/call_manager/proposal_assist/drafting/locking.py`, delete the
`class CommitMutexTimeout` definition (line 135) and both message constants
(`BUSY_ERROR_MESSAGE` at 146, `DELETED_ERROR_MESSAGE` at 185), and add near the top
imports:

```python
from call_manager.proposal_assist.ports.errors import (
    BUSY_ERROR_MESSAGE as BUSY_ERROR_MESSAGE,
)
from call_manager.proposal_assist.ports.errors import (
    DELETED_ERROR_MESSAGE as DELETED_ERROR_MESSAGE,
)
from call_manager.proposal_assist.ports.errors import (
    CommitMutexTimeout as CommitMutexTimeout,
)
```

Leave every comment that surrounds those definitions where it is if it explains a
neighbouring constant — in particular `MUTEX_TAIL_LOCK_TIMEOUT_SECONDS` and its long
derivation comment are NOT part of this move.

- [ ] **Step 5: Correct the Protocol docstring this closes**

`src/call_manager/proposal_assist/ports/effects.py`'s `Effects` docstring currently
says F3d owns the decision of how a tool names `CommitMutexTimeout`, and that
`ports/` cannot simply hold the class because `BUSY_ERROR_MESSAGE` lives beside it.
Both have now happened. Replace BOTH of those paragraphs — the one beginning
"A THIRD exception escapes and it is NOT in ports/errors.py" AND the one beginning
"A converter that catches only the two errors above" — with the single block below.
Replacing only the first leaves the docstring contradicting itself.

```python
    A THIRD exception escapes, and since F3d1 it lives in `ports/errors.py` beside
    the others: `CommitMutexTimeout`, raised by the shared lease when an exclusive
    holder outlasts `acquire_shared_lease`'s 10s cap. It means "busy, retry", not
    "failed" — answer it with BUSY_ERROR_MESSAGE, which is in `ports/errors.py` too.
    A converted tool that catches only the two errors above DOWNGRADES a transient
    busy into an internal error, because `tools/registry.py::_safe_tool` catches
    every unnamed exception and returns "internal error".
```

- [ ] **Step 6: Run the tests**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d1_spine.py tests/proposal_assist/test_f3b_ports_boundary.py -q > /tmp/f3d1-2.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d1-2.txt
uv run pytest tests/proposal_assist/collab/test_collab_retire_mutex.py tests/test_api_workspaces.py tests/proposal_assist/test_mutex_tail_bound.py -q > /tmp/f3d1-2b.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -5 /tmp/f3d1-2b.txt
```
Expected: `PYTEST_EXIT=0` for both. The second command is the identity-sensitive set —
the retire-mutex file is the one that asserts class membership rather than equality.

- [ ] **Step 7: Commit**

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/ports/errors.py src/call_manager/proposal_assist/drafting/locking.py src/call_manager/proposal_assist/ports/effects.py tests/proposal_assist/test_f3d1_spine.py
git commit -m "feat(ports): the busy and deleted errors a converted tool must name (F3d1 Task 2)"
```

- [ ] **Step 8: Prove the identity requirement is real**

In `src/call_manager/proposal_assist/drafting/locking.py`, replace the
`CommitMutexTimeout` re-export with a fresh definition of the same shape:

```python
class CommitMutexTimeout(RuntimeError):
    """A second class that looks identical."""
```

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d1_spine.py tests/proposal_assist/collab/test_collab_retire_mutex.py -q > /tmp/f3d1-2m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/f3d1-2m.txt
```
Expected: `2 failed`, and BOTH failures are in the spine file —
`test_locking_re_exports_the_identical_error_class` and
`test_an_existing_raise_site_is_still_caught_by_the_ports_class`.

`test_collab_retire_mutex.py` stays FULLY GREEN (11 passed). Do not read that as a
failed restore. It imports `CommitMutexTimeout` from `drafting.locking` (line 28) and
so does the production list it checks (`proposals/kernel_accept.py:19`), so a fork
inside `locking.py` moves both sides together and the membership assertion cannot see
it. That is the point: the retire-mutex test is NOT a net for this, which is exactly
why the `is` check in Step 1 has to exist. See GC-4.

```bash
git checkout src/call_manager/proposal_assist/drafting/locking.py
git diff --exit-code HEAD -- src/call_manager/proposal_assist/drafting/locking.py
```

---

### Task 3: `Invocation` moves into `ports/`

This is the constraint that decides whether F3d can finish at all (GC-2). Every exempt
module pins the `tools.registry` import, a converted impl needs `Invocation` in its
signature, and a lazy annotation without an import is an F821 lint failure. So the type
has to be importable from `ports/`.

`Invocation` is already stdlib-clean apart from `FenceStamp` and `UnfencedInvocation`,
which are in `ports/` — with one exception: `snapshot(cls, ctx: ToolContext)` names the
context. That factory stays in `registry.py`, where its only production caller lives.

**Files:**
- Create: `src/call_manager/proposal_assist/ports/invocation.py`
- Modify: `src/call_manager/proposal_assist/tools/registry.py:160-249`
- Modify: `tests/proposal_assist/test_f3a_invocation_snapshot.py:156-161`
- Modify: `tests/proposal_assist/test_f3b_stamp.py`
- Modify: `tests/proposal_assist/test_f3d1_spine.py`

**Interfaces:**
- Consumes: `FenceStamp` and `UnfencedInvocation` from `ports/` (F3b).
- Produces:
  - `Invocation` in `call_manager.proposal_assist.ports.invocation` — the frozen
    dataclass and THREE members: `_freeze`, `stamp() -> FenceStamp` and
    `provenance() -> Provenance` (`registry.py:236-249`). No `snapshot`.
  - `snapshot_invocation(ctx: ToolContext) -> Invocation` in `tools/registry.py`.
  - `Invocation` stays importable from `tools/registry.py` (re-export).

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_f3d1_spine.py`:

```python
def test_invocation_is_reachable_from_ports():
    """GC-2, the reason this slice exists. Every exempt tool module pins the
    tools.registry import, a converted impl needs Invocation in its signature, and
    `from __future__ import annotations` does not save you — ruff reports F821 for a
    lazily-annotated name with no import. So the type must live in ports."""
    from call_manager.proposal_assist.ports.invocation import Invocation

    assert hasattr(Invocation, "stamp")


def test_the_snapshot_factory_stays_with_the_context_it_reads():
    """ports/ is stdlib-only, so nothing in it may name ToolContext. The factory
    belongs beside its only production caller, _safe_tool."""
    from call_manager.proposal_assist.ports.invocation import Invocation
    from call_manager.proposal_assist.tools.registry import snapshot_invocation

    assert not hasattr(Invocation, "snapshot")
    assert callable(snapshot_invocation)


def test_registry_still_exports_invocation_as_the_same_class():
    """GC-4. Two test files and any future reader import it from registry."""
    from call_manager.proposal_assist.ports.invocation import Invocation as FromPorts
    from call_manager.proposal_assist.tools.registry import Invocation as FromRegistry

    assert FromPorts is FromRegistry


def test_a_snapshot_still_stamps_and_still_refuses_an_unfenced_actor():
    """The behaviour F3a and F3b pinned survives the move: generation 0 is REAL, and
    a missing generation raises rather than defaulting."""
    import uuid

    import pytest

    from call_manager.proposal_assist.ports.errors import UnfencedInvocation
    from call_manager.proposal_assist.tools.registry import (
        ToolContext,
        snapshot_invocation,
    )

    ws = uuid.uuid4()

    def _ctx(generation):
        return ToolContext(
            workspace_id=ws,
            repo_dir=None,
            session_factory=None,
            settings=None,
            lifecycle_generation=generation,
        )

    assert snapshot_invocation(_ctx(0)).stamp().generation == 0
    with pytest.raises(UnfencedInvocation):
        snapshot_invocation(_ctx(None)).stamp()
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d1_spine.py -q -k "invocation or snapshot" > /tmp/f3d1-3.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d1-3.txt
```
Expected: `PYTEST_EXIT=1`, `ModuleNotFoundError: No module named 'call_manager.proposal_assist.ports.invocation'`.

- [ ] **Step 3: Create the ports module**

Create `src/call_manager/proposal_assist/ports/invocation.py` by moving the
`Invocation` dataclass out of `tools/registry.py`. The class is `registry.py:160-249`
and it has FOUR members, not three: every field, the class docstring, `_freeze`,
`stamp` and `provenance` move VERBATIM; only `snapshot` stays behind. `provenance()`
returns a `Provenance`, so that name must be imported too — miss it and `ruff check`
reports `F821 Undefined name \`Provenance\``, which is the exact failure mode GC-2 is
built on. Put it under this module docstring:

```python
"""One tool call's frozen identity — stdlib only.

Moved out of tools/registry.py in F3d1. A converted tool receives an Invocation and
may import only the standard library and this package; naming the type at all
therefore requires it to live here. `snapshot()` stayed behind, because it reads a
ToolContext and ports/ may not name one.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from call_manager.proposal_assist.ports.errors import UnfencedInvocation
from call_manager.proposal_assist.ports.stamp import FenceStamp, Provenance
```

- [ ] **Step 4: Leave the factory behind**

In `src/call_manager/proposal_assist/tools/registry.py`, delete the `Invocation`
class (lines 160-249).

The re-export import goes in the TOP import block, immediately after
`from call_manager.proposal_assist.ports.errors import UnfencedInvocation` — NOT where
the class was. Placing it at line 160 is `E402 Module level import not at top of file`
and `ruff check .` at Step 7 fails.

```python
from call_manager.proposal_assist.ports.invocation import Invocation as Invocation
```

The class took four imports with it, and they are now dead. Delete them or Step 7
fails with four `F401`s:
- `Mapping` from `from collections.abc import Awaitable, Callable, Mapping` (line 25)
  — keep `Awaitable, Callable`, they are still used
- the whole `from types import MappingProxyType` line (28)
- the whole `from call_manager.proposal_assist.ports.errors import UnfencedInvocation`
  line (33) — and therefore put the new import where it stood
- the whole `from call_manager.proposal_assist.ports.stamp import FenceStamp,
  Provenance` line (34)

Then, where the class used to be, add:

```python
def snapshot_invocation(ctx: ToolContext) -> Invocation:
    """Freeze one tool call's identity, at DISPATCH.

    Lives here rather than on Invocation because it reads a ToolContext and ports/
    is stdlib-only. Called before any await or thread handoff: ToolContext is
    MUTABLE and shared, set_turn_provenance restamps it every turn, so a tool
    reading these values when its effect happens can read a LATER turn's.
    """
    return Invocation(
        workspace_id=ctx.workspace_id,
        lifecycle_generation=ctx.lifecycle_generation,
        active_turn_job_id=ctx.active_turn_job_id,
        origin_kind=ctx.origin_kind,
        origin_job_id=ctx.origin_job_id,
        triggering_turn_id=ctx.triggering_turn_id,
        triggering_prompt=ctx.triggering_prompt,
        created_by=ctx.created_by,
        origin_session_id=ctx.origin_session_id,
        turn_selection=Invocation._freeze(ctx.turn_selection),
    )
```

Then change the one call site in `_safe_tool` from
`kwargs["invocation"] = Invocation.snapshot(ctx)` to
`kwargs["invocation"] = snapshot_invocation(ctx)`. Leave its position — before the
deny check — exactly where it is, and leave the comment explaining why.

- [ ] **Step 5: Repoint the two test files**

In `tests/proposal_assist/test_f3a_invocation_snapshot.py` (line ~156) and
`tests/proposal_assist/test_f3b_stamp.py` (lines 16, 36, 48, 57, 62 **and 82** — line
82 is `assert Invocation.snapshot(ctx).provenance() == Provenance(...)`, and an
implementer following the list rather than the sentence leaves it calling a method the
class no longer has), import
`snapshot_invocation` from `call_manager.proposal_assist.tools.registry` and replace
every `Invocation.snapshot(ctx)` with `snapshot_invocation(ctx)`. Do not change what
any of those tests assert — they pin F3a and F3b behaviour that this task must not
alter.

- [ ] **Step 6: Run the tests**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d1_spine.py tests/proposal_assist/test_f3a_invocation_snapshot.py tests/proposal_assist/test_f3b_stamp.py tests/proposal_assist/test_f3b_ports_boundary.py -q > /tmp/f3d1-3.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d1-3.txt
```
Expected: `PYTEST_EXIT=0`. The ports-boundary file is again the proof that the move
did not drag a non-stdlib import into `ports/`.

- [ ] **Step 7: Commit**

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/ports/invocation.py src/call_manager/proposal_assist/tools/registry.py tests/proposal_assist/test_f3d1_spine.py tests/proposal_assist/test_f3a_invocation_snapshot.py tests/proposal_assist/test_f3b_stamp.py
git commit -m "feat(ports): Invocation moves to ports so a converted tool can name it (F3d1 Task 3)"
```

- [ ] **Step 8: Prove the boundary test would catch a bad move**

In `src/call_manager/proposal_assist/ports/invocation.py`, add
`from call_manager.db.models import Workspace as Workspace` at the top.

Do NOT use `from ...tools.registry import ToolContext` for this. After Step 4 the
registry imports `ports.invocation`, so that mutation is a CIRCULAR import: the run
dies at `PYTEST_EXIT=4` with `ImportError while loading conftest ... cannot import
name 'ToolContext' from partially initialized module`, and the boundary test never
executes at all. A collection error is not a demonstration.

Run:
```bash
uv run pytest tests/proposal_assist/test_f3b_ports_boundary.py -q > /tmp/f3d1-3m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/f3d1-3m.txt
```
Expected: FAIL on `test_importing_the_ports_package_pulls_in_no_sqlalchemy` —
`AssertionError: the ports package reached the database layer: ['sqlalchemy', ...]`.

Know what this does and does not prove. That test imports every ports module in a
subprocess and greps `sys.modules` for `{'sqlalchemy','psycopg','alembic'}`; its
sibling flags source-level names containing `port_impl` or starting `sqlalchemy`/
`psycopg`. A ports module importing a stdlib-clean project module — say
`proposal_assist.paths` — passes both. The guard catches the coupling that MATTERS
(the database), not every coupling. Do not write a docstring claiming otherwise.

```bash
git checkout src/call_manager/proposal_assist/ports/invocation.py
git diff --exit-code HEAD -- src/call_manager/proposal_assist/ports/invocation.py
```

---

### Task 4: Every tool surface gets a live `Ports` bundle

`build_ports()` has existed since F3b and nothing calls it. The five adapter factories
each already hold the `session_factory` it needs, so each gains one argument.

`ToolContext.ports` defaults to `None` — GC-5 requires the two inert constructions to keep
building without one, and every hand-rolled `ToolContext` in the test suite (47 sites
across 31 files) must keep working untouched in this slice.

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/registry.py` (`ToolContext`)
- Modify: `src/call_manager/proposal_assist/coherence/agent.py:43`
- Modify: `src/call_manager/proposal_assist/evaluator/agent.py:43`
- Modify: `src/call_manager/proposal_assist/review/agent.py:38`
- Modify: `src/call_manager/proposal_assist/uncited_claims/agent.py:38`
- Modify: `src/call_manager/proposal_assist/drafting/factory.py:92`
- Modify: `tests/proposal_assist/test_f3d1_spine.py`

**Interfaces:**
- Consumes: `build_ports(session_factory) -> Ports` (F3b, `port_impl/__init__.py`).
- Produces: `ToolContext.ports: Ports | None`, populated by all five factories.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_f3d1_spine.py`:

```python
#: Every factory that builds a tool surface, and the builder-specific kwargs it needs.
#: All five already hold a session_factory, which is the only thing build_ports wants.
#: Parametrised for the same reason the F3a construction table is: a per-factory
#: omission is SILENT, because ToolContext.ports defaults to None.
_TOOL_SURFACE_FACTORIES = [
    pytest.param(
        "call_manager.proposal_assist.evaluator.agent",
        "build_evaluator_adapter",
        lambda: {
            "requested_by": 1,
            "job_id": uuid.uuid4(),
            "draft_section_count": 0,
            "spec_section_count": 0,
            "rubric_criteria": [],
        },
        id="evaluator",
    ),
    pytest.param(
        "call_manager.proposal_assist.coherence.agent",
        "build_keeper_adapter",
        lambda: {"requested_by": 1, "job_id": uuid.uuid4(), "locked": False},
        id="coherence_keeper",
    ),
    pytest.param(
        "call_manager.proposal_assist.review.agent",
        "build_review_adapter",
        lambda: {
            "review_run_id": uuid.uuid4(),
            "item_id": uuid.uuid4(),
            "kind": "persona",
        },
        id="review",
    ),
    pytest.param(
        "call_manager.proposal_assist.uncited_claims.agent",
        "build_uncited_claims_adapter",
        lambda: {"requested_by": 1, "job_id": uuid.uuid4(), "draft_section_count": 0},
        id="uncited_claims",
    ),
]


@pytest.mark.parametrize("module_path, builder_name, extra_kwargs", _TOOL_SURFACE_FACTORIES)
def test_every_one_shot_factory_injects_a_working_ports_bundle(
    tmp_path, monkeypatch, module_path, builder_name, extra_kwargs
):
    """A capability bundle, not module globals: every tool surface is handed one at
    construction. isinstance against the runtime_checkable Protocols is what proves
    the object is usable rather than merely present."""
    mod = importlib.import_module(module_path)
    builder = getattr(mod, builder_name)

    captured: dict = {}

    def _capture(ctx):
        captured["ctx"] = ctx
        return object()

    monkeypatch.setattr(mod, "build_proposal_assist_mcp_server", _capture)

    class _FakeSDK:
        def __init__(self, *, tools_factory=None, **kwargs):
            tools_factory(tmp_path, uuid.uuid4())

    monkeypatch.setattr(mod, "ClaudeAgentSDKAdapter", _FakeSDK, raising=False)
    kwargs = dict(extra_kwargs())
    if "_sdk_factory" in inspect.signature(builder).parameters:
        kwargs["_sdk_factory"] = _FakeSDK

    builder(
        settings=SimpleNamespace(proposal_assist_llm_model="m", workspaces_dir=tmp_path),
        session_factory=lambda: None,
        workspace_id=uuid.uuid4(),
        lifecycle_generation=3,
        **kwargs,
    )

    ports = captured["ctx"].ports
    assert ports is not None, f"{builder_name} built a tool surface with no ports"
    assert isinstance(ports.queries, Queries)
    assert isinstance(ports.effects, Effects)
    assert isinstance(ports.accounting, Accounting)


def test_every_tool_surface_construction_injects_ports():
    """The fifth factory cannot be faked like the four above: drafting/factory.py
    imports ClaudeAgentSDKAdapter and build_proposal_assist_mcp_server LAZILY, inside
    _build_adapter, and reaches build_citation_service on the way — so patching its
    module namespace does not reach them and a stub Settings does not survive the
    trip. An AST check covers all five uniformly instead.

    The two INERT constructions are excluded by the same literal
    tests/fencing/detectors.py keys on: session_factory=None. That is not a
    coincidence to tidy away — it is the one rule both places have to agree on.
    """
    import ast
    import pathlib

    src_root = pathlib.Path(__file__).resolve().parents[2] / "src"
    missing = []
    found = 0
    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name != "ToolContext":
                continue
            kwargs = {k.arg: k.value for k in node.keywords if k.arg}
            inert = isinstance(kwargs.get("session_factory"), ast.Constant) and (
                kwargs["session_factory"].value is None
            )
            if inert:
                continue
            found += 1
            if "ports" not in kwargs:
                missing.append(f"{path.relative_to(src_root)}:{node.lineno}")

    assert found == 5, f"expected 5 live tool-surface constructions, found {found}"
    assert not missing, (
        "tool surfaces built without a ports bundle:\n  " + "\n  ".join(missing)
    )


def test_a_context_without_ports_is_still_constructible():
    """GC-5. collect_mcp_tool_names in tools/registry.py and the shim in
    proposals/accepting.py build a context with session_factory=None, and
    tests/fencing/detectors.py keys on exactly that literal to classify them INERT and
    keep them out of the inventory. They must not gain a ports argument (there is no
    session_factory there to build a bundle from), so the field has to default.
    No line numbers here on purpose: Task 3 already moved the registry site."""
    from call_manager.proposal_assist.tools.registry import ToolContext

    ctx = ToolContext(
        workspace_id=uuid.uuid4(), repo_dir=None, session_factory=None, settings=None
    )
    assert ctx.ports is None
```

and extend the imports at the top of the file:

```python
import importlib
import inspect
import uuid
from types import SimpleNamespace

import pytest

from call_manager.proposal_assist.ports.accounting import Accounting
from call_manager.proposal_assist.ports.effects import Effects
from call_manager.proposal_assist.ports.queries import Queries
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d1_spine.py -q -k "ports_bundle or without_ports" > /tmp/f3d1-4.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d1-4.txt
```
Expected: FAIL — `AttributeError: 'ToolContext' object has no attribute 'ports'`,
**5 failures total**: four parametrisations (the list has four entries; the fifth
factory is covered by the AST test, not this one) plus the defaulting test.

- [ ] **Step 3: Add the field**

In `src/call_manager/proposal_assist/tools/registry.py`, add to `ToolContext`, beside
the other injected collaborators:

```python
    # F3d1: the capability bundle a converted tool holds INSTEAD of a session.
    # Defaults to None because two constructions deliberately have no session to
    # build one from — collect_mcp_tool_names below and proposals/accepting.py's
    # shim, both of which pass session_factory=None. tests/fencing/detectors.py
    # keys on that literal to classify them inert, so they must stay as they are.
    ports: Ports | None = field(default=None)
```

and import the type:

```python
from call_manager.proposal_assist.ports import Ports
```

- [ ] **Step 4: Wire the five factories**

In each of `coherence/agent.py`, `evaluator/agent.py`, `review/agent.py`,
`uncited_claims/agent.py` and `drafting/factory.py`, add one line to the
`ToolContext(...)` construction inside `tools_factory`:

```python
            ports=build_ports(session_factory),
```

and to each module's imports:

```python
from call_manager.proposal_assist.port_impl import build_ports
```

These five modules are NOT under `tools/`, so the import gate never runs on them at
all (`check_composition_root` is scoped to `tools/`, `inventory.py:215`). There is no
constraint here to satisfy and nothing to "fix" — do not go looking for one.

For the record, since a careful reader will ask: `registry.py` itself imports
`ports`, not `port_impl` (Step 3), and it is in `GATE_SKIPS` regardless.

- [ ] **Step 5: Run the tests**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d1_spine.py -q > /tmp/f3d1-4.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d1-4.txt
uv run pytest tests/proposal_assist tests/fencing -q > /tmp/f3d1-4b.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -5 /tmp/f3d1-4b.txt
```
Expected: `PYTEST_EXIT=0` for both. The second is the check that no hand-rolled
`ToolContext` in the suite broke and that the inventory scan still sees exactly what
it saw before.

- [ ] **Step 6: Commit**

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/tools/registry.py src/call_manager/proposal_assist/coherence/agent.py src/call_manager/proposal_assist/evaluator/agent.py src/call_manager/proposal_assist/review/agent.py src/call_manager/proposal_assist/uncited_claims/agent.py src/call_manager/proposal_assist/drafting/factory.py tests/proposal_assist/test_f3d1_spine.py
git commit -m "feat(tools): every tool surface is handed a live Ports bundle (F3d1 Task 4)"
```

- [ ] **Step 7: Prove each factory is covered separately**

Delete `ports=build_ports(session_factory),` from
`src/call_manager/proposal_assist/review/agent.py` only.

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d1_spine.py -q -k ports_bundle > /tmp/f3d1-4m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d1-4m.txt
```
Expected: FAIL on exactly ONE parametrisation — `[review]` — with
`build_review_adapter built a tool surface with no ports`. One failure and not five
is the point: the field defaults to None, so a single missed factory is otherwise
silent, and only a per-factory case can name which one.

```bash
git checkout src/call_manager/proposal_assist/review/agent.py
git diff --exit-code HEAD -- src/call_manager/proposal_assist/review/agent.py
```

---

### Task 5: `_safe_tool` threads the bundle to a converted impl

The dispatch half of the spine. `pass_invocation=True` already exists from F3a and no
production tool sets it; `pass_ports` is its sibling and F3d2 is what starts flipping both.

An impl asking for ports on a context that has none is a wiring bug, not a runtime
condition — it means a tool surface was built by a path that never called `build_ports`.
It must fail loudly at dispatch rather than hand the impl a `None` to dereference.

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/registry.py` (`_safe_tool`)
- Modify: `tests/proposal_assist/test_f3d1_spine.py`

**Interfaces:**
- Consumes: `ToolContext.ports` (Task 4).
- Produces: `_safe_tool(..., pass_ports: bool = False)`, which passes the bundle to the
  impl as the keyword argument `ports`.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_f3d1_spine.py`:

```python
async def test_safe_tool_threads_the_ports_bundle_to_the_impl():
    """The dispatch half of the spine. F3d2 flips pass_ports per tool as sites are
    converted; this proves the wire works before anything rides on it."""
    from call_manager.proposal_assist.port_impl import build_ports
    from call_manager.proposal_assist.tools.registry import ToolContext, _safe_tool

    seen: dict = {}

    async def _impl(ctx, *, ports=None, invocation=None):
        seen["ports"] = ports
        seen["invocation"] = invocation
        return {"ok": True}

    bundle = build_ports(lambda: None)
    ctx = ToolContext(
        workspace_id=uuid.uuid4(),
        repo_dir=None,
        session_factory=None,
        settings=None,
        lifecycle_generation=2,
        ports=bundle,
    )

    await _safe_tool("t", ctx, _impl, ctx, pass_ports=True, pass_invocation=True)

    assert seen["ports"] is bundle
    assert seen["invocation"].lifecycle_generation == 2


async def test_asking_for_ports_a_surface_never_built_fails_loudly():
    """Not a runtime condition — a tool surface built by a path that never called
    build_ports. Handing the impl None would surface later as an AttributeError deep
    inside a tool, attributed to the wrong thing."""
    from call_manager.proposal_assist.tools.registry import ToolContext, _safe_tool

    async def _impl(ctx, *, ports=None):
        return {"ok": True}

    ctx = ToolContext(
        workspace_id=uuid.uuid4(), repo_dir=None, session_factory=None, settings=None
    )

    with pytest.raises(RuntimeError, match="no ports"):
        await _safe_tool("t", ctx, _impl, ctx, pass_ports=True)


async def test_an_unconverted_tool_is_dispatched_exactly_as_before():
    """GC-1. Nothing is converted in this slice, so the default path must be
    untouched: no ports keyword reaches an impl that did not ask for one."""
    from call_manager.proposal_assist.tools.registry import ToolContext, _safe_tool

    seen: dict = {}

    async def _impl(ctx):
        seen["called"] = True
        return {"ok": True}

    ctx = ToolContext(
        workspace_id=uuid.uuid4(), repo_dir=None, session_factory=None, settings=None
    )

    result = await _safe_tool("t", ctx, _impl, ctx)
    assert seen["called"] is True
    # NOT `"error" not in str(result)`: the success envelope is
    # {"content": [...], "is_error": False}, so the substring "error" is always
    # present inside the KEY. That assertion is false by construction, before and
    # after this task.
    assert result["is_error"] is False
    assert "ports" not in seen
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d1_spine.py -q -k "threads_the_ports or fails_loudly or exactly_as_before" > /tmp/f3d1-5.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d1-5.txt
```
Expected: FAIL for the first two, but NOT with a `_safe_tool()` TypeError —
`_safe_tool` already ends in `**kwargs: Any`, so `pass_ports=True` is absorbed and
forwarded to the impl, and the TypeError it raises there is swallowed by the
`except Exception` catch-all and returned as an "internal error" envelope. The real
failures are `KeyError: 'ports'` and `Failed: DID NOT RAISE <class 'RuntimeError'>`.

The third passes already, which is correct: it is the regression guard for the path
this task must not change.

- [ ] **Step 3: Add the parameter**

In `src/call_manager/proposal_assist/tools/registry.py`, add `pass_ports: bool = False`
to `_safe_tool`'s keyword-only parameters beside `pass_invocation`, and immediately
after the invocation snapshot:

```python
    if pass_ports:
        if ctx.ports is None:
            raise RuntimeError(
                f"tool {tool_name} asked for ports on a context that has no ports — "
                "the tool surface was built without build_ports()"
            )
        kwargs["ports"] = ctx.ports
```

Extend the docstring paragraph that explains `pass_invocation` with:

```
    F3d1: pass_ports=True hands the impl the capability bundle as ``ports``. It sits
    beside the snapshot deliberately — both are the converted-tool calling
    convention, and a reader looking for "what does a converted tool receive" should
    find both in one place. No production call site sets it in F3d1; F3d2 flips it
    per tool as effect sites are converted.
```

- [ ] **Step 4: Run the tests**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d1_spine.py -q > /tmp/f3d1-5.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d1-5.txt
uv run pytest tests/proposal_assist/test_mcp_tool_containment.py tests/proposal_assist/tools -q > /tmp/f3d1-5b.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -5 /tmp/f3d1-5b.txt
```
Expected: `PYTEST_EXIT=0` for both.

- [ ] **Step 5: Commit**

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/tools/registry.py tests/proposal_assist/test_f3d1_spine.py
git commit -m "feat(tools): _safe_tool threads the ports bundle to a converted impl (F3d1 Task 5)"
```

- [ ] **Step 6: Prove the loud failure is load-bearing**

In `_safe_tool`, replace the raise with a silent default:

```python
    if pass_ports:
        kwargs["ports"] = ctx.ports
```

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d1_spine.py -q -k fails_loudly > /tmp/f3d1-5m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d1-5m.txt
```
Expected: FAIL with `DID NOT RAISE`. Without the guard the impl receives `None` and
the failure resurfaces much later, inside a tool, attributed to the wrong cause.

```bash
git checkout src/call_manager/proposal_assist/tools/registry.py
git diff --exit-code HEAD -- src/call_manager/proposal_assist/tools/registry.py
```

---

### Task 6: The inventory learns that F3d is three slices

`SLICE_STATUS` has no partial state: `test_each_slice_matches_its_declared_status` fails on
any row that is `fenced: true` while its slice is `pending` (GC-6). F3d2 flips five rows
and F3d3 flips three, so the key must be split now — before anything can flip — or F3d2 is
un-mergeable.

The assignment follows the port surface F3b shipped. The three `submit_*` effects belong to
modules with no relocation debt; the two `propose_*` belong to exactly the two modules that
must be relocated first, and `_write_citekey_cache` moves with its ancestor.

**Files:**
- Modify: `tests/fencing/inventory.py` (`SLICE_STATUS`)
- Modify: `tests/fencing/fence_inventory.json` (the eight `slice` fields)

**Interfaces:**
- Consumes: nothing.
- Produces: `SLICE_STATUS` keys `F3d1`, `F3d2`, `F3d3` in place of `F3d`.

- [ ] **Step 1: Split the status keys**

In `tests/fencing/inventory.py`, replace the single `F3d` line in `SLICE_STATUS` with:

```python
    # Spec §7's F3d, cut into three because it is ~26 production files and a
    # 512-line relocation. SLICE_STATUS has no partial state, so a multi-slice
    # conversion needs one key per slice or the first flipped row goes red.
    "F3d1": "pending",  # the spine: ports/, the bundle, the dispatch — converts nothing
    "F3d2": "pending",  # the submitters and the reads
    "F3d3": "pending",  # the citation core relocation, and the propose_* sites
```

Then, in the SAME file, fix the merge default at `tests/fencing/inventory.py:21`:

```python
_DEFAULT_SLICE = "F3d2"                   # was "F3d", split in F3d1
```

This is not optional bookkeeping. `_DEFAULT_SLICE` is what
`regenerate_inventory` stamps on a newly-detected site, and after Step 1 the key it
names no longer exists. Measured: add one commit site under `tools/`, regenerate, and
the new row reads `"slice": "F3d"` — `pytest tests/fencing` then gives **5 failed**
with `'F3d' has no entry in SLICE_STATUS`. It fails loudly rather than silently, so it
is not a safety hole, but F3d2 and F3d3 both regenerate and both would hit it.

- [ ] **Step 2: Reassign the eight rows**

In `tests/fencing/fence_inventory.json`, change the `slice` field of each F3d row:

| symbol | new slice |
|---|---|
| `submit_review_impl` | `F3d2` |
| `submit_scorecard_impl` | `F3d2` |
| `submit_uncited_claims_impl` | `F3d2` |
| `fetch_reference_impl` | `F3d2` |
| `fetch_reference_impl._refund_fetch_slot` | `F3d2` |
| `propose_citation_impl` | `F3d3` |
| `_propose_artifact_edit_impl` | `F3d3` |
| `_write_citekey_cache` | `F3d3` |

No row is assigned `F3d1`: the spine converts nothing, and an empty slice key is inert
exactly as `F3c`'s is. Leave every other field on every row untouched — in particular
`fenced` stays `false` on all eight (GC-1).

Then close the prose drift this creates. `docs/superpowers/specs/2026-08-18-fence-session-ownership-design.md:399`
still says "split `SLICE_STATUS` into `F3a…F3f`" and assigns the 7 `tool_commit` rows
plus `_write_citekey_cache` to a single **F3d**. Change that passage to name
`F3a…F3f` with F3d cut three ways, and say which of the eight rows go to `F3d2` and
which to `F3d3`. No gate reads the spec, which is exactly why it rots unless a slice
schedules it.

- [ ] **Step 3: Run the fencing suite**

Run:
```bash
uv run pytest tests/fencing -q > /tmp/f3d1-6.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d1-6.txt
```
Expected: `PYTEST_EXIT=0`, 82 passed. `test_every_slice_in_the_inventory_is_known`
is the one that catches a typo in any of the three new keys.

- [ ] **Step 4: Commit**

```bash
uv run ruff check .
git add tests/fencing/inventory.py tests/fencing/fence_inventory.json docs/superpowers/specs/2026-08-18-fence-session-ownership-design.md
git commit -m "test(fencing): F3d is three slices, and SLICE_STATUS has to say so (F3d1 Task 6)"
```

- [ ] **Step 5: Prove the split was necessary**

The obvious mutation — flip one F3d2 row to `fenced: true` — does NOT prove this.
It fails identically against an unsplit `F3d` key (same message, different slice
name), so it is invariant to the thing it is supposed to justify. Run the mutation
that discriminates instead: **the complete direction.**

In `tests/fencing/fence_inventory.json`, set `"fenced": true` on all FIVE `F3d2`
rows, and in `tests/fencing/inventory.py` set `"F3d2": "complete"`.

Run:
```bash
uv run pytest tests/fencing -q -k declared_status > /tmp/f3d1-6m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/f3d1-6m.txt
```
Expected: `PYTEST_EXIT=0`. This is the state F3d2 has to be able to reach, and with
three keys it reaches it cleanly.

Now the counterfactual, which is the actual proof. Restore, then edit
`inventory.py` back to a SINGLE `"F3d": "complete"` key, put `"slice": "F3d"` on all
eight rows, and set `fenced: true` on the five submitter rows only.

Expected: FAIL — the three citation/proposal rows go red as
`fenced=False but slice F3d is complete`. **No ordering of F3d2's work can avoid
that**, because one key cannot describe five converted rows and three unconverted
ones. That is why the split has to happen in THIS slice.

```bash
git checkout tests/fencing/fence_inventory.json tests/fencing/inventory.py
git diff --exit-code HEAD -- tests/fencing/fence_inventory.json tests/fencing/inventory.py
```

---

### Task 7: The eight identities can no longer vanish quietly

The checklist erases itself. `tests/fencing/detectors.py:83-90` records a `tool_commit`
only when `/proposal_assist/tools/` is in the path, and `FencedSiteVanished` is raised
**only for rows already `fenced: true`**. Every F3d row is `fenced: false` today.
(That block is `inventory.py:283-285` on entry and `:288-290` after Task 6 adds five
lines to the same file — so the docstring in Step 1 names neither.)

So when F3d3 moves the citation core out of `tools/`, `propose_citation_impl` and
`_write_citekey_cache` disappear from the scan **with no exception raised**. If every
`tool_commit` eventually leaves `tools/`, the inventory falls from 20 rows to 11 and
`test_each_slice_matches_its_declared_status` passes over an empty set — F3d ends up
`complete`, proved by nothing. That is this project's recurring defect, and it has cost a
slice before.

**Files:**
- Modify: `tests/fencing/inventory.py`
- Modify: `tests/fencing/test_fence_coverage.py`

**Interfaces:**
- Consumes: `SLICE_STATUS` (Task 6).
- Produces: `F3D_IDENTITIES: frozenset[tuple[str, str, str]]` — `(slice, kind, symbol)`.

When you extend `test_fence_coverage.py`'s import from `tests.fencing.inventory`, put
`F3D_IDENTITIES` between `F3A_COLUMN_FILES` and `FENCING_IMPORT_FILES`. Appending it at
the end of the list is `I001` and Step 5's `ruff check .` fails.

- [ ] **Step 1: Write the failing test**

Append to `tests/fencing/test_fence_coverage.py`:

```python
def test_every_f3d_identity_is_still_in_the_inventory():
    """ANTI-VACUITY. The scan only records a tool_commit while the code is under
    tools/ (detectors.py:83-90), and FencedSiteVanished only fires for rows already
    fenced=True — so relocating an UNFENCED site deletes its row silently, and a
    slice can reach `complete` over an empty set.

    This is the ratchet that makes that impossible. If a row here has vanished, the
    fix is not to edit this set: flip the row `fenced: true` BEFORE moving its code,
    which is what makes FencedSiteVanished fire and preserves the row.
    """
    present = {(r["slice"], r["kind"], r["symbol"]) for r in load_inventory()}
    missing = sorted(F3D_IDENTITIES - present)
    assert not missing, (
        "F3d identities that disappeared from the inventory:\n  "
        + "\n  ".join(f"{s} {k} {sym}" for s, k, sym in missing)
        + "\n\nA relocated site is not a converted site. Flip the row fenced=true "
        "in the SAME commit that moves the code, or before it."
    )


def test_a_complete_f3d_slice_has_every_one_of_its_identities_fenced():
    """`complete` must mean every identity the slice owns is proven, not merely that
    nothing red is left in the scan.

    Known and accepted: this is not an independent falsifier. Its red set is a subset
    of the union of test_every_f3d_identity_is_still_in_the_inventory (row relocated)
    and test_each_slice_matches_its_declared_status (row present but unfenced). It is
    kept for the message it produces, which names the identity rather than the row.
    """
    by_id = {(r["slice"], r["kind"], r["symbol"]): r for r in load_inventory()}
    unproven = []
    for identity in sorted(F3D_IDENTITIES):
        slice_name = identity[0]
        if SLICE_STATUS.get(slice_name) != "complete":
            continue
        row = by_id.get(identity)
        if row is None or not row.get("fenced"):
            unproven.append(identity)
    assert not unproven, (
        "slices marked complete with identities that are not fenced:\n  "
        + "\n  ".join(f"{s} {k} {sym}" for s, k, sym in unproven)
    )
```

and extend that file's import from `tests.fencing.inventory` with `F3D_IDENTITIES`.

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
uv run pytest tests/fencing/test_fence_coverage.py -q > /tmp/f3d1-7.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d1-7.txt
```
Expected: collection error — `ImportError: cannot import name 'F3D_IDENTITIES'`.

- [ ] **Step 3: Declare the identities**

Append to `tests/fencing/inventory.py`:

```python
#: The eight identities spec §7's F3d owns, pinned by (slice, kind, symbol).
#:
#: They are written down because the SCAN cannot be trusted to keep them: a
#: tool_commit is only detected while the code sits under proposal_assist/tools/
#: (detectors.py), and FencedSiteVanished only guards rows that are already
#: fenced=True (see the merge above). Relocating an unfenced site therefore drops
#: its row in silence, and every remaining consistency test would keep passing over
#: a smaller and smaller set.
#:
#: Deleting an entry here is not a way to make a test pass. The entry leaves when
#: the identity is genuinely gone from the design — which for F3d means never; each
#: of these is converted, not abolished.
F3D_IDENTITIES: frozenset[tuple[str, str, str]] = frozenset({
    ("F3d2", "tool_commit", "submit_review_impl"),
    ("F3d2", "tool_commit", "submit_scorecard_impl"),
    ("F3d2", "tool_commit", "submit_uncited_claims_impl"),
    ("F3d2", "tool_commit", "fetch_reference_impl"),
    ("F3d2", "tool_commit", "fetch_reference_impl._refund_fetch_slot"),
    ("F3d3", "tool_commit", "propose_citation_impl"),
    ("F3d3", "tool_commit", "_propose_artifact_edit_impl"),
    ("F3d3", "tool_commit", "_write_citekey_cache"),
})
```

- [ ] **Step 4: Run the tests**

Run:
```bash
uv run pytest tests/fencing -q > /tmp/f3d1-7.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d1-7.txt
```
Expected: `PYTEST_EXIT=0`, 84 passed — the 82 that existed plus these two.

- [ ] **Step 5: Commit**

```bash
uv run ruff check .
git add tests/fencing/inventory.py tests/fencing/test_fence_coverage.py
git commit -m "test(fencing): F3d's eight identities cannot vanish quietly (F3d1 Task 7)"
```

- [ ] **Step 6: Prove the guard catches the real failure**

The mutation must reproduce the RELOCATION, not just a missing row. Deleting a row
while its code still sits under `tools/` is a different failure — the scan still finds
the site, so `test_the_inventory_matches_the_tree_exactly` reports it "unlisted" and
`test_inventory_merge.py::test_regeneration_preserves_every_human_slice_assignment`
reports a slice-map mismatch. Three reds, none of them the new guard.

Do this instead. Move `propose_citation_impl` out of `tools/` — rename
`src/call_manager/proposal_assist/tools/citations.py` to
`src/call_manager/proposal_assist/citations/_relocated_probe.py` — then regenerate:

```bash
uv run python -m tests.fencing.regenerate_inventory
uv run pytest tests/fencing -q > /tmp/f3d1-7m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -40 /tmp/f3d1-7m.txt
```
Expected: FAIL on `test_every_f3d_identity_is_still_in_the_inventory`, naming
`F3d3 tool_commit propose_citation_impl`. That is the silent-erasure path: the scan
stops recording the site, `FencedSiteVanished` stays quiet because the row is
`fenced: false`, and regeneration legitimately drops it.

Report every other red you see. Expect one: `test_inventory_merge.py:108`'s
hard-coded `assert len(after) == 20` also catches a vanished row today. That does not
make the guard redundant — it makes it the ONLY thing left once F3d3 updates that
literal, which F3d3 must do. **Write that consequence into the F3d3 plan**: the moment
`== 20` is hand-edited to match a smaller inventory, the count ratchet stops covering
this at all and `F3D_IDENTITIES` is the whole net.

Restore with `git checkout` AND undo the rename, then regenerate again to confirm the
inventory is byte-identical.

```bash
git checkout tests/fencing/fence_inventory.json
git diff --exit-code HEAD -- tests/fencing/fence_inventory.json
```

- [ ] **Step 7: Run the full suite**

Run:
```bash
uv run pytest -n 16 --dist loadfile > /tmp/f3d1-full.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -3 /tmp/f3d1-full.txt
```
Expected: `PYTEST_EXIT=0` and **3737 passed, 1 skipped**. That total is determined:
the master baseline of 3715, plus 20 tests added in Tasks 1-5 (4 + 3 + 4 + 6 + 3, where
Task 4's six are four parametrisations plus two singles) and 2 in Task 7. If your
number differs, work out WHICH term is wrong before touching anything — do not adjust
the expectation to match the run. (If you are executing Tasks 6-7 in isolation, the
20 from Tasks 1-5 are simply not in your tree and the number is **3717**.) **Read the `1 skipped` as the signal, not the
total**: it is the `ANTHROPIC_API_KEY`-gated test, and anything else skipped means
something got disabled.

- [ ] **Step 8: Confirm the slice converted nothing**

Run:
```bash
uv run python -c "
from tests.fencing.inventory import TOOLS_NOT_YET_CONVERTED, load_inventory
print('pins:', len(TOOLS_NOT_YET_CONVERTED))
print('fenced F3d rows:', sum(1 for r in load_inventory() if r['slice'].startswith('F3d') and r['fenced']))
"
```
Expected: `pins: 9` and `fenced F3d rows: 0`. This is GC-1 stated as a command: the
spine is in place and not one tool has moved.

---

## What F3d1 deliberately does NOT do

Named so a reviewer does not read them as gaps:

- **No tool imports a port.** Not one of the nine modules changes. The bundle reaches
  every tool surface and nothing consumes it yet — the same shape F3b shipped, and for the
  same reason: the conversion is reviewed on its own.
- **No exemption is deleted and no row is fenced.** Nine pins in, nine pins out.
- **`tools/citations.py` keeps its own `CITATIONS_FILENAME`.** It is F3d3's module;
  touching it here would be a conversion. Task 1's last test is what stops the two copies
  drifting in the meantime.
- **`WorkspaceDeleted` stays in `drafting/locking.py`.** Only the citation path catches
  it, and that path is F3d3's. Moving it now would be moving something no converted tool
  can yet name.
- **No `agent_view_*` decision.** `tools/citations.py` reads that read-time snapshot and
  `tools/proposal.py` writes it, so the two modules cannot convert independently. F3d3
  owns the choice.
- **The two inert `ToolContext` constructions are untouched** (GC-5) — deliberately, and
  the test in Task 4 pins that they still work without ports.

## Self-review notes

Checked against the survey that produced this cut:

| Requirement | Task |
|---|---|
| `Invocation` must leave `registry.py` before any module can go green | 3 |
| the artifact filenames a converted tool still has to name | 1 |
| `CommitMutexTimeout` + `BUSY_ERROR_MESSAGE` reachable from `ports/` (the F3b carry) | 2 |
| `build_ports` actually called, on every tool surface | 4 |
| the converted-tool calling convention exists and is exercised | 5 |
| `SLICE_STATUS` split before any row can flip (GC-6) | 6 |
| the eight identities survive relocation (the anti-vacuity hazard) | 7 |

Three things this plan asserts that the executor should verify rather than trust: the
expected total of 3737 in Task 7 Step 7 (20 + 2 new tests over a 3715 baseline); that
`tests/fencing` is 82 tests before Task 7 and 84 after; and that exactly five live
`ToolContext` constructions exist in `src/` (Task 4's AST test asserts the count, so a
sixth tool surface appearing later fails loudly rather than going uncovered).
