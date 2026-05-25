"""fetch_reference MCP tool — per-turn cap is the load-bearing piece.
Uses atomic UPDATE turn_jobs SET fetch_count = fetch_count + 1
WHERE workspace_id = ? AND status = 'running' AND fetch_count < 3
RETURNING fetch_count."""

import socket
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from call_manager.config import Settings
from call_manager.db.models import (
    ChatTurn,
    Reference,
    TurnJob,
    User,
    Workspace,
)
from call_manager.proposal_assist.tools.references import fetch_reference_impl
from call_manager.proposal_assist.tools.registry import ToolContext


@pytest.fixture(autouse=True)
def fake_public_dns(monkeypatch):
    """pytest-httpx mocks at the httpx transport layer, NOT the socket
    layer, so the SSRF resolver still attempts real DNS for hosts
    referenced by tests. Pretend arxiv.org resolves to a real public
    IP (8.8.8.8). Other hostnames fall through."""
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


def _make_ctx(pg_session_factory, tmp_path, workspace_id):
    settings = Settings.model_construct(workspaces_dir=str(tmp_path))
    return ToolContext(
        workspace_id=workspace_id,
        repo_dir=tmp_path / str(workspace_id) / "repo",
        session_factory=pg_session_factory,
        settings=settings,
    )


def _seed_running_turn(pg_session_factory):
    """Create a workspace + a running TurnJob (fetch_count starts at 0)."""
    with pg_session_factory() as s:
        owner = User(email=f"o-{uuid.uuid4()}@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="T")
        s.add(ws)
        s.flush()
        user_turn = ChatTurn(workspace_id=ws.id, role="user", content="hi")
        s.add(user_turn)
        s.flush()
        job = TurnJob(
            workspace_id=ws.id,
            user_turn_id=user_turn.id,
            status="running",
            started_at=datetime.now(UTC),
            submitted_by_user_id=owner.id,
        )
        s.add(job)
        s.commit()
        return ws.id, job.id


@pytest.mark.asyncio
async def test_fetch_reference_no_running_turn_returns_error(pg_session_factory, tmp_path):
    """The atomic UPDATE returns 0 rows if there's no running turn
    for this workspace (e.g., the tool was invoked from a stale
    handle); we return a clear error."""
    with pg_session_factory() as s:
        owner = User(email=f"o-{uuid.uuid4()}@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="T")
        s.add(ws)
        s.commit()
        wsid = ws.id

    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    out = await fetch_reference_impl(ctx, input="2401.12345")
    assert "error" in out
    assert "no running turn" in out["error"].lower() or "cap" in out["error"].lower()


@pytest.mark.asyncio
@pytest.mark.httpx_mock(should_mock=lambda req: req.url.host != "localhost")
async def test_fetch_reference_increments_fetch_count(pg_session_factory, tmp_path, httpx_mock):
    """Successful fetch increments turn_jobs.fetch_count and inserts
    a Reference row."""
    from io import BytesIO

    from reportlab.pdfgen.canvas import Canvas

    buf = BytesIO()
    c = Canvas(buf)
    c.drawString(72, 720, "arxiv test")
    c.save()
    pdf_bytes = buf.getvalue()

    httpx_mock.add_response(
        url="https://arxiv.org/pdf/2401.12345.pdf",
        content=pdf_bytes,
    )

    wsid, jobid = _seed_running_turn(pg_session_factory)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    out = await fetch_reference_impl(ctx, input="2401.12345")
    assert "error" not in out, out
    assert "reference_id" in out

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == jobid))
        ref = s.scalar(select(Reference).where(Reference.workspace_id == wsid))
    assert job.fetch_count == 1
    assert ref is not None
    assert ref.source_type == "arxiv"


@pytest.mark.asyncio
@pytest.mark.httpx_mock(should_mock=lambda req: req.url.host != "localhost")
async def test_fetch_reference_4th_attempt_returns_cap_error(
    pg_session_factory, tmp_path, httpx_mock
):
    """3 successful fetches → 4th attempt returns cap error WITHOUT
    incrementing further (atomic WHERE fetch_count < 3)."""
    from io import BytesIO

    from reportlab.pdfgen.canvas import Canvas

    buf = BytesIO()
    c = Canvas(buf)
    c.drawString(72, 720, "x")
    c.save()
    pdf_bytes = buf.getvalue()

    wsid, jobid = _seed_running_turn(pg_session_factory)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)

    for i, ax_id in enumerate(["2401.00001", "2401.00002", "2401.00003"]):
        httpx_mock.add_response(
            url=f"https://arxiv.org/pdf/{ax_id}.pdf",
            content=pdf_bytes,
        )
        out = await fetch_reference_impl(ctx, input=ax_id)
        assert "error" not in out, (i, out)

    # 4th attempt: cap hit BEFORE any HTTP — no mock registered because
    # the atomic UPDATE returns 0 rows and we short-circuit.
    out = await fetch_reference_impl(ctx, input="2401.00004")
    assert "error" in out
    assert "cap" in out["error"].lower() or "3" in out["error"]

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == jobid))
    assert job.fetch_count == 3  # not 4


@pytest.mark.asyncio
async def test_fetch_reference_with_invalid_input_returns_error(pg_session_factory, tmp_path):
    """Unrecognized input → dispatcher raises ValueError → tool returns
    a clear error. fetch_count NOT incremented (defensive: bad input
    shouldn't cost the budget)."""
    wsid, jobid = _seed_running_turn(pg_session_factory)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    out = await fetch_reference_impl(ctx, input="just some text")
    assert "error" in out

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == jobid))
    assert job.fetch_count == 0
