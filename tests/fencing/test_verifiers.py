"""Fence F2 — verifier fixtures.

The NEGATIVE cases are the point (Global Constraint 7). Each is a shape that
revision 2's analysis-based verifier accepted.

`fenced_write` / `fenced_execute` do not exist yet — F3 creates them. These
fixtures reference them by name only, the way a lint rule knows the name of the
thing it requires before that thing is common.
"""

from __future__ import annotations

from tests.fencing.detectors import KIND_CLAIM_FN, KIND_TOOL_COMMIT, KIND_TOOL_CONTEXT
from tests.fencing.verifiers import verify

P = "src/call_manager/proposal_assist/tools/a.py"


def row(kind, category, symbol, **extra):
    return {
        "kind": kind, "path": P, "symbol": symbol,
        "category": category, "fenced": True, "slice": "F3", **extra,
    }


def ok(kind, category, src, symbol, **extra):
    return verify(row(kind, category, symbol, **extra), src) == []


# --- tool_commit / semantic: containment ------------------------------------


def test_an_effect_inside_the_managed_block_passes():
    src = """
def f(ctx, ws, gen):
    with fenced_write(session, ws, generation=gen):
        session.add(ProposedChange())
"""
    assert ok(KIND_TOOL_COMMIT, "semantic", src, "f")


def test_a_fenced_execute_passes():
    src = """
def f(ctx, ws, gen):
    fenced_execute(session, update(Job).values(x=1), workspace_id=ws, generation=gen)
"""
    assert ok(KIND_TOOL_COMMIT, "semantic", src, "f")


def test_a_BARE_commit_outside_the_block_is_REJECTED():
    src = """
def f(ctx, ws, gen):
    with fenced_write(session, ws, generation=gen):
        session.add(X())
    session.commit()
"""
    assert not ok(KIND_TOOL_COMMIT, "semantic", src, "f")


def test_a_BARE_orm_add_beside_a_fenced_execute_is_REJECTED():
    """NEGATIVE. Revision 2 examined only execute(update/insert/delete), so a
    retained `session.add` beside an unrelated guarded DML passed."""
    src = """
def f(ctx, ws, gen):
    fenced_execute(session, update(Job).values(x=1), workspace_id=ws, generation=gen)
    session.add(ProposedChange())
"""
    assert not ok(KIND_TOOL_COMMIT, "semantic", src, "f")


def test_a_BARE_write_execute_is_REJECTED():
    src = """
def f(ctx, ws, gen):
    session.execute(update(Job).values(x=1))
    session.commit()
"""
    assert not ok(KIND_TOOL_COMMIT, "semantic", src, "f")


def test_a_DETACHED_pre_check_is_REJECTED():
    """NEGATIVE. The two-statement defect that shipped twice (28abbc4, 701fbb7).
    Under the canonical form it fails for a simpler reason: the effect is not in
    a fenced region at all."""
    src = """
def f(ctx, ws, gen):
    if not session.execute(select(live_at_generation(ws, gen))).scalar():
        return
    session.execute(update(Job).values(x=1))
    session.commit()
"""
    assert not ok(KIND_TOOL_COMMIT, "semantic", src, "f")


def test_a_reassigned_statement_variable_cannot_smuggle_an_effect():
    """NEGATIVE. Revision 2 resolved the FINAL assignment anywhere in the
    function, so an early unguarded write passed on a later guarded one's
    coattails. Containment makes the question moot."""
    src = """
def f(ctx, ws, gen):
    stmt = update(Job).values(x=1)
    session.execute(stmt)
    stmt = update(Job).where(live_at_generation(ws, gen)).values(x=2)
    session.commit()
"""
    assert not ok(KIND_TOOL_COMMIT, "semantic", src, "f")


def test_a_fenced_region_in_a_NESTED_def_does_not_cover_the_parent():
    """NEGATIVE. references.py really nests committing helpers."""
    src = """
def outer(ctx, ws, gen):
    def helper():
        with fenced_write(session, ws, generation=gen):
            session.add(X())
    session.commit()
"""
    assert not ok(KIND_TOOL_COMMIT, "semantic", src, "outer")


def test_a_nested_def_is_verified_on_its_OWN_body():
    src = """
def outer(ctx, ws, gen):
    def helper():
        with fenced_write(session, ws, generation=gen):
            session.add(X())
    return helper
"""
    assert ok(KIND_TOOL_COMMIT, "semantic", src, "outer.helper")


def test_a_site_with_NO_effect_at_all_is_REJECTED():
    """A semantic site that commits nothing is a misclassification, not a pass."""
    src = """
def f(ctx):
    return 1
"""
    assert not ok(KIND_TOOL_COMMIT, "semantic", src, "f")


def test_the_OLD_liveness_manager_is_not_the_canonical_form():
    src = """
def f(ctx, ws, gen):
    with workspace_write(session, ws, timeout_seconds=5, what="x"):
        session.add(X())
"""
    assert not ok(KIND_TOOL_COMMIT, "semantic", src, "f")


# --- tool_commit / accounting ----------------------------------------------


def test_accounting_needs_no_fence():
    """Spec 7: a stale actor must STILL record cleanup and accounting.
    Demanding a fence here would push F3 into breaking that."""
    src = """
def f(session):
    session.commit()
"""
    assert ok(KIND_TOOL_COMMIT, "accounting", src, "f")


# --- tool_commit / caller_fenced -------------------------------------------


def test_caller_fenced_passes_when_the_ancestor_encloses_the_call():
    src = """
def _cache(session, ref, key, *, owns_txn):
    if owns_txn:
        session.commit()

def _outer(ctx, ws, gen):
    with fenced_write(session, ws, generation=gen):
        _cache(session, ref, key, owns_txn=False)
"""
    assert ok(KIND_TOOL_COMMIT, "caller_fenced", src, "_cache", fenced_by="_outer")


def test_caller_fenced_is_REJECTED_when_the_ancestor_does_not_call_it():
    """NEGATIVE. Revision 2 only checked that `fenced_by` was a nonempty string,
    so any arbitrary value passed."""
    src = """
def _cache(session):
    session.commit()

def _outer(ctx, ws, gen):
    with fenced_write(session, ws, generation=gen):
        something_else()
"""
    assert not ok(KIND_TOOL_COMMIT, "caller_fenced", src, "_cache", fenced_by="_outer")


def test_caller_fenced_is_REJECTED_when_the_call_is_OUTSIDE_the_block():
    src = """
def _cache(session):
    session.commit()

def _outer(ctx, ws, gen):
    with fenced_write(session, ws, generation=gen):
        pass
    _cache(session)
"""
    assert not ok(KIND_TOOL_COMMIT, "caller_fenced", src, "_cache", fenced_by="_outer")


def test_caller_fenced_is_REJECTED_when_the_ancestor_is_unfenced():
    src = """
def _cache(session):
    session.commit()

def _outer(session):
    _cache(session)
"""
    assert not ok(KIND_TOOL_COMMIT, "caller_fenced", src, "_cache", fenced_by="_outer")


def test_caller_fenced_is_REJECTED_when_the_ancestor_does_not_exist():
    src = """
def _cache(session):
    session.commit()
"""
    assert not ok(KIND_TOOL_COMMIT, "caller_fenced", src, "_cache", fenced_by="ghost")


# --- tool_context -----------------------------------------------------------


def test_a_one_shot_context_carrying_a_generation_passes():
    src = """
def build(sf, gen):
    return ToolContext(workspace_id=w, session_factory=sf, lifecycle_generation=gen)
"""
    assert ok(KIND_TOOL_CONTEXT, "semantic", src, "build", ctx_lifetime="one_shot")


def test_a_one_shot_context_WITHOUT_a_generation_is_REJECTED():
    src = """
def build(sf):
    return ToolContext(workspace_id=w, session_factory=sf)
"""
    assert not ok(KIND_TOOL_CONTEXT, "semantic", src, "build", ctx_lifetime="one_shot")


def test_a_one_shot_context_with_generation_None_is_REJECTED():
    src = """
def build(sf):
    return ToolContext(workspace_id=w, session_factory=sf, lifecycle_generation=None)
"""
    assert not ok(KIND_TOOL_CONTEXT, "semantic", src, "build", ctx_lifetime="one_shot")


def test_a_RETAINED_context_may_omit_the_generation_at_construction():
    """It is built during launch, which deliberately runs BEFORE the claim
    (turns/processor.py:159), so the generation does not exist yet. Requiring one
    here would bless a placeholder. F3's runtime two-turn test is authoritative."""
    src = """
def tools_factory(repo_dir, workspace_id):
    return ToolContext(workspace_id=workspace_id, session_factory=sf)
"""
    assert ok(KIND_TOOL_CONTEXT, "semantic", src, "tools_factory", ctx_lifetime="retained")


# --- claim_fn ---------------------------------------------------------------


def test_a_claim_assigning_the_column_passes():
    src = """
def claim_next_x(session):
    job.status = "running"
    job.claimed_at_generation = gen_subquery
    session.flush()
"""
    assert ok(KIND_CLAIM_FN, "semantic", src, "claim_next_x")


def test_a_claim_setting_the_column_via_values_passes():
    src = """
def claim_next_x(session):
    session.execute(update(Job).values(status="running", claimed_at_generation=g))
"""
    assert ok(KIND_CLAIM_FN, "semantic", src, "claim_next_x")


def test_a_claim_only_READING_the_column_is_REJECTED():
    """NEGATIVE. Revision 2 used unrestricted ast.walk and accepted any matching
    attribute — a read counted as recording."""
    src = """
def claim_next_x(session):
    if job.claimed_at_generation is None:
        pass
    job.status = "running"
"""
    assert not ok(KIND_CLAIM_FN, "semantic", src, "claim_next_x")


def test_a_claim_mentioning_the_column_as_a_STRING_is_REJECTED():
    src = """
def claim_next_x(session):
    logger.info("claimed_at_generation not set yet")
    job.status = "running"
"""
    assert not ok(KIND_CLAIM_FN, "semantic", src, "claim_next_x")


def test_a_claim_writing_the_column_in_a_NESTED_def_is_REJECTED():
    src = """
def claim_next_x(session):
    def later():
        job.claimed_at_generation = g
    job.status = "running"
"""
    assert not ok(KIND_CLAIM_FN, "semantic", src, "claim_next_x")


def test_a_claim_NOT_recording_the_column_is_REJECTED():
    src = """
def claim_next_x(session):
    job.status = "running"
    session.flush()
"""
    assert not ok(KIND_CLAIM_FN, "semantic", src, "claim_next_x")


# --- resolution and reporting ----------------------------------------------


def test_a_def_nested_inside_a_with_block_is_resolvable():
    """Detector and verifier MUST resolve symbols identically, or the gate
    reports phantom 'symbol not found' for sites that exist."""
    src = """
def outer(ctx, ws, gen):
    with ctx.session_factory() as s:
        def helper():
            with fenced_write(s, ws, generation=gen):
                s.add(X())
        return helper
"""
    assert ok(KIND_TOOL_COMMIT, "semantic", src, "outer.helper")


def test_a_missing_symbol_is_REJECTED():
    assert not ok(KIND_TOOL_COMMIT, "semantic", "def other(): pass", "absent")


def test_reasons_are_returned_for_a_human():
    src = """
def f(session):
    session.commit()
"""
    reasons = verify(row(KIND_TOOL_COMMIT, "semantic", "f"), src)
    assert reasons and all(isinstance(r, str) for r in reasons)
