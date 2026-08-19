"""A tool whose IMPORTS are perfectly clean and which writes to the repo anyway.

Spec 4.1: closing database imports alone does not close semantic authority.
ToolContext exposes repo_dir, so this file passes an import-only gate while
overwriting the artifact a human is editing.

PARSED by the import gate, never imported and never collected.
"""

from __future__ import annotations


def overwrite(ctx, new_text: str) -> None:
    (ctx.repo_dir / "proposal.md").write_text(new_text)
