import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from call_manager.config import Settings
from call_manager.db.models import ProposedChange, User
from call_manager.proposal_assist.artifacts import Artifact, filename_for
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.kernel import kernel_accept_prose
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.port_impl import build_ports
from call_manager.proposal_assist.proposals.staleness import refresh_staleness
from call_manager.proposal_assist.tools.citations import propose_citation_impl
from call_manager.proposal_assist.tools.proposal import _read_artifact_impl, read_proposal_impl
from call_manager.proposal_assist.tools.registry import ToolContext, snapshot_invocation
from tests.proposal_assist.collab.test_collab_kernel_accept import (
    _git_head,
    _init_repo,
    _live_room,
    _mk_pc,
)
from tests.proposal_assist.conftest import make_user, make_workspace, stamp_for
from tests.proposal_assist.tools.test_propose_citation import (
    ANCHOR,
    CHUNK_TEXT,
    _seed_reference,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _ctx(wid, repo_dir, pg_session_factory, _settings_env):
    # _settings_env is received but not used directly — the parameter exists only
    # to force the Settings-env fixture side-effect (e.g. WORKSPACES_DIR env var)
    # before Settings() is instantiated below.
    ctx = ToolContext(
        workspace_id=wid, repo_dir=repo_dir,
        session_factory=pg_session_factory, settings=Settings(),
        ports=build_ports(pg_session_factory),
    )
    # Invocation.stamp() raises UnfencedInvocation on None; generation 0 is REAL.
    ctx.lifecycle_generation = 0
    return ctx


@pytest.mark.anyio
async def test_read_collab_on_returns_log_canonical(pg_session_factory, _settings_env, tmp_path):
    # Raw HEAD is TIGHT (no blank line after heading); the durable log holds the
    # normalized form. Collab-on read must return the normalized (log) content so
    # its section shas match the kernel's live.sha at accept.
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="canon-read@ki.se"))
        s.commit()
        wid = ws.id
    (tmp_path / filename_for(Artifact.DRAFT)).write_text("## A\nBody a.\n")
    norm = pm_to_markdown(markdown_to_pm("## A\nBody a.\n"))
    seed = pm_json_to_ydoc(markdown_to_pm(norm)).get_update()
    with pg_session_factory() as s:
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None, update=seed)
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()

    ctx = _ctx(wid, tmp_path, pg_session_factory, _settings_env)
    out = await _read_artifact_impl(
        ctx, filename=filename_for(Artifact.DRAFT),
        head_attr="agent_view_head", sections_attr="agent_view_sections",
        proposal_attr="agent_view_proposal",
    )
    assert out["content"] == norm
    # The captured base shas are the NORMALIZED section shas (match kernel live.sha).
    assert ctx.agent_view_sections == {sec.index: sec.sha for sec in split_sections(norm)}


@pytest.mark.anyio
async def test_read_flag_off_returns_raw_head(pg_session_factory, _settings_env, tmp_path):
    wid = uuid.uuid4()   # no DB write on the flag-off path → bare uuid is fine
    raw = "## A\nBody a.\n"  # tight — NOT normalized
    (tmp_path / filename_for(Artifact.DRAFT)).write_text(raw)
    ctx = _ctx(wid, tmp_path, pg_session_factory, _settings_env)   # collab NOT enabled
    out = await _read_artifact_impl(
        ctx, filename=filename_for(Artifact.DRAFT),
        head_attr="agent_view_head", sections_attr="agent_view_sections",
        proposal_attr="agent_view_proposal",
    )
    assert out["content"] == raw  # byte-for-byte raw HEAD (parity)


@pytest.mark.anyio
async def test_read_then_accept_end_to_end_not_false_stale(
    pg_session_factory, _settings_env, tmp_path
):
    # THE end-to-end C1 regression (spec Testing / Component 1): a base captured via
    # the REAL read path (_read_artifact_impl, collab-on) is ACCEPTED by the kernel,
    # not rejected as stale — the exact false-stale bug this slice fixes. Uses a
    # tight raw HEAD so the un-fixed read (raw base) would be flagged stale.
    with pg_session_factory() as s:
        o = make_user(s, email="canon-e2e@ki.se")
        ws = make_workspace(s, owner=o, current_phase="active")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, "## A\nBody a.\n")           # tight (non-normalized) HEAD
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    with pg_session_factory() as s:
        room_doc = _live_room(s, wid, str(tmp_path))   # hydrates normalized log
    # Agent reads via the real tool path → captures the (normalized) base.
    ctx = _ctx(wid, repo, pg_session_factory, _settings_env)
    await _read_artifact_impl(
        ctx, filename=filename_for(Artifact.DRAFT),
        head_attr="agent_view_head", sections_attr="agent_view_sections",
        proposal_attr="agent_view_proposal",
    )
    base_sha = ctx.agent_view_sections[1]          # section 1 = "## A"
    with pg_session_factory() as s:
        pc_id = _mk_pc(s, wid, section_index=1, new_text="## A\n\nAccepted body.\n",
                       base_commit_sha=_git_head(repo), base_section_sha=base_sha)
    with pg_session_factory() as s:
        res = kernel_accept_prose(s, stamp=stamp_for(s, wid), room_doc=room_doc,
                                  proposed_change_id=pc_id, workspace_id=wid,
                                  approver=s.get(User, oid), workspaces_dir=str(tmp_path))
        s.commit()
        assert res.pc.status == "accepted"        # NOT "stale"


def _seed_ws_with_proposal(pg_session_factory, tmp_path, *, collab, base_from):
    """Workspace + a pending prose proposal on section 1 (the '## A' section — its
    sha DIFFERS raw vs normalized; section 0 the empty-ish preamble would be
    identical and the test vacuous). Writes tight raw HEAD to the real workspace
    repo dir (<tmp_path>/<wid>/repo); seeds the normalized log when collab=True."""
    raw = "Preamble.\n\n## A\nBody a.\n"           # tight: no blank line after '##'
    norm = pm_to_markdown(markdown_to_pm(raw))
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email=f"stale-{uuid.uuid4().hex[:8]}@ki.se"))
        s.commit()
        wid = ws.id
    repo_dir = workspace_repo_dir(str(tmp_path), wid)
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / filename_for(Artifact.DRAFT)).write_text(raw)
    base_md = norm if base_from == "norm" else raw
    base_sha = split_sections(base_md)[1].sha        # section 1 = "## A"
    with pg_session_factory() as s:
        if collab:
            seed = pm_json_to_ydoc(markdown_to_pm(norm)).get_update()
            store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None, update=seed)
            state.enable_collab(s, workspace_id=wid, artifact="draft")
        pc = ProposedChange(
            workspace_id=wid, target_artifact="draft", section_index=1,
            base_section_sha=base_sha, base_commit_sha="0" * 40,  # NOT NULL; sweep never reads it
            origin_kind="human",                                  # NOT NULL
            status="pending", kind="prose_edit", payload={"new_text": "## A\n\nEdited.\n"},
        )
        s.add(pc)
        s.commit()
        return wid, pc.id


def test_sweep_collab_on_does_not_false_flag_normalized_base(pg_session_factory, tmp_path):
    # Collab-on, base captured from the NORMALIZED log form → the sweep must NOT
    # flip it stale (raw HEAD differs from normalized, which is the #1 bug).
    ws_id, pc_id = _seed_ws_with_proposal(
        pg_session_factory, tmp_path, collab=True, base_from="norm"
    )
    with pg_session_factory() as s:
        refresh_staleness(session=s, workspace_id=ws_id, workspaces_dir=str(tmp_path))
        s.commit()
    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id).status == "pending"


def test_sweep_flag_off_uses_raw_head(pg_session_factory, tmp_path):
    # Flag-off, base captured from RAW head → matches raw sweep → stays pending
    # (proves the sweep still reads raw git when collab is off; parity).
    ws_id, pc_id = _seed_ws_with_proposal(
        pg_session_factory, tmp_path, collab=False, base_from="raw"
    )
    with pg_session_factory() as s:
        refresh_staleness(session=s, workspace_id=ws_id, workspaces_dir=str(tmp_path))
        s.commit()
    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id).status == "pending"


# ---------------------------------------------------------------------------
# C1 citation consumer — anchor probe reads log-canonical text when collab-on
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_citation_anchor_probe_reads_log_canonical_collab_on(
    pg_session_factory, _settings_env, tmp_path
):
    """Collab-on: anchor in durable log but NOT on disk → probe reads log → no error."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="cite-collab@ki.se"))
        s.commit()
        wid = ws.id

    # Disk proposal lacks the anchor.
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, "preamble\n\n## Methods\nNo citable anchor on disk.\n")

    # Seed the durable Yjs log with anchor-bearing text.
    anchor_md = "preamble\n\n## Methods\nWe argue that " + ANCHOR + " in our population.\n"
    seed = pm_json_to_ydoc(markdown_to_pm(anchor_md)).get_update()
    with pg_session_factory() as s:
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None, update=seed)
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()

    rid = _seed_reference(pg_session_factory, wid, chunks=[(0, "Results", CHUNK_TEXT)])
    ctx = _ctx(wid, repo, pg_session_factory, _settings_env)

    await read_proposal_impl(ctx)
    result = await propose_citation_impl(
        ctx,
        ports=ctx.ports,
        invocation=snapshot_invocation(ctx),
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote=CHUNK_TEXT[:40],
    )
    assert "error" not in result, result
    # A pending ProposedChange row must have been created for this workspace.
    with pg_session_factory() as s:
        rows = s.scalars(
            select(ProposedChange).where(ProposedChange.workspace_id == wid)
        ).all()
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].kind == "citation_insert"


@pytest.mark.anyio
async def test_citation_anchor_probe_reads_raw_disk_flag_off(
    pg_session_factory, _settings_env, tmp_path
):
    """Flag-off: anchor absent from disk → probe reads raw disk → anchor not found (parity)."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="cite-flagoff@ki.se"))
        s.commit()
        wid = ws.id

    # Disk proposal lacks the anchor (same divergence as collab-on test).
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, "preamble\n\n## Methods\nNo citable anchor on disk.\n")

    rid = _seed_reference(pg_session_factory, wid, chunks=[(0, "Results", CHUNK_TEXT)])
    ctx = _ctx(wid, repo, pg_session_factory, _settings_env)

    await read_proposal_impl(ctx)
    result = await propose_citation_impl(
        ctx,
        ports=ctx.ports,
        invocation=snapshot_invocation(ctx),
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote=CHUNK_TEXT[:40],
    )
    assert "anchor not found" in result["error"]
