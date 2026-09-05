"""lifecycle_generation: allocated inside the transition's own transaction.

Retention Slice C0 (spec 4.4). The generation exists so a transition's effects
can be recognised as stale. Sampling it after the commit binds it to when the
sampler ran, not to which transition it belongs to — which is the defect this
column replaces.
"""

from __future__ import annotations

from grant_lab.db.models import Workspace
from grant_lab.workspaces import bump_lifecycle_generation, soft_delete_workspace_db
from tests.proposal_assist.conftest import make_user, make_workspace


def test_a_new_workspace_starts_at_generation_zero(pg_session_factory):
    """0 is reserved for "no transition has ever happened": every transition
    allocates >= 1, so a generation of 0 can never collide with a real one."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        assert s.get(Workspace, ws.id).lifecycle_generation == 0


def test_bump_increments_and_returns_the_new_value(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        assert bump_lifecycle_generation(s, workspace_id=ws_id) == 1
        assert bump_lifecycle_generation(s, workspace_id=ws_id) == 2
        s.commit()

    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).lifecycle_generation == 2


def test_bump_is_computed_in_the_database_not_from_a_stale_read(pg_session_factory):
    """Two sessions that both read 0 must not both write 1. `column + 1` in SQL
    is what makes the commit mutex the only thing deciding order; `python_value
    + 1` would let a stale read silently reuse a generation, and two different
    transitions sharing a generation defeats staleness detection entirely."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as a, pg_session_factory() as b:
        a.get(Workspace, ws_id)   # both load the row at generation 0
        b.get(Workspace, ws_id)
        assert bump_lifecycle_generation(a, workspace_id=ws_id) == 1
        a.commit()
        assert bump_lifecycle_generation(b, workspace_id=ws_id) == 2, (
            "the second bump reused a stale in-Python value instead of reading "
            "the committed column"
        )
        b.commit()


def test_soft_delete_allocates_a_generation_and_returns_it(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        generation = soft_delete_workspace_db(s, workspace_id=ws_id)

    assert generation == 1
    with pg_session_factory() as s:
        row = s.get(Workspace, ws_id)
        assert row.soft_deleted_at is not None
        assert row.lifecycle_generation == 1, (
            "the delete must allocate its generation in the SAME transaction as "
            "the state change; allocating it later is spec 4.4's defect"
        )


def test_the_generation_is_per_workspace(pg_session_factory):
    with pg_session_factory() as s:
        owner_a = make_user(s, email="gen-a@umu.se")
        owner_b = make_user(s, email="gen-b@umu.se")
        a = make_workspace(s, owner=owner_a)
        b = make_workspace(s, owner=owner_b)
        s.commit()
        a_id, b_id = a.id, b.id

    with pg_session_factory() as s:
        soft_delete_workspace_db(s, workspace_id=a_id)

    with pg_session_factory() as s:
        assert s.get(Workspace, a_id).lifecycle_generation == 1
        assert s.get(Workspace, b_id).lifecycle_generation == 0
