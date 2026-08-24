"""Fence F3e1a — the mechanism-2 generation must come from a stamp.

Two layers, and BOTH are load-bearing:

* **Fixtures** drive `check_source` against synthetic sources, pinning the shapes
  the checker must reject — inline late read, bare local read, literal, positional
  — whether or not the tree happens to contain one today.
* **A scan over `src/`** ARMS that checker against production. Through F3e1a2 there
  was nothing to arm it against: `acquire_commit_mutex_at_generation` had zero
  production callers, so a tree scan was vacuously green and this file said so.
  The 2026-08-24 endgame ended that premise on its first conversion task, and the
  scan is the only thing standing between a real conversion and a cosmetic one.
  Measured, not theorised: with the fixtures alone, rewriting a converted site to
  `generation=session.get(Workspace, wid).lifecycle_generation` left the whole
  fencing suite green while `check_source` on that same file returned the
  violation. `test_the_tree_scan_is_not_vacuous` keeps the scan from quietly
  reverting to that state.
"""

from __future__ import annotations

import ast
import pathlib

from tests.fencing.stamp_gate import MECHANISM_2_ACQUIRE, check_source

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"


def _check(src: str) -> list[str]:
    return check_source(src, path="svc/example.py")


def _production_sources() -> list[tuple[str, str]]:
    """Every production module as (repo-relative path, source text)."""
    return [
        (str(p.relative_to(_ROOT)), p.read_text(encoding="utf-8"))
        for p in sorted(_SRC.rglob("*.py"))
        if "__pycache__" not in p.parts
    ]


def _primitive_b_calls(source: str) -> int:
    """Call NODES, not text mentions — `fencing.py` defines the primitive and
    names it in prose, and neither is a call."""
    n = 0
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        if name == MECHANISM_2_ACQUIRE:
            n += 1
    return n


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


# --------------------------------------------------------------------------
# The armed gate: the checker above, run over the real tree.
# --------------------------------------------------------------------------


def test_no_production_site_reads_its_generation_at_the_mutex():
    """THE gate this whole slice turns on, over `src/` rather than a fixture.

    Every fixture above proves the checker can catch a violation. Only this
    proves the tree contains none. Without it a conversion could take
    Primitive B and still read `lifecycle_generation` at the mutex — which is
    the pre-conversion behaviour wearing the new primitive's name, and which
    passed every other test in tests/fencing/ when it was tried.
    """
    problems: list[str] = []
    for rel, source in _production_sources():
        problems += check_source(source, rel)
    assert problems == [], (
        "a production site passes a generation that did not come from a "
        "FenceStamp:\n  " + "\n  ".join(problems)
    )


def test_the_tree_scan_is_not_vacuous():
    """A scan of zero call sites returns [] and proves nothing.

    That is exactly the state this file documented until the endgame's first
    conversion, and it is one `git revert` away from returning. Asserting only
    a lower bound is deliberate: the endgame converts sites task by task, so a
    fixed count would have to be edited by every task and would stop being a
    measurement of anything.
    """
    found = {
        rel: n for rel, source in _production_sources() if (n := _primitive_b_calls(source))
    }
    assert found, (
        f"no production site calls {MECHANISM_2_ACQUIRE} — the scan in "
        "test_no_production_site_reads_its_generation_at_the_mutex is vacuous "
        "and is proving nothing"
    )
