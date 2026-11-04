"""The type checker and the coverage floors must be INVOKED by CI, not merely exist.

mypy, the backend coverage floors and the frontend coverage floors each need a
CI step that runs them and can fail the build. A pin accepts a step only under
these rules (`ci_workflow.sole_command_lines` enforces the first four):

1. The job and the step always run and block on failure: no `if:` and no truthy
   `continue-on-error`.
2. The job has no `needs:` (a needed job that is skipped skips this one).
3. No shell is overridden: no `shell:` on the step, and no `defaults.run.shell`
   on the job or the workflow.
4. The step's `run:` body is exactly one command line. Any second line (`exit 0`,
   `set +e`, a heredoc, a continuation) could neutralise the gate.
5. The line is split into shell words. The gate command must start a command of
   its own (not follow an `echo`), with nothing that can swallow its failure:
   `||`, `;`, `|`, `|&`, `&`.
6. A floors check carries none of NEVER_IN_CI (flags that make it always pass)
   and none of EXITS_WITHOUT_CHECKING (`-h`, `--help`, which exit 0 unchecked).

Each pin is also run against copies of the workflow that break it in each of
these ways, and must reject every one. NEVER_IN_CI and SWALLOWS_FAILURE are
checked against literal expectations too, so deleting an entry cannot delete its
own tests.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from tests.architecture.ci_workflow import CI, sole_command_lines

# Any of these lets a step succeed when the command before it fails. `&&` is
# safe: the step still fails when either side does.
SWALLOWS_FAILURE = frozenset({"||", ";", "|", "|&", "&"})

# Flags that make a floors check exit 0 whatever the coverage is.
NEVER_IN_CI = ("--update",)

# Flags that make argparse print usage and exit 0 without checking anything.
EXITS_WITHOUT_CHECKING = ("-h", "--help")

BANNED_FLAGS = (*NEVER_IN_CI, *EXITS_WITHOUT_CHECKING)

MYPY = ["uv", "run", "mypy", "--config-file", "pyproject.toml"]
PYTEST = ["uv", "run", "pytest"]
FLOORS = ["uv", "run", "python", "scripts/check_coverage_floors.py"]
VITEST = ["pnpm", "run", "test:coverage"]


def _commands(lines: list[str]) -> list[list[str]]:
    """Every simple command on the lines, as shell words, split at `&&`.

    A line with anything in SWALLOWS_FAILURE gives no commands, and neither does
    a line that is not valid shell (an unclosed quote).
    """
    out: list[list[str]] = []
    for line in lines:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        try:
            words = list(lexer)
        except ValueError:
            continue
        if SWALLOWS_FAILURE & set(words):
            continue
        command: list[str] = []
        for word in [*words, "&&"]:
            if word == "&&":
                out.append(command)
                command = []
            else:
                command.append(word)
    return out


def _has(command: list[str], *words: str) -> bool:
    """Whether `words` appear side by side in `command`, e.g. a flag and its value."""
    n = len(words)
    return any(tuple(command[i : i + n]) == words for i in range(len(command) - n + 1))


def _typecheck_runs_mypy(lines: list[str]) -> bool:
    return MYPY in _commands(lines)


def _backend_measures_coverage(lines: list[str]) -> bool:
    return any(
        command[:3] == PYTEST
        and _has(command, "--cov")
        and _has(command, "--cov-report=json:coverage.json")
        for command in _commands(lines)
    )


def _checks_floors(lines: list[str], section: str, report: str) -> bool:
    return any(
        command[:4] == FLOORS
        and _has(command, "--section", section)
        and _has(command, "--report", report)
        and not any(flag in command for flag in BANNED_FLAGS)
        for command in _commands(lines)
    )


def _backend_checks_its_floors(lines: list[str]) -> bool:
    return _checks_floors(lines, "backend", "coverage.json")


def _frontend_measures_coverage(lines: list[str]) -> bool:
    return VITEST in _commands(lines)


def _frontend_checks_its_floors(lines: list[str]) -> bool:
    return _checks_floors(lines, "frontend", "frontend/coverage/coverage-summary.json")


@dataclass(frozen=True)
class Pin:
    job: str
    check: Callable[[list[str]], bool]
    command: str  # how the gate command starts in ci.yml
    step: str  # text found in exactly one step: the step the mutations change
    floors: bool = False  # a floors check, so BANNED_FLAGS apply


PINS: dict[str, Pin] = {
    "typecheck runs mypy": Pin("typecheck", _typecheck_runs_mypy, "uv run mypy", "uv run mypy"),
    "backend measures coverage": Pin(
        "backend", _backend_measures_coverage, "uv run pytest", "uv run pytest"
    ),
    "backend checks its floors": Pin(
        "backend",
        _backend_checks_its_floors,
        "uv run python scripts/check_coverage_floors.py",
        "--section backend",
        floors=True,
    ),
    "frontend measures coverage": Pin(
        "frontend", _frontend_measures_coverage, "pnpm run test:coverage", "pnpm run test:coverage"
    ),
    "frontend checks its floors": Pin(
        "frontend",
        _frontend_checks_its_floors,
        "uv run python scripts/check_coverage_floors.py",
        "--section frontend",
        floors=True,
    ),
}


@pytest.mark.parametrize("name", sorted(PINS))
def test_each_safety_check_runs_in_its_ci_job(name):
    pin = PINS[name]
    assert pin.check(sole_command_lines(CI, job=pin.job)), (
        f"{name}: no enforcing step in job {pin.job!r}"
    )


STEP_MUTATIONS = [
    "remove",
    "step-if-false",
    "job-if-false",
    "continue-on-error",
    "step-shell",
    "job-shell",
    "workflow-shell",
    "job-needs",
]
LINE_MUTATIONS = ["or-true", "pipe-and", "echo", "exit-before", "set+e-around"]


def test_the_banned_flag_lists_are_not_shortened():
    """Literal expectations: `_mutations` is built from these tuples, so shrinking
    one would silently delete its own mutation cases."""
    assert {"--update"} <= set(NEVER_IN_CI)
    assert {"-h", "--help"} <= set(EXITS_WITHOUT_CHECKING)
    assert {"||", ";", "|", "|&", "&"} <= SWALLOWS_FAILURE


def _mutations(pin: Pin) -> list[str]:
    flags = [f"add {flag}" for flag in BANNED_FLAGS] if pin.floors else []
    return STEP_MUTATIONS + LINE_MUTATIONS + flags


def _rewrite(line: str, pin: Pin, how: str) -> str:
    if how == "or-true":
        return f"{line} || true"
    if how == "pipe-and":
        return f"{line} |& tee out.log"
    if how == "echo":
        return line.replace(pin.command, f"echo {pin.command}", 1)
    return f"{line} {how.removeprefix('add ')}"


def _mutated(tmp_path: Path, pin: Pin, how: str) -> Path:
    """A copy of ci.yml whose one step containing `pin.step` is broken in one way."""
    workflow = yaml.safe_load(CI.read_text())
    hits = 0
    for job in workflow["jobs"].values():
        steps = job.get("steps", [])
        for step in list(steps):
            body = str(step.get("run", ""))
            if pin.step not in body:
                continue
            hits += 1
            if how == "remove":
                steps.remove(step)
            elif how == "step-if-false":
                step["if"] = "false"
            elif how == "job-if-false":
                job["if"] = "false"
            elif how == "continue-on-error":
                step["continue-on-error"] = True
            elif how == "step-shell":
                step["shell"] = "true {0}"
            elif how == "job-shell":
                job["defaults"] = {"run": {"shell": "true {0}"}}
            elif how == "workflow-shell":
                workflow["defaults"] = {"run": {"shell": "true {0}"}}
            elif how == "job-needs":
                job["needs"] = ["audit"]
            elif how in ("exit-before", "set+e-around"):
                gate = body.splitlines()[0]
                extra = {"exit-before": ["exit 0", gate], "set+e-around": ["set +e", gate, "true"]}
                step["run"] = "\n".join(extra[how])
            else:
                lines = body.splitlines()
                at = next(i for i, line in enumerate(lines) if pin.step in line)
                lines[at] = _rewrite(lines[at], pin, how)
                step["run"] = "\n".join(lines)
    assert hits == 1, f"{pin.step!r} is in {hits} steps; the mutation needs exactly one"
    out = tmp_path / "ci.yml"
    out.write_text(yaml.safe_dump(workflow))
    return out


@pytest.mark.parametrize(
    ("name", "how"), [(name, how) for name in sorted(PINS) for how in _mutations(PINS[name])]
)
def test_each_pin_rejects_a_step_that_cannot_fail_the_build(tmp_path, name, how):
    pin = PINS[name]
    assert pin.check(sole_command_lines(CI, job=pin.job)), (
        "control: the real workflow must pass first"
    )
    mutant = _mutated(tmp_path, pin, how)
    assert mutant.read_text() != yaml.safe_dump(yaml.safe_load(CI.read_text())), "no change made"
    assert not pin.check(sole_command_lines(mutant, job=pin.job))


def test_the_frontend_floors_check_runs_after_our_own_gates_and_before_the_audit():
    """A failing step skips every later step in its job (the hazard the header
    comment of ci.yml describes). So the floors check comes after every gate on
    our own code (tests, build, drift check), and the blocking `pnpm audit`, which
    can go red for reasons outside our code, stays after it."""
    steps = yaml.safe_load(CI.read_text())["jobs"]["frontend"]["steps"]
    names = [step.get("name", "") for step in steps]
    at = names.index("Coverage floors (frontend)")
    assert names[at - 3 : at] == [
        "Frontend unit tests with coverage (vitest)",
        "Frontend typecheck + production build",
        "OpenAPI types in sync (drift check)",
    ]
    assert names[at + 1 : at + 3] == [
        "Upload the frontend coverage report",
        "pnpm audit (production deps — blocking)",
    ]
