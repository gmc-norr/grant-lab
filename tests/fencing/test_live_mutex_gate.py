"""Tests for the live-mutex gate (Task 8, F3e1a).

Four tests:
- ``test_the_tree_has_no_unpinned_live_caller`` — the gate is green over the real tree.
- ``test_removing_a_pin_reddens_the_gate`` — the pin list is load-bearing.
- ``test_no_pin_is_stale`` — a fenced or deleted site must leave the pin list.
- ``test_the_direct_gate_catches_the_sink_holder_not_the_enclosing_caller`` — the
  direct gate reports the inner holder, not the outer function that takes ``_live``.
"""

from __future__ import annotations

from tests.fencing.live_mutex_gate import check_tree


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

    reported = g.all_unprotected()  # {(path, qualname)} over src/
    stale = sorted(p for p in g.UNCONVERTED_LIVE_CALLERS if p not in reported)
    assert not stale, f"pins for sites the checker no longer reports: {stale}"


def test_the_direct_gate_catches_the_sink_holder_not_the_enclosing_caller():
    """The contrast a naive `_live`-only rule gets wrong, driven through `check_sources`
    so it needs no `src/` edit. A synthetic two-function module: `inner()` directly holds
    the sink; `outer()` takes `_live` at the boundary, mutates, then delegates to `inner()`.
    The DIRECT gate reports `inner` (the sink holder) and NOT `outer` (an enclosing-
    acquisition site whose proof is a §5.2 runtime race, not this gate). A rule that only
    inspected `_live` on the function under test would be satisfied by `outer` and miss
    `inner` entirely."""
    from tests.fencing.live_mutex_gate import check_sources

    source = (
        "from call_manager.proposal_assist.citations.gitio import write_and_commit_files\n"
        "def inner(repo, files, msg):\n"
        "    return write_and_commit_files(repo, files, msg)\n"
        "def outer(session, wid, repo):\n"
        "    acquire_commit_mutex_live(session, wid)\n"
        "    return inner(repo, {}, 'm')\n"
    )
    problems = check_sources({"call_manager/_synthetic/two_function.py": source})
    reported = {p.split("::")[1].split(" ")[0] for p in problems}
    assert "inner" in reported, problems
    assert "outer" not in reported, problems


def test_the_live_acquirers_partition_exactly():
    """Every `acquire_commit_mutex_live` caller in `src/` is in exactly one record.

    The three records replace one frozenset that held two categories under a name
    claiming one — which is how a reader concluded spec §4.2's "seven" was wrong. It is
    not: §4.2 is scoped to F3e1b, whose seven enclosing sites all reach
    `checkpoint_artifact`. The old record additionally held six direct sink holders and
    one F5 site, so it summed to fourteen against a different denominator.

    A TOTAL partition is what makes these records honest. The old one had zero readers,
    so nothing noticed that its own comment said "thirteen" while it held fourteen. Under
    this test a NEW `_live` caller belongs to no record and fails here by name, and a
    deleted one fails as a stale entry.
    """
    from tests.fencing.live_mutex_gate import (
        ENCLOSING_ACQUISITIONS,
        LIVE_ACQUIRERS_WITHOUT_A_SINK,
        LIVE_ACQUIRING_SINK_HOLDERS,
        live_acquirer_qualnames,
    )

    recorded = (
        set(ENCLOSING_ACQUISITIONS) | LIVE_ACQUIRING_SINK_HOLDERS | LIVE_ACQUIRERS_WITHOUT_A_SINK
    )
    actual = live_acquirer_qualnames()

    assert actual - recorded == set(), f"unrecorded `_live` caller(s): {sorted(actual - recorded)}"
    assert recorded - actual == set(), f"stale record entr(ies): {sorted(recorded - actual)}"

    # Disjoint, not merely covering: an entry in two records would let a category error
    # hide, which is the exact defect this replaces.
    pairs = [
        (set(ENCLOSING_ACQUISITIONS), LIVE_ACQUIRING_SINK_HOLDERS),
        (set(ENCLOSING_ACQUISITIONS), LIVE_ACQUIRERS_WITHOUT_A_SINK),
        (LIVE_ACQUIRING_SINK_HOLDERS, LIVE_ACQUIRERS_WITHOUT_A_SINK),
    ]
    for left, right in pairs:
        assert left & right == set(), f"a qualname is in two records: {sorted(left & right)}"


def test_f3e1b_owns_exactly_the_seven_enclosing_acquisitions_of_spec_4_2():
    """Spec §4.2's table, as a gate. Its seven rows all reach `checkpoint_artifact`.

    `Phase0Worker._m1` is the eighth enclosing acquisition in the tree and is NOT one of
    them: it reaches `_init_repo`, whose inventory row is F5. Scheduling it into F3e1b
    would make F3e1b's completion criterion (spec §13 crit-9) unsatisfiable, because F5's
    sink is not F3e1b's to convert.
    """
    from tests.fencing.live_mutex_gate import ENCLOSING_ACQUISITIONS

    assert {q for q, s in ENCLOSING_ACQUISITIONS.items() if s == "F3e1b"} == {
        "apply_citation_to_room",
        "kernel_accept_prose",
        "kernel_init_spec",
        "kernel_accept_citation",
        "apply_annotation_insert_to_room",
        "post_lock_spec",
        "post_unlock_spec",
    }
    assert ENCLOSING_ACQUISITIONS["Phase0Worker._m1"] == "F5"
