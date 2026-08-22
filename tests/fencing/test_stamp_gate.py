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
