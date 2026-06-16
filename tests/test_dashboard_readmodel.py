from call_manager.db.models import User
from call_manager.web.dashboard import build_dashboard_view, resolve_profile_selection


def _seed(pg_session_factory):
    with pg_session_factory() as s:
        u = User(status="active", email="u@ki.se")
        s.add(u)
        s.commit()
        return u.id


def test_resolve_readonly_does_not_persist(pg_session_factory):
    uid = _seed(pg_session_factory)
    with pg_session_factory() as s:
        resolve_profile_selection(s, user_id=uid, raw_profile="999999", persist=False)
        s.commit()
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id is None


def test_build_view_shape(pg_session_factory):
    uid = _seed(pg_session_factory)
    with pg_session_factory() as s:
        view = build_dashboard_view(
            s,
            user_id=uid,
            source=None,
            sort="newest",
            profile_id=None,
            limit=50,
            page=1,
            show_closed=False,
        )
    assert view.ranking == {"pending": 0, "failed": 0}
    assert view.pagination["page"] == 1
    assert isinstance(view.calls, list)
    assert "open_count" in view.summary
