#!/usr/bin/env python
"""One-shot pipe-table audit (3b-5d.D4 §3.8): list live workspaces whose HEAD
draft/spec contains pipe tables — these artifacts are HELD BACK from the
enable-on-read flip until cleaned up manually. Read-only.

Run from the repo root:  uv run python scripts/collab_table_audit.py
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from call_manager.config import Settings
from call_manager.db.models import Workspace
from call_manager.db.session import make_engine, make_session_factory
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME, SPEC_FILENAME
from call_manager.proposal_assist.collab.flip import has_pipe_tables
from call_manager.proposal_assist.paths import workspace_repo_dir


def main() -> None:
    settings = Settings()
    sf = make_session_factory(make_engine(settings.database_url))
    hits = 0
    with sf() as s:
        rows = s.execute(
            select(Workspace.id, Workspace.title).where(Workspace.soft_deleted_at.is_(None))
        ).all()
    for ws_id, title in rows:
        repo = workspace_repo_dir(Path(settings.workspaces_dir), ws_id)
        for artifact, fname in (("draft", PROPOSAL_FILENAME), ("spec", SPEC_FILENAME)):
            try:
                md = (repo / fname).read_text(encoding="utf-8")
            except OSError:
                continue
            if has_pipe_tables(md):
                hits += 1
                print(f"{ws_id}\t{artifact}\t{title}")
    print(
        f"\n{hits} held-back artifact(s)."
        if hits
        else "clean — no pipe tables; the hold-back is removable."
    )


if __name__ == "__main__":
    main()
