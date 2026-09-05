"""HTTP header helpers shared by the routes.

`content_disposition` exists because Starlette encodes response headers as
latin-1 and every non-Latin-1 filename this app built — a workspace title with
CJK, an en-dash, an emoji; an uploaded attachment named in Chinese — raised
UnicodeEncodeError → 500 (review 2026-09-02 A1 item 8, reproduced on the export
routes and the attachment serve). RFC 6266 with RFC 5987: an ASCII fallback in
`filename=` for old clients, the real name percent-encoded in `filename*=`.
"""

from __future__ import annotations

import re
from urllib.parse import quote

# Characters that survive into the quoted ASCII fallback. Quotes, backslashes,
# control characters and anything non-ASCII become underscores; a header value
# must never contain CR/LF (response splitting) and the fallback is inside quotes.
_FALLBACK_BAD = re.compile(r"[^A-Za-z0-9._\- ()\[\]+=,;@!#$%&\'~^{}]")


def content_disposition(disposition: str, filename: str) -> str:
    """`attachment; filename="…"` (plus `; filename*=UTF-8''…` when the name is
    not plain ASCII). Safe to hand to a Starlette Response as-is."""
    fallback = _FALLBACK_BAD.sub("_", filename) or "download"
    header = f'{disposition}; filename="{fallback}"'
    if not filename.isascii():
        header += f"; filename*=UTF-8''{quote(filename, safe='')}"
    return header
