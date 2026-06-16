"""Generate / verify the wheels-only allowlist in ``[tool.uv] no-build-package``.

Supply-chain baseline (see the supply-chain design spec, §3.2): we want uv to
refuse building any *third-party* source distribution at install time, so no
dependency's sdist build backend runs arbitrary code on the build host. uv's
``no-build = true`` is global and would also block the editable install of our
own first-party ``call_manager`` package (which has no wheel) — and uv exposes
no allow-list to exempt the project root. So instead we enumerate every
third-party dependency in ``no-build-package`` (the per-package form), leaving
our own package free to build editable for the app + CLIs.

That list must stay in lockstep with ``uv.lock``. This script is the single
source of truth for it.

Usage::

    uv run python scripts/gen_no_build.py            # rewrite pyproject.toml
    uv run python scripts/gen_no_build.py --check     # CI: fail if out of sync

The ``--check`` mode also fails if any locked dependency is *source-only* (no
wheel), because such a dep cannot be added to the wheels-only list without
breaking ``uv sync``. That is the deliberate signal to review the new dep and,
if it is genuinely source-only and trusted, exclude it from the list (the
documented allowlist exception) with a note in the PR.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
LOCK = ROOT / "uv.lock"

# Matches the generated array body, from `no-build-package = [` to the closing
# `]` at column 0. The surrounding comment block is preserved.
ARRAY_RE = re.compile(r"(?ms)^no-build-package = \[\n.*?^\]\n")


def classify_packages(lock: dict) -> tuple[list[str], list[str]]:
    """Split an already-parsed ``uv.lock`` dict into third-party deps.

    Returns ``(sorted dep names with wheels, sorted source-only dep names)``.
    Editable / virtual packages (our own first-party project) are skipped —
    they must stay buildable and never enter the wheels-only allowlist.
    """
    with_wheels: set[str] = set()
    source_only: set[str] = set()
    for pkg in lock.get("package", []):
        src = pkg.get("source", {})
        if src.get("editable") or src.get("virtual"):
            continue  # our own first-party project — must stay buildable
        name = pkg["name"]
        (with_wheels if pkg.get("wheels") else source_only).add(name)
    return sorted(with_wheels), sorted(source_only)


def third_party_packages(lock_path: Path = LOCK) -> tuple[list[str], list[str]]:
    """Read ``uv.lock`` and classify its packages (see :func:`classify_packages`)."""
    return classify_packages(tomllib.loads(lock_path.read_text()))


def render_array(names: list[str]) -> str:
    body = "".join(f'    "{n}",\n' for n in names)
    return f"no-build-package = [\n{body}]\n"


def main(
    argv: list[str] | None = None,
    *,
    lock_path: Path = LOCK,
    pyproject_path: Path = PYPROJECT,
) -> int:
    """Generate or (with ``--check``) verify the allowlist.

    ``argv`` is the already-sliced argument list (no program name); it defaults
    to ``sys.argv[1:]``. ``lock_path`` / ``pyproject_path`` are injectable so the
    logic can be unit-tested against synthetic files.
    """
    if argv is None:
        argv = sys.argv[1:]
    check = "--check" in argv
    with_wheels, source_only = third_party_packages(lock_path)

    if source_only:
        print(
            "ERROR: these locked dependencies are source-only (no wheel) and "
            "cannot be added to the wheels-only allowlist:\n  "
            + "\n  ".join(source_only)
            + "\nReview each: if genuinely source-only and trusted, exclude it "
            "from no-build-package with a note in the PR.",
            file=sys.stderr,
        )
        return 2

    text = pyproject_path.read_text()
    if not ARRAY_RE.search(text):
        print("ERROR: no-build-package array not found in pyproject.toml", file=sys.stderr)
        return 2

    new_text = ARRAY_RE.sub(render_array(with_wheels), text, count=1)

    if check:
        if new_text != text:
            print(
                "ERROR: [tool.uv] no-build-package is out of sync with uv.lock.\n"
                "Run: uv run python scripts/gen_no_build.py",
                file=sys.stderr,
            )
            return 1
        print("no-build-package is in sync with uv.lock.")
        return 0

    if new_text != text:
        pyproject_path.write_text(new_text)
        print(f"Updated no-build-package with {len(with_wheels)} packages.")
    else:
        print("no-build-package already up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
