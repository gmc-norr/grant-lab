"""Query DTOs and the Queries contract — stdlib only.

Spec 3.1. Every method opens its own read-only session and returns a FROZEN
dataclass, never an attached ORM instance: an attached instance carries
object_session(instance), which is the full Session again.

Reads are NOT fenced (spec 5). A stale read that informs a durable write is
caught by that write's operation, which re-checks the generation inside its own
transaction. The consequence is stated rather than engineered around: a tool can
read a workspace deleted microseconds later. The agent's WRITE will fail.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ReferenceView:
    id: uuid.UUID
    workspace_id: uuid.UUID
    citekey: str | None
    title: str | None
    #: Reported, not hidden: "no such reference" and "tombstoned, cannot be
    #: cited" are different messages to the agent.
    is_tombstoned: bool


@dataclass(frozen=True, slots=True)
class ReferenceChunkView:
    reference_id: uuid.UUID
    workspace_id: uuid.UUID
    chunk_index: int
    section_path: str | None
    text: str
    #: `embedding` is deliberately absent — a Vector(1024) the tool has no use for.


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshotView:
    """The workspace's frozen copies of the linked call and profile.

    Plain values only. Returning the Workspace row itself would hand the tool
    object_session(row) — the full Session again, which is the authority this whole
    design removes (§5).
    """

    call_body: str | None
    rubric_criteria: list[dict[str, Any]]
    rubric_funder_notes: str
    profile_desc: str | None


@runtime_checkable
class Queries(Protocol):
    def get_reference(
        self, workspace_id: uuid.UUID, reference_id: uuid.UUID
    ) -> ReferenceView | None: ...

    def get_reference_chunk(
        self, workspace_id: uuid.UUID, reference_id: uuid.UUID, chunk_index: int
    ) -> ReferenceChunkView | None: ...

    def get_workspace_snapshot(
        self, workspace_id: uuid.UUID
    ) -> WorkspaceSnapshotView | None:
        """The linked call + profile snapshots, or None when the workspace is gone.

        Reads are NOT fenced (spec §8). A workspace that was deleted mid-turn reads
        as None, which the tools answer as {"linked": False} — the same shape they
        have always returned for an empty snapshot.
        """
        ...
