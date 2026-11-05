"""Every `# type: ignore` in src/ names its error code and says why, and no
`# mypy:` inline directive appears in src/ at all.

mypy's `warn_unused_ignores` already fails on an ignore that hides nothing. This
test covers the other half: an ignore that still hides something must name what
it hides (the error code in brackets) and why that is acceptable (a reason of at
least three words after a second `#`). A `# mypy: ignore-errors` or
`# mypy: disable-error-code=...` comment switches checks off for a whole file, so
it is refused outright. Comments are found with `tokenize`, so
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
_MYPY_DIRECTIVE = re.compile(r"^#\s*mypy:")
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


def mypy_directives(source: str) -> list[str]:
    """The comments that start with `# mypy:`, which configure mypy per file."""
    return [
        f"line {tok.start[0]}: {tok.string}"
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type == tokenize.COMMENT and _MYPY_DIRECTIVE.match(tok.string)
    ]


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


def test_no_mypy_inline_directive_in_src():
    problems = [
        f"{path.relative_to(SRC.parents[1])} {problem}"
        for path in sorted(SRC.rglob("*.py"))
        for problem in mypy_directives(path.read_text())
    ]
    assert problems == []


@pytest.mark.parametrize(
    "comment", ["# mypy: ignore-errors", '# mypy: disable-error-code="attr-defined"']
)
def test_a_mypy_inline_directive_is_refused(comment):
    assert mypy_directives(f"{comment}\nx = 1\n") == [f"line 1: {comment}"]


def test_text_inside_a_string_is_not_a_mypy_directive():
    assert mypy_directives('x = "# mypy: ignore-errors"\n') == []


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
