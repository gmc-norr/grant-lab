"""The PRODUCTION row-lock ceilings, pinned. DB-free, import-only, milliseconds.

Why this file exists
--------------------
Every row-contention gate in Slice C1/C2 monkeypatches these constants down to
milliseconds so the tests stay fast (e.g.
`tests/proposal_assist/test_api_writers_mutex_straddle.py`, which patches the
constant *as imported into the route module*). That is right for those tests and
wrong for the system: it leaves the production VALUES exercised by nothing. An
external reviewer raised both constants in `row_locks.py` from 2 to 30 and all 22
of the slice's new tests still passed. This file is the only thing that fails
when a cap grows.

What the constants actually are
-------------------------------
`row_locks.bounded_lock_waits` issues `SET LOCAL lock_timeout`. Postgres applies
that PER LOCK ACQUISITION, not per transaction: a transaction that contends for
N distinct rows can stall N x cap before it reaches COMMIT and releases what it
holds. The caps below are therefore per-lock judgement calls that prevent an
UNBOUNDED pin; they are not a transaction-wide deadline, and this file does not
claim they compose into one (see "Not proven here").

The two ceilings, in decreasing hardness
----------------------------------------
1. MECHANICAL — not a judgement call. The four mutex-held writers
   (`web/api/settings.py` x3, `web/api/rubric.py` x3) hold the per-workspace
   commit mutex across their whole bounded region. `soft_delete_workspace_db`
   acquires that SAME mutex, with the acquisition timeout read below off its own
   signature, and converts failure into `CommitMutexTimeout` -> the route's 429:
   **the owner's DELETE then deletes nothing at all.** So for the two caps taken
   while holding the mutex, `MAX_CONTENDED_ROWS x cap` must stay strictly under
   that acquisition timeout. Past it, a merely slow writer turns a delete into a
   functional failure. This is the ceiling the 30 mutation crosses: 2 x 30 = 60,
   against a 10s acquisition budget.

2. BUDGETARY — a judgement call, stated as one.
   `tests/proposal_assist/test_phase0_transaction_boundaries.py` asserts the
   DELETE route answers in under a fixed wall-clock budget while a Phase-0
   extractor holds rows (that number is read out of that file below rather than
   restated here). A lock stall is only ONE term of that route's time: it also
   runs its own cancel-everything transaction, then blocks on the retirement
   barrier (`retirement._RETIREMENT_BARRIER_SECONDS` — imported below, and large
   enough that the barrier alone can exhaust the budget), then scrubs bytes. So
   the stall a cap may contribute has to be a FRACTION of the budget, not the
   budget. `BUDGET_FRACTION_FOR_LOCK_STALLS` fixes that fraction at two thirds,
   leaving a third for the route's own work. The fraction is arguable; the
   direction is not — raise a cap and the DELETE's worst case grows in lockstep.

   `PASS_PREFIX_LOCK_TIMEOUT_SECONDS` is bound by ceiling 2 ONLY. The four pass
   prefixes hold NO mutex, so an over-long prefix cannot deny the DELETE the
   mutex; what it does is make the DELETE — which past the mutex runs with
   `lock_timeout` back at DEFAULT, because `acquire_commit_mutex` restores it —
   wait on the `workspaces` row for as long as the prefix's own cap allows.

MAX_CONTENDED_ROWS = 2, derived from the code, per family
---------------------------------------------------------
* PASS_PREFIX: the `workspaces` row `FOR UPDATE` plus that pass's own job row
  (e.g. `coherence/pass_runner.py`), held to COMMIT.
* SETUP_JOB: `rubric.py`'s `activate` takes the `WorkspaceSetupJob` row
  `FOR UPDATE` and then re-reads the `workspaces` row `FOR UPDATE` — two.
  (`retry_setup` contends for one.)
* WORKSPACE_ROW: one contended row, the `workspaces` row itself (the re-read,
  the UPDATE and the member routes' `FOR KEY SHARE` all land on it).

The member routes' inserts also take `FOR KEY SHARE` on their `users` FK
parents; those are not counted, because nothing in this system holds a `users`
row across slow work, so they cannot be a contended wait. If that ever changes,
this number is what has to change with it.

Not proven here (the P3 residual, spec section 10)
--------------------------------------------------
A per-lock cap is not a total deadline, so these ceilings bound one stall each,
not a transaction's sum. Two consequences are known and deliberately NOT
asserted, because closing them is a design change beyond this slice:

* `activate` can wait up to one cap on the setup-job row and then up to another
  on the `workspaces` row, all while holding the mutex — so a DELETE queued
  behind it can wait ~2 caps before it starts its own work.
* `phases/setup.py`'s persist transactions contend for THREE rows (job,
  workspace, call — `setup.py` `_m3`) at their own `_ROW_LOCK_TIMEOUT_SECONDS`,
  which is why that constant is deliberately not pinned by the ceilings here: at
  three rows it already exceeds the budgetary ceiling this file enforces.

If a test in this file fails, a cap was raised. Do not raise the ceiling to
match it: re-derive the row counts first, and if a cap genuinely needs to grow,
the thing that has to change is the mechanism (a real transaction-wide
deadline), not this arithmetic.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from call_manager.proposal_assist import retirement, row_locks
from call_manager.workspaces import soft_delete_workspace_db

# Maximum number of CONTENDED rows a single bounded transaction acquires — the
# multiplier that turns a per-lock cap into that transaction's worst-case stall.
# Derivation per family in the module docstring.
MAX_CONTENDED_ROWS = 2

# Share of the DELETE route's asserted budget that lock stalls may consume; the
# remainder is the route's own transaction, retirement barrier and scrub.
BUDGET_FRACTION_FOR_LOCK_STALLS = 2 / 3

_BUDGET_TEST = Path(__file__).with_name("test_phase0_transaction_boundaries.py")
_BUDGET_RE = re.compile(r"assert elapsed < ([0-9]+(?:\.[0-9]+)?)")

# The caps this file governs, with the row family each one waits on. Imported,
# never restated: a second copy of the numbers is exactly the drift this test is
# supposed to catch.
_MUTEX_HELD_CAPS = {
    "SETUP_JOB_LOCK_TIMEOUT_SECONDS": row_locks.SETUP_JOB_LOCK_TIMEOUT_SECONDS,
    "WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS": row_locks.WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS,
}
_ALL_CAPS = {
    **_MUTEX_HELD_CAPS,
    "PASS_PREFIX_LOCK_TIMEOUT_SECONDS": row_locks.PASS_PREFIX_LOCK_TIMEOUT_SECONDS,
}


def _delete_route_budget_seconds() -> float:
    """The DELETE route's asserted end-to-end budget, read out of the test that
    asserts it.

    Deliberately parsed rather than copied: a copy drifts silently the moment
    that test's number changes, and this file's whole point is that unpinned
    numbers drift. If the assertion is reworded or removed, this raises — which
    is the correct outcome, because the ceilings below lose their premise.
    """
    matches = _BUDGET_RE.findall(_BUDGET_TEST.read_text())
    assert len(matches) == 1, (
        f"expected exactly one DELETE-budget assertion in {_BUDGET_TEST.name}, "
        f"found {len(matches)}: {matches}. The ceilings in this file are derived "
        f"from that budget — re-point this parser at the right assertion."
    )
    return float(matches[0])


def test_mutex_held_caps_stay_under_the_deletes_mutex_acquisition_timeout():
    """Ceiling 1 (mechanical). A writer holding the commit mutex must not be able
    to hold it longer than the DELETE is willing to wait for it, or the owner's
    DELETE returns 429 having deleted NOTHING.
    """
    acquire_timeout = inspect.signature(soft_delete_workspace_db).parameters[
        "timeout_seconds"
    ].default
    assert isinstance(acquire_timeout, (int, float)) and acquire_timeout > 0, (
        "soft_delete_workspace_db no longer carries a numeric timeout_seconds "
        "default — this ceiling is derived from it"
    )
    for name, cap in _MUTEX_HELD_CAPS.items():
        worst_case = MAX_CONTENDED_ROWS * cap
        assert worst_case < acquire_timeout, (
            f"{name}={cap}s x {MAX_CONTENDED_ROWS} contended rows = {worst_case}s, "
            f"which is not under the {acquire_timeout}s the DELETE route spends "
            f"acquiring the same commit mutex (soft_delete_workspace_db). A writer "
            f"that slow makes the owner's DELETE time out on the mutex and return "
            f"429 having deleted nothing. Lower the cap; do not raise this ceiling."
        )


def test_every_cap_leaves_headroom_under_the_delete_route_budget():
    """Ceiling 2 (budgetary). Each cap's worst-case stall must stay inside the
    share of the DELETE route's asserted budget that lock waits may claim — the
    rest belongs to the route's own transaction, its retirement barrier and the
    scrub.
    """
    budget = _delete_route_budget_seconds()
    stall_ceiling = budget * BUDGET_FRACTION_FOR_LOCK_STALLS
    assert retirement._RETIREMENT_BARRIER_SECONDS > 0, (
        "the retirement barrier is why lock stalls get only a fraction of the "
        "budget — if it is gone, re-derive BUDGET_FRACTION_FOR_LOCK_STALLS"
    )
    for name, cap in _ALL_CAPS.items():
        worst_case = MAX_CONTENDED_ROWS * cap
        assert worst_case <= stall_ceiling, (
            f"{name}={cap}s x {MAX_CONTENDED_ROWS} contended rows = {worst_case}s, "
            f"over the {stall_ceiling:.1f}s that lock stalls may take out of the "
            f"DELETE route's asserted {budget}s budget "
            f"(test_phase0_transaction_boundaries.py). The route still has its own "
            f"cancel transaction, a retirement barrier bounded at "
            f"{retirement._RETIREMENT_BARRIER_SECONDS}s and the byte scrub to do "
            f"inside that budget. Lower the cap; do not raise this ceiling."
        )
