# Fence F3d3a — The Citation Core Relocation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teach the fencing machinery that an effect keeps its identity when its code leaves `tools/`, then move the session- and model-dependent citation core out of `tools/citations.py` into `citations/core.py` — a pure relocation with **zero behaviour change**.

**Architecture:** Spec §7 calls F3d "a module-level dependency migration, not five site conversions". This is that migration, and nothing else: no tool calls a port, no impl changes behaviour, no inventory row flips `fenced`. It is split from the conversions (F3d3b) deliberately — a large mechanical move and two semantic conversions in one diff is the combination that makes a review unable to tell which change broke what. A pure move is verifiable by diffing the moved bytes; a conversion is not.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, pytest.

**Spec:** `docs/superpowers/specs/2026-08-18-fence-session-ownership-design.md` (revision 8) §7's F3d subsection (lines 456-475). This plan implements its first clause — "moves the whole session/model-dependent citation core out of `tools/` into the effects/domain layer and updates **every non-tool consumer**". The clause after it ("then converts …") is F3d3b's.

---

## What the survey found, and what it changed

Seven readers mapped this surface and seven adversarial verifiers re-checked every claim against the code. Three findings reshaped the slice, and each was independently reproduced before this plan was written.

**1. F3d3 cannot retire either exemption. Pins stay at 5.**
The early scoping note assumed 5 → 3. It is wrong. `citations.py` carries **28** gate violations, of which **9 are `repo_dir`**; `proposal.py` carries **14 raw / 9 normalized**, of which **4 are `repo_dir`**. `repo_dir` is a filesystem locator and it retires with the other capability holders in **F3f** — no amount of citation work removes it. Measured directly:

```
citations.py: 28 raw, 19 normalized — locators {repo_dir: 9, session_factory: 2}
proposal.py:  14 raw,  9 normalized — locators {repo_dir: 4, session_factory: 3}
```

What this slice actually buys is a large **shrink**: moving the closure takes four `repo_dir` namings (`:121`, `:126`, `:135`, `:140`) and two more (`:836`, `:845`) out of `tools/`, leaving three. That is real progress toward F3f, and it is not the same thing as retiring a pin. Say so plainly rather than implying the exemption list moves.

**2. `RENAMES` cannot carry a row out of `tools/` — the mechanism refuses the only move this slice makes.**
`regenerate_inventory.py` offers `RENAMES` "for when you rename a fenced site". But `merge()` validates every target against `found_ids`:

```python
if new not in found_ids:
    bad.append(f"target not found in the scan: {new}")
```

and `found_ids` comes from `find_sites`, which records a `tool_commit` only when `/proposal_assist/tools/` is in the path. Proved directly — same source text, two paths:

```
tools/citations.py    -> {Site(kind='tool_commit', ..., symbol='_write_citekey_cache')}
citations/core.py     -> set()
```

So a rename pointing outside `tools/` raises `BadRenameMap`. **Task 1 has to widen the scan before Task 3 moves anything.** This is the third time this project has had to fix detection before it could convert (F2→F3, then F3d2's Tasks 1-2). The chosen fix follows the architecture's own rule — *the audit unit is the EFFECT, not the mutex site* — so an effect keeps a row wherever it lives.

**3. Exactly ONE inventory row relocates, not three.**
The early reading feared all three F3d3 rows would vanish. They do not. Counting `.commit()`/`.begin()` sites per function across the whole file:

| function | sites | fate |
|---|---|---|
| `propose_citation_impl` | `:563` | **stays** in `tools/` — F3d3b converts it |
| `_write_citekey_cache` | `:757`, `:761` | **moves** — this is the one row to carry |

`_propose_artifact_edit_impl` (`proposal.py:184`) is untouched by this slice. `_resolve_citekey_for_ref`, `compute_citation_insert`, `insert_citation_impl` and `_insert_citation_locked` contain **no commit sites at all** and own no rows — they move silently and correctly.

`_write_citekey_cache`'s row is `category: caller_fenced`, `fenced_by: _insert_citation_locked`. Both move to the same file, so `test_caller_fenced_entries_name_a_resolvable_ancestor` — which parses `REPO / row["path"]` and checks the ancestor resolves *in that file* — stays green at the new path.

**Also confirmed and load-bearing:** `_write_citekey_cache`'s `owns_txn=False` branch uses `session.begin_nested()`, a SAVEPOINT. F3d2's `_RAW_COMMIT_ATTRS = {"commit", "begin"}` matches `begin` exactly, **not** `begin_nested`, so the savepoint is correctly not read as a raw commit. Do not "fix" that.

---

## Global Constraints

Every task's requirements implicitly include this section.

- **GC-1 — This is a MOVE. Zero behaviour change.** Every moved function keeps its name, its signature, its docstring and its body byte-for-byte. No tool calls a port. No inventory row flips `fenced`. No exemption retires — `TOOLS_NOT_YET_CONVERTED` still has **five** keys at the end. If you find yourself improving a moved function, stop: that is F3d3b's or F3f's, and it destroys this slice's one review property — that the move is verifiable by diffing bytes.
- **GC-2 — Detection widens before code moves.** Task 1 must be committed before Task 3. Moving first raises `BadRenameMap` at regeneration, or silently drops the row if you skip the rename — and `test_every_f3d_identity_is_still_in_the_inventory` is then the only thing standing between you and a slice that declares itself complete over a shrinking set.
- **GC-3 — The relocated module is NOT under the import gate.** `citations/core.py` lives outside `tools/`, so it may import `sqlalchemy`, `db.models` and `ToolContext` freely. That is the entire point of moving it. Do NOT try to make it gate-clean, and do NOT change a moved signature to avoid naming `ToolContext`.
- **GC-4 — No re-export shim from `tools/citations.py`.** Every consumer is repointed at `citations/core.py` directly. A shim would keep the import (and its violation) in `tools/`, defeating the shrink, and would let a stale monkeypatch keep landing on a name nothing reads. The one exception is the handful of names the *remaining* thin tools in `tools/citations.py` genuinely still call — those are imported back, explicitly, and pinned.
- **GC-5 — The four `monkeypatch.setattr(citations_mod, ...)` sites are the sharpest hazard in this slice.** Patching a module attribute patches the name *as that module sees it*. When a function moves, a patch left pointing at the old module lands on nothing — and **two of the four are NEGATIVE tests** (a forced citekey collision at `test_accepting.py:591`, a forced `GitCommitError` at `:655`) that would go GREEN for the wrong reason. Task 5 repoints them and adds a guard that makes the class of mistake detectable.
- **GC-6 — Test command.** `uv run pytest -n 16 --dist loadfile`. Never `-n auto` (resolves to 72 here) and never `--dist loadgroup`. Baseline on master before this branch: **3771 passed, 1 skipped** (`f9014b68`). The `1 skipped` is an `ANTHROPIC_API_KEY`-gated test — read it as the signal, not the total. `tests/fencing` alone is 97 tests in ~5s.
- **GC-6a — If you work in a worktree, `uv run` is a trap.** A fresh worktree has no `.venv` and `requires-python = ">=3.12"`, so bare `uv sync` resolves to Python 3.14. Use `uv sync --python 3.12`, then confirm with
  `.venv/bin/python -c "import call_manager, pathlib; print(pathlib.Path(call_manager.__file__).resolve())"`
  that it points at THIS worktree's `src/`. If it points at the main tree, your edits do nothing while the tests still pass.
- **GC-7 — `ruff check .` is the lint gate. `ruff format` is NOT** — do not run it and do not reformat files to satisfy it. Note `ruff` will reorder imports (rule `I001`) in every file Task 4 and Task 5 touch; let it, via `--fix`, and review the result.
- **GC-8 — Mutation testing happens AFTER the task's commit, never before.** `git checkout <path>` cannot restore a file the task itself created, and it also destroys any *uncommitted* fix in a file you are mutating. So commit the green implementation first, then mutate, then:

  ```bash
  git checkout <path>
  git diff --exit-code HEAD -- <path>
  ```

  Never `git status --porcelain`: it exits 0 whether the tree is clean or filthy. When a file has uncommitted work, restore from a copy instead of `git checkout`.
- **GC-9 — A mutation must fail for the reason you claim.** Confirm the NAMED test failed with the NAMED message. A mutation that merely goes red proves nothing — three of F3d1's failed for unrelated reasons and were only caught by running them.
- **GC-10 — The exemption sets shrink, never grow.** `TOOLS_NOT_YET_CONVERTED` is a subset check and `test_no_exempt_module_acquires_a_new_violation` is armed. `tools/audit.py` imports a relocating helper, so repointing it produces a NEW normalized violation string — audit.py's pinned set must be edited in the SAME commit.

---

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `src/call_manager/proposal_assist/citations/core.py` | The session- and model-dependent citation core: 11 functions moved verbatim out of `tools/citations.py`, plus the 7 module constants they read. Free to import `sqlalchemy`, `db.models` and `ToolContext`. |
| `tests/fencing/fixtures/relocated_effect.py` | A commit site at a path outside `tools/` — the positive fixture for the widened scan. |
| `tests/fencing/fixtures/unrelated_domain_commit.py` | A commit site outside `tools/` and outside the declared relocated set — the negative fixture that keeps the widening from becoming "scan everything". |
| `tests/proposal_assist/test_f3d3a_relocation.py` | The slice's own tests: identity of the moved objects, the patch-target guard, and the no-behaviour-change assertions. |

**Modified:**

| File | Change |
|---|---|
| `tests/fencing/detectors.py` | `RELOCATED_EFFECT_PATHS` + `_in_effect_scope`; `find_sites` and `find_fenced_tool_commits` both use it instead of the bare `_TOOLS_DIR` test. |
| `tests/fencing/inventory.py` | `NON_SEMANTIC_PINS` key for `_write_citekey_cache` moves to the new path; `TOOLS_NOT_YET_CONVERTED` shrinks for `citations.py` and changes for `audit.py`. |
| `tests/fencing/regenerate_inventory.py` | `RENAMES` filled for ONE run, then cleared. |
| `tests/fencing/fence_inventory.json` | `_write_citekey_cache`'s `path` becomes `citations/core.py`. Nothing else changes. |
| `src/call_manager/proposal_assist/tools/citations.py` | Loses 11 functions and 7 constants; imports back only what its remaining thin tools call. |
| `analysis/insert.py`, `citations/human_cite.py`, `collab/kernel.py`, `proposals/accepting.py`, `web/api/annotations.py`, `tools/audit.py` | Import the moved names from `citations/core.py`. |
| 14 test files (19 import statements, 4 monkeypatch targets) | Same repoint. |

**Deliberately NOT done here:** no port is called and no impl converts (F3d3b owns `propose_citation_impl` and `_propose_artifact_edit_impl`); no exemption retires (both are blocked on `repo_dir`, which is F3f's); `tools/proposal.py` is not touched at all; `CITATIONS_FILENAME`'s duplicate in `tools/citations.py:80` stays until F3d3b, exactly as `ports/artifacts.py:35-38` says.

---

### Task 1: An effect keeps its identity when it leaves `tools/`

`find_sites` records a `tool_commit` only when `/proposal_assist/tools/` is in the path (`detectors.py:28`), and `merge()` rebuilds rows only from what the scan found. So relocating a site deletes its row, and `RENAMES` cannot carry it because the target is not in `found_ids`. This slice's entire deliverable is a relocation, so this task lands first.

The fix follows the architecture's own rule — *the audit unit is the EFFECT, not the mutex site*. A declared, explicit set of relocated-effect paths joins `tools/` in the scan. Explicit rather than "anything under `proposal_assist/`", because widening to the whole package would mint dozens of `UNCLASSIFIED` rows for every `.commit()` in the domain layer and turn the gate into noise.

**Files:**
- Create: `tests/fencing/fixtures/relocated_effect.py`
- Create: `tests/fencing/fixtures/unrelated_domain_commit.py`
- Modify: `tests/fencing/detectors.py`
- Modify: `tests/fencing/test_detectors.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `RELOCATED_EFFECT_PATHS: frozenset[str]` in `tests/fencing/detectors.py` — repo-relative POSIX paths.
  - `_in_effect_scope(norm: str) -> bool`, used by BOTH `find_sites` and `find_fenced_tool_commits`.

- [ ] **Step 1: Write the fixtures**

Create `tests/fencing/fixtures/relocated_effect.py`:

```python
"""POSITIVE fixture: an effect that has moved OUT of tools/.

Its source is handed to find_sites with the path of a DECLARED relocated-effect
module. The commit is still a tool effect — it just does not live in tools/ any
more — so it must still own an inventory row.
"""


def _write_citekey_cache(session, ref, citekey):
    ref.citekey = citekey
    session.commit()
```

Create `tests/fencing/fixtures/unrelated_domain_commit.py`:

```python
"""NEGATIVE fixture: an ordinary domain-layer commit.

Outside tools/ AND outside the declared relocated set. The domain layer commits
constantly and none of it is a tool effect; if this fixture ever starts producing
a site, the widening has become "scan everything" and the inventory becomes noise
nobody reads.
"""


def save_thing(session, thing):
    session.add(thing)
    session.commit()
```

- [ ] **Step 2: Write the failing test**

Append to `tests/fencing/test_detectors.py`:

```python
def test_a_declared_relocated_effect_still_owns_a_site():
    """The whole reason Task 1 exists. F3d3a moves the citation core out of tools/,
    and find_sites scoped tool_commit detection to tools/ alone — so the move would
    delete the row, and RENAMES cannot carry it because merge() validates the target
    against what the scan found."""
    import pathlib

    from tests.fencing.detectors import KIND_TOOL_COMMIT, find_sites

    src = (
        pathlib.Path(__file__).parent / "fixtures" / "relocated_effect.py"
    ).read_text(encoding="utf-8")
    path = "src/call_manager/proposal_assist/citations/core.py"
    assert (KIND_TOOL_COMMIT, "_write_citekey_cache") in {
        (s.kind, s.symbol) for s in find_sites(src, path)
    }


def test_an_ordinary_domain_commit_is_not_a_site():
    """The widening is a DECLARED list, not a directory sweep. The domain layer
    commits constantly; recording all of it would mint dozens of UNCLASSIFIED rows
    and the gate would stop being read."""
    import pathlib

    from tests.fencing.detectors import find_sites

    src = (
        pathlib.Path(__file__).parent / "fixtures" / "unrelated_domain_commit.py"
    ).read_text(encoding="utf-8")
    assert find_sites(src, "src/call_manager/proposal_assist/editing/save.py") == set()


def test_the_relocated_scope_is_declared_and_currently_holds_the_citation_core():
    """The set is source, not inference. A path enters it when a slice moves an
    effect out of tools/, and the slice that moves it is the one that adds it."""
    from tests.fencing.detectors import RELOCATED_EFFECT_PATHS

    assert RELOCATED_EFFECT_PATHS == frozenset(
        {"src/call_manager/proposal_assist/citations/core.py"}
    )


def test_the_fenced_measurement_uses_the_same_scope():
    """find_sites and find_fenced_tool_commits must agree on what is in scope. If
    only one widened, a relocated effect would own a row whose fenced flag could
    never be measured — permanently false, and silently so."""
    import pathlib

    from tests.fencing.detectors import find_fenced_tool_commits

    src = (
        pathlib.Path(__file__).parent / "fixtures" / "relocated_effect.py"
    ).read_text(encoding="utf-8")
    path = "src/call_manager/proposal_assist/citations/core.py"
    # It commits directly, so it is NOT fenced — but the function must have looked,
    # which an out-of-scope path would not have done.
    assert find_fenced_tool_commits(src, path) == set()
    assert find_fenced_tool_commits(src.replace("session.commit()", "ports.effects.submit_review(a, b)"), path) == {
        (path, "_write_citekey_cache")
    }
```

- [ ] **Step 3: Run the tests to verify they fail**

Run:
```bash
uv run pytest tests/fencing/test_detectors.py -q -k "relocated or ordinary_domain or same_scope" > /tmp/f3d3a-1.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3a-1.txt
```
Expected: `PYTEST_EXIT=1`, `ImportError: cannot import name 'RELOCATED_EFFECT_PATHS'` for the third test and assertion failures for the first and fourth.

- [ ] **Step 4: Widen the scope**

In `tests/fencing/detectors.py`, beside `_TOOLS_DIR`:

```python
#: Modules OUTSIDE tools/ that hold effects relocated out of it.
#:
#: An effect does not stop being a tool effect because its code moved — "the audit
#: unit is the EFFECT, not the mutex site". Without this, relocating a site deletes
#: its inventory row (merge() rebuilds only from the scan) and RENAMES cannot carry
#: it, because merge() validates every rename target against found_ids.
#:
#: DECLARED, never inferred. Widening to all of proposal_assist/ would record every
#: domain-layer commit as an UNCLASSIFIED site and the gate would stop being read.
#: A path enters this set in the SAME slice that moves an effect into it.
RELOCATED_EFFECT_PATHS: frozenset[str] = frozenset({
    "src/call_manager/proposal_assist/citations/core.py",  # F3d3a
})


def _in_effect_scope(norm: str) -> bool:
    """Is this path one where a tool effect may live? tools/, or a declared
    relocation target."""
    return _TOOLS_DIR in norm or norm in RELOCATED_EFFECT_PATHS
```

Then replace the scope test in BOTH places. In `find_sites`, the `.commit()` branch and the port-write branch each currently test `_TOOLS_DIR in norm`; both become `_in_effect_scope(norm)`. In `find_fenced_tool_commits`, the early return becomes:

```python
    norm = path.replace("\\", "/")
    if not _in_effect_scope(norm):
        return set()
```

Leave the `ToolContext` branch of `find_sites` alone — a `tool_context` row is about where a context is CONSTRUCTED, which is a different question and is not what this slice moves.

- [ ] **Step 5: Run the tests**

Run:
```bash
uv run pytest tests/fencing -q > /tmp/f3d3a-1.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3a-1.txt
uv run python -m tests.fencing.regenerate_inventory && git diff --exit-code tests/fencing/fence_inventory.json && echo "INVENTORY UNCHANGED"
```
Expected: `PYTEST_EXIT=0`, 101 passed (97 + 4). The regeneration must leave the JSON **byte-identical** — `citations/core.py` does not exist yet, so widening the scope to a non-existent path can change nothing. If the file moves, the widening caught something unintended: stop and report what.

- [ ] **Step 6: Commit**

```bash
uv run ruff check .
git add tests/fencing/detectors.py tests/fencing/test_detectors.py tests/fencing/fixtures/relocated_effect.py tests/fencing/fixtures/unrelated_domain_commit.py
git commit -m "test(fencing): an effect keeps its identity when it leaves tools/ (F3d3a Task 1)"
```

- [ ] **Step 7: Prove the scope is what makes the relocation survivable**

In `tests/fencing/detectors.py`, empty the set: `RELOCATED_EFFECT_PATHS: frozenset[str] = frozenset()`.

Run:
```bash
uv run pytest tests/fencing/test_detectors.py -q > /tmp/f3d3a-1m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/f3d3a-1m.txt
```
Expected: FAIL on `test_a_declared_relocated_effect_still_owns_a_site` (the identity missing from the recorded set), on `test_the_relocated_scope_is_declared_and_currently_holds_the_citation_core`, and on `test_the_fenced_measurement_uses_the_same_scope`. `test_an_ordinary_domain_commit_is_not_a_site` must still PASS — it is the guard against over-widening and this mutation narrows.

```bash
git checkout tests/fencing/detectors.py
git diff --exit-code HEAD -- tests/fencing/detectors.py
```

---

### Task 2: `RENAMES` can now carry a row across the boundary

Task 1 widened the scan; this task proves the consequence the move depends on, and pins the limit of it. Without this, Task 3 discovers at regeneration time whether the mechanism works — and `BadRenameMap` aborts the write, leaving a stale JSON that still looks right.

**Files:**
- Modify: `tests/fencing/test_inventory_merge.py`

**Interfaces:**
- Consumes: `RELOCATED_EFFECT_PATHS`, `_in_effect_scope` (Task 1); `merge`, `BadRenameMap` (existing).
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

Append to `tests/fencing/test_inventory_merge.py`:

```python
def test_a_rename_can_move_a_row_into_the_relocated_scope():
    """The mechanism F3d3a's move depends on. Before Task 1, merge() rejected any
    rename whose target lived outside tools/ — `target not found in the scan` —
    because find_sites never recorded one. That made RENAMES useless for the only
    move this slice makes."""
    from tests.fencing.detectors import KIND_TOOL_COMMIT, Site
    from tests.fencing.inventory import merge

    old = ("tool_commit", "src/call_manager/proposal_assist/tools/citations.py", "_write_citekey_cache")
    new = ("tool_commit", "src/call_manager/proposal_assist/citations/core.py", "_write_citekey_cache")
    previous = [
        {
            "kind": old[0], "path": old[1], "symbol": old[2],
            "category": "caller_fenced", "fenced": False, "slice": "F3d3",
            "fenced_by": "_insert_citation_locked",
        }
    ]
    found = {Site(KIND_TOOL_COMMIT, new[1], new[2])}

    rows = merge(previous, found, {old: new}, set())

    assert len(rows) == 1
    assert rows[0]["path"] == new[1]
    # The human-owned fields ride along — that is the point of a rename.
    assert rows[0]["category"] == "caller_fenced"
    assert rows[0]["slice"] == "F3d3"
    assert rows[0]["fenced_by"] == "_insert_citation_locked"


def test_a_rename_into_an_undeclared_path_is_still_refused():
    """The limit. A rename is not a way to park a row anywhere — the target has to
    be somewhere the scan actually looks, which is tools/ plus the declared
    relocated set. Otherwise a row could be 'carried' to a path nothing ever
    re-checks, and the identity would be preserved in name only."""
    import pytest

    from tests.fencing.detectors import KIND_TOOL_COMMIT, Site
    from tests.fencing.inventory import BadRenameMap, merge

    old = ("tool_commit", "src/call_manager/proposal_assist/tools/citations.py", "_write_citekey_cache")
    new = ("tool_commit", "src/call_manager/proposal_assist/editing/save.py", "_write_citekey_cache")
    previous = [
        {
            "kind": old[0], "path": old[1], "symbol": old[2],
            "category": "caller_fenced", "fenced": False, "slice": "F3d3",
            "fenced_by": "_insert_citation_locked",
        }
    ]
    # The scan did not find it there, because editing/save.py is not in scope.
    with pytest.raises(BadRenameMap, match="target not found in the scan"):
        merge(previous, set(), {old: new}, set())
```

- [ ] **Step 2: Run the tests to verify they behave as claimed**

Run:
```bash
uv run pytest tests/fencing/test_inventory_merge.py -q -k "relocated_scope or undeclared_path" > /tmp/f3d3a-2.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3a-2.txt
```
Expected: `PYTEST_EXIT=0` — **both PASS immediately**, and that is correct. Task 1 already supplied the mechanism; these two tests exist to PIN it, because Task 3 depends on it and nothing else in the tree states it. Do not invent a failing state to satisfy TDD ritual here — say in your report that they passed on first run and why.

- [ ] **Step 3: Commit**

```bash
uv run ruff check .
git add tests/fencing/test_inventory_merge.py
git commit -m "test(fencing): a rename can carry a row into the relocated scope, and only there (F3d3a Task 2)"
```

- [ ] **Step 4: Prove the pin is load-bearing in both directions**

First, narrow the scope: in `tests/fencing/detectors.py` set `RELOCATED_EFFECT_PATHS` to `frozenset()`.

Run:
```bash
uv run pytest tests/fencing/test_inventory_merge.py -q -k "relocated_scope" > /tmp/f3d3a-2m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3a-2m.txt
```
Expected: still PASS. Read that carefully before you go further — `merge()` validates the target against the `found` set the CALLER passes, and this test constructs `found` by hand, so it does not exercise `find_sites`' scoping at all. That is exactly why `test_the_fenced_measurement_uses_the_same_scope` in Task 1 exists: these two tests pin the merge contract, and Task 1's pin the scan. Neither substitutes for the other. Restore, then record this in your report — a reviewer will ask.

```bash
git checkout tests/fencing/detectors.py
git diff --exit-code HEAD -- tests/fencing/detectors.py
```

Second, remove the guard: in `tests/fencing/inventory.py`, delete the two lines

```python
        if new not in found_ids:
            bad.append(f"target not found in the scan: {new}")
```

Run:
```bash
uv run pytest tests/fencing/test_inventory_merge.py -q -k "undeclared_path" > /tmp/f3d3a-2n.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3a-2n.txt
```
Expected: FAIL on `test_a_rename_into_an_undeclared_path_is_still_refused` with `DID NOT RAISE <class 'BadRenameMap'>`.

```bash
git checkout tests/fencing/inventory.py
git diff --exit-code HEAD -- tests/fencing/inventory.py
```

---

### Task 3: The core moves to `citations/core.py`, and its inventory row moves with it

The relocation itself. Eleven functions and seven module constants, moved VERBATIM.

The set is not a judgement call — it is the transitive closure of the five symbols the spec names, computed from the AST. Reproduce it yourself before you start, and stop if your list differs:

```bash
uv run python -c "
import ast
p='src/call_manager/proposal_assist/tools/citations.py'
tree=ast.parse(open(p).read())
defs={n.name:n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
seed=['_resolve_citekey_for_ref','insert_citation_impl','compute_citation_insert','_write_citekey_cache','_insert_citation_locked']
closure=set(seed); frontier=list(seed)
while frontier:
    cur=frontier.pop()
    for c in ast.walk(defs[cur]):
        if isinstance(c,ast.Name) and c.id in defs and c.id not in closure:
            closure.add(c.id); frontier.append(c.id)
for n in sorted(closure, key=lambda k: defs[k].lineno):
    print(f'{n:32} {defs[n].lineno}-{defs[n].end_lineno}')
"
```

Expected output, exactly:

```
_validate_stance_confidence      100-111
_read_proposal_and_ledger        114-132
_current_head                    135-147
_citation_snapshot               150-191
_find_idempotent_row             194-217
_resolve_free_citekey            220-247
_resolve_citekey_for_ref         250-291
insert_citation_impl             366-408
compute_citation_insert          567-742
_write_citekey_cache             745-769
_insert_citation_locked          772-871
```

The six module constants those functions read: `logger`, `CITATIONS_FILENAME`, `_BUSY_ERROR`, `_DELETED_ERROR`, `_GIT_COMMIT_FAILED_ERROR`, `ALLOWED_STANCES`, `ALLOWED_CONFIDENCE` — seven names, but they do not all travel the same way:

- **Five move:** `_BUSY_ERROR`, `_DELETED_ERROR`, `_GIT_COMMIT_FAILED_ERROR`, `ALLOWED_STANCES`, `ALLOWED_CONFIDENCE`.
- **`logger` is re-declared**, not moved, so messages carry the new module's name.
- **`CITATIONS_FILENAME` does not move at all.** There is exactly ONE literal, at `tools/citations.py:80`, and both staying tools read it (`:126`, `:329`). Deleting it would strand them; copying it would make a third copy of a string `ports/artifacts.py:35-38` exists to hold. So the literal stays where it is — F3d3b removes it — and `core.py` imports the name from `ports/artifacts.py`, which is where every other consumer outside `tools/` already gets it.

**Files:**
- Create: `src/call_manager/proposal_assist/citations/core.py`
- Modify: `src/call_manager/proposal_assist/tools/citations.py`
- Modify: `tests/proposal_assist/test_f3d3a_relocation.py`
- Modify: `tests/fencing/regenerate_inventory.py`
- Modify: `tests/fencing/fence_inventory.json`
- Modify: `tests/fencing/inventory.py`

**Interfaces:**
- Consumes: `RELOCATED_EFFECT_PATHS` (Task 1) and the rename contract (Task 2). This is
  the single run that uses them.
- Produces, in `call_manager.proposal_assist.citations.core`: the eleven functions above with unchanged names and signatures, plus the five moved constants, a re-declared `logger`, and `CITATIONS_FILENAME` imported from `ports/artifacts.py`. `insert_citation_impl` and `_insert_citation_locked` keep their `ctx: ToolContext` parameter — GC-3, the new module is outside the gate.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_f3d3a_relocation.py`:

```python
"""F3d3a — the citation core relocation. A pure move: the objects are the same
objects, at a new address, with no behaviour changed.

Spec 2026-08-18 revision 8 section 7: "moves the whole session/model-dependent
citation core out of tools/ into the effects/domain layer and updates every
non-tool consumer".
"""

from __future__ import annotations

import inspect

MOVED = [
    "_validate_stance_confidence",
    "_read_proposal_and_ledger",
    "_current_head",
    "_citation_snapshot",
    "_find_idempotent_row",
    "_resolve_free_citekey",
    "_resolve_citekey_for_ref",
    "insert_citation_impl",
    "compute_citation_insert",
    "_write_citekey_cache",
    "_insert_citation_locked",
]

CONSTANTS = [
    "CITATIONS_FILENAME",
    "_BUSY_ERROR",
    "_DELETED_ERROR",
    "_GIT_COMMIT_FAILED_ERROR",
    "ALLOWED_STANCES",
    "ALLOWED_CONFIDENCE",
]


def test_the_whole_closure_lives_in_the_new_module():
    """The set is the transitive closure of the five symbols the spec names,
    computed from the AST — not a judgement call. Moving a subset leaves the new
    module calling a name it cannot see."""
    from call_manager.proposal_assist.citations import core

    missing = [n for n in MOVED + CONSTANTS if not hasattr(core, n)]
    assert not missing, f"not moved: {missing}"


def test_the_moved_functions_are_defined_there_not_imported_back():
    """GC-4: no re-export shim. If a moved function's __module__ still points at
    tools.citations, the move did not happen — an import was added instead, and
    tools/ keeps the violation the move exists to remove."""
    from call_manager.proposal_assist.citations import core

    wrong = [
        n
        for n in MOVED
        if getattr(core, n).__module__ != "call_manager.proposal_assist.citations.core"
    ]
    assert not wrong, f"still owned by another module: {wrong}"


def test_the_tool_module_no_longer_defines_them():
    """The other half of the same claim, and the one that catches a copy rather
    than a move. A duplicated function drifts; that is what F3d1's four
    CITATIONS_FILENAME literals cost."""
    from call_manager.proposal_assist.tools import citations as tool_mod

    duplicated = [
        n
        for n in MOVED
        if getattr(getattr(tool_mod, n, None), "__module__", None)
        == "call_manager.proposal_assist.tools.citations"
    ]
    assert not duplicated, f"still defined in tools/: {duplicated}"


def test_the_signatures_are_unchanged():
    """GC-1: this is a MOVE. A changed signature is a conversion wearing a move's
    clothes, and it destroys the one review property this slice has — that the
    diff can be checked by comparing bytes."""
    from call_manager.proposal_assist.citations import core

    assert str(inspect.signature(core._write_citekey_cache)) == (
        "(session: 'Session', ref: 'Reference', citekey: 'str', *, owns_txn: 'bool') -> 'None'"
    )
    assert list(inspect.signature(core._resolve_citekey_for_ref).parameters) == [
        "session",
        "ledger",
        "ref",
        "workspace_id",
    ]
    assert list(inspect.signature(core._insert_citation_locked).parameters)[:2] == [
        "ctx",
        "session",
    ]


def test_the_relocated_module_may_hold_a_session_and_that_is_the_point():
    """GC-3. The new module is outside tools/, so the import gate does not apply.
    Do not try to make it gate-clean: being able to name a Session here is the
    reason the code moved."""
    from call_manager.proposal_assist.citations import core

    src = inspect.getsource(core)
    assert "from sqlalchemy" in src or "import sqlalchemy" in src
    assert "ToolContext" in src
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d3a_relocation.py -q > /tmp/f3d3a-3.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3a-3.txt
```
Expected: `PYTEST_EXIT=1`,
`ModuleNotFoundError: No module named 'call_manager.proposal_assist.citations.core'`.

- [ ] **Step 3: Create the module**

Create `src/call_manager/proposal_assist/citations/core.py` under this docstring:

```python
"""The session- and model-dependent citation core.

Moved out of tools/citations.py in F3d3a, unchanged. It lives here because tool
code may import only the standard library and call_manager.proposal_assist.ports,
and every function below needs a Session, a model, or the repo directory.

Nothing here is converted. F3d3b converts propose_citation_impl (which stayed in
tools/), and the git/filesystem locators these functions name retire with the rest
of the capability holders in F3f.
"""
```

Then move the eleven functions and the five moved constants **verbatim** — same order as in the original file, so the diff is readable. Add `logger = logging.getLogger(__name__)` and `from call_manager.proposal_assist.ports.artifacts import CITATIONS_FILENAME`; do NOT copy the `CITATIONS_FILENAME` literal. Build the import header from what the moved code actually references; the source block at `tools/citations.py:20-76` is the superset to select from. Do not copy imports the moved code does not use: `ruff` reports `F401` and Step 6 fails.

`logger` is re-declared, not moved, so messages carry the new module's name.

`test_the_whole_closure_lives_in_the_new_module` checks `hasattr(core, "CITATIONS_FILENAME")`, which the `ports/artifacts.py` import satisfies — the constant is expected to be REACHABLE from `core.py`, not defined there.

- [ ] **Step 4: Strip the tool module and import back what remains**

Delete the eleven functions and the five moved constants from
`src/call_manager/proposal_assist/tools/citations.py`. Leave `logger` and the
`CITATIONS_FILENAME` literal at `:80` exactly where they are.

The thin tools that remain — `list_citations_impl` and `propose_citation_impl` — still call some of the moved names. Find exactly which:

```bash
uv run python -c "
import ast
p='src/call_manager/proposal_assist/tools/citations.py'
tree=ast.parse(open(p).read())
MOVED={'_validate_stance_confidence','_read_proposal_and_ledger','_current_head','_citation_snapshot','_find_idempotent_row','_resolve_free_citekey','_resolve_citekey_for_ref','insert_citation_impl','compute_citation_insert','_write_citekey_cache','_insert_citation_locked','_BUSY_ERROR','_DELETED_ERROR','_GIT_COMMIT_FAILED_ERROR','ALLOWED_STANCES','ALLOWED_CONFIDENCE'}
stay=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name not in MOVED]
need=sorted({c.id for f in stay for c in ast.walk(f) if isinstance(c,ast.Name) and c.id in MOVED})
print('still needed by the staying tools:', need)
"
```

Add ONE import for exactly that list:

```python
from call_manager.proposal_assist.citations.core import (
    ...  # the names the command printed, alphabetically
)
```

Measured on `f9014b68`, the command prints exactly `['_validate_stance_confidence']`. If it prints anything else, the strip in the previous step did not match this plan's move set — stop and report the difference rather than importing back a longer list.

Do not import anything the command did not print, and in particular do not import
`CITATIONS_FILENAME`: its literal at `tools/citations.py:80` was never deleted, and an
import alongside it is `ruff F811`. `ports/artifacts.py:35-38` records why that copy
survives; removing it is F3d3b's.

- [ ] **Step 5: Verify no cycle and no drift**

Run:
```bash
uv run python -c "
import importlib
for m in ('call_manager.proposal_assist.citations.core','call_manager.proposal_assist.tools.citations','call_manager.proposal_assist.tools.registry'):
    importlib.import_module(m); print('ok', m)
"
```
Expected: three `ok` lines. `citations/core.py` imports `ToolContext` from `tools/registry.py`, and `tools/registry.py` imports `tools/citations.py` only lazily inside `_collect_tools` — so there is no module-level cycle. If you get `ImportError: partially initialized module`, something made the registry import eager: report it rather than working around it.

Then confirm the move was a move:
```bash
git diff --stat src/call_manager/proposal_assist/tools/citations.py
uv run python -c "
import ast
a=ast.parse(open('src/call_manager/proposal_assist/citations/core.py').read())
print('functions in core.py:', sum(1 for n in a.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))))
"
```
Expected: `citations.py` loses roughly 480 lines; `core.py` holds **11** functions.

The moment the code lands at its new path, the fencing inventory disagrees with the
tree: `_write_citekey_cache`'s row still names `tools/citations.py`, and the scan (which
Task 1 widened to `citations/core.py`) now finds it at the new address. Steps 6-10 carry
the row across in the SAME commit as the code. They are not a separate task: a commit
that moves the code without the row leaves `tests/fencing` red for a reason nothing in
that commit fixes, and it strands `RENAMES` — a one-shot mechanism — half-applied for
anyone who regenerates in between.

- [ ] **Step 6: Fill in the rename**

In `tests/fencing/regenerate_inventory.py`:

```python
RENAMES: dict[tuple[str, str, str], tuple[str, str, str]] = {
    (
        "tool_commit",
        "src/call_manager/proposal_assist/tools/citations.py",
        "_write_citekey_cache",
    ): (
        "tool_commit",
        "src/call_manager/proposal_assist/citations/core.py",
        "_write_citekey_cache",
    ),
}
```

- [ ] **Step 7: Move the `NON_SEMANTIC_PINS` key**

That dict is keyed by `(kind, path, symbol)`, so the relocation invalidates the KEY
itself, not just the inventory row. In `tests/fencing/inventory.py` change the first
entry's path from `.../tools/citations.py` to `.../citations/core.py`. Leave both
`references.py` entries exactly as they are.

`F3D_IDENTITIES` needs NO edit — it is keyed `(slice, kind, symbol)` with no path, so
`("F3d3", "tool_commit", "_write_citekey_cache")` survives the move untouched. Verify
that before assuming it; if it turns out to carry a path, stop, because the ratchet is
then load-bearing on the old address and this step is incomplete.

- [ ] **Step 8: Regenerate**

```bash
uv run python -m tests.fencing.regenerate_inventory
git diff tests/fencing/fence_inventory.json
```
Expected: **exactly one changed line** — `_write_citekey_cache`'s `path`. Its
`category` (`caller_fenced`), `fenced` (`false`), `slice` (`F3d3`) and `fenced_by`
(`_insert_citation_locked`) all ride along, which is what a rename is for. Row count
stays **20**.

If instead you see `BadRenameMap`, Task 1 did not land or the path string does not
match byte-for-byte — the JSON is NOT written when merge() raises, so a stale file
will look fine. Check the exit code, not the file.

- [ ] **Step 9: Clear the rename**

Set `RENAMES` back to `{}`. It is a one-run instruction, and leaving it populated makes
the next regeneration raise `BadRenameMap` on an identity that no longer exists.

Re-run the regeneration and confirm the JSON does not move:
```bash
uv run python -m tests.fencing.regenerate_inventory && git diff --exit-code tests/fencing/fence_inventory.json && echo "STABLE WITH RENAMES EMPTY"
```

- [ ] **Step 10: Shrink the `citations.py` exemption**

The move takes six `repo_dir` namings and most of the import violations out of
`tools/`. Get the new set:

```bash
uv run python -c "
from tests.fencing.import_gate import check_file, normalized_violation, tool_key, tool_modules
for p in tool_modules():
    if tool_key(p) == 'citations.py':
        vs = check_file(p)
        print('raw:', len(vs))
        for v in sorted({normalized_violation(x) for x in vs}): print(repr(v))
"
```

Replace `TOOLS_NOT_YET_CONVERTED["citations.py"]` with exactly that set. Record the
before/after counts in your report — the before is **28 raw / 19 normalized**, and the
shrink is this slice's measurable deliverable even though the exemption does not
retire.

Unlike `audit.py`'s re-pin in Task 4, this shrink is not forced by a gate:
`test_no_exempt_module_acquires_a_new_violation` is a subset check, so removing
violations is free, and `test_no_exemption_is_stale` only asks that ONE violation
survive — `citations.py` keeps several `repo_dir` namings in the two staying tools. Do
it anyway: an oversized pin is authority the module no longer uses.

- [ ] **Step 11: Run the tests**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d3a_relocation.py -q > /tmp/f3d3a-3.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3a-3.txt
uv run pytest tests/fencing -q > /tmp/f3d3a-3f.txt 2>&1; echo "FENCE_EXIT=$?"; tail -20 /tmp/f3d3a-3f.txt
```
Expected: `PYTEST_EXIT=0` with 5 passed, and `FENCE_EXIT=0`.

`test_caller_fenced_entries_name_a_resolvable_ancestor` is the one worth watching: it
parses `REPO / row["path"]` and requires `fenced_by` to resolve **in that file**. Both
`_write_citekey_cache` and `_insert_citation_locked` moved to `core.py`, so it passes at
the new path — if it fails, they did not move together.

The fencing suite is AST-only and does not import the modules it reads, so it is
expected to be green here even though `src/` does not import cleanly yet. If it fails at
COLLECTION with an ImportError, that assumption is wrong: report it rather than
repointing a consumer early to get past it.

The wider suite is expected to be RED at this point — Tasks 4 and 5 have not repointed
the consumers yet, so imports fail at collection. Do not chase it. Record the count and
move on:
```bash
uv run pytest tests/proposal_assist -q -n 16 --dist loadfile 2>&1 | tail -3
```

- [ ] **Step 12: Commit**

```bash
uv run ruff check . --fix && uv run ruff check .
git add src/call_manager/proposal_assist/citations/core.py src/call_manager/proposal_assist/tools/citations.py tests/proposal_assist/test_f3d3a_relocation.py tests/fencing/regenerate_inventory.py tests/fencing/fence_inventory.json tests/fencing/inventory.py
git commit -m "refactor(citations): the session-dependent core moves out of tools/, row and all (F3d3a Task 3)"
```

Committing a tree whose wider suite is red is deliberate here and it is the only task in
this plan that does it: the move and the repointing cannot land atomically without one
commit holding a 480-line move plus 20 import edits, which is exactly the diff a
reviewer cannot check. The FENCING suite, though, is green — that is the line this task
holds. Say both in your report.

- [ ] **Step 13: Prove the row would have vanished without Task 1**

GC-10 applies: commit Step 12 before mutating, and restore from a copy rather than
`git checkout` if you have uncommitted work in the file.

In `tests/fencing/detectors.py`, empty `RELOCATED_EFFECT_PATHS`.

Run:
```bash
uv run pytest tests/fencing -q > /tmp/f3d3a-3m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/f3d3a-3m.txt
```
Expected: FAIL on `test_the_inventory_matches_the_tree_exactly` — the JSON lists a
`citations/core.py` row the scan no longer finds. This is the state the slice would
have been in without Task 1, and it is the LOUD version; the silent version is the one
F3d1's ratchet was built for. Note whether
`test_every_f3d_identity_is_still_in_the_inventory` also fires and say so — it should
not, because the row is still IN the JSON; only the scan disagrees.

```bash
git checkout tests/fencing/detectors.py
git diff --exit-code HEAD -- tests/fencing/detectors.py
```

---

### Task 4: The six source consumers repoint

Five non-tool modules and `tools/audit.py` import from `tools/citations.py`. Every one is a hard `ImportError` at collection until it is repointed.

**Files:**
- Modify: `src/call_manager/proposal_assist/analysis/insert.py`
- Modify: `src/call_manager/proposal_assist/citations/human_cite.py`
- Modify: `src/call_manager/proposal_assist/collab/kernel.py`
- Modify: `src/call_manager/proposal_assist/proposals/accepting.py`
- Modify: `src/call_manager/proposal_assist/web/api/annotations.py`
- Modify: `src/call_manager/proposal_assist/tools/audit.py`
- Modify: `tests/fencing/inventory.py`

**Interfaces:**
- Consumes: `citations/core.py` (Task 3).
- Produces: nothing new.

- [ ] **Step 1: Find every site**

```bash
grep -rn "proposal_assist\.tools\.citations import\|proposal_assist\.tools import citations" src/ --include=*.py
```

Expected: seven files — the five non-tool consumers, `tools/audit.py`, and `tools/registry.py`. Registry's is the lazy import of the tool impls inside `_collect_tools`; it imports `list_citations_impl` and `propose_citation_impl`, both of which STAYED, so it does not change. Confirm that before assuming it.

- [ ] **Step 2: Repoint each**

For each of the six, split the import: names that moved come from
`call_manager.proposal_assist.citations.core`, names that stayed keep their existing
source. Change nothing else — not the call sites, not the argument order, not a
docstring.

The moved names each file uses, verified against the code:

| file | moved names it imports |
|---|---|
| `analysis/insert.py` | `_citation_snapshot`, `_resolve_citekey_for_ref` |
| `citations/human_cite.py` | `_citation_snapshot`, `_resolve_citekey_for_ref` |
| `collab/kernel.py` | `compute_citation_insert`, `_write_citekey_cache` |
| `proposals/accepting.py` | `_GIT_COMMIT_FAILED_ERROR`, `_insert_citation_locked` |
| `web/api/annotations.py` | `_citation_snapshot` |
| `tools/audit.py` | `_read_proposal_and_ledger` |

`analysis/insert.py` and `collab/kernel.py` also import `CITATIONS_FILENAME` from
`tools.citations`. That name did NOT move (Task 3), so leave it pointing where it is —
those two files end up with two import statements, and F3d3b collapses them.

Verify each file's actual import line before editing it — the table is what the survey
found, and a survey is not the file. Codex caught a missing `_insert_citation_locked` in
the `accepting.py` row of an earlier draft; assume the same class of omission is still
possible and read each import block.

- [ ] **Step 3: Re-pin `audit.py`**

GC-10. `tools/audit.py` stays exempt, and repointing its import produces a NEW
normalized violation string — `test_no_exempt_module_acquires_a_new_violation` is a
subset check against a PINNED set, so it goes red unless the pin is edited in this
commit. Get the new set:

```bash
uv run python -c "
from tests.fencing.import_gate import check_file, normalized_violation, tool_key, tool_modules
for p in tool_modules():
    if tool_key(p) == 'audit.py':
        for v in sorted({normalized_violation(x) for x in check_file(p)}): print(repr(v))
"
```

Replace `TOOLS_NOT_YET_CONVERTED["audit.py"]` in `tests/fencing/inventory.py` with
exactly that set. It should lose the `tools.citations` entry and gain a
`citations.core` one — a swap, not a growth. If the count grew by more than that,
stop and report what else changed.

- [ ] **Step 4: Run the tests**

Run:
```bash
uv run pytest tests/fencing -q > /tmp/f3d3a-4.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3a-4.txt
uv run python -c "import call_manager.proposal_assist.web.api.annotations; print('src imports resolve')"
```
Expected: `PYTEST_EXIT=0` for the fencing suite and the import line printing. The
broader suite is still red until Task 5 — record the number, do not chase it.

- [ ] **Step 5: Commit**

```bash
uv run ruff check . --fix && uv run ruff check .
git add src/call_manager/proposal_assist/analysis/insert.py src/call_manager/proposal_assist/citations/human_cite.py src/call_manager/proposal_assist/collab/kernel.py src/call_manager/proposal_assist/proposals/accepting.py src/call_manager/proposal_assist/web/api/annotations.py src/call_manager/proposal_assist/tools/audit.py tests/fencing/inventory.py
git commit -m "refactor(citations): the six source consumers import the core's new home (F3d3a Task 4)"
```

- [ ] **Step 6: Prove the exempt-module pin is armed**

In `src/call_manager/proposal_assist/tools/audit.py`, add
`from call_manager.proposal_assist.editing.save import CITATIONS_FILENAME as _X` and
use it nowhere meaningful.

Run:
```bash
uv run pytest tests/fencing -q -k import_gate > /tmp/f3d3a-4m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/f3d3a-4m.txt
```
Expected: FAIL on `test_no_exempt_module_acquires_a_new_violation`, naming `audit.py`
and the `editing.save` import. This is what makes Step 3 a real obligation rather than
bookkeeping.

```bash
git checkout src/call_manager/proposal_assist/tools/audit.py
git diff --exit-code HEAD -- src/call_manager/proposal_assist/tools/audit.py
```

---

### Task 5: The tests repoint, and the four monkeypatch targets move with them

Fourteen test files hold nineteen import statements. Four of them are
`monkeypatch.setattr(citations_mod, ...)` and they are the sharpest hazard in the
slice (GC-5): patching a module attribute patches the name **as that module sees it**,
so a patch left pointing at `tools.citations` after the function moved lands on
nothing. Two of the four are NEGATIVE tests — a forced citekey collision and a forced
`GitCommitError` — which would go GREEN for the wrong reason.

**Files:**
- Modify: 14 test files (enumerate them in Step 1)
- Modify: `tests/proposal_assist/test_f3d3a_relocation.py`

**Interfaces:**
- Consumes: `citations/core.py` (Task 3).
- Produces: nothing new.

- [ ] **Step 1: Write the guard test**

Append to `tests/proposal_assist/test_f3d3a_relocation.py`:

```python
# Each patched name paired with the function in citations/core.py that actually READS
# it. Verified against f9014b68 with ast: compute_citation_insert reads
# _resolve_citekey_for_ref and new_ev_id; _insert_citation_locked reads
# write_and_commit_files. Pairing all three with one reader is how the first draft of
# this test got it wrong.
PATCH_TARGETS = [
    ("_resolve_citekey_for_ref", "compute_citation_insert"),
    ("new_ev_id", "compute_citation_insert"),
    ("write_and_commit_files", "_insert_citation_locked"),
]


def test_the_monkeypatch_targets_are_the_module_that_actually_reads_them():
    """GC-5, the sharpest hazard in this slice.

    monkeypatch.setattr(mod, "name", ...) rebinds the name AS THAT MODULE SEES IT.
    Three test modules patch these names on tools.citations at four sites; after the
    move the readers live in citations.core, so a patch on the old module rebinds a
    name nothing reads and the test passes vacuously. Two of the four are NEGATIVE
    tests (a forced citekey collision at test_accepting.py:591, a forced GitCommitError
    at :655), which is the direction that fails silently.

    Pinning the READER, not just the module, is what makes this a real check: a name
    that is present in citations.core but read by nobody would satisfy a hasattr test
    and still leave the patch site dead.
    """
    import importlib

    core = importlib.import_module("call_manager.proposal_assist.citations.core")

    for name, reader_name in PATCH_TARGETS:
        assert hasattr(core, name), f"citations.core has no {name}"
        reader = getattr(core, reader_name)
        assert name in reader.__code__.co_names, (
            f"{name} is not read by {reader_name} — the patch-target list is stale, "
            "which is exactly the failure this test exists to catch"
        )
```

- [ ] **Step 2: Run the guard to verify it fails**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d3a_relocation.py -q -k monkeypatch_targets > /tmp/f3d3a-5.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3a-5.txt
```
Expected: `PYTEST_EXIT=0` — it PASSES once Task 3 landed, because each name moved with
the function that reads it. It is a ratchet, not a red-to-green step. If it FAILS, one
of `compute_citation_insert` or `_insert_citation_locked` did not move into `core.py`
with the names it reads, and Task 3 is incomplete: go back rather than editing this
list. Editing `PATCH_TARGETS` to make this pass is the one repair that defeats the
test's purpose.

- [ ] **Step 3: Enumerate every test import**

```bash
grep -rn "proposal_assist\.tools\.citations import\|proposal_assist\.tools import citations" tests/ --include=*.py
```

Expected: 19 statements across 14 files. Two further files —
`tests/fencing/inventory.py` and `tests/fencing/test_import_gate.py` — hold the dotted
path as a STRING literal, not an import. Those strings are the gate's own subject
matter and must NOT be changed.

- [ ] **Step 4: Repoint them**

For each statement, moved names come from `call_manager.proposal_assist.citations.core`
and names that stayed keep their source. Do not change what any test asserts.

Then the four patch sites. Each aliases the module:

| file | alias line | patch site | name patched |
|---|---|---|---|
| `tests/proposal_assist/proposals/test_accepting.py` | 558 | 591 | `_resolve_citekey_for_ref` |
| `tests/proposal_assist/proposals/test_accepting.py` | 626 | 655 | `write_and_commit_files` |
| `tests/proposal_assist/tools/test_citations_insert.py` | 27 | 435 | `write_and_commit_files` |
| `tests/proposal_assist/collab/test_collab_kernel_citation.py` | ~155 | ~155 | `new_ev_id` |

Repoint each alias to `call_manager.proposal_assist.citations.core`. Verify the line
numbers against the files before editing — they come from a survey, and a survey is
not the file.

- [ ] **Step 5: Run the full suite**

Run:
```bash
uv run pytest -n 16 --dist loadfile -q > /tmp/f3d3a-5b.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -5 /tmp/f3d3a-5b.txt
```
Expected: `PYTEST_EXIT=0` and **3783 passed, 1 skipped** — the master baseline of 3771
(`f9014b68`) plus the 12 tests added so far: 4 (Task 1) + 2 (Task 2) + 5 (Task 3) +
1 (Task 5). Task 6 adds three more, so the FINAL total is 3786 — do not expect it here.
Derive it yourself from the tests you actually added and report both your arithmetic and
the run. If they disagree, work out which term is wrong — do not adjust either to match
the other, and do not delete a test to reach a number.

This is the first green full suite since Task 3. If anything other than an import path
had to change to get here, that is a behaviour change and it violates GC-1: report it
instead of absorbing it.

- [ ] **Step 6: Commit**

```bash
uv run ruff check . --fix && uv run ruff check .
git add tests/ && git commit -m "refactor(citations): the test surface follows the core, monkeypatch targets included (F3d3a Task 5)"
```

- [ ] **Step 7: Prove a stale patch target fails loudly now**

Point one alias back at the old module: in
`tests/proposal_assist/tools/test_citations_insert.py`, change the `citations_mod`
import at line 27 back to `call_manager.proposal_assist.tools.citations`.

Run:
```bash
uv run pytest tests/proposal_assist/tools/test_citations_insert.py -q > /tmp/f3d3a-5m.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -30 /tmp/f3d3a-5m.txt
```
Expected: FAIL — `AttributeError` from `monkeypatch.setattr`, because
`tools/citations.py` no longer has `write_and_commit_files` to patch. Record the exact
error. **If it PASSES, say so loudly in your report**: it would mean the patch is
landing on a name nothing reads and the test was already vacuous, which is a finding
about the existing suite, not about this slice.

```bash
git checkout tests/proposal_assist/tools/test_citations_insert.py
git diff --exit-code HEAD -- tests/proposal_assist/tools/test_citations_insert.py
```

---

### Task 6: Confirm the slice moved code and changed nothing

The closing check. This slice's claim is unusually strong — *zero behaviour change* —
and a claim that strong deserves a command that would catch a violation.

**Files:**
- Modify: `tests/proposal_assist/test_f3d3a_relocation.py`

**Interfaces:**
- Consumes: everything above.
- Produces: nothing.

- [ ] **Step 1: Write the closing tests**

Append to `tests/proposal_assist/test_f3d3a_relocation.py`:

```python
def test_no_tool_module_gained_a_port_call():
    """GC-1. This slice converts nothing. If a tool started calling a port, the
    move turned into a conversion somewhere along the way and the review property
    this slice was split to preserve is gone."""
    from tests.fencing.inventory import load_inventory, scan_tree

    _sites, fenced = scan_tree()
    assert fenced == {
        ("src/call_manager/proposal_assist/tools/scorecard.py", "submit_scorecard_impl"),
        ("src/call_manager/proposal_assist/tools/review_submit.py", "submit_review_impl"),
        (
            "src/call_manager/proposal_assist/tools/uncited_claims.py",
            "submit_uncited_claims_impl",
        ),
        ("src/call_manager/proposal_assist/tools/references.py", "fetch_reference_impl"),
        (
            "src/call_manager/proposal_assist/tools/references.py",
            "fetch_reference_impl._refund_fetch_slot",
        ),
    }, "F3d2's five and no more — F3d3a converts nothing"
    assert sum(1 for r in load_inventory() if r["slice"] == "F3d3" and r["fenced"]) == 0


def test_the_exemption_list_still_has_five_keys():
    """The honest headline. Both citations.py and proposal.py are blocked on the
    repo_dir locator, which retires in F3f — so this slice SHRINKS two pin sets and
    retires neither. Saying otherwise would misreport the fence's progress."""
    from tests.fencing.inventory import TOOLS_NOT_YET_CONVERTED

    assert set(TOOLS_NOT_YET_CONVERTED) == {
        "audit.py",
        "citations.py",
        "history.py",
        "proposal.py",
        "references.py",
    }


def test_the_moved_code_still_names_repo_dir_and_that_is_expected():
    """Where the remaining work lives. Four of citations.py's nine repo_dir namings
    left tools/ with this move; the rest sit in list_citations_impl and
    propose_citation_impl, which stay. F3f owns the locator."""
    import inspect

    from call_manager.proposal_assist.citations import core

    assert "repo_dir" in inspect.getsource(core)
```

- [ ] **Step 2: Run them**

Run:
```bash
uv run pytest tests/proposal_assist/test_f3d3a_relocation.py -q > /tmp/f3d3a-6.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -20 /tmp/f3d3a-6.txt
```
Expected: `PYTEST_EXIT=0`.

- [ ] **Step 3: Run the full suite and the shape**

Run:
```bash
uv run pytest -n 16 --dist loadfile -q > /tmp/f3d3a-6b.txt 2>&1; echo "PYTEST_EXIT=$?"; tail -3 /tmp/f3d3a-6b.txt
# Expected: 3786 passed, 1 skipped — 3783 from Task 5 plus this task's three.
uv run ruff check .
uv run python -c "
from tests.fencing.inventory import SLICE_STATUS, TOOLS_NOT_YET_CONVERTED, load_inventory, scan_tree
from tests.fencing.import_gate import check_file, tool_key, tool_modules
rows = load_inventory(); _s, fenced = scan_tree()
print('inventory rows:', len(rows))
print('pins:', len(TOOLS_NOT_YET_CONVERTED), sorted(TOOLS_NOT_YET_CONVERTED))
print('F3d3 status:', SLICE_STATUS['F3d3'], '| F3d3 fenced rows:', sum(1 for r in rows if r['slice']=='F3d3' and r['fenced']))
print('citekey row path:', next(r['path'] for r in rows if r['symbol']=='_write_citekey_cache'))
for p in tool_modules():
    if tool_key(p) in ('citations.py','proposal.py'):
        print(f'  {tool_key(p)}: {len(check_file(p))} raw violations')
"
```
Expected exactly:
```
inventory rows: 20
pins: 5 ['audit.py', 'citations.py', 'history.py', 'proposal.py', 'references.py']
F3d3 status: pending | F3d3 fenced rows: 0
citekey row path: src/call_manager/proposal_assist/citations/core.py
  citations.py: <a number well below 28> raw violations
  proposal.py: 14 raw violations
```
`proposal.py: 14` is the check that this slice stayed in its lane — it is untouched, so
its count must not have moved at all.

- [ ] **Step 4: Commit**

```bash
git add tests/proposal_assist/test_f3d3a_relocation.py
git commit -m "test(fence): F3d3a moved code and converted nothing (F3d3a Task 6)"
```

---

## What F3d3a deliberately does NOT do

Named so a reviewer does not read them as gaps:

- **No conversion.** `propose_citation_impl` and `_propose_artifact_edit_impl` still
  open sessions and commit directly. F3d3b converts them, and its Task 1 will find the
  ports already in place from F3b.
- **No exemption retires.** Both `citations.py` and `proposal.py` are blocked on
  `repo_dir`, which is a filesystem locator that retires with the capability holders in
  F3f. This slice shrinks two pin sets and that is all it claims.
- **`tools/proposal.py` is not touched.** Its four `repo_dir` namings all sit in
  `_read_artifact_impl` on the read path, and its `tools.registry` import cannot retire
  while `ctx: ToolContext` annotates three read-path functions.
- **`CITATIONS_FILENAME` keeps its duplicate at `tools/citations.py:80`.**
  `ports/artifacts.py:35-38` records why: that copy retires when F3d3b converts the
  module.
- **The relocated module is not made gate-clean.** It holds a `Session`, names
  `repo_dir`, and imports `ToolContext`. That is the point of moving it (GC-3).
- **`RELOCATED_EFFECT_PATHS` is not generalised.** It holds exactly one path. F3e and
  F3f will each add their own, in the slice that moves the code.

## Self-review notes

Checked against the spec and the survey that produced this cut:

| Requirement | Task |
|---|---|
| §7 "moves the whole session/model-dependent citation core out of `tools/`" | 3 |
| §7 "updates **every non-tool consumer**" | 4, 5 |
| Survey finding 1 — pins cannot retire (`repo_dir` is F3f's) | 6, and stated in the header |
| Survey finding 2 — `RENAMES` cannot cross the boundary | 1, 2, 3 |
| Survey finding 3 — exactly one row relocates | 3 |
| GC-5 — the four monkeypatch targets | 5 |
| `NON_SEMANTIC_PINS` is keyed by path | 3 Step 7 |
| `caller_fenced` ancestor must resolve in the row's file | 3 Step 11 |

Three things a reviewer should push on, named here rather than discovered later:

1. **Task 3 commits a tree whose wider suite is red.** It is the only task that does,
   and its own fencing suite is green — the code and its inventory row cross together,
   so no gate is left red for a reason that commit does not fix. The alternative is one
   commit holding a 480-line move plus twenty import edits, which no reviewer can check.
   If a reviewer disagrees, the fix is to squash Tasks 3-5 at merge, not to reorder them.
2. **Task 2's tests pass on first run.** They pin a contract Task 1 already supplied.
   Step 4 deliberately shows that narrowing `RELOCATED_EFFECT_PATHS` does NOT break them,
   because `merge()` validates against a caller-supplied `found` set — the scan's scoping
   is pinned by Task 1's tests instead. Two pins, two different mechanisms, neither
   redundant.
3. **Two different totals are expected, and both are correct.** Task 5 Step 5 expects
   **3783** (baseline 3771 + the 12 tests that exist by then); Task 6 Step 3 expects
   **3786** (+ Task 6's own three). A reviewer seeing two numbers should check the
   arithmetic, not assume a typo: 4 (Task 1) + 2 (Task 2) + 5 (Task 3) + 1 (Task 5) +
   3 (Task 6) = 15, and 3771 + 15 = 3786. Both are derived, neither is measured — this
   plan has not been executed, and the totals are the first thing a run should falsify.


---

## What execution actually found (recorded 2026-08-21, after merge e7cd8f2f)

The plan was read three times before it ran — self-review, a 14-agent survey, and an
adversarial read that found four P1s. Execution then found ten more. The split below is
the point of keeping this section: it shows which defect classes reading can reach.

### Reading could not have caught these

**PD-3 (MAJOR).** Step 11 claimed "the fencing suite is AST-only and does not import
the modules it reads", so it expected `tests/fencing` green at Task 3. It is not
insulated. `tests/conftest.py:135` holds an autouse teardown fixture that lazily imports
the app for EVERY test in the repo, so once Task 3 broke the app import chain the
fencing suite came back **103 passed, 103 teardown ERRORS, 0 FAILED**.

The author had checked whether `tests/fencing/*.py` import `call_manager`. They do not.
The root conftest was never checked. That is the shape of the miss: the right question,
asked of the wrong scope.

Worse, the plan's guard said "if `tests/fencing` is red at the Task 3 commit, something
is wrong with the row relocation — stop." The runner read the actual failures, saw zero
assertion failures, diagnosed teardown-only, and proceeded. **It was right to override
the guard.** A blindly obedient run would have stalled on a correct implementation.
Corrected rule: key the guard on FAILED, never on ERROR.

**PD-1.** The plan's verbatim test bodies do not pass `ruff check` — `SIM300` (yoda
condition), `E501`, and an `F401` for an import the plan's own test never uses. Tasks 1
and 2 then ran a bare `ruff check` with no `--fix`. A plan that says "copy this
verbatim" must run `--fix`, or its own text is the first thing that fails.

### Reading could have caught these, and did not

**PD-2.** The plan asserted "both staying tools read `CITATIONS_FILENAME` (`:126`,
`:329`)". Actually `:126` is read by `_read_proposal_and_ledger` (moved) and `:847` by
`_insert_citation_locked` (moved, and omitted from the plan entirely). Only `:329` is a
staying tool. The prescribed ACTION was right and the stated REASON was backwards — the
move needs the ports import precisely because two moved functions read it.

**PD-7.** The plan named `src/call_manager/proposal_assist/web/api/annotations.py` in
three places. That path does not exist; the file is `src/call_manager/web/api/
annotations.py`. Anyone running the plan's literal import check gets a spurious
`ModuleNotFoundError`.

**PD-4/5/6/8/9/10.** Cosmetic: a predicted `ModuleNotFoundError` that is really an
`ImportError`; "~480 lines" that is 563; `ports/artifacts.py:35-38` that is `:39`; two
line-number drifts; and four sites called `monkeypatch.setattr` of which two are
actually `unittest.mock.patch.object` (same hazard, same fix, imprecise description).

### What the two post-branch reviews found

Both reviews were the same model through different harnesses. Neither found a defect in
the moved code. Both found defects in this slice's own safety net.

- **The GC-4 re-export guard did not guard.** It tested ownership
  (`__module__ != tools.citations`) when the hazard is presence: a re-export shim leaves
  `__module__` pointing at `citations.core`, so the shim passed. Reproduced by injecting
  it at runtime; both guards stayed green. Now `DELIBERATELY_IMPORTED_BACK`.
- **Discovery and measurement disagreed about what a raw commit is.** `find_sites`
  matched a bare `"commit"` while `find_fenced_tool_commits` used `_RAW_COMMIT_ATTRS`,
  so a `begin`-only effect owned no row at all. Latent today; F3e's ingest is where it
  becomes live.
- **A stale INSTRUCTION, not just stale prose.** `test_fence_coverage.py` told a future
  mover to flip a row `fenced: true` before relocating it. Since F3d2 `fenced` is a
  measurement, so that is the exact false claim the measurement exists to prevent.

**The harness difference was disposition, not capability.** The reviewer that mutated
found the hollow guard; the reviewer that read passed it — and it had a shell, having
already run a 192-test subset. It also declined to re-run the suite because the package
had reassured it the suite was green. Both causes were the package's fault and are now
fixed in `scripts/review_package.py`.

**And the fix repeated the bug.** The first drift test written to pin the two detector
halves together asserted `"_RAW_COMMIT_ATTRS" in inspect.getsource(find_sites)` — and
stayed GREEN under the narrowing mutation, because the COMMENT beside the code still
said the words. A test a comment can satisfy is not a test. Only running the mutation
caught it.

### Totals

Both derived totals were falsified by real runs and matched exactly: **3783/1** after
Task 5, **3786/1** after Task 6. No number was adjusted and no test deleted. The merged
total is **3788/1**, the two extra being the detector tests added by the review fixes.
