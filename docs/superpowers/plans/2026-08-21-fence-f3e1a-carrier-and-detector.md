# Fence F3e1a — the carrier, the detector, the inventory

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give services a carrier for the generation, discover every git-committing site
in the repository, and arm the gates — **converting nothing**.

**Nine tasks.** Task 5 is the load-bearing one: a single order- and path-aware predicate
that both the `fenced` measurement and the no-`_live` gate derive from. An earlier draft
implemented those separately and they disagreed immediately — spec §3.7's "one predicate
cannot disagree with itself" is the reason this task exists.

**Architecture:** `F3e1a` is a spine slice, like `F3b`, `F3c` and `F3d1`. It adds
`stamp_for_workspace` (the service-side twin of `Invocation.stamp()`) with **zero
production callers**, a repo-wide `git_commit` detector kind whose `fenced` value is
**measured** rather than stored, and three static gates. No production code path changes
behaviour. `F3e1b` converts.

**Spec:** `docs/superpowers/specs/2026-08-21-fence-f3e1-git-mechanism-design.md`
(revision 5, `af7b1909`). Read its **§7.0** first — it records why revisions 1–4 kept
failing on the same section, and this plan exists in the shape it does because of it.

**Tech stack:** Python 3.12, `uv`, `pytest` (+ testcontainers Postgres), `ruff`.

---

## Global Constraints

- **Verified baseline: `3803 passed, 1 skipped`** on master at `af7b1909`.
  `uv run pytest -n 16 --dist loadfile -q`. Never `-n auto` (resolves to **72** here),
  never `--dist loadgroup`. `ruff check .` is a gate; **`ruff format` is not — do not run it.**
- **`0 skipped` means a live Anthropic API call ran** (a real-SDK test gated on
  `ANTHROPIC_API_KEY`). Report it if you see it.
- **CONVERTS NOTHING.** No file under `src/` changes behaviour. The only `src/` edit in
  this whole plan is Task 1's new function, which nothing calls. If you find yourself
  editing `collab/kernel.py`, `checkpoint.py`, `human_cite.py`, `editing/save.py`,
  `citations/core.py` or any route, **stop** — that is `F3e1b`.
- **Retires no exemption pin.** Pins stay at **5**. Four are held by `repo_dir`
  (`audit.py`, `citations.py`, `history.py`, `proposal.py`); `references.py` is held by
  `session_factory`. Both retire in `F3f`. If you can delete a pin, stop.
- **Key every stop-the-run judgement on FAILED, never on ERROR.** `tests/conftest.py:135`
  is an autouse teardown fixture that imports the app for *every* test in the repo, so a
  broken `src/` import shows as mass teardown **ERRORS with zero FAILED**. That is
  collection damage, not a gate failing.
- **No assertion may be satisfiable by a comment.** This project has shipped that mistake
  four times. Every gate assertion keys on parsed AST call nodes or on behaviour — never on
  `inspect.getsource(...)` containing a string. If you write a source-text assertion, ask
  whether a comment beside the code would satisfy it; if yes, rewrite it.
- **Every expected total is DERIVED except the 3803 baseline.** Per task, the tests this
  plan actually writes:

  | Task | New tests | Running total |
  |---|---|---|
  | baseline | — | **3803** |
  | 1 carrier | 6 | 3809 |
  | 2 stamp gate | 7 | 3816 |
  | 3 machinery | **0** (renames and edits existing tests) | 3816 |
  | 4 slice split | 1 | 3817 |
  | 5 shared checker | 12 | 3829 |
  | 6 detection | 5 | 3834 |
  | 7 sink guards | 7 (+1 stale-pin) | 3841 |
  | 8 gate over the tree | 4 | 3845 |
  | 9 confirm | 5 | **3850** |

  If your run disagrees,
  do **not** adjust a number and do **not** delete a test to reach one. Work out which term
  is wrong, record it under PLAN DEFECTS, and report both your arithmetic and the run.
- **Restore every mutation from a COPY, not `git checkout`** — it destroys uncommitted work
  in the same file. Re-run the suite green after restoring, before committing.
- Do not run `uv sync` bare in a worktree; it resolves Python 3.14. Use
  `uv sync --python 3.12` if you must sync at all.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/call_manager/proposal_assist/ports/stamp.py` | **modify** — add `stamp_for_workspace` | 1 |
| `tests/proposal_assist/ports/test_stamp_for_workspace.py` | **create** — the carrier's tests | 1 |
| `tests/fencing/stamp_gate.py` | **create** — AST: a mechanism-2 generation comes from a stamp | 2 |
| `tests/fencing/git_fence.py` | **create** — THE shared conservative predicate + `GIT_SINKS` | 5 |
| `tests/fencing/test_git_fence.py` | **create** — its twelve shapes and five mutations | 5 |
| **`tests/fencing/test_detectors.py`** | **modify** — whole-set equality on `find_fenced_tool_commits` (line 316) | 3 |
| `tests/fencing/test_stamp_gate.py` | **create** — its fixtures and mutations | 2 |
| `tests/fencing/detectors.py` | **modify** — `KIND_GIT_COMMIT`; discovery and measurement both DELEGATE to `git_fence` | 3, 6 |
| `tests/fencing/inventory.py` | **modify** — categories, slice defaults, `MEASURED_KINDS`, triple `fenced_ids`, `SLICE_STATUS`, `F3D_IDENTITIES` | 3, 4, 6 |
| `tests/fencing/fence_inventory.json` | **modify** — 2 slice strings (T4), then 20 → 29 rows (T6) | 4, 6 |
| `tests/fencing/test_fence_coverage.py` | **modify** — narrow one test, rename one, add the measurement gate | 3, 4, 6 |
| `tests/fencing/test_inventory_merge.py` | **modify** — `fenced_ids` shape at 48 AND 79, `== 20` → `== 29` | 3, 6 |
| `tests/proposal_assist/test_f3d3a_relocation.py` | **modify** — `fenced_ids` shape only | 3 |
| `tests/fencing/sink_guard.py` / `test_sink_guard.py` | **create** — argv / execution-surface / `shell=True` guards | 7 |
| `tests/fencing/live_mutex_gate.py` | **create** — runs `git_fence` over `src/`; pins + `ENCLOSING_ACQUISITIONS` | 8 |
| `tests/fencing/test_live_mutex_gate.py` | **create** — its pin list, stale-pin gate and mutations | 8 |
| `tests/fencing/test_f3e1a_slice.py` | **create** — the slice's own claims | 9 |
| `docs/superpowers/specs/2026-08-18-fence-session-ownership-design.md` | **modify** — the §7 amendment | 4 |

---

## Task 1: The carrier

Gives a service the same thing `Invocation.stamp()` gives a tool. **Nothing calls it** —
that is the point, exactly as `F3b` shipped ports with no caller.

**Files:**
- Modify: `src/call_manager/proposal_assist/ports/stamp.py`
- Test: `tests/proposal_assist/ports/test_stamp_for_workspace.py` (create)

**Interfaces:**
- Consumes: `FenceStamp` (same module), `UnfencedInvocation` from `ports/errors.py`.
- Produces: `stamp_for_workspace(workspace) -> FenceStamp`. `F3e1b` calls it from every
  route, orchestrator and maintenance pre-screen.

**Why `stamp.py` and not a new module:** it is stdlib-only today (`uuid`, `dataclasses`)
and must stay importable from a route, the kernel and a maintenance job. A `Workspace`
type annotation would import `db.models` and break that, so the parameter is **duck-typed
and documented**, not annotated with the ORM class. `TYPE_CHECKING` is not used either —
it would still put `db.models` in the module's import graph for type-checkers reading the
ports package, and the point of the ports package is that it names nothing from the app.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/ports/test_stamp_for_workspace.py`:

```python
"""Fence F3e1a — the service-side twin of Invocation.stamp().

Drives the real function against a duck-typed stand-in AND a real Workspace row,
because the production callers in F3e1b pass a real one and a stand-in alone would
not catch an attribute-name change on the model.
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from call_manager.proposal_assist.ports.errors import UnfencedInvocation
from call_manager.proposal_assist.ports.stamp import FenceStamp, stamp_for_workspace


class _WS:
    """The minimum a caller must hold: an id and a generation."""

    def __init__(self, id_, generation):
        self.id = id_
        self.lifecycle_generation = generation


def test_it_mints_the_workspace_id_and_generation():
    wid = uuid.uuid4()
    stamp = stamp_for_workspace(_WS(wid, 7))
    assert stamp.workspace_id == wid
    assert stamp.generation == 7


def test_generation_zero_is_real_and_is_not_refused():
    """Workspace.lifecycle_generation has server_default 0, so a fresh workspace
    legitimately carries 0. A falsiness test here would refuse every new workspace —
    Invocation.stamp() documents the same trap."""
    stamp = stamp_for_workspace(_WS(uuid.uuid4(), 0))
    assert stamp.generation == 0


def test_the_job_fields_are_none():
    """A request or a maintenance pass is not a background job. This is the same
    None registry.py already writes for interactive contexts."""
    stamp = stamp_for_workspace(_WS(uuid.uuid4(), 3))
    assert stamp.active_turn_job_id is None
    assert stamp.origin_job_id is None


def test_it_refuses_a_workspace_with_no_generation():
    """Refusing beats defaulting: an actor with no claimed generation has no belief
    to fence against, and a default would submit an effect against a generation
    nobody claimed. Mirrors Invocation.stamp()."""
    wid = uuid.uuid4()
    with pytest.raises(UnfencedInvocation):
        stamp_for_workspace(_WS(wid, None))


def test_the_stamp_is_immutable():
    # dataclasses.FrozenInstanceError, not a bare Exception: ruff has "B" selected
    # so B017 rejects the broad form, and test_f3b_stamp.py:63 already uses this
    # spelling for the same assertion on the same type.
    stamp = stamp_for_workspace(_WS(uuid.uuid4(), 1))
    with pytest.raises(dataclasses.FrozenInstanceError):
        stamp.generation = 99


def test_it_mints_from_a_real_workspace_row(pg_session_factory):
    """The attribute names must match the ORM, not just the stand-in.

    pg_session_factory is the fixture this repo provides (tests/conftest.py:70);
    make_workspace in tests/proposal_assist/conftest.py:200 is a HELPER, not a
    fixture, so it is imported and called, not requested.
    """
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as session:
        ws = make_workspace(session)
        stamp = stamp_for_workspace(ws)
    assert stamp == FenceStamp(
        workspace_id=ws.id,
        generation=ws.lifecycle_generation,
        active_turn_job_id=None,
        origin_job_id=None,
    )
```

**Read `make_workspace`'s signature before writing this** — it is a helper at
`tests/proposal_assist/conftest.py:200`, and its required arguments are not guessed here:

```bash
sed -n '195,215p' tests/proposal_assist/conftest.py
```

Call it with whatever it actually needs. If importing from a `conftest` module is awkward
in this layout, build the `Workspace` row inline with `pg_session_factory` instead — the
test's claim is about attribute names, not about how the row was made. **Do not invent a
fixture.**

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/proposal_assist/ports/test_stamp_for_workspace.py -q
```
Expected: collection error / `ImportError: cannot import name 'stamp_for_workspace'`.
That is the correct failure — the symbol does not exist yet.

- [ ] **Step 3: Implement**

Append to `src/call_manager/proposal_assist/ports/stamp.py`:

```python
def stamp_for_workspace(workspace) -> FenceStamp:
    """The fencing identity of an actor holding a Workspace it just loaded.

    The service-side twin of `Invocation.stamp()`. Tools get their stamp from
    dispatch; a route, the collab kernel and a maintenance pass get it here, from
    the object whose access check or liveness pre-screen they have already run.
    Spec 2026-08-21 §7.0.2.

    *workspace* is duck-typed on purpose — it needs `.id` and
    `.lifecycle_generation` and nothing else. Annotating it `Workspace` would put
    `db.models` in this module's import graph, and the whole point of the ports
    package is that it names nothing from the app (it is stdlib-only, so a route,
    the kernel and a background job can all import it).

    The job fields are None because a request is not a background job — the same
    None `registry.py` already writes for interactive contexts, by design.

    Raises UnfencedInvocation rather than defaulting, for the reason
    Invocation.stamp() gives: an actor with no claimed generation has no belief to
    fence against, and defaulting would submit an effect against a generation
    nobody claimed. Generation 0 is REAL (Workspace.lifecycle_generation has
    server_default 0), so this tests for None, never for falsiness.
    """
    if workspace.lifecycle_generation is None:
        raise UnfencedInvocation(workspace.id)
    return FenceStamp(
        workspace_id=workspace.id,
        generation=workspace.lifecycle_generation,
        active_turn_job_id=None,
        origin_job_id=None,
    )
```

Add the import at the top of `stamp.py`:

```python
from call_manager.proposal_assist.ports.errors import UnfencedInvocation
```

**Check for an import cycle before you run:** `ports/errors.py` must not import
`ports/stamp.py`. Run `grep -n "^from\|^import" src/call_manager/proposal_assist/ports/errors.py`.
If it does import stamp, do **not** add the import — raise `UnfencedInvocation` via a local
import inside the function and record it under PLAN DEFECTS.

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/proposal_assist/ports/test_stamp_for_workspace.py -q
```
Expected: **6 passed** (5 if the real-row fixture did not exist — say which).

- [ ] **Step 5: Prove it has no callers**

```bash
grep -rn "stamp_for_workspace" src/ | grep -v "def stamp_for_workspace"
```
Expected: **no output.** The carrier ships before its users, as `F3b`'s ports did. If this
prints anything, something converted a caller — that is `F3e1b`.

- [ ] **Step 6: Full suite + lint**

```bash
uv run pytest -n 16 --dist loadfile -q && uv run ruff check .
```
Expected: **3809 passed, 1 skipped** (3803 + 6). An `__init__.py` is not a test and does
not change the count; only add one if this repo's test tree uses them — check a sibling
directory first.

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/proposal_assist/ports/stamp.py tests/proposal_assist/ports/
git commit -m "feat(fence): services can mint a FenceStamp (F3e1a Task 1)"
```

---

## Task 2: The stamp-provenance gate

The gate that makes "capture early" checkable instead of a paragraph. It has **no real
call sites to police yet** — every mechanism-2 acquisition arrives in `F3e1b` — so it is
proven against fixtures, exactly as `F3c` proved the import gate.

**Files:**
- Create: `tests/fencing/stamp_gate.py`
- Create: `tests/fencing/test_stamp_gate.py`

**Interfaces:**
- Produces: `check_source(source, path) -> list[str]`, one string per violation.
  `F3e1b` runs it over `src/` once real call sites exist.

**The rule:** every `generation=` keyword argument passed to
`acquire_commit_mutex_at_generation` must be an attribute access on a name (e.g.
`stamp.generation`), not a call. `generation=session.get(Workspace, wid).lifecycle_generation`
is a `Call` inside the attribute chain and is rejected; `generation=stamp.generation` is not.

- [ ] **Step 1: Write the failing tests**

Create `tests/fencing/test_stamp_gate.py`:

```python
"""Fence F3e1a — the mechanism-2 generation must come from a stamp.

Fixture-driven: no production site takes acquire_commit_mutex_at_generation yet
(F3e1b converts them), so a scan over src/ would be vacuously green and would prove
nothing. These drive the checker directly against synthetic sources.
"""

from __future__ import annotations

from tests.fencing.stamp_gate import check_source


def _check(src: str) -> list[str]:
    return check_source(src, path="svc/example.py")


def test_a_stamp_attribute_is_accepted():
    src = (
        "def f(session, wid, stamp):\n"
        "    acquire_commit_mutex_at_generation(session, wid, generation=stamp.generation)\n"
    )
    assert _check(src) == []


def test_an_inline_late_read_is_rejected():
    """The exact defect four spec revisions could only warn about in prose."""
    src = (
        "def f(session, wid):\n"
        "    acquire_commit_mutex_at_generation(\n"
        "        session, wid,\n"
        "        generation=session.get(Workspace, wid).lifecycle_generation,\n"
        "    )\n"
    )
    problems = _check(src)
    assert len(problems) == 1
    assert "generation" in problems[0]


def test_a_bare_local_read_is_rejected():
    """`generation=ws.lifecycle_generation` reads the model directly rather than a
    stamp. Legal-looking and wrong: the stamp is what carries the actor's belief."""
    src = (
        "def f(session, wid, ws):\n"
        "    acquire_commit_mutex_at_generation(session, wid, generation=ws.lifecycle_generation)\n"
    )
    assert len(_check(src)) == 1


def test_a_literal_is_rejected():
    src = (
        "def f(session, wid):\n"
        "    acquire_commit_mutex_at_generation(session, wid, generation=0)\n"
    )
    assert len(_check(src)) == 1


def test_a_positional_generation_is_rejected():
    """The primitive is kw-only today, but a checker that only inspects keywords
    would silently pass a future positional form."""
    src = (
        "def f(session, wid, stamp):\n"
        "    acquire_commit_mutex_at_generation(session, wid, stamp.generation)\n"
    )
    assert len(_check(src)) == 1


def test_the_live_variant_is_not_this_gate_s_business():
    """Scoping. acquire_commit_mutex_live is the no-_live gate's problem (Task 7);
    this gate must not double-report it."""
    src = "def f(session, wid):\n    acquire_commit_mutex_live(session, wid)\n"
    assert _check(src) == []


def test_the_checker_reports_the_qualified_symbol():
    """A violation nobody can locate is a violation nobody fixes."""
    src = (
        "class C:\n"
        "    def m(self, session, wid):\n"
        "        acquire_commit_mutex_at_generation(session, wid, generation=1)\n"
    )
    assert "C.m" in _check(src)[0]
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/fencing/test_stamp_gate.py -q
```
Expected: collection error, `ModuleNotFoundError: tests.fencing.stamp_gate`.

- [ ] **Step 3: Implement**

Create `tests/fencing/stamp_gate.py`:

```python
"""Fence F3e1a — a mechanism-2 generation must come from a FenceStamp.

Spec 2026-08-21 §7.0.3. Revisions 1-4 of that spec could only SAY "capture the
generation early"; four review rounds found defects in the prose that said it. This
turns the rule into something the build checks.

Reads source as TEXT and matches parsed AST call nodes. It never inspects source
text for a substring, because a comment beside the code would satisfy that — a
mistake this project has shipped four times.
"""

from __future__ import annotations

import ast

#: The mechanism-2 acquisition. Its generation argument is the one under scrutiny.
MECHANISM_2_ACQUIRE = "acquire_commit_mutex_at_generation"

#: The attribute a stamp exposes. `stamp.generation` is the only accepted shape.
STAMP_FIELD = "generation"

SCOPED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _is_stamp_attribute(node: ast.AST) -> bool:
    """`<name>.generation` and nothing else.

    A Call anywhere in the chain is what rejects an inline late read such as
    `session.get(Workspace, wid).lifecycle_generation`: its value is an Attribute
    whose .value is a Call, not a Name.
    """
    return (
        isinstance(node, ast.Attribute)
        and node.attr == STAMP_FIELD
        and isinstance(node.value, ast.Name)
    )


def check_source(source: str, path: str) -> list[str]:
    """One string per violation. Pure; imports nothing from src/."""
    tree = ast.parse(source)
    problems: list[str] = []

    def visit(node: ast.AST, stack: list[str]) -> None:
        if isinstance(node, SCOPED):
            stack = stack + [node.name]
        elif isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name == MECHANISM_2_ACQUIRE:
                qual = ".".join(stack) if stack else "<module>"
                supplied = [kw.value for kw in node.keywords if kw.arg == STAMP_FIELD]
                # A positional third argument is the generation too. Checking only
                # keywords would pass a future positional form silently.
                if len(node.args) > 2:
                    supplied.append(node.args[2])
                if not supplied:
                    problems.append(
                        f"{path}::{qual} calls {MECHANISM_2_ACQUIRE} with no generation"
                    )
                for value in supplied:
                    if not _is_stamp_attribute(value):
                        problems.append(
                            f"{path}::{qual} passes a generation that is not "
                            f"<stamp>.{STAMP_FIELD} — the actor's belief must come "
                            f"from a FenceStamp, never a read at the mutex"
                        )
        for child in ast.iter_child_nodes(node):
            visit(child, stack)

    visit(tree, [])
    return problems
```

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/fencing/test_stamp_gate.py -q
```
Expected: **7 passed**.

- [ ] **Step 5: Mutation — prove the gate can fail**

Copy the file first; **never** restore with `git checkout`.

```bash
cp tests/fencing/stamp_gate.py /tmp/stamp_gate.bak
```

Mutate `_is_stamp_attribute` to accept anything:

```python
def _is_stamp_attribute(node: ast.AST) -> bool:
    return True
```

```bash
uv run pytest tests/fencing/test_stamp_gate.py -q
```
Expected: **4 FAILED** — `test_an_inline_late_read_is_rejected`,
`test_a_bare_local_read_is_rejected`, `test_a_literal_is_rejected`,
`test_a_positional_generation_is_rejected`. **FAILED, not ERROR.** If they ERROR, the
mutation broke an import and proves nothing — fix the mutation, do not accept the result.

Restore and re-verify green:

```bash
cp /tmp/stamp_gate.bak tests/fencing/stamp_gate.py
uv run pytest tests/fencing/test_stamp_gate.py -q   # 7 passed
```

- [ ] **Step 6: Full suite + lint**

```bash
uv run pytest -n 16 --dist loadfile -q && uv run ruff check .
```
Expected: **3816 passed, 1 skipped** (3809 + 7).

- [ ] **Step 7: Commit**

```bash
git add tests/fencing/stamp_gate.py tests/fencing/test_stamp_gate.py
git commit -m "test(fence): a mechanism-2 generation must come from a stamp (F3e1a Task 2)"
```

---

## Task 3: Inventory machinery, inert

Everything the new kind needs, added **before** anything discovers one. Nothing new is
found in this task, so the inventory file does not change and every gate stays green.

**Files:**
- Modify: `tests/fencing/detectors.py` (constant only)
- Modify: `tests/fencing/inventory.py`
- Modify: `tests/fencing/test_fence_coverage.py`
- Modify: `tests/fencing/test_inventory_merge.py`
- Modify: `tests/proposal_assist/test_f3d3a_relocation.py`

**Interfaces:**
- Produces: `KIND_GIT_COMMIT`; `MEASURED_KINDS`; `fenced_ids` widened from
  `set[(path, symbol)]` to `set[(kind, path, symbol)]`. Task 6 turns discovery on.

**Why widen `fenced_ids` instead of returning a third set:** `scan_tree()`'s docstring
already warns that a pair exists so "every caller that merges needs both halves and cannot
forget the second". A third element repeats that problem. One set keyed by the full site
identity scales to any number of measured kinds, and `merge()` already keys rows by
`(kind, path, symbol)` everywhere else.

- [ ] **Step 1: Add the constant and the categories**

In `tests/fencing/detectors.py`, beside the other kinds:

```python
KIND_GIT_COMMIT = "git_commit"
```

In `tests/fencing/inventory.py`, import it alongside the others, then:

```python
_SLICE_FOR_KIND = {
    KIND_CLAIM_FN: "F4",          # a NEW claim fn still defaults to F4
    KIND_GIT_COMMIT: UNCLASSIFIED,  # a NEW git site stops the build until allocated
}
```

**`UNCLASSIFIED` must already be defined above `_SLICE_FOR_KIND`** — in the current file it
is defined below. Move the `UNCLASSIFIED = "UNCLASSIFIED"` assignment (and its comment)
above `_SLICE_FOR_KIND`, or Python raises `NameError` at import. This is a real ordering
trap: check it.

Then extend the categories:

```python
CATEGORIES_BY_KIND = {
    KIND_TOOL_COMMIT: frozenset({"semantic", "accounting", "caller_fenced", UNCLASSIFIED}),
    KIND_TOOL_CONTEXT: frozenset({"semantic", UNCLASSIFIED}),
    KIND_CLAIM_FN: frozenset({"semantic", UNCLASSIFIED}),
    # `semantic` only, and deliberately NO caller_fenced analogue: a caller's
    # transaction can fence a DB write, but nothing anyone else's transaction does
    # can fence a git commit. Either the function takes the mutex at a generation
    # itself or the effect is unfenced — there is no third party to defer to.
    KIND_GIT_COMMIT: frozenset({"semantic", UNCLASSIFIED}),
}
```

**Note the two `UNCLASSIFIED` uses are different fields.** `_SLICE_FOR_KIND` maps a new
site's **slice** to it so the gate refuses the site; the **category** must never stay
`UNCLASSIFIED` on a committed row.

- [ ] **Step 2: Widen the measurement to a set of triples**

In `detectors.py`, change `find_fenced_tool_commits`'s return to carry the kind:

```python
    return {(KIND_TOOL_COMMIT, path, q) for q in port_calls - raw_commits}
```

and update its docstring's first line to `(kind, path, symbol)`.

In `inventory.py`, add beside the kind constants:

```python
#: Kinds whose `fenced` is RECOMPUTED from source on every regeneration rather than
#: carried forward. A kind outside this set keeps its stored flag — `is this a port
#: call` is meaningless for a claim function.
MEASURED_KINDS = frozenset({KIND_TOOL_COMMIT})
```

and change `merge()`'s `fenced` expression to:

```python
            "fenced": (
                (s.kind, s.path, s.symbol) in fenced_ids
                if s.kind in MEASURED_KINDS
                else (prev["fenced"] if prev else False)
            ),
```

Update `merge()`'s type hint: `fenced_ids: set[tuple[str, str, str]]`.

- [ ] **Step 3: Update EVERY pair-shaped consumer — there are more than the obvious ones**

`fenced_ids` is consumed in three shapes, and only the first is easy to spot: membership
tests, **whole-set equality assertions**, and **hand-built inputs passed into `merge()`**.
A `_fenced_ids`/`_measured` discard needs no change; everything else does.

Known sites — read each before editing, and do not assume this list is complete:

| File | Line | Shape |
|---|---|---|
| `tests/fencing/inventory.py` | `merge()`, `scan_tree()` | type hints `set[tuple[str, str]]` → `set[tuple[str, str, str]]` |
| `tests/fencing/detectors.py` | `find_fenced_tool_commits` | return **and** its type hint and docstring |
| `tests/fencing/test_inventory_merge.py` | 48 | hand-built input `{(P, "alpha")}` → `{(KIND_TOOL_COMMIT, P, "alpha")}` |
| `tests/fencing/test_inventory_merge.py` | **79** | a SECOND hand-built input `{(P, "beta")}` — revision 2 of this plan still missed it |
| `tests/fencing/test_inventory_merge.py` | 109 | pass-through; no edit |
| **`tests/fencing/test_detectors.py`** | **316** | **whole-set equality on `find_fenced_tool_commits`** |
| `tests/fencing/test_fence_coverage.py` | 303 | whole-set equality |
| `tests/fencing/test_fence_coverage.py` | 331, 400 | membership |
| `tests/proposal_assist/test_f3d3a_relocation.py` | 175 | whole-set equality against a literal set of pairs |

**`test_detectors.py` is a whole file this plan's earlier draft never mentioned.** Derive
the real list rather than trusting the table:

```bash
grep -rnE "fenced_ids|find_fenced_tool_commits|_measured|_fenced" \
  tests/fencing/ tests/proposal_assist/test_f3d3a_relocation.py
```

Then run the fencing suite and let it name anything the grep missed.

- [ ] **Step 4: Narrow the scoping test**

`test_non_tool_commit_rows_keep_their_stored_flag` (`test_fence_coverage.py:345`) asserts
the OLD scoping and becomes wrong once a second kind is measured. **Narrow it; do not
delete it** — the coverage migrates. Replace its body's claim with the `MEASURED_KINDS`
rule and rename it:

```python
def test_unmeasured_kinds_keep_their_stored_flag():
    """Scoping, tested against merge() ITSELF rather than against the file.

    Reading load_inventory() here would prove nothing: it reports what the JSON
    says, and the JSON says F3a is fenced because a human wrote that. The claim
    under test is about the CODE — that merge() applies the measured flag only to
    kinds in MEASURED_KINDS — so drive merge() with an EMPTY fenced_ids set that
    would flip an F3a row if the kind check were missing.

    F3e1a widened this from "only tool_commit" to MEASURED_KINDS, which now also
    holds git_commit. claim_fn and tool_context stay stored-flag kinds.
    """
    previous = load_inventory()
    f3a = [r for r in previous if r["slice"] == "F3a"]
    assert len(f3a) == 9
    assert all(r["kind"] in {"claim_fn", "tool_context"} for r in f3a)
    assert not ({"claim_fn", "tool_context"} & MEASURED_KINDS), (
        "this test's premise is that these kinds are NOT measured"
    )

    found, _measured = scan_tree()
    merged = merge(previous, found, None, set())
    by_id = {(r["kind"], r["path"], r["symbol"]): r for r in merged}
    for row in f3a:
        assert by_id[(row["kind"], row["path"], row["symbol"])]["fenced"] is True, row
```

Import `MEASURED_KINDS` at the top of that test module.

- [ ] **Step 5: Run the fencing suite**

```bash
uv run pytest tests/fencing tests/proposal_assist/test_f3d3a_relocation.py -q
```
Expected: **all green**, and the inventory file **unchanged** — nothing new is discovered
yet. Confirm with `git diff --stat tests/fencing/fence_inventory.json`, which must print
nothing.

- [ ] **Step 6: Full suite + lint**

```bash
uv run pytest -n 16 --dist loadfile -q && uv run ruff check .
```
Expected: **3816 passed, 1 skipped — unchanged.** This task adds NO tests: it renames one
and edits several. A different number means you added or lost a test; find out which before
continuing.

- [ ] **Step 7: Commit**

```bash
git add tests/fencing/ tests/proposal_assist/test_f3d3a_relocation.py
git commit -m "test(fence): inventory machinery for a second measured kind (F3e1a Task 3)"
```

---

## Task 4: The slice split, and the spec amendment

Splits `F3e` into `F3e1a` / `F3e1b` / `F3e2` and moves the two existing rows. **Still no
new sites** — this is four coordinated edits that must land in ONE commit, because each
alone reddens a gate.

**Files:**
- Modify: `tests/fencing/inventory.py` (`SLICE_STATUS`, `F3D_IDENTITIES`)
- Modify: `tests/fencing/fence_inventory.json` (two `slice` strings)
- Modify: `tests/fencing/test_fence_coverage.py` (the ingest-row test)
- Modify: `docs/superpowers/specs/2026-08-18-fence-session-ownership-design.md`

- [ ] **Step 1: Split `SLICE_STATUS`**

**This is a FRAGMENT, not the whole dict.** It replaces the single `"F3e"` line and
the `"F3d3"` line above it is shown only as an anchor. Every other key — `F3a`, `F3b`,
`F3c`, `F3d1`, `F3d2`, `F4`, `F5` — stays exactly as it is.

```python
    "F3d3": "complete",  # the citation core relocation, and the propose_* sites
    # Spec §7's F3e, cut into three for the same reason F3d was cut into three:
    # SLICE_STATUS has no partial state, so a multi-slice conversion needs one key
    # per slice or the first flipped row goes red.
    "F3e1a": "complete",  # the carrier, the git detector, the gates — no row of its own
    "F3e1b": "pending",   # the git-effect conversions
    "F3e2": "pending",    # the ingest pipeline and the filesystem effects
    "F3f": "pending",    # capability holders, then the handle
```

The `"F3e"` key is **removed**.

- [ ] **Step 2: Move the two rows in the JSON**

In `tests/fencing/fence_inventory.json`, change `"slice": "F3e"` → `"slice": "F3e2"` for
both `_write_citekey_cache` and `fetch_reference_impl._do_fetch_and_ingest`. Leave every
other field alone.

- [ ] **Step 3: Update `F3D_IDENTITIES`**

`tests/fencing/inventory.py`, in the pinned identity set:

```python
    ("F3e2", "tool_commit", "_write_citekey_cache"),
```

**This does not contradict the constant's own comment.** That comment forbids *deleting* an
entry to make a test pass. This **modifies** an entry's slice to follow a reallocation a
human made — the identity is intact and the row it names still exists. Extend the comment
above the set to say so, so the next reader does not hesitate:

```python
#: `_write_citekey_cache` moved F3d3 -> F3e (F3d3b) -> F3e2 (F3e1a). Modifying a
#: slice string to follow a human reallocation is NOT the deletion this comment
#: forbids: the identity is intact and the row still exists.
```

- [ ] **Step 4: Update the ingest-row test**

`test_fence_coverage.py:408` — its **name, docstring and assertion** all say `F3e`:

```python
def test_the_ingest_row_is_untouched_and_still_f3e2():
    """The boundary this slice must not cross. fetch_reference_impl._do_fetch_and_ingest
    is a SEPARATE nested identity holding the ingest commit. F3d2 converted the
    accounting around it and left it alone; F3e1a discovers git sites but converts
    nothing, so it is still unfenced and now sits in F3e2."""
    from tests.fencing.inventory import load_inventory

    row = next(
        r
        for r in load_inventory()
        if r["symbol"] == "fetch_reference_impl._do_fetch_and_ingest"
    )
    assert row["slice"] == "F3e2"
    assert row["fenced"] is False
```

- [ ] **Step 5: Amend the governing spec**

In `docs/superpowers/specs/2026-08-18-fence-session-ownership-design.md`, in the `### F3e`
section (~line 505), add a dated revision note recording that F3e is split into `F3e1a` /
`F3e1b` / `F3e2`, that the mechanism-2 effects are fenced by **converting the mutex
acquisition**, not by a port operation, and pointing to
`2026-08-21-fence-f3e1-git-mechanism-design.md` §9 for the argument. Do not rewrite the
section; add the note in the style of the existing "Revised 2026-08-21 (F3d3b)" note
already in that file.

- [ ] **Step 6: Prove regeneration is a no-op**

The reassignment is human-owned, so `merge()` must preserve it. **Do not use
`git diff --exit-code`** — this task deliberately edited the file, so that can never pass.
Regenerate into a temporary copy and compare:

```bash
uv run python - <<'PY'
import json
from tests.fencing.inventory import INVENTORY, load_inventory, merge, scan_tree
sites, fenced_ids = scan_tree()
regenerated = merge(load_inventory(), sites, {}, fenced_ids)
committed = json.loads(INVENTORY.read_text())
print("IDENTICAL" if regenerated == committed else "DIFFERS")
assert regenerated == committed
PY
```
Expected: `IDENTICAL`. This proves `merge()` preserved the human `slice` values rather than
recomputing them from the kind defaults.

- [ ] **Step 7: Fencing suite, then full suite**

```bash
uv run pytest tests/fencing -q
uv run pytest -n 16 --dist loadfile -q && uv run ruff check .
```
Expected: fencing **green**; full suite **3817 passed, 1 skipped** (3816 + 1). The +1 is a
test asserting `SLICE_STATUS` has no `"F3e"` key and does have all three new ones — write
it, in `test_fence_coverage.py`, beside the other `SLICE_STATUS` tests.

- [ ] **Step 8: Commit — all four edits together**

```bash
git add tests/fencing/ docs/superpowers/specs/2026-08-18-fence-session-ownership-design.md
git commit -m "test(fence): F3e splits into F3e1a/F3e1b/F3e2 (F3e1a Task 4)"
```

---

## Task 5: The shared git-fence checker — deliberately CONSERVATIVE

**This checker under-reports on purpose, and that is the whole design.**

Three earlier drafts of this predicate were wrong in three different ways: it was
**vacuous** (zero acquisitions satisfied it), then **order-blind** (set co-occurrence
accepted a sink running before its acquisition), then **modelling the wrong events** (it
compared acquisitions to git sinks while spec §4.1's rule is "before the path's first
effect of any kind" — a room mutation counts, and `kernel.py:169` performs one before its
checkpoint).

Patching a fourth time would mean **enumerating every effect**, which is the open set that
got F2 demoted from a gate in the first place: *"Writes are an open set, so the enumeration
could never be complete, and a review found five reachable bypasses."*

**So it is not patched. It is narrowed.** Spec §5.1 already says this gate is *"a cheap
ratchet, not the proof — the proof is §5.2"* (the runtime two-ordered-lock cases per site).
What the checker must never do is **over-report**: `fenced` is what lets a slice declare
itself `complete`, so a false `true` lets a slice claim conversion it did not do. A false
`false` costs a pin entry and a human sentence.

### The rule

> A qualname is **fenced** when, and only when, `acquire_commit_mutex_at_generation`
> appears as a **standalone expression statement directly in that function's body**
> (`ast.Expr` whose value is the call, and whose parent statement list is the function
> body itself — not inside `if`, `try`, `for`, `while` or `with`), it appears **before
> every statement containing a sink call**, and **no `acquire_commit_mutex_live` appears
> anywhere in the function**.

Everything else is **unfenced**, including shapes that are perfectly correct.

Why each clause dissolves a defect rather than patching it:

| Clause | Kills |
|---|---|
| standalone `ast.Expr` statement | the nested-call case `acquire(session, sink(...), generation=...)`, where preorder says "acquisition first" but Python evaluates the argument first |
| directly in the function body | every branch-bypass shape — `if c: acquire() else: sink()`, `try/except/finally`, loops — without needing to distinguish `body`/`orelse`/`handlers`/`finalbody` edges at all |
| statement-index comparison, not preorder | "before" now means before in execution, because both are top-level statements of the same block |
| no effect enumeration anywhere | the open-set problem that demoted F2 |

**Nested function definitions are separate qualnames** and are analysed independently; a
`def` inside a function does not inherit its parent's acquisition.

### What this costs, stated loudly

Three real sites acquire inside a `try:` — `_insert_citation_locked`,
`insert_citation_for_user`, and `post_lock_spec`. They will measure **unfenced even after
F3e1b converts them correctly**, and each needs a pin carrying a human sentence plus its
§5.2 runtime proof. **`fenced: false` on a git row does not mean "broken"** — it means
"not provable by the cheap check". Put that sentence in the module docstring, in the
inventory's own comment, and in the pin list's comment, because a future reader who
believes `false` means broken will "fix" working code.

**Files:**
- Create: `tests/fencing/git_fence.py`
- Create: `tests/fencing/test_git_fence.py`

**Interfaces:**
- Produces: `GIT_SINKS` (canonical `(path_suffix, qualname, kind)` declaration),
  `sink_calls(source, path)`, `unprotected_sinks(source, path) -> dict[str, list[str]]`,
  `fenced_qualnames(source, path) -> set[str]`.
  Task 6 derives `fenced` from it; **Task 8 runs the same function over the tree.**

- [ ] **Step 1: Write the failing tests**

Create `tests/fencing/test_git_fence.py`. Each test is a shape an earlier draft got wrong.

```python
"""Fence F3e1a — the one predicate the measurement and the gate both use.

CONSERVATIVE BY DESIGN: it under-reports. A shape it cannot prove fenced measures
unfenced, including shapes that are correct. That direction is the safe one — a
false `fenced: true` lets a slice claim a conversion it did not make, while a false
`false` costs a pin and a sentence.
"""

from __future__ import annotations

from tests.fencing.git_fence import fenced_qualnames, sink_calls, unprotected_sinks

_ACQ = "    acquire_commit_mutex_at_generation(s, w, generation=stamp.generation)\n"
_SINK = "    write_and_commit_files(repo, {}, 'm')\n"
_HEAD = "def f(s, w, stamp, repo):\n"


def _fenced(body: str) -> set[str]:
    return fenced_qualnames(_HEAD + body, "x/f.py")


def test_acquisition_then_sink_is_fenced():
    assert _fenced(_ACQ + _SINK) == {"f"}


def test_no_acquisition_at_all_is_not_fenced():
    """The vacuous case: zero acquisitions must not satisfy a positive rule."""
    assert _fenced(_SINK) == set()


def test_sink_before_acquisition_is_not_fenced():
    """Order. The bytes are on disk before the fence is taken."""
    assert _fenced(_SINK + _ACQ) == set()


def test_an_acquisition_nested_as_a_call_argument_is_not_fenced():
    """Preorder is not execution order. Python evaluates the ARGUMENT first, so the
    sink runs before the acquisition here — while an AST preorder walk visits the
    outer call first and would call it protected."""
    src = (
        _HEAD
        + "    acquire_commit_mutex_at_generation(\n"
        + "        s, write_and_commit_files(repo, {}, 'm'), generation=stamp.generation\n"
        + "    )\n"
    )
    assert fenced_qualnames(src, "x/f.py") == set()


def test_an_acquisition_in_an_if_branch_is_not_fenced():
    """Branch bypass, the true form: acquisition and sink on MUTUALLY EXCLUSIVE
    arms. An ancestor-containment rule that records `ast.If` without distinguishing
    body from orelse calls this protected."""
    src = _HEAD + "    if cond:\n    " + _ACQ + "    else:\n    " + _SINK
    assert fenced_qualnames(src, "x/f.py") == set()


def test_an_acquisition_inside_try_is_not_fenced_conservatively():
    """The deliberate under-report. This shape is CORRECT — post_lock_spec and
    _insert_citation_locked both write it — and the checker still says unfenced,
    because proving it needs branch reasoning this design refuses to do. Those
    sites get a pin and a runtime proof instead."""
    src = _HEAD + "    try:\n    " + _ACQ + "    " + _SINK + "    except Exception:\n        pass\n"
    assert fenced_qualnames(src, "x/f.py") == set()


def test_a_second_unprotected_sink_disqualifies_the_function():
    """EVERY sink must follow the acquisition, not just one."""
    src = _HEAD + _SINK + _ACQ + _SINK
    assert fenced_qualnames(src, "x/f.py") == set()


def test_a_live_acquisition_disqualifies_even_with_a_correct_one():
    assert _fenced(_ACQ + "    acquire_commit_mutex_live(s, w)\n" + _SINK) == set()


def test_a_nested_def_does_not_inherit_its_parent_acquisition():
    """Separate qualnames, analysed independently."""
    src = _HEAD + _ACQ + "    def inner():\n    " + _SINK + "    return inner\n"
    assert "f.inner" not in fenced_qualnames(src, "x/f.py")


def test_a_function_with_no_sink_is_neither_fenced_nor_reported():
    src = "def g(s, w):\n    acquire_commit_mutex_live(s, w)\n"
    assert fenced_qualnames(src, "x/g.py") == set()
    assert unprotected_sinks(src, "x/g.py") == {}


def test_unprotected_sinks_names_the_reason():
    reasons = unprotected_sinks("def f(repo):\n" + _SINK, "x/f.py")
    assert "f" in reasons
    assert any("no top-level acquire_commit_mutex_at_generation" in r for r in reasons["f"])


def test_the_two_halves_PARTITION_every_sink_qualname():
    """Not merely disjoint — disjointness is satisfied by both sets being empty,
    which would hide a sink from the measurement AND the gate at once. Every
    qualname holding a sink must land in exactly one side.
    """
    for body in (_ACQ + _SINK, _SINK, _SINK + _ACQ):
        src = _HEAD + body
        holders = {q for q, _n, _i in sink_calls(src, "x/f.py")}
        fenced = fenced_qualnames(src, "x/f.py")
        unprot = set(unprotected_sinks(src, "x/f.py"))
        assert fenced | unprot == holders, (body, holders, fenced, unprot)
        assert not (fenced & unprot)
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/fencing/test_git_fence.py -q
```
Expected: `ModuleNotFoundError: tests.fencing.git_fence`.

- [ ] **Step 3: Implement**

Create `tests/fencing/git_fence.py`, providing:

**The canonical sink declaration** — exact identities, not bare names, because Task 7's
guards need the file and the exact function and a whole-file exemption would let a new
committing function inside `gitio.py` escape:

```python
#: The ONLY two ways this repository makes a git commit, declared as EXACT
#: identities. Verified by sweeping every "commit" string argument in src/.
GIT_SINKS = (
    ("proposal_assist/citations/gitio.py", "write_and_commit_files", "always"),
    ("proposal_assist/phases/setup.py", "_run_git", "argv"),   # only argv[0] == "commit"
)
```

**`_sink_bindings(tree) -> dict[str, str]`** — local name → **canonical** sink name.
Returning only local spellings loses which sink it is, and the argv rule is per-sink, so an
aliased `_run_git(["init"])` would otherwise count as a commit.

**`_is_commit_call(canonical, node)`** keyed on the canonical name.

**`sink_calls(source, path)`** → `[(qualname, node, stmt_index)]`, where `stmt_index` is the
index of the **function-body top-level statement** containing the call, or `None` if the
call is not under one (nested in a branch, a nested `def`, or an argument position).

**`fenced_qualnames` / `unprotected_sinks`** implementing the rule above.
`fenced_qualnames` must be defined as `holders - set(unprotected_sinks(...))` so the two
cannot drift; do not compute it with a second walk.

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/fencing/test_git_fence.py -q
```
Expected: **12 passed**.

- [ ] **Step 5: Mutations — each clause must be provably load-bearing**

`cp tests/fencing/git_fence.py /tmp/git_fence.bak` first. Restore between each.

| Mutation | Must FAIL |
|---|---|
| accept an acquisition anywhere in the function, not only a top-level `ast.Expr` | `test_an_acquisition_nested_as_a_call_argument_is_not_fenced`, `test_an_acquisition_in_an_if_branch_is_not_fenced` |
| drop the statement-index comparison | `test_sink_before_acquisition_is_not_fenced`, `test_a_second_unprotected_sink_disqualifies_the_function` |
| require only ONE protected sink rather than all | `test_a_second_unprotected_sink_disqualifies_the_function` |
| drop the `_live` check | `test_a_live_acquisition_disqualifies_even_with_a_correct_one` |
| let `fenced_qualnames` compute its own walk instead of subtracting | `test_the_two_halves_PARTITION_every_sink_qualname` |

**FAILED, not ERROR** each time. Restore, confirm 12 passed.

- [ ] **Step 6: Full suite + lint, then commit**

```bash
uv run pytest -n 16 --dist loadfile -q && uv run ruff check .
git add tests/fencing/git_fence.py tests/fencing/test_git_fence.py
git commit -m "test(fence): one conservative predicate for a fenced git sink (F3e1a Task 5)"
```
Expected: **3829 passed, 1 skipped** (3817 + 12).

---
## Task 6: Git-sink detection, and the nine rows

The task that turns discovery on. Detection widening and the inventory regeneration land
in **ONE commit** — splitting them leaves the gate red for a reason that commit does not
fix, which is the F3d3a lesson.

**Files:**
- Modify: `tests/fencing/detectors.py`
- Modify: `tests/fencing/inventory.py` (`MEASURED_KINDS`)
- Modify: `tests/fencing/fence_inventory.json` (20 → 29 rows)
- Modify: `tests/fencing/test_inventory_merge.py` (`== 20` → `== 29`)
- Modify: `tests/fencing/test_fence_coverage.py` (the git measurement gate)

- [ ] **Step 1: Import the sinks from the shared checker — do not redeclare them**

Task 5 owns `GIT_SINKS`, `_sink_bindings` and `_is_commit_call`. `detectors.py` imports
them; it does **not** define its own copy. Two declarations of what a git sink is would
drift, which is the defect Task 5 exists to prevent.

```python
from tests.fencing.git_fence import fenced_qualnames, sink_calls
```

**Import only what you use** — ruff has `F` selected, so an unused `GIT_SINKS` here is
F401. Task 7's guard imports `GIT_SINKS`; `detectors.py` does not need it.

**Check for an import cycle first:** `git_fence.py` must not import `detectors.py`. It
should not need to — it parses source and knows nothing about `Site`. Run
`grep -n "^from\|^import" tests/fencing/git_fence.py` and confirm.

- [ ] **Step 2: Discover git sites, repo-wide**

Inside `find_sites`, add a branch. It is **not** guarded by `_in_effect_scope` — that scope
is tool-centric and git effects live in services, exactly as `KIND_CLAIM_FN` is already
discovered repo-wide.

Rather than re-walking, reuse Task 5's `sink_calls(tree)` and emit one `Site` per qualname
it reports:

```python
    for qual, _node, _ancestors in sink_calls(tree, path):
        sites.add(Site(KIND_GIT_COMMIT, path, qual))
```

If `find_sites`'s existing structure makes that awkward, add the call after its `visit()`
walk rather than inside it — the sites are a set, so order does not matter.

- [ ] **Step 3: Measure the new kind by DERIVING it from Task 5**

Add to `detectors.py`:

```python
def find_fenced_git_commits(source: str, path: str) -> set[tuple[str, str, str]]:
    """`(kind, path, symbol)` for every git-committing qualname that is FENCED.

    Derived from `git_fence.fenced_qualnames`, which is the SAME predicate the gate
    in test_live_mutex_gate.py uses. This function must contain no rule of its own:
    spec §3.7 requires one predicate, because two that must agree eventually will
    not — and in an earlier draft of this plan they disagreed immediately, the
    measurement accepting a sink that ran BEFORE its acquisition.
    """
    return {(KIND_GIT_COMMIT, path, q) for q in fenced_qualnames(source, path)}
```

In `inventory.py`, call it from `scan_tree` beside the existing measurement:

```python
        fenced_ids |= find_fenced_tool_commits(source, rel)
        fenced_ids |= find_fenced_git_commits(source, rel)
```

and widen `MEASURED_KINDS`:

```python
MEASURED_KINDS = frozenset({KIND_TOOL_COMMIT, KIND_GIT_COMMIT})
```

- [ ] **Step 4: Confirm the scan finds exactly nine, then regenerate**

```bash
uv run python - <<'PY'
from tests.fencing.inventory import scan_tree
sites, fenced = scan_tree()
git = sorted(s for s in sites if s.kind == "git_commit")
print(f"git_commit sites: {len(git)}")
for s in git:
    print(" ", s.path, "|", s.symbol)
print("measured fenced git sites:", sorted(f for f in fenced if f[0] == "git_commit"))
PY
```

Expected **exactly these nine**, and **zero** measured as fenced:

| path | symbol |
|---|---|
| `.../collab/checkpoint.py` | `checkpoint_artifact` |
| `.../citations/core.py` | `_insert_citation_locked` |
| `.../citations/human_cite.py` | `insert_citation_for_user` |
| `.../editing/save.py` | `save_section` |
| `.../analysis/insert.py` | `insert_from_annotation` |
| `.../citations/bibliography.py` | `build_bibliography` |
| `.../web/api/workspaces.py` | `post_init_spec._git` |
| `.../review/pass_runner.py` | `run_review_pass` |
| `.../phases/setup.py` | `Phase0Worker._init_repo` |

**If the set differs, STOP and record it under PLAN DEFECTS before regenerating.** A tenth
site is real information; a missing one means the detector is wrong.

Then regenerate:

```bash
uv run python -m tests.fencing.regenerate_inventory
```
Expected: `wrote 29 sites` and `9 UNCLASSIFIED`.

- [ ] **Step 5: Classify all nine by hand**

Every new row arrives `"category": "UNCLASSIFIED"` and `"slice": "UNCLASSIFIED"`. Edit
`fence_inventory.json` and set, for each of the nine, `"category": "semantic"` and the
slice from this table:

| symbol | slice |
|---|---|
| `checkpoint_artifact` | `F3e1b` |
| `_insert_citation_locked` | `F3e1b` |
| `insert_citation_for_user` | `F3e1b` |
| `save_section` | `F3e1b` |
| `insert_from_annotation` | `F3e2` |
| `build_bibliography` | `F3e2` |
| `post_init_spec._git` | `F3e2` |
| `run_review_pass` | `F4` |
| `Phase0Worker._init_repo` | `F5` |

All nine keep `"fenced": false` — nothing is converted in this slice.

- [ ] **Step 6: Fix the hardcoded count**

`tests/fencing/test_inventory_merge.py:113`:

```python
    assert len(after) == 29
```

- [ ] **Step 7: Add the git measurement gate**

In `test_fence_coverage.py`, beside the existing `tool_commit` drift test:

```python
def test_the_inventory_git_flags_match_the_tree():
    """The git half of the measurement. A hand-edited `fenced: true` on a git row
    does not survive a regeneration, so `complete` cannot be claimed over an
    unconverted git site."""
    from tests.fencing.inventory import load_inventory, scan_tree

    _sites, fenced_ids = scan_tree()
    problems = [
        f"{row['path']}::{row['symbol']} says fenced={row['fenced']}"
        for row in load_inventory()
        if row["kind"] == "git_commit"
        and row["fenced"] is not ((row["kind"], row["path"], row["symbol"]) in fenced_ids)
    ]
    assert not problems, (
        "git_commit rows whose fenced flag disagrees with the code:\n  "
        + "\n  ".join(problems)
        + "\n\nRegenerate the inventory; do not edit the flag by hand."
    )


def test_the_measurement_is_the_shared_predicate_and_nothing_more():
    """Task 5 owns the rule and its ten shape tests. What this asserts is that the
    measurement ADDS no rule of its own — the two cannot disagree if one delegates.
    Spec §3.7."""
    from tests.fencing.detectors import find_fenced_git_commits
    from tests.fencing.git_fence import fenced_qualnames

    src = (
        "def f(session, wid, stamp, repo):\n"
        "    acquire_commit_mutex_at_generation(session, wid, generation=stamp.generation)\n"
        "    return write_and_commit_files(repo, {}, 'm')\n"
    )
    assert find_fenced_git_commits(src, "x/f.py") == {
        ("git_commit", "x/f.py", q) for q in fenced_qualnames(src, "x/f.py")
    }
    # And the ordering rule reaches the measurement, not just the checker:
    after = (
        "def f(session, wid, stamp, repo):\n"
        "    write_and_commit_files(repo, {}, 'm')\n"
        "    acquire_commit_mutex_at_generation(session, wid, generation=stamp.generation)\n"
    )
    assert find_fenced_git_commits(after, "x/f.py") == set()


def test_an_aliased_sink_import_is_still_discovered():
    """Revision 1 claimed every git-committing function is discovered; that was
    false for an aliased import. No alias exists in the tree — this keeps it that
    way honestly rather than by luck."""
    from tests.fencing.detectors import find_sites

    src = (
        "from call_manager.proposal_assist.citations.gitio import "
        "write_and_commit_files as commit_files\n"
        "def f(repo):\n"
        "    return commit_files(repo, {}, 'm')\n"
    )
    assert any(s.kind == "git_commit" and s.symbol == "f" for s in find_sites(src, "x/f.py"))


def test_run_git_is_a_site_only_when_its_argv_commits():
    """_run_git also runs init/add/rev-list. Only a commit argv makes its caller a
    git-commit site, or phase 0 would own three spurious rows."""
    from tests.fencing.detectors import find_sites

    commits = "def f(d):\n    _run_git(['commit', '-q', '-m', 'x'], cwd=d)\n"
    inits = "def g(d):\n    _run_git(['init', '-q'], cwd=d)\n"
    assert any(s.kind == "git_commit" for s in find_sites(commits, "x/s.py"))
    assert not any(s.kind == "git_commit" for s in find_sites(inits, "x/s.py"))


def test_an_aliased_run_git_keeps_its_argv_rule():
    """An alias must keep its CANONICAL identity, not just a local spelling. An
    earlier draft resolved aliases to local names and then tested the local name
    against "_run_git" — so aliased `run_git(['init'])` was wrongly a commit."""
    from tests.fencing.detectors import find_sites

    head = "from call_manager.proposal_assist.phases.setup import _run_git as run_git\n"
    commits = head + "def f(d):\n    run_git(['commit', '-q', '-m', 'x'], cwd=d)\n"
    inits = head + "def g(d):\n    run_git(['init', '-q'], cwd=d)\n"
    assert any(s.kind == "git_commit" for s in find_sites(commits, "x/s.py"))
    assert not any(s.kind == "git_commit" for s in find_sites(inits, "x/s.py"))
```

- [ ] **Step 8: Fencing suite, then full suite**

```bash
uv run pytest tests/fencing -q
uv run pytest -n 16 --dist loadfile -q && uv run ruff check .
```
Expected: fencing **green** (all 29 rows classified, all nine git rows `fenced: false` in
`pending` slices — consistent with the bidirectional slice gate); full suite **3834 passed, 1 skipped** (3829 + 5).

- [ ] **Step 9: Commit — detection and rows together**

```bash
git add tests/fencing/
git commit -m "test(fence): nine git-commit sites discovered and measured (F3e1a Task 6)"
```

---

## Task 7: The sink-completeness guards

Makes "every way this repository can invoke git is enumerated" an assertion rather than a
hope. Three clauses, because revision 4's single argv clause was narrower than the claim it
supported.

**Files:**
- Create: `tests/fencing/sink_guard.py`
- Create: `tests/fencing/test_sink_guard.py`

- [ ] **Step 1: Write the tests**

Create `tests/fencing/test_sink_guard.py`:

```python
"""Fence F3e1a — a third way to run git must stop the build.

Spec 2026-08-21 §3.2. The declared sink list is only trustworthy if nothing can
commit outside it, so each clause is proven against a fixture that violates it AND
run over the real tree.
"""

from __future__ import annotations

from tests.fencing.sink_guard import (
    check_execution_surfaces,
    check_no_shell_true,
    check_no_stray_git_commit,
)


def test_the_real_tree_has_no_stray_git_commit():
    assert check_no_stray_git_commit() == []


def test_the_real_tree_uses_only_subprocess():
    assert check_execution_surfaces() == []


def test_the_real_tree_has_no_shell_true():
    assert check_no_shell_true() == []


def test_a_stray_subprocess_git_commit_is_caught():
    from tests.fencing.sink_guard import _stray_git_commit_in

    src = (
        "import subprocess\n"
        "def f(d):\n"
        "    subprocess.run(['git', 'commit', '-m', 'x'], cwd=d)\n"
    )
    assert _stray_git_commit_in(src, "x/rogue.py")


def test_os_system_is_caught():
    from tests.fencing.sink_guard import _execution_surfaces_in

    assert _execution_surfaces_in("import os\ndef f():\n    os.system('git commit')\n", "x/r.py")


def test_shell_true_is_caught():
    from tests.fencing.sink_guard import _shell_true_in

    src = "import subprocess\ndef f():\n    subprocess.run('git commit -m x', shell=True)\n"
    assert _shell_true_in(src, "x/r.py")
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/fencing/test_sink_guard.py -q
```
Expected: `ModuleNotFoundError: tests.fencing.sink_guard`.

- [ ] **Step 3: Implement**

Create `tests/fencing/sink_guard.py` with:

- `_stray_git_commit_in(source, path) -> bool` — any `subprocess.*` call that commits,
  **outside the exact sink function**.

  **Exempt the function, never the file.** An earlier draft exempted
  `citations/gitio.py` and `phases/setup.py` wholesale, which would let a NEW committing
  function added inside either file escape every guard. Task 5's `GIT_SINKS` carries
  `(path_suffix, qualname, kind)` precisely so this can key on the qualname.

  **Fail closed on unprovable argv.** A literal-list matcher misses
  `subprocess.run(argv)`, `subprocess.run(("git", "commit"))` and
  `subprocess.run(base + ["commit"])`. Outside a sink function, any `subprocess.*` call
  whose argv the checker **cannot prove is not a commit** is reported. That over-reports
  the repo's existing non-git subprocess calls, so seed an explicit
  `PROVEN_NON_COMMIT` allowlist of `(path, qualname)` from the Step 3b probe below — a
  reviewed list, like every other pin in this suite.

  **Pin the CALL, not the function.** A `(path, qualname)` entry waives every subprocess
  call in that function, so adding a second `base + ["commit"]` call inside an
  already-pinned read helper would be silently exempt. Key each entry on a call-level
  fingerprint — `(path, qualname, lineno_offset_within_function, argv_repr)` — and add a
  stale-pin test asserting every entry still corresponds to a call the guard reports.

  **Resolve aliases for EVERY execution surface, not just `subprocess`**:
  `import subprocess as sp`, `from subprocess import run`, `from os import system`,
  `import asyncio as aio`. A bare attribute match on the module name sees none of them, and
  `check_execution_surfaces` is as alias-blind as `check_no_stray_git_commit` was.
- `_execution_surfaces_in(source, path) -> bool` — any `os.system`, `os.popen`, or
  `asyncio.create_subprocess_*` call.
- `_shell_true_in(source, path) -> bool` — any `subprocess.*` call with `shell=True`
  outside a declared sink file.
- Three `check_*()` functions walking `src/**/*.py` (skip `__pycache__`) and returning a
  list of `path::qualname` strings.

Also assert **no git library is importable or declared**, which is two checks, not one:

- **AST**: no `import git`, `import pygit2`, `import dulwich`, or `from <any of those>
  import ...` anywhere in `src/`. Spec §13 criterion 2 asks for a git-library import to fail
  the guard, and a dependency-manifest check alone does not catch a vendored or
  transitively-available module.
- **Manifests**: reject `gitpython`, `pygit2`, `dulwich` (case-insensitive) in **both
  `pyproject.toml` and `uv.lock`**. `pyproject.toml` alone
misses a transitive dependency, and a transitive GitPython is just as capable of
committing. Verified absent from both today.

Match on parsed AST call nodes only. Never on source text.

- [ ] **Step 4: Seed the `PROVEN_NON_COMMIT` allowlist from the tree**

Fail-closed means the guard first reports every subprocess call it cannot prove innocent.
Derive the list; do not guess it.

```bash
uv run python - <<'PY'
from tests.fencing.sink_guard import check_no_stray_git_commit
for p in sorted(check_no_stray_git_commit()):
    print(p)
PY
```

Read each hit and confirm from the source that it does not commit — `reconcile.py`,
`tools/history.py` and `citations/core.py` all run git for reads. Paste the confirmed ones
into `PROVEN_NON_COMMIT` with a one-line reason each. **If you cannot tell what a call
runs, leave it in the report rather than allowlisting it** and record it under PLAN DEFECTS.

- [ ] **Step 5: Run the tests**

```bash
uv run pytest tests/fencing/test_sink_guard.py -q
```
Expected: **6 passed**.

- [ ] **Step 6: Mutation — plant a real violation**

```bash
cp src/call_manager/proposal_assist/reconcile.py /tmp/reconcile.bak
```
Add to a function in `reconcile.py` (it already imports `subprocess`):

```python
    subprocess.run(["git", "commit", "-m", "planted"], cwd=repo_dir, check=False)
```

```bash
uv run pytest tests/fencing/test_sink_guard.py -q
```
Expected: **`test_the_real_tree_has_no_stray_git_commit` FAILED**, naming
`reconcile.py`. FAILED, not ERROR.

```bash
cp /tmp/reconcile.bak src/call_manager/proposal_assist/reconcile.py
uv run pytest tests/fencing/test_sink_guard.py -q   # 6 passed
```

- [ ] **Step 7: Full suite + lint, then commit**

```bash
uv run pytest -n 16 --dist loadfile -q && uv run ruff check .
git add tests/fencing/sink_guard.py tests/fencing/test_sink_guard.py
git commit -m "test(fence): a third way to run git stops the build (F3e1a Task 7)"
```
Expected: **3841 passed, 1 skipped** (3834 + 7).

---

## Task 8: The gate over the tree

The gate that would have caught spec revision 1's defect: converting the shared
`checkpoint_artifact` while five kernel operations still take `_live` at an outer boundary
and mutate before reaching it.

**Files:**
- Create: `tests/fencing/live_mutex_gate.py`
- Create: `tests/fencing/test_live_mutex_gate.py`

**The rule:** every function `git_fence.unprotected_sinks()` reports over `src/` must be
pinned. That is the **same function** Task 6's measurement subtracts from — not a separate
rule, and not a `_live`-only rule.

An earlier draft had Task 8 build its own call graph and reject only functions still taking
`_live`. Two holes: deleting an outer `_live` **without adding** `_at_generation` passed
once its pin was removed, and a bare-name call graph does not "only over-report" — an alias
or a rebound helper **hides** an edge, so it can under-report too.

So the gate is **positive and direct**: a function holding a sink must be provably fenced or
explicitly pinned. Direct sink holders come from `sink_calls`, which already resolves
aliases, so nothing is hidden.

**Transitive callers are NOT covered by this gate, and the plan says so rather than
implying otherwise.** `apply_citation_to_room` mutates the room and then calls
`checkpoint_artifact`, which holds the sink; a per-function checker cannot see across that
boundary. Those seven enclosing acquisitions are spec §4.2's list, they convert in
**F3e1b**, and their proof is the §5.2 runtime race per entry point — not this gate. Record
them in a separate `ENCLOSING_ACQUISITIONS` constant here so the list is written down and
`F3e1b` inherits it, but do not pretend the static gate proves anything about them.

- [ ] **Step 1: Build the call graph and the gate**

`live_mutex_gate.py` provides:

```python
#: Functions holding a git sink that the conservative checker cannot prove fenced.
#:
#: **A pin here does NOT mean the site is broken.** git_fence under-reports BY DESIGN
#: (Task 5): a correct acquisition inside a `try:` measures unfenced. Three sites are
#: exactly that shape and will still be pinned after F3e1b converts them correctly,
#: carrying a human sentence and a §5.2 runtime proof instead.
#:
#: This list SHRINKS; an addition is a reviewed diff, not a formality. Entries leave
#: across THREE slices, not one: the citation/checkpoint/save sites in F3e1b, the
#: annotation and bibliography sites in F3e2, and phase 0's in F5. A comment claiming
#: they all leave in F3e1b would make a reader treat a still-pinned F5 site as a bug.
UNCONVERTED_LIVE_CALLERS: frozenset[tuple[str, str]] = frozenset({
    # (path, qualname) — fill from the Step 2 probe, do not guess
})
```

and `check_tree() -> list[str]`, which:

1. builds a module-level static call graph over `src/` (qualname → callee bare names);
2. marks every function reaching a `GIT_SINKS` name transitively;
3. reports any marked function containing an `acquire_commit_mutex_live` call, unless its
   `(path, qualname)` is pinned.

A bare-name call graph over-approximates (two same-named helpers in different modules
merge). **That is the safe direction** — it can only over-report, and an over-report is a
pin entry a human reads. Say so in the module docstring.

- [ ] **Step 2: Derive the pin list from the tree, do not guess**

```bash
uv run python - <<'PY'
from tests.fencing.live_mutex_gate import check_tree
for p in sorted(check_tree()):
    print(p)
PY
```

Expect every function holding a sink that is not provably fenced — which, before any
conversion, is **all nine**. Separately, `ENCLOSING_ACQUISITIONS` records spec §4.2's seven: `apply_citation_to_room`, `kernel_accept_prose`, `kernel_init_spec`,
`kernel_accept_citation`, `apply_annotation_insert_to_room`, `post_lock_spec`,
`post_unlock_spec`, `checkpoint_artifact`, `_insert_citation_locked`,
`insert_citation_for_user`, `save_section`, `insert_from_annotation`, and
**`post_init_spec._git`**, which takes `_live` at `workspaces.py:540` directly before its
sink at `:557`.

**`_init_repo` IS expected in the pin list** — it holds the sink and no top-level
acquisition, so the checker reports it. **`_m1` belongs in `ENCLOSING_ACQUISITIONS`**: it
takes `_live` (`setup.py:363`) and calls `self._init_repo(ws.id)` (`:375`), which is the
enclosing-acquisition shape, and its membership must not be conditional on how a call graph
resolves `self.` — it is written down. An earlier draft got both backwards and omitted
`post_init_spec._git`. **Record the actual list** from the probe; a difference is
information, not a reason to edit the expectation silently.

Paste the real output into `UNCONVERTED_LIVE_CALLERS`, then assert the gate is green.

- [ ] **Step 3: Write the tests**

**This block is a FRAGMENT** — three tests to add to a module whose header imports
`check_tree` and `UNCONVERTED_LIVE_CALLERS` from `tests.fencing.live_mutex_gate`. Write
that header.

```python
def test_the_tree_has_no_unpinned_live_caller():
    assert check_tree() == []


def test_removing_a_pin_reddens_the_gate():
    """The gate is only a gate if the pin list is load-bearing. Drop one entry and
    the site it covered must be reported."""
    import tests.fencing.live_mutex_gate as g

    victim = sorted(g.UNCONVERTED_LIVE_CALLERS)[0]
    original = g.UNCONVERTED_LIVE_CALLERS
    try:
        g.UNCONVERTED_LIVE_CALLERS = original - {victim}
        problems = g.check_tree()
        assert any(victim[1] in p for p in problems), (victim, problems)
    finally:
        g.UNCONVERTED_LIVE_CALLERS = original


def test_no_pin_is_stale():
    """The other direction, and the one that makes the list shrink. A pin whose
    function the checker now proves fenced — or which no longer holds a sink at all —
    must LEAVE the list, or it silently exempts a site nobody is watching.

    The predicate is "still reported by unprotected_sinks", NOT "still takes _live":
    a function that dropped _live without adding _at_generation is still unfenced and
    must stay pinned. Mirrors test_no_exemption_is_stale in the import gate."""
    import tests.fencing.live_mutex_gate as g

    reported = g.all_unprotected()          # {(path, qualname)} over src/
    stale = sorted(p for p in g.UNPROVEN_SINKS if p not in reported)
    assert not stale, f"pins for sites the checker no longer reports: {stale}"


def test_a_transitive_reacher_is_caught_not_just_a_direct_caller():
    """The defect this gate exists for: an OUTER function takes _live and mutates,
    then calls a helper that reaches the sink. A direct-caller-only rule misses it."""
    # drive check_source-style helpers over a synthetic two-function module
```

Write the third test against whatever synthetic entry point `live_mutex_gate.py` exposes —
if it only offers `check_tree()`, add a `check_sources(mapping)` taking `{path: source}` so
the transitive case is testable without touching `src/`. **A gate whose central claim is
only tested against the real tree cannot be tested at all once the tree is converted.**

- [ ] **Step 4: Mutation**

Copy, then change `checkpoint_artifact`'s `acquire_commit_mutex_live` to
`acquire_commit_mutex_at_generation(session, workspace_id, generation=0)` and remove its
pin. `check_tree()` must stop reporting it — proving the gate keys on the call, not on the
name being in a list. Restore from the copy.

- [ ] **Step 5: Full suite + lint, then commit**

```bash
uv run pytest -n 16 --dist loadfile -q && uv run ruff check .
git add tests/fencing/live_mutex_gate.py tests/fencing/test_live_mutex_gate.py
git commit -m "test(fence): no _live may reach a git sink (F3e1a Task 8)"
```
Expected: **3845 passed, 1 skipped** (3841 + 4). If the transitive fixture needed a
fifth test, say so and carry it forward.

---

## Task 9: Confirm the slice claimed exactly what it did

**Files:**
- Create: `tests/fencing/test_f3e1a_slice.py`

- [ ] **Step 1: Write the slice's own claims**

```python
"""Fence F3e1a — what this slice did, and what it deliberately did not."""

from __future__ import annotations


def test_the_carrier_has_no_production_callers():
    """F3e1a ships the carrier; F3e1b uses it. A caller here means a conversion
    leaked into the spine slice.

    AST, not a regex: `def stamp_for_workspace(` matches a name regex, so a
    text scan would report the DEFINITION as its own caller and could never pass.
    Alias-resolving too, since `import ... as` would evade a bare name match.
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[2] / "src"
    hits = []
    for f in src.rglob("*.py"):
        if "__pycache__" in f.parts:
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        names = {"stamp_for_workspace"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names |= {a.asname for a in node.names
                          if a.name == "stamp_for_workspace" and a.asname}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                called = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                if called in names:
                    hits.append(f.relative_to(src).as_posix())
    assert hits == [], f"F3e1a converts nothing, but these call the carrier: {hits}"


def test_no_git_site_is_fenced_yet():
    from tests.fencing.inventory import load_inventory

    git = [r for r in load_inventory() if r["kind"] == "git_commit"]
    assert len(git) == 9
    assert not any(r["fenced"] for r in git)


def test_every_git_row_is_classified_and_allocated():
    from tests.fencing.inventory import UNCLASSIFIED, load_inventory

    for row in load_inventory():
        if row["kind"] == "git_commit":
            assert row["category"] == "semantic", row
            assert row["slice"] != UNCLASSIFIED, row


def test_the_pins_did_not_move():
    """F3e1a retires no exemption. Four modules are held by repo_dir and
    references.py by session_factory; both retire in F3f."""
    from tests.fencing.inventory import TOOLS_NOT_YET_CONVERTED as T

    assert len(T) == 5
    assert sum(any("repo_dir" in v for v in vs) for vs in T.values()) == 4
    assert any("session_factory" in v for v in T["references.py"])
```

- [ ] **Step 2: Prove the UNCLASSIFIED default refuses a new site**

Add a test that a newly discovered git site lands `UNCLASSIFIED` and the slice gate refuses
it. Drive `merge()` with a synthetic `Site(KIND_GIT_COMMIT, "src/x.py", "f")` added to the
scanned set and assert the resulting row's slice is `UNCLASSIFIED` — then assert
`test_each_slice_matches_its_declared_status`'s rule rejects it. **Do not** edit `src/` to
create the site.

- [ ] **Step 3: Full suite, lint, and the criteria**

```bash
uv run pytest -n 16 --dist loadfile -q
uv run ruff check .
uv run pytest tests/fencing -q
```
Expected: **3850 passed, 1 skipped** if every derived step matched; otherwise report your
arithmetic against the run. `0 skipped` means a live API call ran.

Then confirm each spec §13 `F3e1a` criterion with the command that proves it, and write the
result. Criterion 6 — "no production behaviour changes" — is proven by:

```bash
git diff af7b1909..HEAD --stat -- src/
```
Expected: **`ports/stamp.py` only.** Any other `src/` file in that diff is a conversion that
does not belong in this slice.

- [ ] **Step 4: Commit**

```bash
git add tests/fencing/test_f3e1a_slice.py
git commit -m "test(fence): F3e1a shipped a carrier and a detector, and converted nothing (F3e1a Task 9)"
```

---

## What this plan deliberately does not do

- **Converts no site.** All nine git rows end `fenced: false`. `F3e1b` converts.
- **Calls the carrier from nothing.** Task 9 asserts it.
- **Retires no pin.** Pins stay at 5.
- **Touches no route, kernel, or effect module.** The only `src/` edit is Task 1.
- **Does not add a generation column to `ProposedChange`.** Whether accepting a proposal
  created at G should be refused after a delete/restore to G+2 is a product question the
  spec defers with no owner.
