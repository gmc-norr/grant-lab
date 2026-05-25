"""POST /workspaces/<id>/references/fetch — UI counterpart to the
agent's fetch_reference MCP tool. Owner-only; same auth as upload."""

import socket
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Reference, User, Workspace
from call_manager.main import create_app


@pytest.fixture
def fake_public_dns(monkeypatch):
    """pytest-httpx mocks at the httpx transport layer, NOT the socket
    layer, so the SSRF resolver still attempts real DNS for hosts
    referenced by tests. Pretend the known mocked hostnames resolve to
    a real public IP (8.8.8.8). Other hostnames fall through.

    We use 8.8.8.8 rather than a TEST-NET address because Python's
    ipaddress.is_private treats the documentation ranges as private,
    which would defeat the SSRF check."""
    real_getaddrinfo = socket.getaddrinfo

    _fake_hosts = {
        "api.crossref.org",
        "eutils.ncbi.nlm.nih.gov",
        "arxiv.org",
        "example.com",
    }

    def fake_getaddrinfo(host, *args, **kwargs):
        if host in _fake_hosts or host.endswith(".example.com"):
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("8.8.8.8", 0),
                )
            ]
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return fake_getaddrinfo


def _build_app(monkeypatch, pg_url, tmp_path):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))
    return create_app()


def _authed(app, *, user_id):
    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    signer: SessionSigner = app.state.session_signer
    client.cookies.set("session", signer.make(user_id=user_id))
    return client


def _seed_owner_and_workspace(session, *, email=None):
    owner = User(email=email or f"owner-{uuid.uuid4()}@ki.se")
    session.add(owner)
    session.flush()
    ws = Workspace(owner_id=owner.id, title="T")
    session.add(ws)
    session.commit()
    return owner.id, ws.id


def _minimal_pdf(text: str) -> bytes:
    """One-page PDF helper from Slice 1 T5 tests."""
    from io import BytesIO

    from reportlab.pdfgen.canvas import Canvas

    buf = BytesIO()
    c = Canvas(buf)
    c.drawString(72, 720, text)
    c.save()
    return buf.getvalue()


@pytest.mark.httpx_mock(should_mock=lambda req: req.url.host != "localhost")
def test_fetch_arxiv_creates_row(
    monkeypatch, pg_url, pg_session_factory, tmp_path, httpx_mock, fake_public_dns
):
    """End-to-end: POST /fetch with an arXiv ID; the fetcher backend
    returns mock PDF bytes; row + chunks are created via Slice 1's
    ingest pipeline. Embedder calls to localhost:11434 (Ollama) pass
    through to the real local server so Phase 2 can complete."""
    httpx_mock.add_response(
        url="https://arxiv.org/pdf/2401.12345.pdf",
        content=_minimal_pdf("arXiv paper body"),
    )

    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)

    client = _authed(app, user_id=uid)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references/fetch",
            data={"input": "2401.12345", "kind": "paper"},
        )
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200, resp.text
    with pg_session_factory() as s:
        refs = list(s.scalars(select(Reference).where(Reference.workspace_id == wsid)))
    assert len(refs) == 1
    assert refs[0].source_type == "arxiv"
    assert refs[0].source_uri == "2401.12345"
    assert refs[0].text_status == "done"


def test_fetch_with_invalid_input_returns_400(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """Garbage input → 400 with clear error message."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
    client = _authed(app, user_id=uid)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references/fetch",
            data={"input": "just some text", "kind": "paper"},
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 400
    assert "unrecognized" in resp.text.lower() or "invalid" in resp.text.lower()


def test_fetch_returns_403_for_non_owner(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """Same owner-only policy as upload (B7 'shared-read, single-write')."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        _, wsid = _seed_owner_and_workspace(s)
        other = User(email=f"other-{uuid.uuid4()}@ki.se")
        s.add(other)
        s.commit()
        other_id = other.id
    client = _authed(app, user_id=other_id)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references/fetch",
            data={"input": "10.1234/foo", "kind": "paper"},
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 403


def test_fetch_with_fetch_error_returns_502(
    monkeypatch, pg_url, pg_session_factory, tmp_path, httpx_mock, fake_public_dns
):
    """Fetcher failure (404, paywall, SSRF reject) → 502 with the
    error message. No row inserted."""
    httpx_mock.add_response(
        url="https://arxiv.org/pdf/9999.99999.pdf",
        status_code=404,
    )
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
    client = _authed(app, user_id=uid)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references/fetch",
            data={"input": "9999.99999", "kind": "paper"},
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 502
    # No row inserted.
    with pg_session_factory() as s:
        refs = list(s.scalars(select(Reference).where(Reference.workspace_id == wsid)))
    assert refs == []
