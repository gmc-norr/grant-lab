"""Find code that lost its tests: uncovered lines and branches that were not uncovered before.

Compares two coverage reports of one section, each read against the source it was
measured on, and lists every uncovered line or branch of the "after" report that no
equal uncovered line or branch of the "before" report accounts for:

    uv run python scripts/compare_uncovered.py --section backend \\
        --before before.json --before-rev <sha> --after after.json [--after-rev <sha>]
    uv run python scripts/compare_uncovered.py --section frontend \\
        --before before-final.json --before-rev <sha> \\
        --after frontend/coverage/coverage-final.json

`--after-rev WORKTREE`, or no `--after-rev`, reads the after side's source from the
working tree of `--repo` (default: the checkout this script is in). A revision's source
is read with `git show <rev>:<path>`.

Why a percentage is not enough: deleting well-covered code lowers a group's percentage
while no surviving line loses its tests, and deleting a file that held a few uncovered
lines lets as many lines elsewhere lose their tests while the group's uncovered total
stays level. So the check is per item, never per total.

Reports:

- backend: coverage.py JSON (`--cov-report=json`), measured with branch coverage. An
  uncovered line is an entry of `files.<path>.missing_lines`; an uncovered branch is a
  `[from, to]` pair of `missing_branches`, where a negative `to` is an exit from the
  function. The report must list exactly the `.py` files under `src/grant_lab/` at its
  revision, and its lists must add up to its own summaries.
- frontend: Istanbul's `coverage-final.json` (vitest's `json` reporter; the
  `json-summary` file has totals only). An uncovered line is a source line on which at
  least one statement starts and every statement that starts there ran 0 times; this is
  how Istanbul derives the `lines` figure of `coverage-summary.json`, which the floors
  check reads. An uncovered branch is one arm of a `branchMap` entry whose count in `b`
  is 0, which is how Istanbul counts `branches`. Its from-line is the start line of the
  entry's `loc`, its to-line the start line of that arm's location, and the arm's
  index is part of its identity. Every file of the report must exist at its revision.

Every file the before report measured that is still at the after revision must be in
the after report, so a report of part of the suite cannot hide what a file it left out
lost; a file the change deleted may be absent. For the backend this already follows
from its file-set rule; for the frontend it is the only check of the file set.

Every reported line must be a non-blank line inside its file at the given revision, so
a report read against the wrong revision fails as bad input instead of matching noise.

Groups are the ones `scripts/check_coverage_floors.py` uses: the top-level package after
`src/grant_lab/` (backend) or the folder after `frontend/src/` (frontend), and `<root>`
for a file directly inside it. A backend report's uncovered counts per group are checked
against that script's own grouping of the same report.

Matching. A line is identified by its source text, stripped of leading and trailing
whitespace; a branch by the texts of its from-line and to-line, with `<exit>` as the
to-line of a function exit. Each group is compared on its own, in this order:

1. Same place. Each file's before lines are aligned with its after lines (difflib, on
   stripped text). A line the alignment pairs is in place; any other line was deleted,
   added or changed. A branch is in place when both its lines are. An after item at the
   place a before item of the same identity moved to is matched, and is not listed.
2. Not run before. An after branch whose from-line was an uncovered line before, at the
   same place, is not new: code that did not run had no tests to lose. v8 lists no
   arms inside a function that never ran, so such arms first appear when it runs.
   These are listed, but do not fail the check. This step runs before step 3, because
   such an arm is usually on lines that are in place.
3. In place. An after item still unmatched whose line is in place is new: the line was
   there before and was not uncovered, so it lost its tests. A before item still
   unmatched whose line is in place excuses nothing: its line is covered now.
4. Same text. What is left is code the line diff did not keep: before items whose line
   is gone, and after items whose line is new. Only that code may have moved, so only
   it matches by text. It is counted as a multiset per identity: if two uncovered
   `return None` lines of `web` are gone and three new ones appear, one is new. An
   after item matches gone items of its own file first, then of other files in the
   group; a match in another file is a move inside the group, and is listed so it can
   be checked. In a file whose text is unchanged every line is in place, so nothing in
   it matches by text.

So deleted code, absent from the after side, offsets nothing it could not have moved
to, and code moved to a different group shows as new. Limit: "in place" is the line
diff's guess, made on stripped text. When same-text lines sit close together, which one
stayed is the diff's choice, and it can be wrong either way:

- It can keep a covered line where an uncovered one moved in. The moved line is then
  reported as new, which fails closed.
- It can keep an uncovered line where a covered one stayed and lost its tests. The loss
  then matches at that place, which fails open. For example, cutting `if x:` and its
  `return None` from above a second `return None` keeps the first `return None`.

When some, not all, of a file's same-text candidates are new, every candidate is printed
with `[k of n]`; the counts are exact either way.

Output: one line per new item, `group  path:line  text`. A branch prints
`path:from->to  from-text  ->  to-text`, with the text once when both lines are one.
Then come the items matched in another file and the branches that did not run before,
a table of uncovered counts per group before and after, and a one-line summary.

Exit codes: 0 nothing new, 1 something new, 2 a missing or malformed input.
"""

from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SECTIONS = ("backend", "frontend")
KINDS = ("line", "branch")
WORKTREE = "WORKTREE"
EXIT_MARKER = "<exit>"
EXIT_LINE = 0  # the to-line of an exit branch, in an item's position
BACKEND_PREFIX = "src/grant_lab/"
FRONTEND_PREFIX = "frontend/src/"
SHOWN_FILES = 10  # the most files an error names


class InputError(Exception):
    """A report, revision or source file that cannot be read as expected."""


def _load_floors_checker() -> ModuleType:
    """`scripts/check_coverage_floors.py`, whose grouping this script shares."""
    name = "check_coverage_floors"
    path = Path(__file__).resolve().with_name(f"{name}.py")
    loaded = sys.modules.get(name)
    if loaded is not None and Path(getattr(loaded, "__file__", "")).resolve() == path:
        return loaded
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: @dataclass looks its own module up in sys.modules.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


floors = _load_floors_checker()


@dataclass(frozen=True)
class Item:
    """One uncovered line or branch, and the identity it is matched by."""

    group: str
    kind: str  # "line" or "branch"
    identity: tuple[str, ...]
    path: str
    position: tuple[int, ...]  # (line,) or (from-line, to-line); EXIT_LINE for an exit
    where: str  # "412", or "412->418" for a branch
    shown: str


@dataclass
class Finding:
    """After items of one identity in one file, `count` of which no before item matched."""

    count: int
    candidates: list[Item]

    @property
    def first(self) -> Item:
        return self.candidates[0]


@dataclass
class Side:
    """One report: its uncovered items, and every file and group it measured."""

    items: list[Item]
    files: set[str]
    groups: set[str]


@dataclass
class Result:
    new: list[Finding]
    moved: list[Finding]
    did_not_run: list[Item]
    before: Counter[tuple[str, str]]
    after: Counter[tuple[str, str]]
    groups: set[str]

    def new_counts(self) -> Counter[tuple[str, str]]:
        counts: Counter[tuple[str, str]] = Counter()
        for finding in self.new:
            counts[(finding.first.group, finding.first.kind)] += finding.count
        return counts


# --- source ---------------------------------------------------------------------


def _git(repo: Path, *args: str) -> bytes:
    try:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    except OSError as e:
        raise InputError(f"cannot run git: {e}") from e
    if done.returncode != 0:
        message = done.stderr.decode(errors="replace").strip() or f"exit {done.returncode}"
        raise InputError(f"git {' '.join(args)} failed in {repo}: {message}")
    return done.stdout


class Source:
    """The files of `repo` at one commit, or in its working tree when `rev` is None."""

    def __init__(self, repo: Path, rev: str | None) -> None:
        self.repo = repo
        if rev is None:
            self.rev = None
            self.label = f"the working tree at {repo}"
            listed = _git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
            names = {name for name in listed.decode().split("\0") if name}
            self.paths = {name for name in names if (repo / name).is_file()}
        else:
            try:
                sha = _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
            except InputError as e:
                raise InputError(f"no commit {rev!r} in {repo}") from e
            self.rev = sha.decode().strip()
            self.label = self.rev[:12]
            listed = _git(repo, "ls-tree", "-r", "-z", "--name-only", self.rev)
            self.paths = {name for name in listed.decode().split("\0") if name}
        self._cache: dict[str, list[str]] = {}

    def require(self, path: str) -> None:
        if path not in self.paths:
            raise InputError(f"{path} is in the report but not in {self.label}")

    def lines(self, path: str) -> list[str]:
        self.require(path)
        if path not in self._cache:
            if self.rev is None:
                data = (self.repo / path).read_bytes()
            else:
                data = _git(self.repo, "show", f"{self.rev}:{path}")
            self._cache[path] = data.decode("utf-8", errors="replace").splitlines()
        return self._cache[path]

    def text(self, path: str, number: int) -> str:
        lines = self.lines(path)
        if not 1 <= number <= len(lines):
            raise InputError(
                f"{path}:{number} is past the end of the file ({len(lines)} lines) in "
                f"{self.label}; was the report measured on it?"
            )
        text = lines[number - 1].strip()
        if not text:
            raise InputError(
                f"{path}:{number} is a blank line in {self.label}; was the report measured on it?"
            )
        return text


class LineMap:
    """Where each before line of a file sits in its after text, aligned on stripped text.

    A line the alignment pairs is in place; any other line was deleted, added or changed.
    """

    def __init__(self, before: Source, after: Source) -> None:
        self.before = before
        self.after = after
        self._maps: dict[str, tuple[dict[int, int], dict[int, int]]] = {}

    def _map(self, path: str) -> tuple[dict[int, int], dict[int, int]]:
        if path not in self._maps:
            forward: dict[int, int] = {}
            if path in self.before.paths and path in self.after.paths:
                old = [line.strip() for line in self.before.lines(path)]
                new = [line.strip() for line in self.after.lines(path)]
                matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
                for a, b, size in matcher.get_matching_blocks():
                    forward.update((a + k + 1, b + k + 1) for k in range(size))
            backward = {after: before for before, after in forward.items()}
            self._maps[path] = (forward, backward)
        return self._maps[path]

    def forward(self, path: str, position: tuple[int, ...]) -> tuple[int, ...] | None:
        """A before position in the after text; None when a line of it is gone."""
        return _through(self._map(path)[0], position)

    def backward(self, path: str, position: tuple[int, ...]) -> tuple[int, ...] | None:
        """An after position in the before text; None when a line of it is new."""
        return _through(self._map(path)[1], position)


def _through(mapping: dict[int, int], position: tuple[int, ...]) -> tuple[int, ...] | None:
    """`position` with each line mapped; None unless every line of it is in place."""
    moved = tuple(n if n == EXIT_LINE else mapping.get(n, -1) for n in position)
    return None if -1 in moved else moved


# --- reports --------------------------------------------------------------------


def backend_location(path: str) -> tuple[str, str]:
    """(repo path, group) of a coverage.py path, split as the floors check splits it."""
    _, sep, relative = path.replace("\\", "/").partition(BACKEND_PREFIX)
    if not sep:
        raise InputError(f"a file outside {BACKEND_PREFIX} in the report: {path}")
    return BACKEND_PREFIX + relative, floors._group_key(relative)


def frontend_location(path: str) -> tuple[str, str]:
    """(repo path, group) of an absolute Istanbul path, split as the floors check splits it."""
    _, sep, relative = path.replace("\\", "/").rpartition("/" + FRONTEND_PREFIX)
    if not sep:
        raise InputError(f"a file outside {FRONTEND_PREFIX} in the report: {path}")
    return FRONTEND_PREFIX + relative, floors._group_key(relative)


def _line(group: str, path: str, number: int, source: Source) -> Item:
    text = source.text(path, number)
    return Item(group, "line", (text,), path, (number,), str(number), text)


def _branch(group: str, path: str, frm: int, to: int | None, source: Source, *extra: str) -> Item:
    """A branch from line `frm` to line `to`; None is an exit from the function."""
    from_text = source.text(path, frm)
    to_text = EXIT_MARKER if to is None else source.text(path, to)
    return Item(
        group,
        "branch",
        (from_text, to_text, *extra),
        path,
        (frm, EXIT_LINE if to is None else to),
        f"{frm}->{'exit' if to is None else to}",
        from_text if to == frm else f"{from_text}  ->  {to_text}",
    )


def backend_side(report: dict[str, Any], source: Source) -> Side:
    if not {"meta", "files", "totals"} <= set(report):
        raise InputError("it has no meta, files and totals, as coverage.py JSON has")
    if not report["meta"].get("branch_coverage"):
        raise InputError("the report was measured without branch coverage (branch = true)")
    items: list[Item] = []
    measured: set[str] = set()
    groups: set[str] = set()
    for raw_path, data in report["files"].items():
        path, group = backend_location(raw_path)
        measured.add(path)
        groups.add(group)
        source.require(path)
        items.extend(_line(group, path, number, source) for number in data["missing_lines"])
        items.extend(
            _branch(group, path, frm, None if to < 0 else to, source)
            for frm, to in data["missing_branches"]
        )
    expected = {p for p in source.paths if p.startswith(BACKEND_PREFIX) and p.endswith(".py")}
    if unmeasured := sorted(expected - measured):
        raise InputError(
            f"{len(unmeasured)} file(s) of {source.label} are not in the report, such as "
            f"{unmeasured[0]}; was it measured on that revision, with --cov?"
        )
    _check_against_floors(report, items)
    return Side(items, measured, groups)


def _check_against_floors(report: dict[str, Any], items: list[Item]) -> None:
    """The report's lists must add up to its summaries, grouped by the floors check."""
    try:
        groups = floors.group_backend(report)
    except floors.InputError as e:
        raise InputError(str(e)) from e
    listed = Counter((item.group, item.kind) for item in items)
    for group, counts in groups.items():
        if group == floors.TOTAL_KEY:
            continue
        summed = {
            "line": counts.lines_total - counts.lines_covered,
            "branch": counts.branches_total - counts.branches_covered,
        }
        for kind, want in summed.items():
            if listed[(group, kind)] != want:
                raise InputError(
                    f"{group}: the report lists {listed[(group, kind)]} uncovered {kind}(s), "
                    f"but its summaries count {want}"
                )


def frontend_side(report: dict[str, Any], source: Source) -> Side:
    shape = ("statementMap", "s", "branchMap", "b")
    if not all(isinstance(data, dict) and set(shape) <= set(data) for data in report.values()):
        raise InputError(
            "it is not Istanbul's coverage-final.json (vitest's `json` reporter), whose "
            "every entry has statementMap, s, branchMap and b; coverage-summary.json has "
            "totals only"
        )
    if not report:
        raise InputError("the report measured no files")
    items: list[Item] = []
    measured: set[str] = set()
    groups: set[str] = set()
    for key, data in report.items():
        path, group = frontend_location(data.get("path", key))
        measured.add(path)
        groups.add(group)
        source.require(path)
        line_counts: dict[int, int] = {}
        for sid, count in data["s"].items():
            line = data["statementMap"][sid]["start"]["line"]
            line_counts[line] = max(line_counts.get(line, 0), count)
        items.extend(
            _line(group, path, number, source)
            for number, count in sorted(line_counts.items())
            if count == 0
        )
        for bid, counts in data["b"].items():
            entry = data["branchMap"][bid]
            for arm, count in enumerate(counts):
                if count == 0:
                    frm = entry["loc"]["start"]["line"]
                    to = entry["locations"][arm]["start"].get("line", frm)
                    items.append(_branch(group, path, frm, to, source, f"arm {arm}"))
    return Side(items, measured, groups)


READERS = {"backend": backend_side, "frontend": frontend_side}


def read_side(section: str, report_path: Path, source: Source) -> Side:
    if not report_path.is_file():
        raise InputError(f"no coverage report at {report_path}")
    try:
        report = json.loads(report_path.read_text())
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise InputError(f"{report_path} is not readable JSON: {e}") from e
    try:
        return READERS[section](report, source)
    except InputError as e:
        raise InputError(f"{report_path}: {e}") from e
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as e:
        raise InputError(f"{report_path} is not a {section} coverage report: {e!r}") from e


def require_survivors(before: Side, after: Side, after_source: Source, report: Path) -> None:
    """Every file the before report measured is in the after one, unless it was deleted."""
    if missing := sorted(p for p in before.files - after.files if p in after_source.paths):
        named = ", ".join(missing[:SHOWN_FILES])
        if len(missing) > SHOWN_FILES:
            named += f" and {len(missing) - SHOWN_FILES} more"
        raise InputError(
            f"{report}: {len(missing)} file(s) of the before report are still in "
            f"{after_source.label} but not in this report: {named}; was it measured on "
            "the whole suite?"
        )


# --- matching -------------------------------------------------------------------

Key = tuple[str, str, tuple[str, ...]]


def _by_key(items: list[Item]) -> dict[Key, dict[str, list[Item]]]:
    out: dict[Key, dict[str, list[Item]]] = defaultdict(lambda: defaultdict(list))
    for item in items:
        out[(item.group, item.kind, item.identity)][item.path].append(item)
    return out


def compare(before: Side, after: Side, lines: LineMap) -> Result:
    """Every after item that no equal before item of the same group accounts for."""
    old_by_key = _by_key(before.items)
    did_not_run_before = {(i.path, i.position) for i in before.items if i.kind == "line"}
    new: list[Finding] = []
    moved: list[Finding] = []
    did_not_run: list[Item] = []
    for key, new_by_path in _by_key(after.items).items():
        old_by_path = old_by_key.get(key, {})
        spare: Counter[str] = Counter()  # before items per file free to match by text
        unmatched: dict[str, list[Item]] = {}  # after items free to match by text
        in_place: dict[str, list[Item]] = {}
        for path in sorted(set(old_by_path) | set(new_by_path)):
            places = Counter(lines.forward(path, i.position) for i in old_by_path.get(path, []))
            for item in sorted(new_by_path.get(path, []), key=lambda i: i.position):
                if places[item.position]:
                    places[item.position] -= 1
                elif (
                    item.kind == "branch"
                    and (path, lines.backward(path, item.position[:1])) in did_not_run_before
                ):
                    # Before the in-place rule: such an arm is usually on lines in place.
                    did_not_run.append(item)
                elif lines.backward(path, item.position) is not None:
                    # Its line was there before and was not uncovered: it lost its tests.
                    in_place.setdefault(path, []).append(item)
                else:
                    unmatched.setdefault(path, []).append(item)
            # Only a before item whose line is gone (counted under None) may have moved,
            # so only those match by text. One still in place matched above, or is
            # covered now and excuses nothing.
            spare[path] = places[None]
        left: dict[str, int] = {}
        for path, items in unmatched.items():
            same_file = min(spare[path], len(items))
            spare[path] -= same_file
            left[path] = len(items) - same_file
        pool = sum(spare.values())
        for path, items in sorted(unmatched.items()):
            elsewhere = min(pool, left[path])
            pool -= elsewhere
            if elsewhere:
                moved.append(Finding(elsewhere, items))
            if left[path] > elsewhere:
                new.append(Finding(left[path] - elsewhere, items))
        new.extend(Finding(len(items), items) for items in in_place.values())
    return Result(
        sorted(new, key=_order),
        sorted(moved, key=_order),
        sorted(did_not_run, key=lambda i: (i.group, i.path, i.position)),
        Counter((item.group, item.kind) for item in before.items),
        Counter((item.group, item.kind) for item in after.items),
        before.groups | after.groups,
    )


def _order(finding: Finding) -> tuple[str, str, tuple[int, ...], str]:
    first = finding.first
    return (first.group, first.path, first.position, first.kind)


# --- output ---------------------------------------------------------------------


def _row(item: Item) -> str:
    return f"{item.group}  {item.path}:{item.where}  {item.shown}"


def _rows(finding: Finding) -> list[str]:
    if finding.count == len(finding.candidates):
        return [_row(item) for item in finding.candidates]
    first = finding.first
    wheres = ",".join(i.where for i in finding.candidates)
    share = f"[{finding.count} of {len(finding.candidates)}]"
    return [f"{first.group}  {first.path}:{wheres}  {first.shown}  {share}"]


def render(section: str, result: Result) -> str:
    out = [f"### Uncovered code: {section}", ""]
    if result.new:
        out.append("New uncovered lines and branches:")
        out.extend(row for finding in result.new for row in _rows(finding))
        out.append("")
    if result.moved:
        out.append("Matched in another file of the same group (check each one is a move):")
        out.extend(row for finding in result.moved for row in _rows(finding))
        out.append("")
    if result.did_not_run:
        out.append("Branches whose from-line did not run before (no tests to lose):")
        out.extend(_row(item) for item in result.did_not_run)
        out.append("")
    new = result.new_counts()
    out.append("| group | lines before | after | new | branches before | after | new |")
    out.append("|---|---|---|---|---|---|---|")
    for group in sorted(result.groups):
        cells = [
            str(counter[(group, kind)])
            for kind in KINDS
            for counter in (result.before, result.after, new)
        ]
        out.append(f"| {group} | {' | '.join(cells)} |")
    lines, branches = (sum(n for (_, k), n in new.items() if k == kind) for kind in KINDS)
    moved = sum(finding.count for finding in result.moved)
    out.append("")
    out.append(
        f"{'FAIL' if lines or branches else 'OK'}: {lines} new uncovered line(s), "
        f"{branches} new uncovered branch(es); {moved} matched in another file of its "
        f"group, {len(result.did_not_run)} branch(es) in code that did not run before"
    )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--section", choices=SECTIONS, required=True)
    parser.add_argument("--before", type=Path, required=True, help="the before report")
    parser.add_argument("--before-rev", required=True, help="the commit it was measured on")
    parser.add_argument("--after", type=Path, required=True, help="the after report")
    parser.add_argument(
        "--after-rev",
        default=WORKTREE,
        help=f"the commit it was measured on, or {WORKTREE} (the default) for the working tree",
    )
    parser.add_argument("--repo", type=Path, default=REPO, help="the git checkout to read")
    args = parser.parse_args(argv)

    try:
        if args.before.resolve() == args.after.resolve():
            raise InputError(f"--before and --after are the same file: {args.before}")
        before_source = Source(args.repo, args.before_rev)
        after_source = Source(args.repo, None if args.after_rev == WORKTREE else args.after_rev)
        before = read_side(args.section, args.before, before_source)
        after = read_side(args.section, args.after, after_source)
        require_survivors(before, after, after_source, args.after)
        result = compare(before, after, LineMap(before_source, after_source))
    except InputError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(render(args.section, result))
    return 1 if result.new else 0


if __name__ == "__main__":
    sys.exit(main())
