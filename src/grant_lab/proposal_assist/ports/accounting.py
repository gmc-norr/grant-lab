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


@runtime_checkable
class Accounting(Protocol):
    def reserve_fetch_slot(self, active_turn_job_id: uuid.UUID) -> int | None:
        """Atomic cap-check + increment. Returns the NEW count, or None when the
        turn is not running or the cap is already reached."""
        ...

    def refund_fetch_slot(self, active_turn_job_id: uuid.UUID) -> None:
        """Return a slot that was reserved but never spent on usable content."""
        ...
