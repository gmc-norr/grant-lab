"""Hold test coverage at its recorded floors, per package.

Reads one coverage report, groups it by top-level package (backend) or folder
(frontend), and compares each group's line and branch coverage with
`coverage-floors.json`:

    uv run python scripts/check_coverage_floors.py --section backend --report coverage.json
    uv run python scripts/check_coverage_floors.py --section frontend \\
        --report frontend/coverage/coverage-summary.json

Backend reports are coverage.py JSON; frontend reports are vitest's
`coverage-summary.json`. The script reads raw counts only. coverage.py's
`percent_covered` mixes lines and branches, and vitest writes `pct` as the string
"Unknown" when a total is 0, so neither tool's own percentage is used.

Percentages are integer tenths, `covered * 1000 // total`, so they round down and
are compared as integers. `--update` writes floors with the same rounding, taking
the lowest value over every `--report` given; reports that disagree on their groups,
or on which groups have branches, are refused. `--report-only` prints the table and
exits 0. Every run appends the table to `$GITHUB_STEP_SUMMARY` when it is set; it shows
each group's measured value, its floor and the difference, all in one-decimal form.

Exit codes: 0 pass, 1 a floor problem, 2 a missing or malformed input.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_FLOORS = REPO / "coverage-floors.json"
ROOT_KEY = "<root>"
TOTAL_KEY = "<total>"
SECTIONS = ("backend", "frontend")


class InputError(Exception):
    """A report or floors file that cannot be read as expected."""


@dataclass
class Counts:
    lines_covered: int = 0
    lines_total: int = 0
    branches_covered: int = 0
    branches_total: int = 0

    def add(self, other: Counts) -> None:
        self.lines_covered += other.lines_covered
        self.lines_total += other.lines_total
        self.branches_covered += other.branches_covered
        self.branches_total += other.branches_total


def tenths(covered: int, total: int) -> int:
    """Percent in integer tenths, rounded down: 7 of 9 is 777 (77.7%)."""
    return covered * 1000 // total


def show(value: int) -> str:
    return f"{value // 10}.{value % 10}"


def floor_tenths(value: float) -> int:
    """A stored one-decimal floor as integer tenths (89.5 -> 895)."""
    return round(value * 10)


def show_difference(measured: int, floor: int) -> str:
    """Measured minus floor, in signed one-decimal form: +0.4, -1.2, +0.0."""
    difference = measured - floor
    return f"{'-' if difference < 0 else '+'}{show(abs(difference))}"


def _group_key(relative: str) -> str:
    return relative.split("/", 1)[0] if "/" in relative else ROOT_KEY


def _with_total(groups: dict[str, Counts], total: Counts) -> dict[str, Counts]:
    if total.lines_total == 0:
        raise InputError("the report measured no statements")
    summed = Counts()
    for counts in groups.values():
        summed.add(counts)
    if summed != total:
        raise InputError(f"the files add up to {summed}, but the report's total is {total}")
    measured = {key: counts for key, counts in groups.items() if counts.lines_total > 0}
    measured[TOTAL_KEY] = total
    return measured


def group_backend(report: dict) -> dict[str, Counts]:
    """Group a coverage.py JSON report by the folder after `src/grant_lab/`."""
    groups: dict[str, Counts] = {}
    for path, data in report["files"].items():
        _, sep, relative = path.replace("\\", "/").partition("src/grant_lab/")
        if not sep:
            raise InputError(f"a file outside src/grant_lab/ in the report: {path}")
        summary = data["summary"]
        groups.setdefault(_group_key(relative), Counts()).add(
            Counts(
                summary["covered_lines"],
                summary["num_statements"],
                summary.get("covered_branches", 0),
                summary.get("num_branches", 0),
            )
        )
    totals = report["totals"]
    total = Counts(
        totals["covered_lines"],
        totals["num_statements"],
        totals.get("covered_branches", 0),
        totals.get("num_branches", 0),
    )
    return _with_total(groups, total)


def group_frontend(summary: dict) -> dict[str, Counts]:
    """Group a vitest coverage-summary.json by the folder after `frontend/src/`."""
    groups: dict[str, Counts] = {}
    for path, data in summary.items():
        if path == "total":
            continue
        _, sep, relative = path.replace("\\", "/").rpartition("/frontend/src/")
        if not sep:
            raise InputError(f"a file outside frontend/src/ in the report: {path}")
        groups.setdefault(_group_key(relative), Counts()).add(
            Counts(
                data["lines"]["covered"],
                data["lines"]["total"],
                data["branches"]["covered"],
                data["branches"]["total"],
            )
        )
    t = summary["total"]
    total = Counts(
        t["lines"]["covered"], t["lines"]["total"], t["branches"]["covered"], t["branches"]["total"]
    )
    return _with_total(groups, total)


GROUPERS = {"backend": group_backend, "frontend": group_frontend}


def read_report(section: str, path: Path) -> dict[str, Counts]:
    if not path.is_file():
        raise InputError(f"no coverage report at {path}")
    try:
        return GROUPERS[section](json.loads(path.read_text()))
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        raise InputError(f"{path} is not a {section} coverage report: {e!r}") from e


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def load_floors(path: Path) -> dict[str, dict]:
    """Read the floors file: {section: {group: {"line": n, "branch": n (optional)}}}."""
    if not path.is_file():
        return {}
    try:
        floors = json.loads(path.read_text())
    except ValueError as e:
        raise InputError(f"the floors file {path} is not valid JSON: {e}") from e
    shape = "the floors file must be {section: {group: {line, branch?}}}"
    if not isinstance(floors, dict):
        raise InputError(f"{path}: {shape}")
    for section, groups in floors.items():
        if not isinstance(groups, dict):
            raise InputError(f"{path}: {shape}; section {section!r} is not an object")
        for group, floor in groups.items():
            where = f"{path}: {section}/{group}"
            if not isinstance(floor, dict) or not _is_number(floor.get("line")):
                raise InputError(f"{where} needs a numeric 'line' floor")
            if "branch" in floor and not _is_number(floor["branch"]):
                raise InputError(f"{where} has a 'branch' floor that is not a number")
    return floors


def problems(measured: dict[str, Counts], floors: dict[str, dict]) -> list[str]:
    out: list[str] = []
    for key in sorted(measured):
        counts = measured[key]
        floor = floors.get(key)
        if floor is None:
            out.append(f"{key}: has no floor (a new group needs one; set it with --update)")
            continue
        line = tenths(counts.lines_covered, counts.lines_total)
        if line < floor_tenths(floor["line"]):
            out.append(f"{key}: line {show(line)}% is below its floor {floor['line']}%")
        has_branches = counts.branches_total > 0
        if has_branches != ("branch" in floor):
            change = "gained" if has_branches else "lost"
            out.append(f"{key}: {change} branches since its floor was set (update it on purpose)")
        elif has_branches:
            branch = tenths(counts.branches_covered, counts.branches_total)
            if branch < floor_tenths(floor["branch"]):
                out.append(f"{key}: branch {show(branch)}% is below its floor {floor['branch']}%")
    stale = sorted(set(floors) - set(measured))
    out.extend(f"{key}: has a floor but no measured group" for key in stale)
    new = sorted(key for key in measured if key not in floors)
    if stale and new:
        out.append(f"if a group was renamed, move its floor: {stale} -> {new}")
    return out


def _floor_cells(value: int | None, floor: float | None) -> str:
    """The "floor | diff" cells of one table row; dashes where either is missing."""
    if floor is None:
        return "- | -"
    wanted = floor_tenths(floor)
    return f"{show(wanted)} | {'-' if value is None else show_difference(value, wanted)}"


def table(section: str, measured: dict[str, Counts], floors: dict[str, dict]) -> str:
    rows = [
        f"### Coverage: {section}",
        "",
        "| group | line % | floor | diff | branch % | floor | diff |",
        "|---|---|---|---|---|---|---|",
    ]
    for key in sorted(measured):
        counts = measured[key]
        floor = floors.get(key, {})
        line = tenths(counts.lines_covered, counts.lines_total)
        branch = (
            tenths(counts.branches_covered, counts.branches_total)
            if counts.branches_total
            else None
        )
        rows.append(
            f"| {key} | {show(line)} | {_floor_cells(line, floor.get('line'))}"
            f" | {'-' if branch is None else show(branch)}"
            f" | {_floor_cells(branch, floor.get('branch'))} |"
        )
    return "\n".join(rows) + "\n"


def lowest(reports: list[dict[str, Counts]]) -> dict[str, dict]:
    """Floors from several runs: per group, the lowest value, in one-decimal form."""
    keys = set(reports[0])
    for report in reports[1:]:
        if set(report) != keys:
            raise InputError(f"the reports disagree on their groups: {sorted(keys ^ set(report))}")
    floors: dict[str, dict] = {}
    for key in sorted(keys):
        with_branches = {r[key].branches_total > 0 for r in reports}
        if len(with_branches) > 1:
            raise InputError(f"{key}: has branches in some reports and none in others")
        line = min(tenths(r[key].lines_covered, r[key].lines_total) for r in reports)
        entry: dict[str, float] = {"line": line / 10}
        if with_branches == {True}:
            branch = min(tenths(r[key].branches_covered, r[key].branches_total) for r in reports)
            entry["branch"] = branch / 10
        floors[key] = entry
    return floors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--section", choices=SECTIONS, required=True)
    parser.add_argument("--report", type=Path, action="append", required=True)
    parser.add_argument("--floors", type=Path, default=DEFAULT_FLOORS)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--report-only", action="store_true")
    mode.add_argument("--update", action="store_true")
    args = parser.parse_args(argv)

    try:
        reports = [read_report(args.section, path) for path in args.report]
        all_floors = load_floors(args.floors)
        if args.update:
            all_floors[args.section] = lowest(reports)
            args.floors.write_text(json.dumps(all_floors, indent=2, sort_keys=True) + "\n")
            print(f"wrote {args.section} floors from {len(reports)} report(s) to {args.floors}")
            return 0
        if len(reports) != 1:
            raise InputError("checking takes exactly one --report")
        if not args.floors.is_file() and not args.report_only:
            raise InputError(f"no floors file at {args.floors}")
    except InputError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    measured = reports[0]
    floors = all_floors.get(args.section, {})
    text = table(args.section, measured, floors)
    found = problems(measured, floors) if floors or not args.report_only else []
    if found:
        text += "\n" + "\n".join(f"- {p}" for p in found) + "\n"
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(text + "\n")
    if args.report_only:
        return 0
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
