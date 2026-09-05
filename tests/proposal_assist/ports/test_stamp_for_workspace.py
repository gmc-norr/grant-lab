"""Fence F3e1a — the service-side twin of Invocation.stamp().

Drives the real function against a duck-typed stand-in AND a real Workspace row,
because the production callers in F3e1b pass a real one and a stand-in alone would
not catch an attribute-name change on the model.
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from grant_lab.proposal_assist.ports.errors import UnfencedInvocation
from grant_lab.proposal_assist.ports.stamp import FenceStamp, stamp_for_workspace


class _WS:
    """The minimum a caller must hold: an id and a generation."""

    def __init__(self, id_, generation):
        self.id = id_
        self.lifecycle_generation = generation


def test_it_mints_the_workspace_id_and_generation():
    wid = uuid.uuid4()
    stamp = stamp_for_workspace(_WS(wid, 7))
    assert stamp.workspace_id == wid
    assert stamp.generation == 7


def test_generation_zero_is_real_and_is_not_refused():
    """Workspace.lifecycle_generation has server_default 0, so a fresh workspace
    legitimately carries 0. A falsiness test here would refuse every new workspace —
    Invocation.stamp() documents the same trap."""
    stamp = stamp_for_workspace(_WS(uuid.uuid4(), 0))
    assert stamp.generation == 0


def test_the_job_fields_are_none():
    """A request or a maintenance pass is not a background job. This is the same
    None registry.py already writes for interactive contexts."""
    stamp = stamp_for_workspace(_WS(uuid.uuid4(), 3))
    assert stamp.active_turn_job_id is None
    assert stamp.origin_job_id is None


def test_it_refuses_a_workspace_with_no_generation():
    """Refusing beats defaulting: an actor with no claimed generation has no belief
    to fence against, and a default would submit an effect against a generation
    nobody claimed. Mirrors Invocation.stamp()."""
    wid = uuid.uuid4()
    with pytest.raises(UnfencedInvocation):
        stamp_for_workspace(_WS(wid, None))


def test_the_stamp_is_immutable():
    # dataclasses.FrozenInstanceError, not a bare Exception: ruff has "B" selected
    # so B017 rejects the broad form, and test_f3b_stamp.py:63 already uses this
    # spelling for the same assertion on the same type.
    stamp = stamp_for_workspace(_WS(uuid.uuid4(), 1))
    with pytest.raises(dataclasses.FrozenInstanceError):
        stamp.generation = 99


def test_it_mints_from_a_real_workspace_row(pg_session_factory):
    """The attribute names must match the ORM, not just the stand-in.

    pg_session_factory is the fixture this repo provides (tests/conftest.py:70);
    make_workspace in tests/proposal_assist/conftest.py:200 is a HELPER, not a
    fixture, so it is imported and called, not requested.
    """
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as session:
        ws = make_workspace(session)
        stamp = stamp_for_workspace(ws)
    assert stamp == FenceStamp(
        workspace_id=ws.id,
        generation=ws.lifecycle_generation,
        active_turn_job_id=None,
        origin_job_id=None,
    )
