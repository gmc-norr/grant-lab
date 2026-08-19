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

    `origin_job_id` serves two roles, deliberately: for the submit_* effects it
    targets the actor's own background job row, and for the propose_* effects
    it is ALSO the durable attribution written into proposed_changes.origin_job_id
    (there is no separate copy on Provenance). Both roles answer the same
    question — which actor did this — so one field serving both is the point,
    not a shortcut: splitting it into a second, independently-suppliable copy
    on Provenance is exactly what let the two values disagree. `active_turn_job_id`
    is a genuinely separate field and cannot be folded into it: registry.py
    leaves origin_job_id None for interactive contexts BY DESIGN, and that stays
    true independent of whichever turn is currently active.
    """

    workspace_id: uuid.UUID
    generation: int
    active_turn_job_id: uuid.UUID | None
    origin_job_id: uuid.UUID | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Provenance:
    """Row data, not identity: copied verbatim into durable proposed_changes.

    Everything the actor's identity itself supplies — including the durable
    origin_job_id attribution — lives on FenceStamp instead (see its
    docstring). What is here is the rest of a proposed-change row's
    provenance: which kind of context produced it, which session/turn/prompt
    triggered it, and which user (if any) is the human author of record.
    """

    origin_kind: str
    origin_session_id: str | None
    triggering_turn_id: uuid.UUID | None
    triggering_prompt: str | None
    created_by: int | None
