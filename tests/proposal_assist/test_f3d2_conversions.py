"""F3d2 — the submitters and the reads. Each converted site against its port, and
each with the compensation disabled.

Spec 2026-08-18 revision 8 §5: "a conversion test that passes with the fence removed
is measuring the compensation, not the fence".
"""

from __future__ import annotations

from call_manager.proposal_assist.tools.registry import ToolContext, snapshot_invocation

_G = 4  # the generation every actor in this file claims at
_NEXT = 5  # what a lifecycle transition moves it to


def _ctx_and_invocation(workspace_id, **extra):
    """A ToolContext at generation _G plus its frozen Invocation.

    ports=None deliberately: a converted tool receives its bundle as a KEYWORD from
    _safe_tool, not through the context, and these tests call the impls directly.
    """
    ctx = ToolContext(
        workspace_id=workspace_id,
        repo_dir=None,
        session_factory=None,
        settings=None,
        lifecycle_generation=_G,
        **extra,
    )
    return ctx, snapshot_invocation(ctx)


class _ExplodingPorts:
    """Fails the test if the tool reaches for a capability at all. Used wherever a
    tool must refuse a bad payload BEFORE touching a port."""

    def __getattr__(self, name):
        raise AssertionError(f"the port was touched: {name}")


def _criterion(index, name="A"):
    return {"index": index, "name": name, "band": "adequate", "note": "n"}


def test_the_band_rollup_is_reachable_from_ports():
    """A converted submit_scorecard_impl still has to compute the overall band —
    Effects.submit_scorecard returns None and the tool reports the band back to the
    agent — and it may import only stdlib and ports."""
    from call_manager.proposal_assist.ports.scoring import compute_overall_band

    assert callable(compute_overall_band)


def test_the_old_scoring_module_re_exports_the_same_function():
    """GC-4 of F3d1, still binding: one source of truth, and the moved object stays
    the SAME object so no caller can drift onto a second copy."""
    from call_manager.proposal_assist.evaluator import scoring as old
    from call_manager.proposal_assist.ports import scoring as new

    assert old.compute_overall_band is new.compute_overall_band
