"""Shared pieces for the migration backfill tests (0037, 0066, 0068, 0083).

The workspace tables are parked (src/grant_lab/db/models/workspace/), so nothing
in the app builds their rows any more. `make_user` and `make_workspace` replace
the helpers of the same names that the deleted tests/proposal_assist/conftest.py
provided, cut down to what these tests use.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

from alembic.config import Config

from grant_lab.db.models import User
from grant_lab.db.models.workspace import Workspace

REPO = Path(__file__).resolve().parents[2]
VERSIONS = REPO / "alembic" / "versions"

#: Identity for the test-side `git commit`. tests/conftest.py's
#: `_hermetic_git_env` removes the ambient one on purpose.
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": "/usr/bin:/bin",
}


def load_migration(filename: str) -> ModuleType:
    """Execute one file in alembic/versions as a module, so the frozen backfill in
    it can be called directly. Same pattern as tests/test_migration_0082_*."""
    path = VERSIONS / filename
    spec = importlib.util.spec_from_file_location(f"_migration_{path.stem}", path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def alembic_cfg(pg_url: str) -> Config:
    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def make_user(session, *, email: str = "owner@umu.se") -> User:
    """A User row. The default email is a fixed literal, so a second call in one
    session must pass its own `email` or it collides on uq_users_email."""
    user = User(status="active", email=email)
    session.add(user)
    session.flush()
    return user


def make_workspace(
    session, *, owner: User | None = None, title: str = "Test workspace"
) -> Workspace:
    """A Workspace row owned by *owner*, or by a fresh `make_user()` when none is
    given (so two calls without `owner=` in one session collide on the email)."""
    if owner is None:
        owner = make_user(session)
    ws = Workspace(title=title, owner_id=owner.id, current_phase="active")
    session.add(ws)
    session.flush()
    return ws


def repo_dir(workspaces_dir, workspace_id) -> Path:
    """`<WORKSPACES_DIR>/<workspace_id>/repo`, the layout the app used.

    Written out here, not taken from the migration under test, so a frozen copy
    that looks somewhere else goes red."""
    return Path(workspaces_dir) / str(workspace_id) / "repo"


def git_repo(workspaces_dir, workspace_id, head, *, extra=(), commit=True, detach=False) -> Path:
    """A workspace repo on branch `head`, plus one ref per name in `extra`."""
    d = repo_dir(workspaces_dir, workspace_id)
    d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", head, "."], cwd=d, check=True)
    if commit:
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "x"], cwd=d, check=True, env=GIT_ENV
        )
        for name in extra:
            subprocess.run(["git", "branch", name], cwd=d, check=True)
    if detach:
        subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=d, check=True)
    return d
