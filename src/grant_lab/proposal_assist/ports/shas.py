"""The one hex-sha validator — stdlib only.

Two private copies used to exist and they disagreed: `tools/history.py` was
`{7,40}` and `web/review.py` was `{7,64}`. This is the merge, at `{7,64}`.

WHY it exists, from `tools/history.py`'s original docstring: every sha that
reaches git as an argument must be validated first. Git parses `--output=...`
as an option before it reaches the `--` path separator, so an unvalidated
"sha" is an option-injection vector. Separately and more mundanely,
`gitio.blob_sha` FAILS OPEN — a ref it cannot resolve is echoed back as if it
were a sha — so the boundary is the only place a bad value gets a clean answer.

Lives in `ports/` because `tools/` may import only the standard library and
this package (`tests/fencing/import_gate.py`), and `tools/history.py` is a
caller.

`$` in a Python regex also matches immediately before a trailing newline, so
the pattern ends with `\\Z` — otherwise "abc1234\\n" validates.
"""

from __future__ import annotations

import re

#: 7 is git's `--abbrev-commit` default and the shortest this codebase produces.
#: 64 is sha-256 full length; 40 is sha-1's. Accept both so a future repo-format
#: migration does not need a code change.
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}\Z")


def is_sha(value: object) -> bool:
    """True if `value` is a string safe to pass to git as a commit-ish."""
    return isinstance(value, str) and SHA_RE.match(value) is not None


def sha_error(sha: object, *, arg_name: str = "commit_sha") -> str | None:
    """Return an error message if `sha` is unsafe, else None.

    The message form is for the agent-tool callers, which return errors as
    dicts rather than raising.
    """
    if is_sha(sha):
        return None
    return f"{arg_name} must be a hex commit sha (7-64 chars, 0-9a-f); got: {sha!r}"
