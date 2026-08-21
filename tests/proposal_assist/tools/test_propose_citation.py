"""propose_citation_impl: verifies the citation at PROPOSE time (reference,
chunk, quote, anchor) and writes a pending citation_insert proposed_change.
No git commit (unlike insert_citation) — propose writes only a DB row."""

import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from call_manager.config import Settings
from call_manager.db.models import ProposedChange, Reference, ReferenceChunk
from call_manager.proposal_assist.port_impl import build_ports
from call_manager.proposal_assist.ports.errors import (
    BUSY_ERROR_MESSAGE,
    DELETED_ERROR_MESSAGE,
    CommitMutexTimeout,
    EffectRejected,
    WorkspaceGenerationStale,
)
from call_manager.proposal_assist.tools.citations import propose_citation_impl
from call_manager.proposal_assist.tools.proposal import (
    PROPOSAL_FILENAME,
    read_proposal_impl,
)
from call_manager.proposal_assist.tools.registry import ToolContext, snapshot_invocation
from tests.proposal_assist.conftest import make_workspace

EMBED_DIM = 1024
CHUNK_TEXT = (
    "Adjuvant therapy reduces recurrence by 30% in the treated cohort. "
    "Secondary endpoints were not statistically significant."
)
ANCHOR = "recurrence is reduced substantially"
PROPOSAL = "preamble\n\n## Methods\nWe argue that " + ANCHOR + " in our population.\n"


def _init_repo(repo_dir: Path, content: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


def _commit_count(repo_dir: Path) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return int(out.stdout.strip())


def _seed_reference(pg_session_factory, workspace_id, *, chunks):
    with pg_session_factory() as s:
        ref = Reference(
            workspace_id=workspace_id,
            kind="paper",
            source_type="uploaded",
            filename="seed.pdf",
            storage_path="seed/seed.pdf",
            mime_type="application/pdf",
            metadata_status="approved",
        )
        s.add(ref)
        s.flush()
        rid = ref.id
        for idx, section_path, text in chunks:
            s.add(
                ReferenceChunk(
                    reference_id=rid,
                    workspace_id=workspace_id,
                    chunk_index=idx,
                    section_path=section_path,
                    text=text,
                    embedding=[0.0] * EMBED_DIM,
                    embedding_model="fake-embedder",
                )
            )
        s.commit()
    return rid


def _build_ctx(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    repo = Path(tmp_path) / str(wsid) / "repo"
    _init_repo(repo, PROPOSAL)
    ctx = ToolContext(
        workspace_id=wsid,
        repo_dir=repo,
        session_factory=pg_session_factory,
        settings=Settings(),
        ports=build_ports(pg_session_factory),
    )
    # Invocation.stamp() raises UnfencedInvocation on None; generation 0 is REAL.
    ctx.lifecycle_generation = 0
    return ctx, wsid, repo


@pytest.mark.asyncio
async def test_propose_citation_writes_row_and_maps_section(
    pg_session_factory, _settings_env, tmp_path
):
    ctx, wsid, repo = _build_ctx(pg_session_factory, tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    await read_proposal_impl(ctx)  # populate agent_view_head + sections
    before = _commit_count(repo)

    result = await propose_citation_impl(
        ctx,
        ports=ctx.ports,
        invocation=snapshot_invocation(ctx),
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
        stance="supports",
    )

    assert "error" not in result, result
    assert result["status"] == "pending"
    # No git commit — propose writes only a DB row.
    assert _commit_count(repo) == before

    with pg_session_factory() as s:
        pc = s.get(ProposedChange, uuid.UUID(result["proposed_change_id"]))
        assert pc.kind == "citation_insert"
        # The anchor falls in section 1 (## Methods), so the row is section-keyed there.
        assert pc.section_index == 1
        assert pc.payload["reference_id"] == str(rid)
        assert pc.payload["anchor"] == ANCHOR
        assert pc.payload["chunk_index"] == 0
        assert pc.payload["evidence_quote"] == "reduces recurrence by 30%"
        assert pc.payload["stance"] == "supports"


@pytest.mark.asyncio
async def test_propose_citation_keys_section_skipping_marker_spans(
    pg_session_factory, _settings_env, tmp_path
):
    # Defect B7-1: the section lookup must use the SAME marker-skipping logic
    # (markers.anchor_occurrences) the uniqueness check uses. Here the anchor
    # text ("smith2021") also occurs INSIDE an existing marker in section 0
    # (## Background). A raw `.find()` would land on that marker-internal
    # occurrence and key the new row to section 0; the marker-skipping lookup
    # must instead find the PLAIN-TEXT occurrence in section 1 (## Methods).
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    repo = Path(tmp_path) / str(wsid) / "repo"
    # The marker's visible citekey contains the anchor substring "smith2021".
    proposal = (
        "preamble\n\n"
        "## Background\n"
        "Prior work [@smith2021]<!-- ev:ev-abc123 --> established the baseline.\n\n"
        "## Methods\n"
        "We extend smith2021 in our population.\n"
    )
    _init_repo(repo, proposal)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    ctx = ToolContext(
        workspace_id=wsid,
        repo_dir=repo,
        session_factory=pg_session_factory,
        settings=Settings(),
        ports=build_ports(pg_session_factory),
    )
    ctx.lifecycle_generation = 0
    await read_proposal_impl(ctx)

    result = await propose_citation_impl(
        ctx,
        ports=ctx.ports,
        invocation=snapshot_invocation(ctx),
        reference_id=str(rid),
        anchor="smith2021",
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
        stance="supports",
    )
    assert "error" not in result, result

    with pg_session_factory() as s:
        pc = s.get(ProposedChange, uuid.UUID(result["proposed_change_id"]))
        # Section 0 = preamble, 1 = ## Background, 2 = ## Methods. The plain-text
        # anchor lives in ## Methods (section 2), NOT the marker in Background.
        assert pc.section_index == 2, (
            f"row mis-keyed to section {pc.section_index} (raw .find landed in a marker span)"
        )


@pytest.mark.asyncio
async def test_propose_citation_bad_quote_rejected_no_commit(
    pg_session_factory, _settings_env, tmp_path
):
    ctx, wsid, repo = _build_ctx(pg_session_factory, tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    await read_proposal_impl(ctx)
    before = _commit_count(repo)

    result = await propose_citation_impl(
        ctx,
        ports=ctx.ports,
        invocation=snapshot_invocation(ctx),
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote="this span is not in the chunk at all",
        stance="supports",
    )
    assert "error" in result
    assert _commit_count(repo) == before  # nothing committed
    with pg_session_factory() as s:
        from sqlalchemy import select

        rows = s.scalars(select(ProposedChange).where(ProposedChange.workspace_id == wsid)).all()
        assert rows == []  # no pending row written for an unverifiable citation


@pytest.mark.asyncio
async def test_propose_citation_bad_anchor_rejected(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid, repo = _build_ctx(pg_session_factory, tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    await read_proposal_impl(ctx)
    result = await propose_citation_impl(
        ctx,
        ports=ctx.ports,
        invocation=snapshot_invocation(ctx),
        reference_id=str(rid),
        anchor="this anchor is nowhere in the proposal",
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_propose_citation_anchor_absent_from_read_view_rejected(
    pg_session_factory, _settings_env, tmp_path
):
    # FIX 2 regression: if the anchor exists in the LIVE on-disk proposal
    # (step 5 passes) but was NOT present in the agent's read-time snapshot
    # (anchor_offset == -1), the offset walk silently collapses section_index
    # to 0 (preamble), writing a row keyed to the WRONG section. The fix must
    # detect offset == -1 and return the same "read_proposal first" error,
    # writing NO ProposedChange row.
    from sqlalchemy import select

    ctx, wsid, repo = _build_ctx(pg_session_factory, tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    # 1. Populate the agent's view (head + sections) via read_proposal.
    await read_proposal_impl(ctx)

    # 2. After the read, doctor ctx.agent_view_proposal to a string that LACKS
    #    the anchor, while the LIVE on-disk proposal still contains it (the
    #    anchor was introduced on disk by a human after the agent read).
    ctx.agent_view_proposal = "preamble only, no anchor here\n"

    result = await propose_citation_impl(
        ctx,
        ports=ctx.ports,
        invocation=snapshot_invocation(ctx),
        reference_id=str(rid),
        anchor=ANCHOR,  # ANCHOR lives in the live on-disk proposal
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
        stance="supports",
    )

    assert "error" in result, f"expected error, got {result}"
    assert "read_proposal" in result["error"], result["error"]

    # No ProposedChange row written (the citation was refused, not mis-keyed).
    with pg_session_factory() as s:
        rows = s.scalars(select(ProposedChange).where(ProposedChange.workspace_id == wsid)).all()
        assert rows == [], f"expected no rows, got {len(rows)}"


class _RaisingEffects:
    """An Effects whose propose_citation_insert raises one chosen port error."""

    def __init__(self, exc):
        self._exc = exc

    def propose_citation_insert(self, stamp, cmd):
        raise self._exc


class _Ports:
    def __init__(self, effects):
        self.effects = effects


# Verified against ports/errors.py:27-36 and :63. WorkspaceGenerationStale requires BOTH
# keyword arguments — WorkspaceGenerationStale(wid) raises TypeError at COLLECTION, so a
# plan that guesses here does not even produce a red test. CommitMutexTimeout is a plain
# RuntimeError subclass and takes no arguments. EffectRejected's reason is the exact
# string returned to the agent.
_WID = uuid.uuid4()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (WorkspaceGenerationStale(_WID, expected=0, actual=1), DELETED_ERROR_MESSAGE),
        (CommitMutexTimeout(), BUSY_ERROR_MESSAGE),
        (
            EffectRejected("this workspace is no longer accepting proposed changes"),
            "this workspace is no longer accepting proposed changes",
        ),
    ],
)
async def test_propose_citation_answers_every_port_error_as_an_error_dict(
    pg_session_factory, _settings_env, tmp_path, exc, expected
):
    """The three arms Task 3 adds. ports/effects.py's Protocol docstring says a tool
    catching only the first two DOWNGRADES a transient busy into "internal error",
    because _safe_tool turns every unnamed exception into that — so CommitMutexTimeout
    is the arm most worth pinning, and the one no existing test covered.

    propose_citation_impl reaches the port only AFTER its reads succeed, so this needs
    the real seeded workspace + Reference + chunk + agent_view snapshot — the
    happy-path arrangement, with the port swapped for one that raises.
    """
    ctx, wsid, repo = _build_ctx(pg_session_factory, tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    await read_proposal_impl(ctx)

    out = await propose_citation_impl(
        ctx,
        ports=_Ports(_RaisingEffects(exc)),
        invocation=snapshot_invocation(ctx),
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
        stance="supports",
    )
    assert out == {"error": expected}


@pytest.mark.asyncio
async def test_a_tombstoned_reference_is_refused_and_writes_nothing(
    pg_session_factory, _settings_env, tmp_path
):
    """The check most likely to be lost, tested by BEHAVIOUR.

    ReferenceView.is_tombstoned exists so the tool can tell 'no such reference' from
    'tombstoned, cannot be cited' — two different messages to the agent. A conversion
    that reads only `is None` collapses them, and a source grep for the message would be
    satisfied by the COMMENT explaining it. This project has shipped a
    comment-satisfiable test twice; do not make it three.

    Seed a real tombstoned Reference, call the tool, assert the exact string AND that no
    ProposedChange row landed.
    """
    # The REAL context FIRST — it creates the workspace and returns its id. _build_ctx
    # gives a real repo, a live session_factory, Settings() and ports; _ctx_and_invocation
    # is NOT usable here: its ToolContext has repo_dir=None, session_factory=None and no
    # read snapshot, so the tool answers "call read_proposal first" and never reaches the
    # tombstone branch.
    ctx, ws_id, repo = _build_ctx(pg_session_factory, tmp_path)

    # _seed_reference is the house way to build a Reference plus its chunks. f3b_seeds.py
    # has NO Reference builder.
    ref_id = _seed_reference(pg_session_factory, ws_id, chunks=[(0, "s", "quote text")])
    with pg_session_factory() as sess:
        sess.get(Reference, ref_id).tombstoned_at = datetime.now(UTC)
        sess.commit()

    await read_proposal_impl(ctx)          # populates ctx.agent_view_* — required
    # _build_ctx sets lifecycle_generation=0 (Invocation.stamp() raises UnfencedInvocation
    # on None; generation 0 is REAL, so it tests for None not falsiness).
    invocation = snapshot_invocation(ctx)

    out = await propose_citation_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=invocation,
        reference_id=str(ref_id),
        anchor="anchor",
        chunk_index=0,
        evidence_quote="q",
    )
    assert out == {"error": "reference is tombstoned and cannot be cited"}

    with pg_session_factory() as sess:
        assert sess.query(ProposedChange).filter_by(workspace_id=ws_id).count() == 0
