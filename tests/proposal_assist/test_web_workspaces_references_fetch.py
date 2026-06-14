"""POST /workspaces/<id>/references/fetch — UI counterpart to the
agent's fetch_reference MCP tool. Owner-only; same auth as upload.

`fake_public_dns` lives in tests/proposal_assist/conftest.py; tests
that hit the SSRF resolver request it by name (kept non-autouse so
the 403/400 tests below don't pay the DNS monkeypatch cost)."""

import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Reference, User, Workspace
from call_manager.main import create_app

_ARXIV_ATOM_URL = re.compile(r"https://export\.arxiv\.org/api/query.*")
_EMPTY_ATOM_FEED = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'


@pytest.fixture(autouse=True)
def _mock_arxiv_atom(httpx_mock):
    """Register a reusable stub for the arXiv Atom metadata endpoint.

    fetch_arxiv() calls _fetch_arxiv_atom_meta() which GETs
    https://export.arxiv.org/api/query?id_list=<id> for every arXiv
    fetch.  The call is non-fatal (failures are caught + logged), but
    pytest-httpx errors at teardown for any outbound request that has no
    registered mock.  An empty <feed> (no <entry>) makes the metadata
    parse return nothing, keeping these tests focused on the
    fetch/ingest behaviour they assert.
    """
    httpx_mock.add_response(
        url=_ARXIV_ATOM_URL,
        text=_EMPTY_ATOM_FEED,
        is_reusable=True,
        is_optional=True,
    )


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
    owner = User(status="active", email=email or f"owner-{uuid.uuid4()}@ki.se")
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
        other = User(status="active", email=f"other-{uuid.uuid4()}@ki.se")
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
