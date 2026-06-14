from unittest.mock import patch

from fastapi.testclient import TestClient

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Profile, ProfileKind, User
from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def test_editing_only_areas_triggers_reembed(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(status="active", email="root@ki.se", is_admin=True)
        s.add(u)
        s.flush()
        p = Profile(
            name="P",
            kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            description="Fixed desc",
            structured={"areas": ["old area"]},
            created_by=u.id,
        )
        s.add(p)
        s.commit()
        pid, uid = p.id, u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))

    # Patch the background driver so we can assert refresh_embedding=True even
    # though description is unchanged (only areas changed).
    with patch("call_manager.web.routes.profiles._refresh_and_backfill_in_background") as bg:
        with client:
            r = client.post(
                f"/profiles/{pid}/edit",
                data={
                    "description": "Fixed desc",
                    "areas": "new area",
                    "must_include": "",
                    "hard_exclusions": "",
                    "budget_currency": "",
                    "level": "group",
                },
                headers={"Origin": "http://testserver"},
                follow_redirects=False,
            )
        assert r.status_code == 303
    kwargs = bg.call_args.kwargs
    assert kwargs["refresh_embedding"] is True
