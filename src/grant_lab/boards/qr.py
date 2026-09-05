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
    modules: int  # matrix width, EXCLUDING the quiet zone
    path: str  # an SVG `d` string in module units


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
    """
    q = segno.make_qr(url, error="m")
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
