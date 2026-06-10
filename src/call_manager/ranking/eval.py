"""Golden-set evaluation — Kendall's tau between CallRanking scores and
human-curated expected scores.

Read-only. Logs a structured summary; the admin panel surfaces the most
recent result.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from call_manager.db.models import CallRanking

_logger = logging.getLogger(__name__)

# From src/call_manager/ranking/eval.py: parents[0] is `ranking/`, [1] is
# `call_manager/`, [2] is `src/`, [3] is the repo root. Fixture lives at
# <repo>/tests/fixtures/golden_set.json — so parents[3], not [2].
_DEFAULT_FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "golden_set.json"
# Sanity check the parents[3] math — fail loudly at import time if the
# repo layout shifts and this constant points somewhere unexpected.
assert _DEFAULT_FIXTURE.parent.name == "fixtures", _DEFAULT_FIXTURE

# Path the weekly eval job writes to and /admin reads from. Computed once
# at import — env-var override only takes effect on (re-)import. Tests
# override this constant via monkeypatch.setattr rather than the env var.
#
# Anchored to an absolute path: when CALL_MANAGER_DATA_DIR is unset the
# default "data" is relative to CWD, so the scheduler (writer) and web admin
# (reader) — which may launch from different working directories — would
# otherwise resolve it to different files and silently disagree on the last
# report. resolve() pins it to one absolute location regardless of CWD.
_REPORT_PATH = (
    Path(os.environ.get("CALL_MANAGER_DATA_DIR", "data")).resolve() / "golden_set_last.json"
)


@dataclass(frozen=True)
class GoldenSetReport:
    """Golden-set evaluation result.

    `n_fixture` is the size of the curated triple list. `n_compared` is
    the count of triples that actually had a CallRanking row to compare
    against — usually less than n_fixture if some pairs haven't been
    ranked yet. tau is computed on n_compared rows, NOT n_fixture, so the
    displayed tau and the count must agree."""

    n_fixture: int
    n_compared: int
    tau: float | None


def kendalls_tau(xs: list[float], ys: list[float]) -> float:
    """Kendall's tau-b — handles ties. O(n²); fine for n ≤ ~100."""
    n = len(xs)
    if n < 2:
        return 0.0
    concordant = discordant = ties_x = ties_y = 0
    for i in range(n):
        for j in range(i + 1, n):
            dx = xs[i] - xs[j]
            dy = ys[i] - ys[j]
            if dx == 0 and dy == 0:
                ties_x += 1
                ties_y += 1
            elif dx == 0:
                ties_x += 1
            elif dy == 0:
                ties_y += 1
            elif (dx > 0) == (dy > 0):
                concordant += 1
            else:
                discordant += 1
    n_pairs = n * (n - 1) // 2
    denom_x = ((n_pairs - ties_x) * (n_pairs - ties_y)) ** 0.5
    if denom_x == 0:
        return 0.0
    return (concordant - discordant) / denom_x


def run_golden_set_eval(
    *,
    session_factory,
    fixture_path: str | None = None,
) -> GoldenSetReport:
    """Read the golden-set fixture, query CallRanking, compute Kendall's
    tau against expected scores. Logs the report and returns it.

    Reports `n_compared` separately from the curated fixture size
    (`n_fixture`) so the admin panel doesn't display "tau computed over
    50 pairs" when only 12 of those 50 had a CallRanking row.

    Fixture schema is trusted: each triple must have integer call_id,
    profile_id, and numeric expected_score. No defensive validation —
    curated by hospital admins."""
    path = Path(fixture_path) if fixture_path else _DEFAULT_FIXTURE
    if not path.exists():
        _logger.warning(
            "ranking.golden_eval.fixture_missing",
            extra={"event": "golden_fixture_missing", "path": str(path)},
        )
        return GoldenSetReport(n_fixture=0, n_compared=0, tau=None)

    triples_raw = json.loads(path.read_text())
    # Dedup on (call_id, profile_id) — keep first occurrence. A duplicated
    # triple would otherwise double-weight that pair in tau, biasing the
    # result. Log a warning so a curator notices fixture errors.
    seen: set[tuple[int, int]] = set()
    triples: list[dict] = []
    for t in triples_raw:
        key = (t["call_id"], t["profile_id"])
        if key in seen:
            _logger.warning(
                "ranking.golden_eval.duplicate_triple",
                extra={
                    "event": "golden_duplicate_triple",
                    "call_id": t["call_id"],
                    "profile_id": t["profile_id"],
                },
            )
            continue
        seen.add(key)
        triples.append(t)
    n_fixture = len(triples)

    actual: list[float] = []
    expected: list[float] = []
    with session_factory() as session:
        for t in triples:
            row = session.execute(
                select(CallRanking).where(
                    CallRanking.call_id == t["call_id"],
                    CallRanking.profile_id == t["profile_id"],
                )
            ).scalar_one_or_none()
            if row is None:
                continue
            actual.append(float(row.aggregate_score))
            expected.append(float(t["expected_score"]))

    tau = kendalls_tau(actual, expected) if len(actual) >= 2 else None

    report = GoldenSetReport(
        n_fixture=n_fixture,
        n_compared=len(actual),
        tau=tau,
    )
    _logger.info(
        "ranking.golden_eval.report",
        extra={
            "event": "golden_eval_report",
            "n_fixture": n_fixture,
            "n_compared": len(actual),
            "tau": tau,
        },
    )
    _persist_report(report)
    return report


def _persist_report(report: GoldenSetReport) -> None:
    """Write the latest report to disk for /admin to read on each load.

    Overwrite-in-place: only the most recent run is kept. The weekly job
    bumps this; the admin route never blocks on re-running the eval."""
    _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REPORT_PATH.write_text(
        json.dumps(
            {
                "n_fixture": report.n_fixture,
                "n_compared": report.n_compared,
                "tau": report.tau,
            }
        )
    )


def load_last_report() -> GoldenSetReport | None:
    """Read the most recent persisted report, or None if never run.

    Defensive: if the sidecar file is missing OR corrupted/incomplete,
    return None and log a warning. /admin must always render — it's the
    operator's recovery surface."""
    if not _REPORT_PATH.exists():
        return None
    try:
        data = json.loads(_REPORT_PATH.read_text())
        return GoldenSetReport(
            n_fixture=data.get("n_fixture", 0),
            n_compared=data.get("n_compared", 0),
            tau=data.get("tau"),
        )
    except (OSError, json.JSONDecodeError, TypeError) as e:
        _logger.warning(
            "ranking.golden_eval.load_failed",
            extra={
                "event": "golden_load_failed",
                "path": str(_REPORT_PATH),
                "error": str(e),
            },
        )
        return None
