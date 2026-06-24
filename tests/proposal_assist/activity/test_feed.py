import uuid
from datetime import UTC, datetime, timedelta

from call_manager.db.models import WorkspaceEvent
from call_manager.proposal_assist.activity.feed import (
    actor_user_id,
    build_activity_feed,
)

_BASE = datetime(2026, 6, 23, 12, 0, 0, tzinfo=UTC)


def _ev(kind, *, actor_id=None, payload=None, secs_ago=0, ev_id=None):
    """A WorkspaceEvent built in-memory (no session). created_at is set
    explicitly so the helper does not depend on DB defaults. Newer events
    have a smaller secs_ago, since callers pass events newest-first."""
    return WorkspaceEvent(
        id=ev_id or uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        kind=kind,
        payload=payload or {},
        actor_id=actor_id,
        created_at=_BASE - timedelta(seconds=secs_ago),
    )


def test_excluded_and_unknown_kinds_produce_no_rows():
    events = [
        _ev("git_drift_reconciled"),
        _ev("disclosure_acknowledged", actor_id=1),
        _ev("cloud_turn_sent", actor_id=1),
        _ev("some_future_kind", actor_id=1),
    ]
    assert build_activity_feed(events, {1: "a@ki.se"}) == []


def test_collapse_same_actor_same_heading_edits():
    events = [
        _ev("user_edit", actor_id=1, payload={"heading": "Methods", "artifact": "draft"}, secs_ago=0),
        _ev("user_edit", actor_id=1, payload={"heading": "Methods", "artifact": "draft"}, secs_ago=10),
        _ev("user_edit", actor_id=1, payload={"heading": "Methods", "artifact": "draft"}, secs_ago=20),
    ]
    rows = build_activity_feed(events, {1: "a@ki.se"})
    assert len(rows) == 1
    r = rows[0].as_dict()
    assert r["category"] == "edit"
    assert r["summary"] == "edited Methods ×3"
    assert r["count"] == 3
    assert r["actor"] == "a@ki.se"
    assert r["target"] == {"top_tab": "draft", "bottom_tab": None}


def test_collapse_mixed_headings_makes_n_edits():
    events = [
        _ev("user_edit", actor_id=1, payload={"heading": "Methods", "artifact": "draft"}),
        _ev("user_edit", actor_id=1, payload={"heading": "Aims", "artifact": "spec"}),
        _ev("user_edit", actor_id=1, payload={"heading": "Budget", "artifact": "draft"}),
    ]
    rows = build_activity_feed(events, {1: "a@ki.se"})
    assert len(rows) == 1
    assert rows[0].summary == "made 3 edits"
    # mixed artifacts default to draft
    assert rows[0].target.top_tab == "draft"


def test_spec_init_edit_without_heading():
    events = [_ev("user_edit", actor_id=1, payload={"artifact": "spec"})]
    rows = build_activity_feed(events, {1: "a@ki.se"})
    assert rows[0].summary == "edited the spec"
    assert rows[0].count == 1
    assert rows[0].target.top_tab == "spec"


def test_blank_heading_run_uses_artifact_fallback():
    events = [
        _ev("user_edit", actor_id=1, payload={"heading": "Methods", "artifact": "draft"}),
        _ev("user_edit", actor_id=1, payload={"artifact": "draft"}),  # blank heading
    ]
    rows = build_activity_feed(events, {1: "a@ki.se"})
    assert rows[0].summary == "edited the draft ×2"
    assert rows[0].target.top_tab == "draft"


def test_run_broken_by_interleaving_preserves_chronology():
    events = [
        _ev("user_edit", actor_id=1, payload={"heading": "Methods", "artifact": "draft"}, secs_ago=0),
        _ev("cloud_turn_completed", actor_id=1, secs_ago=10),
        _ev("user_edit", actor_id=1, payload={"heading": "Methods", "artifact": "draft"}, secs_ago=20),
    ]
    rows = build_activity_feed(events, {1: "a@ki.se"})
    assert [r.category for r in rows] == ["edit", "turn", "edit"]
    assert all(r.count == 1 for r in rows)


def test_actor_boundary_splits_run():
    events = [
        _ev("user_edit", actor_id=1, payload={"heading": "Methods", "artifact": "draft"}),
        _ev("user_edit", actor_id=2, payload={"heading": "Methods", "artifact": "draft"}),
    ]
    rows = build_activity_feed(events, {1: "a@ki.se", 2: "b@ki.se"})
    assert [r.actor for r in rows] == ["a@ki.se", "b@ki.se"]
    assert len(rows) == 2


def test_render_and_target_table():
    cases = {
        "created": ("created the workspace", None),
        "user_citation_insert": ("added a citation", {"top_tab": "draft", "bottom_tab": None}),
        "cloud_turn_completed": ("ran the assistant", None),
        "cloud_turn_failed": ("had an assistant turn fail", None),
        "cloud_turn_cancelled": ("cancelled an assistant turn", None),
        "review_run_triggered": ("started a panel review", {"top_tab": "review", "bottom_tab": None}),
        "uncited_claims_run_triggered": ("started an uncited-claims check", {"top_tab": None, "bottom_tab": "problems"}),
        "uncited_claim_dismissed": ("dismissed an uncited claim", {"top_tab": None, "bottom_tab": "problems"}),
        "user_claim_dismissed": ("dismissed a flagged claim", None),
        "reference.added": ("added a reference", None),
        "reference.metadata_approved": ("approved reference metadata", None),
        "reference_metadata_waived": ("waived reference metadata", None),
        "reference_metadata_waiver_cleared": ("cleared a reference metadata waiver", None),
        "reference.kind_changed": ("changed a reference type", None),
        "reference.reprocessed": ("reprocessed a reference", None),
        "reference.tombstoned": ("removed a reference", None),
        "reference.deleted": ("deleted a reference", None),
        "rubric_initialized": ("set up the funder criteria", {"top_tab": "spec", "bottom_tab": None}),
        # rubric_edited with actor_id set (the payload edited_by_user_id fallback is
        # exercised separately by test_actor_resolution_variants + the endpoint test).
        "rubric_edited": ("edited the funder criteria", {"top_tab": "spec", "bottom_tab": None}),
    }
    for kind, (summary, target) in cases.items():
        rows = build_activity_feed([_ev(kind, actor_id=1)], {1: "a@ki.se"})
        assert len(rows) == 1, kind
        d = rows[0].as_dict()
        assert d["summary"] == summary, kind
        assert d["target"] == target, kind
        assert d["count"] == 1, kind


def test_actor_resolution_variants():
    # rubric_edited has no actor_id; user is in payload.edited_by_user_id
    rubric = _ev("rubric_edited", payload={"edited_by_user_id": 7})
    assert actor_user_id(rubric) == 7
    assert build_activity_feed([rubric], {7: "ed@ki.se"})[0].actor == "ed@ki.se"
    # rubric_initialized carries no user id anywhere -> System (None)
    init = _ev("rubric_initialized")
    assert actor_user_id(init) is None
    assert build_activity_feed([init], {})[0].actor is None
    # actor_id present but absent from names map -> None (defensive)
    assert build_activity_feed([_ev("created", actor_id=99)], {})[0].actor is None
    # null actor_id -> None
    assert build_activity_feed([_ev("created")], {})[0].actor is None


def test_collapsed_row_id_and_time_come_from_most_recent():
    newest = uuid.uuid4()
    events = [
        _ev("user_edit", actor_id=1, payload={"heading": "M", "artifact": "draft"}, secs_ago=0, ev_id=newest),
        _ev("user_edit", actor_id=1, payload={"heading": "M", "artifact": "draft"}, secs_ago=30),
    ]
    row = build_activity_feed(events, {1: "a@ki.se"})[0]
    assert row.id == str(newest)
    assert row.created_at == (_BASE).isoformat()


def test_member_added_renders_under_member_category_with_email():
    from call_manager.proposal_assist.activity.feed import build_activity_feed

    ev = _ev("member_added", actor_id=1, payload={"target_user_id": 2, "target_email": "alice@ki.se"})
    rows = build_activity_feed([ev], {1: "owner@ki.se"})
    assert len(rows) == 1
    assert rows[0].category == "member"
    assert "alice@ki.se" in rows[0].summary
    assert rows[0].actor == "owner@ki.se"
