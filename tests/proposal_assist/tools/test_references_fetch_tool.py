"""fetch_reference MCP tool — per-turn cap is the load-bearing piece.
Uses atomic UPDATE turn_jobs SET fetch_count = fetch_count + 1
WHERE workspace_id = ? AND status = 'running' AND fetch_count < 3
RETURNING fetch_count."""

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
from call_manager.proposal_assist.references.embed import FakeEmbedder
from call_manager.proposal_assist.tools.references import fetch_reference_impl
from call_manager.proposal_assist.tools.registry import ToolContext

# Every test reaches the SSRF resolver through fetch_reference_impl;
# apply the shared fake_public_dns fixture from tests/proposal_assist/
# conftest.py to all tests in this module.
pytestmark = pytest.mark.usefixtures("fake_public_dns")


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
async def test_fetch_reference_increments_fetch_count(pg_session_factory, tmp_path, httpx_mock):
    """Successful fetch increments turn_jobs.fetch_count and inserts
    a Reference row. Injects FakeEmbedder so ingest doesn't try to hit
    Ollama."""
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
    out = await fetch_reference_impl(ctx, input="2401.12345", embedder=FakeEmbedder())
    assert "error" not in out, out
    assert "reference_id" in out

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == jobid))
        ref = s.scalar(select(Reference).where(Reference.workspace_id == wsid))
    assert job.fetch_count == 1
    assert ref is not None
    assert ref.source_type == "arxiv"


@pytest.mark.asyncio
async def test_fetch_reference_4th_attempt_returns_cap_error(
    pg_session_factory, tmp_path, httpx_mock
):
    """3 successful fetches → 4th attempt returns cap error WITHOUT
    incrementing further (atomic WHERE fetch_count < 3). FakeEmbedder
    keeps ingest off the Ollama dependency."""
    from io import BytesIO

    from reportlab.pdfgen.canvas import Canvas

    buf = BytesIO()
    c = Canvas(buf)
    c.drawString(72, 720, "x")
    c.save()
    pdf_bytes = buf.getvalue()

    wsid, jobid = _seed_running_turn(pg_session_factory)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    embedder = FakeEmbedder()

    for i, ax_id in enumerate(["2401.00001", "2401.00002", "2401.00003"]):
        httpx_mock.add_response(
            url=f"https://arxiv.org/pdf/{ax_id}.pdf",
            content=pdf_bytes,
        )
        out = await fetch_reference_impl(ctx, input=ax_id, embedder=embedder)
        assert "error" not in out, (i, out)

    # 4th attempt: cap hit BEFORE any HTTP — no mock registered because
    # the atomic UPDATE returns 0 rows and we short-circuit.
    out = await fetch_reference_impl(ctx, input="2401.00004", embedder=embedder)
    assert "error" in out
    assert "cap" in out["error"].lower() or "3" in out["error"]

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == jobid))
    assert job.fetch_count == 3  # not 4


@pytest.mark.asyncio
async def test_fetch_reference_refunds_count_on_fetch_error(
    pg_session_factory, tmp_path, httpx_mock
):
    """Regression: the per-turn slot is reserved (fetch_count += 1)
    BEFORE the fetch. On a transient FetchError the slot was never spent
    on usable content, so it must be refunded — otherwise a single flaky
    URL permanently shrinks the agent's per-turn budget."""
    # Server returns 500 → fetch_bytes raises FetchError → tool refunds.
    httpx_mock.add_response(
        url="https://arxiv.org/pdf/2401.99999.pdf",
        status_code=500,
    )

    wsid, jobid = _seed_running_turn(pg_session_factory)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    out = await fetch_reference_impl(ctx, input="2401.99999", embedder=FakeEmbedder())
    assert "error" in out
    assert "fetch failed" in out["error"].lower()
    # The reported count reflects the refund.
    assert out["fetch_count"] == 0

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == jobid))
    # Slot refunded: the failed fetch did not burn the budget.
    assert job.fetch_count == 0


@pytest.mark.asyncio
async def test_fetch_reference_refund_keeps_other_successful_fetches(
    pg_session_factory, tmp_path, httpx_mock
):
    """A refund must only undo the just-consumed slot, not zero out
    earlier successful fetches in the same turn."""
    from io import BytesIO

    from reportlab.pdfgen.canvas import Canvas

    buf = BytesIO()
    c = Canvas(buf)
    c.drawString(72, 720, "ok")
    c.save()
    pdf_bytes = buf.getvalue()

    wsid, jobid = _seed_running_turn(pg_session_factory)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    embedder = FakeEmbedder()

    # One good fetch → count goes to 1.
    httpx_mock.add_response(url="https://arxiv.org/pdf/2401.00010.pdf", content=pdf_bytes)
    out_ok = await fetch_reference_impl(ctx, input="2401.00010", embedder=embedder)
    assert "error" not in out_ok, out_ok

    # One bad fetch → count goes to 2, then refunded back to 1.
    httpx_mock.add_response(url="https://arxiv.org/pdf/2401.00011.pdf", status_code=503)
    out_bad = await fetch_reference_impl(ctx, input="2401.00011", embedder=embedder)
    assert "error" in out_bad
    assert out_bad["fetch_count"] == 1

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == jobid))
    assert job.fetch_count == 1


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
