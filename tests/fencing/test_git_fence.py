"""Fence F3e1a — the one predicate the measurement and the gate both use.

CONSERVATIVE BY DESIGN: it under-reports. A shape it cannot prove fenced measures
unfenced, including shapes that are correct. That direction is the safe one — a
false `fenced: true` lets a slice claim a conversion it did not make, while a false
`false` costs a pin and a sentence.
"""

from __future__ import annotations

import pytest

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


def test_an_acquisition_in_an_if_with_a_toplevel_sink_after_is_not_fenced():
    """Pins the acquisition-POSITION clause independently of the sink-position clause
    (DEFECT D9). Acquire nested in an `if`, sink a top-level statement AFTER it: the
    faithful checker says unfenced (the acquisition is not a top-level statement), but a
    checker relaxed to 'accept an acquisition anywhere in the function' would over-report
    {'f'}. This is the ONLY test that flips under Step-5 Mutation 1 — the two branch /
    argument tests keep their set() verdict from the sink side alone, so without this
    fixture that mutation is undetected."""
    src = _HEAD + "    if cond:\n    " + _ACQ + _SINK
    assert fenced_qualnames(src, "x/f.py") == set()


def test_a_nested_defs_sink_does_not_make_the_outer_a_holder():
    """A sink inside a nested `def` belongs to that inner qualname ALONE (DEFECT D11).
    If `sink_calls` also credited the OUTER function as a holder (with stmt_index None),
    discovery would find a TENTH git site over the tree and Task 6's 'exactly nine' would
    break. The outer holds no sink of its own here, so it must appear in none of the three
    maps. The existing nested-def test checks only that the INNER def is not fenced, so it
    does not catch this leak."""
    src = _HEAD + "    def inner():\n    " + _SINK + "    return inner\n"
    holders = {q for q, _n, _i in sink_calls(src, "x/f.py")}
    assert "f" not in holders
    assert "f" not in unprotected_sinks(src, "x/f.py")
    assert "f" not in fenced_qualnames(src, "x/f.py")


def test_branch_nested_def_attributed_to_inner_not_outer():
    """A `def` nested inside an `if` block is a separate qualname — it is NOT a
    direct body statement of the outer function, but _collect_functions must still
    discover it and assign it the qualname `outer.inner`.

    Before the fix: _collect_functions only walked direct body statements, so `inner`
    was never discovered; sink_calls attributed the sink to `outer` (via ast.walk
    descending into the if-body and into the nested def).

    After the fix:
    - `outer` is NOT a sink holder (the sink belongs to `outer.inner` alone).
    - `outer.inner` IS a holder and IS reported by unprotected_sinks (it holds a sink
      with no acquisition).
    """
    src = (
        "def outer(repo):\n"
        "    if cond:\n"
        "        def inner():\n"
        "            write_and_commit_files(repo, {}, 'm')\n"
        "        return inner\n"
    )
    holders = {q for q, _n, _i in sink_calls(src, "x/f.py")}
    assert "outer" not in holders
    assert "outer" not in unprotected_sinks(src, "x/f.py")
    assert "outer" not in fenced_qualnames(src, "x/f.py")
    assert "outer.inner" in holders
    assert "outer.inner" in unprotected_sinks(src, "x/f.py")


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
    _disqualified_but_held(
        _ACQ + "    yield from [1]\n    sha = write_and_commit_files(repo, {}, 'm')\n"
    )


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


# ------------------------------------- F3e1a2 §4.4 — visible transaction endings
# The commit mutex is TRANSACTION-SCOPED: acquire_commit_mutex says "Released
# automatically at COMMIT or ROLLBACK; there is no explicit release"
# (drafting/locking.py:195-203). So a commit between the two drops it.

@pytest.mark.parametrize("ending", ["commit", "rollback"])
def test_a_visible_transaction_ending_disqualifies(ending):
    _disqualified_but_held(
        _ACQ + f"    s.{ending}()\n    sha = write_and_commit_files(repo, {{}}, 'm')\n"
    )


@pytest.mark.parametrize("ending", ["commit", "rollback"])
def test_a_transaction_ending_AFTER_the_sink_also_disqualifies(ending):
    """§4.4 says "in the function's own scope" — ANYWHERE, not "before the sink".

    The sibling test above only ever places the ending BETWEEN acquisition and
    sink, so an implementation that stopped scanning at the sink would pass it and
    still violate §4.4. `_ends_transaction` iterates the whole body precisely so
    position does not matter; this test is what makes that load-bearing.

    Conservative on purpose: an ending after the sink means the mutex probably WAS
    held for the write. §4.4 still fails closed, because deciding otherwise needs
    ordering reasoning this intraprocedural checker does not have.
    """
    _disqualified_but_held(
        _ACQ + f"    sha = write_and_commit_files(repo, {{}}, 'm')\n    s.{ending}()\n"
    )


# ---------------------------------------------------------------------------
# Unknown argv at a known git executor must not read as "not a commit".
#
# Until 2026-09-05 it did. An outside review added this to setup.py:
#
#     def _review_unfenced_commit(repo):
#         arguments = ["commit", "--allow-empty", "-m", "review-unfenced"]
#         return _run_git(arguments, cwd=repo)
#
# and the ENTIRE fencing suite passed — 239 passed — while calling it against a
# disposable repository really did move HEAD with no mutex and no session
# (`git_exit=0 head_changed=True fence=none`). Writing the same list inline
# reddened five tests across four files. One local variable was the difference.
#
# Reproduced here after the fix: the variable-argv mutant now reddens those same
# five tests. These cases pin the classifier directly so the whole-suite
# reproduction does not have to be redone to notice a regression.
# ---------------------------------------------------------------------------

import ast as _ast  # noqa: E402

import pytest  # noqa: E402

from tests.fencing.git_fence import _is_commit_call  # noqa: E402


def _call(source: str) -> _ast.Call:
    node = _ast.parse(source, mode="eval").body
    assert isinstance(node, _ast.Call)
    return node


@pytest.mark.parametrize(
    ("source", "expected", "why"),
    [
        ('_run_git(["commit", "-m", "x"], cwd=r)', True, "the literal commit case"),
        ('_run_git(["add", "-A"], cwd=r)', False, "a literal non-commit verb"),
        ('_run_git(["init", "-q", "-b", "main"], cwd=r)', False, "another literal"),
        ("_run_git(arguments, cwd=r)", True, "a NAME — the review's mutant"),
        ("_run_git(build_argv(), cwd=r)", True, "a call result"),
        ("_run_git(ARGV_BY_MODE[mode], cwd=r)", True, "a subscript"),
        ("_run_git([verb, '-m', msg], cwd=r)", True, "literal list, computed verb"),
        ("_run_git([*base, '-m', msg], cwd=r)", True, "a splat in first position"),
        ("_run_git()", True, "no arguments at all"),
    ],
)
def test_only_a_provably_non_commit_literal_is_treated_as_safe(source, expected, why):
    """False means "this call cannot commit". It must be earned, not assumed.

    What trivially passes this: a classifier returning True for everything. The
    three literal non-commit rows are what stop that — they are the reason
    `_run_git(["add", "-A"])` does not become a fenced sink and flood the
    inventory.
    """
    assert _is_commit_call("_run_git", _call(source)) is expected, why
