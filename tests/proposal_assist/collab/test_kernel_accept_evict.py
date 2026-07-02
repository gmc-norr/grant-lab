from pathlib import Path

import pytest

from call_manager.db.models import User
from call_manager.proposal_assist.collab import state
from call_manager.proposal_assist.collab.hydrate import load_or_hydrate
from call_manager.proposal_assist.collab.markdown_bridge import pm_to_markdown
from call_manager.proposal_assist.collab.room import MANAGER, ClientState
from call_manager.proposal_assist.collab.yjs_bridge import ydoc_to_pm_json
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.proposals.kernel_accept import accept_via_kernel
from tests.proposal_assist.collab.test_collab_kernel_accept import _git_head, _init_repo, _mk_pc
from tests.proposal_assist.collab.test_collab_room import FakeClient
from tests.proposal_assist.conftest import make_user, make_workspace


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _seed_repo(pg_session_factory, tmp_path, email):
    with pg_session_factory() as s:
        o = make_user(s, email=email)
        ws = make_workspace(s, owner=o, current_phase="active")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, "Preamble.\n\n## A\n\nBody a.\n")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    return wid, oid, repo


async def _acquire_client(pg_session_factory, tmp_path, wid, oid):
    async def _load():
        with pg_session_factory() as s:
            u = load_or_hydrate(s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path))
            s.commit()
            return u
    client = FakeClient()
    await MANAGER.acquire((wid, "draft"), load=_load, client=client,
                          state=ClientState(user_id=oid, can_write=True))
    return client


@pytest.mark.anyio
async def test_pre_mutation_failure_does_not_evict(pg_session_factory, tmp_path):
    # PRE-mutation IndexError (section_index out of range, in _CLEAN_ACCEPT_ERRORS)
    # → no evict; the registered client keeps its live doc.
    wid, oid, repo = _seed_repo(pg_session_factory, tmp_path, "evict-pre@ki.se")
    key = (wid, "draft")
    client = await _acquire_client(pg_session_factory, tmp_path, wid, oid)
    try:
        with pg_session_factory() as s:
            pc_id = _mk_pc(s, wid, section_index=999, new_text="## X\n\nx.\n",
                           base_commit_sha=_git_head(repo), base_section_sha="deadbeef")
        with pg_session_factory() as s:
            approver = s.get(User, oid)
        with pytest.raises(IndexError):
            await accept_via_kernel(
                session_factory=pg_session_factory, workspaces_dir=str(tmp_path),
                workspace_id=wid, pc_id=pc_id, approver=approver, artifact="draft",
            )
        assert MANAGER.get(key) is not None          # NOT evicted (pre-mutation)
        assert client in MANAGER.get(key).clients      # client kept
        assert client.closed is None
    finally:
        await MANAGER.evict(key)


@pytest.mark.anyio
async def test_post_mutation_failure_evicts(pg_session_factory, tmp_path, monkeypatch):
    # POST-mutation failure (checkpoint_artifact raises AFTER the fragment mutation)
    # → evict. Discriminating: WITH a client registered, the room would survive on
    # exit if the evict arm were removed (client present), so this pins the evict.
    import call_manager.proposal_assist.collab.kernel as kmod
    wid, oid, repo = _seed_repo(pg_session_factory, tmp_path, "evict-post@ki.se")
    key = (wid, "draft")
    client = await _acquire_client(pg_session_factory, tmp_path, wid, oid)
    # base from the live (registered) room so the accept passes staleness + mutates.
    live_md = pm_to_markdown(ydoc_to_pm_json(MANAGER.get(key).doc))
    base_sha = split_sections(live_md)[1].sha   # section 1 = "## A"
    with pg_session_factory() as s:
        pc_id = _mk_pc(s, wid, section_index=1, new_text="## A\n\nEdited.\n",
                       base_commit_sha=_git_head(repo), base_section_sha=base_sha)
    with pg_session_factory() as s:
        approver = s.get(User, oid)

    def _boom(*_a, **_k):
        raise RuntimeError("checkpoint failed")

    monkeypatch.setattr(kmod, "checkpoint_artifact", _boom)
    with pytest.raises(RuntimeError):
        await accept_via_kernel(session_factory=pg_session_factory, workspaces_dir=str(tmp_path),
                                workspace_id=wid, pc_id=pc_id, approver=approver, artifact="draft")
    assert MANAGER.get(key) is None       # evicted (post-mutation recovery)
    assert client.closed == 1011           # evict force-closed the socket
