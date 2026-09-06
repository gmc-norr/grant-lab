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
    long_ = qr_for("https://example.test/" + "x" * 200)
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
    """
    fits = "x" * 2331
    overflows = "x" * 2332
    segno.make_qr(fits, error="m")  # does not raise -- pins the boundary
    with pytest.raises(segno.DataOverflowError):
        segno.make_qr(overflows, error="m")

    qr = qr_for(overflows)
    assert qr.modules == 0
    assert qr.path == ""


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
