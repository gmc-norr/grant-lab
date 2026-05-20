import pytest
from sqlalchemy.exc import IntegrityError

from call_monitor.db.models import ReviewRun, ReviewRunItem
from tests.proposal_assist.conftest import make_workspace


def test_review_run_active_lock_blocks_double_queued(pg_session_factory):
    """The active-review lock — at most one queued/running run per workspace."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(ReviewRun(workspace_id=ws.id, status="queued", draft_sha="d" * 40))
        s.commit()
        s.add(ReviewRun(workspace_id=ws.id, status="queued", draft_sha="d" * 40))
        with pytest.raises(IntegrityError):
            s.commit()


def test_review_run_active_lock_allows_after_terminal(pg_session_factory):
    """Completed/failed/partial runs don't block a new queued run."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(ReviewRun(workspace_id=ws.id, status="done", draft_sha="d" * 40))
        s.commit()
        s.add(ReviewRun(workspace_id=ws.id, status="queued", draft_sha="d" * 40))
        s.commit()  # should not raise


def test_review_run_status_check_constraint(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(ReviewRun(workspace_id=ws.id, status="not_a_status", draft_sha="d" * 40))
        with pytest.raises(IntegrityError):
            s.commit()


def test_review_run_partial_status_allowed(pg_session_factory):
    """'partial' is one of the 6 accepted statuses (some persona failed,
    consolidation still produced a result)."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(ReviewRun(workspace_id=ws.id, status="partial", draft_sha="d" * 40))
        s.commit()


def test_review_run_waiver_lineage_self_fk(pg_session_factory):
    """waived_from_review_run_id is a self-FK on review_runs(id)."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        original = ReviewRun(
            workspace_id=ws.id,
            status="failed",
            draft_sha="d" * 40,
        )
        s.add(original)
        s.flush()
        waived = ReviewRun(
            workspace_id=ws.id,
            status="queued",
            draft_sha="d" * 40,
            waived_from_review_run_id=original.id,
        )
        s.add(waived)
        s.commit()
        fetched = s.get(ReviewRun, waived.id)
        assert fetched.waived_from_review_run_id == original.id


def test_review_run_item_kind_unique_per_run(pg_session_factory):
    """At most one item per (run, kind) combination."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        rr = ReviewRun(workspace_id=ws.id, status="running", draft_sha="d" * 40)
        s.add(rr)
        s.flush()
        s.add(ReviewRunItem(review_run_id=rr.id, kind="fit_check", status="queued"))
        s.commit()
        s.add(ReviewRunItem(review_run_id=rr.id, kind="fit_check", status="queued"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_review_run_item_kind_check_constraint(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        rr = ReviewRun(workspace_id=ws.id, status="running", draft_sha="d" * 40)
        s.add(rr)
        s.flush()
        s.add(ReviewRunItem(review_run_id=rr.id, kind="not_a_kind", status="queued"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_review_run_item_status_check_constraint(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        rr = ReviewRun(workspace_id=ws.id, status="running", draft_sha="d" * 40)
        s.add(rr)
        s.flush()
        s.add(
            ReviewRunItem(
                review_run_id=rr.id,
                kind="fit_check",
                status="not_a_status",
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()
