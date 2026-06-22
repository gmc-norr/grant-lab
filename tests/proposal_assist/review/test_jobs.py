from decimal import Decimal

from call_manager.config import Settings
from call_manager.db.models import ReviewRun, Workspace
from call_manager.proposal_assist.review.jobs import (
    active_review_run,
    cancel_review_runs_for_workspace,
    claim_next_review_run,
    enqueue_review_run,
    estimate_dollars,
    reap_stuck_review_runs,
    review_dollar_cap,
    review_dollars_today,
    review_run_cap,
    review_runs_today,
)


def test_enqueue_create_or_return_active(review_workspace):
    session, ws_id, *_ = review_workspace
    r1 = enqueue_review_run(session, workspace_id=ws_id, draft_sha="a" * 64)
    session.flush()
    r2 = enqueue_review_run(session, workspace_id=ws_id, draft_sha="b" * 64)
    assert r1.id == r2.id  # returns the existing active run
    assert active_review_run(session, workspace_id=ws_id).id == r1.id


def test_claim_flips_to_running_and_excludes_running_ws(review_workspace):
    session, ws_id, *_ = review_workspace
    enqueue_review_run(session, workspace_id=ws_id, draft_sha="a" * 64)
    session.flush()
    claimed = claim_next_review_run(session)
    assert claimed is not None and claimed.status == "running"
    assert claim_next_review_run(session) is None  # ws already has a running run


def test_cancel_and_reap(review_workspace):
    session, ws_id, *_ = review_workspace
    enqueue_review_run(session, workspace_id=ws_id, draft_sha="a" * 64)
    session.flush()
    assert cancel_review_runs_for_workspace(session, workspace_id=ws_id) == 1
    # a running run older than TTL is reaped
    r = ReviewRun(workspace_id=ws_id, status="running", draft_sha="a" * 64)
    session.add(r)
    session.flush()
    # force triggered_at into the past
    session.execute(
        ReviewRun.__table__.update().where(ReviewRun.id == r.id).values(
            triggered_at=__import__("datetime").datetime(2000, 1, 1, tzinfo=__import__("datetime").UTC)
        )
    )
    assert reap_stuck_review_runs(session, timeout_seconds=10) == 1


def test_caps_and_pricing(review_workspace, _settings_env):
    session, ws_id, *_ = review_workspace
    ws = session.get(Workspace, ws_id)
    s = Settings()
    assert review_run_cap(ws, s) == 5
    assert review_dollar_cap(ws, s) == Decimal("5.000")
    ws.review_runs_per_day_override = 2
    ws.review_dollar_cap_override = Decimal("1.500")
    session.flush()
    assert review_run_cap(ws, s) == 2 and review_dollar_cap(ws, s) == Decimal("1.500")
    assert review_runs_today(session, workspace_id=ws_id) == 0
    assert review_dollars_today(session, workspace_id=ws_id) == Decimal("0")
    # 1M in + 1M out => $5 + $25 = $30.00
    assert estimate_dollars(1_000_000, 1_000_000, s) == Decimal("30.0000")
    assert estimate_dollars(0, 0, s) == Decimal("0.0000")


def test_runs_today_excludes_cancelled(review_workspace):
    session, ws_id, *_ = review_workspace
    r = enqueue_review_run(session, workspace_id=ws_id, draft_sha="a" * 64)
    session.flush()
    assert review_runs_today(session, workspace_id=ws_id) == 1
    r.status = "cancelled"
    session.flush()
    assert review_runs_today(session, workspace_id=ws_id) == 0
