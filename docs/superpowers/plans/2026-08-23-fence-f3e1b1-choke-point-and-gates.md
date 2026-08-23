# Fence F3e1b-1 — the choke point and both gates — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert `checkpoint_artifact` and the seven §4.2 enclosing acquisitions from `acquire_commit_mutex_live` to `acquire_commit_mutex_at_generation`, carrying a `FenceStamp` minted at each semantic boundary, and land the two gates that keep them converted.

**Architecture:** Parameter first, mutex second. Task 2 threads a `stamp: FenceStamp` through every path with **no behaviour change**; Tasks 3–4 flip the mutex, which is the moment the stamp becomes load-bearing. Both gates land in Task 1 **armed and pinning everything**, so every later task is measured as pins leaving the list rather than as a claim.

**Tech Stack:** Python 3.12, pytest (+ testcontainers Postgres), `ast` for the gates. No new dependencies.

## Global Constraints

Every task's requirements implicitly include this section.

1. **Spec §4.2's "seven" is CORRECT — do not "fix" it.** The table is scoped to F3e1b. The tree's eighth enclosing acquisition, `Phase0Worker._m1`, reaches `_init_repo`, whose row is **F5**. Converting it here is OUT OF SCOPE and makes crit-9 unsatisfiable. `test_f3e1b_owns_exactly_the_seven_enclosing_acquisitions_of_spec_4_2` already pins this.
2. **F3e1b-1 does NOT convert `save_section`, `insert_citation_for_user`, or `_insert_citation_locked`.** Those are F3e1b-2. They stay pinned throughout.
3. **No assertion may be satisfiable by a comment** (spec §5.1). Every gate assertion keys on parsed AST call nodes, never on source text. This project has shipped the comment-satisfiable mistake four times.
4. **Judge every stop-the-run decision on FAILED, never ERROR.** `tests/fencing` is not insulated from the app import chain — the root `tests/conftest.py` autouse teardown imports the app for every test in the repo, so a broken import shows as mass teardown **ERRORs with zero FAILED**, which reads like a passing mutation.
5. **Never adjust an expected number to make arithmetic close.** Baseline is **3898 collected** (measured 2026-08-23 at `8f7b1ef7`; 3897 passed + 1 skipped). Each task RECORDS its measured collected count. Where this plan states a per-task delta it is marked **PREDICTED (not run)** — if yours differs, name the term that changed; do not edit the number.
6. **The `1 skipped` is the signal.** It is a real-SDK test gated on `ANTHROPIC_API_KEY`. `0 skipped` means a live API call ran.
7. **Test commands:** `uv run --python 3.12 pytest -n 16 --dist loadfile` for the suite; never `-n auto` (resolves to 72) and never `--dist loadgroup`. The lint gate is `ruff check`, not `ruff format`.
8. **`resolve_access` does not change.** All six call sites destructure exactly two values; a third element raises at every one. Routes read the workspace themselves after the check passes and mint from it.
9. **No authorization call changes in this slice.** The mint site is "the object the existing check already loaded", whatever that check is.

---

## File Structure

| File | Responsibility |
|---|---|
| `tests/fencing/live_reach_gate.py` | NEW. The no-`_live`-reaches-a-sink gate: static call graph over `src/`, plus its pin list. |
| `tests/fencing/test_live_reach_gate.py` | NEW. Gate tests + the mutation that proves each is load-bearing. |
| `tests/fencing/converted_gate.py` | NEW. The §5.1 AST gate: a converted site calls `at_generation`, not `_live`, and the mutex lexically precedes the first effect. |
| `tests/fencing/test_converted_gate.py` | NEW. Gate tests + mutations. |
| `src/call_manager/proposal_assist/collab/checkpoint.py` | `checkpoint_artifact` gains `stamp`; its mutex flips in Task 3. |
| `src/call_manager/proposal_assist/collab/kernel.py` | The four kernel enclosing acquisitions. |
| `src/call_manager/web/api/workspaces.py` | `post_lock_spec` / `post_unlock_spec` mint and thread. |
| `src/call_manager/proposal_assist/citations/collab_cite.py` | Three `*_via_kernel` orchestrators gain `stamp`. |
| `src/call_manager/proposal_assist/proposals/kernel_accept.py` | `accept_via_kernel` gains `stamp`. |
| `src/call_manager/proposal_assist/collab/{permissions,handover}.py` | Two maintenance callers mint from their pre-screen. |
| `src/call_manager/proposal_assist/web/collab_checkpoint.py` | `save_version` mints from `resolve_access`. |
| `tests/proposal_assist/test_f3e1b1_conversions.py` | NEW. The §5.2 two-ordered-lock races, per entry point, compensation disabled. |

---

## The conversion surface, measured

`checkpoint_artifact` has **10** call sites (measured at `8f7b1ef7`). Six are self-contained — they mint from an object they already hold. Four require threading through an orchestrator.

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

### Task 1: Both gates, armed and pinning everything

Converts nothing. The gates exist and are proven load-bearing **before** any conversion, so every later task is measured as pins leaving a list.

**Files:**
- Create: `tests/fencing/live_reach_gate.py`
- Create: `tests/fencing/test_live_reach_gate.py`
- Create: `tests/fencing/converted_gate.py`
- Create: `tests/fencing/test_converted_gate.py`

**Interfaces:**
- Consumes: `tests/fencing/live_mutex_gate.py`'s `ENCLOSING_ACQUISITIONS` (a `Mapping[str, str]` of qualname → owning slice), `LIVE_ACQUIRING_SINK_HOLDERS`, `LIVE_ACQUIRERS_WITHOUT_A_SINK`, `live_acquirer_qualnames()`; `tests/fencing/git_fence.py`'s `_SINK_KIND` and `_callee_name`.
- Produces: `live_reach_gate.check_tree() -> list[str]`, `live_reach_gate.UNCONVERTED` (the pin list), `converted_gate.check_source(source, path) -> list[str]`, `converted_gate.check_tree() -> list[str]`, `converted_gate.NOT_YET_CONVERTED` (its pin list).

- [ ] **Step 1: Write the failing test for the reach gate**

```python
# tests/fencing/test_live_reach_gate.py
def test_the_tree_is_green_under_the_pin_list():
    """Every `_live` caller that reaches a sink is pinned. Nothing is converted
    yet, so a green tree here means the pin list is complete, not that the work
    is done."""
    from tests.fencing.live_reach_gate import check_tree

    assert check_tree() == []


def test_removing_a_pin_reddens_the_gate():
    """The pin list is load-bearing. A pin that can be deleted with no failure is
    not protecting anything — this project has shipped that four times."""
    import tests.fencing.live_reach_gate as g

    original = g.UNCONVERTED
    try:
        g.UNCONVERTED = frozenset(original - {"apply_citation_to_room"})
        problems = g.check_tree()
    finally:
        g.UNCONVERTED = original
    assert any("apply_citation_to_room" in p for p in problems), problems
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --python 3.12 pytest tests/fencing/test_live_reach_gate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.fencing.live_reach_gate'`

- [ ] **Step 3: Implement the reach gate**

```python
# tests/fencing/live_reach_gate.py
"""Gate: no function that transitively reaches a git sink may call
`acquire_commit_mutex_live`.

Spec §4.2. This is the gate that would have caught revision 1's defect —
`checkpoint_artifact` is a storage choke point, not the effect boundary, so
converting it alone leaves seven outer acquisitions mutating before the inner
call. §5.3 states it is therefore NOT optional.

The call graph is name-based and intraprocedural-per-edge. It OVER-approximates:
two functions sharing a name are one node. That is the safe direction — an extra
edge is an extra pin a human reads, never a hidden escape.
"""

from __future__ import annotations

import ast
import pathlib

from tests.fencing.git_fence import _SINK_KIND, _callee_name

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "call_manager"

#: Sites that still call `_live` on a path to a sink. Each entry is MIGRATION
#: DEBT, not permission. This list only SHRINKS: F3e1b-1 removes the seven §4.2
#: enclosing acquisitions and `checkpoint_artifact`; F3e1b-2 removes
#: `save_section`, `insert_citation_for_user` and `_insert_citation_locked`;
#: F5 removes `Phase0Worker._m1`. A slice that leaves an entry behind is a
#: reviewed diff.
UNCONVERTED: frozenset[str] = frozenset(
    {
        # --- F3e1b-1's seven enclosing acquisitions (spec §4.2) ---
        "apply_citation_to_room",
        "kernel_accept_prose",
        "kernel_init_spec",
        "kernel_accept_citation",
        "apply_annotation_insert_to_room",
        "post_lock_spec",
        "post_unlock_spec",
        # --- F3e1b-1's choke point ---
        "checkpoint_artifact",
        # --- F3e1b-2 ---
        "save_section",
        "insert_citation_for_user",
        "insert_from_annotation",
        "_insert_citation_locked",
        # --- F3e2 / F4 / F5 ---
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
    """Every unpinned `_live` caller that reaches a sink, as a human string."""
    calls, defines = _graph()
    problems = []
    for qual, callees in sorted(calls.items()):
        if "acquire_commit_mutex_live" not in callees:
            continue
        if qual in UNCONVERTED:
            continue
        if _reaches_sink(qual, calls, defines):
            problems.append(f"{qual} :: calls acquire_commit_mutex_live and reaches a git sink")
    return problems
```

- [ ] **Step 4: Run to verify both reach-gate tests pass**

Run: `uv run --python 3.12 pytest tests/fencing/test_live_reach_gate.py -q`
Expected: PASS, 2 passed.

If `test_the_tree_is_green_under_the_pin_list` fails, the pin list is missing a
real site. **Add it and say which — do not delete the assertion.**

- [ ] **Step 5: Write the failing test for the §5.1 converted gate**

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
    """§5.3's first mutation, as a unit test: reverting a converted site to
    `_live` must fail this gate."""
    from tests.fencing.converted_gate import check_source

    reverted = _CONVERTED.replace(
        "acquire_commit_mutex_at_generation(session, wid, generation=stamp.generation)",
        "acquire_commit_mutex_live(session, wid)",
    )
    problems = check_source(reverted, "x/f.py")
    assert any("acquire_commit_mutex_live" in p for p in problems), problems


def test_an_effect_before_the_mutex_is_rejected():
    """The mutex must lexically precede the FIRST effect. §4.1: the effect is the
    `Doc` mutation, not `store.append_update` — anchoring one line late accepts an
    implementation that mutates the live room before taking the mutex, the exact
    hollow gate §4.1 warns about."""
    from tests.fencing.converted_gate import check_source

    late = (
        "def f(session, wid, stamp, repo):\n"
        "    apply_pm_section_to_fragment(doc, section)\n"
        "    acquire_commit_mutex_at_generation(session, wid, generation=stamp.generation)\n"
        "    return write_and_commit_files(repo, {}, 'm')\n"
    )
    problems = check_source(late, "x/f.py")
    assert any("precede" in p for p in problems), problems


def test_a_comment_naming_the_primitive_does_not_satisfy_the_gate():
    """Constraint 3. The gate keys on parsed Call nodes; a comment is not a call."""
    from tests.fencing.converted_gate import check_source

    commented = (
        "def f(session, wid, stamp, repo):\n"
        "    # acquire_commit_mutex_at_generation(session, wid, generation=stamp.generation)\n"
        "    return write_and_commit_files(repo, {}, 'm')\n"
    )
    assert check_source(commented, "x/f.py") != []
```

- [ ] **Step 6: Run to verify it fails**

Run: `uv run --python 3.12 pytest tests/fencing/test_converted_gate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.fencing.converted_gate'`

- [ ] **Step 7: Implement the converted gate**

```python
# tests/fencing/converted_gate.py
"""Gate §5.1: a converted site takes Primitive B, never `_live`, before its first effect.

The lexical-precedence check is WEAKER than the real requirement — it cannot see a
call inside a helper, and lexical order is not execution order under branching. It
is a cheap ratchet, not the proof. **The proof is §5.2's runtime races.** Read
git_fence.py's module docstring: passing this does not mean the site is safe.

Every assertion keys on a parsed `ast.Call` node. Never on source text (§5.1).
"""

from __future__ import annotations

import ast

from tests.fencing.git_fence import _SINK_KIND, _callee_name
from tests.fencing.live_mutex_gate import ENCLOSING_ACQUISITIONS

_LIVE = "acquire_commit_mutex_live"
_AT_GENERATION = "acquire_commit_mutex_at_generation"

#: Callee names that count as "the first effect". §4.1: the effect is the `Doc`
#: mutation, NOT `store.append_update`.
#:
#: `append_update` is DELIBERATELY ABSENT and adding it "for completeness" is the
#: mistake §4.1 names. MEASURED at `8f7b1ef7`: including it makes the gate report
#: three `web/collab_ws.py` functions — `collab_ws`, `_persist`, `_write` — that
#: append to the log over a websocket and touch no git sink at all.
_EFFECTS: frozenset[str] = frozenset(_SINK_KIND) | {
    "apply_pm_section_to_fragment",
    "apply_pm_json_to_fragment",
}

#: Sinks other slices own. `check_source` is scope-free so it can be unit-tested on
#: fixtures; `check_tree` is the tree gate and needs this. MEASURED at `8f7b1ef7`:
#: `check_tree` reports 17, of which these nine are correctly out of scope. This
#: list only SHRINKS.
NOT_YET_CONVERTED: frozenset[str] = frozenset(
    {
        "save_section",                # F3e1b-2
        "insert_citation_for_user",    # F3e1b-2
        "_insert_citation_locked",     # F3e1b-2
        "insert_from_annotation",      # F3e2
        "build_bibliography",          # F3e2
        "post_init_spec._git",         # F3e2
        "run_review_pass",             # F4
        "Phase0Worker._init_repo",     # F5
        "Phase0Worker._m1",            # F5 (Constraint 1)
    }
)


def _own_calls(func) -> list[tuple[int, str]]:
    """`(lineno, callee-name)` for calls in THIS function's OWN scope.

    A nested `def`/`lambda`/`class` body belongs to the nested scope. `ast.walk`
    descends into them, so judging `outer` on `inner`'s effects is exactly
    F3e1a2's `_iter_own_nodes` trap — MEASURED: a two-line fixture where `outer`
    takes `_live` and a nested `inner` holds the sink makes `ast.walk(outer)`
    report `write_and_commit_files`, and the gate then blames the wrong function.
    """
    out: list[tuple[int, str]] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
                continue
            if isinstance(child, ast.Call):
                name = _callee_name(child.func)
                if name:
                    out.append((child.lineno, name))
            walk(child)

    walk(func)
    return sorted(out)


def check_source(source: str, path: str) -> list[str]:
    """§5.1's scope, verbatim: "every `git_commit` site AND every enclosing
    acquisition".

    Both halves are required. MEASURED at `8f7b1ef7`: effect-holders alone cover
    only 5 of F3e1b-1's 8 sites — `kernel_accept_citation`, `post_lock_spec` and
    `post_unlock_spec` DELEGATE rather than hold an effect, so a direct-effect rule
    cannot see them. Adding `checkpoint_artifact` to `_EFFECTS` instead was tried
    and rejected: it reaches 8/8 but also drags in every ordinary caller
    (`save_version._do`, `checkpoint_active_tails`, `direct_cite_via_kernel._body`
    …), which take no mutex and are not §5.1's business.
    """
    problems: list[str] = []

    def visit(node: ast.AST, qual: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                sub = f"{qual}.{child.name}" if qual else child.name
                if not isinstance(child, ast.ClassDef):
                    ordered = _own_calls(child)
                    names = {n for _l, n in ordered}
                    if (names & _EFFECTS) or sub in ENCLOSING_ACQUISITIONS:
                        problems += _judge(sub, path, ordered, names)
                visit(child, sub)
            else:
                visit(child, qual)

    visit(ast.parse(source), "")
    return problems


def _judge(qual: str, path: str, ordered, names) -> list[str]:
    if _LIVE in names:
        return [f"{path}::{qual} still calls {_LIVE}"]
    acq = [ln for ln, n in ordered if n == _AT_GENERATION]
    if not acq:
        return [f"{path}::{qual} is in scope but never calls {_AT_GENERATION}"]
    effects = [ln for ln, n in ordered if n in _EFFECTS]
    if effects and min(acq) > min(effects):
        return [
            f"{path}::{qual} — {_AT_GENERATION} must precede the first effect "
            f"(mutex at line {min(acq)}, effect at line {min(effects)})"
        ]
    return []


def check_tree() -> list[str]:
    """`check_source` over `src/`, minus the sinks other slices own."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    problems = []
    for path in (root / "src").rglob("*.py"):
        rel = str(path.relative_to(root))
        problems += [
            p
            for p in check_source(path.read_text(), rel)
            if p.split("::", 1)[1].split(" ", 1)[0] not in NOT_YET_CONVERTED
        ]
    return problems
```


- [ ] **Step 8: Run to verify all four converted-gate tests pass**

Run: `uv run --python 3.12 pytest tests/fencing/test_converted_gate.py -q`
Expected: PASS, 4 passed.

- [ ] **Step 9: Run the fencing suite and the lint gate**

Run: `uv run --python 3.12 pytest tests/fencing/ -q` then `uv run --python 3.12 ruff check tests/fencing/`
Expected: all pass; ruff `All checks passed!`.
**Record the measured collected count.** PREDICTED (not run): 193 + 6 = 199.

- [ ] **Step 10: Commit**

```bash
git add tests/fencing/live_reach_gate.py tests/fencing/test_live_reach_gate.py \
        tests/fencing/converted_gate.py tests/fencing/test_converted_gate.py
git commit -m "test(fence): both F3e1b gates, armed and pinning everything (F3e1b-1 Task 1)"
```

---

### Task 2: Thread `stamp` everywhere — no behaviour change

Pure plumbing. Every path carries a `FenceStamp`; **every mutex call is still `_live`.** The gates stay green because nothing left the pin list. This task is mechanical on purpose: a wrong thread here is a type error, not a plausible-looking integer.

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/checkpoint.py` (`checkpoint_artifact` signature; tail-checkpoint caller at `:210`)
- Modify: `src/call_manager/proposal_assist/collab/kernel.py` (`:174`, `:294`, `:343`, `:524` + the five enclosing functions)
- Modify: `src/call_manager/web/api/workspaces.py` (`:665`, `:746`)
- Modify: `src/call_manager/proposal_assist/web/collab_checkpoint.py` (`:52`)
- Modify: `src/call_manager/proposal_assist/collab/handover.py` (`:40`), `collab/permissions.py` (`:58`)
- Modify: `src/call_manager/proposal_assist/citations/collab_cite.py` (`direct_cite_via_kernel:49`, `init_spec_via_kernel:99`, `insert_annotation_via_kernel:149`)
- Modify: `src/call_manager/proposal_assist/proposals/kernel_accept.py` (`accept_via_kernel:56`)
- Test: `tests/proposal_assist/test_f3e1b1_conversions.py` (created here, grows in Task 5)

**Interfaces:**
- Consumes: `call_manager.proposal_assist.ports.stamp.FenceStamp` and `stamp_for_workspace(workspace) -> FenceStamp` (raises `UnfencedInvocation` when `lifecycle_generation is None`; generation 0 is REAL, so it tests for `None`, never falsiness).
- Produces: `checkpoint_artifact(session, *, workspace_id, artifact, workspaces_dir, stamp: FenceStamp, extra_files=None, record_version=True, record_event=None)`. `stamp` is **keyword-only with no default** — an optional stamp is a hole a caller can forget.

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/test_f3e1b1_conversions.py
"""F3e1b-1 — the choke point and the seven enclosing acquisitions.

Each converted site against its stamp, and each §5.2 race with the compensation
disabled: "a conversion test that passes with the fence removed is measuring the
compensation, not the fence" (spec 2026-08-18 rev 8 §5).
"""

from __future__ import annotations

import inspect

import pytest


def test_checkpoint_artifact_requires_a_stamp():
    """Keyword-only and no default. A defaulted stamp is a hole: a caller that
    forgets one still type-checks and still runs, and the gate cannot see intent."""
    from call_manager.proposal_assist.collab.checkpoint import checkpoint_artifact

    sig = inspect.signature(checkpoint_artifact)
    assert "stamp" in sig.parameters, sorted(sig.parameters)
    param = sig.parameters["stamp"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
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
    `_body()`, so the route's Workspace is discarded before they are entered. The
    stamp must be threaded, not re-derived inside."""
    import importlib

    func = getattr(importlib.import_module(module_path), func_name)
    assert "stamp" in inspect.signature(func).parameters
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --python 3.12 pytest tests/proposal_assist/test_f3e1b1_conversions.py -q`
Expected: FAIL — 5 failed (`assert 'stamp' in ...`). **FAILED, not ERROR** — if you see ERROR, an import broke; fix that first (Constraint 4).

- [ ] **Step 3: Add the parameter, self-contained sites first**

`checkpoint_artifact` (`collab/checkpoint.py:59`) — add to the signature only. Do **not** touch its `acquire_commit_mutex_live(session, workspace_id)` call at `:91`; that is Task 3.

```python
def checkpoint_artifact(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    artifact: str,
    workspaces_dir,
    stamp: FenceStamp,
    extra_files: dict[str, str] | None = None,
    record_version: bool = True,
    record_event: bool | None = None,
) -> CheckpointResult:
```

Then the six self-contained call sites. Each mints from the object its existing
check or pre-screen **already loaded** — no new query, no new authorization call:

| Call site | Add |
|---|---|
| `workspaces.py:665` (`post_lock_spec`) | `stamp=stamp_for_workspace(ws)` — `ws` from `require_owner` at `:645` |
| `workspaces.py:746` (`post_unlock_spec`) | `stamp=stamp_for_workspace(ws)` — `ws` from `require_owner` at `:734` |
| `collab_checkpoint.py:52` (`save_version`) | `stamp=stamp_for_workspace(<the Workspace resolve_access loaded at :38>)` |
| `handover.py:40` | `stamp=stamp_for_workspace(ws)` — its pre-screen at `:36-38` |
| `permissions.py:58` | `stamp=stamp_for_workspace(ws)` — its pre-screen at `:53-55` |
| `checkpoint.py:210` | `stamp=stamp_for_workspace(ws)` — its pre-screen at `:205-207` |

§7.3: the three maintenance callers act for no one, and all three already run the
identical `ws = s.get(Workspace, workspace_id)` + `soft_deleted_at` pre-screen
immediately before calling. **That instance is the mint site**, exactly as a
route's access check is. No special case and no exemption from the gate.

- [ ] **Step 4: Thread the four orchestrator paths**

Each of `direct_cite_via_kernel` (`collab_cite.py:49`), `init_spec_via_kernel`
(`:99`), `insert_annotation_via_kernel` (`:149`) and `accept_via_kernel`
(`kernel_accept.py:56`) gains `stamp: FenceStamp`, and passes it to the kernel
function it calls. Each kernel function — `apply_citation_to_room` (`:136`),
`kernel_accept_prose` (`:208`), `kernel_init_spec` (`:325`),
`kernel_accept_citation` (`:369`), `apply_annotation_insert_to_room` (`:443`) —
gains `stamp: FenceStamp` and passes it to `checkpoint_artifact`.

**`kernel_accept_citation` passes its stamp to `apply_citation_to_room`**, which
is how the seventh enclosing acquisition reaches the sink.

**Nested acquisition threads the same stamp inward** (§7.2). `kernel_accept_prose`
acquires and then calls `checkpoint_artifact`, which acquires again — safe today
because PostgreSQL advisory xact locks are re-entrant within a transaction
(`kernel.py:203-206`). The inner call receives **the same stamp object, never a
re-mint**: re-minting under a held mutex would read the same value and so hide a
caller that threaded the wrong one.

The four routes that call these orchestrators mint per §7.1's table and pass it
down. **No authorization call changes** (Constraint 9).

- [ ] **Step 5: Run the new tests, then the full suite**

Run: `uv run --python 3.12 pytest tests/proposal_assist/test_f3e1b1_conversions.py -q`
Expected: PASS, 5 passed.

Run: `uv run --python 3.12 pytest -n 16 --dist loadfile -q`
Expected: all pass, **1 skipped**. **Record the measured collected count.**
PREDICTED (not run): 3898 + 6 (Task 1) + 5 (here) = 3909.

Behaviour has not changed, so any test that now fails is a threading defect, not
an expected delta. Do not adjust a number to absorb it (Constraint 5).

- [ ] **Step 6: Confirm the gates did NOT move**

Run: `uv run --python 3.12 pytest tests/fencing/ -q`
Expected: PASS. The pin list is unchanged and no mutex call changed, so a gate
result that moved here means the gate is keying on something other than the mutex
call — **that is a finding, and it means Task 1's gate is wrong.**

- [ ] **Step 7: Commit**

```bash
git add -A src/ tests/proposal_assist/test_f3e1b1_conversions.py
git commit -m "feat(fence): thread a FenceStamp to every checkpoint path, no behaviour change (F3e1b-1 Task 2)"
```

---

### Task 3: Flip `checkpoint_artifact` to Primitive B

The stamp becomes load-bearing. One pin leaves the list.

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/checkpoint.py:91`
- Modify: `tests/fencing/live_reach_gate.py` (remove `checkpoint_artifact` from `UNCONVERTED`)
- Test: `tests/proposal_assist/test_f3e1b1_conversions.py`

**Interfaces:**
- Consumes: `acquire_commit_mutex_at_generation(session, workspace_id, *, generation: int, timeout_seconds: int = 10) -> None`. Raises `CommitMutexTimeout`, `WorkspaceDeleted` (gone or deleted), or `WorkspaceGenerationStale` (live at a different generation). **Mutates nothing in every case; the caller rolls back.** Deleted takes precedence over stale.

- [ ] **Step 1: Write the failing test**

```python
def test_checkpoint_artifact_refuses_a_stale_generation(db_session, seeded_workspace):
    """Case B in miniature, at the helper. The full route-level race is Task 5 —
    this one only proves the flip happened and the stamp is consulted."""
    from call_manager.proposal_assist.collab.checkpoint import checkpoint_artifact
    from call_manager.proposal_assist.ports.errors import WorkspaceGenerationStale
    from call_manager.proposal_assist.ports.stamp import FenceStamp
    from tests.proposal_assist.f3b_seeds import bump_generation

    wid = seeded_workspace
    stale = FenceStamp(
        workspace_id=wid, generation=0, active_turn_job_id=None, origin_job_id=None
    )
    bump_generation(db_session, wid, to=2)
    db_session.commit()

    with pytest.raises(WorkspaceGenerationStale):
        checkpoint_artifact(
            db_session,
            workspace_id=wid,
            artifact="spec",
            workspaces_dir=...,   # the fixture's dir
            stamp=stale,
        )
```

> The implementer wires `db_session` / `seeded_workspace` to this repo's existing
> fixtures and `seed_workspace` from `tests/proposal_assist/f3b_seeds.py`
> (`seed_workspace(session, *, generation=0)`, `bump_generation(session, workspace_id, *, to)`).

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --python 3.12 pytest tests/proposal_assist/test_f3e1b1_conversions.py -q -k stale_generation`
Expected: FAIL — `DID NOT RAISE WorkspaceGenerationStale` (it still calls `_live`, which does not consult a generation).

- [ ] **Step 3: Flip the mutex**

In `collab/checkpoint.py`, replace `acquire_commit_mutex_live(session, workspace_id)`
at `:91` with:

```python
    acquire_commit_mutex_at_generation(session, workspace_id, generation=stamp.generation)
```

Update the import and rewrite the `_live:` comment block above it (`:87-90`) — it
currently explains a refusal on deletion only, and the refusal is now also on
generation. Leaving the old prose makes a reader treat a stale refusal as a bug.

- [ ] **Step 4: Remove the pin and run both gates**

Delete `"checkpoint_artifact",` from `UNCONVERTED` in `tests/fencing/live_reach_gate.py`.

Run: `uv run --python 3.12 pytest tests/fencing/ tests/proposal_assist/test_f3e1b1_conversions.py -q`
Expected: PASS.

- [ ] **Step 5: Mutation — the pin removal must be load-bearing**

Restore `"checkpoint_artifact",` to `UNCONVERTED`, run
`uv run --python 3.12 pytest tests/fencing/test_live_reach_gate.py -q`, and confirm
`test_no_pin_is_stale` FAILS (a converted site left on the pin list). Then remove it again.

**Commit before mutating and restore from a copy — `git checkout <path>` destroys
any uncommitted work in the same file.**

- [ ] **Step 6: Full suite, then commit**

Run: `uv run --python 3.12 pytest -n 16 --dist loadfile -q`
Expected: all pass, 1 skipped. **Record the measured count.**

```bash
git add -A src/ tests/
git commit -m "feat(fence): checkpoint_artifact takes the mutex at a generation (F3e1b-1 Task 3)"
```

---

### Task 4: Flip the seven enclosing acquisitions

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/kernel.py` (`:136`, `:208`, `:325`, `:369`, `:443`)
- Modify: `src/call_manager/web/api/workspaces.py` (`:651`, `:739`)
- Modify: `tests/fencing/live_reach_gate.py` (seven pins leave)
- Test: `tests/fencing/test_converted_gate.py`

**Interfaces:**
- Consumes: Task 2's threaded `stamp` on all five kernel functions and both routes; Task 3's converted `checkpoint_artifact`.
- Produces: a green `converted_gate.check_tree()`. MEASURED at `8f7b1ef7`: it reports **17**, of which **8** are F3e1b-1's (the seven §4.2 enclosing acquisitions plus `checkpoint_artifact`) and **9** are the pinned out-of-scope sinks. Task 4 takes the 8 to zero.

- [ ] **Step 1: Write the failing test**

```python
def test_the_converted_gate_is_green_over_the_tree():
    """§5.1 over the real tree. `NOT_YET_CONVERTED` carries the three sinks other
    slices own; everything else must satisfy the gate once Task 4 lands.

    `Phase0Worker._m1` needs no entry here — it acquires the mutex but holds no
    effect itself, so the gate never looks at it. It is the reach gate that keeps
    it pinned (Constraint 1)."""
    from tests.fencing.converted_gate import check_tree

    assert check_tree() == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --python 3.12 pytest tests/fencing/test_converted_gate.py -q -k green_over_the_tree`
Expected: FAIL, naming the seven still-`_live` enclosing acquisitions.

**This test belongs to Task 4, not Task 1** — it is the only assertion in the plan
that cannot be green until the seven flips land, so writing it earlier would leave
the suite red across three tasks and mask an unrelated failure. Task 1 ships
`check_tree()` with no test calling it; this is where it gets one.

**MEASURED at `8f7b1ef7`:** `check_tree()` reports **14** before any conversion —
eleven `still calls acquire_commit_mutex_live`, three `reaches an effect without
acquire_commit_mutex_at_generation`.

**Expect it to still report the F3e1b-2 sites after Task 4.** `save_section`,
`insert_citation_for_user`, `insert_from_annotation` and `_insert_citation_locked`
hold effects and still call `_live`, so a bare `check_tree() == []` cannot pass
until F3e1b-2 lands. Add those four to `NOT_YET_CONVERTED` in this task, with a
comment naming F3e1b-2 as their owner, and **record the measured count before and
after** rather than assuming the list is right.

- [ ] **Step 3: Flip all seven**

At each of the seven, replace `acquire_commit_mutex_live(session, workspace_id)`
with `acquire_commit_mutex_at_generation(session, workspace_id, generation=stamp.generation)`.

**The mutex must lexically precede the first effect** (§5.1), and §4.1 is explicit
that the first effect is the **`Doc` mutation**, not `store.append_update`. In
`kernel.py` that means the acquisition goes above `apply_pm_section_to_fragment`
(`:169`, `:274`, `:517`) and `apply_pm_json_to_fragment` (`:338`) — not merely
above the `checkpoint_artifact` call. Anchoring it one line late accepts an
implementation that mutates the live room before taking the mutex: the exact
defect §4.2 describes.

**Do not re-mint inside any of these.** They receive their stamp from Task 2.

- [ ] **Step 4: Remove the seven pins, run both gates**

Delete the seven §4.2 entries from `UNCONVERTED`. `save_section`,
`insert_citation_for_user`, `insert_from_annotation`, `_insert_citation_locked`,
`post_init_spec._git` and `Phase0Worker._m1` **stay** (Constraints 1 and 2).

Run: `uv run --python 3.12 pytest tests/fencing/ -q`
Expected: PASS.

- [ ] **Step 5: Mutation — §5.3's second proof, which is NOT optional**

Revert **one** enclosing acquisition (`apply_citation_to_room`) to
`acquire_commit_mutex_live` and confirm `test_the_tree_is_green_under_the_pin_list`
**FAILS naming that function**. This is the mutation that would have caught
revision 1's defect. Record the exact failure text, then restore from a copy.

Separately: move `apply_citation_to_room`'s acquisition to *below*
`apply_pm_section_to_fragment` and confirm the converted gate fails on
`must precede the first effect`. Restore.

- [ ] **Step 6: Full suite, then commit**

Run: `uv run --python 3.12 pytest -n 16 --dist loadfile -q`
Expected: all pass, 1 skipped. **Record the measured count.**

```bash
git add -A src/ tests/fencing/
git commit -m "feat(fence): the seven enclosing acquisitions take the mutex at a generation (F3e1b-1 Task 4)"
```

---

### Task 5: The §5.2 two-ordered-lock races, per entry point

The gates are a ratchet; **this is the proof.** At the semantic boundary, not the helper: a test that calls `checkpoint_artifact` with a hand-built stamp proves nothing about minting (§7.4).

**Files:**
- Modify: `tests/proposal_assist/test_f3e1b1_conversions.py`

**Interfaces:**
- Consumes: `seed_workspace`, `bump_generation` from `tests/proposal_assist/f3b_seeds.py`; the entry points in §7.1's table.

- [ ] **Step 1: Write the failing races**

For **each** of these seven entry points — `POST …/cite` (collab), accept (collab, prose), accept (collab, citation), `POST …/spec/init` (collab), `POST …/annotations/{id}/insert`, `POST …/spec/lock`, `POST …/spec/unlock` — write **both** cases.

**Case A — effect first, lifecycle second.** The effect takes the mutex at G and commits; the delete/restore blocks and proceeds after. The write **lands**, and that is correct: it was live when it ran. *Case A is what proves the fence does not simply refuse everything* — without it, an implementation that refuses unconditionally passes every Case B.

**Case B — lifecycle first, effect second.** Delete+restore commits to G+2, then the operation proceeds. It must raise and land **nothing**. Assert, on the workspace repo:

```python
    assert head_before == _git(repo, "rev-parse", "HEAD")          # HEAD unchanged
    assert _git(repo, "status", "--porcelain") == ""               # worktree clean
    assert path.read_bytes() == before_bytes                       # byte-identical
    assert session.query(YjsUpdate).filter_by(...).count() == n0   # no yjs_updates row
    assert doc_state_before == _doc_state(room)                    # room Doc unmutated
```

All five, every time. §5.2 is explicit: a proof that inspects only rows passes
while bytes are already on disk; an unchanged HEAD is not enough, because a stale
implementation can dirty the working tree without committing; and cleanliness
alone passes an implementation that wrote and then reverted. **For the kernel
paths the last two are not optional** — a test that checked only git would pass
the exact defect §4.2 describes.

**Disable the compensation in every one of these tests.** Cancel-on-delete is
still in the tree and masks staleness for statements touching the same row; a
conversion test that passes with the fence removed is measuring the compensation,
not the fence. This is the technique that exposed Slice C1's decorative tests.
Follow the precedent in `tests/proposal_assist/test_f3d2_conversions.py`.

- [ ] **Step 2: Run to verify each fails for the right reason**

Run: `uv run --python 3.12 pytest tests/proposal_assist/test_f3e1b1_conversions.py -q`

Each Case B must fail on **its named assertion** — not by aborting collection
(Constraint 4). A Case B that passes immediately is a finding: it means the
compensation is still masking, or the race is not actually racing.

- [ ] **Step 3: Make them pass**

No `src/` change should be needed — Tasks 2–4 did the work. If a race fails, the
conversion is wrong; fix the conversion, not the test.

- [ ] **Step 4: The two the carrier makes possible (§7.4)**

```python
def test_stamp_for_workspace_refuses_a_workspace_with_no_generation():
    """Mirrors Invocation.stamp()'s UnfencedInvocation — asserted, not assumed.
    Generation 0 is REAL (server_default 0), so this must test None, not falsiness."""
    from call_manager.proposal_assist.ports.errors import UnfencedInvocation
    from call_manager.proposal_assist.ports.stamp import stamp_for_workspace

    class _NoGeneration:
        id = uuid.uuid4()
        lifecycle_generation = None

    with pytest.raises(UnfencedInvocation):
        stamp_for_workspace(_NoGeneration())


def test_a_zero_generation_is_accepted():
    """The falsiness trap, pinned. Generation 0 is a real generation."""
    from call_manager.proposal_assist.ports.stamp import stamp_for_workspace

    class _Zero:
        id = uuid.uuid4()
        lifecycle_generation = 0

    assert stamp_for_workspace(_Zero()).generation == 0


def test_the_static_gate_rejects_an_inline_late_read():
    """§7.4: the defect four revisions could only warn about in prose. Already
    covered by tests/fencing/test_stamp_gate.py::test_an_inline_late_read_is_rejected
    — assert here that the REAL tree has none."""
    import pathlib

    from tests.fencing.stamp_gate import check_source

    root = pathlib.Path(__file__).resolve().parents[2]
    problems = []
    for path in (root / "src").rglob("*.py"):
        problems += check_source(path.read_text(), str(path.relative_to(root)))
    assert problems == [], problems
```

- [ ] **Step 5: Full suite, then commit**

Run: `uv run --python 3.12 pytest -n 16 --dist loadfile -q`
Expected: all pass, 1 skipped. **Record the measured count.**

```bash
git add tests/proposal_assist/test_f3e1b1_conversions.py
git commit -m "test(fence): two ordered lock cases per converted entry point (F3e1b-1 Task 5)"
```

---

### Task 6: Inventory, slice status, and the identity checks

**Files:**
- Modify: `tests/fencing/inventory.py` (`SLICE_STATUS`)
- Modify: `tests/fencing/fence_inventory.json` (regenerated, not hand-edited)
- Create: `tests/fencing/test_f3e1b1_slice.py`

**Interfaces:**
- Consumes: `tests/fencing/regenerate_inventory.py`; `load_inventory()` and `SLICE_STATUS` from `tests/fencing/inventory.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/fencing/test_f3e1b1_slice.py
def test_this_slice_is_recorded_complete():
    """F3e1b-1 owns no NEW inventory row, so a row-iterating test never reads its
    status. F3e1a2 shipped exactly this hole: deleting its status key left the
    whole fencing suite green. Assert it directly."""
    from tests.fencing.inventory import SLICE_STATUS

    assert SLICE_STATUS["F3e1b-1"] == "complete"


def test_the_four_checkpoint_rows_are_still_pending():
    """F3e1b-1 converts the CHOKE POINT and the enclosing acquisitions. The four
    row-owning sinks belong to F3e1b-2 (Constraint 2) — `checkpoint_artifact`'s
    own row moves only when its slice does."""
    from tests.fencing.inventory import SLICE_STATUS, load_inventory

    rows = [r for r in load_inventory() if r["kind"] == "git_commit"]
    assert len(rows) == 9
    assert {r["slice"] for r in rows if r["slice"] == "F3e1b"} == {"F3e1b"}
    assert SLICE_STATUS["F3e1b"] == "pending"


def test_the_reach_gate_pin_list_shrank_by_exactly_eight():
    """Identity, not count: naming them is what stops a swap. Eight left in this
    slice — the seven §4.2 enclosing acquisitions and `checkpoint_artifact`."""
    from tests.fencing.live_reach_gate import UNCONVERTED

    assert UNCONVERTED == frozenset(
        {
            "save_section",
            "insert_citation_for_user",
            "insert_from_annotation",
            "_insert_citation_locked",
            "post_init_spec._git",
            "Phase0Worker._m1",
        }
    )
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --python 3.12 pytest tests/fencing/test_f3e1b1_slice.py -q`
Expected: FAIL — `KeyError: 'F3e1b-1'`.

- [ ] **Step 3: Add the slice status and regenerate the inventory**

Add `"F3e1b-1": "complete",` to `SLICE_STATUS`. **Do not hand-edit
`fence_inventory.json`** — regenerate it (`tests/fencing/regenerate_inventory.py`)
and commit the regenerated file. §5.3: hand-editing a `git_commit` row's `fenced`
to `true` must not survive regeneration.

- [ ] **Step 4: Run the identity checks and paste the real output**

```bash
BASE=8f7b1ef7   # master at the start of F3e1b-1

# 1. The seven-vs-eight rule still holds
uv run --python 3.12 pytest tests/fencing/test_live_mutex_gate.py -q \
  -k "seven_enclosing or partition_exactly"

# 2. The allowlist is still six entries
uv run --python 3.12 pytest tests/fencing/test_f3e1a2_slice.py -q -k allowlist

# 3. Exemption pins unchanged (branch on git diff's EXIT STATUS, never on empty
#    stdout: a failing `git diff` prints nothing, and `grep`-then-`|| echo OK`
#    reports OK for an error exactly as it does for no change)
git diff --quiet "$BASE"..HEAD -- tests/fencing/inventory.py; rc=$?
case $rc in
  0) echo "inventory.py UNCHANGED" ;;
  1) echo "inventory.py CHANGED — expected (SLICE_STATUS gained F3e1b-1); review the diff" ;;
  *) echo "ERROR: git diff failed with $rc"; exit 1 ;;
esac
```

- [ ] **Step 5: Full suite and lint, then commit**

Run: `uv run --python 3.12 pytest -n 16 --dist loadfile -q` and
`uv run --python 3.12 ruff check .`
Expected: all pass, **1 skipped**; ruff `All checks passed!`.
**Record the measured collected count.** Do not reconcile it against a prediction
by editing the prediction.

```bash
git add tests/fencing/
git commit -m "test(fence): F3e1b-1 recorded complete, eight pins gone (F3e1b-1 Task 6)"
```

---

## What this slice does NOT prove

State this in the final review rather than discovering it later:

- **Passing the converted gate does not mean a site is safe.** §5.1 checks lexical
  order; the commit mutex is transaction-scoped, so `acquire(); s.commit(); sink()`
  passes and is not fenced. A helper that commits is invisible to it entirely. The
  proof is Task 5's runtime races.
- **The reach gate over-approximates.** Its call graph is name-based, so two
  same-named functions are one node. Safe direction — an extra edge is an extra
  pin, never a hidden escape — but it is not a precise call graph.
- **Four sinks remain unconverted** (`save_section`, `insert_citation_for_user`,
  `insert_from_annotation`, `_insert_citation_locked`) plus `post_init_spec._git`
  and `Phase0Worker._m1`. Their rows stay `pending` and their pins stay listed.
