import subprocess
from pathlib import Path

from pycrdt import Doc
from sqlalchemy import select

from call_manager.db.models import ArtifactVersion, WorkspaceEvent
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.citations.ledger import dump_ledger, empty_ledger
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.checkpoint import (
    checkpoint_artifact,
    serialize_doc_to_markdown,
)
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from call_manager.proposal_assist.editing.sectionizer import split_sections
from tests.proposal_assist.conftest import make_user, make_workspace, stamp_for


def _init_repo(repo: Path, content: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=repo, check=True)
    (repo / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _seed_room_with_edit(pg_session_factory, wid, actor_id, md) -> Doc:
    # Build a REAL proposal doc through the bridge (the 'prosemirror' XmlFragment
    # the checkpoint serializer reads), persist its full-state update tagged with
    # the author, and return the doc.
    doc = pm_json_to_ydoc(markdown_to_pm(md))
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=actor_id, update=doc.get_update()
        )
        s.commit()
    return doc


def test_checkpoint_commits_serialized_doc_with_provenance(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        o = make_user(s, email="cp-o@ki.se")
        ws = make_workspace(s, owner=o)
        s.commit()
        wid, oid = ws.id, o.id
    _init_repo(Path(tmp_path) / str(wid) / "repo", "## Background\noriginal.\n")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()

    doc = _seed_room_with_edit(
        pg_session_factory, wid, oid, "## Background\n\nrewritten by owner.\n"
    )
    expected_md = serialize_doc_to_markdown(doc)

    with pg_session_factory() as s:
        res = checkpoint_artifact(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            artifact="draft",
            workspaces_dir=str(tmp_path),
        )
        s.commit()
    assert res.committed is True
    assert res.authors == [oid]
    # git now holds the serialized markdown
    disk = (Path(tmp_path) / str(wid) / "repo" / PROPOSAL_FILENAME).read_text()
    assert disk == expected_md
    # real content actually reached git (not a vacuous empty doc)
    assert "rewritten by owner." in disk
    # split_sections invariant holds on the checkpoint output
    assert "".join(sec.text for sec in split_sections(disk)) == disk
    with pg_session_factory() as s:
        av = (
            s.execute(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid))
            .scalars()
            .all()
        )
        assert len(av) == 1 and av[0].commit_sha == res.commit_sha and av[0].created_by == "user"
        ev = (
            s.execute(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == wid,
                    WorkspaceEvent.kind == "collab_checkpoint",
                )
            )
            .scalars()
            .all()
        )
        assert len(ev) == 1 and ev[0].payload["authors"] == [oid]
        assert state.get_checkpoint_watermark(s, workspace_id=wid, artifact="draft") == 1


def test_second_checkpoint_with_no_new_updates_is_noop(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        o = make_user(s, email="cp-noop@ki.se")
        ws = make_workspace(s, owner=o)
        s.commit()
        wid, oid = ws.id, o.id
    _init_repo(Path(tmp_path) / str(wid) / "repo", "## X\norig.\n")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    _seed_room_with_edit(pg_session_factory, wid, oid, "## X\n\nedited.\n")
    with pg_session_factory() as s:
        checkpoint_artifact(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            artifact="draft",
            workspaces_dir=str(tmp_path),
        )
        s.commit()
    with pg_session_factory() as s:
        res2 = checkpoint_artifact(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            artifact="draft",
            workspaces_dir=str(tmp_path),
        )
        s.commit()
    assert res2.committed is False  # watermark already at max update id -> no-op
    with pg_session_factory() as s:
        av = (
            s.execute(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid))
            .scalars()
            .all()
        )
        assert len(av) == 1  # no second version


def test_seed_only_open_close_does_not_checkpoint(pg_session_factory, tmp_path):
    # Review #2: opening a collab room hydrates an unauthored seed (actor_id=None)
    # from git HEAD. A last-leave / explicit checkpoint with NO human edits must
    # NOT commit a spurious system version (that would just re-serialize HEAD,
    # dropping non-schema markup) — but it should advance the watermark past the
    # seed so it isn't re-evaluated forever.
    with pg_session_factory() as s:
        o = make_user(s, email="cp-seedonly@ki.se")
        ws = make_workspace(s, owner=o)
        s.commit()
        wid = ws.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, "## Background\noriginal.\n")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    # an unauthored seed (as hydration persists on first open)
    _seed_room_with_edit(pg_session_factory, wid, None, "## Background\n\noriginal.\n")

    with pg_session_factory() as s:
        res = checkpoint_artifact(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            artifact="draft",
            workspaces_dir=str(tmp_path),
        )
        s.commit()
    assert res.committed is False
    # git HEAD is untouched — no spurious commit
    assert (repo / PROPOSAL_FILENAME).read_text() == "## Background\noriginal.\n"
    with pg_session_factory() as s:
        av = (
            s.execute(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid))
            .scalars()
            .all()
        )
        assert av == []  # no version recorded for a no-edit session
        rows = store.load_rows(s, workspace_id=wid, artifact="draft")
        assert state.get_checkpoint_watermark(s, workspace_id=wid, artifact="draft") == rows[-1][0]


def test_checkpoint_repo_missing_is_clean_noop(pg_session_factory, tmp_path):
    # Review #3: an authored update exists but the workspace has no git repo yet
    # (collab enabled before the repo is created). checkpoint_artifact must resolve
    # to a clean no-op (commit_sha="") instead of raising GitCommitError -> 500.
    with pg_session_factory() as s:
        o = make_user(s, email="cp-norepo@ki.se")
        ws = make_workspace(s, owner=o)
        s.commit()
        wid, oid = ws.id, o.id
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    # authored update, but NO git repo on disk under tmp_path
    _seed_room_with_edit(pg_session_factory, wid, oid, "## X\n\nedited.\n")
    with pg_session_factory() as s:
        res = checkpoint_artifact(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            artifact="draft",
            workspaces_dir=str(tmp_path),
        )
        s.commit()
    assert res.committed is False
    assert res.commit_sha == ""  # no HEAD to report; did not raise
    with pg_session_factory() as s:
        av = (
            s.execute(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid))
            .scalars()
            .all()
        )
        assert av == []


def _files_in_head(repo: Path) -> list[str]:
    """List all files in the HEAD commit via git show."""
    result = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.strip().split("\n") if line]


def test_checkpoint_record_version_false_is_quiet_and_compacts(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        o = make_user(s, email="cp-quiet@ki.se")
        ws = make_workspace(s, owner=o)
        s.commit()
        wid, oid = ws.id, o.id
    _init_repo(Path(tmp_path) / str(wid) / "repo", "## Background\noriginal.\n")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    # TWO authored edits (>= 2 rows) so the quiet path must STILL compact — proving
    # record_version=False did not accidentally skip the compaction block (the point
    # of 3b-5c is tail-bounding). Mirrors test_collab_compaction.py's 2-edit pattern.
    _seed_room_with_edit(pg_session_factory, wid, oid, "## Background\n\nedit one.\n")
    _seed_room_with_edit(
        pg_session_factory, wid, oid, "## Background\n\nedit one.\n\n## Aims\n\nedit two.\n"
    )
    with pg_session_factory() as s:
        # pre-checkpoint tail max
        max_id = store.load_rows(s, workspace_id=wid, artifact="draft")[-1][0]

    with pg_session_factory() as s:
        res = checkpoint_artifact(
            s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft",
            workspaces_dir=str(tmp_path),
            record_version=False,
        )
        s.commit()

    assert res.committed is True                                   # git commit STILL made
    with pg_session_factory() as s:
        av = (
            s.execute(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid))
            .scalars()
            .all()
        )
        ev = s.execute(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == wid, WorkspaceEvent.kind == "collab_checkpoint"
            )
        ).scalars().all()
        assert av == [] and ev == []                               # QUIET: no version / event
        rows = store.load_rows(s, workspace_id=wid, artifact="draft")
        assert len(rows) == 1                                      # COMPACTED 2 -> 1 (quiet path)
        assert state.get_checkpoint_watermark(s, workspace_id=wid, artifact="draft") == max_id


def test_checkpoint_co_commits_extra_files(pg_session_factory, tmp_path):
    # Task 2: checkpoint with extra_files={} co-commits multiple files atomically.
    # Seed a room with an authored edit, then checkpoint with citations.json.
    with pg_session_factory() as s:
        o = make_user(s, email="cp-extra@ki.se")
        ws = make_workspace(s, owner=o)
        s.commit()
        wid, oid = ws.id, o.id
    _init_repo(Path(tmp_path) / str(wid) / "repo", "## Background\noriginal.\n")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()

    _seed_room_with_edit(pg_session_factory, wid, oid, "## Background\n\nedited.\n")

    # Checkpoint with extra_files co-commits the ledger atomically
    payload = dump_ledger(empty_ledger())
    with pg_session_factory() as s:
        res = checkpoint_artifact(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            artifact="draft",
            workspaces_dir=str(tmp_path),
            extra_files={"citations.json": payload},
        )
        s.commit()

    assert res.committed is True
    repo = Path(tmp_path) / str(wid) / "repo"
    assert (repo / "citations.json").read_text() == payload
    # Both files in HEAD
    files_in_head = _files_in_head(repo)
    assert "citations.json" in files_in_head
    assert PROPOSAL_FILENAME in files_in_head

    # Exactly one ArtifactVersion and one WorkspaceEvent (one commit)
    with pg_session_factory() as s:
        av = (
            s.execute(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid))
            .scalars()
            .all()
        )
        assert len(av) == 1
        ev = (
            s.execute(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == wid,
                    WorkspaceEvent.kind == "collab_checkpoint",
                )
            )
            .scalars()
            .all()
        )
        assert len(ev) == 1


# ---------------------------------------------------------------------------
# Review 2026-09-02 §2.3: the checkpoint must never commit bytes the reader
# refuses — that bricked the artifact on its next hydration.
# ---------------------------------------------------------------------------
import pytest  # noqa: E402

from call_manager.proposal_assist.collab import checkpoint as checkpoint_module  # noqa: E402
from call_manager.proposal_assist.collab.checkpoint import (  # noqa: E402
    CheckpointProjectionUnreadable,
)


def _collab_workspace(pg_session_factory, tmp_path, *, email: str):
    with pg_session_factory() as s:
        o = make_user(s, email=email)
        ws = make_workspace(s, owner=o)
        s.commit()
        wid, oid = ws.id, o.id
    _init_repo(Path(tmp_path) / str(wid) / "repo", "## Background\noriginal.\n")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    return wid, oid


def _git_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_checkpoint_of_an_empty_blockquote_commits_readable_markdown(
    pg_session_factory, tmp_path
):
    # Tiptap: typing "> " and moving on leaves blockquote[paragraph()]. This
    # used to checkpoint as a bare ">" that markdown_to_pm refused, so the next
    # GET of the draft 500ed and the git-first save answered 409.
    wid, oid = _collab_workspace(pg_session_factory, tmp_path, email="cp-bq@ki.se")
    t = lambda s: {"type": "text", "text": s}  # noqa: E731
    doc = pm_json_to_ydoc(
        {
            "type": "doc",
            "content": [
                {"type": "heading", "attrs": {"level": 2}, "content": [t("Aims")]},
                {"type": "paragraph", "content": [t("intro")]},
                {"type": "blockquote", "content": [{"type": "paragraph"}]},
                {"type": "paragraph", "content": [t("after")]},
            ],
        }
    )
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=oid, update=doc.get_update()
        )
        s.commit()
    with pg_session_factory() as s:
        res = checkpoint_artifact(
            s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft",
            workspaces_dir=str(tmp_path),
        )
        s.commit()
    assert res.committed is True
    disk = (Path(tmp_path) / str(wid) / "repo" / PROPOSAL_FILENAME).read_text()
    assert disk == "## Aims\n\nintro\n\n>\n\nafter\n"
    # The hydrate step that used to raise on exactly these bytes:
    assert markdown_to_pm(disk)["content"][2] == {
        "type": "blockquote", "content": [{"type": "paragraph"}],
    }


def test_checkpoint_refuses_a_projection_the_reader_cannot_parse(
    pg_session_factory, tmp_path, monkeypatch
):
    # No shape the serializer emits is unreadable any more (that is the point of
    # the §2.3 fixes), so the reader's refusal is simulated; what is real is
    # everything the guard must guarantee: no commit, no version row, watermark
    # unmoved, and the SAME log checkpoints normally once the reader accepts it.
    wid, oid = _collab_workspace(pg_session_factory, tmp_path, email="cp-refuse@ki.se")
    repo = Path(tmp_path) / str(wid) / "repo"
    _seed_room_with_edit(pg_session_factory, wid, oid, "## Background\n\nedited.\n")
    head_before = _git_head(repo)

    def _reader_refuses(md: str):
        raise ValueError("Invalid content for node blockquote: <>")

    monkeypatch.setattr(checkpoint_module, "markdown_to_pm_or_empty", _reader_refuses)
    with pg_session_factory() as s:
        with pytest.raises(CheckpointProjectionUnreadable):
            checkpoint_artifact(
                s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft",
                workspaces_dir=str(tmp_path),
            )
        s.rollback()
    assert _git_head(repo) == head_before
    assert (repo / PROPOSAL_FILENAME).read_text() == "## Background\noriginal.\n"
    with pg_session_factory() as s:
        assert state.get_checkpoint_watermark(s, workspace_id=wid, artifact="draft") == 0
        assert (
            s.execute(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid))
            .scalars()
            .all()
            == []
        )

    monkeypatch.undo()
    with pg_session_factory() as s:
        res = checkpoint_artifact(
            s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft",
            workspaces_dir=str(tmp_path),
        )
        s.commit()
    assert res.committed is True
    assert _git_head(repo) != head_before
    assert "edited." in (repo / PROPOSAL_FILENAME).read_text()
