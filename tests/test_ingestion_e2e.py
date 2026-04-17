from datetime import UTC, datetime

from fastapi.testclient import TestClient

from call_monitor.auth.email import InMemoryEmailSender
from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import User
from call_monitor.ingestion.alerts import ConsecutiveFailureTracker
from call_monitor.ingestion.orchestrator import run_ingestion
from call_monitor.main import create_app
from call_monitor.sources.types import NormalizedCall


class _FakeAdapter:
    slug = "vinnova"

    def fetch_new_calls(self, since):
        yield NormalizedCall(
            external_id="E2E-1",
            url="https://example.org/e2e/1",
            title="End-to-end call one",
            published_at=datetime(2026, 4, 10, tzinfo=UTC),
        )
        yield NormalizedCall(
            external_id="E2E-2",
            url="https://example.org/e2e/2",
            title="End-to-end call two",
            published_at=datetime(2026, 4, 12, tzinfo=UTC),
        )


def test_end_to_end_ingest_and_browse(monkeypatch, pg_url, pg_session_factory):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()

    # 1. Ingest
    sender = InMemoryEmailSender()
    alerter = ConsecutiveFailureTracker(threshold=3, sender=sender, admin_email=None)
    result = run_ingestion(
        session_factory=pg_session_factory, adapter=_FakeAdapter(), alerter=alerter
    )
    assert result.error is None
    assert result.inserted == 2

    # 2. Browse as a logged-in user
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))

    with client:
        r = client.get("/")
        assert r.status_code == 200
        assert "End-to-end call one" in r.text
        assert "End-to-end call two" in r.text
        # Newest first by published_at
        assert r.text.index("call two") < r.text.index("call one")
