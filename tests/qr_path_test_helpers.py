"""Shared strict QR-path parser for `tests/test_boards_qr.py` and
`tests/web/test_api_public_board.py`.

MINOR 6 (Plan C Task 7 review): the endpoint test had its own, weaker copy --
bare `re.finditer` with no `pos` continuity check and no trailing-bytes
assertion, so junk BETWEEN valid `M...z` commands would still rebuild to a
matching matrix there, while the qr.py unit test's copy would catch it. Two
copies of a parser drift exactly like that; one shared strict version can't.

Not a conftest fixture: this takes positional arguments (`path`, `modules`)
and is called directly inside assertions, not injected by pytest.
"""

from __future__ import annotations

import re

# Public (unprefixed) so `test_the_path_is_only_move_line_and_close_commands`
# can reconstruct the raw path from every matched command, independent of
# `matrix_from_qr_path`'s own bookkeeping.
QR_CMD = re.compile(r"M(\d+) (\d+)h(\d+)v1h-(\d+)z")


def matrix_from_qr_path(path: str, modules: int) -> list[list[int]]:
    """Rebuild a 0/1 matrix from `qr_for`'s path -- a path PARSER, not a QR
    decoder (segno has no decoder; a re-encode has the same discriminating
    power as a decode for the failure that matters -- a QR pointing at the
    wrong string). Every command must be exactly `M{x} {y}h{run}v1h-{run}z`,
    contiguous from the start of the string (`pos` continuity, so a stray or
    garbled command between two valid ones cannot be silently skipped by
    `finditer`), with no trailing unparsed bytes, and the two run lengths in
    one command must agree.
    """
    matrix = [[0] * modules for _ in range(modules)]
    pos = 0
    for m in QR_CMD.finditer(path):
        assert m.start() == pos, f"unparsed byte at offset {pos} in {path!r}"
        x, y, run, run2 = int(m[1]), int(m[2]), int(m[3]), int(m[4])
        assert run == run2, f"mismatched run lengths in command {m[0]!r}"
        for i in range(run):
            matrix[y][x + i] = 1
        pos = m.end()
    assert pos == len(path), f"trailing unparsed bytes: {path[pos:]!r}"
    return matrix
