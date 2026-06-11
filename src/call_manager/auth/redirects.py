from __future__ import annotations

from urllib.parse import urlparse

# Cap the relative path we round-trip through the magic-link URL. An
# allowed user could otherwise submit a giant `next` and inflate the
# emailed link (and every redirect that carries it).
MAX_NEXT_BYTES = 2048


def safe_next(path: str | None) -> str | None:
    """Validate an open-redirect-safe local relative path.

    Returns the path unchanged if it is a safe local target, else None
    (callers fall back to "/"). Accepts ONLY a local relative path:

    * must start with exactly one "/" (rejects "//host", "/\\host")
    * no backslashes anywhere, no control characters
    * urlparse must yield neither a scheme nor a netloc
    * at most MAX_NEXT_BYTES bytes

    The returned value is the DECODED path; it may itself contain a query
    string (e.g. "/calls/5?profile=3"), so callers MUST urlencode it when
    embedding it into a URL — never string-interpolate.
    """
    if not path:
        return None
    if len(path.encode("utf-8")) > MAX_NEXT_BYTES:
        return None
    if not path.startswith("/"):
        return None
    if path.startswith("//") or path.startswith("/\\"):
        return None
    if "\\" in path:
        return None
    if any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in path):
        return None
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc:
        return None
    return path
