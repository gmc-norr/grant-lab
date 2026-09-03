import pytest

from call_manager.proposal_assist.port_impl import build_ports
from call_manager.proposal_assist.references.admission import IngestLimiter, OverCapacity
from call_manager.proposal_assist.tools.registry import snapshot_invocation


def test_limiter_admits_up_to_capacity():
    lim = IngestLimiter(capacity=1)
    with lim.slot(), pytest.raises(OverCapacity):  # noqa: SIM117
        with lim.slot():
            pass


def test_limiter_releases_slot():
    lim = IngestLimiter(capacity=1)
    with lim.slot():
        pass
    with lim.slot():  # freed
        pass


# ---------------------------------------------------------------------------
# MCP fetch tool busy test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_reference_returns_busy_when_limiter_exhausted(pg_session_factory, tmp_path):
    """With an exhausted limiter on ctx.reference_ingest_limiter, fetch_reference_impl
    returns a busy error dict AND refunds the per-turn fetch slot (fetch_count stays 0)."""
    import uuid as _uuid
    from datetime import UTC, datetime

    from sqlalchemy import select

    from call_manager.config import Settings
    from call_manager.db.models import ChatTurn, TurnJob, User, Workspace
    from call_manager.proposal_assist.references.embed import FakeEmbedder
    from call_manager.proposal_assist.tools.references import fetch_reference_impl
    from call_manager.proposal_assist.tools.registry import ToolContext

    # Seed workspace + running TurnJob.
    with pg_session_factory() as s:
        owner = User(email=f"o-{_uuid.uuid4()}@umu.se")
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
        wsid = ws.id
        job_id = job.id

    # Build an exhausted limiter (capacity=1, slot already taken).
    lim = IngestLimiter(capacity=1)
    assert lim.try_acquire() is True  # hold the only slot

    settings = Settings.model_construct(workspaces_dir=str(tmp_path))
    ctx = ToolContext(
        workspace_id=wsid,
        repo_dir=tmp_path / str(wsid) / "repo",
        session_factory=pg_session_factory,
        settings=settings,
        reference_ingest_limiter=lim,
        lifecycle_generation=0,
        active_turn_job_id=job_id,
    )

    _ports = build_ports(pg_session_factory)
    _invocation = snapshot_invocation(ctx)
    out = await fetch_reference_impl(
        ctx,
        ports=_ports,
        invocation=_invocation,
        input="2401.12345",
        embedder=FakeEmbedder(),
    )

    # Tool must return a busy error.
    assert "error" in out, out
    assert "busy" in out["error"].lower() or "retry" in out["error"].lower()

    # The per-turn fetch count must be refunded (not permanently consumed).
    with pg_session_factory() as s:
        job_row = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
    assert job_row.fetch_count == 0, (
        f"fetch_count should be 0 (refunded), got {job_row.fetch_count}"
    )

    # Release the held slot so the semaphore is clean.
    lim.release()
