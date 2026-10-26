"""Every `RankingJobStatus` member must be classified terminal or not terminal.

`web/dashboard.py::_TERMINAL_JOB_STATUSES` is a hand-written tuple --
`(DONE, FAILED)` -- and it decides which rows `latest_failed_ranking_attempts`
is even allowed to call "the latest attempt". That marker is the sentence
"Couldn't rank this call with the local model (last tried <date>)", shown beside
a score on four surfaces (review 2026-10-26 A4 finding 2).

THE FAILURE THIS CATCHES. `RankingJobStatus` has four members today and
DONE/FAILED is the right terminal pair, so nothing is wrong now. But the tuple
is a LITERAL with no link to the enum: a fifth member -- say a `cancelled` or a
`gave_up`, both of which already exist on sibling status vocabularies in
`docs/ARCHITECTURE.md` -- would be silently excluded. If that member were
terminal, every pair whose last attempt landed on it would fall out of the
window entirely, the "latest terminal attempt" would resolve to some OLDER row,
and the marker would go stale or vanish while reading as current. No existing
test reddens on that: the enum is only ever consumed member-by-member, and the
dashboard's own tests seed the statuses they name.

THE SHAPE: A PARTITION, NOT A COPY OF THE TUPLE.

An equality pin on the tuple (`== (DONE, FAILED)`) catches a change to the
tuple and NOT a change to the enum, which is the direction that actually goes
unnoticed -- nobody editing `db/models/ranking.py` is thinking about a read
model in `web/`. So the assertion is that the SHIPPED tuple and ONE
hand-maintained set below together cover the enum exactly, with no overlap:

    set(_TERMINAL_JOB_STATUSES) | _DECIDED_NOT_TERMINAL == set(RankingJobStatus)
    set(_TERMINAL_JOB_STATUSES) & _DECIDED_NOT_TERMINAL == set()

One assertion, three directions, and only one literal in this file:

  * a FIFTH MEMBER lands in neither bucket               -> red, named
  * a member REMOVED from the tuple (DONE, say)          -> red, in neither
  * a member ADDED to the tuple that is not terminal     -> red, in both

and the terminal half is READ FROM THE LIVE SOURCE rather than copied here, so
this file cannot drift into agreeing with itself.

WHAT WOULD TRIVIALLY PASS, and is therefore not what is asserted:

  * `set(_TERMINAL_JOB_STATUSES) <= set(RankingJobStatus)`. A subset check is
    green on an EMPTY tuple, which is the state in which the marker never
    renders at all.
  * `len(RankingJobStatus) == 4`. A rename (`DONE` -> `COMPLETED`) keeps the
    count and breaks the tuple, and the failure message says nothing about
    which member moved.
  * writing the full four-member vocabulary out here. Then the tuple is never
    read, and deleting `_TERMINAL_JOB_STATUSES` from `dashboard.py` outright
    would not redden this file.

`test_the_partition_rule_rejects_an_unclassified_member` below runs the real
rule against a doctored five-member vocabulary and requires a RED, so the rule
cannot quietly start accepting everything.
"""

from __future__ import annotations

import enum

from grant_lab.db.models import RankingJobStatus
from grant_lab.web.dashboard import _TERMINAL_JOB_STATUSES

# THE ONLY LITERAL IN THIS FILE: the members deliberately judged NOT terminal,
# each with the reason it is excluded from `latest_failed_ranking_attempts`.
#
#   PENDING -- an attempt that has not run, or one `mark_failed(terminal=False)`
#              put back in the queue after a retryable error. Counting it would
#              make the marker blink with the 04:00 reconcile instead of with
#              the facts (see the helper's docstring).
#   RUNNING -- in flight. There is no outcome to report yet.
#
# A fifth member must be added HERE or to `_TERMINAL_JOB_STATUSES` in
# `web/dashboard.py`, and the choice is a decision about whether the marker
# should speak for it.
_DECIDED_NOT_TERMINAL = frozenset({RankingJobStatus.PENDING, RankingJobStatus.RUNNING})


def _unclassified(vocabulary: set, terminal: set, not_terminal: set) -> tuple[set, set]:
    """(members in neither bucket, members in both) for one vocabulary.

    The real rule, factored out so the positive control below can run it against
    a doctored vocabulary rather than re-implementing it.
    """
    return (vocabulary - terminal - not_terminal, terminal & not_terminal)


def test_every_ranking_job_status_is_classified_terminal_or_not():
    terminal = set(_TERMINAL_JOB_STATUSES)
    missing, both = _unclassified(set(RankingJobStatus), terminal, set(_DECIDED_NOT_TERMINAL))
    assert missing == set(), (
        f"RankingJobStatus member(s) {sorted(m.value for m in missing)} are classified "
        "neither terminal nor non-terminal. `web/dashboard.py::_TERMINAL_JOB_STATUSES` "
        "decides which rows latest_failed_ranking_attempts treats as 'the latest "
        "attempt', and the \"Couldn't rank this call\" marker goes stale for a terminal "
        "status it does not list. Decide: add it to that tuple, or to "
        "_DECIDED_NOT_TERMINAL here."
    )
    assert both == set(), (
        f"RankingJobStatus member(s) {sorted(m.value for m in both)} are listed as BOTH "
        "terminal and non-terminal"
    )


def test_the_terminal_tuple_is_read_from_the_dashboard_and_is_not_empty():
    """The tuple above is the live one, and it still has content.

    `_unclassified` is satisfied by an EMPTY terminal set as long as
    `_DECIDED_NOT_TERMINAL` grows to cover the enum, and an empty tuple is
    exactly the state in which the marker never renders. Also pins that the
    tuple carries no duplicates, since `.in_()` over a duplicated member is
    silently fine in SQL and hides a copy-paste.
    """
    assert len(_TERMINAL_JOB_STATUSES) > 0
    assert len(set(_TERMINAL_JOB_STATUSES)) == len(_TERMINAL_JOB_STATUSES)
    assert all(isinstance(m, RankingJobStatus) for m in _TERMINAL_JOB_STATUSES)


def test_the_partition_rule_rejects_an_unclassified_member():
    """POSITIVE CONTROL: the rule is red on a vocabulary it should reject.

    A five-member enum whose extra member appears in neither bucket -- the exact
    shape a future `cancelled` would arrive in. If this test ever passes with
    `missing == set()`, `_unclassified` has stopped being able to fail and the
    gate above is hollow.
    """

    class _FiveMemberStatus(enum.StrEnum):
        PENDING = "pending"
        RUNNING = "running"
        FAILED = "failed"
        DONE = "done"
        CANCELLED = "cancelled"

    missing, both = _unclassified(
        set(_FiveMemberStatus),
        {_FiveMemberStatus.DONE, _FiveMemberStatus.FAILED},
        {_FiveMemberStatus.PENDING, _FiveMemberStatus.RUNNING},
    )
    assert missing == {_FiveMemberStatus.CANCELLED}
    assert both == set()
    # ...and the overlap half is red too, so neither return value is dead code.
    _, both_overlap = _unclassified(
        set(_FiveMemberStatus),
        {_FiveMemberStatus.DONE, _FiveMemberStatus.FAILED, _FiveMemberStatus.PENDING},
        {_FiveMemberStatus.PENDING, _FiveMemberStatus.RUNNING, _FiveMemberStatus.CANCELLED},
    )
    assert both_overlap == {_FiveMemberStatus.PENDING}
