from pathlib import Path

import pycrdt

from call_manager.db.models import User
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.kernel import kernel_accept_prose
from call_manager.proposal_assist.collab.markdown_bridge import pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import ydoc_to_pm_json
from call_manager.proposal_assist.editing.sectionizer import split_sections
from tests.proposal_assist.collab.test_collab_kernel_accept import (
    _git_head,
    _init_repo,
    _live_room,
    _mk_pc,
)
from tests.proposal_assist.conftest import make_user, make_workspace

_DOC = "Preamble.\n\n## A\n\nBody a.\n\n## B\n\nBody b.\n"
_NO_PREAMBLE = "## A\n\nBody a.\n\n## B\n\nBody b.\n"


def _fork_edit_last_paragraph(room_doc, text: str) -> bytes:
    """Simulate a client: fork the room state, char-insert `text` into the LAST
    top-level block's XmlText, and return the incremental Yjs update (as a real
    y-prosemirror client would emit). Applied to the room AFTER the accept, it
    reproduces the C2 race."""
    fork = pycrdt.Doc()
    fork.apply_update(room_doc.get_update())
    frag = fork.get("prosemirror", type=pycrdt.XmlFragment)
    xtext = list(list(frag.children)[-1].children)[0]   # last block's XmlText
    cap: list[bytes] = []
    sub = fork.observe(lambda e: cap.append(e.update))
    with fork.transaction():
        xtext.insert(len(xtext), text)
    fork.unobserve(sub)
    return cap[-1]


def _seed(pg_session_factory, tmp_path, doc_md, email):
    with pg_session_factory() as s:
        o = make_user(s, email=email)
        ws = make_workspace(s, owner=o, current_phase="active")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, doc_md)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    with pg_session_factory() as s:
        room_doc = _live_room(s, wid, str(tmp_path))
    return wid, oid, repo, room_doc


def test_accept_preserves_concurrent_edit_in_other_section(pg_session_factory, tmp_path):
    wid, oid, repo, room_doc = _seed(pg_session_factory, tmp_path, _DOC, "k-c2@ki.se")
    # Client generates a char-level edit in section B (last block) vs the pre-accept
    # state; the update arrives AFTER the accept.
    fork_update = _fork_edit_last_paragraph(room_doc, " CONCURRENT")
    live_md = pm_to_markdown(ydoc_to_pm_json(room_doc))
    base_sha = split_sections(live_md)[1].sha   # section 1 = "## A"
    with pg_session_factory() as s:
        pc_id = _mk_pc(s, wid, section_index=1, new_text="## A\n\nA EDITED.\n",
                       base_commit_sha=_git_head(repo), base_section_sha=base_sha)
    with pg_session_factory() as s:
        approver = s.get(User, oid)
        kernel_accept_prose(s, room_doc=room_doc, proposed_change_id=pc_id,
                            workspace_id=wid, approver=approver, workspaces_dir=str(tmp_path))
        s.commit()
    room_doc.apply_update(fork_update)   # concurrent client update merges post-accept
    out = pm_to_markdown(ydoc_to_pm_json(room_doc))
    assert "A EDITED." in out                       # accept applied
    assert "CONCURRENT" in out                       # section-B racing edit SURVIVED


def test_clear_preamble_via_empty_new_text_does_not_crash(pg_session_factory, tmp_path):
    # Empty new_text is a legal preamble clear (kernel.py:106); the section-scoped
    # apply must map it to blocks=[] (raw markdown_to_pm raises on '') and succeed.
    wid, oid, repo, room_doc = _seed(pg_session_factory, tmp_path, _DOC, "k-clear@ki.se")
    live_md = pm_to_markdown(ydoc_to_pm_json(room_doc))
    base_sha = split_sections(live_md)[0].sha   # section 0 = the "Preamble." preamble
    with pg_session_factory() as s:
        pc_id = _mk_pc(s, wid, section_index=0, new_text="",
                       base_commit_sha=_git_head(repo), base_section_sha=base_sha)
    with pg_session_factory() as s:
        approver = s.get(User, oid)
        res = kernel_accept_prose(s, room_doc=room_doc, proposed_change_id=pc_id,
                                  workspace_id=wid, approver=approver, workspaces_dir=str(tmp_path))
        s.commit()
        assert res.pc.status == "accepted"
    assert "Preamble." not in pm_to_markdown(ydoc_to_pm_json(room_doc))


def test_normal_single_section_accept_unchanged(pg_session_factory, tmp_path):
    # Non-concurrent regression: a plain accept still yields the expected content.
    wid, oid, repo, room_doc = _seed(pg_session_factory, tmp_path, _DOC, "k-plain@ki.se")
    live_md = pm_to_markdown(ydoc_to_pm_json(room_doc))
    base_sha = split_sections(live_md)[1].sha
    with pg_session_factory() as s:
        pc_id = _mk_pc(s, wid, section_index=1, new_text="## A\n\nPlain edit.\n",
                       base_commit_sha=_git_head(repo), base_section_sha=base_sha)
    with pg_session_factory() as s:
        approver = s.get(User, oid)
        kernel_accept_prose(s, room_doc=room_doc, proposed_change_id=pc_id,
                            workspace_id=wid, approver=approver, workspaces_dir=str(tmp_path))
        s.commit()
    secs = split_sections(pm_to_markdown(ydoc_to_pm_json(room_doc)))
    assert "Plain edit." in secs[1].text and "Body b." in secs[2].text


def test_blank_clear_of_already_empty_preamble_is_noop_accept(pg_session_factory, tmp_path):
    # Reaches the kernel's `if not update:` guard: newline glue turns "" into "\n"
    # (dodging the new_text == live.text short-circuit), blocks=[] on the already-
    # empty preamble range [0,0) yields b"" — which must be accepted as a no-op,
    # NOT persisted (a b"" row would poison the next hydration).
    wid, oid, repo, room_doc = _seed(pg_session_factory, tmp_path, _NO_PREAMBLE, "k-noop@ki.se")
    before = pm_to_markdown(ydoc_to_pm_json(room_doc))
    base_sha = split_sections(before)[0].sha    # section 0 = the EMPTY preamble
    with pg_session_factory() as s:
        pc_id = _mk_pc(s, wid, section_index=0, new_text="",
                       base_commit_sha=_git_head(repo), base_section_sha=base_sha)
    with pg_session_factory() as s:
        approver = s.get(User, oid)
        res = kernel_accept_prose(s, room_doc=room_doc, proposed_change_id=pc_id,
                                  workspace_id=wid, approver=approver, workspaces_dir=str(tmp_path))
        s.commit()
        assert res.pc.status == "accepted"
        assert res.update is None and res.committed is False
    with pg_session_factory() as s:
        rows = store.load_updates(s, workspace_id=wid, artifact="draft")
        assert b"" not in rows                       # no poison row persisted
    assert pm_to_markdown(ydoc_to_pm_json(room_doc)) == before   # doc untouched
