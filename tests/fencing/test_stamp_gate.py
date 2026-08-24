"""Fence F3e1a — the mechanism-2 generation must come from a stamp.

Two layers, and BOTH are load-bearing:

* **Fixtures** drive `check_source` against synthetic sources, pinning the shapes
  the checker must reject — inline late read, bare local read, literal, positional,
  and a late read laundered through `stamp_for_workspace` — whether or not the tree
  happens to contain one today.
* **A scan over `src/`** ARMS that checker against production. Through F3e1a2 there
  was nothing to arm it against: `acquire_commit_mutex_at_generation` had zero
  production callers, so a tree scan was vacuously green and this file said so.
  The 2026-08-24 endgame ended that premise on its first conversion task, and the
  scan is the only thing standing between a real conversion and a cosmetic one.
  Measured, not theorised: with the fixtures alone, rewriting a converted site to
  `generation=session.get(Workspace, wid).lifecycle_generation` left the whole
  fencing suite green while `check_source` on that same file returned the
  violation.

The scan is guarded TWICE, because a scan can be defeated at either end.
`test_the_tree_scan_is_not_vacuous` keeps it from reverting to zero call sites, and
`test_the_tree_scan_reaches_every_site_the_inventory_says_is_fenced` keeps it from
being narrowed to a subset — measured: with only the first guard, narrowing `_SRC`
to one package while planting a late read outside it left this file at 9 passed.
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


def test_a_stamp_minted_from_a_bound_name_is_accepted():
    """The shape both production sites use: the object the access check loaded,
    bound to a name well before the mutex could block."""
    src = (
        "def f(session, wid, workspace):\n"
        "    stamp = stamp_for_workspace(workspace)\n"
        "    acquire_commit_mutex_at_generation(session, wid, generation=stamp.generation)\n"
    )
    assert _check(src) == []


def test_a_late_read_laundered_through_the_mint_is_rejected():
    """The defect the `<name>.generation` rule ALONE cannot see (fix round 2,
    Finding 1). Every character of the mutex line is correct; the stamp is minted
    from a read taken at the write, so the comparison is the post-race value
    against itself and the fence is cosmetic. Measured on this tree before the
    mint rule existed: this exact shape in `analysis/insert.py` left
    `tests/fencing/` + `test_f3d3a_relocation.py` at 201 passed, exit 0."""
    src = (
        "def f(session, wid):\n"
        "    stamp = stamp_for_workspace(session.get(Workspace, wid))\n"
        "    acquire_commit_mutex_at_generation(session, wid, generation=stamp.generation)\n"
    )
    problems = _check(src)
    assert len(problems) == 1
    assert "mints a stamp" in problems[0]
    assert "::f " in problems[0]


def test_the_mint_rule_sees_through_the_keyword_spelling():
    """`stamp_for_workspace(workspace=<expr>)` is the same call. A rule that read
    only `node.args[0]` would pass it, and the mint's parameter IS named."""
    src = (
        "def f(session, wid):\n"
        "    stamp = stamp_for_workspace(workspace=session.get(Workspace, wid))\n"
        "    acquire_commit_mutex_at_generation(session, wid, generation=stamp.generation)\n"
    )
    assert len(_check(src)) == 1


def test_the_mint_rule_rejects_an_inlined_mint():
    """No intermediate name at all — the mint, the read and the mutex on one line.
    Note this is caught by the mint rule, NOT by `_is_stamp_attribute`, whose
    Call-in-the-chain test would flag it too; the assertion pins ONE problem so a
    future refactor cannot leave the mint rule doing nothing while the shape rule
    quietly covers for it."""
    src = (
        "def f(session, wid):\n"
        "    acquire_commit_mutex_at_generation(\n"
        "        session, wid,\n"
        "        generation=stamp_for_workspace(session.get(Workspace, wid)).generation,\n"
        "    )\n"
    )
    problems = _check(src)
    assert len(problems) == 2
    assert any("mints a stamp" in p for p in problems)
    assert any("never a read at the mutex" in p for p in problems)


def test_a_mint_with_no_workspace_argument_is_rejected():
    """An origin the scan cannot see is treated as no origin. (`stamp_for_workspace()`
    is a TypeError at runtime; a gate that shrugged at it would be a gate with a
    silent `None` branch.)"""
    src = "def f():\n    stamp = stamp_for_workspace()\n"
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


def test_the_tree_scan_reaches_every_site_the_inventory_says_is_fenced():
    """A lower bound of one is not coverage — it is PARTIAL vacuity (fix round 2,
    Finding 2).

    Measured: narrowing `_SRC` to `.../citations` while planting a late read in
    `analysis/insert.py` left the armed gate AND its non-vacuity guard green at
    9 passed, because one surviving call site satisfies `assert found`. The scan
    has to be answerable to something outside itself, and the inventory is that
    something: `fenced` is MEASURED from source for `git_commit` rows
    (`inventory.MEASURED_KINDS`), so this grows on its own as Tasks 3-6 convert
    the remaining sites — no edit here, and no fixed count to drift.
    """
    from tests.fencing.inventory import load_inventory

    scanned = {rel for rel, _ in _production_sources()}
    must_be_scanned = {
        r["path"]
        for r in load_inventory()
        if r["kind"] == "git_commit" and r["fenced"] is True
    }
    assert must_be_scanned, (
        "the inventory lists no fenced git_commit row — either a conversion was "
        "reverted or the inventory is stale; either way the scan below proves nothing"
    )
    assert must_be_scanned <= scanned, (
        "the src/ scan does not reach a site the inventory records as fenced, so "
        "the armed gate is green over a subset of the tree:\n  "
        + "\n  ".join(sorted(must_be_scanned - scanned))
    )
