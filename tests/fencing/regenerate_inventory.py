"""Regenerate fence_inventory.json. RUN BY HAND, DELIBERATELY:

    uv run python -m tests.fencing.regenerate_inventory

NEVER call this from a test. A test that regenerates and then compares to what it
just generated always passes — a gate that cannot fail is worse than no gate,
because every later slice would trust it (Global Constraint 3).

New sites arrive category=UNCLASSIFIED and the gate REFUSES them until a human
classifies each. Renames need an explicit mapping below; without one a vanished
fenced site aborts the run rather than silently losing its proof.
"""

from __future__ import annotations

import json

from tests.fencing.inventory import INVENTORY, load_inventory, merge, scan_tree

#: Fill in for ONE run when you rename a fenced site, then clear it.
#: {("kind", "path", "old.symbol"): ("kind", "path", "new.symbol")}
RENAMES: dict[tuple[str, str, str], tuple[str, str, str]] = {}


def main() -> None:
    previous = load_inventory() if INVENTORY.exists() else []
    rows = merge(previous, scan_tree(), RENAMES)
    INVENTORY.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    unclassified = sum(1 for r in rows if r["category"] == "UNCLASSIFIED")
    print(f"wrote {len(rows)} sites to {INVENTORY}")
    if unclassified:
        print(f"  {unclassified} UNCLASSIFIED — classify each before the gate passes")


if __name__ == "__main__":
    main()
