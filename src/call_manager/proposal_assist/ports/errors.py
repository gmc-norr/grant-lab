"""The error taxonomy a tool is allowed to catch — stdlib only.

WorkspaceGenerationStale lived in fencing.py through F1-F3a. It moves here
because a tool must be able to NAME it, and fencing.py imports sqlalchemy and
db.models. fencing.py re-exports it, so every pre-F3b import site is unchanged.
"""

from __future__ import annotations

import uuid


class WorkspaceGenerationStale(RuntimeError):
    """The workspace has moved past the generation the caller carried.

    NOT an error condition. It means "your epoch is over; produce no further
    semantic effect." It never reaches a user as a 5xx.

    It does NOT mean "write nothing". An actor that catches this must still
    record cleanup and accounting — tokens, dollars, terminal status, releasing
    an active-unique slot — via conditional writes only (spec 7). Abandoning
    silently strands the job holding its slot and destroys deliberate late token
    accounting (see review/pass_runner.py, which keeps the tokens a cancelled
    run's sub-call spent on purpose).
    """

    def __init__(
        self, workspace_id: uuid.UUID, *, expected: int, actual: int | None
    ) -> None:
        super().__init__(
            f"workspace {workspace_id} moved from generation {expected} to {actual}"
        )
        self.workspace_id = workspace_id
        self.expected = expected
        self.actual = actual


class EffectRejected(RuntimeError):
    """The effect was refused for a reason that is NOT staleness — a wrong or
    finished target, or a second submission where the contract is exactly-once.

    `reason` is the exact string the tool returns to the agent, so the messages
    cannot drift when F3d converts the call sites.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnfencedInvocation(RuntimeError):
    """A tool invocation reached an effect without a claimed generation.

    Never expected in production: F3a gives all five actors one. Refusing beats
    defaulting, which would submit an effect against a generation nobody claimed.
    """

    def __init__(self, workspace_id: uuid.UUID) -> None:
        super().__init__(f"invocation for workspace {workspace_id} carries no generation")
        self.workspace_id = workspace_id
