# Fence F3e1a2 — the allowlist walk — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teach `tests/fencing/git_fence.py` to credit a sink's position through an explicit six-entry allowlist, so F3e1b's four sites can reach `fenced: true` once converted — while measuring nothing differently today.

**Architecture:** Discovery is broadened (every sink stays visible, including ones in nested defaults nobody can credit); crediting is narrowed to an allowlist of `(node type, field)` pairs, with anything unrecognised failing closed. Two disqualifiers are added — an own-scope `yield` and a visible transaction ending — because both can drop the transaction-scoped mutex before the sink runs.

**Tech Stack:** Python 3.12 stdlib `ast` only. `pytest`. No `src/` change, no new dependency.

**Spec:** `docs/superpowers/specs/2026-08-23-fence-f3e1a2-containing-statement-order-design.md` (revision 6, commit `7841e64f`). Section numbers below refer to it.

## Global Constraints

- **Baseline is `3857 passed, 1 skipped`** on master at `7841e64f`. The `1 skipped` is the `ANTHROPIC_API_KEY`-gated real-SDK test. **`0 skipped` means a live billed API call ran** — do not export that key.
- **Fast suite:** `uv run pytest -n 16 --dist loadfile -q`. Never `-n auto` (resolves to 72 here). Never `--dist loadgroup`.
- **`ruff check .` is THE lint gate.** `ruff format` is not. Line limit 100. Rules: `E,F,I,B,UP,SIM`.
- **`tests/fencing` is NOT insulated from the app import chain.** The root `tests/conftest.py` autouse teardown imports the app for every test in the repo, so a broken import shows as mass teardown **ERRORS with zero FAILED**. **Judge every mutation on FAILED, never on ERROR.**
- **NO `src/` change in this slice.** `git diff master..HEAD --stat -- src/` must be empty at every commit. This is a slice-identity line: if a task seems to need one, STOP and escalate.
- **The inventory must not move.** `fence_inventory.json` byte-identical, all nine `git_commit` rows still `fenced: false`, exemption pins still 5.
- **Never widen §4.1's allowlist.** Six entries, exactly what the nine production paths need. Eagerness is NOT a reason to add one (§4.1). If a task seems to need a seventh, STOP and escalate.
- **Every uncredited and disqualified test case asserts the qualname is STILL a sink holder.** An invisible sink is worse than an unfenced one (§3).

---

## File Structure

| File | Change |
|---|---|
| `tests/fencing/git_fence.py` | Modify. New allowlist table, new field-aware walk, `sink_calls` rewritten, two disqualifiers, docstring corrections. |
| `tests/fencing/test_git_fence.py` | Modify. 15 existing tests keep their verdict; 7 positives + 24 negatives added. |
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

def _unfenced_but_held(body: str, head: str = _HEAD) -> None:
    src = head + body
    assert fenced_qualnames(src, "x/f.py") == set(), "must not be credited"
    holders = {q for q, _n, _i in sink_calls(src, "x/f.py")}
    assert holders == {"f"}, f"sink must remain discoverable, got {holders}"
    indices = [i for _q, _n, i in sink_calls(src, "x/f.py")]
    assert indices == [None], f"position must be unprovable, got {indices}"


def test_lambda_body_is_not_credited():
    _unfenced_but_held(_ACQ + "    return lambda: write_and_commit_files(repo, {}, 'm')\n")


def test_generator_expression_is_not_credited():
    _unfenced_but_held(
        _ACQ + "    return (write_and_commit_files(repo, {}, 'm') for i in [1])\n"
    )


def test_lambda_nested_in_a_returned_container_is_not_credited():
    _unfenced_but_held(
        _ACQ + "    return {'go': lambda: write_and_commit_files(repo, {}, 'm')}\n"
    )


def test_generator_expression_as_a_call_argument_is_not_credited():
    _unfenced_but_held(
        _ACQ + "    register((write_and_commit_files(repo, {}, 'm') for i in [1]))\n"
    )


def test_a_pep695_type_alias_is_not_credited():
    """PEP 695: the RHS is lazy until `__value__` is read, and the alias can escape."""
    _unfenced_but_held(_ACQ + "    type Deferred = write_and_commit_files(repo, {}, 'm')\n")


def test_an_unlisted_NODE_type_is_not_credited():
    """ast.For is not in CREDITED_FIELDS at all. The default must be no-credit."""
    _unfenced_but_held(_ACQ + "    for i in [1]:\n    " + _SINK)


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


def test_an_unlisted_FIELD_of_a_listed_node_is_not_credited():
    """`ast.If` IS listed, but only its `body`. `orelse` is not.

    The unknown-NODE test above does not prove this: an implementation that
    allowlisted by node type alone would pass that one and fail here.
    """
    _unfenced_but_held(_ACQ + "    if cond:\n        pass\n    else:\n    " + _SINK)
```

- [ ] **Step 2: Run them to see which fail and why**

Run: `uv run pytest tests/fencing/test_git_fence.py -q -p no:randomly`

Expected: **9 failed, 22 passed.** The failures are the four credited-container positives (`if`/`try`/`with`/`async with`), the `Assign` positive, and the four holder-index assertions that currently return `None` for the right reason but the wrong one. `test_expr_value_is_credited` passes already (F3e1a's shape). The `for`, `orelse`, lambda, genexp and type-alias negatives pass already — today *everything* nested is `None`.

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

#: Scopes whose BODY belongs to a different qualname and is never walked from here,
#: but whose decorators and defaults execute in the ENCLOSING scope and are.
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
```

- [ ] **Step 4: Add the walk and rewrite `sink_calls`'s descent**

Add after `_iter_own_nodes` (leave that function and `_collect_functions` untouched — §4.2):

```python
def _iter_credited(node: ast.AST, credited: bool):
    """Yield ``(node, credited)`` for everything reachable in the ENCLOSING scope.

    F3e1a2 §4.2. Three properties, each load-bearing:

    * **Monotonic.** ``credited`` starts True at a top-level statement and can only
      go False. A listed field beneath an unlisted one stays uncredited — otherwise
      a sink at ``Lambda.body -> Assign.value`` would be credited.
    * **Discovery does not stop at False.** The walk keeps descending, so a sink in a
      lambda, a generator expression or a type alias is still FOUND; only its
      position is lost. A site missing from the inventory is worse than one measuring
      ``false``.
    * **Nested scopes are field-aware, and def/lambda differ.** Decorators and
      defaults run in the enclosing scope and are walked (uncredited). A nested
      ``def``'s body is NOT walked — it has its own qualname. A ``lambda``'s body IS
      walked, uncredited — a lambda is anonymous, so nothing else would ever find a
      sink in it.

    Unlike ``_iter_own_nodes`` this handles a nested scope as its ENTRY node, not
    only as a child — ``sink_calls`` no longer pre-skips them.
    """
    yield node, credited

    if isinstance(node, _NESTED_SCOPES):
        enclosing: list[ast.AST] = list(getattr(node, "decorator_list", []))
        args = getattr(node, "args", None)
        if args is not None:
            enclosing.extend(d for d in args.defaults if d is not None)
            enclosing.extend(d for d in args.kw_defaults if d is not None)
        if isinstance(node, ast.Lambda):
            # A LAMBDA'S BODY IS WALKED; A DEF'S IS NOT. This asymmetry is not an
            # oversight — read before "simplifying" it. A nested `def` has its own
            # qualname, so `_collect_functions` analyses its body on a separate pass
            # and skipping it here loses nothing. A lambda is ANONYMOUS: it gets no
            # qualname, nothing else ever visits it, and skipping its body makes a
            # sink inside it VANISH from the inventory entirely. That is the
            # invisible-sink defect this slice exists to close.
            enclosing.append(node.body)
        for child in enclosing:
            yield from _iter_credited(child, False)
        return

    if isinstance(node, ast.ClassDef):
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

> **Prototyped before this plan was committed.** The code above was run against
> `git_fence.py` at `7841e64f` and reverted. All 6 positives and all 11 uncredited /
> discovery cases behave as specified, `153 passed`, real tree still nine holders and
> an empty fenced set. **The first version of this code lost lambda-body sinks
> entirely** — caught by running it, not by reading it, which is why the asymmetry
> above carries a comment rather than a shrug. Treat this as evidence the code works,
> not that it is correct: lock the tests first, per spec §5.7.

- [ ] **Step 5: Run the tests, then prove the new logic by breaking it**

Run: `uv run pytest tests/fencing/test_git_fence.py -q -p no:randomly`
Expected: **29 passed** (15 existing + 6 positives + 8 negatives).

Then run the full fencing package: `uv run pytest tests/fencing/ -q -p no:randomly`
Expected: **167 passed** (153 + 14).

Now the safety mutations. For each: apply, run, confirm **FAILED** (not ERROR) for the named tests, restore with `git checkout -- tests/fencing/git_fence.py`, re-confirm green.

| # | Mutation | Must redden |
|---|---|---|
| 3 | `CREDITED_FIELDS.get(type(node), frozenset())` → a sentinel meaning "all fields" | all 7 uncredited negatives (not the `_live`-in-lambda one — that is a disqualifier, unaffected by crediting) |
| 4 | `child_credited = credited and type(node) in CREDITED_FIELDS` (ignore the field) | `test_an_unlisted_FIELD_of_a_listed_node_is_not_credited` |
| 6 | walk `Lambda.body` with `credited=True` instead of `False` | the two lambda-body negatives falsely fence |
| 6b | **skip `Lambda.body` entirely** (drop the `isinstance(node, ast.Lambda)` branch) | the two lambda-body negatives fail on the **holder** assertion — the sink vanishes. Discovery coverage, not safety: it under-reports. Found by prototyping this exact code; the plan shipped it broken. |
| 9 | `child_credited = field in allowed` (drop `credited and` — non-monotonic) | `test_lambda_nested_in_a_returned_container_is_not_credited` |
| 12a | remove `ast.Expr` from the table | all 5 container positives — `Expr`, `If`, `Try`, `With`, `AsyncWith`, because each reaches its call through a statement node. NOT the `Assign` positive. |
| 12b | remove `ast.Assign` from the table | only `test_assign_value_is_credited` |

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
    _unfenced_but_held(_ACQ + "    yield 1\n    sha = write_and_commit_files(repo, {}, 'm')\n")


def test_a_yield_AFTER_the_sink_still_disqualifies():
    """§4.3 is a DISQUALIFIER, not a position rule. Deliberately over-broad: a
    correct generator costs a pin, and deciding which yields precede which sinks is
    the branch reasoning this design refuses to do."""
    _unfenced_but_held(_ACQ + "    sha = write_and_commit_files(repo, {}, 'm')\n    yield 1\n")


def test_yield_from_also_disqualifies():
    _unfenced_but_held(_ACQ + "    yield from [1]\n    sha = write_and_commit_files(repo, {}, 'm')\n")


def test_an_async_generator_is_not_fenced():
    _unfenced_but_held(
        _ACQ + "    yield 1\n    sha = write_and_commit_files(repo, {}, 'm')\n",
        head="async def f(s, w, stamp, repo, cond):\n",
    )


def test_a_yield_in_a_nested_defs_DEFAULT_disqualifies_the_ENCLOSING_function():
    """Verified with inspect.isgeneratorfunction: the yield belongs to f, not inner."""
    _unfenced_but_held(
        _ACQ
        + "    sha = write_and_commit_files(repo, {}, 'm')\n"
        + "    def inner(x=(yield 1)):\n        return x\n"
    )


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
Expected: **5 failed, 1 passed.** The five generator cases fail (currently credited); `test_an_unrelated_nested_generator_does_not_disqualify_the_outer` passes already and is a regression pin.

- [ ] **Step 3: Implement**

Add to `git_fence.py`:

```python
def _is_generator(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if *func* has an own-scope ``yield`` — its whole body is deferred.

    F3e1a2 §4.3. Uses ``_iter_credited`` for the scope boundary, so a ``yield`` in a
    nested def's DEFAULT is seen (it belongs to this function — verified with
    ``inspect.isgeneratorfunction``) while a ``yield`` in that def's BODY is not.
    The ``credited`` flag is irrelevant here and discarded: deferred execution is not
    an excuse for a disqualifier.
    """
    for stmt in func.body:
        for node, _credited in _iter_credited(stmt, True):
            if isinstance(node, (ast.Yield, ast.YieldFrom)):
                return True
    return False
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

Run: `uv run pytest tests/fencing/ -q -p no:randomly` → **173 passed**.

Mutation 7 (safety): make `_is_generator` `return False`. Expect **5 FAILED** — the five generator cases, FAILED not ERROR. Restore, re-confirm green.

Mutation 9 (discovery): in `_is_generator`, iterate `ast.walk(stmt)` instead of `_iter_credited`. Expect **1 FAILED** — `test_an_unrelated_nested_generator_does_not_disqualify_the_outer`. This is the trap §4.2 names. Restore.

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
    _unfenced_but_held(
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
    from here, and no static checker can ever catch that. It does not license
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

Run: `uv run pytest tests/fencing/ -q -p no:randomly` → **175 passed**.

Mutation 8: make `_ends_transaction` `return False`. Expect **2 FAILED** (both parameters). Restore.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check .
git add tests/fencing/git_fence.py tests/fencing/test_git_fence.py
git commit -m "test(fence): a visible commit releases the mutex before the sink (F3e1a2 Task 3)"
```

---

### Task 4: The discovery cases

**Files:**
- Test: `tests/fencing/test_git_fence.py`

**Interfaces:** consumes Task 1's walk. **No production change is expected** — Task 1 already removed the pre-skip. This task proves it, and exists separately because revision 5 shipped a mutation with nothing to redden.

- [ ] **Step 1: Write the tests**

```python
# ----------------------------------------------- F3e1a2 §5.4 — DISCOVERY group
# These four are the cases that drive the pre-existing invisible-sink defect. All
# four are EAGER in the enclosing scope (verified) and NONE is credited, because
# defaults and decorators are not on §4.1's table — eager is not a reason to credit.
# Each asserts the sink is still FOUND. Without this group the defect survives every
# other test in this file.

def test_a_sink_in_a_nested_defs_DEFAULT_is_found_but_not_credited():
    _unfenced_but_held(
        _ACQ + "    def inner(x=write_and_commit_files(repo, {}, 'm')):\n        return x\n"
    )


def test_a_sink_in_a_nested_defs_DECORATOR_is_found_but_not_credited():
    _unfenced_but_held(
        _ACQ
        + "    @register(write_and_commit_files(repo, {}, 'm'))\n"
        + "    def inner():\n        pass\n"
    )


def test_a_sink_in_a_LAMBDAS_default_is_found_but_not_credited():
    _unfenced_but_held(
        _ACQ + "    cb = lambda x=write_and_commit_files(repo, {}, 'm'): x\n"
    )


def test_a_sink_in_a_nested_ASYNC_defs_default_is_found_but_not_credited():
    _unfenced_but_held(
        _ACQ
        + "    async def inner(x=write_and_commit_files(repo, {}, 'm')):\n        return x\n"
    )
```

- [ ] **Step 2: Run**

Run: `uv run pytest tests/fencing/test_git_fence.py -q -p no:randomly -k "DEFAULT or DECORATOR or LAMBDAS or ASYNC_defs"`
Expected: **4 passed.** If any fails on the *holder* assertion, Task 1's walk did not remove the pre-skip correctly — fix Task 1, do not weaken the assertion.

- [ ] **Step 3: Prove they are load-bearing**

Mutation 10 — **restore** the pre-skip at the top of `sink_calls`'s statement loop:

```python
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
```

Expect **3 FAILED** on the *holder* assertion (`sink must remain discoverable`) — the two nested-`def` cases and the async one. The lambda-default case is unaffected, since a `lambda` is not a statement. This is discovery coverage, not a safety mutation: it does **not** falsely fence anything. Restore.

Mutation 11 — in `_iter_credited`, drop `decorator_list` / defaults from the nested-scope branch (`enclosing = []`). Expect **4 FAILED** on the holder assertion. Restore.

- [ ] **Step 4: Run the whole package, lint, commit**

Run: `uv run pytest tests/fencing/ -q -p no:randomly` → **179 passed**.

```bash
uv run ruff check .
git add tests/fencing/test_git_fence.py
git commit -m "test(fence): a sink in a nested default must still be visible (F3e1a2 Task 4)"
```

---

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
here — no static checker can catch that. See the 2026-08-23 F3e1a2 spec §2.

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
grep -rn "provably" tests/fencing/ || echo "clean"
uv run pytest tests/fencing/ -q -p no:randomly
uv run ruff check .
```

Expected: `clean`, then **179 passed**. No new tests — this task changes prose and one dict key.

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
import json
import pathlib
import subprocess

from tests.fencing.git_fence import fenced_qualnames, sink_calls
from tests.fencing.inventory import SLICE_STATUS, load_inventory

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_primitive_b_still_has_zero_production_callers():
    """The premise of the whole inertness argument. If this ever goes red, the slice
    is no longer inert and every other assertion here means something different."""
    hits = []
    for path in (_ROOT / "src").rglob("*.py"):
        if path.name == "fencing.py" and path.parent.name == "proposal_assist":
            continue
        if "acquire_commit_mutex_at_generation" in path.read_text():
            hits.append(str(path.relative_to(_ROOT)))
    assert hits == [], f"Primitive B gained a caller: {hits}"


def test_the_real_tree_still_has_exactly_nine_sink_holders():
    """Two-sided: §4.2 WIDENS discovery, so this must not grow either."""
    holders = set()
    for path in (_ROOT / "src").rglob("*.py"):
        rel = str(path.relative_to(_ROOT))
        holders |= {q for q, _n, _i in sink_calls(path.read_text(), rel)}
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


def test_the_slice_touched_no_production_code():
    base = subprocess.run(
        ["git", "merge-base", "master", "HEAD"],
        cwd=_ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()
    changed = subprocess.run(
        ["git", "diff", "--name-only", f"{base}..HEAD", "--", "src/"],
        cwd=_ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert changed == "", f"F3e1a2 converts nothing but touched: {changed}"


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


def test_the_inventory_file_is_byte_identical_to_master():
    base = subprocess.run(
        ["git", "merge-base", "master", "HEAD"],
        cwd=_ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()
    before = subprocess.run(
        ["git", "show", f"{base}:tests/fencing/fence_inventory.json"],
        cwd=_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    now = (_ROOT / "tests/fencing/fence_inventory.json").read_text()
    assert json.loads(before) == json.loads(now), "the inventory moved"
```

- [ ] **Step 2: Run**

Run: `uv run pytest tests/fencing/test_f3e1a2_slice.py -q -p no:randomly`
Expected: **7 passed.**

- [ ] **Step 3: Prove the identity gates bite**

- Add `ast.For: frozenset({"body"})` to `CREDITED_FIELDS` → `test_the_allowlist_is_still_six_entries` FAILS, and `test_an_unlisted_NODE_type_is_not_credited` FAILS. Restore.
- Hand-edit one git row's `fenced` to `true` in `fence_inventory.json` → `test_every_git_row_is_still_unfenced_and_classified` and `test_the_inventory_file_is_byte_identical_to_master` FAIL. Restore with `git checkout --`.

- [ ] **Step 4: Full suite, lint, commit**

```bash
uv run pytest -n 16 --dist loadfile -q
uv run ruff check .
```

Expected: **`3890 passed, 1 skipped`** (3857 + 14 + 6 + 2 + 4 + 0 + 7 — **re-derive this from the actual per-task counts rather than trusting this arithmetic**; the plan's number is a prediction and every previous fence plan's prediction moved during the build. Record the real total and the term that changed).

**`1 skipped` is the signal.** `0 skipped` means a live billed Anthropic API call ran.

```bash
git add tests/fencing/test_f3e1a2_slice.py
git commit -m "test(fence): F3e1a2 widened the checker and moved no measurement (F3e1a2 Task 6)"
```

---

## Task-count reconciliation

| Task | New tests | Running total |
|---|---|---|
| baseline | — | 3857 |
| 1 — allowlist + walk | 14 | 3871 |
| 2 — generator disqualifier | 6 | 3877 |
| 3 — transaction disqualifier | 2 | 3879 |
| 4 — discovery cases | 4 | 3883 |
| 5 — docstrings + slice key | 0 | 3883 |
| 6 — slice confirmation | 7 | 3890 |

Spec §5.3 promises 7 positives and §5.4 promises 24 negatives = **31 cases**, but only **26 new test functions** appear in Tasks 1–4. The gap is deliberate and must be checked, not assumed:

- **6 of §5.4's 7 order/acquisition negatives already exist** among F3e1a's 15 — `test_sink_before_acquisition_is_not_fenced`, `test_a_second_unprotected_sink_disqualifies_the_function`, `test_an_acquisition_nested_as_a_call_argument_is_not_fenced`, `test_an_acquisition_in_an_if_with_a_toplevel_sink_after_is_not_fenced`, `test_a_live_acquisition_disqualifies_even_with_a_correct_one`, `test_a_nested_def_does_not_inherit_its_parent_acquisition`. They are re-verified against the new walk, not rewritten. The seventh, `_live` inside a lambda, is new and is in Task 1.
- The `commit`/`rollback` case is one §5.4 entry but two collected tests.
- `test_expr_value_is_credited` restates `test_acquisition_then_sink_is_fenced` on purpose: mutation 12a needs a positive that names the `Expr.value` entry explicitly.

26 + 6 existing + 1 double-counted parameterisation reconciles to the 31. **If your total differs, find the term that changed and write it down — do not adjust a number to make the chain close.** That rule caught two real defects in F3e1a's build.

## What to do when the plan is wrong

It will be, somewhere. F3e1a's dry run found 18 defects in its plan and the real build found 2 more.

- **Record, work around, continue.** Do not stop at a defect.
- **A defect that would cross a slice-identity line is an ESCALATE, not a decision.** The lines: no `src/` change; the inventory does not move; pins stay at 5; the allowlist stays at six entries.
- **Never adjust an expected number to hide a discrepancy.** Name the term that changed.
- **Judge mutations on FAILED, never on ERROR** — a broken import shows as mass teardown ERRORs with zero FAILED, and reads like a passing mutation if you squint.
