"""FenceStamp and Provenance — an actor's immutable identity, stdlib only.

Spec §3.4. Snapshotted per tool INVOCATION at dispatch, never read from the
retained ToolContext at the moment of the effect: set_turn_provenance restamps
that context every turn, so a late read reads a NEWER generation and the stale
actor's write is then perfectly fenced against the wrong generation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class FenceStamp:
    """What an effect needs to fence itself and find its own target row.

    `origin_job_id` targets the actor's own background job. `active_turn_job_id`
    is separate and cannot be folded into it: registry.py leaves origin_job_id
    None for interactive contexts BY DESIGN, and tools/proposal.py copies it into
    durable provenance, so populating it would change stored data.
    """

    workspace_id: uuid.UUID
    generation: int
    active_turn_job_id: uuid.UUID | None
    origin_job_id: uuid.UUID | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Provenance:
    """Row data, not identity: copied verbatim into durable proposed_changes."""

    origin_kind: str
    origin_session_id: str | None
    origin_job_id: uuid.UUID | None
    triggering_turn_id: uuid.UUID | None
    triggering_prompt: str | None
    created_by: int | None
