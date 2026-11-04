"""Read `.github/workflows/ci.yml` the way the CI-wiring tests need it.

Only commands that can fail the build count. `yaml.safe_load` drops YAML
comments, `#` lines inside a `run:` body are skipped, and a job or step with any
`if:` or a truthy `continue-on-error` is left out, because a command that cannot
fail the build is not a gate.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
CI = REPO / ".github" / "workflows" / "ci.yml"


def enforces(node: dict) -> bool:
    """Whether a job or step both RUNS unconditionally and BLOCKS on failure.

    A GitHub `if:` is an expression, and proving one is truthy is not something
    a test can do. So any `if:` at all disqualifies a node from carrying a gate.
    The rule is scoped to the nodes whose commands a test depends on: a trace
    upload's legitimate `if: failure()` elsewhere in the file is fine.

    `continue-on-error` disqualifies unless it is literally false. A truthy
    string like "true" must not slip through a bare truthiness test.
    """
    if "if" in node:
        return False
    return not ("continue-on-error" in node and node["continue-on-error"] is not False)


def run_lines(path: Path = CI, *, job: str | None = None) -> list[str]:
    """Every non-comment line of every enforcing step's `run:` body.

    With `job`, only that job's lines; a missing or non-enforcing job gives [].
    """
    workflow = yaml.safe_load(path.read_text())
    jobs = workflow["jobs"]
    selected = [jobs[job]] if job is not None and job in jobs else []
    if job is None:
        selected = list(jobs.values())
    out: list[str] = []
    for node in selected:
        if not enforces(node):
            continue
        for step in node.get("steps", []):
            body = step.get("run")
            if not body or not enforces(step):
                continue
            for line in str(body).splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    out.append(stripped)
    return out
