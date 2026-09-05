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

import re

import segno

from grant_lab.boards.qr import qr_for

_CMD = re.compile(r"M(\d+) (\d+)h(\d+)v1h-(\d+)z")


def _matrix_from_path(path: str, modules: int) -> list[list[int]]:
    """Rebuild a 0/1 matrix from `qr_for`'s path -- a path PARSER, not a QR
    decoder (see the endpoint test's own docstring for why a decoder is out
    of scope). Every command must be exactly `M{x} {y}h{run}v1h-{run}z`
    (asserted via `pos`, so a stray/garbled command cannot be silently
    skipped by `finditer`) and the two run lengths must agree.
    """
    matrix = [[0] * modules for _ in range(modules)]
    pos = 0
    for m in _CMD.finditer(path):
        assert m.start() == pos, f"unparsed byte at offset {pos} in {path!r}"
        x, y, run, run2 = int(m[1]), int(m[2]), int(m[3]), int(m[4])
        assert run == run2, f"mismatched run lengths in command {m[0]!r}"
        for i in range(run):
            matrix[y][x + i] = 1
        pos = m.end()
    assert pos == len(path), f"trailing unparsed bytes: {path[pos:]!r}"
    return matrix


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
