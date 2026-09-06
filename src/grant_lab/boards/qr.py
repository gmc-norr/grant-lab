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
from functools import lru_cache

import segno


@dataclass(frozen=True)
class QrCode:
    modules: int  # matrix width, EXCLUDING the quiet zone; 0 means NO QR was
    # produced (segno's OWN encode-overflow ceiling, ~2331 UTF-8 bytes at
    # error level M -- see `qr_for`) -- the caller renders no `<svg>` for
    # this card. 0 is unambiguous: the smallest possible QR is 21 modules
    # (segno version 1).
    path: str  # an SVG `d` string in module units; "" pairs with modules == 0


# Fix round 3: reverses fix round 2's `_MAX_URL_BYTES` = 180 legibility bound.
# That bound was removed, not just relaxed -- see `qr_for`'s docstring for
# why, and for the measurement it demotes to a recorded-but-unenforced open
# question. `qr_for` is cached instead: it is a pure `str -> frozen dataclass`
# function, so repeat calls with a URL already seen cost nothing.
#
# Preconditions checked before applying `lru_cache` (both required; neither
# was assumed):
# - HASHABLE key: `qr_for`'s only argument is a plain `str`, always hashable.
# - The cached VALUE must never be mutated by a caller, or every cache hit
#   after the first would hand out a mutated object. `QrCode` is
#   `@dataclass(frozen=True)`, and both its fields (`int`, `str`) are
#   themselves immutable, so the whole object is structurally safe from
#   mutation, not merely by convention. Confirmed live, not assumed:
#   `q1.modules = 2` on a `QrCode` instance raises
#   `dataclasses.FrozenInstanceError`. A frozen dataclass with the default
#   `eq=True` also gets a generated `__hash__` (confirmed:
#   `hash(QrCode(1, "a")) == hash(QrCode(1, "a"))` is `True`), though that
#   hash is unused here -- `lru_cache` hashes the URL argument, not the
#   return value.
#
# `maxsize` is sized from a MEASURED per-entry cost, not a guess. Measured
# (this file's own worst case: a 2331-UTF-8-byte URL, segno's own
# `DataOverflowError` ceiling at error level M -- see `qr_for`): a `QrCode`
# for that URL carries a 115,305-byte path (177 modules), and the actual
# cache-entry cost (`tracemalloc`, snapshot diff over 200 distinct
# same-length entries, dividing the total by 200 to average out per-call
# noise) is ~118,346 bytes -- the url key, the `QrCode` object, and the
# `functools` cache-node overhead together, not just the path string alone.
# `maxsize=256` therefore bounds the cache's own worst-case memory to
# 256 x 118,346 ~= 28.9 MiB, while still holding a full 192-item board's
# worth of DISTINCT worst-case URLs (`items_per_page(<=64) * max_pages(3)`,
# the real ceiling `web/api/boards.py`'s `approve_items` enforces) without
# evicting mid-board. Real funder URLs are far shorter than 2331 bytes (fix
# round 2's own probe measured a real EU Funding & Tenders URL at 171
# bytes), so this is the pathological ceiling, not the typical cost.
@lru_cache(maxsize=256)
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

    CACHED (fix round 3): `@lru_cache(maxsize=256)`, see the module-level
    comment above for the hashability/mutation preconditions and how
    `maxsize` was measured. A corridor board is polled repeatedly by a
    static display (the caller's own docstring: a ~15-minute rate-limit
    window over a ~5-minute client-side cache is ~3 requests per screen, and
    the board's items do not change between board edits) -- so the realistic
    cost profile is one real encode per URL followed by many free repeats,
    not a fresh encode on every request. Measured (this repo, segno 1.6.6,
    a 2331-UTF-8-byte URL -- the worst case, see below): first call (cold,
    a real encode) ~127 ms; every subsequent call with the SAME url (warm,
    a cache hit) ~0.09 microseconds -- effectively free. For a full 192-item
    board (`items_per_page(<=64) * max_pages(3)`) where every item carries a
    DISTINCT worst-case URL: the FIRST request to that board still costs
    ~24.2 s of real encoding (measured) -- the cache cannot make novel work
    free, only repeat work -- but every SECOND and later request to the SAME
    board, once all 192 URLs are warm, costs ~0.03 ms total. That is the
    guarantee this closes fix round 2's CRITICAL finding with: not a lower
    worst-case first-request cost (encoding real QR data has a real CPU
    cost, whatever bounds a URL's length), but a bounded, ~zero REPEAT cost,
    which is what an anonymous, unauthenticated, rate-limited-but-not-cached
    endpoint is actually exposed to on every poll.

    Fix round 2 also added `_MAX_URL_BYTES = 180`: any URL whose UTF-8
    encoding exceeded it returned the empty-QR sentinel before segno was
    ever called, on the theory that a symbol denser than ~53 modules could
    not be scanned at the spec's 170 px legibility floor. Fix round 3
    REMOVES that bound entirely -- not because the underlying arithmetic
    was wrong, but because removing a working QR from a class of real calls
    is a product decision, not a side effect of a performance fix, and the
    bound was never validated:

    - The spec (`docs/superpowers/specs/2026-09-03-call-match-release-1-design.md`,
      section 6) never mentions modules or pixels-per-module. Its rule is
      physical, not digital: "A QR code must be >= 1/10 of its scanning
      distance", and separately, "QR 170 px (~10.6 cm on a 55" screen). The
      1/10 rule at the 1 m maximum scanning distance stated above needs 10
      cm". Both constrain the symbol's PRINTED/DISPLAYED SIZE at a given
      scan distance -- neither says anything about how many modules may
      subdivide that physical size.
    - The removed bound's own "~3 px/module for a phone to read it reliably"
      threshold was never measured: no phone, no camera, no scan, just an
      assumption laid over the spec's physical rule. The cliff it produced
      (53 modules / 3.21 px-per-module = OK, 57 modules / 2.98 = blanked)
      is a 7% arithmetic difference asserted as a hard binary.
    - The blast radius was never measured either (no
      `SELECT count(*) FROM calls WHERE octet_length(url) > 180` was ever
      run), and the one real probe that WAS done argued against the bound:
      a real EU Funding & Tenders URL measured at 171 bytes -- 9 bytes of
      headroom -- and real portal URLs carrying tracking querystrings
      (`;callCode=;freeTextSearchKeyword=;typeCodes=;statusCodes=`) run past
      250. Ordinary calls, not pathological ones, would silently lose their
      QR, and nothing would log, report, or surface that to anyone.

    RECORDED OPEN QUESTION, NOT ENFORCED: the module-count-vs-legibility
    data above is still real and still useful, just not acted on here. At
    170 px (spec §6's own on-screen size), a 49-module symbol is ~3.5
    px/module and a 177-module symbol (segno's own maximum, at the
    2331-byte `DataOverflowError` boundary below) is ~0.96 px/module -- so a
    very long URL MAY produce a QR too dense to scan on the corridor wall.
    No one has scanned one to find out. If this turns out to be a real
    problem, the fix a human should choose from is a larger QR box (the
    spec calls 170 px a floor, not a target, so there is room to grow it)
    or stripping tracking parameters before encoding -- not silently
    blanking the card, which is what fix round 2 did and this round undoes.

    A URL that is too large for ANY QR version at all (segno's OWN ceiling,
    independent of legibility) raises `segno.DataOverflowError` (measured,
    segno 1.6.6, error level M: 2331 ASCII characters encodes, 2332 raises
    "Data too large. No QR Code can handle the provided data"). `Call.url`
    is `Text NOT NULL` with no length bound (`db/models/calls.py`),
    populated by ingestion adapters this task does not own, so a URL over
    that ceiling is a real possibility, not a hypothetical. `qr_for` is the
    ONLY module that imports segno, so every caller -- today's one and any
    future one -- inherits the same fallback without having to remember to
    wrap the call. Returns the empty-QR sentinel (modules=0, path="") rather
    than raising, matching `PublicBoardItemOut`'s non-optional
    `qr_modules`/`qr_path` fields: one bad URL costs one missing QR, not the
    whole board.
    """
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
