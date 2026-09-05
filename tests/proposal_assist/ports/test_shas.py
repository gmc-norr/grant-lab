"""The one hex-sha validator. Bound is {7,64}: sha-256-ready.

Git-option injection is the reason this exists — `tools/history.py`'s module
docstring names `--output=proposal.md` as the value it must refuse.
"""

import pytest

from grant_lab.proposal_assist.ports.shas import is_sha, sha_error


@pytest.mark.parametrize(
    "value",
    [
        "abc1234",  # 7, the minimum (git's --abbrev-commit default)
        "a" * 40,  # sha-1 full length
        "A" * 40,  # upper case hex
        "f" * 64,  # sha-256 full length
    ],
)
def test_accepts_hex_shas(value):
    assert is_sha(value) is True
    assert sha_error(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "abc123",  # 6 — one short
        "f" * 65,  # 65 — one long
        "--output=proposal.md",  # the injection this guard exists for
        "-h",
        "HEAD",
        "zzzzzzz",  # right length, not hex
        "abc1234\n",  # trailing newline must not slip past $
        "",
        None,  # not a str at all
        1234567,
    ],
)
def test_rejects_everything_else(value):
    assert is_sha(value) is False
    msg = sha_error(value)
    assert msg is not None
    assert "hex commit sha" in msg


def test_error_message_names_the_argument():
    assert "commit_a must be" in (sha_error("nope", arg_name="commit_a") or "")
