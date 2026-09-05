"""Filesystem layout for workspace attachments.

Each workspace's attachments live under
<workspaces_dir>/<workspace_uuid>/attachments/<attachment_uuid>.<ext>.

The parent workspace dir already exists from B5 (git repos live under
<workspaces_dir>/<workspace_uuid>/repo/). We only manage the
``attachments/`` subdir — ``delete_workspace`` removes that subdir only,
leaving the B5 repo intact.

**Atomicity contract:** ``write_atomic`` writes to a sibling ``.partial``
file, fsyncs, then ``os.replace``s into the final path. POSIX guarantees
the rename is atomic on the same filesystem, so concurrent readers see
either no file or the complete file — never a partial write. On any
exception, the ``.partial`` is cleaned up so we never leak temp files.

**Suffix allowlist (defense in depth):** ``path_for`` rejects any suffix
not in the image allowlist before constructing the path. That blocks
``../../etc/passwd``-style traversal AND prevents non-image types from
being persisted even if the caller's MIME check is bypassed.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

_ALLOWED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})


def attachment_path(
    *,
    root: Path,
    workspace_id: uuid.UUID,
    attachment_id: uuid.UUID,
    suffix: str,
) -> Path:
    """Resolve the on-disk path for one attachment.

    Suffix is normalised to lowercase and validated against the image
    allowlist. Any other value (including ``..`` traversal attempts)
    raises ``ValueError`` so the caller never accidentally writes
    outside the workspace's ``attachments/`` directory.
    """
    suffix = suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise ValueError(f"disallowed suffix: {suffix!r}")
    return root / str(workspace_id) / "attachments" / f"{attachment_id}{suffix}"


class AttachmentStorage:
    """Stateless helper that owns the ``<root>/<wsid>/attachments/`` tree.

    Methods are thin wrappers around ``os``/``shutil`` so tests can
    monkeypatch them. The caller's transaction ordering matters more
    than this class's internal state — see ``write_atomic``'s docstring
    for the upload-handler contract.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def path_for(
        self,
        *,
        workspace_id: uuid.UUID,
        attachment_id: uuid.UUID,
        suffix: str,
    ) -> Path:
        return attachment_path(
            root=self.root,
            workspace_id=workspace_id,
            attachment_id=attachment_id,
            suffix=suffix,
        )

    def write_atomic(
        self,
        *,
        workspace_id: uuid.UUID,
        attachment_id: uuid.UUID,
        suffix: str,
        data: bytes,
    ) -> Path:
        """Write bytes to a ``.partial`` temp file alongside the target,
        fsync, then ``os.replace`` to the final path. POSIX guarantees
        the rename is atomic on the same filesystem, so concurrent
        readers see either no file or the complete file — never a
        partial write.

        Caller's responsibility: if the surrounding DB transaction
        rolls back AFTER this returns, call :py:meth:`delete_one` to
        clean up the orphan file. The reverse (DB commits, write fails)
        cannot happen because the upload handler writes the file BEFORE
        inserting the DB row.
        """
        final = self.path_for(
            workspace_id=workspace_id,
            attachment_id=attachment_id,
            suffix=suffix,
        )
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

    def delete_one(
        self,
        *,
        workspace_id: uuid.UUID,
        attachment_id: uuid.UUID,
        suffix: str,
    ) -> None:
        """Idempotent: remove a single attachment file if it exists.

        The delete handler commits the DB row removal FIRST, then calls
        this. If the unlink fails after commit the file is orphaned —
        log + sweep later. The DB row is already gone so no broken-link
        from app code.
        """
        p = self.path_for(
            workspace_id=workspace_id,
            attachment_id=attachment_id,
            suffix=suffix,
        )
        if p.exists():
            p.unlink()

    def delete_workspace(self, *, workspace_id: uuid.UUID) -> None:
        """Idempotent: remove the workspace's ``attachments/`` subdir.

        Only this subdir is removed; the workspace's ``repo/`` subdir
        (B5 git workspace) is untouched. No-op if the subdir doesn't
        exist (covers workspaces that never had any attachments).
        """
        target = self.root / str(workspace_id) / "attachments"
        if target.exists():
            shutil.rmtree(target)
