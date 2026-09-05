"""load_or_hydrate must take the commit mutex before it READS the log, not only
before it writes a seed. Spec §4.4 — this is what makes a mid-switch reconnect
block instead of republishing the old branch's room."""

import pytest

from grant_lab.proposal_assist.collab import hydrate, store
from grant_lab.proposal_assist.collab.hydrate import load_or_hydrate
from tests.proposal_assist.conftest import make_workspace, stamp_for


def test_mutex_is_taken_even_when_the_log_ALREADY_has_rows(  # noqa: E501 — long test name
    pg_session_factory, tmp_path, monkeypatch
):
    # The warm path. Before this change it took no lock at all, which is the
    # whole defect: a reconnect during a branch switch sailed past the mutex.
    with pg_session_factory() as s:
        ws = make_workspace(s, title="w")
        wid = ws.id
        s.commit()

    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=None, update=b"\x00\x00"
        )
        s.commit()

    calls: list[tuple] = []
    real = hydrate.acquire_commit_mutex_at_generation

    def _spy(session, workspace_id, *, generation, timeout_seconds):
        calls.append((workspace_id, generation))
        return real(session, workspace_id, generation=generation, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(hydrate, "acquire_commit_mutex_at_generation", _spy)

    with pg_session_factory() as s:
        stamp = stamp_for(s, wid)
        updates = load_or_hydrate(
            s, stamp=stamp, workspace_id=wid, artifact="draft", workspaces_dir=tmp_path
        )

    assert updates == [b"\x00\x00"]  # still returns the rows
    assert calls == [(wid, 0)]  # and it LOCKED first


def test_mutex_is_taken_before_load_updates_is_called(pg_session_factory, tmp_path, monkeypatch):
    # Order, not merely presence: a lock taken after the read would satisfy the
    # test above and still lose the race this exists to close.
    with pg_session_factory() as s:
        ws = make_workspace(s, title="w")
        wid = ws.id
        s.commit()
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=None, update=b"\x00\x00"
        )
        s.commit()

    order: list[str] = []
    real_mutex = hydrate.acquire_commit_mutex_at_generation
    real_load = hydrate.store.load_updates

    def _mutex(session, workspace_id, *, generation, timeout_seconds):
        order.append("mutex")
        return real_mutex(
            session, workspace_id, generation=generation, timeout_seconds=timeout_seconds
        )

    def _load(session, *, workspace_id, artifact):
        order.append("load")
        return real_load(session, workspace_id=workspace_id, artifact=artifact)

    monkeypatch.setattr(hydrate, "acquire_commit_mutex_at_generation", _mutex)
    monkeypatch.setattr(hydrate.store, "load_updates", _load)

    with pg_session_factory() as s:
        load_or_hydrate(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            artifact="draft",
            workspaces_dir=tmp_path,
        )

    assert order == ["mutex", "load"]


def test_a_stale_generation_is_refused_on_the_WARM_path_too(pg_session_factory, tmp_path):
    # Consequence of moving the lock, and the behaviour §4.4 relies on: a connect
    # stamped before a switch wakes holding an old generation and is refused,
    # rather than being handed the pre-switch log.
    from grant_lab.proposal_assist.fencing import WorkspaceGenerationStale
    from grant_lab.workspaces import bump_lifecycle_generation

    with pg_session_factory() as s:
        ws = make_workspace(s, title="w")
        wid = ws.id
        s.commit()
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=None, update=b"\x00\x00"
        )
        s.commit()

    with pg_session_factory() as s:
        stale = stamp_for(s, wid)  # belief formed BEFORE the bump

    with pg_session_factory() as s:
        bump_lifecycle_generation(s, workspace_id=wid)
        s.commit()

    with pg_session_factory() as s, pytest.raises(WorkspaceGenerationStale):
        load_or_hydrate(s, stamp=stale, workspace_id=wid, artifact="draft", workspaces_dir=tmp_path)


def test_the_document_epoch_is_read_after_the_mutex_and_before_the_rows(
    pg_session_factory, tmp_path, monkeypatch
):
    # Spec 2026-09-02-collab-doc-epoch-design.md §3.2: the epoch and the rows are
    # one snapshot only if BOTH are read under the mutex. A switch truncates the
    # log and bumps the epoch in one transaction; an epoch read taken before the
    # lock could label post-switch rows with the pre-switch epoch.
    with pg_session_factory() as s:
        ws = make_workspace(s, title="w")
        wid = ws.id
        s.commit()
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=None, update=b"\x00\x00"
        )
        s.commit()

    order: list[str] = []
    real_mutex = hydrate.acquire_commit_mutex_at_generation
    real_epoch = hydrate.state.get_doc_epoch
    real_load = hydrate.store.load_updates

    def _mutex(session, workspace_id, *, generation, timeout_seconds):
        order.append("mutex")
        return real_mutex(
            session, workspace_id, generation=generation, timeout_seconds=timeout_seconds
        )

    def _epoch(session, *, workspace_id, artifact):
        order.append("epoch")
        return real_epoch(session, workspace_id=workspace_id, artifact=artifact)

    def _load(session, *, workspace_id, artifact):
        order.append("load")
        return real_load(session, workspace_id=workspace_id, artifact=artifact)

    monkeypatch.setattr(hydrate, "acquire_commit_mutex_at_generation", _mutex)
    monkeypatch.setattr(hydrate.state, "get_doc_epoch", _epoch)
    monkeypatch.setattr(hydrate.store, "load_updates", _load)

    with pg_session_factory() as s:
        updates = load_or_hydrate(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            artifact="draft",
            workspaces_dir=tmp_path,
        )

    assert order == ["mutex", "epoch", "load"]
    assert updates.doc_epoch == 0
