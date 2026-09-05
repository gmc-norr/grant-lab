"""Shared helpers for reference-permission web tests."""

from contextlib import suppress

from fastapi.testclient import TestClient

from grant_lab.db.models import WorkspaceViewer
from grant_lab.embeddings.client import EmbedResult
from grant_lab.main import create_app


def build_app(monkeypatch, pg_url, tmp_path, pg_session_factory):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))
    app = create_app()
    app.state.session_factory = pg_session_factory
    return app


def add_member(session, *, workspace_id, user):
    """Insert a workspace_viewers row (a 'member' for the references feature)."""
    session.add(WorkspaceViewer(workspace_id=workspace_id, user_id=user.id))
    session.commit()


# Every authed_client() enters an app lifespan, and its ~60 call sites use the
# result inline (`authed_client(app, ...).post(...)`), so none of them can exit it.
# Unclosed, each one leaves a started APScheduler running for the rest of the
# session; its _analysis_drain_tick needs no app_loop, so it keeps draining the
# shared analysis queue and stealing jobs from unrelated tests, and it holds the
# single-instance advisory lock that test_scheduler_single_instance asserts is
# free. Rather than rewrite every call site, entered clients are registered here
# and closed by an autouse fixture in tests/conftest.py after each test.
_ENTERED: list[TestClient] = []


def authed_client(app, *, user_id):
    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    _ENTERED.append(client)
    client.cookies.set("session", app.state.session_signer.make(user_id=user_id))
    return client


def close_entered_clients() -> None:
    """Exit every lifespan authed_client opened, innermost first (LIFO): nested
    lifespans on one app must unwind in reverse. Never raises — a teardown error
    must not mask the test's own result."""
    while _ENTERED:
        client = _ENTERED.pop()
        with suppress(Exception):        # best-effort teardown
            client.__exit__(None, None, None)


class FakeEmbedClient:
    """Stand-in for OllamaEmbedClient so ingest runs without Ollama."""

    model = "fake-embedder"

    def embed(self, text):
        v = [0.0] * 1024
        if text:
            v[0] = (len(text) % 1024) / 1024.0
        return EmbedResult(vector=v, embedded_chars=len(text))
