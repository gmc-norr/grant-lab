"""Reference upload/serve/metadata helpers (neutral, no Jinja templates).

The upload helpers are distinct from the image helpers in
``proposal_assist/web/_file_uploads.py`` (different MIME allowlist, size cap,
and sanitizer). ``_parse_authors`` is the CSL author-list parser used by the
metadata-approval route.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import HTTPException, Request, UploadFile

from call_manager.proposal_assist.references.storage import ReferenceStorage

_ALLOWED_MIMES = frozenset(
    {
        "application/pdf",
        "text/markdown",
        "text/plain",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/rtf",
        "text/csv",
        # Legacy .xls (application/vnd.ms-excel) is intentionally NOT accepted:
        # openpyxl can't read the binary BIFF format, so it would always fail
        # extraction. Users should upload .xlsx (OOXML).
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/x-ipynb+json",
    }
)
_MAX_BYTES = 50 * 1024 * 1024  # 50 MB per spec REFERENCES_MAX_BYTES default
_CHUNK_BYTES = 64 * 1024

_FILENAME_OK = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_filename(name: str) -> str:
    cleaned = _FILENAME_OK.sub("_", name)
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    cleaned = cleaned.strip("._")
    return cleaned[:120] or "reference"


def _storage_for(request: Request) -> ReferenceStorage:
    return ReferenceStorage(root=Path(request.app.state.settings.workspaces_dir))


async def _read_capped(upload: UploadFile, max_bytes: int) -> bytes:
    buf = bytearray()
    while True:
        chunk = await upload.read(_CHUNK_BYTES)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            # Include the actual size observed so far (capped to max+1)
            # so the user knows roughly how oversize it is — "your 75 MB
            # file exceeds the 50 MB limit" is more actionable than
            # "exceeds 50 MB".
            observed_mb = len(buf) / (1024 * 1024)
            raise HTTPException(
                status_code=400,
                detail=(
                    f"file (>{observed_mb:.1f} MB) exceeds the "
                    f"{max_bytes // (1024 * 1024)} MB upload limit"
                ),
            )
    return bytes(buf)


def _parse_authors(text: str) -> list[dict]:
    """Parse a newline-separated author string into a CSL author list.

    Decoding rules (inverse of ``_authors_to_lines``):
    - Lines containing a comma are split on the FIRST comma into family/given.
      ``given`` is only included in the dict when it is non-empty, so
      ``"WHO,"`` → ``{"family": "WHO"}`` and ``"Roe, Jane"`` →
      ``{"family": "Roe", "given": "Jane"}``.
    - Lines with no comma → ``{"literal": line}``.
    - Blank lines are skipped.
    """
    authors = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "," in line:
            family, _, given = line.partition(",")
            family = family.strip()
            given = given.strip()
            entry: dict = {"family": family}
            if given:
                entry["given"] = given
            authors.append(entry)
        else:
            authors.append({"literal": line})
    return authors
