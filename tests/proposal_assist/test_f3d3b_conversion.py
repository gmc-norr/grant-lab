"""F3d3b — the two propose sites go onto the effect port, and F3d3 closes.

Spec 2026-08-18 section 7 (as amended 2026-08-21) assigns TWO rows to F3d3, both DB-only
writes on mechanism 1. It previously assigned three; _write_citekey_cache moved to F3e
because it is reached only from inside a transaction already holding the workspace commit
mutex EXCLUSIVELY, so the operation that fences it must own the whole git-then-rows
sequence — mechanism 2, which section 3.5 and EFFECT_MECHANISMS place in F3e.
"""

from __future__ import annotations

import uuid

import pytest

from call_manager.proposal_assist.ports.errors import (
    BUSY_ERROR_MESSAGE,
    DELETED_ERROR_MESSAGE,
    CommitMutexTimeout,
    EffectRejected,
    WorkspaceGenerationStale,
)
from call_manager.proposal_assist.tools.proposal import _propose_artifact_edit_impl
from tests.proposal_assist.test_f3d2_conversions import _G, _ctx_and_invocation


def test_the_citekey_row_belongs_to_the_slice_whose_mechanism_it_needs():
    """The reassignment, stated where a reviewer will look for it.

    Fencing this row with a mechanism-1 operation is not merely inelegant, it hangs: the
    caller holds pg_advisory_xact_lock on the workspace's mutex key and the port would
    open a SECOND connection asking for pg_advisory_xact_lock_shared on the same key.
    port_impl/effects.py's module docstring names this inversion explicitly.
    """
    from tests.fencing.inventory import F3D_IDENTITIES, load_inventory

    assert ("F3e", "tool_commit", "_write_citekey_cache") in F3D_IDENTITIES
    assert ("F3d3", "tool_commit", "_write_citekey_cache") not in F3D_IDENTITIES

    row = next(r for r in load_inventory() if r["symbol"] == "_write_citekey_cache")
    assert row["slice"] == "F3e"
    assert row["fenced"] is False


def test_f3d3_now_owns_exactly_the_two_propose_sites():
    from tests.fencing.inventory import load_inventory

    assert {r["symbol"] for r in load_inventory() if r["slice"] == "F3d3"} == {
        "propose_citation_impl",
        "_propose_artifact_edit_impl",
    }


def test_the_artifact_edit_uses_the_invocation_snapshot_not_the_live_context():
    """ports/invocation.py:44-51 exists for exactly this call site. ToolContext is
    MUTABLE and shared — set_turn_provenance restamps it every turn — so a tool that
    reads ctx.turn_selection when its effect happens can copy a LATER turn's selection
    into a durable proposed-change row.

    The check is on the RECEIVER, not the attribute name. A first draft of this test
    walked __code__.co_names, which records `turn_selection` for BOTH `ctx.turn_selection`
    and `invocation.turn_selection` — so it passed against the unconverted source and
    proved nothing. Verified: the AST form below returns True/False for the two
    receivers respectively; the co_names form returns True for both.
    """
    import ast
    import inspect
    import textwrap

    from call_manager.proposal_assist.tools import proposal as mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(mod._propose_artifact_edit_impl)))

    def reads(receiver: str, attr: str) -> bool:
        return any(
            isinstance(n, ast.Attribute)
            and n.attr == attr
            and isinstance(n.value, ast.Name)
            and n.value.id == receiver
            for n in ast.walk(tree)
        )

    assert reads("invocation", "turn_selection"), (
        "reads the selection off the live ToolContext instead of the dispatch snapshot — "
        "a later turn's selection can be copied into a durable proposed-change row"
    )
    assert not reads("ctx", "turn_selection")
    assert reads("ports", "effects"), "does not call the effect port"
    assert not reads("ctx", "session_factory"), "still opens a session to write"


class _RaisingEffects:
    """An Effects whose propose_artifact_edit raises one chosen port error."""

    def __init__(self, exc):
        self._exc = exc

    def propose_artifact_edit(self, stamp, cmd):
        raise self._exc


class _Ports:
    def __init__(self, effects):
        self.effects = effects


# Verified against ports/errors.py:27-36 and :63. WorkspaceGenerationStale requires BOTH
# keyword arguments — `WorkspaceGenerationStale(wid)` raises TypeError at COLLECTION, so
# a plan that guesses here does not even produce a red test. CommitMutexTimeout is a
# plain RuntimeError subclass and takes no arguments.
_WID = uuid.uuid4()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (WorkspaceGenerationStale(_WID, expected=_G, actual=_G + 1), DELETED_ERROR_MESSAGE),
        (CommitMutexTimeout(), BUSY_ERROR_MESSAGE),
        (EffectRejected("this workspace is no longer accepting proposed changes"),
         "this workspace is no longer accepting proposed changes"),
    ],
)
async def test_the_artifact_edit_answers_every_port_error_as_an_error_dict(exc, expected):
    """The three arms Step 4 added. ports/effects.py's Protocol docstring says a tool
    catching only the first two DOWNGRADES a transient busy into "internal error",
    because _safe_tool turns every unnamed exception into that — so CommitMutexTimeout
    is the arm most worth pinning, and the one no existing test covered.
    """
    ctx, invocation = _ctx_and_invocation(uuid.uuid4())
    out = await _propose_artifact_edit_impl(
        ctx,
        ports=_Ports(_RaisingEffects(exc)),
        invocation=invocation,
        target_artifact="draft",
        head="abc",
        sections={0: "sha"},
        section_index=0,
        new_text="x",
        not_read_err="unread",
    )
    assert out == {"error": expected}


def test_the_citation_propose_writes_through_the_port_and_reads_through_a_session():
    """GC-2 precisely: the WRITE authority goes, the read stays.

    A session used only for reads holds no commit, and `fenced` is measured as
    "port call AND no raw commit" — so this tool measures fenced while still reading
    through a session. Its session_factory and repo_dir namings are already pinned
    import-gate violations that retire in F3f.
    """
    import inspect

    from call_manager.proposal_assist.tools import citations as mod

    src = inspect.getsource(mod.propose_citation_impl)
    assert "ports.effects.propose_citation_insert" in src, "does not call the effect port"
    assert "session.commit()" not in src, "still commits directly"
    assert "propose_citation_insert(\n            session=" not in src


def test_both_f3d3_rows_are_fenced_and_the_slice_says_so():
    from tests.fencing.inventory import SLICE_STATUS, load_inventory

    rows = {r["symbol"]: r for r in load_inventory() if r["slice"] == "F3d3"}
    assert set(rows) == {"propose_citation_impl", "_propose_artifact_edit_impl"}
    assert all(r["fenced"] for r in rows.values())
    assert SLICE_STATUS["F3d3"] == "complete"


def test_the_exemption_list_still_has_five_keys():
    """This slice SHRINKS two pin sets and retires neither: both modules still name
    repo_dir and session_factory, which are F3f's."""
    from tests.fencing.inventory import TOOLS_NOT_YET_CONVERTED

    assert set(TOOLS_NOT_YET_CONVERTED) == {
        "audit.py", "citations.py", "history.py", "proposal.py", "references.py",
    }


def test_the_citekey_cache_is_untouched():
    """The row moved; the code did not. If a later slice finds this failing, it is
    converting the accept path — which is F3e's job and needs a mechanism-2 operation."""
    import inspect

    from call_manager.proposal_assist.citations import core

    src = inspect.getsource(core._write_citekey_cache)
    assert "session.commit()" in src
    assert "begin_nested" in src
    assert "ports" not in inspect.signature(core._write_citekey_cache).parameters


def test_no_new_effect_operation_was_added():
    """EFFECT_MECHANISMS and EFFECT_NAMES are set-equality gates; this slice converts
    onto operations F3d1 already built and adds none."""
    from call_manager.proposal_assist.ports.effects import EFFECT_MECHANISMS

    assert set(EFFECT_MECHANISMS) == {
        "submit_scorecard",
        "submit_review",
        "submit_uncited_claims",
        "propose_artifact_edit",
        "propose_citation_insert",
    }
