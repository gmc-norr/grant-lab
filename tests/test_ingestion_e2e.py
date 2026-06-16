from datetime import UTC, datetime
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from call_manager.auth.email import InMemoryEmailSender
from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import User
from call_manager.ingestion.alerts import ConsecutiveFailureTracker
from call_manager.ingestion.orchestrator import run_ingestion
from call_manager.main import create_app
from call_manager.sources.types import NormalizedCall


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
    embed_client = MagicMock()
    embed_client.model = "bge-m3"
    embed_client.embed.return_value = [0.1] * 1024
    result = run_ingestion(
        session_factory=pg_session_factory,
        adapter=_FakeAdapter(),
        alerter=alerter,
        embed_client=embed_client,
    )
    assert result.error is None
    assert result.inserted == 2

    # 2. Browse as a logged-in user
    with pg_session_factory() as s:
        u = User(status="active", email="alice@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))

    # Browse via the JSON feed (the React SPA's data source; the Jinja `GET /`
    # dashboard was retired in Plan 3 Task 9).
    with client:
        r = client.get("/api/calls")
        assert r.status_code == 200
        titles = [item["title"] for item in r.json()["items"]]
    assert "End-to-end call one" in titles
    assert "End-to-end call two" in titles
    # Newest first by published_at: call two (Apr 12) before call one (Apr 10).
    assert titles.index("End-to-end call two") < titles.index("End-to-end call one")
