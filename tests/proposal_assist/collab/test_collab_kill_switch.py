"""3b-5d.D4: COLLAB_KILL_SWITCH masks the collab flag at the single choke point."""

import pytest

from grant_lab.config import Settings
from grant_lab.proposal_assist.collab import state
from tests.proposal_assist.conftest import make_user, make_workspace


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    """The switch is a module global — a leaked True poisons every collab test."""
    yield
    state.set_kill_switch(False)


def test_settings_parse_env_alias(monkeypatch, _settings_env):
    monkeypatch.setenv("COLLAB_KILL_SWITCH", "1")
    assert Settings().collab_kill_switch is True
    monkeypatch.delenv("COLLAB_KILL_SWITCH")
    assert Settings().collab_kill_switch is False


def test_kill_switch_masks_enabled_flag(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="ks@umu.se"))
        state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid = ws.id
    with pg_session_factory() as s:
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is True
        state.set_kill_switch(True)
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is False
        state.set_kill_switch(False)
        # enabled_at row untouched — the switch masks, never clears (sticky-forward)
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is True


def test_enable_collab_still_writes_under_kill_switch_mask(pg_session_factory):
    """enable_collab itself is NOT gated (the handover/scan need DB truth);
    only the is_collab_enabled VIEW is masked."""
    state.set_kill_switch(True)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="ks2@umu.se"))
        state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid = ws.id
    state.set_kill_switch(False)
    with pg_session_factory() as s:
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is True
