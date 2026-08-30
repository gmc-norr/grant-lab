"""The branch entity: a branch is a row with an immutable id (spec §3.2).

Uses the house fixtures: `make_workspace` (tests/proposal_assist/conftest.py) and
`pg_session_factory` (tests/conftest.py). Do not construct a Workspace by hand —
it needs an owner.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import Branch, Workspace
from tests.proposal_assist.conftest import make_user, make_workspace


def test_a_branch_row_can_be_created_and_read_back(pg_session_factory):
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()

    with pg_session_factory() as s:
        b = Branch(workspace_id=wid, name="main")
        s.add(b)
        s.commit()
        bid = b.id

    with pg_session_factory() as s:
        row = s.get(Branch, bid)
        assert row.workspace_id == wid
        assert row.name == "main"
        assert row.last_sdk_session_id is None
        assert row.created_at is not None


def test_one_name_per_workspace(pg_session_factory):
    """The name is a label, but it is a UNIQUE label within its workspace —
    otherwise `git branch` and the rows could disagree about how many exist."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()

    with pg_session_factory() as s:
        s.add(Branch(workspace_id=wid, name="dup"))
        s.commit()

    with pg_session_factory() as s:
        s.add(Branch(workspace_id=wid, name="dup"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_the_same_name_in_two_workspaces_is_fine(pg_session_factory):
    """Control for the test above. Uniqueness is per workspace, not global —
    every workspace has a 'main', and a global unique key would break the
    second one silently at backfill time."""
    with pg_session_factory() as s:
        # Distinct owners: make_user's default email is the fixed literal
        # "owner@ki.se" (tests/proposal_assist/conftest.py:200-204), so two
        # make_workspace() calls with no owner= in the same session collide
        # on uq_users_email. That collision is incidental to this test's
        # subject (Branch.name uniqueness), so give each workspace its own
        # owner rather than let an unrelated fixture collision mask it.
        a = make_workspace(s, title="a", owner=make_user(s, email="branch-test-a@ki.se")).id
        b = make_workspace(s, title="b", owner=make_user(s, email="branch-test-b@ki.se")).id
        s.commit()

    with pg_session_factory() as s:
        s.add(Branch(workspace_id=a, name="main"))
        s.add(Branch(workspace_id=b, name="main"))
        s.commit()  # must not raise

    with pg_session_factory() as s:
        assert len(s.execute(select(Branch).where(Branch.name == "main")).all()) == 2


def test_root_pointer_holds_a_branch_id(pg_session_factory):
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()

    with pg_session_factory() as s:
        b = Branch(workspace_id=wid, name="main")
        s.add(b)
        s.flush()
        s.get(Workspace, wid).root_branch_id = b.id
        s.commit()
        bid = b.id

    with pg_session_factory() as s:
        assert s.get(Workspace, wid).root_branch_id == bid


def test_root_pointer_starts_null(pg_session_factory):
    """A workspace with no repo has no branch to point at. Nullable is the honest
    shape; the invariant test in Task 6 pins that a workspace WITH a repo is
    never left null."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    with pg_session_factory() as s:
        assert s.get(Workspace, wid).root_branch_id is None


def test_root_pointer_rejects_an_unknown_branch(pg_session_factory):
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    with pg_session_factory() as s:
        s.get(Workspace, wid).root_branch_id = uuid.uuid4()
        with pytest.raises(IntegrityError):
            s.commit()


def test_the_root_branch_cannot_be_deleted(pg_session_factory):
    """§7.3: 'The branch the pointer names cannot be deleted.' RESTRICT makes that
    a database guarantee rather than an application promise, so a future caller
    that forgets the check cannot destroy the root."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()

    with pg_session_factory() as s:
        b = Branch(workspace_id=wid, name="main")
        s.add(b)
        s.flush()
        s.get(Workspace, wid).root_branch_id = b.id
        s.commit()
        bid = b.id

    with pg_session_factory() as s:
        s.delete(s.get(Branch, bid))
        with pytest.raises(IntegrityError):
            s.commit()


def test_a_non_root_branch_can_be_deleted(pg_session_factory):
    """Control. RESTRICT must protect ONLY the root — if it blocked every branch
    the delete path would be dead, and the test above would pass for the wrong
    reason."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()

    with pg_session_factory() as s:
        root = Branch(workspace_id=wid, name="main")
        other = Branch(workspace_id=wid, name="new_ideas")
        s.add_all([root, other])
        s.flush()
        s.get(Workspace, wid).root_branch_id = root.id
        s.commit()
        other_id = other.id

    with pg_session_factory() as s:
        s.delete(s.get(Branch, other_id))
        s.commit()  # must not raise
        assert s.get(Branch, other_id) is None
