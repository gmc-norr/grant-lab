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
