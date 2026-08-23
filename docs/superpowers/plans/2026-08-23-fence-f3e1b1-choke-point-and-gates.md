# Fence F3e1b-1 — the choke point and both gates — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Revision 2.** Revision 1 was reviewed by `gpt-5.6-sol` and returned 17 findings — three Critical, all confirmed by running them. Revision 2 restructures the task sequence around the worst of them. Everything revision 1 got wrong is recorded at the end, so a future reader does not reintroduce it.

**Goal:** Convert `checkpoint_artifact` and the seven §4.2 enclosing acquisitions from `acquire_commit_mutex_live` to `acquire_commit_mutex_at_generation`, carrying a `FenceStamp` minted at each semantic boundary, and land the two gates that keep them converted.

**Architecture:** Plumbing, then contract, then **one atomic conversion**. Tasks 1–3 are each green and independently reviewable: they add the gates, thread the stamp with no behaviour change, and make the stale outcome correct *before* anything can produce it. Task 4 flips all eight acquisitions, updates the records and removes the pins in a **single commit**, because no split ordering leaves both a green boundary and a safe tree (Constraint 2).

**Tech Stack:** Python 3.12, pytest (+ testcontainers Postgres), `ast` for the gates. No new dependencies.

## Global Constraints

Every task's requirements implicitly include this section.

1. **Spec §4.2's "seven" is CORRECT — do not "fix" it.** The table is scoped to F3e1b. The tree's eighth enclosing acquisition, `Phase0Worker._m1`, reaches `_init_repo`, whose row is **F5**. Converting it here is OUT OF SCOPE and makes crit-9 unsatisfiable. `test_f3e1b_owns_exactly_the_seven_enclosing_acquisitions_of_spec_4_2` already pins this.
2. **Task 4 is ONE commit. Do not split it.** Flipping `checkpoint_artifact` before its callers leaves outer functions mutating under `_live` while the inner call refuses on generation — a live half-conversion. Flipping the callers first publishes a mixed primitive graph. Either split also breaks `test_the_live_acquirers_partition_exactly` at the boundary, because a converted site leaves the AST-discovered `_live` set while the literal record still names it. The cost of atomicity is exactly one bisect boundary; take it.
3. **F3e1b-1 does NOT convert `save_section`, `insert_citation_for_user`, or `_insert_citation_locked`.** Those are F3e1b-2. They stay pinned throughout.
4. **No assertion may be satisfiable by a comment** (spec §5.1). Every gate assertion keys on parsed AST call nodes, never on source text. This project has shipped the comment-satisfiable mistake four times.
5. **Judge every stop-the-run decision on FAILED, never ERROR.** `tests/fencing` is not insulated from the app import chain — the root `tests/conftest.py` autouse teardown imports the app for every test in the repo, so a broken import shows as mass teardown **ERRORs with zero FAILED**, which reads like a passing mutation.
6. **Never adjust an expected number to make arithmetic close.** Baseline is **3898 collected** (measured 2026-08-23 at `8f7b1ef7`; 3897 passed + 1 skipped). Each task RECORDS its measured collected count. Any per-task delta stated here is marked **PREDICTED (not run)**; if yours differs, name the term that changed.
7. **The `1 skipped` is the signal.** It is a real-SDK test gated on `ANTHROPIC_API_KEY`. `0 skipped` means a live API call ran.
8. **Test commands:** `uv run --python 3.12 pytest -n 16 --dist loadfile` for the suite; never `-n auto` (resolves to 72) and never `--dist loadgroup`. The lint gate is `ruff check`, not `ruff format`. **Line limit is 100** — revision 1's gate code was 101 and would have failed the gate.
9. **`resolve_access` does not change.** All six call sites destructure exactly two values; a third element raises at every one. Routes read the workspace themselves after the check passes and mint from it.
10. **No authorization call changes in this slice.** The mint site is "the object the existing check already loaded", whatever that check is.
11. **Code in this plan was executed before it was written down.** Where a block says MEASURED, it was run against the tree at `8f7b1ef7` and passed `ruff check`. Revision 1 shipped a gate whose `problems += …` inside a closure raised `UnboundLocalError` on every call — it had been prototyped with `.append()` and re-typed differently into the plan. Copy these blocks verbatim; do not "tidy" them.

---

## File Structure

| File | Responsibility |
|---|---|
| `tests/fencing/f3e1b1_sites.py` | NEW. `DECLARED_SITES` — the immutable eight, path-qualified. The gates key off this, never off the mutable live-acquirer records. |
| `tests/fencing/live_reach_gate.py` | NEW. The no-`_live`-reaches-a-sink gate: static call graph over `src/`, plus its pin list. |
| `tests/fencing/test_live_reach_gate.py` | NEW. Gate tests + the mutation proving the pin list is load-bearing. |
| `tests/fencing/converted_gate.py` | NEW. The §5.1 AST gate. |
| `tests/fencing/test_converted_gate.py` | NEW. Gate tests + mutations. |
| `src/call_manager/proposal_assist/fencing.py` | Primitive B gains a required `stamp` and transaction-bound stamp identity. |
| `src/call_manager/proposal_assist/ports/errors.py` | NEW `FenceStampMismatch`. |
| `src/call_manager/proposal_assist/collab/checkpoint.py` | `checkpoint_artifact` gains `stamp`; its mutex flips in Task 4. |
| `src/call_manager/proposal_assist/collab/kernel.py` | The four kernel enclosing acquisitions. |
| `src/call_manager/web/api/workspaces.py` | `post_lock_spec` / `post_unlock_spec` mint and thread. |
| `src/call_manager/proposal_assist/citations/collab_cite.py` | Three `*_via_kernel` orchestrators gain `stamp`. |
| `src/call_manager/proposal_assist/proposals/kernel_accept.py` | `accept_via_kernel` gains `stamp`. |
| `src/call_manager/proposal_assist/collab/{permissions,handover}.py` | Two maintenance callers mint from their pre-screen. |
| `src/call_manager/proposal_assist/web/collab_checkpoint.py` | `save_version` mints from `resolve_access`. |
| `tests/proposal_assist/test_f3e1b1_stale_outcomes.py` | NEW (Task 3). The §8 outcome matrix, by injection. |
| `tests/proposal_assist/test_f3e1b1_conversions.py` | NEW. Plumbing tests, then the §5.2 races. |
| `tests/fencing/test_f3e1b1_slice.py` | NEW (Task 5). Inventory and identity. |

---

## The conversion surface, measured at `8f7b1ef7`

`checkpoint_artifact` has **10** call sites. Six are self-contained — they mint from an object they already hold. Four thread through an orchestrator.

| # | Call site | Enclosing function | Mints from | Threading |
|---|---|---|---|---|
| 1 | `web/api/workspaces.py:665` | `post_lock_spec` | `ws` (`require_owner`, `:645`) | self-contained |
| 2 | `web/api/workspaces.py:746` | `post_unlock_spec` | `ws` (`require_owner`, `:734`) | self-contained |
| 3 | `web/collab_checkpoint.py:52` | `save_version` | the `Workspace` `resolve_access` loaded (`:38`) | self-contained |
| 4 | `collab/handover.py:40` | kill-switch handover | its pre-screen (`:36-38`) | self-contained |
| 5 | `collab/permissions.py:58` | evict hook | its pre-screen (`:53-55`) | self-contained |
| 6 | `collab/checkpoint.py:210` | periodic tail checkpoint | its pre-screen (`:205-207`) | self-contained |
| 7 | `collab/kernel.py:174` | `apply_citation_to_room` | route → `direct_cite_via_kernel` | orchestrator |
| 8 | `collab/kernel.py:294` | `kernel_accept_prose` | route → `accept_via_kernel` | orchestrator |
| 9 | `collab/kernel.py:343` | `kernel_init_spec` | route → `init_spec_via_kernel` | orchestrator |
| 10 | `collab/kernel.py:524` | `apply_annotation_insert_to_room` | route → `insert_annotation_via_kernel` | orchestrator |

`kernel_accept_citation` is the seventh enclosing acquisition and does **not** appear above: it reaches the sink via `apply_citation_to_room` (spec §4.2). It still acquires the mutex itself and still gains a `stamp`.

---

### Task 1: The declared-site catalog and both gates

Converts nothing. The gates exist and are proven load-bearing **before** any conversion.

**Files:**
- Create: `tests/fencing/f3e1b1_sites.py`, `tests/fencing/live_reach_gate.py`, `tests/fencing/test_live_reach_gate.py`, `tests/fencing/converted_gate.py`, `tests/fencing/test_converted_gate.py`

**Interfaces:**
- Consumes: `tests/fencing/live_mutex_gate.py`'s `ENCLOSING_ACQUISITIONS` (`Mapping[str, str]`, qualname → owning slice); `tests/fencing/git_fence.py`'s `_SINK_KIND` and `_callee_name`.
- Produces: `f3e1b1_sites.DECLARED_SITES`; `live_reach_gate.check_tree()` and `.UNCONVERTED`; `converted_gate.check_source(source, path)`, `.check_tree()`, `.NOT_YET_CONVERTED`, `._own_calls(func)`.

- [ ] **Step 1: Write the declared-site catalog**

```python
# tests/fencing/f3e1b1_sites.py
"""The eight sites F3e1b-1 converts, as a FIXED catalog.

Why this is its own module: the obvious source for "which sites does this slice own"
is `live_mutex_gate.ENCLOSING_ACQUISITIONS`. But Task 4 REMOVES the seven from that
record as it converts them, so a gate keyed off it would silently narrow to nothing
exactly when the conversion happened — and the test proving the conversion would still
be green. A permanent obligation needs a permanent, immutable list.

Deriving it from the AST scan instead would make every assertion tautological.
"""

from __future__ import annotations

#: (repo-relative path, qualname). Path-qualified, because two files can define the
#: same bare name, and a set of bare names cannot tell an added site from a moved one.
DECLARED_SITES: frozenset[tuple[str, str]] = frozenset(
    {
        ("src/call_manager/proposal_assist/collab/checkpoint.py", "checkpoint_artifact"),
        ("src/call_manager/proposal_assist/collab/kernel.py", "apply_citation_to_room"),
        ("src/call_manager/proposal_assist/collab/kernel.py", "kernel_accept_prose"),
        ("src/call_manager/proposal_assist/collab/kernel.py", "kernel_init_spec"),
        ("src/call_manager/proposal_assist/collab/kernel.py", "kernel_accept_citation"),
        (
            "src/call_manager/proposal_assist/collab/kernel.py",
            "apply_annotation_insert_to_room",
        ),
        ("src/call_manager/web/api/workspaces.py", "post_lock_spec"),
        ("src/call_manager/web/api/workspaces.py", "post_unlock_spec"),
    }
)
```

- [ ] **Step 2: Write the failing tests for the reach gate**

```python
# tests/fencing/test_live_reach_gate.py
def test_the_tree_is_green_under_the_pin_list():
    """Every `_live` caller that reaches a sink is pinned. Nothing is converted yet, so
    green here means the pin list is COMPLETE, not that the work is done."""
    from tests.fencing.live_reach_gate import check_tree

    assert check_tree() == []


def test_removing_a_pin_reddens_the_gate():
    """The pin list is load-bearing.

    The target is `save_section` DELIBERATELY. Revision 1 used `apply_citation_to_room`,
    which Task 4 removes from `UNCONVERTED` — after which this mutation removes a pin
    that is not there, the gate stays green, and the test fails for a reason unrelated
    to what it tests. `save_section` is F3e1b-2's and survives this whole slice.
    """
    import tests.fencing.live_reach_gate as gate

    original = gate.UNCONVERTED
    try:
        gate.UNCONVERTED = frozenset(original - {"save_section"})
        problems = gate.check_tree()
    finally:
        gate.UNCONVERTED = original
    assert any("save_section" in p for p in problems), problems


def test_every_pin_is_load_bearing():
    """No pin may be decorative. MEASURED at `8f7b1ef7`: dropping each of the fourteen
    one at a time reddens the gate on that name, every time."""
    import tests.fencing.live_reach_gate as gate

    original = gate.UNCONVERTED
    dead = []
    try:
        for pin in sorted(original):
            gate.UNCONVERTED = frozenset(original - {pin})
            if not any(pin in p for p in gate.check_tree()):
                dead.append(pin)
    finally:
        gate.UNCONVERTED = original
    assert dead == [], f"pins that protect nothing: {dead}"
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run --python 3.12 pytest tests/fencing/test_live_reach_gate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.fencing.live_reach_gate'`.
**FAILED, not ERROR** (Constraint 5).

- [ ] **Step 4: Implement the reach gate**

```python
# tests/fencing/live_reach_gate.py
"""Gate: no function that transitively reaches a git sink may call
`acquire_commit_mutex_live`.

Spec §4.2. This is the gate that would have caught revision 1's defect —
`checkpoint_artifact` is a storage choke point, not the effect boundary, so converting
it alone leaves seven outer acquisitions mutating before the inner call. §5.3 says it
is therefore NOT optional.

The call graph is name-based: two functions sharing a name are ONE node, and an alias
bound to an `ast.Attribute` is not followed. It OVER-approximates, which is the safe
direction — an extra edge is an extra pin a human reads, never a hidden escape. It is
NOT a precise call graph and must not be described as one.
"""

from __future__ import annotations

import ast
import pathlib

from tests.fencing.git_fence import _SINK_KIND, _callee_name

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "call_manager"

#: Sites that still call `_live` on a path to a sink. MIGRATION DEBT, not permission.
#: This list only SHRINKS: Task 4 removes the eight F3e1b-1 entries; F3e1b-2 removes
#: `save_section`, `insert_citation_for_user` and `_insert_citation_locked`; F3e2 and
#: F5 remove the rest. MEASURED at `8f7b1ef7`: all fourteen are load-bearing.
UNCONVERTED: frozenset[str] = frozenset(
    {
        # --- F3e1b-1: the seven §4.2 enclosing acquisitions, removed in Task 4 ---
        "apply_citation_to_room",
        "kernel_accept_prose",
        "kernel_init_spec",
        "kernel_accept_citation",
        "apply_annotation_insert_to_room",
        "post_lock_spec",
        "post_unlock_spec",
        # --- F3e1b-1: the choke point, removed in Task 4 ---
        "checkpoint_artifact",
        # --- F3e1b-2 ---
        "save_section",
        "insert_citation_for_user",
        "_insert_citation_locked",
        # --- F3e2 / F5 ---
        "insert_from_annotation",
        "post_init_spec._git",
        "Phase0Worker._m1",
    }
)


def _graph() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """`calls[qualname] -> callee final-names`, and `defines[name] -> qualnames`."""
    calls: dict[str, set[str]] = {}
    defines: dict[str, set[str]] = {}

    def walk(node: ast.AST, qual: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                sub = f"{qual}.{child.name}" if qual else child.name
                if not isinstance(child, ast.ClassDef):
                    defines.setdefault(child.name, set()).add(sub)
                walk(child, sub)
                continue
            if isinstance(child, ast.Call):
                name = _callee_name(child.func)
                if name:
                    calls.setdefault(qual, set()).add(name)
            walk(child, qual)

    for path in _SRC.rglob("*.py"):
        walk(ast.parse(path.read_text()), "")
    return calls, defines


def _reaches_sink(qual: str, calls, defines, seen: set[str] | None = None) -> bool:
    if seen is None:
        seen = set()
    if qual in seen:
        return False
    seen.add(qual)
    for name in calls.get(qual, ()):
        if name in _SINK_KIND:
            return True
        for target in defines.get(name, ()):
            if _reaches_sink(target, calls, defines, seen):
                return True
    return False


def check_tree() -> list[str]:
    """Every unpinned `_live` caller that reaches a sink."""
    calls, defines = _graph()
    problems: list[str] = []
    for qual, callees in sorted(calls.items()):
        if "acquire_commit_mutex_live" not in callees:
            continue
        if qual in UNCONVERTED:
            continue
        if _reaches_sink(qual, calls, defines):
            problems.append(f"{qual} :: calls acquire_commit_mutex_live and reaches a git sink")
    return problems
```

- [ ] **Step 5: Run to verify all three reach-gate tests pass**

Run: `uv run --python 3.12 pytest tests/fencing/test_live_reach_gate.py -q`
Expected: PASS, 3 passed.

If `test_the_tree_is_green_under_the_pin_list` fails, the pin list is missing a real
site. **Add it and say which — do not delete the assertion.**

- [ ] **Step 6: Write the failing tests for the §5.1 converted gate**

```python
# tests/fencing/test_converted_gate.py
_CONVERTED = (
    "def f(session, wid, stamp, repo):\n"
    "    acquire_commit_mutex_at_generation(session, wid, generation=stamp.generation)\n"
    "    return write_and_commit_files(repo, {}, 'm')\n"
)


def test_a_converted_site_passes():
    from tests.fencing.converted_gate import check_source

    assert check_source(_CONVERTED, "x/f.py") == []


def test_a_site_still_calling_live_is_rejected():
    """§5.3's first mutation, as a unit test."""
    from tests.fencing.converted_gate import check_source

    reverted = _CONVERTED.replace(
        "acquire_commit_mutex_at_generation(session, wid, generation=stamp.generation)",
        "acquire_commit_mutex_live(session, wid)",
    )
    assert any("acquire_commit_mutex_live" in p for p in check_source(reverted, "x/f.py"))


def test_an_effect_before_the_mutex_is_rejected():
    """§4.1: the effect is the `Doc` mutation, not `store.append_update`. Anchoring one
    line late accepts an implementation that mutates the live room before taking the
    mutex — the exact hollow gate §4.1 warns about."""
    from tests.fencing.converted_gate import check_source

    late = (
        "def f(session, wid, stamp, repo):\n"
        "    apply_pm_section_to_fragment(doc, section)\n"
        "    acquire_commit_mutex_at_generation(session, wid, generation=stamp.generation)\n"
        "    return write_and_commit_files(repo, {}, 'm')\n"
    )
    assert any("precede" in p for p in check_source(late, "x/f.py"))


def test_precedence_is_checked_for_a_DELEGATING_enclosing_acquisition():
    """The half revision 1 missed entirely.

    `post_lock_spec` holds no sink — it calls `checkpoint_artifact`. With only
    `_EFFECTS` to look for, its body contains no effect, so the precedence branch never
    ran and the gate checked nothing but "calls Primitive B". MEASURED: three of
    F3e1b-1's eight sites are this shape.
    """
    from tests.fencing.converted_gate import check_source

    late = (
        "def post_lock_spec(s, wid, stamp, ws):\n"
        "    cp = checkpoint_artifact(s, workspace_id=wid, stamp=stamp)\n"
        "    acquire_commit_mutex_at_generation(s, wid, generation=stamp.generation)\n"
        "    return cp\n"
    )
    problems = check_source(late, "x/f.py")
    assert any("precede" in p for p in problems), problems


def test_a_nested_def_is_judged_separately_from_its_parent():
    """F3e1a2's `_iter_own_nodes` trap. MEASURED: `ast.walk(outer)` reports the nested
    function's sink, so a naive gate blames `outer` for `inner`'s effect."""
    from tests.fencing.converted_gate import check_source

    nested = (
        "def outer(s, wid):\n"
        "    acquire_commit_mutex_live(s, wid)\n"
        "    def inner(repo):\n"
        "        return write_and_commit_files(repo, {}, 'm')\n"
        "    return inner\n"
    )
    problems = check_source(nested, "x/f.py")
    assert all("::outer.inner" in p for p in problems), problems


def test_a_comment_naming_the_primitive_does_not_satisfy_the_gate():
    """Constraint 4. The gate keys on parsed Call nodes; a comment is not a call."""
    from tests.fencing.converted_gate import check_source

    commented = (
        "def f(session, wid, stamp, repo):\n"
        "    # acquire_commit_mutex_at_generation(session, wid, generation=stamp.generation)\n"
        "    return write_and_commit_files(repo, {}, 'm')\n"
    )
    assert check_source(commented, "x/f.py") != []
```

- [ ] **Step 7: Run to verify they fail**

Run: `uv run --python 3.12 pytest tests/fencing/test_converted_gate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.fencing.converted_gate'`.

- [ ] **Step 8: Implement the converted gate**

**Copy this verbatim.** It was written to a file, executed against the tree, and passed
`ruff check` before being pasted here (Constraint 11). MEASURED: `check_tree()` returns
**8** at `8f7b1ef7` — exactly F3e1b-1's eight, the other nine filtered.

```python
# tests/fencing/converted_gate.py
"""Gate §5.1: a converted site takes Primitive B, never `_live`, before its first effect.

The lexical-precedence check is WEAKER than the real requirement — it cannot see a
call inside a helper, and lexical order is not execution order under branching. It is
a cheap ratchet, not the proof. **The proof is §5.2's runtime races.** Read
git_fence.py's module docstring: passing this does not mean the site is safe.

Every assertion keys on a parsed `ast.Call` node. Never on source text (§5.1).
"""

from __future__ import annotations

import ast
import pathlib

from tests.fencing.git_fence import _SINK_KIND, _callee_name
from tests.fencing.live_mutex_gate import ENCLOSING_ACQUISITIONS

_LIVE = "acquire_commit_mutex_live"
_AT_GENERATION = "acquire_commit_mutex_at_generation"

#: Callee names that count as "the first effect". §4.1: the effect is the `Doc`
#: mutation, NOT `store.append_update`.
#:
#: `append_update` is DELIBERATELY ABSENT and adding it "for completeness" is the
#: mistake §4.1 names. MEASURED: including it makes the gate report three
#: `web/collab_ws.py` functions — `collab_ws`, `_persist`, `_write` — that append to
#: the log over a websocket and touch no git sink at all.
_EFFECTS: frozenset[str] = frozenset(_SINK_KIND) | {
    "apply_pm_section_to_fragment",
    "apply_pm_json_to_fragment",
}

#: For an ENCLOSING acquisition, the delegated call IS its first effect — it holds no
#: sink of its own. Without this, `post_lock_spec`, `post_unlock_spec` and
#: `kernel_accept_citation` are checked for "calls Primitive B, not `_live`" and their
#: PRECEDENCE is never checked at all, because `_EFFECTS` finds nothing in their body.
#:
#: These names are NOT in `_EFFECTS`, deliberately. `_EFFECTS` applies to every
#: function in the tree; this set applies only to declared enclosing acquisitions.
#: Putting them in `_EFFECTS` was measured and rejected — it drags in every ordinary
#: caller (`save_version._do`, `checkpoint_active_tails`, `direct_cite_via_kernel._body`),
#: which take no mutex and are not §5.1's business.
_DELEGATED_EFFECTS: frozenset[str] = frozenset(
    {
        "checkpoint_artifact",
        "apply_citation_to_room",
    }
)

#: Sinks other slices own. `check_source` is scope-free so it can be unit-tested on
#: fixtures; `check_tree` is the tree gate and needs this. This list only SHRINKS.
NOT_YET_CONVERTED: frozenset[str] = frozenset(
    {
        "save_section",  # F3e1b-2
        "insert_citation_for_user",  # F3e1b-2
        "_insert_citation_locked",  # F3e1b-2
        "insert_from_annotation",  # F3e2
        "build_bibliography",  # F3e2
        "post_init_spec._git",  # F3e2
        "run_review_pass",  # F4
        "Phase0Worker._init_repo",  # F5
        "Phase0Worker._m1",  # F5
    }
)


def _own_calls(func: ast.AST) -> list[tuple[int, str]]:
    """`(lineno, callee-name)` for calls in THIS function's OWN scope.

    A nested `def`/`lambda`/`class` body belongs to the nested scope. `ast.walk`
    descends into them, so judging `outer` on `inner`'s effects is exactly F3e1a2's
    `_iter_own_nodes` trap — MEASURED: a fixture where `outer` takes `_live` and a
    nested `inner` holds the sink makes `ast.walk(outer)` report the sink, and the
    gate then blames the wrong function.
    """
    out: list[tuple[int, str]] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef
            ):
                continue
            if isinstance(child, ast.Call):
                name = _callee_name(child.func)
                if name:
                    out.append((child.lineno, name))
            walk(child)

    walk(func)
    return sorted(out)


def _judge(qual: str, path: str, ordered: list[tuple[int, str]], enclosing: bool) -> list[str]:
    """One function's verdict. Returns a list so callers can `extend`, never `+=`
    inside a closure — that rebinds the name and raises UnboundLocalError."""
    names = {n for _l, n in ordered}
    if _LIVE in names:
        return [f"{path}::{qual} still calls {_LIVE}"]
    acquisitions = [ln for ln, n in ordered if n == _AT_GENERATION]
    if not acquisitions:
        return [f"{path}::{qual} is in scope but never calls {_AT_GENERATION}"]
    effects_here = _EFFECTS | (_DELEGATED_EFFECTS if enclosing else frozenset())
    effects = [ln for ln, n in ordered if n in effects_here]
    if effects and min(acquisitions) > min(effects):
        return [
            f"{path}::{qual} — {_AT_GENERATION} must precede the first effect "
            f"(mutex at line {min(acquisitions)}, effect at line {min(effects)})"
        ]
    return []


def check_source(source: str, path: str) -> list[str]:
    """§5.1's scope, verbatim: "every `git_commit` site AND every enclosing acquisition".

    Both halves are required. MEASURED: effect-holders alone cover only 5 of F3e1b-1's
    8 sites — `kernel_accept_citation`, `post_lock_spec` and `post_unlock_spec`
    DELEGATE rather than hold an effect, so a direct-effect rule cannot see them.
    """
    problems: list[str] = []

    def visit(node: ast.AST, qual: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                sub = f"{qual}.{child.name}" if qual else child.name
                if not isinstance(child, ast.ClassDef):
                    ordered = _own_calls(child)
                    enclosing = sub in ENCLOSING_ACQUISITIONS
                    if enclosing or ({n for _l, n in ordered} & _EFFECTS):
                        problems.extend(_judge(sub, path, ordered, enclosing))
                visit(child, sub)
            else:
                visit(child, qual)

    visit(ast.parse(source), "")
    return problems


def check_tree() -> list[str]:
    """`check_source` over `src/`, minus the sinks other slices own."""
    root = pathlib.Path(__file__).resolve().parents[2]
    problems: list[str] = []
    for path in (root / "src").rglob("*.py"):
        rel = str(path.relative_to(root))
        problems.extend(
            p
            for p in check_source(path.read_text(), rel)
            if p.split("::", 1)[1].split(" ", 1)[0] not in NOT_YET_CONVERTED
        )
    return problems
```

- [ ] **Step 9: Run all six converted-gate tests**

Run: `uv run --python 3.12 pytest tests/fencing/test_converted_gate.py -q`
Expected: PASS, 6 passed.

There is deliberately **no** `check_tree() == []` assertion yet — it cannot be green
until Task 4, and a test red across three tasks masks unrelated failures.

- [ ] **Step 10: Suite, lint, commit**

Run: `uv run --python 3.12 pytest -n 16 --dist loadfile -q` and `uv run --python 3.12 ruff check .`
Expected: all pass, **1 skipped**; ruff `All checks passed!`. **Record the measured collected count.**
PREDICTED (not run): 3898 + 9 = 3907.

```bash
git add tests/fencing/f3e1b1_sites.py tests/fencing/live_reach_gate.py \
        tests/fencing/test_live_reach_gate.py tests/fencing/converted_gate.py \
        tests/fencing/test_converted_gate.py
git commit -m "test(fence): the declared-site catalog and both F3e1b gates (F3e1b-1 Task 1)"
```

---

### Task 2: Stamp plumbing and stamp identity — no behaviour change

Every path carries a `FenceStamp`; **every mutex call is still `_live`.** Primitive B
gains its identity check now, while it still has zero callers, so Task 4 flips into a
primitive that already refuses a mis-threaded stamp.

**Files:**
- Modify: `fencing.py`, `ports/errors.py`, `collab/checkpoint.py`, `collab/kernel.py`, `web/api/workspaces.py`, `citations/collab_cite.py`, `proposals/kernel_accept.py`, `collab/handover.py`, `collab/permissions.py`, `web/collab_checkpoint.py`
- Create: `tests/proposal_assist/test_f3e1b1_conversions.py`

**Interfaces:**
- Consumes: `ports.stamp.FenceStamp` (frozen, `slots=True`, `kw_only=True`: `workspace_id`, `generation`, `active_turn_job_id`, `origin_job_id`) and `stamp_for_workspace(workspace) -> FenceStamp` (raises `UnfencedInvocation` when `lifecycle_generation is None`; **generation 0 is REAL**, so it tests `None`, never falsiness).
- Produces: `checkpoint_artifact(session, *, workspace_id, artifact, workspaces_dir, stamp: FenceStamp, extra_files=None, record_version=True, record_event=None)`; `acquire_commit_mutex_at_generation(session, workspace_id, *, generation: int, stamp: FenceStamp, timeout_seconds: int = 10)`; `FenceStampMismatch(RuntimeError)` in `ports/errors.py`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/proposal_assist/test_f3e1b1_conversions.py
"""F3e1b-1 — the choke point and the seven enclosing acquisitions.

Each converted site against its stamp, and each §5.2 race with the compensation
disabled: "a conversion test that passes with the fence removed is measuring the
compensation, not the fence" (spec 2026-08-18 rev 8 §5).
"""

from __future__ import annotations

import inspect
import uuid

import pytest


def test_checkpoint_artifact_requires_a_stamp():
    """Keyword-only, no default. A defaulted stamp is a hole: a caller that forgets one
    still type-checks and still runs."""
    from call_manager.proposal_assist.collab.checkpoint import checkpoint_artifact

    param = inspect.signature(checkpoint_artifact).parameters.get("stamp")
    assert param is not None
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


def test_primitive_b_requires_a_stamp_with_no_default():
    """MEASURED at `8f7b1ef7`: Primitive B has ZERO production callers — the only
    mentions in `src/` are its own docstring and its `__all__` entry. The parameter is
    therefore REQUIRED; a compatibility default would be a hole with nothing to be
    compatible with."""
    from call_manager.proposal_assist.fencing import acquire_commit_mutex_at_generation

    param = inspect.signature(acquire_commit_mutex_at_generation).parameters.get("stamp")
    assert param is not None
    assert param.default is inspect.Parameter.empty


@pytest.mark.parametrize(
    "module_path,func_name",
    [
        ("call_manager.proposal_assist.citations.collab_cite", "direct_cite_via_kernel"),
        ("call_manager.proposal_assist.citations.collab_cite", "init_spec_via_kernel"),
        ("call_manager.proposal_assist.citations.collab_cite", "insert_annotation_via_kernel"),
        ("call_manager.proposal_assist.proposals.kernel_accept", "accept_via_kernel"),
    ],
)
def test_each_orchestrator_carries_a_stamp(module_path, func_name):
    """§7.2: these carry only `workspace_id` and open their own sessions inside
    `_body()`, so the route's Workspace is discarded before they are entered."""
    import importlib

    func = getattr(importlib.import_module(module_path), func_name)
    assert "stamp" in inspect.signature(func).parameters


def test_a_nested_acquisition_with_a_DIFFERENT_stamp_raises(db_session, seeded_workspace):
    """§7.2's invariant, ENFORCED rather than documented.

    The inner acquisition must receive THE SAME OBJECT. Re-minting under a held mutex
    reads the same generation, so a field-equality check passes and the wrong-stamp
    caller is hidden. The second stamp here has IDENTICAL FIELDS and must still raise.
    """
    from call_manager.proposal_assist.fencing import acquire_commit_mutex_at_generation
    from call_manager.proposal_assist.ports.errors import FenceStampMismatch
    from call_manager.proposal_assist.ports.stamp import FenceStamp

    wid = seeded_workspace
    fields = dict(workspace_id=wid, generation=0, active_turn_job_id=None, origin_job_id=None)
    outer = FenceStamp(**fields)
    twin = FenceStamp(**fields)  # equal, NOT identical
    assert outer == twin

    acquire_commit_mutex_at_generation(db_session, wid, generation=0, stamp=outer)
    # re-entrant with the SAME object is fine (advisory xact locks are re-entrant)
    acquire_commit_mutex_at_generation(db_session, wid, generation=0, stamp=outer)
    with pytest.raises(FenceStampMismatch):
        acquire_commit_mutex_at_generation(db_session, wid, generation=0, stamp=twin)


def test_a_generation_disagreeing_with_its_stamp_raises(db_session, seeded_workspace):
    """`generation=` and `stamp.generation` must agree, or the stamp is decorative."""
    from call_manager.proposal_assist.fencing import acquire_commit_mutex_at_generation
    from call_manager.proposal_assist.ports.errors import FenceStampMismatch
    from call_manager.proposal_assist.ports.stamp import FenceStamp

    wid = seeded_workspace
    stamp = FenceStamp(
        workspace_id=wid, generation=0, active_turn_job_id=None, origin_job_id=None
    )
    with pytest.raises(FenceStampMismatch):
        acquire_commit_mutex_at_generation(db_session, wid, generation=1, stamp=stamp)


def test_stamp_for_workspace_refuses_a_workspace_with_no_generation():
    """Mirrors `Invocation.stamp()`'s `UnfencedInvocation` — asserted, not assumed."""
    from call_manager.proposal_assist.ports.errors import UnfencedInvocation
    from call_manager.proposal_assist.ports.stamp import stamp_for_workspace

    class _NoGeneration:
        id = uuid.uuid4()
        lifecycle_generation = None

    with pytest.raises(UnfencedInvocation):
        stamp_for_workspace(_NoGeneration())


def test_a_zero_generation_is_accepted():
    """The falsiness trap, pinned. `lifecycle_generation` has server_default 0, so 0 is
    a REAL generation and `if not workspace.lifecycle_generation` would reject it."""
    from call_manager.proposal_assist.ports.stamp import stamp_for_workspace

    class _Zero:
        id = uuid.uuid4()
        lifecycle_generation = 0

    assert stamp_for_workspace(_Zero()).generation == 0
```

> The implementer wires `db_session` / `seeded_workspace` to this repo's existing
> fixtures and `seed_workspace(session, *, generation=0)` from
> `tests/proposal_assist/f3b_seeds.py`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --python 3.12 pytest tests/proposal_assist/test_f3e1b1_conversions.py -q`
Expected: FAIL. **FAILED, not ERROR** — an ERROR means a broken import; fix that first.

- [ ] **Step 3: Give Primitive B the stamp and the identity check**

Add `FenceStampMismatch(RuntimeError)` to `ports/errors.py`. It is a **programming
defect, not lifecycle staleness** — it must NOT join any §8 clean-error tuple in Task 3,
and must stay loud.

In `fencing.py`, `acquire_commit_mutex_at_generation` gains a **required** keyword
`stamp: FenceStamp` and, **before** its generation query:

1. Raise `FenceStampMismatch` if `stamp.workspace_id != workspace_id` or
   `stamp.generation != generation`.
2. Bind the stamp object under `session.info[("f3e1b_stamp", workspace_id)]` if absent.
3. If already bound, require `bound is stamp` — **identity, not equality** — and raise
   `FenceStampMismatch` otherwise, before acquiring or querying again.
4. Clear the binding when the transaction ends, so a later transaction on the same
   `Session` may bind a fresh stamp.

Keep everything else: `CommitMutexTimeout`, `WorkspaceDeleted` (which takes precedence
over stale), `WorkspaceGenerationStale`, and mutating nothing in every failure case.

- [ ] **Step 4: Thread the stamp, self-contained sites first**

`checkpoint_artifact` gains `stamp: FenceStamp` in its signature only. **Do not touch its
`acquire_commit_mutex_live(session, workspace_id)` call at `:91` — that is Task 4.**

| Call site | Add |
|---|---|
| `workspaces.py:665` (`post_lock_spec`) | `stamp=stamp_for_workspace(ws)` — `ws` from `require_owner` at `:645` |
| `workspaces.py:746` (`post_unlock_spec`) | `stamp=stamp_for_workspace(ws)` — `ws` from `require_owner` at `:734` |
| `collab_checkpoint.py:52` (`save_version`) | `stamp=stamp_for_workspace(<the Workspace resolve_access loaded at :38>)` |
| `handover.py:40` | `stamp=stamp_for_workspace(ws)` — pre-screen at `:36-38` |
| `permissions.py:58` | `stamp=stamp_for_workspace(ws)` — pre-screen at `:53-55` |
| `checkpoint.py:210` | `stamp=stamp_for_workspace(ws)` — pre-screen at `:205-207` |

§7.3: the three maintenance callers act for no one, and all three already run the
identical `ws = s.get(Workspace, workspace_id)` + `soft_deleted_at` pre-screen
immediately before calling. **That instance is the mint site**, exactly as a route's
access check is. No special case and no exemption from the gate.

- [ ] **Step 5: Thread the four orchestrator paths**

`direct_cite_via_kernel` (`collab_cite.py:49`), `init_spec_via_kernel` (`:99`),
`insert_annotation_via_kernel` (`:149`) and `accept_via_kernel` (`kernel_accept.py:56`)
each gain `stamp: FenceStamp` and pass it to their kernel function. Each kernel function
— `apply_citation_to_room` (`:136`), `kernel_accept_prose` (`:208`), `kernel_init_spec`
(`:325`), `kernel_accept_citation` (`:369`), `apply_annotation_insert_to_room` (`:443`)
— gains `stamp: FenceStamp` and passes it on.

**`kernel_accept_citation` passes its stamp to `apply_citation_to_room`.**

**Pass the same object inward; never re-mint.** The four routes mint per §7.1's table.
**No authorization call changes** (Constraint 10).

- [ ] **Step 6: Run the new tests, then the full suite**

Run: `uv run --python 3.12 pytest tests/proposal_assist/test_f3e1b1_conversions.py -q`
Expected: PASS.

Run: `uv run --python 3.12 pytest -n 16 --dist loadfile -q`
Expected: all pass, 1 skipped. **Record the measured count.**

Behaviour has not changed, so any other test that now fails is a threading defect, not
an expected delta. Do not absorb it into a number (Constraint 6).

- [ ] **Step 7: Confirm the gates did NOT move**

Run: `uv run --python 3.12 pytest tests/fencing/ -q`
Expected: PASS, unchanged. No mutex call changed and no pin left the list, so a gate
result that moved here means the gate keys on something other than the mutex call —
**a finding about Task 1's gate.**

- [ ] **Step 8: Commit**

```bash
git add -A src/ tests/proposal_assist/test_f3e1b1_conversions.py
git commit -m "feat(fence): thread a FenceStamp and enforce its identity, no behaviour change (F3e1b-1 Task 2)"
```

---

### Task 3: The §8 stale-generation outcome contract

Lands **before** conversion, so the first revision that can produce
`WorkspaceGenerationStale` already handles it. Tests inject the exception directly and so
depend on no converted caller.

**Files:**
- Create: `tests/proposal_assist/test_f3e1b1_stale_outcomes.py`
- Modify: the global HTTP handler, the three §8 clean-error tuples, and the lifecycle-race branch of both maintenance catch shapes.

- [ ] **Step 1: Read spec §8 and enumerate the matrix**

Open `docs/superpowers/specs/2026-08-21-fence-f3e1-git-mechanism-design.md` §8 and list,
in the test module docstring, each outcome it specifies and where it is enforced. **If §8
and this task disagree, §8 governs — say so and stop.**

- [ ] **Step 2: Write the failing tests**

Cover, by injecting `WorkspaceGenerationStale` at the boundary:

- **Global HTTP handler** — the canonical non-disclosing 404, not a 500.
- **Each of the three §8 clean-error tuples** — the same outward outcome as
  `WorkspaceDeleted`: no eviction, no 500.
- **Both maintenance catch shapes**, for all three callers — rollback, benign skip, and
  **no error-level log**.
- **Precedence** — a deleted or missing workspace with a mismatched generation still
  raises `WorkspaceDeleted`, never stale.
- **Control cases** — an unrelated exception keeps its existing 500 / eviction /
  error-level behaviour. Without these, widening a handler to `except Exception` would
  pass every test above.
- **`FenceStampMismatch` is NOT in any clean-error tuple** — a programming defect must
  stay loud. Assert it still surfaces as a 500.

- [ ] **Step 3: Run to verify each fails on its named assertion**

Run: `uv run --python 3.12 pytest tests/proposal_assist/test_f3e1b1_stale_outcomes.py -q`
Expected: FAIL. A test that passes immediately means that outcome is already correct —
**record which, and keep the test.**

- [ ] **Step 4: Make the narrow changes only**

Register the stale exception with the existing 404 handler; add that exact class to the
three existing tuples; add it to the lifecycle-race branch of both maintenance catch
shapes. **Do not add retries and do not broaden any `except` to `Exception`** — a broad
catch conceals programming failures and would make the control cases fail.

- [ ] **Step 5: Suite, lint, commit**

Run the full suite and `ruff check .`. **Record the measured count.**

```bash
git add -A src/ tests/proposal_assist/test_f3e1b1_stale_outcomes.py
git commit -m "feat(fence): the stale-generation outcome contract, before any converter (F3e1b-1 Task 3)"
```

---

### Task 4: THE ATOMIC CONVERSION — one commit

Read Constraint 2 before starting. **Everything here lands in a single commit:** the
checkpoint flip, all seven enclosing flips, the live-record updates, the pin removals,
and the §5.2 runtime races. There is no intermediate green state, so do not create one.

**Files:**
- Modify: `collab/checkpoint.py:91`, `collab/kernel.py` (`:136`, `:208`, `:325`, `:369`, `:443`), `web/api/workspaces.py` (`:651`, `:739`)
- Modify: `tests/fencing/live_mutex_gate.py` (records), `tests/fencing/live_reach_gate.py` (pins)
- Modify: `tests/proposal_assist/test_f3e1b1_conversions.py`, `tests/fencing/test_converted_gate.py`

**Interfaces:**
- Consumes: Task 2's threaded stamp and stamp-identity primitive; Task 3's outcome contract; Task 1's catalog and gates.
- Produces: a green `converted_gate.check_tree()`, and `ENCLOSING_ACQUISITIONS` reduced to `{"Phase0Worker._m1": "F5"}`.

- [ ] **Step 1: Write the failing tree-level gate tests**

```python
# tests/fencing/test_converted_gate.py  (append)
def test_the_converted_gate_is_green_over_the_tree():
    """§5.1 over the real tree. MEASURED before conversion: `check_tree()` returns 8 —
    exactly F3e1b-1's eight, the other nine filtered by `NOT_YET_CONVERTED`."""
    from tests.fencing.converted_gate import check_tree

    assert check_tree() == []


def test_every_declared_site_positively_takes_primitive_b():
    """Two-sided. A green `check_tree()` also passes if a declared site VANISHED — the
    gate reports nothing about a function that is not there. This asserts presence."""
    import ast
    import pathlib

    from tests.fencing.converted_gate import _own_calls
    from tests.fencing.f3e1b1_sites import DECLARED_SITES

    root = pathlib.Path(__file__).resolve().parents[2]
    missing = []
    for rel, qual in sorted(DECLARED_SITES):
        found = False

        def visit(node, prefix=""):
            nonlocal found
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                    sub = f"{prefix}.{child.name}" if prefix else child.name
                    if sub == qual and not isinstance(child, ast.ClassDef):
                        names = {n for _l, n in _own_calls(child)}
                        if "acquire_commit_mutex_at_generation" in names:
                            found = True
                    visit(child, sub)
                else:
                    visit(child, prefix)

        visit(ast.parse((root / rel).read_text()))
        if not found:
            missing.append((rel, qual))
    assert missing == [], f"declared sites not taking Primitive B: {missing}"
```

- [ ] **Step 2: Run to verify both fail**

Run: `uv run --python 3.12 pytest tests/fencing/test_converted_gate.py -q`
Expected: FAIL — 8 `still calls acquire_commit_mutex_live` entries, and 8 missing
declared sites.

- [ ] **Step 3: Flip all eight**

Replace `acquire_commit_mutex_live(session, workspace_id)` with

```python
    acquire_commit_mutex_at_generation(
        session, workspace_id, generation=stamp.generation, stamp=stamp
    )
```

at `checkpoint.py:91` and at each of the seven.

**The mutex must lexically precede the first effect** (§5.1), and §4.1 is explicit that
the first effect is the **`Doc` mutation**, not `store.append_update`. In `kernel.py` the
acquisition goes above `apply_pm_section_to_fragment` (`:169`, `:274`, `:517`) and
`apply_pm_json_to_fragment` (`:338`) — **not merely above the `checkpoint_artifact`
call.** Anchoring it one line late accepts an implementation that mutates the live room
before taking the mutex: the exact defect §4.2 describes.

Rewrite `checkpoint.py`'s `_live:` comment block at `:87-90` — it explains a refusal on
deletion only, and the refusal is now also on generation.

**Do not re-mint inside any of these.** They receive their stamp from Task 2.

- [ ] **Step 4: Update the records and pins in the same working tree**

- `live_mutex_gate.ENCLOSING_ACQUISITIONS` — remove the seven; it becomes
  `{"Phase0Worker._m1": "F5"}`.
- `live_mutex_gate.LIVE_ACQUIRING_SINK_HOLDERS` — remove `checkpoint_artifact` **only**.
  MEASURED: it is the sole in-scope member; the other five are F3e1b-2's and F3e2's.
- `live_mutex_gate.LIVE_ACQUIRERS_WITHOUT_A_SINK` — **unchanged**. MEASURED: none of the
  eight is a member.
- `live_reach_gate.UNCONVERTED` — remove the eight F3e1b-1 entries. Six remain.
- `converted_gate.NOT_YET_CONVERTED` — **unchanged**. It never held the eight.

`test_the_live_acquirers_partition_exactly` must stay green throughout: the eight leave
the AST-discovered `_live` set and the literal records in the same commit.

- [ ] **Step 5: Write the §5.2 races — both cases, every entry point**

For each of the seven §7.1 entry points this slice converts — `POST …/cite` (collab),
accept (collab, prose), accept (collab, citation), `POST …/spec/init` (collab),
`POST …/annotations/{id}/insert`, `POST …/spec/lock`, `POST …/spec/unlock` — **plus the
four non-enclosing `checkpoint_artifact` entry paths** (`save_version`, the evict hook,
the kill-switch handover, the periodic tail checkpoint), which now refuse on generation
and which revision 1 omitted entirely.

**Case A — effect first, lifecycle second.** The effect takes the mutex at G and commits;
the delete/restore blocks and proceeds after. The write **lands**, and that is correct.
*Case A is what proves the fence does not simply refuse everything* — without it, an
implementation that refuses unconditionally passes every Case B.

**Case B — lifecycle first, effect second.** Delete+restore commits to G+2, then the
operation proceeds. It must raise and land **nothing**. Assert all five, every time:

```python
    assert head_before == _git(repo, "rev-parse", "HEAD")          # HEAD unchanged
    assert _git(repo, "status", "--porcelain") == ""               # worktree clean
    assert path.read_bytes() == before_bytes                       # byte-identical
    assert yjs_update_count(session, wid) == n0                    # no yjs_updates row
    assert doc_state_before == _doc_state(room)                    # room Doc unmutated
```

§5.2 is explicit: a proof that inspects only rows passes while bytes are already on disk;
an unchanged HEAD is not enough, because a stale implementation can dirty the working
tree without committing; and cleanliness alone passes an implementation that wrote and
then reverted. **For the kernel paths the last two are not optional** — a test that
checked only git would pass the exact defect §4.2 describes.

**Disable the compensation in every one of these tests.** Cancel-on-delete is still in
the tree and masks staleness for statements touching the same row; a conversion test that
passes with the fence removed is measuring the compensation, not the fence. This is the
technique that exposed Slice C1's decorative tests. Follow the precedent in
`tests/proposal_assist/test_f3d2_conversions.py`.

**Also prove the threading, per edge:**

```python
def test_the_inner_acquisition_receives_the_SAME_stamp_object(monkeypatch):
    """§7.2. Spy every outer→inner edge and assert `received is supplied`, not `==`.
    A re-mint under the held mutex reads the same generation, so equality passes and the
    mis-threading is invisible."""
```

- [ ] **Step 6: Collaborator authorization parity (spec crit-13)**

```python
def test_a_collaborator_with_write_access_can_still_cite_and_accept():
    """Constraint 10 says no authorization call changes. This ASSERTS it rather than
    trusting it. The collab cite branch authorizes with `resolve_access` (ANY member
    with write); only the flag-off branch uses `require_owner`. Revision 2 of the spec
    guessed wrong here and would have revoked collaborator access."""
```

- [ ] **Step 7: Prove the gates bite — §5.3's mutations**

Commit the work first, then mutate, then restore **from a copy**. `git checkout <path>`
destroys uncommitted work in the same file.

| Mutation | Must fail |
|---|---|
| Revert `apply_citation_to_room` to `_live` | reach gate, naming that function — **§5.3 says this one is not optional** |
| Move `apply_citation_to_room`'s acquisition below `apply_pm_section_to_fragment` | converted gate, on `must precede the first effect` |
| Delete `post_lock_spec`'s acquisition entirely | `test_every_declared_site_positively_takes_primitive_b` |
| Hand an inner acquisition an equal-but-distinct stamp | `FenceStampMismatch` |
| Remove the fence from one Case B site | that Case B test — if it still passes, it was measuring the compensation |

Record each failure's **exact text**. A mutation that reddens the wrong test, or reddens
by aborting collection, proves nothing (Constraint 5).

- [ ] **Step 8: Suite, lint, ONE commit**

Run: `uv run --python 3.12 pytest -n 16 --dist loadfile -q` and `uv run --python 3.12 ruff check .`
Expected: all pass, 1 skipped. **Record the measured count.**

```bash
git add -A src/ tests/
git commit -m "feat(fence): the choke point and seven enclosing acquisitions take the mutex at a generation (F3e1b-1 Task 4)"
```

---

### Task 5: Inventory, slice status, and the identity checks

**Files:**
- Modify: `tests/fencing/inventory.py` (`SLICE_STATUS`), `tests/fencing/fence_inventory.json` (regenerated)
- Create: `tests/fencing/test_f3e1b1_slice.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/fencing/test_f3e1b1_slice.py
def test_this_slice_is_recorded_complete():
    """F3e1b-1 owns no NEW inventory row, so a row-iterating test never reads its
    status. F3e1a2 shipped exactly this hole: deleting its status key left the whole
    fencing suite green."""
    from tests.fencing.inventory import SLICE_STATUS

    assert SLICE_STATUS["F3e1b-1"] == "complete"


def test_f3e1b_still_owns_exactly_four_pending_git_rows():
    """IDENTITY, not membership. Revision 1 asserted
    `{r["slice"] for r in rows if r["slice"] == "F3e1b"} == {"F3e1b"}`, which is True
    for one row and for four — it proved "at least one exists". MEASURED: there are
    four, and they are F3e1b-2's to convert."""
    from tests.fencing.inventory import SLICE_STATUS, load_inventory

    rows = [r for r in load_inventory() if r["kind"] == "git_commit"]
    assert len(rows) == 9
    assert {r["symbol"] for r in rows if r["slice"] == "F3e1b"} == {
        "_insert_citation_locked",
        "insert_citation_for_user",
        "checkpoint_artifact",
        "save_section",
    }
    assert SLICE_STATUS["F3e1b"] == "pending"
    assert all(r["fenced"] is False for r in rows)


def test_the_reach_gate_pin_list_is_exactly_the_six_that_remain():
    """Identity, not count: naming them is what stops a swap."""
    from tests.fencing.live_reach_gate import UNCONVERTED

    assert UNCONVERTED == frozenset(
        {
            "save_section",
            "insert_citation_for_user",
            "_insert_citation_locked",
            "insert_from_annotation",
            "post_init_spec._git",
            "Phase0Worker._m1",
        }
    )


def test_the_exemption_pins_are_unchanged_by_this_slice():
    """Revision 1 checked this with `git diff BASE..HEAD -- inventory.py`, which reports
    only whether the whole FILE changed, never the pin identities — and compares two
    COMMITS, so it could not see this task's own uncommitted edit. Assert the parsed
    collection instead.

    NOT `len(...) == 5`: pins are designed to only SHRINK, and a literal count would
    block a later slice's legitimate cleanup. Assert the exact names this slice must not
    disturb.
    """
    from tests.fencing.inventory import (
        NON_SEMANTIC_PINS,
        RETAINED_PINS,
        TOOLS_NOT_YET_CONVERTED,
    )

    # Fill these from the tree at slice start; MEASURED at 8f7b1ef7:
    # TOOLS_NOT_YET_CONVERTED has 5 entries, NON_SEMANTIC_PINS 3, RETAINED_PINS 1.
    assert set(TOOLS_NOT_YET_CONVERTED) == {...}
    assert set(NON_SEMANTIC_PINS) == {...}
    assert set(RETAINED_PINS) == {...}
```

- [ ] **Step 2: Run to verify they fail**

Expected: FAIL — `KeyError: 'F3e1b-1'`.

- [ ] **Step 3: Add the status and regenerate the inventory**

Add `"F3e1b-1": "complete",` to `SLICE_STATUS`. **Do not hand-edit
`fence_inventory.json`** — regenerate it via `tests/fencing/regenerate_inventory.py`.
§5.3: hand-editing a row's `fenced` to `true` must not survive regeneration.

- [ ] **Step 4: Suite, lint, commit**

Run the full suite and `ruff check .`. Expected: all pass, **1 skipped**.
**Record the measured collected count.** Do not reconcile it by editing a prediction.

```bash
git add tests/fencing/
git commit -m "test(fence): F3e1b-1 recorded complete, eight pins gone (F3e1b-1 Task 5)"
```

---

## What this slice does NOT prove

State this in the final review rather than discovering it later.

- **Passing the converted gate does not mean a site is safe.** §5.1 checks lexical order;
  the commit mutex is transaction-scoped, so `acquire(); s.commit(); sink()` passes and
  is not fenced. A helper that commits is invisible to it entirely. The proof is Task 4's
  runtime races.
- **The reach gate over-approximates.** Its call graph is name-based — two same-named
  functions are one node, and an alias bound to an `ast.Attribute` is not followed. Safe
  direction, but not a precise call graph.
- **Six sinks remain unconverted**: `save_section`, `insert_citation_for_user`,
  `_insert_citation_locked` (F3e1b-2), `insert_from_annotation` and `post_init_spec._git`
  (F3e2), `Phase0Worker._m1` (F5). Their rows stay `pending` and their pins stay listed.
- **Task 4 is one commit**, so `git bisect` cannot separate the eight flips from each
  other. That is the deliberate cost of Constraint 2.

## What revision 1 got wrong

Kept so a future reader does not reintroduce any of it. Items 1–3 were Critical.

1. **The converted gate could not run.** `problems += _judge(...)` inside a closure
   rebinds the name and raises `UnboundLocalError`. It had been prototyped with
   `.append()` and re-typed differently into the plan — hence Constraint 11.
2. **Tasks 3 and 4 were separate commits.** Either order leaves a live half-conversion or
   a red boundary. Now one atomic commit (Constraint 2).
3. **The gates keyed off the live-acquirer records**, which Task 4 must edit — so the
   conversion would have narrowed the gate's own scope to nothing while its test stayed
   green. Hence `f3e1b1_sites.DECLARED_SITES`.
4. **The pin-mutation test targeted `apply_citation_to_room`**, a pin Task 4 deletes;
   afterwards it removed a pin that was not there and could never redden.
5. **`append_update` was in the effect set** — the mistake §4.1 names. MEASURED: three
   `collab_ws.py` false positives.
6. **The gate reported bare function names**, so `post_init_spec._git` appeared as two
   separate entries.
7. **It used `ast.walk` per function**, judging an outer function on its nested
   function's effects — F3e1a2's `_iter_own_nodes` trap.
8. **Direct-effect scoping covered only 5 of 8 sites**; three delegate.
9. **No task implemented §8's outcome matrix**, so the first converted revision would
   have produced 500s and evictions on a stale generation.
10. **The four non-enclosing `checkpoint_artifact` entry paths had no runtime proof.**
11. **The "four rows" assertion proved "at least one".**
12. **The exemption-pin check examined no pins**, and compared two commits, so it could
    not see the task's own edit.
13. **Primitive B was to take an OPTIONAL `stamp`** for compatibility with callers that
    do not exist — MEASURED: zero production callers, so the parameter is required.
14. **The same-stamp-threading invariant was mandated and never tested.** Now enforced in
    the primitive and spied per edge.
15. **Gate code was 101 characters wide** and would have failed `ruff check` (limit 100).
