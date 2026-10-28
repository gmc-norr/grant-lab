"""Accounting commands — owner-bound and DELIBERATELY unfenced. Stdlib only.

Spec 3.3. Freeing an active-unique slot and refunding an admission slot must
happen AFTER the workspace has moved on, so these operations carry no generation
predicate on purpose. Fencing them would strand jobs holding their slots.

They are still atomic and still owned: each is ONE conditional statement bound to
the turn job that originated it, never "whichever turn is running for this
workspace". A read-modify-write (`job.fetch_count += 1`) is prohibited here as
everywhere — that defect shipped twice (28abbc4, 701fbb7).
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

#: Per-turn reference-fetch budget. Lives in the tool-facing contract so the
#: tool can name it in the message the agent sees without importing anything
#: that owns a session.
MAX_FETCHES_PER_TURN = 3

#: Per-turn budget of outbound fetch ATTEMPTS, failures included. Separate from
#: MAX_FETCHES_PER_TURN because that one is refunded on every failure and HTTP
#: 404 is a failure, so it bounds nothing an attacker cares about: the review of
#: 2026-10-26 (M-PA-3) got 8 outbound GETs — a chunk of the draft in each query
#: string — with fetch_count still at 2. This counter is NOT refunded, which is
#: the whole point, so it sits two above the success cap to leave a legitimate
#: turn room to retry a flaky URL. Enforced in memory on the ToolContext (there
#: is deliberately no DB column); see tools/references._claim_fetch_attempt.
#:
#: What it bounds, exactly: at most this many ATTACKER-CHOSEN URLs per turn —
#: not outbound requests. One attempt can make several requests of its own
#: (fetch_crossref_doi = the Crossref API then the publisher PDF; fetch_arxiv =
#: an Atom query then the PDF), plus redirect hops. Those extra hops are
#: derived by the resolver from the one input, so they carry nothing the agent
#: chose, and it is the count of agent-chosen destinations that bounds
#: exfiltration.
MAX_FETCH_ATTEMPTS_PER_TURN = MAX_FETCHES_PER_TURN + 2


@runtime_checkable
class Accounting(Protocol):
    def reserve_fetch_slot(self, active_turn_job_id: uuid.UUID) -> int | None:
        """Atomic cap-check + increment. Returns the NEW count, or None when the
        turn is not running or the cap is already reached."""
        ...

    def refund_fetch_slot(self, active_turn_job_id: uuid.UUID) -> None:
        """Return a slot that was reserved but never spent on usable content."""
        ...
