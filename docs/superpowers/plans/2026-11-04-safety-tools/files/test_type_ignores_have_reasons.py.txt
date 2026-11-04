"""Every `# type: ignore` in src/ names its error code and says why.

mypy's `warn_unused_ignores` already fails on an ignore that hides nothing. This
test covers the other half: an ignore that still hides something must name what
it hides (the error code in brackets) and why that is acceptable (a reason of at
least three words after a second `#`). Comments are found with `tokenize`, so
text inside strings and docstrings never counts.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "grant_lab"

_MENTIONS_IGNORE = re.compile(r"type:\s*ignore")
_EXPLAINED = re.compile(
    r"#\s*type:\s*ignore\[[a-z][a-z0-9-]*(?:,\s*[a-z][a-z0-9-]*)*\]"
    r"\s+#\s*\S+(?:\s+\S+){2,}\s*$"
)


def ignore_comments(source: str) -> list[tuple[int, str]]:
    """Every comment that mentions `type: ignore`, with its line number."""
    found = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT and _MENTIONS_IGNORE.search(tok.string):
            found.append((tok.start[0], tok.string))
    return found


def unexplained(source: str) -> list[str]:
    """The ignore comments that lack an error code or a reason."""
    return [
        f"line {line}: {text}"
        for line, text in ignore_comments(source)
        if not _EXPLAINED.match(text)
    ]


def test_every_type_ignore_in_src_names_a_code_and_a_reason():
    files = sorted(SRC.rglob("*.py"))
    assert len(files) > 100, f"walked only {len(files)} files under {SRC}"
    problems = [
        f"{path.relative_to(SRC.parents[1])} {problem}"
        for path in files
        for problem in unexplained(path.read_text())
    ]
    assert problems == []


def test_a_coded_and_explained_ignore_is_accepted():
    source = "x = f()  # type: ignore[attr-defined]  # the stub lacks it\n"
    assert ignore_comments(source) == [(1, "# type: ignore[attr-defined]  # the stub lacks it")]
    assert unexplained(source) == []


@pytest.mark.parametrize(
    "comment",
    [
        "# type: ignore",
        "# type: ignore[attr-defined]",
        "# type: ignore  # the stub lacks it",
        "# type: ignore[attr-defined]  # two words",
    ],
)
def test_an_ignore_without_a_code_or_a_reason_is_refused(comment):
    assert unexplained(f"x = f()  {comment}\n") != []


def test_text_inside_a_string_is_not_an_ignore():
    assert ignore_comments('x = "# type: ignore"\n') == []
