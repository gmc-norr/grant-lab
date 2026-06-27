"""Shared helpers for reference-permission web tests."""

from fastapi.testclient import TestClient

from call_manager.db.models import WorkspaceViewer
from call_manager.embeddings.client import EmbedResult
from call_manager.main import create_app


def build_app(monkeypatch, pg_url, tmp_path, pg_session_factory):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))
    app = create_app()
    app.state.session_factory = pg_session_factory
    return app


def add_member(session, *, workspace_id, user):
    """Insert a workspace_viewers row (a 'member' for the references feature)."""
    session.add(WorkspaceViewer(workspace_id=workspace_id, user_id=user.id))
    session.commit()


def authed_client(app, *, user_id):
    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    client.cookies.set("session", app.state.session_signer.make(user_id=user_id))
    return client


class FakeEmbedClient:
    """Stand-in for OllamaEmbedClient so ingest runs without Ollama."""

    model = "fake-embedder"

    def embed(self, text):
        v = [0.0] * 1024
        if text:
            v[0] = (len(text) % 1024) / 1024.0
        return EmbedResult(vector=v, embedded_chars=len(text))
