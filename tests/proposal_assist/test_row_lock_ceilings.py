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
   enough that the barrier alone can exhaust the budget). So the stall a cap may
   contribute has to be a FRACTION of the budget, not the budget.
   `BUDGET_FRACTION_FOR_LOCK_STALLS` fixes that fraction at two thirds,
   leaving a third for the route's own work. The fraction is arguable; the
   direction is not — raise a cap and the DELETE's worst case grows in lockstep.

   `PASS_PREFIX_LOCK_TIMEOUT_SECONDS` is bound by ceiling 2 ONLY. The four pass
   prefixes hold NO mutex, so an over-long prefix cannot deny the DELETE the
   mutex; what it does is make the DELETE wait on the `workspaces` row for as
   long as the prefix's own cap allows. Since Slice C3 that wait is itself capped
   at 2s past the mutex, so the DELETE gives up rather than hanging — but giving
   up is a 429 with nothing deleted, which is why the prefix's cap still governs
   whether the owner's DELETE succeeds.

MAX_CONTENDED_ROWS = 2 — derived for the row_locks families, a backstop for the rest
-----------------------------------------------------------------------------------
It is one number, not a per-family table, and every cap below is multiplied by
it. For the three families it was derived from it reads as "no bounded
transaction here contends for more than two rows"; for the mutex-tail family it
is a generic backstop that UNDER-counts (see below). Derived per family from the
code:

* PASS_PREFIX: TWO — the `workspaces` row `FOR UPDATE` plus that pass's own job
  row (e.g. `coherence/pass_runner.py`), held to COMMIT.
* SETUP_JOB: TWO — `rubric.py`'s `activate` takes the `WorkspaceSetupJob` row
  `FOR UPDATE` and then re-reads the `workspaces` row `FOR UPDATE`.
  (`retry_setup` contends for one.)
* WORKSPACE_ROW: ONE — the `workspaces` row itself (the re-read, the UPDATE and
  the member routes' `FOR KEY SHARE` all land on it). Judged at the family
  maximum of two anyway, so this cap carries a deliberate 2x margin rather than
  a tight derivation. That is conservative in the safe direction: the ceilings
  hold a cap to a SMALLER value than its own stack requires. Do not "correct" it
  down to one — that weakens the assertion for the family this slice added.
* MUTEX_TAIL: MORE THAN TWO, and the number is not derived. Read the paragraph
  below before trusting `MAX_CONTENDED_ROWS x MUTEX_TAIL_LOCK_TIMEOUT_SECONDS`
  as this family's worst case — it is a floor, not a bound.

Where the multiplier UNDER-counts: the mutex tail
-------------------------------------------------
`MUTEX_TAIL_LOCK_TIMEOUT_SECONDS` (Slice C3) is the cap left in force for the
REST of a mutex-held transaction, so its "family" is not one call site with a
countable lock stack — it is every row every mutex-held writer touches after it
wins the mutex. Two is a generic backstop reused from the `row_locks` families
above; it was never derived for this one, and it does not hold.

The DELETE route is the clearest counter-example. `soft_delete_workspace_db`
runs, inside ONE mutex-held transaction: the `workspaces` UPDATE, then
`cancel_coherence_jobs_for_workspace`, `cancel_evaluator_jobs_for_workspace`,
`cancel_review_runs_and_items_for_workspace`,
`cancel_uncited_claims_jobs_for_workspace`, and a `turn_jobs` cancel — each an
`UPDATE ... WHERE status IN (...)` taking those rows' locks, plus the
`WorkspaceEvent(actor_id=...)` insert each `record_terminal_outcome` call makes
(`turns/outcomes.py`), taking a FK parent lock on `users`. Six-plus distinct row
families, each carrying its own 2s per-acquisition cap, so the true worst case
is 12s+ and not the 4.0s the arithmetic below computes.
`post_lock_spec`/`post_unlock_spec` reach three; `kernel_accept_prose` reaches
four to five.

This is accepted, not overlooked. Every one of those waits was UNBOUNDED before
Slice C3: a single wedged writer pinned the mutex-held transaction forever, and
with it every other writer for that workspace. 12s+ of worst-case stall is
strictly better arithmetic than no bound at all. What changed on this file is
its COMPLETENESS, not its truth — the ceiling it enforces is still a real
ceiling on the cap, it simply stopped covering a family's whole stack the moment
it began governing that family.

The real fix is the one already named under "Not proven here" below: a
transaction-wide deadline, so a mutex-held transaction has ONE budget instead of
N independent per-acquisition caps. That is a design change, out of scope here.
Do NOT close the gap by lowering `MAX_CONTENDED_ROWS` or the tail cap — the caps
are already the shortest thing that lets ordinary contention succeed, and
shrinking them buys a tighter number on paper at the price of routine writes
failing more often.

The member routes' inserts also take `FOR KEY SHARE` on their `users` FK parents
(`WorkspaceViewer.user_id`), and the rubric routes' `WorkspaceEvent.actor_id`
does the same. Those are not counted — but read the exclusion at its exact
strength, because `row_locks.py` and spec section 8 say something
opposite-sounding about the same lock and both are true of different properties.

THIS file's question is "how LONG can a stall be?", and for a `users` row the
answer is: not long enough to add another whole cap. The only writer that locks
one and then waits is `web/api/admin_users.py`'s `_lock_active_admin_count`
(`FOR UPDATE` over every active admin row, on the disable-last-admin and
demote-last-admin paths), and that is a lock-and-count transaction with no slow
work inside it — `signal_user_revocation` and its 5s barrier run outside the
session. Every other users-row writer (`web/api/auth.py` login/logout,
`web/api/me.py` name/password, the rest of `admin_users.py`) takes its lock
implicitly when its UPDATE flushes and releases it at COMMIT a moment later, with
the argon2 hashing before the flush and the revocation barrier after the session
block, so neither sits under the lock. Nothing holds a `users` row the way the
pass prefixes hold the `workspaces` row.

The OTHER question — "can a 55P03 fire there at all?" — has the opposite answer,
and it is not this file's business. It can: that FK check is the last wait
`update_rubric` and `activate` make, at COMMIT, and untranslated it is a 500.
`test_api_writers_mutex_straddle.py`'s two
`..._commit_time_users_fk_wait_to_429_not_500` cases reproduce exactly that. A
short wait is still a wait — which is why the row sits outside this arithmetic
and inside the bound-through-COMMIT rule (spec section 8, gate 16).

If a `users` row ever comes to be held across slow work — moving that 5s barrier
inside the admin guard's session would do it — this number is what has to change
with it.

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

import importlib
import inspect
import re
from pathlib import Path

from grant_lab.proposal_assist import retirement, row_locks
from grant_lab.proposal_assist.drafting import locking
from grant_lab.workspaces import soft_delete_workspace_db

# The LARGEST number of CONTENDED rows any single bounded transaction in these
# families acquires — the multiplier that turns a per-lock cap into that
# transaction's worst-case stall. Applied uniformly to every cap, so a family
# with a shorter stack (WORKSPACE_ROW: one row) is held to a stricter bound than
# its own derivation demands. Per-family derivation in the module docstring.
MAX_CONTENDED_ROWS = 2

# Share of the DELETE route's asserted budget that lock stalls may consume; the
# remainder is the route's own transaction and retirement barrier.
BUDGET_FRACTION_FOR_LOCK_STALLS = 2 / 3

_BUDGET_TEST = Path(__file__).with_name("test_phase0_transaction_boundaries.py")
_BUDGET_RE = re.compile(r"assert elapsed < ([0-9]+(?:\.[0-9]+)?)")

# The caps this file governs, with the row family each one waits on. Imported,
# never restated: a second copy of the numbers is exactly the drift this test is
# supposed to catch.
_MUTEX_HELD_CAPS = {
    "SETUP_JOB_LOCK_TIMEOUT_SECONDS": row_locks.SETUP_JOB_LOCK_TIMEOUT_SECONDS,
    "WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS": row_locks.WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS,
    "MUTEX_TAIL_LOCK_TIMEOUT_SECONDS": locking.MUTEX_TAIL_LOCK_TIMEOUT_SECONDS,
}
_ALL_CAPS = {
    **_MUTEX_HELD_CAPS,
    "PASS_PREFIX_LOCK_TIMEOUT_SECONDS": row_locks.PASS_PREFIX_LOCK_TIMEOUT_SECONDS,
}

# Deliberately NOT in _MUTEX_HELD_CAPS. Both entries are subprocess bounds on git
# commands that run while the commit mutex is held, so they ARE mutex-held time —
# but neither is a LOCK cap, and neither satisfies this file's ceilings: 2 x 30
# exceeds both the 10s acquisition cap and the 6.0s DELETE budget. Slice C3 accepted
# that (see gitio.py's own comment) because the alternative was unbounded. Listed
# here so each exclusion is a recorded decision rather than an oversight — if you are
# adding a new mutex-held cap, this is the file that governs it.
#
# Dotted target -> (the value the exclusion was reasoned about, why it is excluded).
# The value is kept HERE rather than in the test body so a second entry cannot be
# added without stating what it is worth.
_EXCLUDED_FROM_CEILINGS = {
    "grant_lab.proposal_assist.citations.gitio._GIT_TIMEOUT_SECONDS": (
        30.0,
        "subprocess time, not a lock cap; see gitio.py",
    ),
    # setup.py's own copy, with identical exposure: _m1 takes the commit mutex
    # (setup.py:363) and then runs up to four _run_git calls inside _init_repo while
    # holding both that mutex and the `workspaces` row FOR UPDATE.
    "grant_lab.proposal_assist.phases.setup._GIT_TIMEOUT_SECONDS": (
        30.0,
        "M1's git subprocess time, not a lock cap; mutex-held, same trade as gitio",
    ),
}


def test_the_ceilings_exclusion_list_is_honest():
    """An exclusion that names a constant which no longer exists is worse than no
    exclusion at all — it reads as considered when it is stale.

    Iterates `_EXCLUDED_FROM_CEILINGS` and resolves each entry by import rather than
    hardcoding one of them: a hardcoded check leaves any LATER entry unguarded, and
    lets the dict be deleted outright without a test noticing. An entry whose module
    or attribute cannot be resolved fails here, loudly, as a stale exclusion.
    """
    assert _EXCLUDED_FROM_CEILINGS, (
        "the exclusion list is empty — if every mutex-held cap is now governed by "
        "the ceilings, say so in the docstring rather than leaving a dead dict"
    )
    for dotted, (expected, why) in _EXCLUDED_FROM_CEILINGS.items():
        module_name, _, attr = dotted.rpartition(".")
        assert module_name and attr, f"exclusion key {dotted!r} is not a dotted target"
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:  # pragma: no cover - only on a stale exclusion
            raise AssertionError(
                f"exclusion {dotted!r} ({why}) names module {module_name!r}, which "
                f"no longer imports: {exc}. Re-derive whether the exclusion still "
                f"holds rather than deleting the line."
            ) from exc
        assert hasattr(module, attr), (
            f"exclusion {dotted!r} ({why}) names a constant that no longer exists. "
            f"Re-derive whether the exclusion still holds rather than deleting the "
            f"line."
        )
        assert getattr(module, attr) == expected, (
            f"exclusion {dotted!r} ({why}) moved from {expected} to "
            f"{getattr(module, attr)}; re-derive whether the exclusion still holds "
            f"rather than updating this number"
        )


def test_no_cap_is_zero_because_zero_is_postgres_for_disabled():
    """Every cap must be strictly positive, and 0 is the value that matters.

    `SET LOCAL lock_timeout = '0ms'` does not mean "give up immediately" — 0 is
    PostgreSQL's DISABLED, i.e. wait forever, which is precisely the unbounded tail
    this slice exists to remove. So a 0 is not a small number that the ceilings
    above would catch as too large; it sails past both of them (0 x 2 is under every
    budget) while restoring the exact bug. A reviewer set
    MUTEX_TAIL_LOCK_TIMEOUT_SECONDS to 0, ran both ceilings tests and the AST gate,
    and got four passes.

    `acquire_commit_mutex` guards `timeout_ms <= 0` for its PARAMETER
    (locking.py:222-224), and nothing guarded the constants. This is that guard, DB-
    free, over every cap this file knows about.
    """
    for name, cap in _ALL_CAPS.items():
        assert cap > 0, (
            f"{name}={cap}. Zero is not a tight cap: PostgreSQL reads "
            f"lock_timeout = '0ms' as DISABLED — an UNBOUNDED wait — so this value "
            f"restores the unbounded mutex-held tail Slice C3 removed while passing "
            f"every ceiling above (0 x {MAX_CONTENDED_ROWS} is under any budget). A "
            f"negative is worse still: it fails at the SET LOCAL. If you need a "
            f"shorter cap, the floor is 1."
        )


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
            f"{name}={cap}s x {MAX_CONTENDED_ROWS} (the largest contended-row "
            f"count across these families, applied to every cap) = {worst_case}s, "
            f"which is not under the {acquire_timeout}s the DELETE route spends "
            f"acquiring the same commit mutex (soft_delete_workspace_db). A writer "
            f"that slow makes the owner's DELETE time out on the mutex and return "
            f"429 having deleted nothing. Lower the cap; do not raise this ceiling."
        )


def test_every_cap_leaves_headroom_under_the_delete_route_budget():
    """Ceiling 2 (budgetary). Each cap's worst-case stall must stay inside the
    share of the DELETE route's asserted budget that lock waits may claim — the
    rest belongs to the route's own transaction and its retirement barrier.
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
            f"{name}={cap}s x {MAX_CONTENDED_ROWS} (the largest contended-row "
            f"count across these families, applied to every cap) = {worst_case}s, "
            f"over the {stall_ceiling:.1f}s that lock stalls may take out of the "
            f"DELETE route's asserted {budget}s budget "
            f"(test_phase0_transaction_boundaries.py). The route still has its own "
            f"cancel transaction and a retirement barrier bounded at "
            f"{retirement._RETIREMENT_BARRIER_SECONDS}s to fit inside that budget. "
            f"Lower the cap; do not raise this ceiling."
        )
