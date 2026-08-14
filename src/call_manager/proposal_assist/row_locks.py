"""SET LOCAL lock_timeout over an ENTIRE transaction — the one mechanism this
codebase uses to keep a transaction that holds the `workspaces` row from pinning
it against a concurrent DELETE.

Why this has to exist at all: PostgreSQL's `lock_timeout` defaults to 0
(disabled) and `db/session.py` passes no `connect_args`, so a plain row-lock wait
is unbounded. A DELETE that has already won the per-workspace commit mutex
executes `SET LOCAL lock_timeout = DEFAULT` (drafting/locking.py) before it
stamps `soft_deleted_at`, so there is no bound on the delete's side either: an
unbounded wait ANYWHERE inside a transaction that holds the workspace row blocks
the owner's DELETE for exactly that long, while the DELETE holds the advisory
mutex and every other mutex-participating writer for that workspace 429s behind
it — and the route's asserted budget (< 6.0s) is blown without limit.

The implementation is the idiom `acquire_commit_mutex` uses (SET LOCAL plus the
55P03 → typed-exception translation), minus its restore-to-DEFAULT: the callers
here hold rows from their first acquisition until COMMIT, and their DML, flush
and commit can each block on a lock, so restoring DEFAULT after the acquisitions
would leave exactly the stretch that matters unbounded. Nothing needs restoring —
SET LOCAL is scoped to the transaction, so a commit, a rollback or the implicit
abort a timeout causes all clear it.

WHAT THE BOUND DELIVERS, AND WHAT IT DOES NOT
---------------------------------------------
`lock_timeout` is a cap on a SINGLE lock acquisition, not a deadline on the
transaction. Every statement that waits gets its own fresh allowance, so a
transaction contending for N distinct rows can stall up to N × the constant
before it reaches COMMIT and releases what it holds.

The guarantee here is therefore exactly: **no wait inside a bounded region is
unbounded** — a wedged holder can no longer pin the `workspaces` row, or the
commit mutex above it, indefinitely. That is what these constants were added for
and it is worth having. What they are NOT is a total budget: the three values
below do not compose into "a DELETE waits at most X". The worst case is per
region, and each constant records its own stacking count.

The gap is deliberate and recorded as a residual (§10 of
docs/superpowers/specs/2026-08-11-workspace-retirement-slice-c-design.md). A real
transaction-wide deadline — a monotonic budget threaded through each region,
recomputing `lock_timeout` before every acquisition — is a design change past
this slice, not a constant tweak.

The values themselves are pinned, with their derivation, by
tests/proposal_assist/test_row_lock_ceilings.py. Every contention test shortens
these constants by monkeypatch to stay fast, so that file is the only thing that
fails when one of them grows.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

# The four proposal-assist pass prefixes (coherence, evaluator, uncited-claims,
# review) take the workspace row FOR UPDATE and hold it across context assembly —
# which reads files out of workspaces_dir — and a job-row UPDATE, then commit.
# They hold no commit mutex, so nothing else bounds them.
#
# Why 2 seconds: a PER-ACQUISITION judgement call, not a figure derived from the
# DELETE's budget — see the module docstring. A prefix contends for TWO rows (the
# `workspaces` row FOR UPDATE, then its own job row), so its worst case is
# 2 × 2s = 4s, and that is what a DELETE can spend waiting on the `workspaces`
# row behind it — past the commit mutex the DELETE runs with lock_timeout back at
# DEFAULT (drafting/locking.py restores it), so this constant is the ONLY thing
# bounding that wait. 2s keeps that worst case a fraction of the DELETE route's
# asserted < 6.0s rather than a multiple of it; it does not guarantee the budget,
# because the route's own transaction, retirement barrier and scrub also have to
# fit inside it. Losing a race to contention is cheap on this path: the prefix has
# already claimed a job row that stays 'running', and the per-pass reaper
# reclaims it — a requeue, not a dead letter.
#
# phases/setup.py's _ROW_LOCK_TIMEOUT_SECONDS carries the same value for the same
# kind of reason, but NOT the same worst case: its persist transactions contend
# for three rows (job, workspace, Call), so their stack is 3 × 2s.
PASS_PREFIX_LOCK_TIMEOUT_SECONDS = 2

# The two setup-job routes (activate, retry_setup) take the commit mutex and then
# the workspace's WorkspaceSetupJob row. acquire_commit_mutex restores
# lock_timeout = DEFAULT on success, so that row wait is otherwise unbounded —
# and it is taken while HOLDING the mutex, so an unbounded wait there blocks the
# owner's DELETE for exactly that long and 429s every other mutex-participating
# writer behind it.
#
# Why 2 seconds: the same per-acquisition judgement as
# PASS_PREFIX_LOCK_TIMEOUT_SECONDS above, with a different stack. `activate`
# contends for TWO rows under this bound — the setup-job row FOR UPDATE, then the
# `workspaces` row it re-reads FOR UPDATE — so its worst case is 2 × 2s = 4s of
# mutex pin (`retry_setup` contends for one row: 2s). A DELETE queued behind that
# waits ~4s before it starts its own work, and still wins the mutex: the DELETE's
# acquisition timeout is 10s (soft_delete_workspace_db). THAT is the hard ceiling
# this value has to respect — raised far enough, the DELETE times out on the
# mutex and returns 429 having deleted nothing at all.
#
# Losing a race is cheap here: the route has mutated nothing and the caller gets
# the house 429 with Retry-After, which is the same answer a wedged mutex already
# gives them.
SETUP_JOB_LOCK_TIMEOUT_SECONDS = 2

# The four owner-write routes that take the commit mutex and then write the
# `workspaces` row ITSELF (settings.py's update_settings / add_member /
# remove_member, rubric.py's update_rubric). Deliberately NOT
# SETUP_JOB_LOCK_TIMEOUT_SECONDS: these routes never touch a WorkspaceSetupJob
# row — update_settings and _apply_rubric UPDATE the workspace row directly, and
# the member routes' WorkspaceViewer/WorkspaceEvent writes take FOR KEY SHARE on
# it as their FK parent. Same value, different thing being waited on, so it gets
# its own name rather than borrowing one whose comment describes a job row.
#
# As with the setup-job routes, acquire_commit_mutex restores
# lock_timeout = DEFAULT on success (drafting/locking.py), so this wait is
# otherwise unbounded — and it is taken while HOLDING the mutex. A real holder
# exists: the four pass prefixes take the workspace row FOR UPDATE holding NO
# mutex and keep it across context assembly's filesystem reads (see
# PASS_PREFIX_LOCK_TIMEOUT_SECONDS above).
#
# Why 2 seconds: the same per-acquisition judgement as the two constants above.
# The stack here is the shortest of the three — ONE contended row, the
# `workspaces` row itself (the re-read, the UPDATE and the member routes'
# FOR KEY SHARE all land on it) — so the worst case is 2s of mutex pin, inside
# both the DELETE's 10s mutex-acquisition timeout and a fraction of its asserted
# < 6.0s. Losing a race is cheap on this path: the route has mutated nothing (the
# transaction aborts and rolls back) and the caller gets the house 429 with
# Retry-After, which is the same answer a wedged mutex already gives them.
#
# The `users` rows these routes also touch — FOR KEY SHARE, taken by Postgres to
# validate WorkspaceViewer.user_id and WorkspaceEvent.actor_id — are NOT in that
# count, and the exclusion answers exactly one question: "can a holder pin one
# long enough to add another whole cap to the worst case?" No. The only writer
# that locks `users` rows and then waits is web/api/admin_users.py's
# `_lock_active_admin_count` (FOR UPDATE over every active admin row, on the
# disable-last-admin and demote-last-admin paths), and it is a lock-and-count
# transaction with no slow work in it — `signal_user_revocation` and its 5s
# barrier run outside that session. Nothing holds a `users` row the way the pass
# prefixes hold the `workspaces` row, across context assembly's file reads.
#
# It does NOT answer the other question, "can a 55P03 fire on a `users` row at
# all?" — it can, and it is the wait that matters most here. That FK check is the
# LAST thing these transactions do, at COMMIT, so it is what keeps the
# bound-through-COMMIT rule load-bearing for update_rubric and activate now that
# they take the `workspaces` row at the top of their bound; untranslated it is a
# 500 (tests/proposal_assist/test_api_writers_mutex_straddle.py's two
# `..._commit_time_users_fk_wait_to_429_not_500` cases reproduce it; spec §8).
# Short-but-real is precisely why the row is out of the ARITHMETIC and inside the
# BOUND. If a `users` row ever comes to be held across slow work — moving that 5s
# barrier inside the admin guard's session would do it — the row count above is
# what has to change with it.
WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS = 2


class RowLockTimeout(RuntimeError):
    """A transaction's SET LOCAL lock_timeout fired while waiting on a lock.

    Deliberately typed: lock contention says nothing about whether the workspace
    is gone, so callers must not route it to a 'cancelled'/'deleted' branch.
    """


class PassPrefixLockTimeout(RowLockTimeout):
    """Raised out of a pass prefix that could not take the workspace row within
    PASS_PREFIX_LOCK_TIMEOUT_SECONDS.

    It propagates: the prefix sits outside each runner's failure guard by design
    (the runner's docstrings say so — "a DB/file error there can propagate"), the
    scheduler's `_drain_pass_future` logs it, and the job is left 'running' for
    that pass's reaper. Giving up is the point — blocking here would pin the
    workspace row against the owner's DELETE.
    """


@contextmanager
def bounded_lock_waits(
    session: Session,
    *,
    timeout_seconds: float,
    error: type[RowLockTimeout] = RowLockTimeout,
    message: str = "timed out waiting for a row lock",
) -> Iterator[None]:
    """Bound EVERY lock wait in the enclosing transaction, not just the
    `with_for_update` acquisitions — see the module docstring for why the bound
    must extend past the acquisitions to the commit.

    *timeout_seconds* caps each acquisition INDIVIDUALLY (that is what
    `lock_timeout` means); it is not a deadline on the region. A body that
    contends for N rows can stall up to N × *timeout_seconds* before it commits.
    """
    timeout_ms = int(timeout_seconds * 1000)
    session.execute(text(f"SET LOCAL lock_timeout = '{timeout_ms}ms'"))
    try:
        yield
    except OperationalError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        if sqlstate == "55P03":
            raise error(message) from exc
        raise


@contextmanager
def bounded_prefix_lock_waits(session: Session) -> Iterator[None]:
    """The pass-prefix preset of `bounded_lock_waits`.

    Reads PASS_PREFIX_LOCK_TIMEOUT_SECONDS as a module global at call time so a
    test can shorten it with monkeypatch.
    """
    with bounded_lock_waits(
        session,
        timeout_seconds=PASS_PREFIX_LOCK_TIMEOUT_SECONDS,
        error=PassPrefixLockTimeout,
        message=(
            "timed out waiting for the workspace row — giving up rather than "
            "pinning it against a concurrent delete"
        ),
    ):
        yield
