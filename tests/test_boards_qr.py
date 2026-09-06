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

import pytest
import segno

from grant_lab.boards.qr import qr_for
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
    external ingestion adapters (IMPORTANT 1). Measured, segno 1.6.6, error
    level M: 2331 'x' characters still fits; 2332 raises
    `segno.DataOverflowError`. `qr_for` must swallow exactly that boundary
    and hand back the sentinel (modules=0, path=""), not propagate.

    Fix round 3: this is now the ONLY way `qr_for` returns the sentinel --
    round 2's separate, tighter `_MAX_URL_BYTES` (180) legibility guard was
    removed (see `qr.py`'s module and `qr_for` docstrings for why: the spec
    never bounded module count, only physical symbol size, and the bound
    was never validated against a real scan). This test still calls
    `segno.make_qr` directly (not through `qr_for`) to pin segno's OWN raw
    exception boundary on its own, independent of `qr_for`'s handling of it.

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

    qr = qr_for(overflows)
    assert qr.modules == 0
    assert qr.path == ""


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
