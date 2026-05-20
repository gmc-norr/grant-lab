from sqlalchemy import inspect, select

from call_monitor.db.models import TurnJob, Workspace
from tests.proposal_assist.conftest import make_workspace


def test_workspace_has_b4_columns():
    cols = {c.name for c in inspect(Workspace).columns}
    assert "last_sdk_session_id" in cols
    assert "daily_token_cap_in_override" in cols
    assert "daily_token_cap_out_override" in cols


def test_turn_jobs_has_submitted_by_user_id_column():
    cols = {c.name for c in inspect(TurnJob).columns}
    assert "submitted_by_user_id" in cols


def test_workspace_defaults_b4_columns_to_null(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    with pg_session_factory() as s:
        row = s.scalar(select(Workspace).where(Workspace.id == wsid))
        assert row.last_sdk_session_id is None
        assert row.daily_token_cap_in_override is None
        assert row.daily_token_cap_out_override is None


def test_workspace_can_set_last_sdk_session_id(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    with pg_session_factory() as s:
        row = s.scalar(select(Workspace).where(Workspace.id == wsid))
        row.last_sdk_session_id = "abc-123"
        s.commit()
    with pg_session_factory() as s:
        row = s.scalar(select(Workspace).where(Workspace.id == wsid))
        assert row.last_sdk_session_id == "abc-123"


def test_b4_settings_load_with_defaults(_settings_env):
    """The six new B4 settings expose the documented defaults when no
    override env vars are set."""
    from call_monitor.config import Settings

    s = Settings()
    assert s.daily_token_cap_input == 500_000
    assert s.daily_token_cap_output == 100_000
    assert s.compaction_turn_threshold == 100
    assert s.compaction_token_threshold == 200_000
    assert s.compaction_tick_seconds == 300
    assert s.turn_drain_tick_seconds == 5
