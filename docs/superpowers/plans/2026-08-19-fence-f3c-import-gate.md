# Fence F3c — The Import Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the tool boundary a CI check instead of a memory — `tools/` may import
only the ports and the standard library, and may not name a resource locator — and
delete the containment machinery that check replaces.

**Architecture:** F3b built ports nothing yet uses. F3c installs the rule that makes
them the only way in. The gate is a **positive allowlist**: a tool module may import
the standard library and `call_manager.proposal_assist.ports`, and nothing else — not
SQLAlchemy, not `db.models`, and not any other project module, because a tool can
reach an effectful helper that exposes no SQLAlchemy in its own file. Every module in
`tools/` violates this today and none is converted until F3d, so the gate ships
**armed against the real package with a named exemption per violating module**, plus a
test that no exemption is stale. F2's analysis machinery — which tried to prove
coverage by inspecting writes — is deleted, because the import check is now the proof.

**Tech Stack:** Python 3.12 (`ast`, `sys.stdlib_module_names`), pytest.

**Spec:** `docs/superpowers/specs/2026-08-18-fence-session-ownership-design.md`
(revision 8) — §4 the gate, §4.1 resource locators, §4.2 what this boundary is and the
inventory demotion, §7 F3c, §9 how the gate is proven.

## Global Constraints

Every task's requirements implicitly include this section.

- **GC-1 — The rule is stated POSITIVELY.** A forbidden-name list is not enough: a
  tool can reach an effectful project helper that exposes no SQLAlchemy in the tool's
  own module. Anything not on the allowlist is a violation, and adding to the
  allowlist is a reviewed diff.
- **GC-2 — The allowlist is exactly two things:** modules whose top-level name is in
  `sys.stdlib_module_names`, and `call_manager.proposal_assist.ports` with its
  submodules. Nothing else. **Every relative import is a violation** — inside `tools/`
  a relative import can only resolve to another `tools/` module, which is how
  `audit.py` reaches `citations._read_proposal_and_ledger` today.
- **GC-3 — Closing imports alone does not close authority (§4.1).** `ToolContext`
  exposes `repo_dir`, so `(ctx.repo_dir / "proposal.md").write_text(...)` passes every
  import check. The gate therefore also forbids naming a resource locator:
  `repo_dir`, `workspaces_dir`, `session_factory`, `settings`.
- **GC-4 — This is architectural hygiene for trusted code, not adversarial
  containment (§4.2).** It makes the wrong thing hard to write and easy to spot in
  review, and makes coverage a CI check rather than a memory. It does not claim a
  process, OS, or database-privilege boundary. Do not write assertions or comments
  that claim otherwise.
- **GC-5 — Fixtures are PARSED, never imported.** The checker reads source text and
  walks its AST. A fixture that had to be importable would drag its own violations
  into the test process and could not express a forbidden import at all.
- **GC-6 — An exemption may not be stale.** Every module named in
  `TOOLS_NOT_YET_CONVERTED` must still fail the gate. A converted module that stays
  on the list turns the gate green for the wrong reason, so removing the entry is
  part of converting it.
- **GC-7 — Additive and test-only.** No file under `src/` changes behaviour in this
  slice. No caller is converted, no tool is rewritten, no resource locator is actually
  removed from `ToolContext` — F3d/F3e/F3f do that, and the gate is what tells them
  when they are done.
- **GC-8 — Test command.** `uv run pytest -n 16 --dist loadfile`. Never `-n auto`
  (resolves to 72 here) and never `--dist loadgroup`. Baseline on master before this
  branch: **3685 passed, 1 skipped**. The `1 skipped` is an `ANTHROPIC_API_KEY`-gated
  test — read it as the signal, not the total.
- **GC-9 — `ruff check .` is the lint gate. `ruff format` is NOT** — do not run it and
  do not reformat files to satisfy it.
- **GC-10 — Mutation testing happens AFTER the task's commit, never before, and the
  restore is verified by a command that FAILS.** `git checkout <path>` on a file the
  task itself created fails with `pathspec … did not match any file(s) known to git`
  — the mutation survives, and piped through anything the non-zero exit is masked. So
  commit the green implementation first, then mutate, then restore with:

  ```bash
  git checkout <path>
  git diff --exit-code HEAD -- <path>
  ```

  Never `git status --porcelain`: it exits 0 whether the tree is clean or filthy, and
  this worktree carries an unrelated untracked note that would make "empty"
  unreachable anyway.

## Two decisions taken before planning

Both were open in the spec and would have produced materially different work.

**The gate ships armed, with a shrinking exemption list.** The alternative — a checker
proven only against synthetic fixtures, pointed at `tools/` in a later slice — leaves
the boundary with no teeth on real code and nothing stopping `tools/` drifting further
away meanwhile. Armed-with-exemptions gives a real gate from day one and makes each
later conversion delete a line. It is the same shape as the existing `SLICE_STATUS`.

**`tools/registry.py` and `tools/__init__.py` are permanently exempt, by name, with
their own narrower rule.** `registry.py` is the composition root: it defines
`ToolContext`, builds the MCP server, and is imported by eleven non-tool modules. It
necessarily names things a tool may not. `tools/__init__.py` re-exports from it.
Moving them out of the package was the alternative; it is cleaner but costs an import
update across eleven modules in a slice that otherwise changes no production
behaviour. The exemption is not unconditional: both files are still forbidden to
import `sqlalchemy` or `call_manager.db.models`, checked separately, so the exemption
cannot become where violations hide.

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `tests/fencing/import_gate.py` | The checker. Allowlist, relative-import rule, locator rule, `check_source`. No pytest, no fixtures — pure functions over source text. |
| `tests/fencing/test_import_gate.py` | The checker's own tests, the fixture-driven negatives, and the armed gate over `tools/`. |
| `tests/fencing/fixtures/` | Source files the checker PARSES. Never imported. Never collected (no `test_` prefix). |
| `tests/proposal_assist/dto_contract.py` | `assert_detached(value, path)` — the recursive ORM-instance walker, promoted out of one test file. |
| `tests/proposal_assist/test_ports_dto_contract.py` | Every `Queries` method returns a detached DTO, with a completeness check against the Protocol. |

**Modified:**

| File | Change |
|---|---|
| `tests/fencing/inventory.py` | Gains `TOOLS_NOT_YET_CONVERTED` and `GATE_SKIPS`. Everything else unchanged. |
| `tests/fencing/test_fence_coverage.py` | `test_every_fenced_entry_really_fences` deleted with its import. |
| `tests/proposal_assist/test_f3b_queries.py` | Uses the shared `assert_detached` instead of its private copy. |
| `docs/superpowers/specs/2026-08-17-lifecycle-fence-design.md` | §9.1 records that the machinery is now deleted; status line and §12 changelog. |

**Deleted:**

| File | Why |
|---|---|
| `tests/fencing/verifiers.py` | §4.2: the analysis machinery the import check replaces. Its detector keys on `.commit()`, which the ports remove — it goes blinder the more converted the tree is. |
| `tests/fencing/test_verifiers.py` | Tests for the above. |

**Deliberately kept:** `detectors.py`, `fence_inventory.json`, `regenerate_inventory.py`,
`test_detectors.py`, `test_inventory_merge.py`, and the classification tests in
`test_fence_coverage.py`. The inventory becomes a **checklist**, and the scan is what
keeps it honest: a converted site stops calling `.commit()`, vanishes from the scan,
and the owning slice must delete its row to go green. A NEW unconverted site still
gets detected and forced onto the list.

---

### Task 1: Delete the containment machinery

F2 shipped a gate asking "is every write inside a fence?" Writes are an open set, so
the enumeration could never be complete, and a review found five reachable bypasses.
Worse, its detector keys on `.commit()` — which the canonical port form removes — so
it gets *greener the more converted the codebase is*. §4.2 records the demotion; this
task completes it.

**Files:**
- Delete: `tests/fencing/verifiers.py`
- Delete: `tests/fencing/test_verifiers.py`
- Modify: `tests/fencing/test_fence_coverage.py` (remove `test_every_fenced_entry_really_fences`)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing. This task only removes.

- [ ] **Step 1: Confirm what depends on the machinery before deleting it**

Run:
```bash
grep -rn "verifiers\|verify(" tests/ src/ --include=*.py
```
Expected: matches in exactly three files — `tests/fencing/verifiers.py` itself,
`tests/fencing/test_verifiers.py`, and one import plus one call inside
`tests/fencing/test_fence_coverage.py::test_every_fenced_entry_really_fences`.

If anything else references it, STOP and report BLOCKED with what you found — the
deletion list in this task is then incomplete.

- [ ] **Step 2: Delete the two files**

```bash
git rm tests/fencing/verifiers.py tests/fencing/test_verifiers.py
```

- [ ] **Step 3: Remove the one consumer**

In `tests/fencing/test_fence_coverage.py`, delete the whole
`def test_every_fenced_entry_really_fences():` function and the
`from tests.fencing.verifiers import verify` import inside it. Leave every other test
in that file untouched — they check the inventory's shape, classification and slice
status, which the checklist still needs.

- [ ] **Step 4: Run what is left**

Run:
```bash
uv run pytest tests/fencing -q > /tmp/f3c1.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3c1.txt
```
Expected: `PYTEST_EXIT=0` and a non-zero collected count. The count drops by the
number of tests in `test_verifiers.py` plus one.

- [ ] **Step 5: Commit**

```bash
uv run ruff check .
git add -A tests/fencing
git commit -m "test(fencing): delete the containment machinery the import check replaces (F3c Task 1)"
```

---

### Task 2: The allowlist checker

**Files:**
- Create: `tests/fencing/import_gate.py`
- Create: `tests/fencing/test_import_gate.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `PORTS_PREFIX = "call_manager.proposal_assist.ports"`
  - `LOCATOR_ATTRS: frozenset[str]` — `{"repo_dir", "workspaces_dir", "session_factory", "settings"}` (used in Task 4)
  - `check_source(source: str, *, path: str) -> list[str]` — returns a violation string per offence, empty when clean.

- [ ] **Step 1: Write the failing test**

Create `tests/fencing/test_import_gate.py`:

```python
"""F3c — the import boundary for tool code.

The rule is POSITIVE: a tool may import the standard library and the ports, and
nothing else. A forbidden-name list would not do — a tool can reach an effectful
project helper that exposes no SQLAlchemy in the tool's own module.
"""

from __future__ import annotations

from tests.fencing.import_gate import check_source


def _check(src: str) -> list[str]:
    return check_source(src, path="tools/example.py")


# ------------------------------------------------------------------- allowed


def test_the_standard_library_is_allowed():
    assert _check("import uuid\nfrom dataclasses import dataclass\n") == []


def test_future_annotations_are_allowed():
    assert _check("from __future__ import annotations\n") == []


def test_the_ports_package_is_allowed():
    src = (
        "from call_manager.proposal_assist.ports import Ports\n"
        "from call_manager.proposal_assist.ports.effects import SubmitScorecard\n"
        "from call_manager.proposal_assist.ports.errors import EffectRejected\n"
    )
    assert _check(src) == []


# ------------------------------------------------------------------ rejected


def test_sqlalchemy_is_rejected():
    assert len(_check("from sqlalchemy import select\n")) == 1


def test_the_models_module_is_rejected():
    assert len(_check("from call_manager.db.models import Workspace\n")) == 1


def test_the_port_implementations_are_rejected():
    """The whole point of the split: a tool may name the contract, never the
    implementation that owns a session."""
    src = "from call_manager.proposal_assist.port_impl.effects import SqlEffects\n"
    assert len(_check(src)) == 1


def test_a_plausible_but_unapproved_internal_helper_is_rejected():
    """THE DECISIVE CASE (spec 9). `editing.sectionizer` contains no SQLAlchemy in
    its own module, so a forbidden-name implementation would wave it through — and
    a tool that can reach arbitrary project helpers can reach effectful ones."""
    src = "from call_manager.proposal_assist.editing.sectionizer import split_sections\n"
    violations = _check(src)
    assert len(violations) == 1
    assert "sectionizer" in violations[0]


def test_a_sibling_tool_module_is_rejected():
    src = "from call_manager.proposal_assist.tools.citations import _read_proposal\n"
    assert len(_check(src)) == 1


def test_plain_import_of_a_project_module_is_rejected():
    assert len(_check("import call_manager.proposal_assist.collab.canonical\n")) == 1


def test_a_third_party_package_is_rejected():
    """Not stdlib, not ports. The allowlist has exactly two entries."""
    assert len(_check("import httpx\n")) == 1


# ----------------------------------------------------- relative imports at all


def test_a_bare_relative_import_is_rejected():
    """`from . import x` inside tools/ can only resolve to another tools/ module."""
    violations = _check("from . import citations\n")
    assert len(violations) == 1
    assert "relative" in violations[0].lower()


def test_a_dotted_relative_import_is_rejected():
    violations = _check("from .citations import helper\n")
    assert len(violations) == 1
    assert "relative" in violations[0].lower()


def test_a_parent_relative_import_is_rejected():
    violations = _check("from ..collab import canonical\n")
    assert len(violations) == 1
    assert "relative" in violations[0].lower()


# ------------------------------------------------------------------- reporting


def test_a_violation_names_the_file_and_what_it_imported():
    src = "from sqlalchemy import select\n"
    violation = check_source(src, path="tools/scorecard.py")[0]
    assert "tools/scorecard.py" in violation
    assert "sqlalchemy" in violation


def test_every_violation_is_reported_not_just_the_first():
    src = (
        "from sqlalchemy import select\n"
        "from call_manager.db.models import Workspace\n"
        "import httpx\n"
    )
    assert len(_check(src)) == 3


def test_an_import_inside_a_function_is_still_checked():
    """`tools/proposal.py` uses a lazy import today to break a module cycle. A
    checker that only walked module-level nodes would miss exactly that."""
    src = (
        "def f():\n"
        "    from call_manager.proposal_assist.proposals.proposing import propose\n"
        "    return propose\n"
    )
    assert len(_check(src)) == 1


def test_a_type_checking_only_import_is_still_checked():
    """`if TYPE_CHECKING:` imports do not execute, but they still name a module
    and still tell the next reader that reaching it is normal."""
    src = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from sqlalchemy.orm import sessionmaker\n"
    )
    assert len(_check(src)) == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
uv run pytest tests/fencing/test_import_gate.py -q > /tmp/f3c2.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3c2.txt
```
Expected: collection error — `ModuleNotFoundError: No module named 'tests.fencing.import_gate'`

- [ ] **Step 3: Write the checker**

Create `tests/fencing/import_gate.py`:

```python
"""The tool-code import boundary (spec 2026-08-18 revision 8, section 4).

    tools/ may import only a positive allowlist: the query, effect and accounting
    ports, their DTOs and commands, and the standard library. Not SQLAlchemy, not
    db.models, not Session, Engine or a session factory — and not any project
    module outside the allowlist.

Stated POSITIVELY on purpose. A forbidden-name list is not enough: a tool can
reach an effectful project helper that exposes no SQLAlchemy in the tool's own
module, and `editing.sectionizer` is a real example. Anything not on the list is
a violation, and adding to the list is a reviewed diff.

This is architectural hygiene for trusted code (section 4.2). It makes the wrong
thing hard to write and easy to spot in review, and makes coverage a CI check
rather than a memory. It is NOT adversarial containment — that would need a
process, OS or database-privilege boundary, and this design does not claim one.

Everything here works on SOURCE TEXT. Nothing is imported: a fixture that had to
be importable would drag its own violations into the test process, and could not
express a forbidden import at all.
"""

from __future__ import annotations

import ast
import sys

#: The one project package a tool may name. Submodules included.
PORTS_PREFIX = "call_manager.proposal_assist.ports"

#: Resource locators (section 4.1). Closing imports alone does not close semantic
#: authority: ToolContext exposes repo_dir, so
#:     (ctx.repo_dir / "proposal.md").write_text(new_text)
#: passes every import check. A tool holds names of operations, not paths.
LOCATOR_ATTRS = frozenset({"repo_dir", "workspaces_dir", "session_factory", "settings"})


def _module_is_allowed(dotted: str) -> bool:
    """The entire allowlist: the standard library, plus the ports package.

    `sys.stdlib_module_names` is the interpreter's own answer to "is this
    stdlib", so the rule cannot drift from the Python version in use.
    """
    if dotted.split(".")[0] in sys.stdlib_module_names:
        return True
    return dotted == PORTS_PREFIX or dotted.startswith(PORTS_PREFIX + ".")


def check_source(source: str, *, path: str) -> list[str]:
    """Every import violation in *source*, one string each. Empty when clean.

    Walks the WHOLE tree, so a lazy import inside a function body and an import
    under `if TYPE_CHECKING:` are both checked. The first is how
    `tools/proposal.py` breaks a module cycle today; the second does not execute
    but still tells the next reader that reaching that module is normal.
    """
    violations: list[str] = []
    tree = ast.parse(source, filename=path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _module_is_allowed(alias.name):
                    violations.append(f"{path}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import inside tools/ can only resolve to another
                # tools/ module — which is how audit.py reaches a private helper
                # in citations.py today. There is no legitimate form of this.
                shown = "." * node.level + (node.module or "")
                violations.append(
                    f"{path}:{node.lineno}: relative import from {shown}"
                )
            elif node.module is not None and not _module_is_allowed(node.module):
                violations.append(f"{path}:{node.lineno}: from {node.module} import ...")
    return violations
```

- [ ] **Step 4: Run the tests**

Run:
```bash
uv run pytest tests/fencing/test_import_gate.py -q > /tmp/f3c2.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3c2.txt
```
Expected: `PYTEST_EXIT=0`, 17 passed.

- [ ] **Step 5: Commit**

```bash
uv run ruff check .
git add tests/fencing/import_gate.py tests/fencing/test_import_gate.py
git commit -m "test(fencing): the positive-allowlist import checker (F3c Task 2)"
```

- [ ] **Step 6: Prove the allowlist is not a blacklist**

Replace `_module_is_allowed`'s body with a forbidden-name implementation — the
shape §9 says must NOT pass:

```python
    return dotted.split(".")[0] not in {"sqlalchemy", "psycopg"}
```

Run:
```bash
uv run pytest tests/fencing/test_import_gate.py -q > /tmp/f3c2m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/f3c2m.txt
```
Expected: FAIL, and `test_a_plausible_but_unapproved_internal_helper_is_rejected`
must be among the failures. That test is the decisive one: a blacklist waves
`editing.sectionizer` through because there is no SQLAlchemy in its own module.

```bash
git checkout tests/fencing/import_gate.py
git diff --exit-code HEAD -- tests/fencing/import_gate.py
```

---

### Task 3: The resource-locator rule, and reading from disk

Closing imports alone does not close semantic authority. `ToolContext` exposes
`repo_dir` (`registry.py:51`), so this passes every import check ever written:

```python
(ctx.repo_dir / "proposal.md").write_text(new_text)
```

And `tools/history.py:25` legitimately imports `subprocess` — stdlib, therefore
allowed — for read-only git (the spec cites line 63, which is the `subprocess.run`
call, not the import). A tool holds names of operations, not paths.

This task also adds the disk-reading entry point Task 4 needs, and two fixture files
that are what a clean tool and a dirty one actually look like.

**Files:**
- Modify: `tests/fencing/import_gate.py`
- Modify: `tests/fencing/test_import_gate.py`
- Create: `tests/fencing/fixtures/clean_tool.py`
- Create: `tests/fencing/fixtures/reaches_a_helper.py`
- Create: `tests/fencing/fixtures/reaches_a_locator.py`

**Interfaces:**
- Consumes: `check_source`, `LOCATOR_ATTRS`, `PORTS_PREFIX` (Task 2).
- Produces:
  - `check_source(source, *, path)` now also reports locator access.
  - `check_file(path: pathlib.Path, *, display: str | None = None) -> list[str]`
  - `REPO: pathlib.Path` and `FIXTURES: pathlib.Path`

**The fixtures are PARSED, never imported.** They live under `tests/fencing/fixtures/`
with no `test_` prefix, so pytest does not collect them, and nothing imports them, so
their deliberate violations never enter the test process. They are written as real,
working-looking modules that USE what they import — a fixture full of `# noqa: F401`
would be lying about what a tool looks like, and `ruff check .` covers this directory.

- [ ] **Step 1: Write the failing tests**

Append to `tests/fencing/test_import_gate.py`:

```python
# --------------------------------------------------------- resource locators


def test_reaching_the_repo_directory_is_rejected():
    """Spec 4.1. The import list is clean here — this is exactly the hole a
    forbidden-import rule alone leaves open."""
    src = 'def f(ctx):\n    (ctx.repo_dir / "proposal.md").write_text("x")\n'
    violations = _check(src)
    assert len(violations) == 1
    assert "repo_dir" in violations[0]


def test_reaching_the_session_factory_is_rejected():
    src = "def f(ctx):\n    with ctx.session_factory() as s:\n        return s\n"
    assert len(_check(src)) == 1


def test_reaching_settings_is_rejected():
    """`ctx.settings` carries workspaces_dir, the ollama url and the embedding
    model — it is an authority handle, not a value."""
    src = "def f(ctx):\n    return ctx.settings.ollama_url\n"
    assert len(_check(src)) == 1


def test_a_chained_locator_reports_each_hop():
    """`ctx.settings.workspaces_dir` names two locators; both are reported so a
    partial fix cannot look complete."""
    src = "def f(ctx):\n    return ctx.settings.workspaces_dir\n"
    assert len(_check(src)) == 2


def test_a_locator_named_on_any_object_is_rejected():
    """The rule is on the NAME, not on `ctx` specifically — rebinding the context
    to another variable must not launder it."""
    src = "def f(anything):\n    return anything.repo_dir\n"
    assert len(_check(src)) == 1


def test_an_unrelated_attribute_is_fine():
    src = "def f(ctx):\n    return ctx.workspace_id\n"
    assert _check(src) == []


# ------------------------------------------------------------ reading a file


def test_the_clean_fixture_passes():
    assert check_file(FIXTURES / "clean_tool.py") == []


def test_the_helper_reaching_fixture_fails():
    violations = check_file(FIXTURES / "reaches_a_helper.py")
    assert len(violations) == 1
    assert "sectionizer" in violations[0]


def test_the_locator_reaching_fixture_fails():
    violations = check_file(FIXTURES / "reaches_a_locator.py")
    assert violations, "the locator fixture stopped violating — fix the fixture"
    assert any("repo_dir" in v for v in violations)


def test_check_file_reports_a_readable_path():
    """A violation must name a path a human can open, not an absolute temp path."""
    violation = check_file(FIXTURES / "reaches_a_helper.py")[0]
    assert violation.startswith("tests/fencing/fixtures/reaches_a_helper.py:")
```

and add `check_file` and `FIXTURES` to the import at the top of the file:

```python
from tests.fencing.import_gate import FIXTURES, check_file, check_source
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
uv run pytest tests/fencing/test_import_gate.py -q > /tmp/f3c3.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3c3.txt
```
Expected: collection error — `ImportError: cannot import name 'check_file'`

- [ ] **Step 3: Add the locator rule and the file entry point**

In `tests/fencing/import_gate.py`, add `pathlib` to the imports and append:

```python
REPO = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
```

Inside `check_source`, in the same `ast.walk` loop, add a branch:

```python
        elif isinstance(node, ast.Attribute) and node.attr in LOCATOR_ATTRS:
            violations.append(f"{path}:{node.lineno}: names the resource locator {node.attr}")
```

The rule is on the attribute NAME, not on `ctx` specifically: binding the context to
another variable first must not launder it. A chained expression such as
`ctx.settings.workspaces_dir` is two `ast.Attribute` nodes and is reported twice, so
fixing one hop cannot look like fixing both.

Then append:

```python
def check_file(path: pathlib.Path, *, display: str | None = None) -> list[str]:
    """Check one source file. *display* overrides the path shown in violations;
    it defaults to the repo-relative path so a failure names something a human
    can open."""
    shown = display or str(path.resolve().relative_to(REPO))
    return check_source(path.read_text(encoding="utf-8"), path=shown)
```

- [ ] **Step 4: Write the three fixtures**

Create `tests/fencing/fixtures/clean_tool.py`:

```python
"""What a converted tool looks like: stdlib and ports, nothing else.

PARSED by the import gate, never imported and never collected. It is here so the
rule has a worked example a reviewer can read, not only a list of prohibitions.
"""

from __future__ import annotations

import uuid

from call_manager.proposal_assist.ports import Ports
from call_manager.proposal_assist.ports.effects import SubmitScorecard
from call_manager.proposal_assist.ports.errors import EffectRejected
from call_manager.proposal_assist.ports.stamp import FenceStamp


def submit(ports: Ports, stamp: FenceStamp, scorecard: dict) -> dict:
    try:
        ports.effects.submit_scorecard(stamp, SubmitScorecard(scorecard=scorecard))
    except EffectRejected as exc:
        return {"error": exc.reason}
    return {"stored": True, "id": str(uuid.uuid4())}
```

Create `tests/fencing/fixtures/reaches_a_helper.py`:

```python
"""THE DECISIVE NEGATIVE (spec 9).

`editing.sectionizer` contains no SQLAlchemy in its own module, so a
forbidden-name implementation of the gate would wave this through. It must be
rejected: a tool that can reach arbitrary project helpers can reach effectful
ones, and this is the shape that proves the allowlist is an allowlist.

PARSED by the import gate, never imported and never collected.
"""

from __future__ import annotations

from call_manager.proposal_assist.editing.sectionizer import split_sections


def count_sections(markdown: str) -> int:
    return len(split_sections(markdown))
```

Create `tests/fencing/fixtures/reaches_a_locator.py`:

```python
"""A tool whose IMPORTS are perfectly clean and which writes to the repo anyway.

Spec 4.1: closing database imports alone does not close semantic authority.
ToolContext exposes repo_dir, so this file passes an import-only gate while
overwriting the artifact a human is editing.

PARSED by the import gate, never imported and never collected.
"""

from __future__ import annotations


def overwrite(ctx, new_text: str) -> None:
    (ctx.repo_dir / "proposal.md").write_text(new_text)
```

- [ ] **Step 5: Run the tests**

Run:
```bash
uv run pytest tests/fencing/test_import_gate.py -q > /tmp/f3c3.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3c3.txt
```
Expected: `PYTEST_EXIT=0`, 27 passed.

Also confirm the fixtures are not collected and do not trip the linter:

```bash
uv run pytest tests/fencing --collect-only -q 2>&1 | grep -c fixtures
uv run ruff check .
```
Expected: `0` collected items from `fixtures`, and `All checks passed!`.

- [ ] **Step 6: Commit**

```bash
git add tests/fencing/import_gate.py tests/fencing/test_import_gate.py tests/fencing/fixtures
git commit -m "test(fencing): the resource-locator rule and the on-disk fixtures (F3c Task 3)"
```

- [ ] **Step 7: Prove the locator rule bites**

Delete the `ast.Attribute` branch you added in Step 3.

Run:
```bash
uv run pytest tests/fencing/test_import_gate.py -q > /tmp/f3c3m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/f3c3m.txt
```
Expected: FAIL, including `test_the_locator_reaching_fixture_fails` — the fixture
with faultless imports goes green, which is the hole this rule exists to close.

```bash
git checkout tests/fencing/import_gate.py
git diff --exit-code HEAD -- tests/fencing/import_gate.py
```

---

### Task 4: Arm the gate over `tools/`

Every module in `tools/` violates the rule today and none is converted until F3d, so
the gate ships **armed with a named exemption per violating module**. Two tests make
that honest in both directions: a module not on the list must be clean, and a module
on the list must still be dirty. Converting a module therefore means deleting its
line, and the gate goes red if you forget.

**The measured starting state** (run against the tree while planning — reproduce it,
do not trust it):

| module | import violations | locators named |
|---|---|---|
| `audit.py` | 7 | `repo_dir`, `session_factory` |
| `citations.py` | 17 | `repo_dir`, `session_factory` |
| `context.py` | 3 | `session_factory` |
| `history.py` | 2 | `repo_dir` |
| `proposal.py` | 7 | `repo_dir`, `session_factory` |
| `references.py` | 10 | `session_factory`, `settings`, `workspaces_dir` |
| `review_submit.py` | 2 | `session_factory` |
| `scorecard.py` | 3 | `session_factory` |
| `uncited_claims.py` | 2 | `session_factory` |

`registry.py` and `__init__.py` are **permanently skipped**: `registry.py` is the
composition root — it defines `ToolContext`, builds the MCP server, imports every tool
module to wire it, and is imported by eleven non-tool modules. It necessarily names
things a tool may not. `__init__.py` re-exports from it. The skip is not
unconditional: both are still forbidden to import `call_manager.db.models` at all, or
`sqlalchemy` outside an `if TYPE_CHECKING:` block, so the exemption cannot become
where violations hide. Verified while planning: `registry.py` imports no `db.models`,
and its only `sqlalchemy` import is `from sqlalchemy.orm import sessionmaker` under
`TYPE_CHECKING`, for the `ToolContext.session_factory` annotation.

**Files:**
- Modify: `tests/fencing/inventory.py`
- Modify: `tests/fencing/import_gate.py`
- Modify: `tests/fencing/test_import_gate.py`

**Interfaces:**
- Consumes: `check_file`, `REPO`, `check_source` (Tasks 2-3).
- Produces:
  - `TOOLS_NOT_YET_CONVERTED: frozenset[str]` and `GATE_SKIPS: frozenset[str]` in `tests/fencing/inventory.py`
  - `TOOLS_DIR: pathlib.Path` and `tool_modules() -> list[pathlib.Path]` in `import_gate.py`
  - `check_composition_root(source: str, *, path: str) -> list[str]` in `import_gate.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/fencing/test_import_gate.py`:

```python
# ----------------------------------------------------- the gate over tools/


def test_every_tool_module_is_either_clean_or_exempt():
    """The armed gate. A module not named in TOOLS_NOT_YET_CONVERTED must pass."""
    problems = []
    for path in tool_modules():
        if path.name in GATE_SKIPS or path.name in TOOLS_NOT_YET_CONVERTED:
            continue
        problems.extend(check_file(path))
    assert not problems, (
        "tool modules violate the import boundary and are not exempt:\n  "
        + "\n  ".join(problems)
        + "\n\nEither fix the module or, if it is genuinely not converted yet, add "
        "it to TOOLS_NOT_YET_CONVERTED in tests/fencing/inventory.py — which is a "
        "reviewed diff, not a formality."
    )


def test_no_exemption_is_stale():
    """The other direction, and the one that makes the list shrink. A converted
    module left on the list turns the gate green for the wrong reason."""
    clean = []
    for name in sorted(TOOLS_NOT_YET_CONVERTED):
        path = TOOLS_DIR / name
        if not check_file(path):
            clean.append(name)
    assert not clean, (
        "these modules now PASS the import boundary but are still exempt:\n  "
        + "\n  ".join(clean)
        + "\n\nDelete them from TOOLS_NOT_YET_CONVERTED — removing the entry is "
        "part of converting the module."
    )


def test_every_exemption_names_a_module_that_exists():
    """A renamed or deleted tool must not leave a permanent free pass behind."""
    on_disk = {p.name for p in tool_modules()}
    missing = sorted(TOOLS_NOT_YET_CONVERTED - on_disk)
    assert not missing, f"exemptions naming modules that no longer exist: {missing}"


def test_the_skips_and_the_exemptions_do_not_overlap():
    """A module is permanently infrastructure or temporarily unconverted, never
    both — otherwise deleting the exemption would silently change nothing."""
    assert not (GATE_SKIPS & TOOLS_NOT_YET_CONVERTED)


def test_the_composition_root_may_not_reach_the_database():
    """The skip is narrow, not unconditional. registry.py wires every tool, so it
    names modules a tool may not — but it must not do database work itself, and a
    TYPE_CHECKING-only annotation import cannot do anything at runtime."""
    problems = []
    for name in sorted(GATE_SKIPS):
        path = TOOLS_DIR / name
        problems.extend(check_composition_root(path.read_text(encoding="utf-8"), path=name))
    assert not problems, "\n  ".join(problems)


def test_the_composition_root_rule_rejects_a_runtime_sqlalchemy_import():
    src = "from sqlalchemy.orm import sessionmaker\n"
    assert len(check_composition_root(src, path="registry.py")) == 1


def test_the_composition_root_rule_allows_a_type_checking_import():
    src = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from sqlalchemy.orm import sessionmaker\n"
    )
    assert check_composition_root(src, path="registry.py") == []


def test_the_composition_root_rule_rejects_models_even_under_type_checking():
    """Naming a model is naming a table. There is no annotation-only excuse for
    it in the module that builds the tool surface."""
    src = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from call_manager.db.models import Workspace\n"
    )
    assert len(check_composition_root(src, path="registry.py")) == 1
```

and extend the imports at the top of the file:

```python
from tests.fencing.import_gate import (
    FIXTURES,
    TOOLS_DIR,
    check_composition_root,
    check_file,
    check_source,
    tool_modules,
)
from tests.fencing.inventory import GATE_SKIPS, TOOLS_NOT_YET_CONVERTED
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
uv run pytest tests/fencing/test_import_gate.py -q > /tmp/f3c4.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3c4.txt
```
Expected: collection error — `ImportError: cannot import name 'TOOLS_DIR'`

- [ ] **Step 3: Add the two lists**

In `tests/fencing/inventory.py`, append:

```python
#: Tool modules that do not yet satisfy the F3c import boundary. Each is a
#: MIGRATION DEBT, not a permission: F3d/F3e/F3f delete its line as they convert
#: it, and `test_no_exemption_is_stale` goes red if a converted module is left
#: here. Adding an entry is a reviewed diff — the cost falls on staying dirty.
TOOLS_NOT_YET_CONVERTED: frozenset[str] = frozenset({
    "audit.py",           # 7 project imports incl. db.models; repo_dir, session_factory
    "citations.py",       # 17 project imports incl. sqlalchemy + db.models; repo_dir, session_factory
    "context.py",         # sqlalchemy + db.models; session_factory
    "history.py",         # sibling tool import; repo_dir (read-only git via subprocess)
    "proposal.py",        # 7 project imports incl. a lazy one; repo_dir, session_factory
    "references.py",      # 10 project imports incl. sqlalchemy + db.models; session_factory, settings, workspaces_dir
    "review_submit.py",   # db.models; session_factory
    "scorecard.py",       # db.models + evaluator.scoring; session_factory
    "uncited_claims.py",  # db.models; session_factory
})

#: PERMANENTLY outside the gate, by name. `registry.py` is the composition root:
#: it defines ToolContext, builds the MCP server, imports every tool module to
#: wire it, and eleven non-tool modules import it. It necessarily names things a
#: tool may not. `__init__.py` re-exports from it.
#:
#: The skip is narrow, not unconditional — `check_composition_root` still forbids
#: both from importing db.models at all, or sqlalchemy outside `if TYPE_CHECKING`,
#: so this cannot become where violations hide.
GATE_SKIPS: frozenset[str] = frozenset({"registry.py", "__init__.py"})
```

- [ ] **Step 4: Add the tools walker and the composition-root rule**

In `tests/fencing/import_gate.py`, append:

```python
TOOLS_DIR = REPO / "src" / "call_manager" / "proposal_assist" / "tools"

#: The composition root may name project modules a tool may not, but it must not
#: do database work. Models are forbidden outright — naming a model is naming a
#: table. SQLAlchemy is forbidden at RUNTIME only: an import that exists solely
#: to annotate a field cannot be used to execute anything.
_MODELS_MODULE = "call_manager.db.models"


def tool_modules() -> list[pathlib.Path]:
    """Every `.py` file directly inside `tools/`, sorted. No subpackages exist
    today; if one appears, it is discovered too."""
    return sorted(p for p in TOOLS_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _type_checking_node_ids(tree: ast.AST) -> set[int]:
    """ids of every node inside an `if TYPE_CHECKING:` body."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        named = (
            (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING")
            or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")
        )
        if named:
            for child in node.body:
                for inner in ast.walk(child):
                    guarded.add(id(inner))
    return guarded


def check_composition_root(source: str, *, path: str) -> list[str]:
    """The narrow rule for GATE_SKIPS. Not the tool rule — these files legitimately
    name every tool module in order to wire them."""
    violations: list[str] = []
    tree = ast.parse(source, filename=path)
    guarded = _type_checking_node_ids(tree)
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            if name == _MODELS_MODULE or name.startswith(_MODELS_MODULE + "."):
                violations.append(f"{path}:{node.lineno}: imports {name}")
            elif name.split(".")[0] == "sqlalchemy" and id(node) not in guarded:
                violations.append(
                    f"{path}:{node.lineno}: imports {name} outside TYPE_CHECKING"
                )
    return violations
```

- [ ] **Step 5: Run the tests**

Run:
```bash
uv run pytest tests/fencing/test_import_gate.py -q > /tmp/f3c4.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3c4.txt
```
Expected: `PYTEST_EXIT=0`, 35 passed.

If `test_every_tool_module_is_either_clean_or_exempt` fails, the exemption list in
Step 3 is wrong for this tree — reproduce the measurement rather than adding whatever
name the failure prints, and record the difference in your report.

- [ ] **Step 6: Commit**

```bash
uv run ruff check .
git add tests/fencing/inventory.py tests/fencing/import_gate.py tests/fencing/test_import_gate.py
git commit -m "test(fencing): arm the import gate over tools/ with shrinking exemptions (F3c Task 4)"
```

- [ ] **Step 7: Prove both directions of the armed gate**

Mutation 1 — delete `"scorecard.py"` from `TOOLS_NOT_YET_CONVERTED`.

Run:
```bash
uv run pytest tests/fencing/test_import_gate.py -q -k either_clean_or_exempt > /tmp/f3c4a.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3c4a.txt
```
Expected: FAIL naming `scorecard.py`'s violations — an unexempted dirty module is caught.

```bash
git checkout tests/fencing/inventory.py
git diff --exit-code HEAD -- tests/fencing/inventory.py
```

Mutation 2 — add `"clean_stub.py"` to `TOOLS_NOT_YET_CONVERTED` and create
`src/call_manager/proposal_assist/tools/clean_stub.py` containing only
`"""Temporary."""` and `import uuid`, then `__all__ = ["uuid"]` so ruff is satisfied.

Run:
```bash
uv run pytest tests/fencing/test_import_gate.py -q -k "stale or exists" > /tmp/f3c4b.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3c4b.txt
```
Expected: FAIL on `test_no_exemption_is_stale` naming `clean_stub.py` — a clean
module cannot hide behind an exemption.

```bash
rm src/call_manager/proposal_assist/tools/clean_stub.py
git checkout tests/fencing/inventory.py
git diff --exit-code HEAD -- tests/fencing/inventory.py
[ ! -e src/call_manager/proposal_assist/tools/clean_stub.py ]
```
The last command EXITS NON-ZERO if the stub survived. Do not substitute
`git status --short` here — it exits 0 whether or not the file is there, which is
the same trap GC-10 names.

---

### Task 5: The DTO contract test

§4.2 keeps one specific claim honest. Revision 5 of the spec said "a tool that cannot
name a table cannot write to one." That is false in one way worth keeping in view: a
query port that accidentally returns an **attached** ORM instance hands the tool
`object_session(instance)` — the full `Session` — without the tool importing anything.
The import gate cannot see that. So the DTO rule needs its own contract test:
**every query result is recursively free of ORM instances.**

F3b proved this for the two methods it shipped, with a walker private to one test
file. This task promotes the walker, drives **every** method on the `Queries` Protocol
through it, and fails if a method is added without a case — so the contract cannot be
outgrown silently.

**Files:**
- Create: `tests/proposal_assist/dto_contract.py`
- Create: `tests/proposal_assist/test_ports_dto_contract.py`
- Modify: `tests/proposal_assist/test_f3b_queries.py`

**Interfaces:**
- Consumes: `SqlQueries`, `Queries`, `ReferenceView`, `ReferenceChunkView` (F3b);
  `seed_workspace` from `tests/proposal_assist/f3b_seeds.py`.
- Produces: `assert_detached(value: object, path: str) -> None` in
  `tests/proposal_assist/dto_contract.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_ports_dto_contract.py`:

```python
"""F3c — the DTO contract (spec 4.2).

The import gate cannot see this one. A query port that returns an ATTACHED ORM
instance hands the tool object_session(instance) — the full Session — without the
tool importing anything at all. So detachment gets its own test, over every
method the Queries Protocol declares.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.port_impl.queries import SqlQueries
from call_manager.proposal_assist.ports.queries import Queries
from tests.proposal_assist.dto_contract import assert_detached
from tests.proposal_assist.f3b_seeds import seed_workspace


def _seed(session, ws_id) -> uuid.UUID:
    ref = Reference(
        workspace_id=ws_id,
        kind="paper",
        source_type="uploaded",
        filename="p.pdf",
        storage_path=f"{ws_id}/p.pdf",
        mime_type="application/pdf",
        title="A Paper",
        citekey="doe2020",
        tombstoned_at=None,
    )
    session.add(ref)
    session.flush()
    session.add(
        ReferenceChunk(
            reference_id=ref.id,
            workspace_id=ws_id,
            chunk_index=0,
            section_path="Intro",
            text="the chunk text",
            embedding=[0.0] * 1024,
            embedding_model="bge-m3",
        )
    )
    session.commit()
    return ref.id


#: One case per Queries method: a callable taking (queries, workspace_id,
#: reference_id) and returning the result to inspect. Every case must return a
#: NON-None result, or it proves nothing — a method returning None trivially
#: carries no session.
CASES = {
    "get_reference": lambda q, ws, ref: q.get_reference(ws, ref),
    "get_reference_chunk": lambda q, ws, ref: q.get_reference_chunk(ws, ref, 0),
}


def test_every_query_method_has_a_contract_case():
    """Completeness. A new query port added without a case here would ship with
    its detachment unproven, and nothing else in the tree would notice."""
    declared = {n for n in vars(Queries) if not n.startswith("_")}
    assert set(CASES) == declared, (
        f"uncovered: {sorted(declared - set(CASES))}; "
        f"stale: {sorted(set(CASES) - declared)}"
    )


@pytest.mark.parametrize("name", sorted(CASES))
def test_every_query_result_is_recursively_free_of_orm_instances(
    name, pg_session_factory
):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=0)
        ref_id = _seed(s, ws_id)

    result = CASES[name](SqlQueries(pg_session_factory), ws_id, ref_id)
    assert result is not None, f"{name} returned None — the case proves nothing"
    assert_detached(result, name)


def test_the_walker_finds_an_orm_instance_hidden_in_a_collection(pg_session_factory):
    """The walker's own proof. A flat DTO of scalars would pass any checker; what
    matters is that a collection field cannot smuggle one through."""
    import dataclasses

    @dataclasses.dataclass(frozen=True)
    class Carrier:
        items: list

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=0)
        _seed(s, ws_id)
        attached = s.query(Reference).first()
        with pytest.raises(AssertionError) as e:
            assert_detached(Carrier(items=[attached]), "carrier")
    assert "carrier.items[0]" in str(e.value)


def test_the_walker_terminates_on_a_cycle():
    """A self-referential structure must fail loudly rather than exhaust the
    stack. Not reachable through today's flat DTOs; the walker is the template
    every future query result goes through."""
    node: dict = {"name": "a"}
    node["self"] = node
    with pytest.raises(RecursionError):
        assert_detached(node, "node")
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
uv run pytest tests/proposal_assist/test_ports_dto_contract.py -q > /tmp/f3c5.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3c5.txt
```
Expected: collection error — `ModuleNotFoundError: No module named 'tests.proposal_assist.dto_contract'`

- [ ] **Step 3: Promote the walker**

Create `tests/proposal_assist/dto_contract.py`, moving the body of
`_assert_detached` out of `tests/proposal_assist/test_f3b_queries.py` verbatim,
renamed and with the module docstring below:

```python
"""The DTO contract: a query result carries data, never a live Session.

Spec 2026-08-18 revision 8 section 4.2. An ATTACHED ORM instance carries
object_session(instance), which is the full Session again — the exact leak that
defeated the forwarding-proxy design. The import gate cannot see this, because
the tool imports nothing to receive it.

Shared by test_f3b_queries.py and test_ports_dto_contract.py so the two cannot
drift; every future query port goes through the same walker.
"""

from __future__ import annotations

import dataclasses

from sqlalchemy import inspect as sa_inspect


def assert_detached(value: object, path: str) -> None:
    """Recursively assert that no ORM instance hides anywhere inside `value`.

    Walks nested dataclasses, lists, tuples, sets and dicts (keys AND values) —
    not just a DTO's own top-level fields, because `dataclasses.asdict` leaves a
    collection of raw ORM instances untouched and a top-level-only check would
    wave it through.

    A cycle raises RecursionError rather than hanging. Deliberately not guarded
    with a visited set: a query DTO containing a cycle is itself a defect, and a
    loud stack overflow says so more clearly than a silent skip.
    """
    assert sa_inspect(value, raiseerr=False) is None, (
        f"{path}: {type(value).__name__} is an ORM-mapped object"
    )
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        for field in dataclasses.fields(value):
            assert_detached(getattr(value, field.name), f"{path}.{field.name}")
    elif isinstance(value, dict):
        for key, item in value.items():
            assert_detached(key, f"{path}.key({key!r})")
            assert_detached(item, f"{path}[{key!r}]")
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            assert_detached(item, f"{path}[{index}]")
```

- [ ] **Step 4: Point the F3b test at the shared walker**

In `tests/proposal_assist/test_f3b_queries.py`, delete the private
`_assert_detached` function, add
`from tests.proposal_assist.dto_contract import assert_detached` to the imports, and
change the two call sites in `test_no_query_result_carries_a_live_session` to call
`assert_detached`. Remove the now-unused `dataclasses` / `sa_inspect` imports if
`ruff` reports them.

- [ ] **Step 5: Run both files**

Run:
```bash
uv run pytest tests/proposal_assist/test_ports_dto_contract.py tests/proposal_assist/test_f3b_queries.py -q > /tmp/f3c5.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3c5.txt
```
Expected: `PYTEST_EXIT=0`, 13 passed.

- [ ] **Step 6: Commit**

```bash
uv run ruff check .
git add tests/proposal_assist/dto_contract.py tests/proposal_assist/test_ports_dto_contract.py tests/proposal_assist/test_f3b_queries.py
git commit -m "test(ports): the DTO contract over every query method (F3c Task 5)"
```

- [ ] **Step 7: Prove the contract bites**

Mutation 1 — in `src/call_manager/proposal_assist/port_impl/queries.py`, make
`get_reference` return the ORM entity: change its `select(...)` to `select(Reference)`
and return `row[0]`.

Run:
```bash
uv run pytest tests/proposal_assist/test_ports_dto_contract.py -q > /tmp/f3c5a.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3c5a.txt
```
Expected: FAIL on the `get_reference` parametrisation with
`Reference is an ORM-mapped object`.

```bash
git checkout src/call_manager/proposal_assist/port_impl/queries.py
git diff --exit-code HEAD -- src/call_manager/proposal_assist/port_impl/queries.py
```

Mutation 2 — delete the `"get_reference_chunk"` entry from `CASES`.

Run:
```bash
uv run pytest tests/proposal_assist/test_ports_dto_contract.py -q -k has_a_contract_case > /tmp/f3c5b.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3c5b.txt
```
Expected: FAIL with `uncovered: ['get_reference_chunk']`.

```bash
git checkout tests/proposal_assist/test_ports_dto_contract.py
git diff --exit-code HEAD -- tests/proposal_assist/test_ports_dto_contract.py
```

---

### Task 6: Record the demotion, and run the suite

**Files:**
- Modify: `tests/fencing/inventory.py` (`SLICE_STATUS`)
- Modify: `docs/superpowers/specs/2026-08-17-lifecycle-fence-design.md`

- [ ] **Step 1: Mark the slice complete**

In `tests/fencing/inventory.py`:

```python
    "F3c": "complete",  # the import gate — no inventory row of its own
```

The inventory carries no `F3c` row: the sites it will serve are sliced `F3d` and
`F3e` and stay `fenced: false` until those slices convert them, so the status/flag
consistency check is inert for `F3c`. The entry records that the slice is done.

- [ ] **Step 2: Record the machinery deletion in §9.1**

`docs/superpowers/specs/2026-08-17-lifecycle-fence-design.md` §9.1 records that F2 was
demoted from a coverage gate to an inventory. Append one paragraph to that section
saying the demotion is now complete:

```markdown
**Completed in F3c.** `verifiers.py` and its tests are deleted; the inventory is a
migration checklist and nothing more. The proof is the import boundary
(`tests/fencing/import_gate.py`): `tools/` may import only the standard library and
`call_manager.proposal_assist.ports`, and may not name a resource locator. Every tool
module is exempted by name until its slice converts it, and an exemption that stops
being true is a red test — so the list can only shrink. What the boundary cannot see,
the DTO contract test covers: a query port returning an attached ORM instance hands
back `object_session(instance)` without the tool importing anything.
```

- [ ] **Step 3: Bump the spec revision**

The status line currently reads `revision 6, 2026-08-19` (F3b's edit). Change it to
`revision 7, 2026-08-19` and extend the same sentence with:

```markdown
revision 7 records that F2's containment machinery is deleted and the import
boundary replaces it.
```

Add the matching entry at the **top** of §12, immediately under the
`## 12. Changelog` heading, in that section's existing prose form
(`**Revision N (date).**`) — do not introduce `###` headings:

```markdown
**Revision 7 (2026-08-19).** §9.1 completed: F2's analysis machinery is deleted and
the F3c import boundary replaces it. The old detector keyed on `.commit()`, which the
port form removes, so it went blinder as conversion proceeded; an import allowlist
does the opposite. `tools/` may import only the standard library and the ports, may
not name a resource locator, and every unconverted module is exempted by name until
its slice deletes the line.
```

- [ ] **Step 4: Run the full suite**

Run:
```bash
uv run pytest -n 16 --dist loadfile > /tmp/f3c-full.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -3 /tmp/f3c-full.txt
```
Expected: `PYTEST_EXIT=0`. The total will be the master baseline of **3685** minus the
tests deleted in Task 1, plus the tests added in Tasks 2-5. **Read the `1 skipped` as
the signal, not the total** — it is an `ANTHROPIC_API_KEY`-gated test, and anything
else skipped means something got disabled. Report the result line verbatim.

- [ ] **Step 5: Commit**

```bash
uv run ruff check .
git add tests/fencing/inventory.py docs/superpowers/specs/2026-08-17-lifecycle-fence-design.md
git commit -m "docs(fencing): the import boundary replaces the containment machinery (F3c Task 6)"
```

---

## What F3c deliberately does NOT do

Named so a reviewer does not read them as gaps:

- **No tool is converted.** All nine remain exempt. F3d converts them as a
  module-level dependency migration — moving only the inventory identities would leave
  `tools/citations.py` importing SQLAlchemy and the boundary still red.
- **No resource locator is removed from `ToolContext`.** `repo_dir`,
  `workspaces_dir`, `session_factory` and `settings` all stay on the dataclass, and
  `registry.py` still builds them. The gate forbids tools from *naming* them, which is
  what tells F3d/F3e/F3f when the removal is safe. Removing the fields is F3f.
- **No git or filesystem read moves behind a query port.** §4.1 requires it and
  `tools/history.py` is the case that needs it — `subprocess` is stdlib, so the import
  gate allows it while the locator rule rejects the `ctx.repo_dir` it operates on.
  That tension is deliberate and is F3d's problem to resolve.
- **`registry.py` is not moved out of `tools/`.** Considered and rejected during
  planning: it would cost an import update across eleven modules in a slice that
  otherwise changes no production behaviour. It is skipped by name, under a narrower
  rule of its own.
- **The gate is not adversarial containment (§4.2).** It makes the wrong thing hard to
  write and easy to spot in review. Real containment would need a process, OS or
  database-privilege boundary, and this design does not claim one.

## Self-review notes

Checked against spec §4, §4.1, §4.2, §7's F3c paragraph and §9:

| Spec requirement | Task |
|---|---|
| the positive allowlist of §4 | 2 |
| negative fixtures | 2 (source-level), 3 (on-disk) |
| the decisive fixture: a plausible but unapproved internal helper | 2 and 3 |
| resource locators leave the tool-facing surface (§4.1) | 3 — the *rule*; the field removal is F3f, stated above |
| the recursive DTO contract test (§4.2) | 5 |
| `verifiers.py` and the containment machinery deleted | 1 |
| the inventory reduced to a checklist with `SLICE_STATUS` | 1 and 6 — the scan is retained deliberately, see File Structure |
| proven against fixtures before it has live data (§7) | 2, 3 — and armed over `tools/` in 4, which the spec leaves open and the plan decides |

Two items are answered rather than implemented, both recorded above: the gate is armed
with exemptions rather than left unarmed (a decision taken before planning), and
resource-locator *fields* stay on `ToolContext` until F3f while the *rule* against
naming them lands here.
