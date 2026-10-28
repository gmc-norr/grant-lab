"""Tests for delete_or_tombstone_reference (Slice 2 Task 8).

An UNCITED reference hard-deletes (row + chunks + stored bytes). A CITED
reference (any evidence row in citations.json referencing it) is tombstoned:
the row stays with tombstoned_at set, chunks/bytes retained, so existing
citations keep resolving.
"""

import json
import subprocess
import uuid

import pytest
from sqlalchemy import func, select

from grant_lab.db.models import Reference, ReferenceChunk, WorkspaceEvent
from grant_lab.proposal_assist.references.deletion import delete_or_tombstone_reference
from grant_lab.proposal_assist.references.storage import ReferenceStorage
from tests.proposal_assist.conftest import make_user, make_workspace
from tests.proposal_assist.references.test_reprocess import _insert_failed_ref

EMBED_DIM = 1024


def _seed_reference(pg_session_factory, workspace_id, storage, tmp_path):
    """Seed a Reference + one chunk + stored original bytes. Returns rid."""
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
        s.add(
            ReferenceChunk(
                reference_id=rid,
                workspace_id=workspace_id,
                chunk_index=0,
                section_path="Results",
                text="some chunk text",
                embedding=[0.0] * EMBED_DIM,
                embedding_model="fake-embedder",
            )
        )
        s.commit()
    # Write stored bytes so we can assert hard-delete removes them.
    storage.write_atomic(
        workspace_id=workspace_id,
        reference_id=rid,
        suffix=".pdf",
        data=b"original bytes",
    )
    return rid


def _make_ws(pg_session_factory):
    with pg_session_factory() as s:
        user = make_user(s, email=f"owner-{uuid.uuid4()}@umu.se")
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        return ws.id


def _write_ledger(repo_dir, rows):
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "citations.json").write_text(
        json.dumps({"version": 2, "evidence": rows}), encoding="utf-8"
    )


def test_uncited_reference_is_hard_deleted(pg_session_factory, tmp_path):
    wsid = _make_ws(pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, storage, tmp_path)
    repo_dir = tmp_path / str(wsid) / "repo"
    # No citations.json (or empty ledger) → uncited.
    byte_path = storage.path_for(workspace_id=wsid, reference_id=rid, suffix=".pdf")
    assert byte_path.exists()

    with pg_session_factory() as s:
        outcome = delete_or_tombstone_reference(
            session=s,
            repo_dir=repo_dir,
            reference_id=rid,
            workspace_id=wsid,
            storage=storage,
        )
    assert outcome == "deleted"

    with pg_session_factory() as s:
        assert s.get(Reference, rid) is None
        remaining = s.query(ReferenceChunk).filter(ReferenceChunk.reference_id == rid).count()
        assert remaining == 0
    # Stored bytes removed too.
    assert not byte_path.exists()


def test_cited_reference_is_tombstoned_not_deleted(pg_session_factory, tmp_path):
    wsid = _make_ws(pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, storage, tmp_path)
    repo_dir = tmp_path / str(wsid) / "repo"
    _write_ledger(repo_dir, [{"id": "ev-1", "reference_id": str(rid)}])
    byte_path = storage.path_for(workspace_id=wsid, reference_id=rid, suffix=".pdf")

    with pg_session_factory() as s:
        outcome = delete_or_tombstone_reference(
            session=s,
            repo_dir=repo_dir,
            reference_id=rid,
            workspace_id=wsid,
            storage=storage,
        )
    assert outcome == "tombstoned"

    with pg_session_factory() as s:
        ref = s.get(Reference, rid)
        assert ref is not None
        assert ref.tombstoned_at is not None
        # Chunks retained so existing citations keep resolving.
        remaining = s.query(ReferenceChunk).filter(ReferenceChunk.reference_id == rid).count()
        assert remaining == 1
    # Stored bytes retained.
    assert byte_path.exists()


def test_unknown_reference_raises(pg_session_factory, tmp_path):
    wsid = _make_ws(pg_session_factory)
    repo_dir = tmp_path / str(wsid) / "repo"
    with pg_session_factory() as s, pytest.raises(ValueError):
        delete_or_tombstone_reference(
            session=s,
            repo_dir=repo_dir,
            reference_id=uuid.uuid4(),
            workspace_id=wsid,
        )


def test_cross_workspace_reference_raises(pg_session_factory, tmp_path):
    wsid_a = _make_ws(pg_session_factory)
    wsid_b = _make_ws(pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    rid = _seed_reference(pg_session_factory, wsid_a, storage, tmp_path)
    repo_dir = tmp_path / str(wsid_b) / "repo"
    with pg_session_factory() as s, pytest.raises(ValueError):
        delete_or_tombstone_reference(
            session=s,
            repo_dir=repo_dir,
            reference_id=rid,
            workspace_id=wsid_b,
            storage=storage,
        )


def test_unreadable_ledger_fails_closed_to_tombstone(pg_session_factory, tmp_path):
    """An UNREADABLE citations.json (OSError — e.g. a directory in place of the
    file) must be treated as CITED (fail-closed).

    This mirrors the corrupt-ledger test: when we cannot read the ledger we
    cannot confirm the reference is uncited, so we must NOT hard-delete it.
    The reference must be tombstoned, with its row, chunk(s), and bytes all
    retained.
    """
    wsid = _make_ws(pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, storage, tmp_path)
    repo_dir = tmp_path / str(wsid) / "repo"
    byte_path = storage.path_for(workspace_id=wsid, reference_id=rid, suffix=".pdf")

    # Place a DIRECTORY where citations.json would be so .exists() is True but
    # read_text() raises IsADirectoryError (a subclass of OSError).
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "citations.json").mkdir()

    with pg_session_factory() as s:
        outcome = delete_or_tombstone_reference(
            session=s,
            repo_dir=repo_dir,
            reference_id=rid,
            workspace_id=wsid,
            storage=storage,
        )

    # Fail-closed: unreadable ledger is treated as cited → tombstone.
    assert outcome == "tombstoned"

    with pg_session_factory() as s:
        ref = s.get(Reference, rid)
        # Row must still exist with tombstoned_at set.
        assert ref is not None
        assert ref.tombstoned_at is not None
        # Chunk(s) retained so existing citations keep resolving.
        remaining = s.query(ReferenceChunk).filter(ReferenceChunk.reference_id == rid).count()
        assert remaining == 1
    # Stored bytes retained.
    assert byte_path.exists()


def test_corrupt_ledger_fails_closed_to_tombstone(pg_session_factory, tmp_path):
    """A MALFORMED citations.json must be treated as CITED (fail-closed).

    This is the data-loss-sensitive branch: if the ledger is corrupt we cannot
    confirm the reference is uncited, so we must NOT hard-delete it. The
    reference must be tombstoned, with its row, chunk(s), and bytes all
    retained.
    """
    wsid = _make_ws(pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, storage, tmp_path)
    repo_dir = tmp_path / str(wsid) / "repo"
    byte_path = storage.path_for(workspace_id=wsid, reference_id=rid, suffix=".pdf")

    # Write a malformed (non-parseable) citations.json.
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "citations.json").write_text("{not valid json", encoding="utf-8")

    with pg_session_factory() as s:
        outcome = delete_or_tombstone_reference(
            session=s,
            repo_dir=repo_dir,
            reference_id=rid,
            workspace_id=wsid,
            storage=storage,
        )

    # Fail-closed: corrupt ledger is treated as cited → tombstone.
    assert outcome == "tombstoned"

    with pg_session_factory() as s:
        ref = s.get(Reference, rid)
        # Row must still exist with tombstoned_at set.
        assert ref is not None
        assert ref.tombstoned_at is not None
        # Chunk(s) retained so existing citations keep resolving.
        remaining = s.query(ReferenceChunk).filter(ReferenceChunk.reference_id == rid).count()
        assert remaining == 1
    # Stored bytes retained.
    assert byte_path.exists()


def test_tombstone_writes_audit_event(pg_session_factory, tmp_path):
    wsid = _make_ws(pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, storage, tmp_path)
    repo_dir = tmp_path / str(wsid) / "repo"
    _write_ledger(repo_dir, [{"id": "ev-1", "reference_id": str(rid)}])

    with pg_session_factory() as s:
        outcome = delete_or_tombstone_reference(
            session=s,
            repo_dir=repo_dir,
            reference_id=rid,
            workspace_id=wsid,
            storage=storage,
        )
    assert outcome == "tombstoned"

    with pg_session_factory() as s:
        count = s.scalar(
            select(func.count(WorkspaceEvent.id)).where(
                WorkspaceEvent.workspace_id == wsid,
                WorkspaceEvent.kind == "reference.tombstoned",
            )
        )
        assert count == 1


def test_retombstone_does_not_duplicate_audit_event(pg_session_factory, tmp_path):
    wsid = _make_ws(pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, storage, tmp_path)
    repo_dir = tmp_path / str(wsid) / "repo"
    _write_ledger(repo_dir, [{"id": "ev-1", "reference_id": str(rid)}])

    # First tombstone.
    with pg_session_factory() as s:
        outcome = delete_or_tombstone_reference(
            session=s,
            repo_dir=repo_dir,
            reference_id=rid,
            workspace_id=wsid,
            storage=storage,
        )
    assert outcome == "tombstoned"

    # Second tombstone on the same (already tombstoned) ref.
    with pg_session_factory() as s:
        outcome = delete_or_tombstone_reference(
            session=s,
            repo_dir=repo_dir,
            reference_id=rid,
            workspace_id=wsid,
            storage=storage,
        )
    assert outcome == "tombstoned"

    # Should only have ONE reference.tombstoned event, not two.
    with pg_session_factory() as s:
        count = s.scalar(
            select(func.count(WorkspaceEvent.id)).where(
                WorkspaceEvent.workspace_id == wsid,
                WorkspaceEvent.kind == "reference.tombstoned",
            )
        )
        assert count == 1, f"expected 1 tombstone event, got {count}"


def test_hard_delete_writes_audit_event(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid, owner_id = ws.id, ws.owner_id
    storage = ReferenceStorage(root=tmp_path)
    repo_dir = tmp_path / str(wsid) / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    with pg_session_factory() as s:
        rid = _insert_failed_ref(s, storage, workspace_id=wsid, data=b"# H\n\nbody")
    with pg_session_factory() as s:
        outcome = delete_or_tombstone_reference(
            session=s,
            repo_dir=repo_dir,
            reference_id=rid,
            workspace_id=wsid,
            storage=storage,
            actor_id=owner_id,
        )
    assert outcome == "deleted"
    with pg_session_factory() as s:
        kinds = {
            e.kind
            for e in s.scalars(select(WorkspaceEvent).where(WorkspaceEvent.workspace_id == wsid))
        }
        assert "reference.deleted" in kinds


_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": "/usr/bin:/bin",
}


def _git(repo_dir, *args):
    subprocess.run(["git", *args], cwd=repo_dir, check=True, env=_GIT_ENV, capture_output=True)


def _commit_ledger(repo_dir, rows, message):
    _write_ledger(repo_dir, rows)
    _git(repo_dir, "add", "citations.json")
    _git(repo_dir, "commit", "-q", "-m", message)


def test_reference_cited_on_ANOTHER_branch_is_not_hard_deleted(pg_session_factory, tmp_path):
    """A reference uncited on the checked-out branch but cited on a sibling
    branch must NOT be hard-deleted.

    References and their stored bytes are workspace-scoped, but citations.json
    is a versioned file: each branch has its own. Judging "uncited" from the
    working tree alone asks one branch about every branch's evidence, and a
    hard-delete destroys the chunks and bytes the sibling branch still needs to
    resolve its citations.
    """
    wsid = _make_ws(pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, storage, tmp_path)
    repo_dir = tmp_path / str(wsid) / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, "init", "-q", "-b", "main", ".")

    # main does not cite the reference; feature does.
    _commit_ledger(repo_dir, [], "main: no citations")
    _git(repo_dir, "checkout", "-q", "-b", "feature")
    _commit_ledger(repo_dir, [{"id": "ev-1", "reference_id": str(rid)}], "feature: cite it")
    _git(repo_dir, "checkout", "-q", "main")

    byte_path = storage.path_for(workspace_id=wsid, reference_id=rid, suffix=".pdf")

    with pg_session_factory() as s:
        outcome = delete_or_tombstone_reference(
            session=s,
            repo_dir=repo_dir,
            reference_id=rid,
            workspace_id=wsid,
            storage=storage,
        )

    assert outcome == "tombstoned"
    with pg_session_factory() as s:
        ref = s.get(Reference, rid)
        assert ref is not None
        assert ref.tombstoned_at is not None
        assert s.query(ReferenceChunk).filter(ReferenceChunk.reference_id == rid).count() == 1
    assert byte_path.exists()


def test_uncited_on_EVERY_branch_still_hard_deletes(pg_session_factory, tmp_path):
    """The negative control for the cross-branch check.

    Widening "cited?" to every branch must not quietly turn hard-delete into
    dead code. In a REAL git repo with several branches, none of which cites the
    reference, the delete must still be a delete — bytes, chunks and row gone.
    Without this test the cross-branch fix could fail closed on every real
    workspace and the rest of the file would still be green, because every other
    hard-delete test here runs against a directory that is not a repo at all.
    """
    wsid = _make_ws(pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, storage, tmp_path)
    repo_dir = tmp_path / str(wsid) / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, "init", "-q", "-b", "main", ".")

    _commit_ledger(repo_dir, [], "main: no citations")
    _git(repo_dir, "checkout", "-q", "-b", "feature")
    _commit_ledger(repo_dir, [{"id": "ev-1", "reference_id": str(uuid.uuid4())}], "other ref")
    # A third branch that predates citations.json entirely — an absent file on a
    # branch must contribute nothing, not fail closed.
    _git(repo_dir, "checkout", "-q", "--orphan", "empty")
    _git(repo_dir, "rm", "-q", "-f", "citations.json")
    _git(repo_dir, "commit", "-q", "--allow-empty", "-m", "empty: no ledger at all")
    _git(repo_dir, "checkout", "-q", "main")

    byte_path = storage.path_for(workspace_id=wsid, reference_id=rid, suffix=".pdf")

    with pg_session_factory() as s:
        outcome = delete_or_tombstone_reference(
            session=s,
            repo_dir=repo_dir,
            reference_id=rid,
            workspace_id=wsid,
            storage=storage,
        )

    assert outcome == "deleted"
    with pg_session_factory() as s:
        assert s.get(Reference, rid) is None
        assert s.query(ReferenceChunk).filter(ReferenceChunk.reference_id == rid).count() == 0
    assert not byte_path.exists()


def test_corrupt_ledger_on_ANOTHER_branch_fails_closed(pg_session_factory, tmp_path):
    """A sibling branch whose citations.json will not parse must fail closed.

    The checked-out branch's ledger is valid and cites nothing, so the only
    thing standing between this reference and a hard-delete is the unreadable
    ledger on `feature` — which might cite it.
    """
    wsid = _make_ws(pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, storage, tmp_path)
    repo_dir = tmp_path / str(wsid) / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, "init", "-q", "-b", "main", ".")

    _commit_ledger(repo_dir, [], "main: no citations")
    _git(repo_dir, "checkout", "-q", "-b", "feature")
    (repo_dir / "citations.json").write_text("{not valid json", encoding="utf-8")
    _git(repo_dir, "add", "citations.json")
    _git(repo_dir, "commit", "-q", "-m", "feature: corrupt ledger")
    _git(repo_dir, "checkout", "-q", "main")

    byte_path = storage.path_for(workspace_id=wsid, reference_id=rid, suffix=".pdf")

    with pg_session_factory() as s:
        outcome = delete_or_tombstone_reference(
            session=s,
            repo_dir=repo_dir,
            reference_id=rid,
            workspace_id=wsid,
            storage=storage,
        )

    assert outcome == "tombstoned"
    with pg_session_factory() as s:
        assert s.get(Reference, rid).tombstoned_at is not None
    assert byte_path.exists()


def test_a_broken_ref_file_fails_closed_and_a_repaired_repo_deletes(pg_session_factory, tmp_path):
    """A repo whose refs cannot be enumerated must NOT permit a hard-delete.

    Review 2026-10-06 M5, re-proved on 2026-10-26 A2 ("still open"):
    `_branches_cite` caught `GitCommitError` and returned **False** — "no
    branches to widen the search with". But `branch_tips` raises that error for
    two completely different reasons. One is "this directory is not a git
    repository", where False is right: the working tree is the whole answer.
    The other is "this IS a repository and its refs are broken", where False
    means we hard-delete the bytes a branch we could not even LIST may still
    cite, and nothing can get them back.

    Measured here: `git for-each-ref` warns "ignoring broken ref" on stderr,
    exits ZERO and omits that branch, which is exactly why `branch_tips` refuses
    to return the short map.

    The second half is the POSITIVE CONTROL that keeps the fix from being "never
    delete anything": remove the garbage ref and the very same reference
    hard-deletes. (`test_uncited_on_EVERY_branch_still_hard_deletes` above is
    the multi-branch version of the same control.)
    """
    wsid = _make_ws(pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, storage, tmp_path)
    repo_dir = tmp_path / str(wsid) / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, "init", "-q", "-b", "main", ".")
    # The working tree's ledger is VALID and cites nothing, so the broken ref is
    # the only thing between this reference and a hard-delete.
    _commit_ledger(repo_dir, [], "main: no citations")
    broken = repo_dir / ".git" / "refs" / "heads" / "broken"
    broken.write_text("not-a-sha\n", encoding="utf-8")

    byte_path = storage.path_for(workspace_id=wsid, reference_id=rid, suffix=".pdf")

    with pg_session_factory() as s:
        outcome = delete_or_tombstone_reference(
            session=s,
            repo_dir=repo_dir,
            reference_id=rid,
            workspace_id=wsid,
            storage=storage,
        )

    assert outcome == "tombstoned", (
        f"a repository whose refs will not enumerate must fail CLOSED; got {outcome!r}"
    )
    with pg_session_factory() as s:
        ref = s.get(Reference, rid)
        assert ref is not None and ref.tombstoned_at is not None
        assert s.query(ReferenceChunk).filter(ReferenceChunk.reference_id == rid).count() == 1
    assert byte_path.exists(), "the bytes a branch may still cite must be retained"

    # POSITIVE CONTROL: repair the repo and the same reference really deletes.
    broken.unlink()
    rid2 = _seed_reference(pg_session_factory, wsid, storage, tmp_path)
    byte_path2 = storage.path_for(workspace_id=wsid, reference_id=rid2, suffix=".pdf")
    with pg_session_factory() as s:
        outcome2 = delete_or_tombstone_reference(
            session=s,
            repo_dir=repo_dir,
            reference_id=rid2,
            workspace_id=wsid,
            storage=storage,
        )
    assert outcome2 == "deleted", (
        f"a HEALTHY repo with no citing branch must still hard-delete; got {outcome2!r}"
    )
    with pg_session_factory() as s:
        assert s.get(Reference, rid2) is None
    assert not byte_path2.exists()


def test_a_repository_we_cannot_STAT_fails_closed_too(monkeypatch, tmp_path):
    """`_is_git_repository` is the arm that decides which way `_branches_cite`
    fails, so an error INSIDE it must not read as "not a repository".

    `Path.exists()` swallows ENOENT/ENOTDIR/EBADF/ELOOP but lets a
    `PermissionError` propagate, and `PermissionError` is an `OSError` — so the
    original `except OSError: return False` turned an unreadable REAL repository
    into "no repository here", and `_branches_cite` then returned False and the
    reference was HARD-DELETED along with the bytes a branch may still cite.
    Measured on the pre-fix code against a real `git init` repo at mode 000:

        Path.exists() RAISED: PermissionError 13
        _is_git_repository(unreadable REAL repo) -> False
        _branches_cite -> False

    A missing directory still answers False, because that case never raises.
    Found by the Task 21 review (2026-10-28)."""
    from pathlib import Path as _Path

    from grant_lab.proposal_assist.references import deletion as deletion_mod

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    real_exists = _Path.exists

    def _raise_on_this_dotgit(self, *a, **kw):
        if self == repo_dir / ".git":
            raise PermissionError(13, "Permission denied")
        return real_exists(self, *a, **kw)

    monkeypatch.setattr(_Path, "exists", _raise_on_this_dotgit)

    assert deletion_mod._is_git_repository(repo_dir) is True, (
        "a repository we cannot stat must read as PRESENT, so _branches_cite fails CLOSED"
    )
    # The direction that must NOT change: a genuinely absent repo is still False,
    # and it reaches that answer without raising.
    assert deletion_mod._is_git_repository(tmp_path / "no_such_dir") is False
