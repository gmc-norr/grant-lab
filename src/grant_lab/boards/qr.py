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

import threading
from dataclasses import dataclass
from functools import lru_cache

import segno


@dataclass(frozen=True)
class QrCode:
    modules: int  # matrix width, EXCLUDING the quiet zone; 0 means NO QR was
    # produced -- either `_MAX_URL_BYTES` refused the url before segno was
    # ever called (the common case now -- see that constant's own comment),
    # or segno's OWN encode-overflow ceiling fired anyway (`qr_for`'s
    # docstring, kept as defense-in-depth) -- the caller renders no `<svg>`
    # for this card. 0 is unambiguous: the smallest possible QR is 21
    # modules (segno version 1).
    path: str  # an SVG `d` string in module units; "" pairs with modules == 0


# Fix round 4 (final security review) reopens fix round 3's own CRITICAL.
# Round 3's whole fix was `@lru_cache` (below): it bounds REPEAT cost, never
# FIRST cost, and its own docstring already said so ("not a lower
# worst-case first-request cost ... but a bounded, ~zero REPEAT cost"). A
# board at its own worst PERMITTED shape (`items_per_page(<=64) *
# max_pages(3)` = 192 items -- `web/api/boards.py`'s `approve_items`
# ceiling) with 192 DISTINCT worst-case urls pays the full encode cost on
# its first request regardless of any cache. Measured on this branch at
# segno's own ceiling (2330 UTF-8 bytes, error level M, byte mode -- see
# `qr_for`'s docstring for the exact boundary and why it is mode-dependent):
# 177 modules, a 133,639-byte path per card; 192 of them, cold, cost 25.7 s
# of CPU and a 26.2 MB response, and warm-cache-but-gzip still cost 4.4 s of
# GIL-holding CPU per request (`get_public_board` is a sync `def`, so this
# runs in the anyio worker threadpool, not on one asyncio task at a time --
# see `_encode_lock` below for why that matters). `board_limiter` bounds
# requests per IP (60 / 15 min) -- nothing per board, nothing across IPs,
# nothing in bytes or CPU -- so nothing stood between an admin-approved (or
# candidate-set-bypassed -- see `approve_items`'s own docstring) long-URL
# call and that cost on this route's only caller.
#
# `_MAX_URL_BYTES` below is the fix, and it is bounded by COST, never by
# legibility. That distinction is the one fix round 2 got wrong and round 3
# corrected: round 2's `_MAX_URL_BYTES = 180` was picked from an unvalidated
# "~3 px/module" scanning assumption laid over a spec rule that is actually
# physical (symbol size at a scan distance), not digital (module count), and
# it fired on ordinary EU Funding & Tenders URLs (round 2's own probe: 171
# bytes, 9 bytes of headroom). Round 3 was right to remove THAT bound. It
# was wrong to conclude no bound was needed at all: `@lru_cache` repeats
# encode CPU and nothing else, so a bound belongs here regardless of what
# fix round 3's legibility question ever resolves to -- and this bound
# answers a different question than round 2's did: not "can a phone scan
# this" (nobody has scanned one; see the RECORDED OPEN QUESTION in
# `qr_for`'s own docstring, still unresolved and still not enforced here),
# but "what does encoding and shipping this cost the one unauthenticated,
# rate-limited-but-not-cached route in this app".
#
# MEASURED (this repo, segno 1.6.6, error level M, this module's own path
# algorithm -- a fixed `https://www.vinnova.se/en/calls-for-proposals/`
# prefix padded with `a` to each exact byte length, byte mode throughout):
#
#     bytes  modules  path bytes  encode (cold, ms)
#       171       53       11122               10.1   <- round 2's own real
#                                                          EU probe URL
#       180       53       11224               10.1   <- round 2's removed
#                                                          legibility bound
#       250       61       15369               13.9   <- round 2's own
#                                                          "runs past 250"
#                                                          tracking-param
#                                                          floor
#       300       69       19137               17.7
#       500       85       27595               27.3
#       800      109       43363               45.5   <- chosen bound
#      1000      121       52898               56.1
#      2330      177      133657              121.1   <- segno's own ceiling
#                                                          (byte mode)
#
# CHOSEN: `_MAX_URL_BYTES = 800`. Headroom over the two real numbers this
# repo has: 800 / 171 ~= 4.7x the measured real EU Funding & Tenders URL,
# and 800 / 250 = 3.2x the documented "portal URLs with
# `;callCode=;freeTextSearchKeyword=;typeCodes=;statusCodes=` run past 250"
# floor -- a bound that fires on either of those is the mistake fix round 2
# already made once, and this leaves headroom for further tracking-param
# growth neither probe already accounts for.
#
# ARITHMETIC at the chosen bound, both ceilings named explicitly, for the
# worst PERMITTED board shape (192 items, `approve_items`'s own ceiling),
# cold, every item a distinct never-before-seen 800-byte url (the LRU-thrash
# scenario this module's own `lru_cache` comment below describes: two
# published boards x up to 192 distinct worst-case urls each can exceed
# `maxsize=256` and land every request on the cold path):
#   - CPU: 192 x ~45.5 ms (measured encode time at 800 bytes, above)
#     ~= 8.7 s. Down from the unbounded case's measured 25.7 s -- a ~3x
#     reduction on this axis alone, before `_encode_lock` (below) removes
#     the N-concurrent-requests multiplier entirely.
#   - Bytes: 192 x (800 [the `url` field, unbounded and out of this task's
#     scope -- see `Call.url`'s own `Text NOT NULL` column] + 43,363 [the
#     `qr_path` this bound now caps] + per-item JSON/field overhead)
#     ~= 8.5 MB. Down from the unbounded case's measured 26.2 MB -- a ~3x
#     reduction. See `tests/web/test_api_public_board.py` for the real,
#     measured (not hand-computed) full-response byte count at this bound.
# Both numbers are ONE-TIME, COLD costs for a board's first request after a
# never-before-seen url is approved onto it -- not a per-request or
# per-viewer multiplier: `_encode_lock` (below) means N concurrent viewers
# of a freshly-published board pay this once, together, not N times, and
# the existing `@lru_cache` means every request after the board is warm
# costs effectively nothing (see `qr_for`'s docstring for both measurements).
#
# A card over this bound still renders its title, funder and deadline --
# only the QR is absent, which the frontend already handles
# (`qr_modules === 0` => no `<svg>`, documented on
# `PublicBoardItemOut.qr_modules` and carried into `schema.gen.ts`).
_MAX_URL_BYTES = 800

# `threading.Lock`, not asyncio: `get_public_board` (`web/api/public_boards.py`)
# is a sync `def`, so FastAPI/Starlette runs it in the anyio worker
# threadpool (default 40) on a REAL OS thread, not as one coroutine among
# many cooperatively-scheduled ones -- an `asyncio.Lock` would not exclude
# those threads from each other at all. See `qr_for`'s "STAMPEDE" section
# for what this closes and how it was verified under real threads (not
# asyncio's cooperative scheduler, which would prove nothing about a sync
# `def` running in a real thread pool).
_encode_lock = threading.Lock()


@lru_cache(maxsize=256)
def _cached_qr_for(url: str) -> QrCode:
    """The real segno encode for one url. `lru_cache`d, and ALWAYS called
    through `qr_for`'s `_encode_lock` -- never call this directly, and never
    export it. `qr_for` guarantees `len(url.encode("utf-8")) <=
    _MAX_URL_BYTES` before this is ever reached, so the
    `segno.DataOverflowError` catch below is defense-in-depth for a future
    change to that bound, not a path this branch's own tests exercise
    through `qr_for` today: 800 bytes is far below segno's own ceiling in
    every mode it supports (2331 byte mode, 3391 alphanumeric, 5596 numeric
    -- see `qr_for`'s docstring). Kept anyway for the same reason the
    module docstring above states: every caller, today's one and any
    future one, inherits the same fallback without having to remember to
    wrap the call.

    `maxsize=256`'s sizing math (this file's earlier history) was measured
    against a 2331-byte worst-case entry (~118 KB each, ~28.9 MiB at
    maxsize). `_MAX_URL_BYTES` now caps every entry that can reach this
    cache at 800 bytes -- a smaller, cheaper entry (43,363-byte path, see
    the module-level comment above) -- so that ~28.9 MiB figure is now a
    conservative OVER-estimate of the cache's real worst-case memory, not a
    stale one: the bound got safer, not looser, and 256 still comfortably
    holds a full 192-item board's worth of distinct worst-case urls without
    evicting mid-board.
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

    COST BOUND (fix round 4, reopening round 3's own CRITICAL -- see the
    module-level comment above `_MAX_URL_BYTES` for the full measured table
    and arithmetic): a url whose UTF-8 encoding exceeds `_MAX_URL_BYTES`
    (800) never reaches segno at all -- this returns the empty-QR sentinel
    immediately, before the cache or the lock below are even touched.

    THIS IS NOT A LEGIBILITY BOUND, and it must never be read as one. Fix
    round 2's `_MAX_URL_BYTES = 180` WAS a legibility bound (an unvalidated
    "~3 px/module" assumption over a spec rule that is actually about
    physical symbol size, not module count) and fix round 3 was right to
    remove it -- see that round's own reasoning, kept below. This bound
    answers a different question entirely: what a url costs THIS route to
    encode and ship, in CPU and response bytes, independent of whether the
    resulting QR would be scannable at 170 px. Whether a dense QR IS
    scannable at that size remains the RECORDED OPEN QUESTION below,
    unresolved and deliberately NOT enforced by this bound or any other
    code in this module -- a human's call, not a side effect of this
    performance/cost fix.

    STAMPEDE (fix round 4): `_encode_lock`, a single process-wide
    `threading.Lock`, wraps every call into the `lru_cache`d encode below.
    `functools.lru_cache` does NOT hold its own internal lock across the
    wrapped function call -- it only serializes the dict lookup/store
    around it -- so N concurrent callers racing the SAME never-before-cached
    url each see a miss and each run the real encode. Measured live on this
    branch, real OS threads (not asyncio, which would prove nothing about
    `get_public_board`'s actual sync-`def`-in-a-threadpool execution --
    see `_encode_lock`'s own comment): 8 threads calling the bare
    `lru_cache`-wrapped function with one shared, never-before-seen url ran
    the real encode 8 times, not once -- `tests/test_boards_qr.py`'s
    `test_N_concurrent_requests_for_the_same_cold_url_cost_one_real_encode`
    pins the FIXED behaviour (1 miss, not N) the same way, against `qr_for`
    itself. This is exactly the gap a burst of concurrent requests to a
    freshly-published, still-cold board hits on every poll:
    `get_public_board` is a sync `def`, so it runs in the anyio worker
    threadpool (default 40), not as one coroutine at a time -- forty
    concurrent cold requests could previously each pay the full encode cost
    for the same url, stalling the whole app (segno is pure Python and
    holds the GIL) for every OTHER route too, authenticated ones included.
    A single lock, not a per-url lock table, is the mechanism: two
    DIFFERENT urls were never actually encoding in parallel even before
    this lock, because segno's pure-Python encode already holds the GIL for
    its whole duration -- a per-url lock table would buy no real
    concurrency over that baseline while adding one `threading.Lock` object
    per distinct url ever approved onto any board, for the life of the
    process, with no eviction. One lock keeps the fix's state at one
    object.

    `qr_for`'s previous docstring (fix round 3) claimed "one real encode per
    URL followed by many free repeats" as `@lru_cache`'s own guarantee. That
    was true SERIALLY ONLY, which is a materially different -- and false as
    written -- claim about a route that is reached by concurrent,
    unauthenticated, un-sessioned callers by construction. This round's
    fix, and this corrected docstring, close that gap: the guarantee now
    genuinely holds under concurrent callers, not merely under a single
    caller repeating itself. CACHED, still, exactly as fix round 3 left it
    (`@lru_cache(maxsize=256)` on `_cached_qr_for`, see its own docstring
    for the maxsize math): a corridor board is polled repeatedly by a
    static display (a ~15-minute rate-limit window over a ~5-minute
    client-side cache is ~3 requests per screen, and a board's items do not
    change between board edits), so the realistic cost profile is one real
    encode per url followed by many free repeats -- now true under
    concurrency too, not only in series.

    A url too large for ANY QR version at all (segno's OWN ceiling,
    independent of `_MAX_URL_BYTES` and independent of legibility) raises
    `segno.DataOverflowError`. MEASURED (segno 1.6.6, error level M, this
    branch's own re-measurement -- a prior round of this same review
    asserted "2331 encodes / 2332 raises"; re-measuring rather than
    trusting that number found it was ALREADY correct for byte-mode
    content, not wrong): 2331 byte-mode characters still encode, 2332
    raises "Data too large. No QR Code can handle the provided data". This
    boundary is MODE-DEPENDENT, which the docstring this replaces never
    said: segno picks numeric mode for an all-digit string and alphanumeric
    mode for a string drawn only from `0-9A-Z $%*+-./:`, both of which pack
    more bits per character than byte mode, so a URL segno happens to
    recognise as one of those modes overflows much later -- measured, same
    error level: 3391 alphanumeric characters still encode (3392 raises),
    and 5596 numeric characters still encode (5597 raises). None of this
    matters for THIS route in practice any more: `_MAX_URL_BYTES` (800)
    refuses a url in ANY mode long before segno's own ceiling in the
    lowest-capacity mode (byte, 2331) is reachable, so the catch below is
    defense-in-depth for a future change to that bound, not a path this
    route's own tests can reach today. `Call.url` is `Text NOT NULL` with
    no length bound (`db/models/calls.py`), populated by ingestion adapters
    this task does not own, so a URL over either ceiling is a real
    possibility, not a hypothetical -- and either way this returns the
    empty-QR sentinel (modules=0, path="") rather than raising, matching
    `PublicBoardItemOut`'s non-optional `qr_modules`/`qr_path` fields: one
    bad or merely-expensive URL costs one missing QR, not the whole board.

    RECORDED OPEN QUESTION, NOT ENFORCED, left for a human: whether a dense
    QR is legible at the spec's 170 px on-screen size. The spec
    (`docs/superpowers/specs/2026-09-03-call-match-release-1-design.md`,
    section 6) never mentions modules or pixels-per-module -- its rule is
    physical, not digital: "A QR code must be >= 1/10 of its scanning
    distance", and separately "QR 170 px (~10.6 cm on a 55" screen)". At
    that 170 px size, a 49-module symbol (the real EU Funding & Tenders
    probe url, 171 bytes) is ~3.5 px/module and a 109-module symbol (this
    round's own 800-byte bound) is ~1.56 px/module -- so a url anywhere
    near this bound MAY produce a QR too dense to scan on the corridor
    wall. No one has scanned one to find out, and this module still does
    not decide the question -- `_MAX_URL_BYTES` above is a cost ceiling,
    not a verdict on legibility. If this turns out to be a real problem,
    the fix a human should choose from is a larger QR box (the spec calls
    170 px a floor, not a target) or stripping tracking parameters before
    encoding -- not silently blanking the card below some assumed
    px-per-module threshold, which is the mistake fix round 2 already made
    once.
    """
    if len(url.encode("utf-8")) > _MAX_URL_BYTES:
        return QrCode(modules=0, path="")
    with _encode_lock:
        return _cached_qr_for(url)


# `cache_info()`/`cache_clear()` observe the REAL, `lru_cache`d function.
# `qr_for` itself is a plain wrapper (the `_MAX_URL_BYTES` check plus
# `_encode_lock`), not an `lru_cache` object, so it carries neither
# attribute on its own -- delegating both keeps `qr_for.cache_info()`
# meaningful to a caller (this module's own tests included) without
# exporting `_cached_qr_for` itself.
qr_for.cache_info = _cached_qr_for.cache_info
qr_for.cache_clear = _cached_qr_for.cache_clear
