"""Image attachment upload/serve helpers (neutral, no Jinja templates).

Shared by the JSON attachment endpoints in ``web/api/files.py``.
"""

from __future__ import annotations

import io
from pathlib import Path

from fastapi import HTTPException, Request, UploadFile
from PIL import Image, UnidentifiedImageError
from PIL.Image import DecompressionBombError

from call_manager.proposal_assist.attachments.storage import AttachmentStorage

# Hard caps (intentionally not env-gated for v1 — revisit if users hit them).
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per image.
_CHUNK_BYTES = 64 * 1024

_ALLOWED_MIMES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})

# Decompression-bomb guard: cap the pixel count Pillow will accept before
# probing dimensions, so a tiny-on-disk image declaring an enormous pixel
# count can't drive a multi-GB allocation. 64 Mpx (~8000x8000) far exceeds
# any legitimate attachment but is bounded.
Image.MAX_IMAGE_PIXELS = 64 * 1024 * 1024

_SUFFIX_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

# Pillow exposes formats as JPEG/PNG/WEBP/GIF (uppercase). Map back to the
# allow-listed MIME so the "client claims X, bytes are actually Y" check
# is a single equality comparison.
_PIL_FORMAT_TO_MIME = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}


def _storage_for(request: Request) -> AttachmentStorage:
    settings = request.app.state.settings
    return AttachmentStorage(root=Path(settings.workspaces_dir))


def _sanitize_filename(name: str, *, default: str = "attachment") -> str:
    """Allowlist-based: keep alnum + ``-_.``; everything else becomes ``_``.

    Explicitly rejects ``..`` runs (would otherwise pass through as ``..``
    after the allowlist filter and let a server-side header consumer
    misinterpret the path) — they collapse to a single ``.``.
    """
    if not name:
        return default
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)
    # Defense-in-depth: collapse ".." runs that survived the allowlist.
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    cleaned = cleaned.strip("._") or default
    return cleaned[:120]


async def _read_capped(file: UploadFile, max_bytes: int) -> bytes:
    """Read up to ``max_bytes + 1`` bytes from the upload, in chunks.

    If we exceed the cap, raise ``HTTPException(400)`` BEFORE returning —
    the caller never sees a too-large payload and the bytes are released
    when the function returns. Reading all-at-once via ``await file.read()``
    would be a memory DoS vector.
    """
    buf = bytearray()
    while True:
        chunk = await file.read(_CHUNK_BYTES)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise HTTPException(
                status_code=400,
                detail=f"file exceeds {max_bytes // (1024 * 1024)} MB limit",
            )
    return bytes(buf)


def _probe_image_format(data: bytes) -> tuple[str, int, int]:
    """Open the bytes with Pillow and return ``(format, width, height)``.

    Raises ``HTTPException(400)`` if the bytes are not a recognized image
    OR if the detected format is not in our allowlist. This is defense
    layer 2 against a client lying about ``content_type``.
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format
            width, height = img.size
            if width * height > Image.MAX_IMAGE_PIXELS:
                raise DecompressionBombError(
                    f"image pixel count {width * height} exceeds cap {Image.MAX_IMAGE_PIXELS}"
                )
    except DecompressionBombError as e:
        raise HTTPException(
            status_code=400, detail=f"image too large (decompression-bomb guard): {e}"
        ) from e
    except (UnidentifiedImageError, OSError) as e:
        raise HTTPException(status_code=400, detail=f"unrecognised image data: {e}") from e
    if fmt not in _PIL_FORMAT_TO_MIME:
        raise HTTPException(status_code=400, detail=f"unsupported image format: {fmt!r}")
    return fmt, width, height
