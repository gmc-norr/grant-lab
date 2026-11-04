"""`scripts/check_coverage_floors.py`: per-package coverage floors.

Each test builds a small report in the shape coverage.py or vitest writes, and a
floors file, then runs the script's `main` and checks the exit code and message.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_coverage_floors.py"
_spec = importlib.util.spec_from_file_location("check_coverage_floors", SCRIPT)
assert _spec is not None and _spec.loader is not None
floors_mod = importlib.util.module_from_spec(_spec)
# Registered before it runs: @dataclass looks its own module up in sys.modules.
sys.modules[_spec.name] = floors_mod
_spec.loader.exec_module(floors_mod)


def backend_report(files: dict[str, tuple[int, int, int, int]]) -> dict:
    """coverage.py JSON: {path: (lines covered, statements, branches covered, branches)}.

    Every file's `percent_covered` is a deliberately wrong 12.3, so a script that
    read it instead of the counts would fail these tests.
    """
    keys = ("covered_lines", "num_statements", "covered_branches", "num_branches")
    totals = dict.fromkeys(keys, 0)
    out: dict = {"files": {}, "totals": totals}
    for path, counts in files.items():
        summary = dict(zip(keys, counts, strict=True))
        summary["percent_covered"] = 12.3
        out["files"][path] = {"summary": summary}
        for key, value in summary.items():
            if key in totals:
                totals[key] += value
    return out


def frontend_summary(files: dict[str, tuple[int, int, int, int]]) -> dict:
    """vitest coverage-summary.json with absolute keys and a `total` entry."""

    def entry(lc: int, lt: int, bc: int, bt: int) -> dict:
        def metric(covered: int, total: int) -> dict:
            pct = "Unknown" if total == 0 else round(100 * covered / total, 2)
            return {"total": total, "covered": covered, "skipped": 0, "pct": pct}

        return {"lines": metric(lc, lt), "branches": metric(bc, bt)}

    out = {path: entry(*counts) for path, counts in files.items()}
    sums = [sum(c[i] for c in files.values()) for i in range(4)]
    out["total"] = entry(*sums)
    return out


def run(tmp_path: Path, section: str, report: dict, floors: dict | None, *extra: str) -> int:
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))
    floors_path = tmp_path / "floors.json"
    if floors is not None:
        floors_path.write_text(json.dumps({section: floors}))
    return floors_mod.main(
        ["--section", section, "--report", str(report_path), "--floors", str(floors_path), *extra]
    )


WEB = "src/grant_lab/web/app.py"
MAIN = "src/grant_lab/main.py"


def test_tenths_round_down():
    assert floors_mod.tenths(2, 3) == 666
    assert floors_mod.tenths(1, 1) == 1000


def test_a_report_at_its_floors_passes(tmp_path, capsys):
    report = backend_report({WEB: (90, 100, 8, 10), MAIN: (10, 10, 0, 0)})
    floors = {
        "web": {"line": 90.0, "branch": 80.0},
        "<root>": {"line": 100.0},
        "<total>": {"line": 90.9, "branch": 80.0},
    }
    assert run(tmp_path, "backend", report, floors) == 0
    assert "| web | 90.0 | 90.0 | 80.0 | 80.0 |" in capsys.readouterr().out


def test_one_tenth_below_a_line_floor_fails(tmp_path, capsys):
    report = backend_report({WEB: (899, 1000, 0, 0)})
    floors = {"web": {"line": 90.0}, "<total>": {"line": 89.9}}
    assert run(tmp_path, "backend", report, floors) == 1
    assert "web: line 89.9% is below its floor 90.0%" in capsys.readouterr().out


def test_a_branch_below_its_floor_fails(tmp_path, capsys):
    report = backend_report({WEB: (10, 10, 7, 10)})
    floors = {"web": {"line": 100.0, "branch": 80.0}, "<total>": {"line": 100.0, "branch": 70.0}}
    assert run(tmp_path, "backend", report, floors) == 1
    assert "web: branch 70.0% is below its floor 80.0%" in capsys.readouterr().out


def test_the_tools_own_percentage_is_never_read(tmp_path):
    """Every file says percent_covered 12.3; the counts say 100%. The floor of 100
    passes only if the script uses the counts."""
    report = backend_report({WEB: (10, 10, 4, 4)})
    floors = {"web": {"line": 100.0, "branch": 100.0}, "<total>": {"line": 100.0, "branch": 100.0}}
    assert run(tmp_path, "backend", report, floors) == 0


def test_a_group_with_no_floor_fails(tmp_path, capsys):
    report = backend_report({WEB: (10, 10, 0, 0), "src/grant_lab/boards/b.py": (5, 5, 0, 0)})
    floors = {"web": {"line": 100.0}, "<total>": {"line": 100.0}}
    assert run(tmp_path, "backend", report, floors) == 1
    assert "boards: has no floor" in capsys.readouterr().out


def test_a_floor_with_no_group_fails_and_a_rename_is_suggested(tmp_path, capsys):
    report = backend_report({"src/grant_lab/boards2/b.py": (5, 5, 0, 0)})
    floors = {"boards": {"line": 100.0}, "<total>": {"line": 100.0}}
    assert run(tmp_path, "backend", report, floors) == 1
    out = capsys.readouterr().out
    assert "boards: has a floor but no measured group" in out
    assert "['boards'] -> ['boards2']" in out


def test_a_group_that_gained_branches_fails(tmp_path, capsys):
    report = backend_report({WEB: (10, 10, 1, 2)})
    floors = {"web": {"line": 100.0}, "<total>": {"line": 100.0, "branch": 50.0}}
    assert run(tmp_path, "backend", report, floors) == 1
    assert "web: gained branches" in capsys.readouterr().out


def test_a_group_with_no_statements_is_skipped(tmp_path):
    report = backend_report({WEB: (10, 10, 0, 0), "src/grant_lab/empty/__init__.py": (0, 0, 0, 0)})
    floors = {"web": {"line": 100.0}, "<total>": {"line": 100.0}}
    assert run(tmp_path, "backend", report, floors) == 0


def test_totals_that_disagree_with_the_files_are_refused(tmp_path, capsys):
    report = backend_report({WEB: (10, 10, 0, 0)})
    report["totals"]["covered_lines"] = 9
    assert run(tmp_path, "backend", report, {"web": {"line": 1.0}}) == 2
    assert "the report's total" in capsys.readouterr().err


def test_a_missing_report_is_refused(tmp_path, capsys):
    assert floors_mod.main(["--section", "backend", "--report", str(tmp_path / "nope.json")]) == 2
    assert "no coverage report" in capsys.readouterr().err


def test_frontend_groups_by_folder_and_ignores_an_unknown_pct(tmp_path):
    base = "/home/runner/work/grant-lab/grant-lab/frontend/src/"
    summary = frontend_summary(
        {
            base + "routes/Feed.tsx": (9, 10, 3, 4),
            base + "theme/tokens.ts": (5, 5, 0, 0),
            base + "main.tsx": (0, 4, 0, 0),
        }
    )
    floors = {
        "routes": {"line": 90.0, "branch": 75.0},
        "theme": {"line": 100.0},
        "<root>": {"line": 0.0},
        "<total>": {"line": 73.6, "branch": 75.0},
    }
    assert run(tmp_path, "frontend", summary, floors) == 0


def test_report_only_exits_zero_below_a_floor_and_still_lists_it(tmp_path, capsys):
    report = backend_report({WEB: (1, 10, 0, 0)})
    floors = {"web": {"line": 90.0}, "<total>": {"line": 90.0}}
    assert run(tmp_path, "backend", report, floors, "--report-only") == 0
    assert "web: line 10.0% is below its floor 90.0%" in capsys.readouterr().out


def test_report_only_needs_no_floors_file(tmp_path):
    report = backend_report({WEB: (1, 10, 0, 0)})
    assert run(tmp_path, "backend", report, None, "--report-only") == 0


def test_update_writes_the_lowest_of_several_runs_and_keeps_the_other_section(tmp_path):
    floors_path = tmp_path / "floors.json"
    floors_path.write_text(json.dumps({"frontend": {"<total>": {"line": 50.0}}}))
    paths = []
    for i, covered in enumerate((9, 8)):
        path = tmp_path / f"run{i}.json"
        path.write_text(json.dumps(backend_report({WEB: (covered, 9, 2, 3)})))
        paths.append(path)
    argv = ["--section", "backend", "--floors", str(floors_path), "--update"]
    for path in paths:
        argv += ["--report", str(path)]
    assert floors_mod.main(argv) == 0
    written = json.loads(floors_path.read_text())
    assert written["frontend"] == {"<total>": {"line": 50.0}}
    assert written["backend"]["web"] == {"line": 88.8, "branch": 66.6}
    assert written["backend"]["<total>"] == {"line": 88.8, "branch": 66.6}


def test_update_refuses_runs_that_disagree_on_branches(tmp_path, capsys):
    floors_path = tmp_path / "floors.json"
    argv = ["--section", "backend", "--floors", str(floors_path), "--update"]
    for i, branches in enumerate((3, 0)):
        path = tmp_path / f"run{i}.json"
        path.write_text(json.dumps(backend_report({WEB: (8, 9, min(2, branches), branches)})))
        argv += ["--report", str(path)]
    assert floors_mod.main(argv) == 2
    assert "has branches in some reports and none in others" in capsys.readouterr().err
    assert not floors_path.exists()


def test_the_table_is_appended_to_the_github_step_summary(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    report = backend_report({WEB: (10, 10, 0, 0)})
    assert (
        run(tmp_path, "backend", report, {"web": {"line": 100.0}, "<total>": {"line": 100.0}}) == 0
    )
    assert "### Coverage: backend" in summary.read_text()


@pytest.mark.parametrize("section", ["backend", "frontend"])
def test_a_report_of_the_wrong_shape_is_refused(tmp_path, section):
    assert run(tmp_path, section, {"unexpected": 1}, {"x": {"line": 1.0}}) == 2
