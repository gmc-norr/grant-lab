"""Golden-set evaluation — Kendall's tau between CallRanking scores and
human-curated expected scores, computed per scheme.

Read-only. Logs a structured summary; the admin panel surfaces the most
recent result (Task 7 — already wired via the v2 metrics block).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from call_monitor.db.models import CallRanking

_logger = logging.getLogger(__name__)

# From src/call_monitor/ranking/eval.py: parents[0] is `ranking/`, [1] is
# `call_monitor/`, [2] is `src/`, [3] is the repo root. Fixture lives at
# <repo>/tests/fixtures/golden_set.json — so parents[3], not [2].
_DEFAULT_FIXTURE = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "golden_set.json"
)
# Sanity check the parents[3] math — fail loudly at import time if the
# repo layout shifts and this constant points somewhere unexpected.
assert _DEFAULT_FIXTURE.parent.name == "fixtures", _DEFAULT_FIXTURE

# Path the weekly eval job writes to and /admin reads from. Computed once
# at import — env-var override only takes effect on (re-)import. Tests
# override this constant via monkeypatch.setattr rather than the env var.
_REPORT_PATH = (
    Path(os.environ.get("CALL_MONITOR_DATA_DIR", "data")) / "golden_set_last.json"
)


@dataclass(frozen=True)
class GoldenSetReport:
    """Per-scheme golden-set evaluation result.

    `n_fixture` is the size of the curated triple list. `n_v1` and `n_v2`
    are the per-scheme counts of triples that actually had a CallRanking
    row to compare against — usually less than n_fixture if some pairs
    haven't been ranked under both schemes yet. tau is computed on
    n_<scheme> rows, NOT n_fixture, so the displayed tau and the count
    must agree (Codex M2)."""
    n_fixture: int
    n_v1: int
    tau_v1: float | None
    n_v2: int
    tau_v2: float | None


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
    """Read the golden-set fixture, query CallRanking under both schemes,
    compute Kendall's tau against expected scores. Logs the report and
    returns it.

    Reports per-scheme `n_v1` / `n_v2` separately from the curated fixture
    size (`n_fixture`) so the admin panel doesn't display "tau computed
    over 50 pairs" when only 12 of those 50 had a CallRanking under v2.

    Fixture schema is trusted: each triple must have integer call_id,
    profile_id, and numeric expected_score. No defensive validation —
    curated by hospital admins."""
    path = Path(fixture_path) if fixture_path else _DEFAULT_FIXTURE
    if not path.exists():
        _logger.warning(
            "ranking.golden_eval.fixture_missing",
            extra={"event": "golden_fixture_missing", "path": str(path)},
        )
        return GoldenSetReport(
            n_fixture=0, n_v1=0, tau_v1=None, n_v2=0, tau_v2=None,
        )

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

    def _scores_for(scheme: str) -> tuple[list[float], list[float]]:
        """Returns the (actual, expected) lists for triples that have a
        CallRanking row under this scheme. May be empty."""
        actual: list[float] = []
        expected: list[float] = []
        with session_factory() as session:
            for t in triples:
                row = session.execute(
                    select(CallRanking).where(
                        CallRanking.call_id == t["call_id"],
                        CallRanking.profile_id == t["profile_id"],
                        CallRanking.scheme == scheme,
                    )
                ).scalar_one_or_none()
                if row is None:
                    continue
                actual.append(float(row.aggregate_score))
                expected.append(float(t["expected_score"]))
        return actual, expected

    v1_actual, v1_expected = _scores_for("v1")
    v2_actual, v2_expected = _scores_for("v2")
    tau_v1 = kendalls_tau(v1_actual, v1_expected) if len(v1_actual) >= 2 else None
    tau_v2 = kendalls_tau(v2_actual, v2_expected) if len(v2_actual) >= 2 else None

    report = GoldenSetReport(
        n_fixture=n_fixture,
        n_v1=len(v1_actual), tau_v1=tau_v1,
        n_v2=len(v2_actual), tau_v2=tau_v2,
    )
    _logger.info(
        "ranking.golden_eval.report",
        extra={
            "event": "golden_eval_report",
            "n_fixture": n_fixture,
            "n_v1": len(v1_actual), "tau_v1": tau_v1,
            "n_v2": len(v2_actual), "tau_v2": tau_v2,
        },
    )
    _persist_report(report)
    return report


def _persist_report(report: GoldenSetReport) -> None:
    """Write the latest report to disk for /admin to read on each load.

    Overwrite-in-place: only the most recent run is kept. The weekly job
    bumps this; the admin route never blocks on re-running the eval."""
    _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REPORT_PATH.write_text(json.dumps({
        "n_fixture": report.n_fixture,
        "n_v1": report.n_v1,
        "tau_v1": report.tau_v1,
        "n_v2": report.n_v2,
        "tau_v2": report.tau_v2,
    }))


def load_last_report() -> GoldenSetReport | None:
    """Read the most recent persisted report, or None if never run."""
    if not _REPORT_PATH.exists():
        return None
    data = json.loads(_REPORT_PATH.read_text())
    return GoldenSetReport(
        n_fixture=data.get("n_fixture", 0),
        n_v1=data.get("n_v1", 0),
        tau_v1=data.get("tau_v1"),
        n_v2=data.get("n_v2", 0),
        tau_v2=data.get("tau_v2"),
    )
