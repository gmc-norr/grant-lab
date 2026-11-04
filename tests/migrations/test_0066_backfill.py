"""Migration 0066: workspaces.current_branch, read from each repo, never guessed.

Moved from tests/proposal_assist/test_branch_column_migration.py on 2026-11-03,
when the proposal workspace left master. The backfill it tested is now a frozen
copy inside alembic/versions/0066_workspace_current_branch.py, and these tests
call it there. The last test runs upgrade() itself on rows that predate it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select, text

from alembic import command
from grant_lab.db.models.workspace import Workspace
from tests.migrations.support import (
    alembic_cfg,
    git_repo,
    load_migration,
    make_user,
    make_workspace,
)

M = load_migration("0066_workspace_current_branch.py")
_OLD = datetime(2020, 1, 1, tzinfo=UTC)


def _branch_of(session_factory, wid):
    with session_factory() as s:
        return s.execute(select(Workspace.current_branch).where(Workspace.id == wid)).scalar_one()


def _backfill(session_factory, root):
    with session_factory() as s:
        n = M._backfill_current_branch(s.connection(), workspaces_dir=root)
        s.commit()
    return n


def test_backfill_reads_the_repos_actual_branch(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "aims-ambitious")

    assert _backfill(pg_session_factory, tmp_path) == 1
    assert _branch_of(pg_session_factory, wid) == "aims-ambitious"


def test_each_workspace_gets_its_own_repos_branch(pg_session_factory, tmp_path):
    """Several rows at once. A copy that wrote one name to every row, or keyed the
    UPDATE on the wrong id, passes the single-row test above."""
    with pg_session_factory() as s:
        a = make_workspace(s, title="a", owner=make_user(s, email="m66-a@umu.se")).id
        b = make_workspace(s, title="b", owner=make_user(s, email="m66-b@umu.se")).id
        none = make_workspace(s, title="none", owner=make_user(s, email="m66-c@umu.se")).id
        s.commit()
    git_repo(tmp_path, a, "aims-ambitious")
    git_repo(tmp_path, b, "eu-version")

    assert _backfill(pg_session_factory, tmp_path) == 2
    assert [_branch_of(pg_session_factory, w) for w in (a, b, none)] == [
        "aims-ambitious",
        "eu-version",
        "main",
    ]


def test_backfill_records_an_unborn_repos_branch_rather_than_failing(pg_session_factory, tmp_path):
    # A workspace still in setup has a repo with no commits. Its branch NAME is
    # known and correct; failing here would block the whole migration on it.
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "draft-1", commit=False)

    _backfill(pg_session_factory, tmp_path)
    assert _branch_of(pg_session_factory, wid) == "draft-1"


def test_backfill_leaves_a_workspace_with_no_repo_at_the_default(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    # No repo directory created at all.
    assert _backfill(pg_session_factory, tmp_path) == 0
    assert _branch_of(pg_session_factory, wid) == "main"


def test_backfill_RAISES_on_a_detached_head(pg_session_factory, tmp_path):
    # Nothing in the app detached HEAD, so a detached repo means something
    # outside the app did. Writing a plausible 'main' would discard that state.
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "main", detach=True)

    with pg_session_factory() as s, pytest.raises(M.GitCommitError):
        M._backfill_current_branch(s.connection(), workspaces_dir=tmp_path)


def test_the_backfill_restamps_updated_at_as_the_orm_update_did(pg_session_factory, tmp_path):
    """The original ran `update(Workspace)`, and SQLAlchemy added the model's
    `onupdate`: `SET updated_at=now(), current_branch=...` (compiled 2026-11-03).
    The frozen copy writes that out, so a database migrated today gets the rows
    production got. A row with no repo is not updated at all."""
    with pg_session_factory() as s:
        a = make_workspace(s, title="repo", owner=make_user(s, email="m66-r@umu.se")).id
        b = make_workspace(s, title="no repo", owner=make_user(s, email="m66-n@umu.se")).id
        s.execute(
            text("UPDATE workspaces SET updated_at = :old WHERE id IN (:a, :b)"),
            {"old": _OLD, "a": a, "b": b},
        )
        s.commit()
    git_repo(tmp_path, a, "main")

    _backfill(pg_session_factory, tmp_path)

    with pg_session_factory() as s:
        stamps = dict(
            s.execute(
                select(Workspace.id, Workspace.updated_at).where(Workspace.id.in_([a, b]))
            ).all()
        )
    assert stamps[a] > _OLD
    assert stamps[b] == _OLD


def test_upgrade_0066_backfills_rows_that_predate_it(
    pg_url, pg_session_factory, tmp_path, monkeypatch
):
    """upgrade() itself: downgrade to 0065, seed workspaces and their repos there,
    upgrade to 0066 with WORKSPACES_DIR pointing at the repos, read the column.

    This is what proves upgrade() calls the frozen copy, with the env var, after
    the column exists. `pg_session_factory` is requested only for its teardown
    TRUNCATE (same as tests/test_migration_0064_claimed_at_generation.py).
    """
    cfg = alembic_cfg(pg_url)
    eng = create_engine(pg_url)
    try:
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0065")
        try:
            with eng.begin() as c:
                uid = c.execute(
                    text("INSERT INTO users (email) VALUES ('m0066@umu.se') RETURNING id")
                ).scalar_one()

                def ws(title):
                    return c.execute(
                        text(
                            "INSERT INTO workspaces (id, title, owner_id, updated_at) "
                            "VALUES (gen_random_uuid(), :t, :u, :at) RETURNING id"
                        ),
                        {"t": title, "u": uid, "at": _OLD},
                    ).scalar_one()

                feature, unborn, no_repo = ws("feature"), ws("unborn"), ws("no repo")
            git_repo(tmp_path, feature, "aims-ambitious", extra=("main",))
            git_repo(tmp_path, unborn, "draft-1", commit=False)
            monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))

            command.upgrade(cfg, "0066")

            with eng.connect() as c:
                rows = c.execute(
                    text(
                        "SELECT id, current_branch, updated_at FROM workspaces WHERE owner_id = :u"
                    ),
                    {"u": uid},
                ).all()
        finally:
            command.upgrade(cfg, "head")
    finally:
        eng.dispose()

    got = {wid: branch for wid, branch, _ in rows}
    stamped = {wid: at for wid, _, at in rows}
    assert got == {feature: "aims-ambitious", unborn: "draft-1", no_repo: "main"}
    assert stamped[no_repo] == _OLD
    assert stamped[feature] > _OLD and stamped[unborn] > _OLD
