"""QR codes are encoded server-side, from segno's documented matrix.

No QR encoder exists on either side of this repo and `pnpm add` is blocked by
the 7-day minimumReleaseAge cooldown, so the frontend cannot gain one. The
payload therefore carries path data, not an image URL: D6 requires the
last-good copy to keep rendering through a transport failure, and eight broken
QR images beside eight live titles is worse than a stale board.

Built from `q.matrix` rather than by parsing segno's SVG serializer, because a
regex over a library's output pins a private format.
"""

from __future__ import annotations

import threading

import pytest
import segno

from grant_lab.boards.qr import _MAX_URL_BYTES, qr_for
from tests.qr_path_test_helpers import QR_CMD as _CMD
from tests.qr_path_test_helpers import matrix_from_qr_path as _matrix_from_path


def test_a_qr_encodes_the_url_and_round_trips():
    url = "https://example.test/x"
    qr = qr_for(url)
    expected = [list(row) for row in segno.make_qr(url, error="m").matrix]
    assert _matrix_from_path(qr.path, qr.modules) == expected


def test_the_matrix_excludes_the_quiet_zone():
    url = "https://example.test/quiet-zone"
    qr = qr_for(url)
    q = segno.make_qr(url, error="m")
    assert qr.modules == len(q.matrix)
    assert q.symbol_size(scale=1, border=0) == (qr.modules, qr.modules)


def test_the_path_is_only_move_line_and_close_commands():
    qr = qr_for("https://example.test/commands")
    assert qr.path  # non-empty: a QR always has at least one dark module
    for ch in qr.path:
        assert ch in "Mhvz-.0123456789 ", f"unexpected character {ch!r} in path"
    # And every command matches the exact grammar this module emits.
    reconstructed = "".join(m[0] for m in _CMD.finditer(qr.path))
    assert reconstructed == qr.path


def test_a_longer_url_needs_more_modules():
    short = qr_for("https://a.se/short")
    long_ = qr_for("https://example.test/" + "x" * 100)
    assert long_.modules > short.modules


def test_the_path_contains_no_angle_bracket_or_quote():
    qr = qr_for('https://example.test/<script>alert(1)</script>?a="b"')
    for ch in "<>\"'&":
        assert ch not in qr.path, f"{ch!r} found in path meant for an SVG attribute"


def test_an_over_long_url_returns_the_empty_qr_sentinel_not_a_raise():
    """`Call.url` is `Text NOT NULL` with no length bound and is populated by
    external ingestion adapters (IMPORTANT 1). Re-measured for fix round 4
    (a prior round of this same review asserted the boundary was "2330
    encodes / 2331 raises" -- re-measuring rather than trusting that number
    found it was already correct as originally written): segno 1.6.6, error
    level M, BYTE mode (this fixture's plain 'x' repeats are not valid QR
    alphanumeric or numeric content): 2331 characters still fits, 2332
    raises `segno.DataOverflowError`. This boundary is MODE-DEPENDENT --
    also re-measured, same error level: 3391 alphanumeric characters
    ('A' * n) still fit (3392 raises), and 5596 numeric characters ('1' * n)
    still fit (5597 raises), because both modes pack more bits per
    character than byte mode. `qr_for` must swallow exactly the byte-mode
    boundary pinned here and hand back the sentinel (modules=0, path=""),
    not propagate.

    Fix round 4: this is NO LONGER the mechanism that actually fires for
    this test's own inputs -- `_MAX_URL_BYTES` (800, a measured COST bound,
    not a legibility one; see `qr.py`'s module comment) now refuses any url
    over it, in any mode, long before segno's own per-mode ceilings above
    are ever reachable. This test still calls `segno.make_qr` directly (not
    through `qr_for`) to pin segno's OWN raw exception boundary on its own,
    independent of `qr_for`'s handling of it; the `qr_for(overflows)` call
    below now returns the sentinel via `_MAX_URL_BYTES` first, which is
    fine -- the sentinel is the contract, not which of the two mechanisms
    produced it. `test_a_url_over_the_cost_bound_...` below pins the
    mechanism that fires in practice.

    Uses a distinct 'x' run from every other test in this file and
    `tests/web/test_api_public_board.py` -- `qr_for` is now `lru_cache`d, and
    a shared fixture string across tests would hide a "does this actually
    re-check overflow" bug behind a stale cache hit.
    """
    fits = "x" * 2331
    overflows = "x" * 2332
    segno.make_qr(fits, error="m")  # does not raise -- pins the boundary
    with pytest.raises(segno.DataOverflowError):
        segno.make_qr(overflows, error="m")

    alnum_fits, alnum_overflows = "A" * 3391, "A" * 3392
    segno.make_qr(alnum_fits, error="m")
    with pytest.raises(segno.DataOverflowError):
        segno.make_qr(alnum_overflows, error="m")

    numeric_fits, numeric_overflows = "1" * 5596, "1" * 5597
    segno.make_qr(numeric_fits, error="m")
    with pytest.raises(segno.DataOverflowError):
        segno.make_qr(numeric_overflows, error="m")

    qr = qr_for(overflows)
    assert qr.modules == 0
    assert qr.path == ""


def test_a_url_at_the_cost_bound_still_produces_a_real_qr():
    """Fix round 4: `_MAX_URL_BYTES` (currently 800) is a COST bound, sized
    from measured encode time and response bytes -- see `qr.py`'s module
    comment for the full table and arithmetic, and `qr_for`'s docstring for
    why this must never be read as a legibility judgement (that question is
    recorded there as still open and still unenforced). A url exactly AT
    the bound must still get a real QR; the test below pins the other side
    of the same boundary.
    """
    base = "https://example.test/at-cost-bound/"
    url = base + "d" * (_MAX_URL_BYTES - len(base))
    assert len(url.encode("utf-8")) == _MAX_URL_BYTES
    qr = qr_for(url)
    assert qr.modules > 0
    assert qr.path


def test_a_url_over_the_cost_bound_returns_the_empty_qr_sentinel_not_a_raise():
    """One byte over `_MAX_URL_BYTES` must return the sentinel -- and, unlike
    `test_an_over_long_url_returns_the_empty_qr_sentinel_not_a_raise` above,
    this url is nowhere near segno's own byte-mode ceiling (2331), so a
    sentinel here can only come from `_MAX_URL_BYTES`, proving the cost
    bound itself is what fires, not merely that segno's ceiling still
    works.
    """
    base = "https://example.test/over-cost-bound/"
    url = base + "e" * (_MAX_URL_BYTES + 1 - len(base))
    assert len(url.encode("utf-8")) == _MAX_URL_BYTES + 1
    qr = qr_for(url)
    assert qr.modules == 0
    assert qr.path == ""


def test_a_realistic_tracking_heavy_url_comfortably_clears_the_cost_bound():
    """Fix round 2's own mistake, not repeated: it picked a legibility bound
    (180 bytes) that fired on real funder urls -- a real EU Funding &
    Tenders url measured at 171 bytes (9 bytes of headroom), and real
    portal urls carrying tracking querystrings
    (`;callCode=;freeTextSearchKeyword=;typeCodes=;statusCodes=`) run past
    250. `_MAX_URL_BYTES` (800) clears both with 3.2x-4.7x headroom (see
    `qr.py`'s module comment for the measured table) -- this pins a
    representative tracking-heavy url, padded to 300 bytes (past the
    documented 250-byte real-world floor), still getting a real QR rather
    than a sentinel.
    """
    base = (
        "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/"
        "opportunities/topic-details/horizon-cl4-2026-01;callCode=;"
        "freeTextSearchKeyword=;typeCodes=;statusCodes="
    )
    url = base + "g" * (300 - len(base.encode("utf-8")))
    assert len(url.encode("utf-8")) == 300
    assert 250 < len(url.encode("utf-8")) < _MAX_URL_BYTES
    qr = qr_for(url)
    assert qr.modules > 0
    assert qr.path


def test_N_concurrent_requests_for_the_same_cold_url_cost_one_real_encode(monkeypatch):
    """Fix round 4 STAMPEDE closure. `functools.lru_cache` does NOT hold its
    own lock across the wrapped call -- proved here the same way the review
    proved the bug: 8 real OS threads (a `threading.Barrier` starts them
    together, not asyncio, which would prove nothing about
    `get_public_board`'s actual sync-`def`-in-a-threadpool execution --
    see `qr.py`'s `_encode_lock` comment) racing ONE never-before-seen url
    through `qr_for` must produce exactly ONE real call into segno, not up
    to 8 -- `segno.make_qr` is monkeypatched with a counting wrapper to
    observe the real encode count directly, rather than inferring it from
    `cache_info()` (which only becomes consistent again after every racer
    has returned, not during the race itself).
    """
    real_make_qr = segno.make_qr
    calls: list[int] = []
    count_lock = threading.Lock()

    def counting_make_qr(*args, **kwargs):
        with count_lock:
            calls.append(1)
        return real_make_qr(*args, **kwargs)

    monkeypatch.setattr(segno, "make_qr", counting_make_qr)

    url = "https://example.test/stampede-check-" + "h" * 60
    n_threads = 8
    barrier = threading.Barrier(n_threads)

    def worker():
        barrier.wait()  # start all 8 together, maximizing the race window
        qr_for(url)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1, (
        f"{len(calls)} real encodes for {n_threads} concurrent requests to the "
        "SAME cold url -- _encode_lock did not close the stampede"
    )


def test_a_repeated_call_hits_the_cache_not_a_re_encode():
    """Fix round 3: `qr_for` is `@lru_cache(maxsize=256)`d so a corridor
    board's repeat requests (the realistic access pattern -- a static
    display polling the same slug) cost one real encode per URL, not one
    per request. `cache_info()` is the deterministic way to observe this
    (preferred over timing, which is noisy under test-suite load): a fresh
    URL, unlikely to have been cached by any other test in this file, must
    register exactly one miss on first call and a hit -- not a second
    miss -- on every call after.
    """
    url = "https://example.test/cache-hit-check-" + "z" * 40
    before = qr_for.cache_info()
    first = qr_for(url)
    after_first = qr_for.cache_info()
    assert after_first.misses == before.misses + 1
    assert after_first.hits == before.hits

    second = qr_for(url)
    after_second = qr_for.cache_info()
    assert after_second.misses == after_first.misses  # no new encode
    assert after_second.hits == after_first.hits + 1
    assert second is first  # same cached object, not merely equal


def test_the_symbol_is_never_a_micro_qr():
    """13 characters -- deliberate. Every other URL in this file is long enough
    to hide the bug this pins: `segno.make(url, error="m")` returns a Micro QR
    (M4, level M) for this exact string, while `make_qr` refuses the family
    outright. A Micro QR round-trips perfectly, so no OTHER test here can see
    the difference -- this is the one fixture short enough to.
    """
    url = "https://a.se/"
    assert len(url) == 13
    q = segno.make_qr(url, error="m")
    assert q.is_micro is False
    qr = qr_for(url)
    assert qr.modules == len(q.matrix)
