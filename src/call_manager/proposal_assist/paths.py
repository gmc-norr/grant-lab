"""Single source of truth for workspace filesystem layout."""

from __future__ import annotations

import uuid
from pathlib import Path


def workspace_repo_dir(workspaces_dir: Path | str, workspace_id: uuid.UUID) -> Path:
    """The workspace's git repo: <workspaces_dir>/<workspace_id>/repo
    (matches drafting/controller.py and phases/setup.py)."""
    return Path(workspaces_dir) / str(workspace_id) / "repo"
