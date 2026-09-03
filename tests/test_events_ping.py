from fastapi.testclient import TestClient

from call_manager.main import create_app


def _app(monkeypatch, pg_url, mode):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "umu.se",
        "APP_BASE_URL": "http://testserver",
        "SPA_MODE": mode,
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


def test_ping_dev_gated(monkeypatch, pg_url, pg_session_factory):
    c = TestClient(_app(monkeypatch, pg_url, "disabled"))
    assert c.get("/events/ping").status_code == 404


def test_ping_emits_named_json_events_with_ids(monkeypatch, pg_url, pg_session_factory):
    c = TestClient(_app(monkeypatch, pg_url, "dev"))
    r = c.get("/events/ping?max=2")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"  # anti-buffering (Vite proxy hop)
    assert r.headers["x-accel-buffering"] == "no"
    body = r.text
    assert "event: ping" in body and "id: 0" in body and '"n": 0' in body


def test_ping_honors_since_for_fresh_connect_catch_up(monkeypatch, pg_url, pg_session_factory):
    # Fresh-connect catch-up: ?since=4 resumes at id 5 (no Last-Event-ID on a new EventSource).
    c = TestClient(_app(monkeypatch, pg_url, "dev"))
    body = c.get("/events/ping?max=2&since=4").text
    assert "id: 5" in body and "id: 0" not in body


def test_ping_last_event_id_wins_over_since(monkeypatch, pg_url, pg_session_factory):
    # In-session reconnect: Last-Event-ID overrides ?since.
    c = TestClient(_app(monkeypatch, pg_url, "dev"))
    body = c.get("/events/ping?max=1&since=4", headers={"last-event-id": "9"}).text
    assert "id: 10" in body
