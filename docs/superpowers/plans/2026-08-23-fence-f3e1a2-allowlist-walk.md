# Fence F3e1a2 — the allowlist walk — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teach `tests/fencing/git_fence.py` to credit a sink's position through an explicit six-entry allowlist, so F3e1b's four sites can reach `fenced: true` once converted — while measuring nothing differently today.

**Architecture:** Discovery is broadened (every sink stays visible, including ones in nested defaults nobody can credit); crediting is narrowed to an allowlist of `(node type, field)` pairs, with anything unrecognised failing closed. Two disqualifiers are added — an own-scope `yield` and a visible transaction ending — because both can drop the transaction-scoped mutex before the sink runs.

**Tech Stack:** Python 3.12 stdlib `ast` only. `pytest`. No `src/` change, no new dependency.

**Spec:** `docs/superpowers/specs/2026-08-23-fence-f3e1a2-containing-statement-order-design.md` (revision 7, commit `ea481e2c`). Section numbers below refer to it.

## Global Constraints

- **Baseline is `3857 passed, 1 skipped`** on master at `ea481e2c`. The `1 skipped` is the `ANTHROPIC_API_KEY`-gated real-SDK test. **`0 skipped` means a live billed API call ran** — do not export that key.
- **Fast suite:** `uv run pytest -n 16 --dist loadfile -q`. Never `-n auto` (resolves to 72 here). Never `--dist loadgroup`.
- **`ruff check .` is THE lint gate.** `ruff format` is not. Line limit 100. Rules: `E,F,I,B,UP,SIM`.
- **`tests/fencing` is NOT insulated from the app import chain.** The root `tests/conftest.py` autouse teardown imports the app for every test in the repo, so a broken import shows as mass teardown **ERRORS with zero FAILED**. **Judge every mutation on FAILED, never on ERROR.**
- **NO `src/` change in this slice.** Check it against the **fixed base `ea481e2c`**, not `master`: this work may happen directly on `master`, where `master..HEAD` is empty and the check is vacuous. `git diff --stat ea481e2c..HEAD -- src/` must be empty at every commit. Slice-identity line — if a task seems to need a `src/` change, STOP and escalate.
- **The base commit is `ea481e2c`** (spec revision 7). Every identity check in this plan uses it literally. Do not substitute `master`, `HEAD~n`, or a merge-base.
- **The inventory must not move.** `fence_inventory.json` byte-identical, all nine `git_commit` rows still `fenced: false`, exemption pins still 5.
- **Never widen §4.1's allowlist.** Six entries, exactly what the nine production paths need. Eagerness is NOT a reason to add one (§4.1). If a task seems to need a seventh, STOP and escalate.
- **Every uncredited and disqualified test case asserts the qualname is STILL a sink holder.** An invisible sink is worse than an unfenced one (§3).

---

## File Structure

| File | Change |
|---|---|
| `tests/fencing/git_fence.py` | Modify. New allowlist table, new field-aware walk, `sink_calls` rewritten, two disqualifiers, docstring corrections. |
| `tests/fencing/test_git_fence.py` | Modify. 15 existing tests keep their verdict; spec §5.3's 7 positives and §5.4's 30 negatives added, as 26 new test functions (some cover several cases). |
| `tests/fencing/live_mutex_gate.py` | Modify. Module docstring only — drop "provably". |
| `tests/fencing/inventory.py` | Modify. One `SLICE_STATUS` key. |
| `tests/fencing/test_f3e1a2_slice.py` | Create. Slice-identity confirmation. |

**Do NOT touch** `_iter_own_nodes` or `_collect_functions` (§4.2): `_collect_functions` consumes `_iter_own_nodes` as plain AST nodes at `git_fence.py:176`, and making it yield tuples would turn `child is stmt` and `isinstance(child, …)` silently False — emptying qualname discovery **without raising**. The new walk is added alongside.

---

### Task 1: The allowlist and the field-aware walk

**Files:**
- Modify: `tests/fencing/git_fence.py`
- Test: `tests/fencing/test_git_fence.py`

**Interfaces:**
- Consumes: `_callee_name`, `_sink_bindings`, `_is_commit_call`, `_collect_functions` (all unchanged).
- Produces:
  - `CREDITED_FIELDS: dict[type[ast.AST], frozenset[str]]` — §4.1's table.
  - `_iter_credited(node, credited: bool) -> Iterator[tuple[ast.AST, bool]]` — the walk of §4.2.
  - `sink_calls(source, path) -> list[tuple[str, ast.Call, int | None]]` — unchanged signature, new semantics.

- [ ] **Step 1: Write the failing tests**

Append to `tests/fencing/test_git_fence.py`:

```python
# ---------------------------------------------------------------- F3e1a2 §5.3
# One positive per §4.1 allowlist entry. There is deliberately NO positive for
# `for`, `while`, `return`, comprehensions or defaults: §4.1 does not credit them,
# and adding one would be a request to widen the list.

_ASYNC_HEAD = "async def f(s, w, stamp, repo, cond):\n"


def test_assign_value_is_credited():
    assert _fenced(_ACQ + "    sha = write_and_commit_files(repo, {}, 'm')\n") == {"f"}


def test_expr_value_is_credited():
    assert _fenced(_ACQ + _SINK) == {"f"}


def test_if_body_is_credited():
    assert _fenced(_ACQ + "    if cond:\n    " + _SINK) == {"f"}


def test_try_body_is_credited():
    src = _ACQ + "    try:\n    " + _SINK + "    except Exception:\n        pass\n"
    assert _fenced(src) == {"f"}


def test_with_body_is_credited():
    assert _fenced(_ACQ + "    with open('x') as fh:\n    " + _SINK) == {"f"}


def test_async_with_body_is_credited():
    src = _ASYNC_HEAD + _ACQ + "    async with lock:\n    " + _SINK
    assert fenced_qualnames(src, "x/f.py") == {"f"}


# ---------------------------------------------------------------- F3e1a2 §5.4
# Uncredited paths. Each asserts BOTH verdicts: unfenced AND still a holder.
# The holder assertion is the point — an invisible sink is worse than an unfenced
# one (spec §3), and a future "fix" that loses the site must go red here.

def _uncredited_but_held(body: str, head: str = _HEAD) -> None:
    """The sink's POSITION is unprovable: unfenced, still found, index None."""
    src = head + body
    calls = sink_calls(src, "x/f.py")
    assert fenced_qualnames(src, "x/f.py") == set(), "must not be credited"
    assert {q for q, _n, _i in calls} == {"f"}, "sink must remain discoverable"
    assert [i for _q, _n, i in calls] == [None], "position must be unprovable"


def _disqualified_but_held(body: str, head: str = _HEAD) -> None:
    """The sink's position is FINE — the FUNCTION is disqualified.

    Distinct from _uncredited_but_held ON PURPOSE. These cases put the sink on a
    CREDITED path (`sha = sink(...)` is `Assign.value`), so its index is an INT, not
    None — asserting None fails for the wrong reason.

    It asserts `isinstance(i, int)` rather than a literal. An earlier draft hardcoded
    `[2]` and was wrong for half these cases: measured, `yield` before the sink gives
    2, `yield` after gives 1, and a `yield` in a nested default gives 1. The exact
    index is an artefact of how many statements the fixture happens to have; what
    matters is that the position was credited and the verdict is still unfenced.
    """
    src = head + body
    calls = sink_calls(src, "x/f.py")
    assert fenced_qualnames(src, "x/f.py") == set(), "must be disqualified"
    assert {q for q, _n, _i in calls} == {"f"}, "sink must remain discoverable"
    indices = [i for _q, _n, i in calls]
    assert all(isinstance(i, int) for i in indices), (
        f"the POSITION should be credited here; the FUNCTION is what fails. got {indices}"
    )


def test_lambda_body_is_not_credited():
    _uncredited_but_held(_ACQ + "    return lambda: write_and_commit_files(repo, {}, 'm')\n")


def test_generator_expression_is_not_credited():
    _uncredited_but_held(
        _ACQ + "    return (write_and_commit_files(repo, {}, 'm') for i in [1])\n"
    )


def test_lambda_nested_in_a_returned_container_is_not_credited():
    _uncredited_but_held(
        _ACQ + "    return {'go': lambda: write_and_commit_files(repo, {}, 'm')}\n"
    )


def test_generator_expression_as_a_call_argument_is_not_credited():
    _uncredited_but_held(
        _ACQ + "    register((write_and_commit_files(repo, {}, 'm') for i in [1]))\n"
    )


def test_a_pep695_type_alias_is_not_credited():
    """PEP 695: the RHS is lazy until `__value__` is read, and the alias can escape."""
    _uncredited_but_held(_ACQ + "    type Deferred = write_and_commit_files(repo, {}, 'm')\n")


def test_an_unlisted_NODE_type_is_not_credited():
    """ast.For is not in CREDITED_FIELDS at all. The default must be no-credit."""
    _uncredited_but_held(_ACQ + "    for i in [1]:\n    " + _SINK)


def test_a_live_acquisition_inside_a_LAMBDA_still_disqualifies():
    """Verified to disqualify today via `has_live`, which uses ast.walk and so
    descends into lambdas. The new walk must not accidentally excuse it: for a
    DISQUALIFIER, deferred execution is not a defence."""
    src = (
        _HEAD
        + _ACQ
        + "    cb = lambda: acquire_commit_mutex_live(s, w)\n"
        + "    sha = write_and_commit_files(repo, {}, 'm')\n"
    )
    assert fenced_qualnames(src, "x/f.py") == set()


# --- DISCOVERY. These live in Task 1, not later, because spec §5.7 requires every
# --- case locked BEFORE the implementation that satisfies it. All execute in the
# --- ENCLOSING scope when their statement runs; NONE is credited, because eager is
# --- not a reason to credit (§4.1). Each must still be FOUND — that is the point.

def test_a_sink_in_a_nested_defs_DEFAULT_is_found_but_not_credited():
    _uncredited_but_held(
        _ACQ + "    def inner(x=write_and_commit_files(repo, {}, 'm')):\n        return x\n"
    )


def test_a_sink_in_a_nested_defs_DECORATOR_is_found_but_not_credited():
    _uncredited_but_held(
        _ACQ
        + "    @register(write_and_commit_files(repo, {}, 'm'))\n"
        + "    def inner():\n        pass\n"
    )


def test_a_sink_in_a_LAMBDAS_default_is_found_but_not_credited():
    _uncredited_but_held(_ACQ + "    cb = lambda x=write_and_commit_files(repo, {}, 'm'): x\n")


def test_a_sink_in_a_nested_ASYNC_defs_default_is_found_but_not_credited():
    _uncredited_but_held(
        _ACQ
        + "    async def inner(x=write_and_commit_files(repo, {}, 'm')):\n        return x\n"
    )


def test_a_sink_in_an_ANNOTATION_is_found_but_not_credited():
    """Argument and return annotations evaluate at `def` time, in THIS scope, unless
    the scanned file carries `from __future__ import annotations` — which the checker
    cannot rely on. Fail closed: found, never credited."""
    _uncredited_but_held(
        _ACQ + "    def inner(x: write_and_commit_files(repo, {}, 'm')):\n        pass\n"
    )
    _uncredited_but_held(
        _ACQ + "    def inner() -> write_and_commit_files(repo, {}, 'm'):\n        pass\n"
    )


def test_a_sink_in_a_nested_CLASS_is_found_but_not_credited():
    """A `class` statement runs its decorators, bases, keywords and body immediately,
    in this function. All four shapes were invisible before this slice."""
    for body in (
        "    class C:\n        x = write_and_commit_files(repo, {}, 'm')\n",
        "    class C(write_and_commit_files(repo, {}, 'm')):\n        pass\n",
        "    class C(metaclass=write_and_commit_files(repo, {}, 'm')):\n        pass\n",
        "    @register(write_and_commit_files(repo, {}, 'm'))\n    class C:\n        pass\n",
    ):
        _uncredited_but_held(_ACQ + body)


def test_an_unlisted_FIELD_of_a_listed_node_is_not_credited():
    """`ast.If` IS listed, but only its `body`. `orelse` is not.

    The unknown-NODE test above does not prove this: an implementation that
    allowlisted by node type alone would pass that one and fail here.
    """
    _uncredited_but_held(_ACQ + "    if cond:\n        pass\n    else:\n    " + _SINK)
```

- [ ] **Step 2: Run them to see which fail and why**

Run: `uv run pytest tests/fencing/test_git_fence.py -q -p no:randomly`

Expected: **10 failed, 25 passed.** The failures are the five credited-container
positives (`Assign`, `If`, `Try`, `With`, `AsyncWith`) and the five discovery cases
that today are not found at all — nested-`def` default, nested-`def` decorator,
nested-`async def` default, annotation, and class. `test_expr_value_is_credited`
already passes — it is F3e1a's shape. **Most negatives already pass**, because today
*everything* nested returns `None`: they cannot tell a correct implementation from a
blanket `None`, which is exactly what Step 5's mutations are for. Re-derive this split
by running it; do not trust this paragraph over the terminal.

**This is expected and it is the point:** the negatives cannot distinguish a correct implementation from today's blanket `None`. Task 1's mutations (Step 5) are what pin them.

- [ ] **Step 3: Add the allowlist table**

In `tests/fencing/git_fence.py`, after `_ACQ_LIVE` (around line 63):

```python
#: F3e1a2 §4.1. The ONLY (node type, field) steps along which a sink's position is
#: credited. Everything absent from this table — an unlisted node, an unlisted field
#: of a listed node, or anything a future Python adds — yields stmt_index=None and
#: fails closed.
#:
#: Six entries, and they are exactly what the nine production sink paths need,
#: derived from the tree rather than chosen. DO NOT ADD AN ENTRY BECAUSE A CONSTRUCT
#: IS EAGER. `For.body`, `Return.value`, `If.orelse`, `Try.finalbody` and every
#: comprehension are all eager and all deliberately absent: each is an under-report
#: costing a pin, and the smallness of this table IS the safety property. Add an
#: entry only when a real site needs it, as a reviewed diff with its own test.
CREDITED_FIELDS: dict[type[ast.AST], frozenset[str]] = {
    ast.Assign: frozenset({"value"}),
    ast.Expr: frozenset({"value"}),
    ast.If: frozenset({"body"}),
    ast.Try: frozenset({"body"}),
    ast.With: frozenset({"body"}),
    ast.AsyncWith: frozenset({"body"}),
}

```

- [ ] **Step 4: Add the walk and rewrite `sink_calls`'s descent**

Add after `_iter_own_nodes` (leave that function and `_collect_functions` untouched — §4.2):

```python
def _walk_fields(node: ast.AST, skip: tuple[str, ...] = ()):
    """Yield every direct AST child of *node*, skipping the named fields."""
    for field, value in ast.iter_fields(node):
        if field in skip:
            continue
        for child in value if isinstance(value, list) else [value]:
            if isinstance(child, ast.AST):
                yield child


def _iter_credited(node: ast.AST, credited: bool):
    """Yield ``(node, credited)`` for everything reachable in the ENCLOSING scope.

    F3e1a2 §4.2.

    * **Monotonic.** ``credited`` starts True at a top-level statement and can only
      go False — a listed field beneath an unlisted one stays uncredited, or a sink
      at ``Lambda.body -> Assign.value`` would be credited.
    * **Discovery does not stop at False.** The walk keeps descending, so a sink in a
      lambda, generator expression, type alias, class base or annotation is still
      FOUND; only its position is lost. A site missing from the inventory is worse
      than one measuring ``false``.
    * **A definition's ONLY unwalked field is a FUNCTION's body.** Everything else a
      ``def``/``class`` carries — decorators, bases, keywords, argument defaults,
      argument and return ANNOTATIONS, the class body — executes in the enclosing
      scope when that statement runs, so all of it is walked, uncredited.

    The def/lambda/class asymmetry is deliberate. Read this before "simplifying" it:

    * a nested ``def``'s **body** is skipped — it has its own qualname and
      ``_collect_functions`` analyses it on a separate pass, so nothing is lost;
    * a ``lambda``'s body is **walked** — a lambda is ANONYMOUS, gets no qualname,
      and nothing else ever visits it, so skipping it makes a sink inside VANISH;
    * a ``class``'s body is **walked** — its statements run at class-definition time,
      in this function. Methods inside are ``FunctionDef``s and stop at their own
      body by the first rule.

    Unlike ``_iter_own_nodes`` this handles a definition as its ENTRY node, not only
    as a child — ``sink_calls`` no longer pre-skips them.
    """
    yield node, credited

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for child in _walk_fields(node, skip=("body",)):
            yield from _iter_credited(child, False)
        return

    if isinstance(node, (ast.Lambda, ast.ClassDef)):
        for child in _walk_fields(node):
            yield from _iter_credited(child, False)
        return

    allowed = CREDITED_FIELDS.get(type(node), frozenset())
    for field, value in ast.iter_fields(node):
        child_credited = credited and field in allowed
        for child in value if isinstance(value, list) else [value]:
            if isinstance(child, ast.AST):
                yield from _iter_credited(child, child_credited)
```

Then in `sink_calls`, **delete the pre-skip** and replace the descent. The loop body becomes:

```python
    for qualname, func in _collect_functions(tree, ""):
        body = func.body
        for stmt_idx, stmt in enumerate(body):
            # NO pre-skip: a nested def's decorators and defaults run HERE, in this
            # function's scope, and a sink in one must still be discovered (§4.2).
            for node, credited in _iter_credited(stmt, True):
                if not isinstance(node, ast.Call):
                    continue
                callee = _callee_name(node.func)
                if callee is None:
                    continue
                canonical = bindings.get(callee)
                if canonical not in relevant:
                    continue
                if not _is_commit_call(canonical, node):
                    continue
                results.append((qualname, node, stmt_idx if credited else None))
```

Note the top-level statement itself is yielded with `credited=True`, so a bare
`ast.Expr` sink is credited by `Expr.value` like any other path — F3e1a's special
`is_bare_top_level` branch is now redundant and must be **deleted**.

**Two docstrings in this file now describe the old rule and must be corrected in the
same commit**, or the module documents a checker it no longer contains:

- **`sink_calls`** (`git_fence.py:200-218`) says `stmt_index` is set only for a *bare
  top-level expression statement*. Replace with: set to the containing top-level
  statement's index when every step of the descent was allowlisted (`CREDITED_FIELDS`),
  and `None` otherwise — including deferred contexts and anything unrecognised.
- **`unprotected_sinks`** (`git_fence.py:272-282`) lists the original three clauses.
  It now has five: add the own-scope `yield` disqualifier (§4.3, Task 2) and the
  visible transaction ending (§4.4, Task 3). Write all five now and implement the last
  two in their tasks — a docstring that lags its code by two tasks is how the module
  docstring got to "provably fenced" in the first place.

> **Prototyped before this plan was committed.** The code above was run against
> `git_fence.py` at `ea481e2c` and reverted. All 6 positives and all 17 uncredited and
> discovery shapes behave as specified, `153 passed`, real tree still nine holders and
> an empty fenced set. **The first version of this code lost lambda-body sinks
> entirely** — caught by running it, not by reading it, which is why the asymmetry
> above carries a comment rather than a shrug. Treat this as evidence the code works,
> not that it is correct: lock the tests first, per spec §5.7.

- [ ] **Step 5: Run the tests, then prove the new logic by breaking it**

Run: `uv run pytest tests/fencing/test_git_fence.py -q -p no:randomly`
Expected: **35 passed** (15 existing + 20 new).

Then run the full fencing package: `uv run pytest tests/fencing/ -q -p no:randomly`
Expected: **173 passed** (153 + 20).

> ### Restoring a mutation: NEVER `git checkout --`, and NEVER a stale checkpoint
>
> The implementation is **not committed yet** at mutation time. `git checkout --
> tests/fencing/git_fence.py` restores **pre-task HEAD** and silently deletes
> everything you just wrote.
>
> **Take a FRESH checkpoint at the start of every task's mutation step**, named for
> that task. Task 1's checkpoint predates `_is_generator` and `_ends_transaction`;
> restoring Task 2 or Task 3 from it deletes the function that task just added, and
> the tests go green again for the wrong reason:
>
> ```bash
> cp tests/fencing/git_fence.py /tmp/git_fence.taskN.py   # N = THIS task
> # ...apply mutation, run, observe FAILED-not-ERROR...
> cp /tmp/git_fence.taskN.py tests/fencing/git_fence.py   # restore
> ```
>
> Re-run and confirm green after every restore, before the next mutation.
> **This applies to every mutation step in every task of this plan.**

Now the safety mutations. For each: apply, run, confirm **FAILED** (not ERROR) for the
named tests, restore from the checkpoint, re-confirm green.

| # | Mutation | Must redden |
|---|---|---|
| 1 | `stmt_index <= first_acq` → `<` | **Measured: NOTHING — this mutation is INERT, and the reason is a real consequence of the allowlist.** Spec §5.5 expects it to flip the same-statement-argument case. Under F3e1a it did. It cannot now: that sink sits at `Expr.value` → `Call.args`, and `ast.Call` has no entry in `CREDITED_FIELDS`, so the case is already caught by *position unknown* before the comparison runs. No credited sink can share the acquisition's statement index at all. **Run it anyway and record the empty result** — do not delete the `<=`, which stays as cheap defence, and do not pretend the mutation bites. Spec revision 8 should record this. |
| 2 | attribute a nested sink to `len(func.body)` instead of its container index | **Measured**: the same-statement-argument case, `lambda body`, and `For` — every case whose real index is `None` becomes a large int that sorts after the acquisition. |
| 5 | credit `FunctionDef.body`: `_walk_fields(node)` with no `skip`, passing `credited` through | **Measured**: the nested-`def`-body case falsely fences, collapsing §4.2's field distinction. |
| 3 | **Exactly this mutant**: `allowed = None` in place of the `CREDITED_FIELDS.get(...)` lookup, plus `child_credited = credited and (allowed is None or field in allowed)` — i.e. credit every field of every node. | **Measured**: `genexp`, `genexp-as-arg`, `type-alias`, `For`, **and `If.orelse`**. NOT the two lambda cases and NOT the discovery cases — the `Lambda`/`ClassDef` branch forces `False` and returns *before* `CREDITED_FIELDS` is read. <br>Two earlier drafts of this row were wrong in **opposite** directions (one said it flips the lambda cases, one said it spares `If.orelse`), and a review that flagged it also omitted `If.orelse`. The flip set depends on the mutant's exact form, which is why the form is spelled out above rather than described. |
| 4 | `child_credited = credited and type(node) in CREDITED_FIELDS` (ignore the field) | `test_an_unlisted_FIELD_of_a_listed_node_is_not_credited` |
| 6 | walk `Lambda.body` with `credited=True` instead of `False` | the two lambda-body negatives falsely fence |
| 6b | **skip a `Lambda`'s body via an EXPLICIT early return**: `if isinstance(node, ast.Lambda): yield node, credited; return`, placed before the `(Lambda, ClassDef)` branch | the two lambda-body negatives fail on the **holder** assertion — the sink vanishes. Discovery coverage, not safety. <br>**Merely removing `ast.Lambda` from the `(Lambda, ClassDef)` tuple does NOT work**: the node falls through to the generic traversal, which still descends into `body`, so the sink stays discoverable and the mutation flips **nothing**. Measured. |
| 9 | **make `credited` non-monotonic**: `child_credited = field in allowed` (drop the `credited and`) | **Measured**: `For`, `If.orelse`, **and `class body`** — any sink reached by a listed field sitting under an unlisted one, or under a definition branch that had already forced `False`. NOT the lambda cases, which an earlier draft named: the `Lambda` branch forces `False` **and returns**, so monotonicity never arises there, while `ClassDef` forces `False` and keeps walking — which is exactly why `class body` flips and the lambda ones do not. |
| 12 | remove ONE entry from the table, six times | The paths **compose**, so the flip sets differ per entry and must each be recorded: removing `ast.Expr` flips the `Expr`, `If`, `Try`, `With` and `AsyncWith` positives (all five reach their call through a statement node); removing `ast.Assign` flips only `test_assign_value_is_credited`; removing `ast.If`, `ast.Try`, `ast.With` or `ast.AsyncWith` flips only that entry's own positive. Run all six — the point is that every entry is load-bearing, and "one test per entry" was false. |

Record each mutation's actual output in the task report. A mutation that reddens nothing is a finding, not a pass.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check .
git add tests/fencing/git_fence.py tests/fencing/test_git_fence.py
git commit -m "test(fence): a six-entry allowlist decides a sink's position (F3e1a2 Task 1)"
```

---

### Task 2: The generator disqualifier

**Files:**
- Modify: `tests/fencing/git_fence.py`
- Test: `tests/fencing/test_git_fence.py`

**Interfaces:**
- Consumes: `_iter_credited` from Task 1.
- Produces: `_is_generator(func) -> bool`; a new reason string from `unprotected_sinks`.

- [ ] **Step 1: Write the failing tests**

```python
# ------------------------------------------------- F3e1a2 §4.3 — generators
# A generator's body does not begin until the caller calls next(), so even the
# ACQUISITION is deferred; and every yield hands control to a caller who may COMMIT
# or ROLLBACK, releasing the transaction-scoped mutex before the sink resumes.

def test_a_yield_between_acquisition_and_sink_is_not_fenced():
    _disqualified_but_held(_ACQ + "    yield 1\n    sha = write_and_commit_files(repo, {}, 'm')\n")


def test_a_yield_AFTER_the_sink_still_disqualifies():
    """§4.3 is a DISQUALIFIER, not a position rule. Deliberately over-broad: a
    correct generator costs a pin, and deciding which yields precede which sinks is
    the branch reasoning this design refuses to do."""
    _disqualified_but_held(_ACQ + "    sha = write_and_commit_files(repo, {}, 'm')\n    yield 1\n")


def test_yield_from_also_disqualifies():
    _disqualified_but_held(_ACQ + "    yield from [1]\n    sha = write_and_commit_files(repo, {}, 'm')\n")


def test_an_async_generator_is_not_fenced():
    _disqualified_but_held(
        _ACQ + "    yield 1\n    sha = write_and_commit_files(repo, {}, 'm')\n",
        head="async def f(s, w, stamp, repo, cond):\n",
    )


def test_a_yield_in_a_nested_defs_DEFAULT_disqualifies_the_ENCLOSING_function():
    """Verified with inspect.isgeneratorfunction: the yield belongs to f, not inner."""
    _disqualified_but_held(
        _ACQ
        + "    sha = write_and_commit_files(repo, {}, 'm')\n"
        + "    def inner(x=(yield 1)):\n        return x\n"
    )


def test_a_yield_in_a_LAMBDA_BODY_does_not_disqualify_the_outer():
    """A yield in a lambda body makes the LAMBDA a generator, not this function —
    verified: inspect.isgeneratorfunction(outer) is False. The yield scan must not
    reuse the sink walk, which deliberately descends into Lambda.body."""
    src = (
        _HEAD
        + _ACQ
        + "    cb = lambda: (yield 1)\n"
        + "    sha = write_and_commit_files(repo, {}, 'm')\n"
    )
    assert fenced_qualnames(src, "x/f.py") == {"f"}


def test_an_unrelated_nested_generator_does_not_disqualify_the_outer():
    """§5.3's scope-boundary positive. A naive yield scan fails this: _iter_own_nodes
    guards its CHILDREN but descends into a FunctionDef handed to it directly, so a
    scan written the obvious way marks a correctly fenced function unfenced."""
    src = (
        _HEAD
        + _ACQ
        + "    sha = write_and_commit_files(repo, {}, 'm')\n"
        + "    def inner():\n        yield 1\n    return inner\n"
    )
    assert fenced_qualnames(src, "x/f.py") == {"f"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/fencing/test_git_fence.py -q -p no:randomly -k "yield or generator"`
Expected: **5 failed, 4 passed.** The `-k` filter also selects Task 1's two
generator-EXPRESSION tests and the new lambda-body-yield positive, which all pass
already — a reminder that `-k` matches substrings, not concepts. The five generator cases fail (currently credited); `test_an_unrelated_nested_generator_does_not_disqualify_the_outer` passes already and is a regression pin.

- [ ] **Step 3: Implement**

Add to `git_fence.py`:

```python
def _iter_yield_scope(node: ast.AST):
    """Yield every node whose ``yield`` would belong to the ENCLOSING function.

    Deliberately NOT ``_iter_credited``. That walk descends into ``Lambda.body``,
    which is right for sink DISCOVERY (a lambda is anonymous, so nothing else would
    find a sink in it) and wrong here: a ``yield`` in a lambda body makes the LAMBDA a
    generator, not its enclosing function. Verified —
    ``inspect.isgeneratorfunction(outer)`` is **False** for::

        def outer():
            cb = lambda: (yield 1)
            return cb

    Lambda DEFAULTS still count: they evaluate in the enclosing scope. So this walk
    treats a ``lambda`` exactly like a nested ``def`` — everything but the body.
    """
    yield node
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        for child in _walk_fields(node, skip=("body",)):
            yield from _iter_yield_scope(child)
        return
    for child in _walk_fields(node):
        yield from _iter_yield_scope(child)


def _is_generator(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if *func* has an own-scope ``yield`` — its whole body is deferred.

    F3e1a2 §4.3. A ``yield`` in a nested def's or lambda's DEFAULT is seen (it belongs
    to this function); one in either BODY is not.
    """
    return any(
        isinstance(node, (ast.Yield, ast.YieldFrom))
        for stmt in func.body
        for node in _iter_yield_scope(stmt)
    )
```

In `unprotected_sinks`, immediately before the existing `if has_live:` block:

```python
        if _is_generator(func):
            reasons.append(
                f"{qualname} is a generator — its body does not run until the caller "
                f"iterates, and a yield returns control to a caller that may COMMIT "
                f"or ROLLBACK, releasing the transaction-scoped mutex"
            )
```

- [ ] **Step 4: Run and mutate**

Run: `uv run pytest tests/fencing/ -q -p no:randomly` → **180 passed**.

Mutation 7 (safety): make `_is_generator` `return False`. Expect **5 FAILED** — the five generator cases, FAILED not ERROR. Restore from the checkpoint, re-confirm green.

Mutation 7b (discovery): in `_is_generator`, iterate `ast.walk(stmt)` instead of the scope-aware walk. Expect **1 FAILED** — `test_an_unrelated_nested_generator_does_not_disqualify_the_outer`. This is the trap §4.2 names. Restore from the checkpoint.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check .
git add tests/fencing/git_fence.py tests/fencing/test_git_fence.py
git commit -m "test(fence): a yield can drop the mutex before the sink runs (F3e1a2 Task 2)"
```

---

### Task 3: The transaction-ending disqualifier

**Files:**
- Modify: `tests/fencing/git_fence.py`
- Test: `tests/fencing/test_git_fence.py`

**Interfaces:**
- Produces: `_TXN_ENDINGS: frozenset[str]`; `_ends_transaction(func) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# ------------------------------------- F3e1a2 §4.4 — visible transaction endings
# The commit mutex is TRANSACTION-SCOPED: acquire_commit_mutex says "Released
# automatically at COMMIT or ROLLBACK; there is no explicit release"
# (drafting/locking.py:195-203). So a commit between the two drops it.

@pytest.mark.parametrize("ending", ["commit", "rollback"])
def test_a_visible_transaction_ending_disqualifies(ending):
    _disqualified_but_held(
        _ACQ + f"    s.{ending}()\n    sha = write_and_commit_files(repo, {{}}, 'm')\n"
    )
```

Add `import pytest` to the test module's imports if absent (F3e1a's review flagged an
unused `import pytest` as F401 — this one is used, so it stays).

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/fencing/test_git_fence.py -q -p no:randomly -k transaction`
Expected: **2 failed** — both currently measure `{'f'}`, which is the defect.

- [ ] **Step 3: Implement**

```python
#: F3e1a2 §4.4. Method names that end the transaction and therefore release the
#: commit mutex. Matched by attribute name only, so an unrelated `.commit()` also
#: disqualifies — an UNDER-report, the safe direction, and not worth special-casing.
_TXN_ENDINGS = frozenset({"commit", "rollback"})


def _ends_transaction(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the function visibly ends its transaction.

    F3e1a2 §4.4, and READ §2 BEFORE CHANGING THIS. It is a ratchet over the cases
    that happen to be visible, NOT a guarantee: a helper that commits is invisible
    from here, and THIS INTRAPROCEDURAL checker cannot catch that. It does not license
    dropping the parent spec's §5.2 runtime two-ordered-lock proof.
    """
    for stmt in func.body:
        for node, _credited in _iter_credited(stmt, True):
            if isinstance(node, ast.Call) and _callee_name(node.func) in _TXN_ENDINGS:
                return True
    return False
```

Add the reason in `unprotected_sinks` beside the generator check:

```python
        if _ends_transaction(func):
            reasons.append(
                f"{qualname} visibly ends its transaction (commit/rollback), which "
                f"releases the transaction-scoped commit mutex before the sink runs"
            )
```

- [ ] **Step 4: Run and mutate**

Run: `uv run pytest tests/fencing/ -q -p no:randomly` → **182 passed**.

Mutation 8: make `_ends_transaction` `return False`. Expect **2 FAILED** (both parameters). Restore from the checkpoint.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check .
git add tests/fencing/git_fence.py tests/fencing/test_git_fence.py
git commit -m "test(fence): a visible commit releases the mutex before the sink (F3e1a2 Task 3)"
```

---

### Task 4: Prove the discovery cases are load-bearing

**Files:** none changed. Verification only — no new tests, no production edit.

Task 1 wrote these tests and its walk satisfies them. This task is separate because
revision 5 of the spec shipped a mutation with nothing to redden, and a discovery test
nobody has tried to break is a comment.

- [ ] **Step 1: Checkpoint**

```bash
cp tests/fencing/git_fence.py /tmp/git_fence.task4.py
```

- [ ] **Step 2: Mutation 10 — restore the pre-skip**

Put back, at the top of `sink_calls`'s statement loop:

```python
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
```

Run: `uv run pytest tests/fencing/test_git_fence.py -q -p no:randomly`

Expected: FAILED on the **holder** assertion for the nested-`def` default, decorator,
async default, annotation and class cases. The lambda cases are unaffected — a
`lambda` is not a statement, so the pre-skip never sees one.

Discovery coverage, not safety: it does **not** falsely fence anything. That is why it
sits here rather than among Task 1's safety mutations. Record the exact failing set.

Restore: `cp /tmp/git_fence.task4.py tests/fencing/git_fence.py`; re-run, green.

- [ ] **Step 3: Mutation 11 — stop walking a definition's non-body fields**

In `_iter_credited`, make both definition branches `return` immediately after
`yield node, credited` — walking the node but none of its fields.

Expected: FAILED on the holder assertion for **every** discovery case, the lambda ones
included. A different route to the same defect than mutation 10: that one loses whole
statements, this one loses fields within them.

Restore from the checkpoint; re-run, green.

- [ ] **Step 4: Record and commit**

No file changes. Record both mutations' real output in the task report. **If either
reddens nothing, that is a finding** — the discovery fix is not doing what Task 1
claims, and Task 1 needs revisiting before this plan continues.

```bash
git commit --allow-empty -m "test(fence): discovery mutations recorded, no code change (F3e1a2 Task 4)"
```

### Task 5: Say what the checker actually proves

**Files:**
- Modify: `tests/fencing/git_fence.py` (module docstring, `fenced_qualnames` docstring)
- Modify: `tests/fencing/live_mutex_gate.py` (module docstring)
- Modify: `tests/fencing/inventory.py` (`SLICE_STATUS`)

This is not a wording task. `live_mutex_gate.py` is the gate a later slice reads to decide it is done, and it currently promises "provably fenced" — which §2 shows is false.

- [ ] **Step 1: Correct `git_fence.py`'s module docstring**

Replace the opening paragraph and the three-clause rule with:

```
"""Fence F3e1a2 — the git-commit-fence predicate.

WHAT THIS PROVES, AND WHAT IT DOES NOT. It proves LEXICAL ORDER: the acquisition
call precedes the sink call in execution order, along a path made only of
constructs vetted to run with their statement. It does NOT prove the mutex is
held when the sink runs. The commit mutex is transaction-scoped, so a commit
between the two releases it, and a commit inside a called helper is invisible
here — this intraprocedural checker cannot catch that. See the 2026-08-23 F3e1a2 spec §2.

So BOTH readings are weaker than they look, and both matter:

* ``fenced: false`` does NOT mean broken. The checker under-reports by design;
  correct sites stay pinned.
* ``fenced: true`` does NOT mean safe. It means the cheap check found nothing
  wrong along a vetted path. A slice claims ``complete`` on this measurement
  PLUS the parent spec's §5.2 runtime two-ordered-lock proof — never on the
  measurement alone.

**The rule** (all clauses required):

1. ``acquire_commit_mutex_at_generation`` appears as a standalone expression
   statement directly in the function body (NOT inside ``if``/``try``/``with``).
2. Every sink has a credited position AFTER it, by statement index. A position is
   credited only along ``CREDITED_FIELDS`` — see that table's comment.
3. No ``acquire_commit_mutex_live`` anywhere (F3e1a).
4. No own-scope ``yield`` — the body is deferred (§4.3).
5. No visible ``commit``/``rollback`` — it drops the mutex (§4.4).
"""
```

- [ ] **Step 2: Correct the two "provably" claims**

`git_fence.py:366` (`fenced_qualnames`):

```python
    """Return the set of qualnames whose git-sink calls PASS THE LEXICAL CHECK.

    Not "provably fenced" — see this module's docstring. Passing means the cheap
    static check found nothing wrong; it is necessary for a slice to claim
    `complete`, and it is not sufficient.
    """
```

`live_mutex_gate.py:1`:

```python
"""Gate: every function holding a git sink must be pinned or pass the lexical check.

"Passes the lexical check" is deliberately weaker than "provably fenced", which is
what this docstring used to say. See git_fence.py's module docstring: the check
proves the acquisition precedes the sink, not that the mutex is still held.
"""
```

- [ ] **Step 3: Add the slice key**

In `tests/fencing/inventory.py`'s `SLICE_STATUS`, after the `"F3e1a"` line:

```python
    "F3e1a2": "complete",  # the allowlist walk — no row of its own
```

- [ ] **Step 4: Verify no "provably" survives, then run**

```bash
grep -rn "provably" tests/fencing/git_fence.py tests/fencing/live_mutex_gate.py || echo "clean"
uv run pytest tests/fencing/ -q -p no:randomly
uv run ruff check .
```

Expected: `clean`, then **182 passed**.

Scope the grep to the two files this task edits. A repo-wide
`grep -rn "provably" tests/fencing/` can **never** print `clean`: `sink_guard.py`
carries an unrelated and correct use of the word, so a whole-directory grep would make
this step permanently unpassable. No new tests — this task changes prose and one dict key.

- [ ] **Step 5: Commit**

```bash
git add tests/fencing/git_fence.py tests/fencing/live_mutex_gate.py tests/fencing/inventory.py
git commit -m "docs(fence): the checker proves lexical order, not a held lock (F3e1a2 Task 5)"
```

---

### Task 6: Confirm the slice moved nothing

**Files:**
- Create: `tests/fencing/test_f3e1a2_slice.py`

- [ ] **Step 1: Write the identity tests**

```python
"""F3e1a2 confirmation: the checker got smarter and the measurement did not move.

The slice's central claim is INERTNESS. `acquire_commit_mutex_at_generation` has zero
production callers, so every git row fails the positive rule before position is ever
consulted — whatever the allowlist does. If any assertion here fails, the slice
changed a measurement it promised not to touch.
"""

from __future__ import annotations

import ast
import pathlib

from tests.fencing.git_fence import _callee_name, fenced_qualnames, sink_calls
from tests.fencing.inventory import SLICE_STATUS, load_inventory

_PRIMITIVE_B = "acquire_commit_mutex_at_generation"

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_primitive_b_still_has_zero_production_callers():
    """The premise of the whole inertness argument. If this ever goes red, the slice
    is no longer inert and every other assertion here means something different."""
    hits = []
    for path in (_ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text())
        # Resolve aliases FIRST. `from ... import acquire_commit_mutex_at_generation
        # as acquire` then `acquire(...)` is a caller, and a bare-name check misses it
        # — the same alias handling F3e1a's own identity test already does.
        names = {_PRIMITIVE_B}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for a in node.names:
                    if a.name == _PRIMITIVE_B and a.asname:
                        names.add(a.asname)
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
                if node.value.id in names:
                    names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        for node in ast.walk(tree):
            # Its own `def` is not a caller. Excluding the whole of fencing.py would
            # hide a real caller added inside it, so exclude the DEFINITION, by AST.
            if isinstance(node, ast.FunctionDef) and node.name == _PRIMITIVE_B:
                continue
            if isinstance(node, ast.Call) and _callee_name(node.func) in names:
                hits.append(f"{path.relative_to(_ROOT)}:{node.lineno}")
    assert hits == [], f"Primitive B gained a caller: {hits}"


def test_the_real_tree_still_has_exactly_nine_sink_holders():
    """Two-sided: §4.2 WIDENS discovery, so this must not grow either."""
    # Identity is (path, qualname), NOT qualname alone: two files can both define
    # `_git` or `checkpoint_artifact`, and counting bare qualnames would collapse
    # them — hiding an ADDED site behind an existing name.
    holders = set()
    for path in (_ROOT / "src").rglob("*.py"):
        rel = str(path.relative_to(_ROOT))
        holders |= {(rel, q) for q, _n, _i in sink_calls(path.read_text(), rel)}
    assert len(holders) == 9, sorted(holders)


def test_no_real_site_measures_fenced():
    fenced = set()
    for path in (_ROOT / "src").rglob("*.py"):
        rel = str(path.relative_to(_ROOT))
        fenced |= fenced_qualnames(path.read_text(), rel)
    assert fenced == set(), f"a site measured fenced with zero Primitive B callers: {fenced}"


def test_every_git_row_is_still_unfenced_and_classified():
    rows = [r for r in load_inventory() if r["kind"] == "git_commit"]
    assert len(rows) == 9
    assert all(r["fenced"] is False for r in rows)
    assert all(SLICE_STATUS[r["slice"]] == "pending" for r in rows)


def test_the_allowlist_is_still_six_entries():
    """§4.1: the smallness IS the safety property. Growing this table needs a spec
    amendment and a real site that requires the entry — not a passing thought that a
    construct is eager."""
    from tests.fencing.git_fence import CREDITED_FIELDS

    assert {(k.__name__, tuple(sorted(v))) for k, v in CREDITED_FIELDS.items()} == {
        ("Assign", ("value",)),
        ("Expr", ("value",)),
        ("If", ("body",)),
        ("Try", ("body",)),
        ("With", ("body",)),
        ("AsyncWith", ("body",)),
    }


# NO GIT-DEPENDENT TESTS HERE. Two earlier drafts asserted `git diff` and
# `git show` against `merge-base master HEAD`. Both are wrong as permanent tests:
# once this merges, base == HEAD and they assert nothing; and CI's shallow
# checkout may have no local `master` ref at all, so they ERROR rather than fail.
# Those two checks are one-time PLAN commands against the fixed base `ea481e2c` —
# see Step 4 — not pytest tests.
```

- [ ] **Step 2: Run**

Run: `uv run pytest tests/fencing/test_f3e1a2_slice.py -q -p no:randomly`
Expected: **5 passed.**

- [ ] **Step 3: Prove the identity gates bite**

- Add `ast.For: frozenset({"body"})` to `CREDITED_FIELDS` → `test_the_allowlist_is_still_six_entries` FAILS, and `test_an_unlisted_NODE_type_is_not_credited` FAILS. Restore from the checkpoint.
- Hand-edit one git row's `fenced` to `true` in `fence_inventory.json` → `test_every_git_row_is_still_unfenced_and_classified` FAILS, and identity check 2 in Step 4 prints `FAIL`. Restore with `git checkout -- tests/fencing/fence_inventory.json` (safe here: that file is unmodified by this slice, so there is nothing of yours to lose).

- [ ] **Step 4: Run the fixed-base identity checks**

These are commands, not pytest tests — a test asserting `merge-base master HEAD` is
vacuous once this merges and ERRORs under CI's shallow checkout. Run them here, and
paste the real output into the task report:

```bash
BASE=ea481e2c

# 1. The slice converts nothing.
git diff --stat "$BASE"..HEAD -- src/ | tee /dev/stderr | grep -q . \
  && echo "FAIL: src/ was touched" || echo "OK: src/ untouched"

# 2. The inventory did not move — byte-identical, not merely equivalent.
git show "$BASE":tests/fencing/fence_inventory.json > /tmp/inv.base.json
diff -q /tmp/inv.base.json tests/fencing/fence_inventory.json \
  && echo "OK: inventory byte-identical" || echo "FAIL: the inventory moved"

# 3. Exemption pins are still 5.
git diff "$BASE"..HEAD -- tests/fencing/inventory.py | grep -E '^[+-].*"[a-z_]+\.py":' \
  && echo "FAIL: the pin list changed" || echo "OK: pins untouched"
```

All three must print `OK`.

- [ ] **Step 5: Full suite, lint, commit**

```bash
uv run pytest -n 16 --dist loadfile -q
uv run ruff check .
```

Expected: **`3891 passed, 1 skipped`** (3857 + 20 + 7 + 2 + 0 + 0 + 5 — **re-derive this from the actual per-task counts rather than trusting this arithmetic**; the plan's number is a prediction and every previous fence plan's prediction moved during the build. Record the real total and the term that changed).

**`1 skipped` is the signal.** `0 skipped` means a live billed Anthropic API call ran.

```bash
git add tests/fencing/test_f3e1a2_slice.py
git commit -m "test(fence): F3e1a2 widened the checker and moved no measurement (F3e1a2 Task 6)"
```

---

## Task-count reconciliation

| Task | New collected tests | Running total |
|---|---|---|
| baseline | — | 3857 |
| 1 — allowlist, walk, and every §5.3/§5.4 case | 20 | 3877 |
| 2 — generator disqualifier | 7 | 3884 |
| 3 — transaction disqualifier | 2 (one case, two params) | 3886 |
| 4 — discovery mutations | 0 (verification only) | 3886 |
| 5 — docstrings + slice key | 0 | 3886 |
| 6 — slice confirmation | 5 | 3891 |

**34 new collected tests**, counted from this plan's own code blocks rather than
predicted. The relationship to spec revision 7's "7 positives + 30 negatives = 37
cases" is not one-to-one, and the differences are all deliberate:

- **6 of §5.4's 7 order/acquisition negatives already exist** among F3e1a's 15 —
  `test_sink_before_acquisition_is_not_fenced`,
  `test_a_second_unprotected_sink_disqualifies_the_function`,
  `test_an_acquisition_nested_as_a_call_argument_is_not_fenced`,
  `test_an_acquisition_in_an_if_with_a_toplevel_sink_after_is_not_fenced`,
  `test_a_live_acquisition_disqualifies_even_with_a_correct_one`,
  `test_a_nested_def_does_not_inherit_its_parent_acquisition`. They are re-verified
  against the new walk, not rewritten. The seventh — `_live` inside a lambda — is new
  and lives in Task 1.
- **Two tests carry several cases each.** `test_a_sink_in_an_ANNOTATION_…` covers the
  argument and return annotation; `test_a_sink_in_a_nested_CLASS_…` loops over the four
  class shapes (body, base, keyword, decorator). **Six §5.4 cases, two collected
  tests** — this is where most of the 37-vs-34 gap lives.
- **The `commit`/`rollback` case is one entry but two collected tests.**
- `test_expr_value_is_credited` restates `test_acquisition_then_sink_is_fenced`
  deliberately: mutation 12 needs a positive that names the `Expr.value` entry.

**If your total differs, find the term that changed and write it down — never adjust a
number to make the chain close.** That rule caught two real defects in F3e1a's build,
and every count in an earlier draft of THIS plan was wrong at least once.

## What to do when the plan is wrong

It will be, somewhere. F3e1a's dry run found 18 defects in its plan and the real build found 2 more.

- **Record, work around, continue.** Do not stop at a defect.
- **A defect that would cross a slice-identity line is an ESCALATE, not a decision.** The lines: no `src/` change; the inventory does not move; pins stay at 5; the allowlist stays at six entries.
- **Never adjust an expected number to hide a discrepancy.** Name the term that changed.
- **Judge mutations on FAILED, never on ERROR** — a broken import shows as mass teardown ERRORs with zero FAILED, and reads like a passing mutation if you squint.
