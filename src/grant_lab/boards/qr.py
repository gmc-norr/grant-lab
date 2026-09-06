"""Server-side QR encoding for the corridor board.

D6 requires the last-good copy of a board to keep rendering through a
transport failure, and this repo has NO QR encoder on the frontend (`pnpm
add` is blocked by the 7-day `minimumReleaseAge` cooldown, so the frontend
cannot gain one either). The payload therefore carries SVG path data, not an
image URL: eight broken QR `<img>`s beside eight live titles on a corridor
wall is worse than a stale board, and path data survives in the same cached
JSON the titles do.
"""

from __future__ import annotations

from dataclasses import dataclass

import segno


@dataclass(frozen=True)
class QrCode:
    modules: int  # matrix width, EXCLUDING the quiet zone; 0 means NO QR was
    # produced (URL over `_MAX_URL_BYTES`, or an ANY-version encode overflow)
    # -- the caller renders no `<svg>` for this card. 0 is unambiguous: the
    # smallest possible QR is 21 modules (segno version 1).
    path: str  # an SVG `d` string in module units; "" pairs with modules == 0


# Fix round 2, CRITICAL: the module-count-vs-legibility bound, derived from
# spec, not from taste.
#
# Spec §6 puts the QR at a 170 px legibility floor on the corridor screen. A
# phone camera / scanner needs roughly 3+ display pixels per module to read a
# symbol reliably, so the largest module count that still clears the floor at
# 170 px is floor(170 / 3) == 56 modules.
#
# QR (non-Micro) module counts step by 4 per version: size = 4*version + 17,
# so the actual sizes near that ceiling are version 9 -> 53 modules and
# version 10 -> 57 modules. 53 modules gives 170/53 == 3.21 px/module (clears
# the floor); 57 modules gives 170/57 == 2.98 px/module (already below 3, i.e.
# unreadable by the same phone that read version 9 fine). So the bound is
# version 9 / 53 modules, not the arithmetic 56 -- there is no real QR size
# between 53 and 57 to round to.
#
# Measured which URL byte-lengths land in each version, segno 1.6.6, error
# level M, byte-mode encoding (any URL with a lowercase letter forces byte
# mode, so this holds for real funder URLs, not just the ASCII 'x' probe
# used to find the boundary):
#   153-180 bytes -> version 9  (53 modules, 3.21 px/module @ 170px -- OK)
#   181+     bytes -> version 10 (57 modules, 2.98 px/module @ 170px -- FAILS)
# (confirmed with a realistic mixed-character 171-byte EU-portal-shaped URL,
# not just the probe string: also 53 modules.)
#
# So 180 bytes is the longest URL whose symbol still scans at the spec's own
# 170 px floor. A symbol denser than that is a dead card on the wall whether
# or not it is encoded -- see `qr_for`'s docstring for the sentinel this
# returns instead. Re-derive this bound (do not just bump the number) if
# either the 170 px display size or the "~3 px/module" scan-legibility
# assumption changes.
_MAX_URL_BYTES = 180


def qr_for(url: str) -> QrCode:
    """The QR for one call's URL, as SVG path data in module units.

    `q.matrix` is documented, is int 0/1, and EXCLUDES the quiet zone (measured:
    a 33-module symbol has 33 matrix rows and symbol_size(scale=1, border=0) ==
    (33, 33)). The 4-module quiet zone the QR spec requires is supplied by the
    renderer's viewBox, not by this path.

    Error level M: spec §6 puts the QR at 170 px on a screen someone steps up to,
    where the extra correction of Q/H buys nothing and costs modules.

    `make_qr`, NOT `make`. `error="m"` constrains the correction LEVEL, not the
    symbol FAMILY, and Micro QR M4 supports level M -- so `segno.make(url,
    error="m")` returns a 17x17 Micro QR for a 13-character URL (measured, segno
    1.6.6: 'https://a.se/' -> version M4, is_micro True, 17 rows; `make_qr` gives
    version 1, 21 rows). Many phone cameras and much scanner hardware cannot read
    a Micro QR, and on a corridor wall that is a silent per-card failure that
    looks exactly like a working QR. `make_qr` refuses the family outright;
    `make(url, error="m", micro=False)` is equivalent.

    CRITICAL (fix round 2): a URL over `_MAX_URL_BYTES` (180 UTF-8 bytes)
    returns the sentinel BEFORE segno is ever called -- see that constant's
    comment for the full 170px / px-per-module derivation. This caps two
    things that Round 1's `DataOverflowError` catch (below) did not:

    - BYTES: an unbounded `Call.url` (`Text NOT NULL`, no length bound,
      `db/models/calls.py`, populated by ingestion adapters this task does
      not own) previously produced up to 134,073 bytes of path per item
      (measured at the 2331-byte DataOverflowError boundary) x however many
      items a board holds. Capped now: the worst a single item can cost is
      the 180-byte boundary's own output (measured: 53 modules, 11,152 bytes
      of path, ~10.8 ms to encode -- see `tests/test_boards_qr.py` and
      `tests/web/test_api_public_board.py` for the reproduction).
    - CPU: a long URL was expensive to encode WHETHER OR NOT its symbol would
      ever be legible (measured: ~128 ms at the 2331-byte boundary vs. ~10.8
      ms at the new 180-byte one). Checking the byte length before calling
      segno at all means an over-bound URL costs a `len()` call, not an
      encode -- the CPU ceiling for a 192-item board (the real
      `items_per_page(<=64) * max_pages(default 3)` maximum, not a
      hypothetical) is now ~192 x 10.8 ms =~ 2.1 s of synchronous work in the
      worst case, not ~192 x 128 ms =~ 24.6 s.

    A URL under the bound that is STILL too large for any QR version (segno's
    OWN ceiling, independent of legibility) raises `segno.DataOverflowError`
    (measured, segno 1.6.6, error level M: 2331 ASCII characters encodes,
    2332 raises "Data too large. No QR Code can handle the provided data").
    That boundary (2331/2332) sits far above `_MAX_URL_BYTES` (180), so this
    branch is unreachable today -- kept as defense-in-depth in case a future
    reader changes `_MAX_URL_BYTES` without also re-checking this one; do not
    delete it as "dead code" without re-deriving both bounds together.
    Caught HERE, not at the call site: `qr_for` is the only module that
    imports segno, so every caller -- today's one and any future one --
    inherits the same fallback without having to remember to wrap the call.
    Returns the empty-QR sentinel (modules=0, path="") rather than raising,
    matching `PublicBoardItemOut`'s non-optional `qr_modules`/`qr_path`
    fields: one bad URL costs one missing QR, not the whole board.
    """
    if len(url.encode("utf-8")) > _MAX_URL_BYTES:
        return QrCode(modules=0, path="")
    try:
        q = segno.make_qr(url, error="m")
    except segno.DataOverflowError:
        return QrCode(modules=0, path="")
    parts: list[str] = []
    for y, row in enumerate(q.matrix):
        x = 0
        while x < len(row):
            if row[x]:
                run = 1
                while x + run < len(row) and row[x + run]:
                    run += 1
                parts.append(f"M{x} {y}h{run}v1h-{run}z")
                x += run
            else:
                x += 1
    return QrCode(modules=len(q.matrix), path="".join(parts))
