"""`scripts/compare_uncovered.py`: no surviving line or branch loses its tests.

Each test commits a few source files to a small git repo in `tmp_path`, writes the
before and after coverage reports in the shape coverage.py or Istanbul writes, and
runs the script's `main`, checking the exit code and what it prints.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compare_uncovered.py"
_spec = importlib.util.spec_from_file_location("compare_uncovered", SCRIPT)
assert _spec is not None and _spec.loader is not None
compare_mod = importlib.util.module_from_spec(_spec)
# Registered before it runs: @dataclass looks its own module up in sys.modules.
sys.modules[_spec.name] = compare_mod
_spec.loader.exec_module(compare_mod)
floors_mod = compare_mod.floors

A = "src/grant_lab/web/a.py"
B = "src/grant_lab/web/b.py"
C = "src/grant_lab/ranking/c.py"

A_SOURCE = """\
def f(x):
    if x:
        return 1
    return compute(x)
"""
B_SOURCE = """\
def g(y):
    value = load(y)
    return value
"""
C_SOURCE = """\
def h():
    return 2
"""


# --- a small repo ----------------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def write(repo: Path, files: dict[str, str | None]) -> None:
    """Write each file; None deletes it."""
    for path, text in files.items():
        target = repo / path
        if text is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)


def commit(repo: Path, files: dict[str, str | None]) -> str:
    write(repo, files)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", "change")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    return root


# --- report shapes -----------------------------------------------------------------

Missing = tuple[list[int], list[list[int]]]


def backend_report(files: dict[str, Missing], covered: int = 5) -> dict:
    """coverage.py JSON: {path: (missing lines, missing branches)}, with branch coverage.

    Each file also has `covered` covered lines and branches, so its summaries are real
    totals and agree with its lists, as coverage.py's do.
    """
    keys = ("covered_lines", "num_statements", "covered_branches", "num_branches")
    totals = dict.fromkeys(keys, 0)
    out: dict = {"meta": {"branch_coverage": True}, "files": {}, "totals": totals}
    for path, (lines, branches) in files.items():
        summary = {
            "covered_lines": covered,
            "num_statements": covered + len(lines),
            "missing_lines": len(lines),
            "covered_branches": covered,
            "num_branches": covered + len(branches),
            "missing_branches": len(branches),
        }
        out["files"][path] = {
            "summary": summary,
            "missing_lines": lines,
            "missing_branches": branches,
        }
        for key in keys:
            totals[key] += summary[key]
    return out


def frontend_report(
    repo: Path,
    files: dict[str, tuple[list[tuple[int, int]], list[tuple[int, list[int]]]]],
) -> dict:
    """Istanbul coverage-final.json: {path: ([(line, count)], [(line, [arm counts])])}.

    Keys are absolute, as vitest writes them. Several statements may start on one line.
    """

    def loc(line: int) -> dict:
        return {"start": {"line": line, "column": 0}, "end": {"line": line, "column": 9}}

    out: dict = {}
    for path, (statements, branches) in files.items():
        absolute = str(repo / path)
        out[absolute] = {
            "path": absolute,
            "statementMap": {str(i): loc(line) for i, (line, _) in enumerate(statements)},
            "s": {str(i): count for i, (_, count) in enumerate(statements)},
            "branchMap": {
                str(i): {
                    "type": "branch",
                    "line": line,
                    "loc": loc(line),
                    "locations": [loc(line) for _ in counts],
                }
                for i, (line, counts) in enumerate(branches)
            },
            "b": {str(i): counts for i, (_, counts) in enumerate(branches)},
            "fnMap": {},
            "f": {},
        }
    return out


def run(
    repo: Path,
    section: str,
    before: dict | str,
    before_rev: str,
    after: dict | str,
    after_rev: str | None = None,
) -> int:
    """Write both reports (a str is written as is) and run the script on them."""
    paths = []
    for name, report in (("before.json", before), ("after.json", after)):
        path = repo.parent / name
        path.write_text(report if isinstance(report, str) else json.dumps(report))
        paths.append(path)
    argv = ["--section", section, "--repo", str(repo)]
    argv += ["--before", str(paths[0]), "--before-rev", before_rev, "--after", str(paths[1])]
    if after_rev is not None:
        argv += ["--after-rev", after_rev]
    return compare_mod.main(argv)


def backend(repo: Path, **files: str) -> str:
    """Commit the default three files, with any replaced by keyword (a, b, c)."""
    sources = {A: A_SOURCE, B: B_SOURCE, C: C_SOURCE}
    for key, text in files.items():
        sources[{"a": A, "b": B, "c": C}[key]] = text
    return commit(repo, sources)


# --- backend -----------------------------------------------------------------------


def test_nothing_new_exits_0(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sha = backend(repo)
    report = backend_report({A: ([4], [[2, 4]]), B: ([], []), C: ([], [])})
    assert run(repo, "backend", report, sha, report, sha) == 0
    out = capsys.readouterr().out
    assert "OK: 0 new uncovered line(s), 0 new uncovered branch(es)" in out
    assert "| web | 1 | 1 | 0 | 1 | 1 | 0 |" in out


def test_a_line_that_loses_its_tests_is_listed(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = backend(repo)
    before = backend_report({A: ([], []), B: ([], []), C: ([], [])})
    after = backend_report({A: ([4], []), B: ([], []), C: ([], [])})
    assert run(repo, "backend", before, sha, after, sha) == 1
    out = capsys.readouterr().out
    assert f"web  {A}:4  return compute(x)" in out
    assert "FAIL: 1 new uncovered line(s), 0 new uncovered branch(es)" in out


def test_deleting_an_uncovered_line_does_not_offset_a_new_one(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reason the script exists: `web`'s uncovered total stays level at 1."""
    old = backend(repo)
    new = commit(repo, {A: "def f(x):\n    return 1\n"})
    before = backend_report({A: ([4], []), B: ([], []), C: ([], [])})
    after = backend_report({A: ([], []), B: ([2], []), C: ([], [])})

    def uncovered(report: dict) -> int:
        web = floors_mod.group_backend(report)["web"]
        return web.lines_total - web.lines_covered

    assert uncovered(before) == uncovered(after) == 1
    assert run(repo, "backend", before, old, after, new) == 1
    out = capsys.readouterr().out
    assert f"web  {B}:2  value = load(y)" in out
    assert "return compute(x)" not in out


def test_a_deleted_line_does_not_excuse_an_added_line_of_other_text(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = backend(repo)
    new = backend(repo, a="def f(x):\n    return 1\n", b=B_SOURCE + "    raise Unreachable\n")
    before = backend_report({A: ([4], []), B: ([], []), C: ([], [])})
    after = backend_report({A: ([], []), B: ([4], []), C: ([], [])})
    assert run(repo, "backend", before, old, after, new) == 1
    out = capsys.readouterr().out
    assert f"web  {B}:4  raise Unreachable" in out
    assert "Matched in another file" not in out


def test_a_deleted_uncovered_file_is_not_a_loss(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = backend(repo)
    new = commit(repo, {B: None})
    before = backend_report({A: ([], []), B: ([2], [[2, 3]]), C: ([], [])})
    after = backend_report({A: ([], []), C: ([], [])})
    assert run(repo, "backend", before, old, after, new) == 0
    assert "| web | 1 | 0 | 0 | 1 | 0 | 0 |" in capsys.readouterr().out


def test_identical_lines_are_counted_as_a_multiset(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two uncovered `return None` lines move from a.py to b.py, which gains a third."""
    old = backend(repo, a="def f(x):\n    if x:\n        return None\n    return None\n")
    three = "def g(y):\n    if y:\n        return None\n    if y > 1:\n        return None\n"
    new = commit(repo, {A: None, B: three + "    return None\n"})
    before = backend_report({A: ([3, 4], []), B: ([], []), C: ([], [])})
    after = backend_report({B: ([3, 5, 6], []), C: ([], [])})
    assert run(repo, "backend", before, old, after, new) == 1
    out = capsys.readouterr().out
    assert f"web  {B}:3,5,6  return None  [1 of 3]" in out.split("Matched in another")[0]
    assert f"web  {B}:3,5,6  return None  [2 of 3]" in out.split("Matched in another")[1]
    assert "FAIL: 1 new uncovered line(s)" in out


def test_an_added_same_text_line_is_placed_exactly(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The two old `return None` lines are matched by place, so the new one is named."""
    text = "def f(x):\n    if x:\n        return None\n    return None\n"
    old = backend(repo, a=text)
    new = backend(repo, a="import os\n" + text + "\n\ndef k():\n    return None\n")
    before = backend_report({A: ([3, 4], []), B: ([], []), C: ([], [])})
    after = backend_report({A: ([4, 5, 9], []), B: ([], []), C: ([], [])})
    assert run(repo, "backend", before, old, after, new) == 1
    out = capsys.readouterr().out
    assert f"web  {A}:9  return None\n" in out
    assert "FAIL: 1 new uncovered line(s)" in out


def test_a_line_that_is_covered_now_does_not_excuse_one_that_is_not(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """a.py is unchanged: its line 3 is still there, so it moved nowhere."""
    text = "def f(x):\n    if x:\n        return None\n    return None\n"
    sha = backend(repo, a=text)
    before = backend_report({A: ([3], []), B: ([], []), C: ([], [])})
    after = backend_report({A: ([4], []), B: ([], []), C: ([], [])})
    assert run(repo, "backend", before, sha, after, sha) == 1
    assert f"web  {A}:4  return None" in capsys.readouterr().out


def test_a_covered_now_line_does_not_excuse_another_files_line(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = backend(repo, a="def f(x):\n    return None\n", b="def g(y):\n    return None\n")
    before = backend_report({A: ([2], []), B: ([], []), C: ([], [])})
    after = backend_report({A: ([], []), B: ([2], []), C: ([], [])})
    assert run(repo, "backend", before, sha, after, sha) == 1
    out = capsys.readouterr().out
    assert f"web  {B}:2  return None" in out
    assert "Matched in another file" not in out


COMMENTED = "def f(x):\n    # {}\n    if x:\n        return None\n    return None\n"


def test_in_a_changed_file_a_covered_now_line_does_not_excuse_one_that_is_not(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A comment edit changes a.py, but both `return None` lines are still in place.

    Line 4 gains its tests and line 5 loses them. Line 4 is still there, so it moved
    nowhere and cannot excuse line 5 by text.
    """
    old = backend(repo, a=COMMENTED.format("check x"))
    new = backend(repo, a=COMMENTED.format("check x first"))
    before = backend_report({A: ([4], []), B: ([], []), C: ([], [])})
    after = backend_report({A: ([5], []), B: ([], []), C: ([], [])})
    assert run(repo, "backend", before, old, after, new) == 1
    out = capsys.readouterr().out
    assert f"web  {A}:5  return None\n" in out
    assert "FAIL: 1 new uncovered line(s)" in out


def test_in_a_changed_file_a_covered_now_line_does_not_excuse_another_files_line(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = backend(repo, a=COMMENTED.format("check x"), b="def g(y):\n    return None\n")
    new = backend(repo, a=COMMENTED.format("check x first"), b="def g(y):\n    return None\n")
    before = backend_report({A: ([4], []), B: ([], []), C: ([], [])})
    after = backend_report({A: ([], []), B: ([2], []), C: ([], [])})
    assert run(repo, "backend", before, old, after, new) == 1
    out = capsys.readouterr().out
    assert f"web  {B}:2  return None\n" in out
    assert "Matched in another file" not in out


KEPT = "def f(x):\n    y = load(x)\n    return None\n"
GUARDED = "def f(x):\n    if x:\n        return None\n    y = load(x)\n    return None\n"


def test_a_covered_now_line_does_not_excuse_an_added_one(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Line 3 gains its tests, and an added `return None` (line 8) has none.

    Line 3 is still in place, so it moved nowhere: the added line did not come from it.
    """
    text = "def f(x):\n    if x:\n        return None\n    return 1\n"
    old = backend(repo, a=text)
    new = backend(repo, a=text + "\n\ndef k():\n    return None\n")
    before = backend_report({A: ([3], []), B: ([], []), C: ([], [])})
    after = backend_report({A: ([8], []), B: ([], []), C: ([], [])})
    assert run(repo, "backend", before, old, after, new) == 1
    assert f"web  {A}:8  return None\n" in capsys.readouterr().out


def test_a_gone_line_does_not_excuse_a_line_still_in_place_that_lost_its_tests(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """a.py's uncovered `return None` is deleted with its `if`; the covered one stays.

    The one that stays (line 5, then 3) loses its tests. It was there before, so it
    matches at its place or is new: the deleted line cannot excuse it by text.
    """
    old = backend(repo, a=GUARDED)
    new = backend(repo, a=KEPT)
    before = backend_report({A: ([3], []), B: ([], []), C: ([], [])})
    after = backend_report({A: ([3], []), B: ([], []), C: ([], [])})
    assert run(repo, "backend", before, old, after, new) == 1
    out = capsys.readouterr().out
    assert f"web  {A}:3  return None\n" in out
    assert "FAIL: 1 new uncovered line(s)" in out


def test_a_gone_line_of_another_file_does_not_excuse_a_line_still_in_place(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The deleted `return None` is in b.py: a.py's line is new, not listed as a move."""
    b = "def g(y):\n    if y:\n        return None\n    value = load(y)\n    return value\n"
    old = backend(repo, a=KEPT, b=b)
    new = backend(repo, a=KEPT, b=B_SOURCE)
    before = backend_report({A: ([], []), B: ([3], []), C: ([], [])})
    after = backend_report({A: ([3], []), B: ([], []), C: ([], [])})
    assert run(repo, "backend", before, old, after, new) == 1
    out = capsys.readouterr().out
    assert f"web  {A}:3  return None\n" in out
    assert "Matched in another file" not in out


def test_a_moved_line_the_diff_puts_on_a_deleted_same_text_line_is_new(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fails closed: g's uncovered `return None` moves into f, over f's covered one.

    By text that is the same change as f's line losing its tests and g's being deleted,
    and the diff keeps f's line in place. So the moved line is reported, not excused.
    """
    old = backend(repo, a=KEPT + "\n\ndef g(y):\n    if y:\n        return None\n")
    new = backend(repo, a=KEPT)
    before = backend_report({A: ([8], []), B: ([], []), C: ([], [])})
    after = backend_report({A: ([3], []), B: ([], []), C: ([], [])})
    assert run(repo, "backend", before, old, after, new) == 1
    assert f"web  {A}:3  return None\n" in capsys.readouterr().out


def test_a_multiset_excess_in_another_file_is_listed_by_line(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = "def f(x):\n    if x:\n        return None\n    return None\n"
    old = backend(repo, a=a)
    new = backend(repo, a=a, b="def g(y):\n    return None\n")
    before = backend_report({A: ([3, 4], []), B: ([], []), C: ([], [])})
    after = backend_report({A: ([3, 4], []), B: ([2], []), C: ([], [])})
    assert run(repo, "backend", before, old, after, new) == 1
    assert f"web  {B}:2  return None\n" in capsys.readouterr().out


def test_a_line_moved_within_its_group_still_matches(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Re-indented too, as when a block moves into a helper: whitespace is stripped."""
    old = backend(repo)
    moved = B_SOURCE + "    if y:\n        return compute(x)\n"
    new = backend(repo, a="def f(x):\n    return 1\n", b=moved)
    before = backend_report({A: ([4], []), B: ([], []), C: ([], [])})
    after = backend_report({A: ([], []), B: ([5], []), C: ([], [])})
    assert run(repo, "backend", before, old, after, new) == 0
    out = capsys.readouterr().out
    assert "Matched in another file of the same group" in out
    assert f"web  {B}:5  return compute(x)" in out
    assert "OK: 0 new uncovered line(s), 0 new uncovered branch(es); 1 matched" in out


MOVED_BEFORE = (
    "def f(x):\n    if x:\n        return None\n    return 1\n\n\n"
    "def g(y):\n    if y:\n        return None\n    return 2\n"
)
MOVED_AFTER = (
    "def f(x):\n    return 1\n\n\n"
    "def g(y):\n    if y:\n        return None\n    if x:\n        return None\n"
    "    return 2\n"
)


def test_a_block_moved_within_its_file_still_matches(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The line diff does not align a cut-and-pasted block, so it matches by text.

    Line 9's covered `return None` stays in place and is aligned; the moved block's
    uncovered `return None` (line 3, then 9) is not, so it is the one free to match.
    """
    old = backend(repo, a=MOVED_BEFORE)
    new = backend(repo, a=MOVED_AFTER)
    before = backend_report({A: ([3], [[2, 3]]), B: ([], []), C: ([], [])})
    after = backend_report({A: ([9], [[8, 9]]), B: ([], []), C: ([], [])})
    assert run(repo, "backend", before, old, after, new) == 0
    out = capsys.readouterr().out
    assert "OK: 0 new uncovered line(s), 0 new uncovered branch(es); 0 matched" in out


def test_a_line_in_place_beside_moved_code_is_named(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Line 7 was there before and loses its tests; only the moved line 9 may match by text."""
    old = backend(repo, a=MOVED_BEFORE)
    new = backend(repo, a=MOVED_AFTER)
    before = backend_report({A: ([3], [[2, 3]]), B: ([], []), C: ([], [])})
    after = backend_report({A: ([7, 9], [[8, 9]]), B: ([], []), C: ([], [])})
    assert run(repo, "backend", before, old, after, new) == 1
    out = capsys.readouterr().out
    assert f"web  {A}:7  return None\n" in out
    assert "FAIL: 1 new uncovered line(s), 0 new uncovered branch(es)" in out


def test_a_branch_from_a_line_in_place_into_moved_code_matches_by_text(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """f's guarded `return None` moves under g's `if x:`, which stays in place.

    The branch's to-line is new, so the branch is not in place: a branch is in place
    only when both its lines are, as on the before side. So it may match by text.
    """
    f = "def f(x):\n    if x:\n        return None\n    return 1\n\n\n"
    old = backend(repo, a=f + "def g(x):\n    if x:\n        pass\n    return 2\n")
    g = "def g(x):\n    if x:\n        return None\n    return 2\n"
    new = backend(repo, a="def f(x):\n    return 1\n\n\n" + g)
    before = backend_report({A: ([3], [[2, 3]]), B: ([], []), C: ([], [])})
    after = backend_report({A: ([7], [[6, 7]]), B: ([], []), C: ([], [])})
    assert run(repo, "backend", before, old, after, new) == 0
    assert "OK: 0 new uncovered line(s), 0 new uncovered branch(es); 0 matched" in (
        capsys.readouterr().out
    )


def test_a_line_moved_to_another_group_is_new(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = backend(repo)
    new = backend(repo, a="def f(x):\n    return 1\n", c=C_SOURCE + "    return compute(x)\n")
    before = backend_report({A: ([4], []), B: ([], []), C: ([], [])})
    after = backend_report({A: ([], []), B: ([], []), C: ([3], [])})
    assert run(repo, "backend", before, old, after, new) == 1
    assert f"ranking  {C}:3  return compute(x)" in capsys.readouterr().out


def test_a_branch_that_loses_its_tests_is_listed(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = backend(repo)
    before = backend_report({A: ([], [[2, 4]]), B: ([], []), C: ([], [])})
    after = backend_report({A: ([], [[2, 3], [2, 4]]), B: ([], []), C: ([], [])})
    assert run(repo, "backend", before, sha, after, sha) == 1
    out = capsys.readouterr().out
    assert f"web  {A}:2->3  if x:  ->  return 1" in out
    assert "FAIL: 0 new uncovered line(s), 1 new uncovered branch(es)" in out


def test_an_exit_branch_is_matched_by_its_marker(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Code inserted above shifts the lines; the exit still matches, by `<exit>`."""
    text = "def f(x):\n    if x:\n        g()\n"
    old = backend(repo, a=text)
    new = backend(repo, a="import os\n\n\n" + text)
    before = backend_report({A: ([], [[2, -1]]), B: ([], []), C: ([], [])})
    after = backend_report({A: ([], [[5, -4]]), B: ([], []), C: ([], [])})
    assert run(repo, "backend", before, old, after, new) == 0
    capsys.readouterr()

    after = backend_report({A: ([], [[5, -4], [5, 6]]), B: ([], []), C: ([], [])})
    assert run(repo, "backend", before, old, after, new) == 1
    out = capsys.readouterr().out
    assert f"web  {A}:5->6  if x:  ->  g()" in out
    assert "->exit" not in out


def test_a_new_exit_branch_prints_the_marker(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = "def f(x):\n    if x:\n        g()\n"
    sha = backend(repo, a=text)
    before = backend_report({A: ([], [[2, 3]]), B: ([], []), C: ([], [])})
    after = backend_report({A: ([], [[2, 3], [2, -1]]), B: ([], []), C: ([], [])})
    assert run(repo, "backend", before, sha, after, sha) == 1
    assert f"web  {A}:2->exit  if x:  ->  <exit>" in capsys.readouterr().out


def test_the_working_tree_is_the_default_after_side(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = backend(repo)
    write(repo, {B: B_SOURCE + "    raise Unreachable\n", "src/grant_lab/web/new.py": "X = 1\n"})
    before = backend_report({A: ([], []), B: ([], []), C: ([], [])})
    after = backend_report(
        {A: ([], []), B: ([4], []), C: ([], []), "src/grant_lab/web/new.py": ([1], [])}
    )
    assert run(repo, "backend", before, sha, after) == 1
    out = capsys.readouterr().out
    assert f"web  {B}:4  raise Unreachable" in out
    assert "web  src/grant_lab/web/new.py:1  X = 1" in out
    assert run(repo, "backend", before, sha, after, "WORKTREE") == 1


@pytest.mark.parametrize(
    ("path", "group"),
    [
        ("src/grant_lab/main.py", "<root>"),
        ("src/grant_lab/web/api/calls.py", "web"),
        ("/ci/work/src/grant_lab/sources/eu_portal.py", "sources"),
    ],
)
def test_backend_groups_are_the_floors_checks(path: str, group: str) -> None:
    report = {
        "files": {path: {"summary": {"covered_lines": 1, "num_statements": 1}}},
        "totals": {"covered_lines": 1, "num_statements": 1},
    }
    floors_groups = set(floors_mod.group_backend(report)) - {floors_mod.TOTAL_KEY}
    assert floors_groups == {group}
    assert compare_mod.backend_location(path)[1] == group


@pytest.mark.parametrize(
    ("path", "group"),
    [
        ("/w/frontend/src/App.tsx", "<root>"),
        ("/w/frontend/src/routes/AdminGoldenSet.tsx", "routes"),
        ("/w/.worktrees/x/frontend/src/api/client.ts", "api"),
    ],
)
def test_frontend_groups_are_the_floors_checks(path: str, group: str) -> None:
    metric = {"total": 1, "covered": 1, "skipped": 0, "pct": 100}
    entry = {"lines": metric, "branches": metric}
    floors_groups = set(floors_mod.group_frontend({path: entry, "total": entry}))
    assert floors_groups - {floors_mod.TOTAL_KEY} == {group}
    assert compare_mod.frontend_location(path)[1] == group


# --- frontend ----------------------------------------------------------------------

ROUTE = "frontend/src/routes/Admin.tsx"
ROUTE_SOURCE = """\
export function Admin({ rows }: Props) {
  const empty = rows.length === 0;
  if (empty) return <Empty />;
  return <Table rows={rows} />;
}
"""


def test_frontend_statement_and_branch(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sha = commit(repo, {ROUTE: ROUTE_SOURCE})
    statements = [(1, 4), (2, 4), (3, 4), (4, 3)]
    before = frontend_report(repo, {ROUTE: (statements, [(3, [1])])})
    assert run(repo, "frontend", before, sha, before, sha) == 0
    capsys.readouterr()

    lost = [(1, 4), (2, 4), (3, 4), (4, 0)]
    after = frontend_report(repo, {ROUTE: (lost, [(3, [0])])})
    assert run(repo, "frontend", before, sha, after, sha) == 1
    out = capsys.readouterr().out
    assert f"routes  {ROUTE}:4  return <Table rows={{rows}} />;" in out
    assert f"routes  {ROUTE}:3->3  if (empty) return <Empty />;\n" in out
    assert "FAIL: 1 new uncovered line(s), 1 new uncovered branch(es)" in out


def test_a_frontend_line_is_uncovered_only_when_every_statement_on_it_is(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Istanbul's line rule: a line's count is the largest count of its statements."""
    sha = commit(repo, {ROUTE: ROUTE_SOURCE})
    before = frontend_report(repo, {ROUTE: ([(3, 1), (3, 1)], [])})
    after = frontend_report(repo, {ROUTE: ([(3, 1), (3, 0)], [])})
    assert run(repo, "frontend", before, sha, after, sha) == 0
    capsys.readouterr()
    after = frontend_report(repo, {ROUTE: ([(3, 0), (3, 0)], [])})
    assert run(repo, "frontend", before, sha, after, sha) == 1
    assert "FAIL: 1 new uncovered line(s)" in capsys.readouterr().out


def test_an_arm_in_code_that_did_not_run_before_is_not_new(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """v8 lists no arms in a function that never ran; they appear once it runs."""
    sha = commit(repo, {ROUTE: ROUTE_SOURCE})
    never_ran = [(1, 1), (2, 0), (3, 0), (4, 0)]
    before = frontend_report(repo, {ROUTE: (never_ran, [])})
    after = frontend_report(repo, {ROUTE: ([(1, 1), (2, 1), (3, 1), (4, 1)], [(3, [0])])})
    assert run(repo, "frontend", before, sha, after, sha) == 0
    out = capsys.readouterr().out
    assert "Branches whose from-line did not run before" in out
    assert f"routes  {ROUTE}:3->3  if (empty)" in out
    assert "1 branch(es) in code that did not run before" in out


def test_a_frontend_branch_arm_is_part_of_its_identity(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = commit(repo, {ROUTE: ROUTE_SOURCE})
    before = frontend_report(repo, {ROUTE: ([(3, 1)], [(3, [0, 1])])})
    after = frontend_report(repo, {ROUTE: ([(3, 1)], [(3, [1, 0])])})
    assert run(repo, "frontend", before, sha, after, sha) == 1
    assert "FAIL: 0 new uncovered line(s), 1 new uncovered branch(es)" in (capsys.readouterr().out)


# --- bad input ---------------------------------------------------------------------


def _bad_reports() -> dict[str, tuple[dict | str, dict | str, str]]:
    """{case: (before report, after report, a fragment of the error it must print)}."""
    good = backend_report({A: ([4], []), B: ([], []), C: ([], [])})
    no_branches = json.loads(json.dumps(good))
    no_branches["meta"]["branch_coverage"] = False
    missing_file = backend_report({A: ([4], []), B: ([], [])})
    extra_file = backend_report(
        {A: ([], []), B: ([], []), C: ([], []), "src/grant_lab/x.py": ([1], [])}
    )
    past_end = backend_report({A: ([9], []), B: ([], []), C: ([], [])})
    disagrees = json.loads(json.dumps(good))
    disagrees["files"][A]["missing_lines"] = []
    outside = backend_report({A: ([], []), B: ([], []), C: ([], []), "lib/other.py": ([], [])})
    shapeless = {"meta": {"branch_coverage": True}, "files": {A: {}}, "totals": {}}
    return {
        "unparseable JSON": (good, "{not json", "after.json is not readable JSON"),
        "not a coverage report": (good, shapeless, "is not a backend coverage report"),
        "a JSON list": ("[]", good, "before.json: it has no meta, files and totals"),
        "no branch coverage": (good, no_branches, "without branch coverage"),
        "a file missing from the report": (missing_file, good, "ranking/c.py; was it"),
        "a file dropped from the after report": (good, missing_file, "ranking/c.py; was it"),
        "a file not at the revision": (good, extra_file, "x.py is in the report but not"),
        "a line past the end of the file": (good, past_end, "a.py:9 is past the end"),
        "lists that disagree with the summaries": (good, disagrees, "web: the report lists 0"),
        "a file outside the package": (good, outside, "outside src/grant_lab/"),
    }


@pytest.mark.parametrize("case", list(_bad_reports()))
def test_a_malformed_report_exits_2(
    repo: Path, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    sha = backend(repo)
    before, after, message = _bad_reports()[case]
    assert run(repo, "backend", before, sha, after, sha) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    assert message in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_a_report_read_against_the_wrong_revision_exits_2(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing line that is blank at the given revision: the report is not from it."""
    old = backend(repo)
    new = backend(repo, a="def f(x):\n\n    return compute(x)\n")
    report = backend_report({A: ([2], []), B: ([], []), C: ([], [])})
    assert run(repo, "backend", report, old, report, new) == 2
    assert f"{A}:2 is a blank line" in capsys.readouterr().err


def test_a_report_of_the_other_section_exits_2(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = backend(repo)
    commit(repo, {ROUTE: ROUTE_SOURCE})
    sha = git(repo, "rev-parse", "HEAD")
    back = backend_report({A: ([], []), B: ([], []), C: ([], [])})
    front = frontend_report(repo, {ROUTE: ([(1, 1)], [])})
    assert run(repo, "backend", back, sha, front, sha) == 2
    assert "after.json: it has no meta, files and totals" in capsys.readouterr().err
    assert run(repo, "frontend", front, sha, back, sha) == 2
    assert "after.json: it is not Istanbul's coverage-final.json" in capsys.readouterr().err


def test_a_frontend_summary_or_an_empty_report_exits_2(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The summary is the easy file to pass by mistake; an empty report would pass."""
    sha = commit(repo, {ROUTE: ROUTE_SOURCE})
    final = frontend_report(repo, {ROUTE: ([(1, 1)], [])})
    metric = {"total": 1, "covered": 1, "skipped": 0, "pct": 100}
    summary = {str(repo / ROUTE): {"lines": metric}, "total": {"lines": metric}}
    assert run(repo, "frontend", final, sha, summary, sha) == 2
    assert "coverage-summary.json has totals only" in capsys.readouterr().err
    assert run(repo, "frontend", final, sha, {}, sha) == 2
    assert "after.json: the report measured no files" in capsys.readouterr().err


def test_a_frontend_report_outside_frontend_src_exits_2(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = commit(repo, {ROUTE: ROUTE_SOURCE, "other/x.ts": "x\n"})
    good = frontend_report(repo, {ROUTE: ([(1, 1)], [])})
    bad = frontend_report(repo, {"other/x.ts": ([(1, 0)], [])})
    assert run(repo, "frontend", good, sha, bad, sha) == 2
    assert "outside frontend/src/" in capsys.readouterr().err


def test_a_frontend_report_missing_a_surviving_file_exits_2(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An after report that drops a file would hide every line that file lost."""
    client = "frontend/src/api/client.ts"
    sha = commit(repo, {ROUTE: ROUTE_SOURCE, client: "export const base = '/api';\n"})
    before = frontend_report(repo, {ROUTE: ([(4, 1)], []), client: ([(1, 1)], [])})
    whole = frontend_report(repo, {ROUTE: ([(4, 0)], []), client: ([(1, 1)], [])})
    assert run(repo, "frontend", before, sha, whole, sha) == 1
    capsys.readouterr()

    dropped = frontend_report(repo, {client: ([(1, 1)], [])})
    assert run(repo, "frontend", before, sha, dropped, sha) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    assert "after.json: 1 file(s) of the before report" in captured.err
    assert ROUTE in captured.err
    assert captured.out == ""

    # A file the change deleted may be absent.
    deleted = commit(repo, {ROUTE: None})
    assert run(repo, "frontend", before, sha, dropped, deleted) == 0


def test_a_missing_report_exits_2(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sha = backend(repo)
    report = repo.parent / "before.json"
    report.write_text(json.dumps(backend_report({A: ([], []), B: ([], []), C: ([], [])})))
    argv = ["--section", "backend", "--repo", str(repo), "--before", str(report)]
    argv += ["--before-rev", sha, "--after", str(repo.parent / "nowhere.json")]
    assert compare_mod.main(argv) == 2
    assert "no coverage report at" in capsys.readouterr().err


def test_the_same_report_on_both_sides_exits_2(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = backend(repo)
    report = repo.parent / "r.json"
    report.write_text(json.dumps(backend_report({A: ([], []), B: ([], []), C: ([], [])})))
    argv = ["--section", "backend", "--repo", str(repo), "--before", str(report)]
    argv += ["--before-rev", sha, "--after", str(report), "--after-rev", sha]
    assert compare_mod.main(argv) == 2
    assert "the same file" in capsys.readouterr().err


@pytest.mark.parametrize("which", ["before", "after"])
def test_an_unknown_revision_exits_2(
    repo: Path, capsys: pytest.CaptureFixture[str], which: str
) -> None:
    sha = backend(repo)
    report = backend_report({A: ([], []), B: ([], []), C: ([], [])})
    revs = {"before": sha, "after": sha, which: "0123456789abcdef"}
    assert run(repo, "backend", report, revs["before"], report, revs["after"]) == 2
    assert "no commit '0123456789abcdef'" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["--section", "middle", "--before", "b", "--before-rev", "x", "--after", "a"],
        ["--section", "backend", "--before", "b", "--after", "a"],
        ["--section", "backend", "--before-rev", "x", "--after", "a"],
        ["--sect", "backend", "--before", "b", "--before-rev", "x", "--after", "a"],
    ],
    ids=["unknown section", "no before rev", "no before report", "abbreviated option"],
)
def test_bad_arguments_exit_2(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        compare_mod.main(argv)
    assert exited.value.code == 2
    err = capsys.readouterr().err
    assert "error:" in err
    assert "Traceback" not in err


def test_runs_as_a_script(repo: Path) -> None:
    """The command the plan runs: a real subprocess, exit code included."""
    sha = backend(repo)
    before = backend_report({A: ([], []), B: ([], []), C: ([], [])})
    after = backend_report({A: ([4], []), B: ([], []), C: ([], [])})
    paths = []
    for name, report in (("b.json", before), ("a.json", after)):
        (repo.parent / name).write_text(json.dumps(report))
        paths.append(str(repo.parent / name))
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--section", "backend", "--repo", str(repo)]
        + ["--before", paths[0], "--before-rev", sha, "--after", paths[1], "--after-rev", sha],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 1, done.stderr
    assert f"web  {A}:4  return compute(x)" in done.stdout
