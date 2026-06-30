# tests/proposal_assist/collab/test_collab_ws_auth.py
import pytest

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import User, WorkspaceViewer
from call_manager.proposal_assist.collab.ws_auth import (
    WSAuthError,
    resolve_access,
    user_from_cookie,
)
from tests.proposal_assist.conftest import make_user, make_workspace


def _signer() -> SessionSigner:
    return SessionSigner(secret="test-secret-at-least-16-chars")


def test_user_from_cookie_valid(pg_session_factory):
    with pg_session_factory() as s:
        u = make_user(s, email="wsauth-ok@ki.se")
        s.commit()
        uid = u.id
    signer = _signer()
    cookie = signer.make(user_id=uid)
    user = user_from_cookie(cookie, signer=signer, session_factory=pg_session_factory)
    assert user.id == uid


def test_user_from_cookie_missing_or_bad_raises(pg_session_factory):
    signer = _signer()
    with pytest.raises(WSAuthError):
        user_from_cookie(None, signer=signer, session_factory=pg_session_factory)
    with pytest.raises(WSAuthError):
        user_from_cookie("garbage", signer=signer, session_factory=pg_session_factory)


def test_user_from_cookie_rejects_inactive_and_stale_epoch(pg_session_factory):
    with pg_session_factory() as s:
        u = User(status="disabled", email="wsauth-disabled@ki.se")
        s.add(u)
        s.commit()
        uid = u.id
    signer = _signer()
    with pytest.raises(WSAuthError):
        user_from_cookie(
            signer.make(user_id=uid), signer=signer, session_factory=pg_session_factory
        )

    # active but epoch bumped after the cookie was minted
    with pg_session_factory() as s:
        u = make_user(s, email="wsauth-epoch@ki.se")
        s.commit()
        uid = u.id
    cookie = signer.make(user_id=uid, session_epoch=0)
    with pg_session_factory() as s:
        s.get(User, uid).session_epoch = 5
        s.commit()
    with pytest.raises(WSAuthError):
        user_from_cookie(cookie, signer=signer, session_factory=pg_session_factory)


def _add_viewer(session, ws, user):
    session.add(WorkspaceViewer(workspace_id=ws.id, user_id=user.id))
    session.commit()


def test_access_matrix(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s, email="acc-owner@ki.se")
        member = make_user(s, email="acc-member@ki.se")
        stranger = make_user(s, email="acc-stranger@ki.se")
        ws_active = make_workspace(s, owner=owner, current_phase="active")
        _add_viewer(s, ws_active, member)
        s.commit()
        owner_id, member_id, stranger_id, wid = owner.id, member.id, stranger.id, ws_active.id

    with pg_session_factory() as s:
        owner = s.get(User, owner_id)
        member = s.get(User, member_id)
        stranger = s.get(User, stranger_id)

        # active draft: owner & member write, stranger has no access
        assert resolve_access(s, user=owner, workspace_id=wid, artifact="draft") == (True, True)
        assert resolve_access(s, user=member, workspace_id=wid, artifact="draft") == (True, True)
        assert resolve_access(s, user=stranger, workspace_id=wid, artifact="draft") == (
            False,
            False,
        )


def test_access_setup_phase_is_owner_only(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s, email="setup-owner@ki.se")
        member = make_user(s, email="setup-member@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="setup")
        _add_viewer(s, ws, member)
        s.commit()
        oid, mid, wid = owner.id, member.id, ws.id
    with pg_session_factory() as s:
        owner, member = s.get(User, oid), s.get(User, mid)
        assert resolve_access(s, user=owner, workspace_id=wid, artifact="draft") == (True, True)
        assert resolve_access(s, user=member, workspace_id=wid, artifact="draft") == (True, False)


def test_access_locked_spec_is_owner_only(pg_session_factory):
    from datetime import UTC, datetime

    with pg_session_factory() as s:
        owner = make_user(s, email="lock-owner@ki.se")
        member = make_user(s, email="lock-member@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        _add_viewer(s, ws, member)
        ws.spec_locked_at = datetime.now(UTC)
        s.commit()
        oid, mid, wid = owner.id, member.id, ws.id
    with pg_session_factory() as s:
        owner, member = s.get(User, oid), s.get(User, mid)
        # locked spec: member read-only, owner still writes
        assert resolve_access(s, user=member, workspace_id=wid, artifact="spec") == (True, False)
        assert resolve_access(s, user=owner, workspace_id=wid, artifact="spec") == (True, True)
        # draft unaffected by spec lock
        assert resolve_access(s, user=member, workspace_id=wid, artifact="draft") == (True, True)


class _FakeWS:
    def __init__(self, origin, base_url="http://testserver"):
        self.headers = {} if origin is None else {"origin": origin}

        class _State:
            pass

        class _Settings:
            app_base_url = base_url

        self.app = type("A", (), {})()
        self.app.state = _State()
        self.app.state.settings = _Settings()


def test_check_origin_matrix():
    from call_manager.proposal_assist.collab.ws_auth import check_origin

    assert check_origin(_FakeWS("http://testserver")) is True      # same origin
    assert check_origin(_FakeWS(None)) is True                     # non-browser (no Origin)
    assert check_origin(_FakeWS("http://evil.example")) is False   # cross-site
    assert check_origin(_FakeWS("null")) is False                  # sandboxed/opaque
