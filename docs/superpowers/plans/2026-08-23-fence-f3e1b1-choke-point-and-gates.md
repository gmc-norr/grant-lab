# Fence F3e1b-1 — the choke point and both gates — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Revision 2.** Revision 1 was reviewed by `gpt-5.6-sol` and returned 17 findings — three Critical, all confirmed by running them. Revision 2 restructures the task sequence around the worst of them. Everything revision 1 got wrong is recorded at the end, so a future reader does not reintroduce it.

**Goal:** Convert `checkpoint_artifact` and the seven §4.2 enclosing acquisitions from `acquire_commit_mutex_live` to `acquire_commit_mutex_at_generation`, carrying a `FenceStamp` minted at each semantic boundary, and land the two gates that keep them converted.

**Architecture:** Plumbing, then contract, then **one atomic conversion**. Tasks 1–3 are each green and independently reviewable: they add the gates, thread the stamp with no behaviour change, and make the stale outcome correct *before* anything can produce it. Task 4 flips all eight acquisitions, updates the records and removes the pins in a **single commit**, because no split ordering leaves both a green boundary and a safe tree (Constraint 2).

**Tech Stack:** Python 3.12, pytest (+ testcontainers Postgres), `ast` for the gates. No new dependencies.

## Global Constraints

Every task's requirements implicitly include this section.

0. **The slice is called `F3e1b-1` in prose; its `SLICE_STATUS` key is `"F3e1b1"`, unhyphenated.** MEASURED: every existing key is unhyphenated (`F3a`, `F3d1`, `F3d2`, `F3e1a`, `F3e1a2`) — a sub-slice appends a character, never a separator. Typing the prose name as the key would make it the first hyphenated entry in the collection.
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
11. **Every code block here — implementation AND test — must be executed before it is written down, and RE-executed after any edit to what it depends on.** Revision 3 changed a gate's data shape from `str` to `(path, str)`, verified the gate, and left revision 2's tests of that gate untouched: the pin-mutation test then removed a bare string from a set of tuples, deleted nothing, and could never redden. MEASURED. Running the implementation is not enough — run the assertions that ride on it.
12. **Before this plan is handed to anyone, every ```python block is dedented, parsed, and name-resolved.** A block that does not parse is ALWAYS a failure — that rule is what catches `<angle-bracket placeholders>`. A block that is not a whole unit declares `# SCAN: fragment` (parse only); a block appended to an existing test file declares `# SCAN: names-from <path>`, and the scan then resolves its free names against that file and **fails if a named helper does not exist there**. Rounds 1 and 9 each shipped a block that could not run — an `UnboundLocalError` from `+=` in a closure, and a `NameError` from using `_callee_name` unimported. Reviewers found both, which wastes a review round on something a seconds-long pass finds mechanically. MEASURED across revisions: the scan found a fixture parameter never bound; three baseline constants never defined; and — once it was made to fail on unparseable blocks instead of skipping them — two `<...>` placeholders that had survived NINE review rounds, one of them in a test built entirely from invented fixtures, routes, and helpers. The skip was the bug. Both rules are proven by mutation: reintroducing a placeholder gives `BLOCK 13: DOES NOT PARSE`, and inventing a helper gives `BLOCK 13: UNBOUND ['_totally_invented_helper'] (not in …)`.
13. **Code in this plan was executed before it was written down.** Where a block says MEASURED, it was run against the tree at `8f7b1ef7` and passed `ruff check`. Revision 1 shipped a gate whose `problems += …` inside a closure raised `UnboundLocalError` on every call — it had been prototyped with `.append()` and re-typed differently into the plan. Copy these blocks verbatim; do not "tidy" them.

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
        # A TUPLE. `original - {"save_section"}` removes nothing from a set of tuples,
        # so the mutation is inert and the test fails for an unrelated reason.
        target = ("src/call_manager/proposal_assist/editing/save.py", "save_section")
        assert target in original, "the pin moved; update this target"
        gate.UNCONVERTED = frozenset(original - {target})
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
            _path, qual = pin
            if not any(qual in p for p in gate.check_tree()):
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

The call graph is name-based. It over-approximates on SHARED NAMES (two functions with
one name are one node — extra edges, extra pins, safe) but **UNDER-approximates on
aliases**: `f = mod.checkpoint_artifact; f(...)` creates an edge this graph does not
follow, and an under-reported edge is a MISSED pin, not an extra one. Do not describe
this gate as purely conservative. It is a ratchet over the shapes production actually
uses; the proof is §5.2's runtime races. If a future slice introduces an aliased sink
call, this gate goes quiet — `test_every_declared_site_positively_takes_primitive_b`
is the two-sided check that does not.
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
#: PATH-QUALIFIED, for the same reason `converted_gate.NOT_YET_CONVERTED` is. An
#: earlier revision path-qualified that one and left this one bare — a new function
#: named `save_section` anywhere in the tree would silently inherit the exemption.
UNCONVERTED: frozenset[tuple[str, str]] = frozenset(
    {
        # --- F3e1b-1: the seven §4.2 enclosing acquisitions, removed in Task 4 ---
        ("src/call_manager/proposal_assist/collab/kernel.py", "apply_citation_to_room"),
        ("src/call_manager/proposal_assist/collab/kernel.py", "kernel_accept_prose"),
        ("src/call_manager/proposal_assist/collab/kernel.py", "kernel_init_spec"),
        ("src/call_manager/proposal_assist/collab/kernel.py", "kernel_accept_citation"),
        ("src/call_manager/proposal_assist/collab/kernel.py", "apply_annotation_insert_to_room"),
        ("src/call_manager/web/api/workspaces.py", "post_lock_spec"),
        ("src/call_manager/web/api/workspaces.py", "post_unlock_spec"),
        # --- F3e1b-1: the choke point, removed in Task 4 ---
        ("src/call_manager/proposal_assist/collab/checkpoint.py", "checkpoint_artifact"),
        # --- F3e1b-2 ---
        ("src/call_manager/proposal_assist/editing/save.py", "save_section"),
        ("src/call_manager/proposal_assist/citations/human_cite.py", "insert_citation_for_user"),
        ("src/call_manager/proposal_assist/citations/core.py", "_insert_citation_locked"),
        # --- F3e2 / F5 ---
        ("src/call_manager/proposal_assist/analysis/insert.py", "insert_from_annotation"),
        ("src/call_manager/web/api/workspaces.py", "post_init_spec._git"),
        ("src/call_manager/proposal_assist/phases/setup.py", "Phase0Worker._m1"),
    }
)


def _graph() -> tuple[dict[tuple[str, str], set[str]], dict[str, set[tuple[str, str]]]]:
    """`calls[(path, qualname)] -> callee names` and `defines[name] -> {(path, qualname)}`.

    Keyed on (path, qualname) THROUGHOUT. An earlier revision keyed `calls` on the bare
    qualname and looked the path up afterwards — which collapses two same-named functions
    in different files into one node, so whichever was parsed first decided the path, and
    a path-qualified pin could be applied to the wrong one. Path-qualifying the pin list
    while the graph behind it is not qualified buys nothing.
    """
    calls: dict[tuple[str, str], set[str]] = {}
    defines: dict[str, set[tuple[str, str]]] = {}

    def walk(node: ast.AST, qual: str, rel: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                sub = f"{qual}.{child.name}" if qual else child.name
                if not isinstance(child, ast.ClassDef):
                    defines.setdefault(child.name, set()).add((rel, sub))
                walk(child, sub, rel)
                continue
            if isinstance(child, ast.Call):
                name = _callee_name(child.func)
                if name:
                    calls.setdefault((rel, qual), set()).add(name)
            walk(child, qual, rel)

    for path in _SRC.rglob("*.py"):
        rel = str(path.relative_to(_REPO))
        walk(ast.parse(path.read_text()), "", rel)
    return calls, defines


def _reaches_sink(node: tuple[str, str], calls, defines, seen: set | None = None) -> bool:
    if seen is None:
        seen = set()
    if node in seen:
        return False
    seen.add(node)
    for name in calls.get(node, ()):
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
    for node, callees in sorted(calls.items()):
        if "acquire_commit_mutex_live" not in callees:
            continue
        if node in UNCONVERTED:
            continue
        if _reaches_sink(node, calls, defines):
            rel, qual = node
            problems.append(
                f"{rel}::{qual} :: calls acquire_commit_mutex_live and reaches a git sink"
            )
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
    # `assert all(...)` ALONE IS VACUOUS — True for an empty list. Assert the exact set.
    #
    # BOTH are reported, for DIFFERENT reasons, and that is the point: `outer` takes
    # `_live` (so it is in scope and wrong), while `inner` holds the sink (so it is in
    # scope and unfenced). An earlier revision asserted every problem named
    # `outer.inner`, which the derived-scope rule makes false — MEASURED.
    assert problems, "the gate reported nothing at all"
    by_qual = {p.split("::", 1)[1].split(" ", 1)[0]: p for p in problems}
    assert set(by_qual) == {"outer", "outer.inner"}, problems
    assert "still calls acquire_commit_mutex_live" in by_qual["outer"]
    assert "never calls acquire_commit_mutex_at_generation" in by_qual["outer.inner"]


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
**8** at `8f7b1ef7` — exactly F3e1b-1's eight. Scope is 21 functions and
`NOT_YET_CONVERTED` holds 13, so 21 − 13 = 8. An earlier revision said "the other
nine", which was the pin count before the four no-sink `_live` callers entered scope.

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

_LIVE = "acquire_commit_mutex_live"
_AT_GENERATION = "acquire_commit_mutex_at_generation"

#: Scope derives from the AST ALONE — no list any slice edits.
#:
#: Two earlier revisions keyed scope on a record (`ENCLOSING_ACQUISITIONS`, then a
#: union including it). Both could be SILENTLY NARROWED by the very conversion they
#: were meant to check: Task 4 empties that record, and MEASURED, the
#: delegating-precedence check then returned nothing while its own test still
#: passed. F5 would do the same to `Phase0Worker._m1`.
#:
#: A function is in scope if it holds an effect OR takes either mutex primitive.
#: That is §5.1's "every git_commit site AND every enclosing acquisition", derived
#: rather than listed. MEASURED at `8f7b1ef7`: 21 functions, covering all 8 of
#: F3e1b-1's sites. `Phase0Worker._m1` stays in `check_source`'s SCOPE no matter what
#: F5 edits — but it is pinned in `NOT_YET_CONVERTED`, so `check_tree` still filters
#: it. The same is true of `Phase0Worker._init_repo`, which holds F5's sink. F5 must
#: REMOVE both pins as it converts, and `test_every_converted_gate_pin_is_load_bearing`
#: is what forces that: a pin whose removal changes nothing fails. Scope surviving is
#: necessary, not sufficient — the load-bearing test is the sufficient half.
_MUTEX_PRIMITIVES: frozenset[str] = frozenset({_LIVE, _AT_GENERATION})

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
#: PATH-QUALIFIED. A bare-name exemption is inherited by any same-named function
#: anywhere in the tree — a new `save_section` in an unrelated module would be exempt
#: without anyone deciding that. Same defect a bare-name holder set has.
NOT_YET_CONVERTED: frozenset[tuple[str, str]] = frozenset(
    {
        ("src/call_manager/proposal_assist/editing/save.py", "save_section"),  # F3e1b-2
        ("src/call_manager/proposal_assist/citations/human_cite.py", "insert_citation_for_user"),
        ("src/call_manager/proposal_assist/citations/core.py", "_insert_citation_locked"),
        ("src/call_manager/proposal_assist/analysis/insert.py", "insert_from_annotation"),  # F3e2
        ("src/call_manager/proposal_assist/citations/bibliography.py", "build_bibliography"),
        ("src/call_manager/web/api/workspaces.py", "post_init_spec._git"),  # F3e2
        ("src/call_manager/proposal_assist/review/pass_runner.py", "run_review_pass"),  # F4
        ("src/call_manager/proposal_assist/phases/setup.py", "Phase0Worker._init_repo"),  # F5
        ("src/call_manager/proposal_assist/phases/setup.py", "Phase0Worker._m1"),  # F5
        # --- take `_live` but reach NO sink, so they are outside §5.1 by definition.
        # They are pinned only because the scope rule above is deliberately MECHANICAL:
        # deriving scope from the AST is what stops a slice narrowing it, and the price
        # is four pins a human reads once. Cheaper than a gate that can go quiet.
        ("src/call_manager/proposal_assist/collab/flip.py", "maybe_auto_enable"),
        ("src/call_manager/proposal_assist/collab/hydrate.py", "load_or_hydrate"),
        ("src/call_manager/proposal_assist/web/_writes.py", "workspace_write"),
        ("src/call_manager/workspaces.py", "soft_delete_workspace_db"),
    }
)


def _own_calls(func: ast.AST) -> list[tuple[int, int, str]]:
    """`(lineno, col_offset, callee-name)` for calls in THIS function's OWN scope.

    A nested `def`/`lambda`/`class` body belongs to the nested scope. `ast.walk`
    descends into them, so judging `outer` on `inner`'s effects is exactly F3e1a2's
    `_iter_own_nodes` trap — MEASURED: a fixture where `outer` takes `_live` and a
    nested `inner` holds the sink makes `ast.walk(outer)` report the sink, and the
    gate then blames the wrong function.

    **BODY ONLY.** `ast.iter_child_nodes(func)` also yields `decorator_list` and the
    default expressions in `args` — which run at DEFINITION time, in the ENCLOSING
    scope, not on invocation. MEASURED: with them included,
    `def f(s, w, stamp, _x=acquire_commit_mutex_at_generation(...)): write_and_commit_files(s)`
    sorts the mutex above the sink and PASSES the gate unfenced.

    **`col_offset`, not `lineno` alone.** Two calls on one line compared equal, so
    MEASURED: `write_and_commit_files(s); acquire_commit_mutex_at_generation(...)` —
    effect first, on one line — PASSED. A position is (line, column).
    """
    out: list[tuple[int, int, str]] = []

    def walk(node: ast.AST) -> None:
        if isinstance(node, ast.Call):
            name = _callee_name(node.func)
            if name:
                out.append((node.lineno, node.col_offset, name))
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef
            ):
                continue
            walk(child)

    body = func.body if hasattr(func, "body") else []
    for stmt in body if isinstance(body, list) else [body]:
        walk(stmt)
    return sorted(out)


def _judge(
    qual: str, path: str, ordered: list[tuple[int, int, str]], enclosing: bool
) -> list[str]:
    """One function's verdict. Returns a list so callers can `extend`, never `+=`
    inside a closure — that rebinds the name and raises UnboundLocalError.

    Positions are `(lineno, col_offset)` pairs, compared as tuples. Comparing bare
    line numbers made two calls on ONE line equal, so an effect-first same-line
    site passed — MEASURED.
    """
    names = {n for *_pos, n in ordered}
    if _LIVE in names:
        return [f"{path}::{qual} still calls {_LIVE}"]
    acquisitions = [(ln, col) for ln, col, n in ordered if n == _AT_GENERATION]
    if not acquisitions:
        return [f"{path}::{qual} is in scope but never calls {_AT_GENERATION}"]
    effects_here = _EFFECTS | (_DELEGATED_EFFECTS if enclosing else frozenset())
    effects = [(ln, col) for ln, col, n in ordered if n in effects_here]
    if effects and min(acquisitions) > min(effects):
        return [
            f"{path}::{qual} — {_AT_GENERATION} must precede the first effect "
            f"(mutex at line {min(acquisitions)[0]}, effect at line {min(effects)[0]})"
        ]
    return []


def check_source(source: str, path: str) -> list[str]:
    """§5.1's scope, verbatim: "every `git_commit` site AND every enclosing acquisition".

    Both halves are required. MEASURED: effect-holders alone cover only 5 of F3e1b-1's
    8 sites — `kernel_accept_citation`, `post_lock_spec` and `post_unlock_spec`
    DELEGATE rather than hold an effect, so a direct-effect rule cannot see them.

    **Lambdas are judged too.** `_own_calls` correctly refuses to attribute a lambda's
    calls to its enclosing function — but for four revisions nothing then judged the
    lambda itself, so its calls were attributed to NOBODY. MEASURED:
    `go = lambda: write_and_commit_files(s)` with no mutex anywhere PASSED. Skipping a
    scope when ATTRIBUTING is right; skipping it when JUDGING is a hole.
    """
    problems: list[str] = []

    def _consider(node: ast.AST, sub: str) -> None:
        ordered = _own_calls(node)
        names = {n for *_pos, n in ordered}
        holds_effect = bool(names & _EFFECTS)
        takes_mutex = bool(names & _MUTEX_PRIMITIVES)
        # "Enclosing" is DERIVED too: takes a mutex, holds no effect of its own,
        # therefore its delegated call is its first effect.
        if holds_effect or takes_mutex:
            problems.extend(
                _judge(sub, path, ordered, enclosing=takes_mutex and not holds_effect)
            )

    def visit(node: ast.AST, qual: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                sub = f"{qual}.{child.name}" if qual else child.name
                if not isinstance(child, ast.ClassDef):
                    _consider(child, sub)
                visit(child, sub)
            elif isinstance(child, ast.Lambda):
                # Line-qualified: a function may hold several, and "<lambda>" alone
                # would collapse them into one indistinguishable name in the report.
                sub = f"{qual}.<lambda:{child.lineno}>" if qual else f"<lambda:{child.lineno}>"
                _consider(child, sub)
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
            if (rel, p.split("::", 1)[1].split(" ", 1)[0]) not in NOT_YET_CONVERTED
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


def test_only_the_two_sanctioned_constructors_build_a_FenceStamp():
    """#7. §7.0.2 says there are "exactly two ways to build one, and each REFUSES rather
    than defaults" — but that was CONVENTION, enforced by nothing. A direct
    `FenceStamp(...)` anywhere in `src/` bypasses both refusals, and the whole carrier
    argument rests on a stamp never existing without a claimed generation.

    MEASURED at `8f7b1ef7`: the only construction in `src/` is `Invocation.stamp()` at
    `ports/invocation.py:75`. This pins that, so the second constructor
    (`stamp_for_workspace`) and any third are a reviewed diff.
    """
    import ast
    import pathlib

    # `_callee_name` is imported HERE, at the top, and used in both blocks below. An
    # earlier revision used it in the second block without importing it at all: a
    # NameError at collection time, in a plan whose Constraint 12 exists because of
    # exactly that mistake.
    from tests.fencing.git_fence import _callee_name

    root = pathlib.Path(__file__).resolve().parents[2]
    allowed = {
        "src/call_manager/proposal_assist/ports/invocation.py",
        "src/call_manager/proposal_assist/ports/stamp.py",
    }
    def _stamp_aliases(source: str) -> set[str]:
        """`from ...stamp import FenceStamp as FS` then `FS(...)` builds one too. A check
        keyed on the literal name "FenceStamp" is bypassed by one import line."""
        names = {"FenceStamp"}
        tree = ast.parse(source)
        # THREE rebinding forms, not two. `from ... import FenceStamp as FS`; the plain
        # `FS = FenceStamp`; and the QUALIFIED `FS = stamp.FenceStamp`, which binds the
        # class without ever writing a bare name. `_callee_name` — NOT
        # `isinstance(node.value, ast.Name)` — is what resolves the third: it returns the
        # final name of an Attribute too. `test_f3e1a2_slice.py:41` already made exactly
        # this correction for Primitive B, with a comment saying so; an earlier revision
        # of THIS gate regressed to the bare-Name check that file had already retired.
        # MEASURED: bare-Name misses `FS = stamp.FenceStamp; FS(...)` and
        # `import x.stamp as m; FS = m.FenceStamp; FS(...)`. PROVEN BY MUTATION on the
        # real tree: planting `FS = stamp.FenceStamp` + `FS(...)` in a new `ports/` file
        # fails this test with "FenceStamp built outside its two constructors:
        # ['...:7']"; with the bare-Name check it passed. Six alias shapes exercised.
        for _ in range(3):  # transitive: FS = stamp.FenceStamp; G = FS
            before = len(names)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for a in node.names:
                        if a.name == "FenceStamp" and a.asname:
                            names.add(a.asname)
                elif isinstance(node, ast.Assign | ast.AnnAssign):
                    if node.value is None or _callee_name(node.value) not in names:
                        continue
                    targets = (
                        node.targets if isinstance(node, ast.Assign) else [node.target]
                    )
                    names.update(t.id for t in targets if isinstance(t, ast.Name))
                elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                    # A DEFAULT ARGUMENT binds the class to a parameter name:
                    # `def rogue(ws, factory=FenceStamp): return factory(...)`.
                    # MEASURED: without this the rogue built a third FenceStamp in an
                    # allowlisted file and the gate PASSED. The binding is per-function
                    # but this alias set is per-file, so an unrelated `factory` parameter
                    # elsewhere in the file also becomes suspect — an over-approximation,
                    # which for a gate means a spurious failure a human reads, never a
                    # silent escape.
                    a = node.args
                    for param, default in zip(
                        (a.posonlyargs + a.args)[-len(a.defaults) :] if a.defaults else [],
                        a.defaults,
                        strict=True,
                    ):
                        if _callee_name(default) in names:
                            names.add(param.arg)
                    for param, default in zip(a.kwonlyargs, a.kw_defaults, strict=True):
                        if default is not None and _callee_name(default) in names:
                            names.add(param.arg)
            if len(names) == before:
                break
        return names

    built = []
    for path in (root / "src").rglob("*.py"):
        rel = str(path.relative_to(root))
        if rel in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if name in _stamp_aliases(path.read_text()):
                    built.append(f"{rel}:{node.lineno}")
    assert built == [], f"FenceStamp built outside its two constructors: {built}"

    # Two-sided. `built == []` is also satisfied if BOTH constructors were deleted, or
    # if a third were added inside an allowlisted file. Assert the population, not just
    # its complement.
    from tests.fencing.converted_gate import _own_calls

    # COUNT with a whole-tree walk, ATTRIBUTE with a scoped one. Do not merge these.
    #
    # Every scoped walk has a blind spot, because scoping is what it is FOR. `_own_calls`
    # answers "what does THIS function call" and so skips nested defs, lambdas and class
    # bodies; an earlier `_outside_functions` covered module and class scope but skipped
    # every FunctionDef. A `(lambda: FenceStamp(...))()` inside a function fell between
    # the two and MEASURED as PASSING — a third constructor in the file the allowlist
    # trusts most. `ast.walk` has no scope and therefore no blind spot, so it does the
    # counting; the scoped walk only says which function owns the one it finds.
    inside = set()
    per_file_total: dict[str, int] = {}
    for rel in sorted(allowed):
        source = (root / rel).read_text()
        tree = ast.parse(source)
        aliases = _stamp_aliases(source)

        per_file_total[rel] = sum(
            1
            for c in ast.walk(tree)
            if isinstance(c, ast.Call) and _callee_name(c.func) in aliases
        )

        def visit(node, prefix=""):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                    sub = f"{prefix}.{child.name}" if prefix else child.name
                    if not isinstance(child, ast.ClassDef):
                        if any(n in aliases for *_pos, n in _own_calls(child)):
                            inside.add((rel, sub))
                    visit(child, sub)
                else:
                    visit(child, prefix)

        visit(tree)

    # EXACTLY ONE construction per allowlisted file, counted where nothing can hide.
    # This subsumes the old module-scope and per-function-count checks: a second build
    # anywhere — module body, class body, lambda, nested def — moves this number.
    assert per_file_total == {rel: 1 for rel in allowed}, f"extra builds: {per_file_total}"

    # And by CONSTRUCTOR IDENTITY, not by file. The count above is satisfied by one
    # construction in the right FILE but the wrong FUNCTION — a module-level
    # `FenceStamp(...)` in stamp.py would pass it while `stamp_for_workspace` was gutted.
    # A build that lives ONLY in a lambda attributes to no owner and fails here.
    assert inside == {
        ("src/call_manager/proposal_assist/ports/invocation.py", "Invocation.stamp"),
        ("src/call_manager/proposal_assist/ports/stamp.py", "stamp_for_workspace"),
    }, f"the two sanctioned constructors are not the two that build one: {sorted(inside)}"


def test_a_stamp_for_a_DIFFERENT_workspace_raises(db_session, seeded_workspace):
    """#10. The workspace-id half of the mismatch check, which was specified and never
    asserted. A stamp minted for workspace A must not fence an effect on workspace B."""
    from call_manager.proposal_assist.fencing import acquire_commit_mutex_at_generation
    from call_manager.proposal_assist.ports.errors import FenceStampMismatch
    from call_manager.proposal_assist.ports.stamp import FenceStamp

    other = FenceStamp(
        workspace_id=uuid.uuid4(), generation=0, active_turn_job_id=None, origin_job_id=None
    )
    with pytest.raises(FenceStampMismatch):
        acquire_commit_mutex_at_generation(db_session, seeded_workspace, generation=0, stamp=other)


def test_a_new_transaction_may_bind_a_FRESH_stamp(db_session, seeded_workspace):
    """#8. Step 3's cleanup clause, asserted rather than assumed. Without it the binding
    outlives its transaction and the SECOND request on a pooled Session raises
    FenceStampMismatch against a stamp from the first — a self-inflicted outage that no
    other test in this plan would catch."""
    from call_manager.proposal_assist.fencing import acquire_commit_mutex_at_generation
    from call_manager.proposal_assist.ports.stamp import FenceStamp

    wid = seeded_workspace
    fields = dict(workspace_id=wid, generation=0, active_turn_job_id=None, origin_job_id=None)
    acquire_commit_mutex_at_generation(db_session, wid, generation=0, stamp=FenceStamp(**fields))
    db_session.rollback()
    # A DISTINCT object, after the transaction ended: must be accepted, not rejected.
    acquire_commit_mutex_at_generation(db_session, wid, generation=0, stamp=FenceStamp(**fields))


@pytest.mark.parametrize("end", ["commit", "rollback"])
def test_the_stamp_binding_clears_on_EITHER_transaction_ending(db_session, seeded_workspace, end):
    """Both endings, not just rollback. An implementation hooked only to rollback leaves
    the binding alive after a COMMIT — and the request path commits. The second request
    on a pooled Session would then raise FenceStampMismatch against the first request's
    stamp. That is the shape that takes a service down, and testing only rollback misses
    it entirely."""
    from call_manager.proposal_assist.fencing import acquire_commit_mutex_at_generation
    from call_manager.proposal_assist.ports.stamp import FenceStamp

    wid = seeded_workspace
    fields = dict(workspace_id=wid, generation=0, active_turn_job_id=None, origin_job_id=None)
    acquire_commit_mutex_at_generation(db_session, wid, generation=0, stamp=FenceStamp(**fields))
    getattr(db_session, end)()
    acquire_commit_mutex_at_generation(db_session, wid, generation=0, stamp=FenceStamp(**fields))


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
| `workspaces.py:665` (`post_lock_spec`) | `stamp = stamp_for_workspace(ws)` as a LOCAL, then `stamp=stamp` — `ws` from `require_owner` at `:645` |
| `workspaces.py:746` (`post_unlock_spec`) | `stamp = stamp_for_workspace(ws)` as a LOCAL, then `stamp=stamp` — `ws` from `require_owner` at `:734` |
| `collab_checkpoint.py:52` (`save_version`) | **`resolve_access` at `:38` returns `tuple[bool, bool]` — there is NO Workspace here.** Inside `_do()`'s session, after the `is_member` check, add `ws = s.get(Workspace, workspace_id)`; that load is the mint site, per §7.3's rule. Then `stamp = stamp_for_workspace(ws)`. |
| `handover.py:40` | `stamp=stamp_for_workspace(ws)` — pre-screen at `:36-38` |
| `permissions.py:58` | `stamp=stamp_for_workspace(ws)` — pre-screen at `:53-55` |
| `checkpoint.py:210` | `stamp=stamp_for_workspace(ws)` — pre-screen at `:205-207` |

**Bind `stamp` as a NAMED LOCAL in every one of these functions**, then pass
`stamp=stamp`. Passing `stamp=stamp_for_workspace(ws)` inline binds no name, and
Task 4 then writes `generation=stamp.generation, stamp=stamp` inside the same
function — a `NameError`. The local is also what makes the same-object rule
checkable: one mint per function, reused, never re-called.

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

**Pass the same object inward; never re-mint.** **No authorization call changes**
(Constraint 10) — every row below binds a value an existing call already returns, or adds
a plain `s.get`, and changes no check.

Earlier revisions said only "the four routes mint per §7.1's table" and named no site. The
four routes are these, and three of them have no `ws` in scope today:

| Route (mints) | Calls | Access check | What to add |
|---|---|---|---|
| `web/api/workspaces.py` `post_citation` | `direct_cite_via_kernel` (`:1052`) | `resolve_access` at `:1039` in `_gate()` | **No Workspace** — booleans only. After the `is_member` check add `ws = s.get(Workspace, workspace_id)`; mint from it. |
| `web/api/workspaces.py` (spec init) | `init_spec_via_kernel` (`:494`) | `require_owner` at `:486` in `_gate()`, **return discarded** | Bind it: `ws = require_owner(...)`. |
| `web/api/annotations.py` | `insert_annotation_via_kernel` (`:261`) | `ws = require_owner(...)` at `:257` in `_gate()` | **Already bound.** Mint from `ws`. |
| `proposal_assist/web/proposals.py` `accept` | `accept_via_kernel` (`:177`) | `require_member` at `:164` in `_route()`, **return discarded** | Bind it: `ws = require_member(...)`. |

**All four mint INSIDE a `run_in_threadpool` closure whose session then closes**, and the
orchestrator is called after it. That is safe and it is the only correct place: a
`FenceStamp` is a frozen carrier of plain values (`workspace_id`, `generation`, two
optional ids), not an ORM object, so it survives its session. But each closure must now
**return the stamp** — `_gate()` currently returns `None` and `_route()` returns
`(enabled, artifact)`. Widen the return; do not re-open a session outside the gate to
mint, which would read a generation no access check covered and defeat the carrier.

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
    exactly F3e1b-1's eight: scope is 21 and `NOT_YET_CONVERTED` holds 13."""
    from tests.fencing.converted_gate import check_tree

    assert check_tree() == []


def test_every_converted_gate_pin_is_load_bearing():
    """#3's real fix. `Phase0Worker._m1` is in scope but PINNED, so `check_tree` filters
    it and the gate enforces nothing on it today — "scope survives F5's edits" is
    necessary, not sufficient.

    What makes F5 actually remove the pin is this: a pin whose removal changes nothing
    is DEAD, and this test fails on it. The moment F5 converts `_m1`, its pin stops being
    load-bearing and F5 must delete it or go red. Same mechanism `live_reach_gate` uses.
    """
    import tests.fencing.converted_gate as gate

    original = gate.NOT_YET_CONVERTED
    dead = []
    try:
        for pin in sorted(original):
            gate.NOT_YET_CONVERTED = frozenset(original - {pin})
            _path, qual = pin
            if not any(qual in p for p in gate.check_tree()):
                dead.append(pin)
    finally:
        gate.NOT_YET_CONVERTED = original
    assert dead == [], f"pins that protect nothing: {dead}"


def test_no_declared_site_is_pinned():
    """The escape hatch that would make every other gate here optional.

    `check_tree` filters `NOT_YET_CONVERTED`, so adding one of this slice's own eight
    sites to that list silences the precedence gate for it — and every remaining
    assertion would stay green. The two lists must be disjoint, permanently.
    """
    from tests.fencing.converted_gate import NOT_YET_CONVERTED
    from tests.fencing.f3e1b1_sites import DECLARED_SITES

    overlap = DECLARED_SITES & NOT_YET_CONVERTED
    assert overlap == frozenset(), f"a site this slice owns was pinned instead of fixed: {overlap}"


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
                        names = {n for *_pos, n in _own_calls(child)}
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
    # SCAN: fragment
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

**For `post_lock_spec` / `post_unlock_spec`, `checkpoint_artifact` is NOT the first
effect.** §4.2's table names the **spec-lock write** / **spec-unlock write**.
MEASURED at `8f7b1ef7`: the mutex at `:651` already precedes both, so this is a
ratchet against future movement rather than a current failure — but the gate as
written would ACCEPT a mutex moved to just above `checkpoint_artifact` at `:665`
and below the lock write.

**MEASURED: there is no lock-write callee, so nothing goes into `_EFFECTS`.** Two
revisions instructed "identify the lock/unlock write's callee and add it to `_EFFECTS`"
and left `<lock-write callee>` as a placeholder in both the fixture and its oracle — so
whatever an implementer picked would satisfy both sides, and a no-op name placed after
the real write would have certified itself. Read the routes: in `post_lock_spec`
(`web/api/workspaces.py`) the spec-lock write is

```python
# SCAN: fragment
ws.spec_locked_at = datetime.now(UTC)     # ORM attribute assignment - NOT a call
ws.spec_locked_by = user.id
session.add(
    SpecLockEvent(
        workspace_id=ws.id,
        action="lock",
        actor_id=user.id,
        artifact_commit_sha=sha,
    )
)
```

and `post_unlock_spec` is the same shape with `None`. `_EFFECTS` is keyed on CALLEE
NAMES. An attribute assignment has no callee, and `session.add` is a generic ORM call
that appears all over the tree — adding it would flag hundreds of unrelated functions.
**So this gate structurally cannot see the spec-lock write, and no name makes it able
to.** Do not invent one.

What the gate DOES cover for these two routes is `checkpoint_artifact`, via
`_DELEGATED_EFFECTS` — already present, already checked for precedence. And the real
ordering is not at risk: MEASURED in both routes, `acquire_commit_mutex_live` is the
FIRST statement in the `try:`, the checkpoint follows it, and the ORM write follows
that. There is no gap for a mutex to move into.

Record the residual in "What this slice does NOT prove" rather than faking a gate for
it, and delete Step 3b — its test asserted a placeholder against itself.

Rewrite `checkpoint.py`'s `_live:` comment block at `:87-90` — it explains a refusal on
deletion only, and the refusal is now also on generation.

**Do not re-mint inside any of these.** They receive their stamp from Task 2.

- [ ] **Step 4: Update the records and pins in the same working tree**

- `live_mutex_gate.ENCLOSING_ACQUISITIONS` — remove the seven. All three records are
  keyed on **`(path, qualname)`**, so it becomes
  `{("src/call_manager/proposal_assist/phases/setup.py", "Phase0Worker._m1"): "F5"}`.
- `live_mutex_gate.LIVE_ACQUIRING_SINK_HOLDERS` — remove `checkpoint_artifact` **only**.
  MEASURED: it is the sole in-scope member; the other five are F3e1b-2's and F3e2's.
- `live_mutex_gate.LIVE_ACQUIRERS_WITHOUT_A_SINK` — **unchanged**. MEASURED: none of the
  eight is a member.
- `live_reach_gate.UNCONVERTED` — remove the eight F3e1b-1 entries. Six remain.
- `converted_gate.NOT_YET_CONVERTED` — **unchanged**. It never held the eight.

`test_the_live_acquirers_partition_exactly` must stay green throughout: the eight leave
the AST-discovered `_live` set and the literal records in the same commit.

- [ ] **Step 4b: Migrate the two existing tests this task's record edits break**

`tests/fencing/test_live_mutex_gate.py` already asserts things about
`ENCLOSING_ACQUISITIONS`, and Step 4 empties its F3e1b entries. MEASURED: the F3e1b
count goes 7 → 0, so `test_f3e1b_owns_exactly_the_seven_enclosing_acquisitions_of_spec_4_2`
fails. Migrate it **in this commit** — Constraint 2 forbids leaving it red.

Re-point it at the immutable catalog, which is what it was always trying to pin:

```python
def test_f3e1b_owns_exactly_the_seven_enclosing_acquisitions_of_spec_4_2():
    """Spec §4.2's table, pinned to a record no conversion edits.

    It previously read the seven out of `ENCLOSING_ACQUISITIONS`, which F3e1b-1 empties —
    so the conversion broke the test that guarded it. `DECLARED_SITES` is fixed, so this
    keeps asserting §4.2 after the sites convert.
    """
    from tests.fencing.f3e1b1_sites import DECLARED_SITES

    assert {q for _p, q in DECLARED_SITES} == {
        "checkpoint_artifact",  # the choke point, not a §4.2 row
        "apply_citation_to_room",
        "kernel_accept_prose",
        "kernel_init_spec",
        "kernel_accept_citation",
        "apply_annotation_insert_to_room",
        "post_lock_spec",
        "post_unlock_spec",
    }


def test_only_F5s_enclosing_acquisition_remains_unconverted():
    """The other half: after this slice, the live record holds exactly `_m1`."""
    from tests.fencing.live_mutex_gate import ENCLOSING_ACQUISITIONS

    assert dict(ENCLOSING_ACQUISITIONS) == {
        ("src/call_manager/proposal_assist/phases/setup.py", "Phase0Worker._m1"): "F5"
    }
```

`test_the_live_acquirers_partition_exactly` also reads those records, and Step 4 keeps it
green by editing the records in the same commit as the calls. **Run
`tests/fencing/test_live_mutex_gate.py` before moving on** — those two tests are the ones
this task's record edits reach.

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
operation proceeds and lands **nothing**.

**What the caller SEES depends on the caller, and Task 3 decided it** — do not assert
`pytest.raises` at a route. Task 3 maps `WorkspaceGenerationStale` to a
non-disclosing **404** at the HTTP boundary and to a **benign skip** in the three
maintenance callers.

**EIGHT HTTP entry points, not seven.** `save_version` (`collab_checkpoint.py:52`)
is a route — the user-triggered "Save a version" — and an earlier revision counted
it among the four "non-enclosing paths" without giving it an outward assertion at
all. It gets a 404 like the other seven. That leaves **three** maintenance callers
asserting a clean skip: the evict hook, the kill-switch handover, and the periodic
tail checkpoint. Reserve `pytest.raises` for direct helper-level calls. An earlier revision said "it must raise" for all of
them, contradicting the task that had just made it not raise.

Then assert the landing, every time:

```python
    # SCAN: fragment
    assert head_before == _git(repo, "rev-parse", "HEAD")          # HEAD unchanged
    assert _git(repo, "status", "--porcelain") == ""               # worktree clean
    assert path.read_bytes() == before_bytes                       # byte-identical
    assert yjs_update_count(session, wid) == n0                    # no yjs_updates row
    assert doc_state_before == _doc_state(room)                    # room Doc unmutated
```

§5.2 is explicit: a proof that inspects only rows passes while bytes are already on disk;
an unchanged HEAD is not enough, because a stale implementation can dirty the working
tree without committing; and cleanliness alone passes an implementation that wrote and
then reverted.

**For `post_lock_spec` and `post_unlock_spec`, add two more lines.** Those five assertions
cover git, Yjs and the room — and the effect §4.2 names for these two routes is none of
those. It is the DB lock write, which every assertion above ignores, so a stale
generation could set `spec_locked_at` and persist a `SpecLockEvent` while Case B reported
clean. This is the same blind spot as the static gate's (see "What this slice does NOT
prove"): having recorded that `_EFFECTS` cannot see an ORM attribute write, the runtime
race is the ONLY place left that can, and it was not looking either.

```python
    # SCAN: fragment
    session.refresh(ws, ["spec_locked_at", "spec_locked_by"])
    assert (ws.spec_locked_at, ws.spec_locked_by) == lock_state_before
    assert session.scalar(
        select(func.count()).select_from(SpecLockEvent).where(
            SpecLockEvent.workspace_id == wid)
    ) == lock_events_before
```

Capture `lock_state_before` and `lock_events_before` alongside `head_before`. **Case A
must assert the mirror image** — that the pair CHANGED and exactly one new
`SpecLockEvent` row exists. An unchanged-only assertion is satisfied by a route that
never writes at all, which is the failure mode Case A exists to exclude.

**The first three apply everywhere. The last two — no `yjs_updates` row, unmutated
room `Doc` — apply to the paths that touch a room**: the five kernel entry points and
the three maintenance callers, which checkpoint a live room. `post_lock_spec`,
`post_unlock_spec` and `save_version` reach the room through `checkpoint_artifact`
too, so assert all five there as well. An earlier revision said "all five, every
time" one sentence before restricting two of them to kernel paths; if a path
provably cannot touch a room, say which and why in the test, rather than dropping
the assertion silently.

**Disable the compensation in every one of these tests.** Cancel-on-delete is still in
the tree and masks staleness for statements touching the same row; a conversion test that
passes with the fence removed is measuring the compensation, not the fence. This is the
technique that exposed Slice C1's decorative tests. Follow the precedent in
`tests/proposal_assist/test_f3d2_conversions.py`.

**Also prove the threading, per edge:**

Add to `tests/proposal_assist/collab/test_collab_kernel_citation.py`, which already has
the setup helpers this needs (`_kernel_setup`, `_git_head`, `_get_ref`, `_mk_citation_pc`).

```python
# SCAN: names-from tests/proposal_assist/collab/test_collab_kernel_citation.py
def test_the_inner_acquisition_receives_the_SAME_stamp_object(
    pg_session_factory, tmp_path, monkeypatch
):
    """§7.2, on the `kernel_accept_citation` -> `apply_citation_to_room` edge (§4.2's
    seventh acquisition). Assert `received is supplied`, not `==`: a re-mint under the
    held mutex reads the same generation, so equality passes and the mis-threading is
    invisible.

    `kernel_accept_citation` RECEIVES its stamp (Step 5) and never mints one, so this
    test pre-mints, passes it in, and proves that same object reached the inner call.

    Revision 11 had this BACKWARDS: it called the kernel with no `stamp` at all and spied
    `kernel.stamp_for_workspace`, asserting exactly one mint. Against a compliant
    implementation that is a TypeError for a missing required argument, and the only way
    to make it green is to mint inside the kernel — which Step 5 forbids in those words
    ("pass the same object inward; never re-mint"). A test that fails against the correct
    implementation and passes against the forbidden one is worse than no test.
    Revisions 1-2 shipped this as a docstring with no body; revision 3 shipped
    `supplied = <mint the stamp...>`, an angle-bracket placeholder that does not parse.

    No separate "zero re-mints" assertion is needed: a re-mint is only observable if the
    new object is what gets passed on, and identity already catches that. A re-mint whose
    result is discarded changes no behaviour.
    """
    import call_manager.proposal_assist.collab.kernel as kernel
    from call_manager.db.models import User, Workspace
    from call_manager.proposal_assist.ports.stamp import stamp_for_workspace

    wid, oid, repo, room_doc, base_sha = _kernel_setup(
        pg_session_factory, tmp_path, ref_citekey="smith2021"
    )
    head_before = _git_head(repo)
    with pg_session_factory() as s:
        ref = _get_ref(s, wid)
        pc_id = _mk_citation_pc(
            s, wid, section_index=1, base_commit_sha=head_before,
            base_section_sha=base_sha, reference_id=ref.id,
        )

    received: list = []
    real_inner = kernel.apply_citation_to_room

    def _inner_spy(*a, **kw):
        received.append(kw.get("stamp"))
        return real_inner(*a, **kw)

    monkeypatch.setattr(kernel, "apply_citation_to_room", _inner_spy)

    with pg_session_factory() as s:
        supplied = stamp_for_workspace(s.get(Workspace, wid))
        kernel_accept_citation(
            s,
            room_doc=room_doc,
            proposed_change_id=pc_id,
            workspace_id=wid,
            approver=s.get(User, oid),
            workspaces_dir=str(tmp_path),
            stamp=supplied,
        )
        s.commit()

    assert received, "apply_citation_to_room never ran — the spy proved nothing"
    assert all(got is supplied for got in received), [id(x) for x in received]
```

- [ ] **Step 6: Collaborator authorization parity (spec crit-13)**

**Write NO new test here.** Constraint 10 says no authorization call changes, and that
requirement is *already* pinned by five tests that exist and pass today. Run them after
Task 4's conversion and require green:

```bash
uv run --python 3.12 pytest -q \
  "tests/test_api_workspaces.py::test_post_citation_collab_any_writer_allowed" \
  "tests/test_api_workspaces.py::test_post_citation_flag_off_still_owner_only" \
  "tests/test_api_workspaces.py::test_post_citation_member_is_403" \
  "tests/proposal_assist/test_web_proposed_changes.py::test_accept_endpoint_allows_viewer" \
  "tests/proposal_assist/test_web_proposed_changes.py::test_accept_endpoint_rejects_non_member"
```

MEASURED at rev 11: `5 passed in 5.98s`. What they pin, in order — a NON-OWNER member
citing with collab ON succeeds (`resolve_access`, any writer); the same member with
collab OFF is 403 (`require_owner`, unchanged git path); a member is 403; a viewer may
accept; a non-member may not. Together that is crit-13, both sides, on the real routes.

**Why this step used to be worse than nothing.** Revisions 1–2 shipped it as a docstring
with no body. Revision 3 gave it a body — invented from memory, and wrong in every
particular: fixtures (`client`, `collaborator`, `ws_id`) that do not exist; routes
`/api/workspaces/{ws_id}/cite` and `/api/workspaces/{ws_id}/accept` that do not exist
(the real ones are `POST /api/workspaces/{workspace_id}/citations` with an
`AddCitationIn` body, and `POST /api/workspaces/{workspace_id}/proposed-changes/{pc_id}/accept`
with **no body at all**); `headers=collaborator` where the file's own idiom is
`_client(app, user_id)`; and no `_enable_collab(...)` call, without which the
already-passing `test_post_citation_member_is_403` proves the OPPOSITE — collab is a DB
row (`enabled_at is not None`), not an env flag. An implementer would have got a 404,
"fixed" it toward the member-403 test, and concluded collaborators cannot cite — which is
exactly the wrong conclusion spec revision 2 reached and this step exists to prevent.
Grep for the coverage before writing the test.

- [ ] **Step 7: Prove the gates bite — §5.3's mutations**

**Do NOT commit before mutating** — Constraint 2 says this task is one commit, and an
earlier revision instructed a commit here and another at Step 8, contradicting it.
Copy every file you are about to mutate (`cp <f> /tmp/<f>.bak`), mutate, record the
failure, restore from the copy. **Never `git checkout <path>`** — the work is
uncommitted at this point and that destroys it.

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
    # KEY IS `F3e1b1`, NOT `F3e1b-1`. MEASURED: every existing key is unhyphenated —
    # F3a, F3d1, F3d2, F3e1a, F3e1a2 — and a sub-slice appends a character rather than
    # a separator. `F3e1b-1` would be the first hyphenated key in the collection.
    """F3e1b-1 owns no NEW inventory row, so a row-iterating test never reads its
    status. F3e1a2 shipped exactly this hole: deleting its status key left the whole
    fencing suite green."""
    from tests.fencing.inventory import SLICE_STATUS

    assert SLICE_STATUS["F3e1b1"] == "complete"


def test_f3e1b_still_owns_exactly_four_pending_git_rows():
    """IDENTITY, not membership. Revision 1 asserted
    `{r["slice"] for r in rows if r["slice"] == "F3e1b"} == {"F3e1b"}`, which is True
    for one row and for four — it proved "at least one exists". MEASURED: there are
    four, and they are F3e1b-2's to convert."""
    from tests.fencing.inventory import SLICE_STATUS, load_inventory

    rows = [r for r in load_inventory() if r["kind"] == "git_commit"]
    assert len(rows) == 9
    # (path, symbol), not bare symbol — two modules can define the same name, and a
    # bare-symbol set cannot tell a MOVED row from an unchanged one.
    assert {(r["path"], r["symbol"]) for r in rows if r["slice"] == "F3e1b"} == {
        ("src/call_manager/proposal_assist/citations/core.py", "_insert_citation_locked"),
        ("src/call_manager/proposal_assist/citations/human_cite.py", "insert_citation_for_user"),
        ("src/call_manager/proposal_assist/collab/checkpoint.py", "checkpoint_artifact"),
        ("src/call_manager/proposal_assist/editing/save.py", "save_section"),
    }
    assert SLICE_STATUS["F3e1b"] == "pending"
    assert all(r["fenced"] is False for r in rows)


def test_the_reach_gate_pin_list_is_exactly_the_six_that_remain():
    """Identity, not count: naming them is what stops a swap."""
    from tests.fencing.live_reach_gate import UNCONVERTED

    assert UNCONVERTED == frozenset(
        {
            ("src/call_manager/proposal_assist/editing/save.py", "save_section"),
            (
                "src/call_manager/proposal_assist/citations/human_cite.py",
                "insert_citation_for_user",
            ),
            ("src/call_manager/proposal_assist/citations/core.py", "_insert_citation_locked"),
            ("src/call_manager/proposal_assist/analysis/insert.py", "insert_from_annotation"),
            ("src/call_manager/web/api/workspaces.py", "post_init_spec._git"),
            ("src/call_manager/proposal_assist/phases/setup.py", "Phase0Worker._m1"),
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

    # The baselines, READ OFF `8f7b1ef7` AND WRITTEN DOWN HERE — not read at Task 5 time.
    #
    # Two earlier revisions got this wrong in two different ways. The first referenced
    # these names without defining them anywhere, so the test could not run. The second
    # "fixed" that with `{...}` placeholders and an instruction to fill them from the tree
    # in Task 5 Step 1 — but Task 5 runs AFTER Tasks 1-4, so any exemption those tasks
    # changed would already be present and would be pasted in AS the baseline. Both the
    # subset and the equality assertion below would then pass against a moved target.
    # `{...}` is also a legal set-of-Ellipsis, so it PARSED and the block scan called it
    # clean. A baseline that is captured after the change it is meant to detect is not a
    # baseline. These are the literal values; `git diff 8f7b1ef7 HEAD --
    # tests/fencing/inventory.py` is empty, so they are also HEAD's.
    _BASELINE_TOOLS = {"audit.py", "citations.py", "history.py", "proposal.py", "references.py"}
    _CORE = "src/call_manager/proposal_assist/citations/core.py"
    _REFS = "src/call_manager/proposal_assist/tools/references.py"
    _BASELINE_NON_SEMANTIC = {
        ("tool_commit", _CORE, "_write_citekey_cache"),
        ("tool_commit", _REFS, "fetch_reference_impl"),
        ("tool_commit", _REFS, "fetch_reference_impl._refund_fetch_slot"),
    }
    _BASELINE_RETAINED = {
        ("src/call_manager/proposal_assist/drafting/factory.py", "_build_adapter.tools_factory"),
    }

    # SUBSET, not equality. Pins only shrink, and this plan says so — but an earlier
    # revision then asserted set EQUALITY, which forbids the shrink it just permitted.
    # `<=` blocks an ADDITION (what this slice must not do) while allowing a later
    # slice's legitimate cleanup. MEASURED at 8f7b1ef7: 5, 3 and 1 entries.
    assert set(TOOLS_NOT_YET_CONVERTED) <= _BASELINE_TOOLS
    assert set(NON_SEMANTIC_PINS) <= _BASELINE_NON_SEMANTIC
    assert set(RETAINED_PINS) <= _BASELINE_RETAINED
    # And this slice must not shrink them either — it touches no tool module and no
    # exemption. Equality is correct here because the claim is "unchanged BY THIS SLICE",
    # and it must cover ALL THREE: an earlier revision asserted equality for one and left
    # the other two free to shrink while the docstring claimed all three were unchanged.
    assert set(TOOLS_NOT_YET_CONVERTED) == _BASELINE_TOOLS
    assert set(NON_SEMANTIC_PINS) == _BASELINE_NON_SEMANTIC
    assert set(RETAINED_PINS) == _BASELINE_RETAINED
```

- [ ] **Step 2: Run to verify they fail**

Expected: FAIL — `KeyError: 'F3e1b1'`. (Not `'F3e1b-1'` — the key was renamed in revision 8 to match every other unhyphenated key.)

- [ ] **Step 3: Add the status and regenerate the inventory**

Add `"F3e1b1": "complete",` to `SLICE_STATUS`. **Do not hand-edit
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
- **The reach gate is NOT purely conservative.** Its call graph over-approximates on
  shared names (extra edges, extra pins — safe) but **UNDER-reports aliases**:
  `f = mod.checkpoint_artifact; f(...)` is an edge it does not follow, and a missed
  edge is a MISSED PIN, not an extra one. An earlier revision described this as safe
  over-approximation in one place while the gate's own docstring said the opposite.
  The compensating check is
  `test_every_declared_site_positively_takes_primitive_b`, which is two-sided.
- **The spec-lock write is invisible to the converted gate, and no name can fix that.**
  MEASURED in `post_lock_spec` / `post_unlock_spec`: the lock write is
  `ws.spec_locked_at = …` (an ORM attribute assignment, which has no callee) plus a
  generic `session.add(...)`. `_EFFECTS` is keyed on callee names, so it cannot see
  either. The gate covers these two routes only through `checkpoint_artifact` in
  `_DELEGATED_EFFECTS`. The ordering is nonetheless correct today —
  `acquire_commit_mutex_live` is the first statement in each route's `try:`, the
  checkpoint follows, the ORM write follows that — but that is a fact about the current
  source, not something this slice's gate would keep true.
- **`_sink_bindings` misses the qualified alias.** MEASURED against the shipped
  `tests/fencing/git_fence.py`: it resolves `from m import _run_git as wac` and
  `wac = _run_git`, but not `wac = m._run_git`. Every gate reading `sink_calls`
  inherits that blind spot, and `live_mutex_gate.py` claimed the direct approach
  "hides nothing" on the strength of it — corrected in this slice's commit to
  "hides strictly less". Fixing `_sink_bindings` itself is NOT in this slice: it
  would newly resolve names across the whole tree and must land with its own suite
  run. The constructor gate's `_stamp_aliases` does not share the gap; it uses
  `_callee_name`.
- **Five git SINKS remain unconverted** — `save_section`, `insert_citation_for_user`,
  `_insert_citation_locked` (F3e1b-2), `insert_from_annotation` and
  `post_init_spec._git` (F3e2) — plus `build_bibliography` (F3e2), `run_review_pass`
  (F4) and `Phase0Worker._init_repo` (F5), which hold sinks but take no `_live` and so
  appear in the converted gate's list, not the reach gate's.
- **Six reach-gate PINS remain**, which is a different set: the five above plus
  `Phase0Worker._m1`, which takes `_live` and holds no sink. Sinks and pins are not
  the same population and an earlier revision conflated them.
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
16. **The constructor gate regressed a fix the tree had already made.** `_stamp_aliases`
    used `isinstance(node.value, ast.Name)`, so `FS = stamp.FenceStamp; FS(...)` built a
    third `FenceStamp` past it. `tests/fencing/test_f3e1a2_slice.py:41` had made exactly
    this correction for Primitive B, with a comment saying a bare-Name check "saw only
    the second form". Now uses `_callee_name`; MEASURED against six alias shapes.
17. **Step 6 was invented from memory instead of grepped.** It posted to
    `/api/workspaces/{ws_id}/cite` and `/accept` — neither exists — with fabricated
    fixtures and `json=<...>` placeholders, to prove something
    `test_post_citation_collab_any_writer_allowed` ALREADY proves and passes. It now runs
    the five existing tests by name. Grep for the coverage before writing the test.
18. **Constraint 12's scan silently skipped blocks that would not parse**, so the two
    `<...>` placeholders survived nine review rounds while the scan reported clean. A
    gate that skips what it cannot handle reports the absence of evidence as evidence of
    absence.
19. **The same-stamp test was inverted.** Revision 11 called `kernel_accept_citation`
    with no `stamp` and spied `kernel.stamp_for_workspace`, asserting one mint — but
    Step 5 makes the kernel RECEIVE its stamp and forbids re-minting in those words.
    The test would have raised TypeError against a compliant build and gone green only
    against the forbidden one. It now pre-mints and asserts identity inward.
20. **Three of the four Step 5 mint sites had no `ws` in scope, and none was named.**
    Step 5 said only "the four routes mint per §7.1's table". MEASURED: `resolve_access`
    returns `tuple[bool, bool]`, not a Workspace, and `require_owner`/`require_member`
    return one that two routes discard. The same false belief had already produced a
    wrong Step 4 row for `save_version`. All four routes are now named with file, line,
    access check and the exact binding to add — plus the threadpool-closure return the
    stamp has to travel out through. The other five Step 4 rows were verified one by one
    against the tree and are correct.
21. **Step 3b was built on a callee that does not exist.** It told the implementer to add
    "the lock/unlock write's callee" to `_EFFECTS` and left `<lock-write callee>` as a
    placeholder in BOTH the fixture and the oracle that checked it, so any name would
    have certified itself. MEASURED: the spec-lock write is `ws.spec_locked_at = …`, an
    ORM attribute assignment with no callee at all. Step 3b is deleted and the residual
    recorded.
22. **The constructor gate missed a build inside a lambda.** `_own_calls` skips nested
    scopes and `_outside_functions` skipped every function, so `(lambda: FenceStamp(…))()`
    inside a function in an allowlisted file fell between them — PROVEN by mutation: the
    gate PASSED. Counting is now a whole-tree `ast.walk` (no scope, no blind spot) and
    the scoped walk only attributes ownership.
23. **The exemption baseline was captured after the changes it was meant to detect.**
    Task 5 Step 1 said to read the three pin sets off the tree — but Task 5 runs after
    Tasks 1-4, so any exemption they moved would have been pasted in as the baseline and
    both assertions would pass. The literal values from `8f7b1ef7` are now written down.
24. **Three placeholders survived because they PARSE.** Two `<…>` inside string literals
    and one `{...}` set-of-Ellipsis. The block scan called them clean for four
    revisions. It now rejects both shapes, each proven by mutation. The lesson repeats
    itself: a check that only sees the failures it was built for reports the rest as
    absent.
25. **`_own_calls` walked decorators and default arguments.** `ast.iter_child_nodes(func)`
    yields `decorator_list` and `args.defaults` — expressions that run at DEFINITION
    time, in the enclosing scope. PROVEN by mutation: a Primitive B call in a default
    argument sorted above a body sink and the site PASSED unfenced. It now walks the
    body only.
26. **Two calls on one line compared equal.** `_judge` compared bare line numbers, so
    `write_and_commit_files(s); acquire_commit_mutex_at_generation(...)` — effect first —
    PASSED. Positions are now `(lineno, col_offset)`.
27. **Nothing judged a lambda.** `_own_calls` correctly refused to attribute a lambda's
    calls to its enclosing function, but `check_source` never visited the lambda itself,
    so those calls were attributed to NOBODY: `go = lambda: write_and_commit_files(s)`
    with no mutex PASSED. Skipping a scope when ATTRIBUTING is right; skipping it when
    JUDGING is a hole. Both gates had this same bug independently — the constructor gate
    (item 22) and the converted gate — and it was fixed in the first without anyone
    checking the second.
28. **The lock/unlock Case B race never inspected the effect it fences.** Its five
    assertions cover git, Yjs and the room; §4.2's effect for these two routes is the DB
    lock write, which none of them touches. Having just recorded that the STATIC gate
    cannot see an ORM attribute write (item 21), the runtime race was the only remaining
    check — and it was not looking either. That is the twin pattern this loop keeps
    finding: document a limitation in one place, leave its only compensating control
    blind in another.
29. **A default argument bound the constructor.** `def rogue(ws, factory=FenceStamp)`
    built a third `FenceStamp` in an allowlisted file and the gate PASSED. Default-arg
    bindings are now tracked as aliases.
