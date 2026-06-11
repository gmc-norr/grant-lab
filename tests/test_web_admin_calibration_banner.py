from fastapi.testclient import TestClient

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import User
from call_manager.main import create_app


def _app(monkeypatch, pg_url, *, model: str, pregate: str | None = None):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("EMBEDDING_MODEL", model)
    if pregate is not None:
        monkeypatch.setenv("EMBEDDING_PREGATE_THRESHOLD", pregate)
    return create_app()


def _admin_client(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="root@ki.se", is_admin=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_banner_shown_for_unknown_model_with_default_thresholds(
    monkeypatch, pg_url, pg_session_factory
):
    app = _app(monkeypatch, pg_url, model="mxbai-embed-large")
    assert app.state.embedding_calibration_warning is not None
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    assert "EMBEDDING_HARD_FAIL_THRESHOLD" in r.text


def test_no_banner_for_known_model(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url, model="bge-m3")
    assert app.state.embedding_calibration_warning is None
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    assert "uncalibrated" not in r.text.lower()
