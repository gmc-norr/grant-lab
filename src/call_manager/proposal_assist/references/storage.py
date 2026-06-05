"""Filesystem layout for workspace references. Mirrors
AttachmentStorage from B7.

Each reference's files live under
<workspaces_dir>/<workspace_id>/references/<reference_id>/
    ├── original.<ext>   uploaded/fetched bytes
    ├── text.txt         extracted plain text (written by Stage 1)
    └── outline.json     v1.1 only — PageIndex output

The parent workspace dir is shared with B5 (repo/) and B7 (attachments/).
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

_ALLOWED_SUFFIXES = frozenset(
    {".pdf", ".md", ".txt", ".docx", ".rtf", ".csv", ".xls", ".xlsx", ".ipynb"}
)


class ReferenceStorage:
    def __init__(self, root: Path | str):
        self.root = Path(root)

    def path_for(
        self,
        *,
        workspace_id: uuid.UUID,
        reference_id: uuid.UUID,
        suffix: str,
    ) -> Path:
        suffix = suffix.lower()
        if suffix not in _ALLOWED_SUFFIXES:
            raise ValueError(f"disallowed suffix: {suffix!r}")
        return (
            self.root / str(workspace_id) / "references" / str(reference_id) / f"original{suffix}"
        )

    def text_path_for(
        self,
        *,
        workspace_id: uuid.UUID,
        reference_id: uuid.UUID,
    ) -> Path:
        return self.root / str(workspace_id) / "references" / str(reference_id) / "text.txt"

    def write_atomic(
        self,
        *,
        workspace_id: uuid.UUID,
        reference_id: uuid.UUID,
        suffix: str,
        data: bytes,
    ) -> Path:
        """Write bytes to a `.partial` temp file, fsync, then os.replace
        to the final path. POSIX guarantees the rename is atomic on the
        same filesystem, so concurrent readers see either no file or the
        complete file."""
        final = self.path_for(workspace_id=workspace_id, reference_id=reference_id, suffix=suffix)
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_suffix(final.suffix + ".partial")
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, final)
        except Exception:
            if tmp.exists():
                tmp.unlink()
            raise
        return final

    def write_text(
        self,
        *,
        workspace_id: uuid.UUID,
        reference_id: uuid.UUID,
        text: str,
    ) -> Path:
        """Persist extracted text.txt alongside the original. Caller has
        already created the ref directory via write_atomic."""
        p = self.text_path_for(workspace_id=workspace_id, reference_id=reference_id)
        p.write_text(text, encoding="utf-8")
        return p

    def delete_one(
        self,
        *,
        workspace_id: uuid.UUID,
        reference_id: uuid.UUID,
    ) -> None:
        """Idempotent: remove the reference's directory (original + text.txt
        + outline.json if present). Caller has already deleted the DB row."""
        ref_dir = self.root / str(workspace_id) / "references" / str(reference_id)
        if ref_dir.exists():
            shutil.rmtree(ref_dir)

    def delete_workspace(self, *, workspace_id: uuid.UUID) -> None:
        """Idempotent: remove the workspace's ``references/`` subdir
        (every reference's original bytes + extracted text.txt).

        Mirrors ``AttachmentStorage.delete_workspace``. Only this subdir
        is removed; sibling ``repo/`` (B5) and ``attachments/`` (B7) trees
        are untouched. No-op if the subdir doesn't exist (covers
        workspaces that never had any references). Called from the
        workspace soft-delete handler so reference bytes don't survive the
        compliance scrub."""
        target = self.root / str(workspace_id) / "references"
        if target.exists():
            shutil.rmtree(target)
