#!/usr/bin/env python3
"""Build a self-contained review package for an external adversarial reviewer.

    uv run python scripts/review_package.py BASE [HEAD] [-o PATH] [--claim FILE]

Writes one markdown file holding the commit list, diff stat and full `-U10` diff,
under a header that tells the reviewer how its findings must be evidenced.

WHY THE RULES AT THE TOP EXIST
------------------------------
Measured on the F3d3a branch (2026-08-21), the same model reviewed the same diff
through two harnesses. One injected a forbidden re-export at runtime and found a
guard test that could not fail; the other read the same test and passed it. The
difference was disposition, not capability — the reading reviewer had a shell and
had already run a subset of the suite.

Two things made the reading reviewer read:

1. The package told it the suite was green. It replied "I did not repeat the full
   3,786-test suite." Reassurance is heard as permission to skip, so this script
   states what was run as UNVERIFIED and asks for anything load-bearing to be
   re-derived.
2. Nothing required a receipt. A finding that a test "does enforce" something is a
   claim about behaviour, and the only evidence for it is breaking the thing and
   watching the test fail. So RAN/READ labelling is mandatory and per-finding —
   one blanket disclaimer at the end hides which findings were actually executed.

Keep both rules in this file rather than in a per-review prompt. A rule that lives
in the artifact cannot be forgotten on a tired day.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

RULES = """\
## How your findings must be evidenced — read before you start

**Label every finding `RAN` or `READ`.** Per finding, not once at the end. `RAN`
means you executed something and are pasting real output. `READ` means you reasoned
over source. Both are welcome; a package of findings that are all `READ` is itself
information, and hiding that behind a single closing disclaimer is not.

**Any claim about what a test enforces must be proven by breaking it.** If you say a
test does — or does not — catch something, inject the violation it claims to catch,
run the test, and paste the output. A test you only read is a test you have not
checked. This is the single highest-yield thing you can do with this package.

**A mutation must fail for the reason you claim.** A test that goes red because
collection aborted, or because an unrelated invariant tripped, proves nothing about
the mechanism you are probing. Read the failure text before believing your own
finding.

**Do not trust the "already verified" section below.** It records what the author
ran, as a courtesy so you do not spend your pass re-confirming green. It is
UNVERIFIED from your side. Re-derive anything your findings rest on.
"""


def sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("base", help="commit the branch started from")
    ap.add_argument("head", nargs="?", default="HEAD")
    ap.add_argument("-o", "--out", default=None, help="output path")
    ap.add_argument(
        "-C", "--repo", default=".", help="repo or worktree to read (default: cwd)"
    )
    ap.add_argument(
        "--claim",
        default=None,
        help="markdown file describing what the branch claims and what to attack; "
        "its text is inserted verbatim above the diff",
    )
    args = ap.parse_args()

    rng = f"{args.base}..{args.head}"
    out = Path(args.out or f"/tmp/review-package-{args.base[:7]}.md")

    parts = [f"# Review package — {rng}\n", RULES]

    if args.claim:
        parts.append("\n" + Path(args.claim).read_text().rstrip() + "\n")
    else:
        parts.append(
            "\n## The claim under review\n\n"
            "> No claim file was supplied. State plainly what this branch is supposed\n"
            "> to do and what would falsify it — a reviewer with no thesis to attack\n"
            "> defaults to reading for style.\n"
        )

    parts += [
        f"\n## Commits\n\n```\n{sh('git', '-C', args.repo, 'log', '--oneline', rng)}```\n",
        f"\n## Diff stat\n\n```\n{sh('git', '-C', args.repo, 'diff', '--stat', rng)}```\n",
        f"\n## Full diff (-U10)\n\n```diff\n"
        f"{sh('git', '-C', args.repo, 'diff', '-U10', rng)}```\n",
    ]

    out.write_text("".join(parts))
    size_kb = out.stat().st_size // 1024
    print(f"{out}  ({size_kb}K)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
