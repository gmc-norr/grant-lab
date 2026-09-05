from datetime import UTC, datetime, timedelta
from decimal import Decimal

from grant_lab.config import Settings
from grant_lab.db.models import ReviewRun, ReviewRunItem, Workspace
from grant_lab.proposal_assist.review.jobs import (
    active_review_run,
    cancel_review_runs_and_items_for_workspace,
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
    assert cancel_review_runs_and_items_for_workspace(session, workspace_id=ws_id) == 1
    # a running run older than TTL is reaped
    r = ReviewRun(workspace_id=ws_id, status="running", draft_sha="a" * 64)
    session.add(r)
    session.flush()
    # force triggered_at into the past
    session.execute(
        ReviewRun.__table__.update()
        .where(ReviewRun.id == r.id)
        .values(triggered_at=datetime(2000, 1, 1, tzinfo=UTC))
    )
    assert reap_stuck_review_runs(session, timeout_seconds=10) == 1


def test_reap_rolls_up_item_spend_and_fails_stale_items(review_workspace):
    """A reaped run must (a) carry the spend its items already incurred so the
    daily dollar cap doesn't undercount, and (b) fail any unfinished items
    instead of leaving them stale. Otherwise the run total stays null and
    review_dollars_today (which sums run totals) silently loses real spend."""
    session, ws_id, *_ = review_workspace
    r = ReviewRun(workspace_id=ws_id, status="running", draft_sha="a" * 64)
    session.add(r)
    session.flush()
    session.add_all([
        ReviewRunItem(
            review_run_id=r.id, kind="fit_check", status="done",
            input_tokens=600, output_tokens=200, estimated_dollars=Decimal("0.0050"),
        ),
        ReviewRunItem(
            review_run_id=r.id, kind="persona_domain_skeptic", status="done",
            input_tokens=400, output_tokens=300, estimated_dollars=Decimal("0.0030"),
        ),
        # still in-flight when the reaper fires
        ReviewRunItem(review_run_id=r.id, kind="consolidation", status="running"),
    ])
    session.flush()
    # Stale enough to be reaped (older than the 10s TTL below) but still "today"
    # so review_dollars_today's day-window includes it — a real reaped run is
    # ~minutes old, not the year-2000 sentinel the cancel/reap test uses.
    session.execute(
        ReviewRun.__table__.update()
        .where(ReviewRun.id == r.id)
        .values(triggered_at=datetime.now(UTC) - timedelta(minutes=2))
    )

    assert reap_stuck_review_runs(session, timeout_seconds=10) == 1
    session.expire_all()

    r = session.get(ReviewRun, r.id)
    assert r.status == "failed"
    # spend rolled up from items (0.0050 + 0.0030 -> 0.008 at run precision)
    assert r.estimated_dollars == Decimal("0.008")
    assert r.input_tokens == 1000 and r.output_tokens == 500
    # the in-flight item is now failed with a reaper note; finished items untouched
    items = {
        i.kind: i
        for i in session.query(ReviewRunItem).filter_by(review_run_id=r.id).all()
    }
    assert items["consolidation"].status == "failed"
    assert "timed out" in (items["consolidation"].error_message or "")
    assert items["fit_check"].status == "done"
    # the daily dollar accounting now reflects the reaped spend
    assert review_dollars_today(session, workspace_id=ws_id) == Decimal("0.008")


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


def test_dollars_today_includes_cancelled_run_spend(review_workspace):
    """_run_one_call deliberately re-rolls a late sub-call's real spend onto a
    cancelled run, so cancelled runs CAN carry estimated_dollars. The dollar query
    must count them or it consistently undercounts. (The run *count* still excludes
    cancelled — see test_runs_today_excludes_cancelled — only dollars include them.)"""
    session, ws_id, *_ = review_workspace
    r = ReviewRun(
        workspace_id=ws_id,
        status="cancelled",
        draft_sha="a" * 64,
        estimated_dollars=Decimal("0.0040"),
    )
    session.add(r)
    session.flush()
    assert review_dollars_today(session, workspace_id=ws_id) == Decimal("0.0040")
