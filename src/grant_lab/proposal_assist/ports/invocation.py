"""One tool call's frozen identity — stdlib only.

Moved out of tools/registry.py in F3d1. A converted tool receives an Invocation and
may import only the standard library and this package; naming the type at all
therefore requires it to live here. `snapshot()` stayed behind, because it reads a
ToolContext and ports/ may not name one.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from grant_lab.proposal_assist.ports.errors import UnfencedInvocation
from grant_lab.proposal_assist.ports.stamp import FenceStamp, Provenance


@dataclass(frozen=True)
class Invocation:
    """One tool call's immutable identity and per-turn provenance.

    Snapshotted at DISPATCH, before any await or thread handoff. It exists
    because ToolContext is MUTABLE and shared: set_turn_provenance restamps the
    retained context every turn, so a tool reading these values when its effect
    happens can read a LATER turn's. A stale generation paired with a newer
    turn's prompt is worse than either alone.

    Every mutable per-turn field an effect consumes belongs here. The provenance
    fields are copied into durable proposed-change rows by the propose_* tools
    (tools/proposal.py), so a late read misattributes stored data.
    """

    workspace_id: uuid.UUID
    lifecycle_generation: int | None
    active_turn_job_id: uuid.UUID | None
    origin_kind: str
    origin_job_id: uuid.UUID | None
    triggering_turn_id: uuid.UUID | None
    triggering_prompt: str | None
    created_by: int | None
    origin_session_id: str | None
    #: A READ-ONLY view over a deep copy. `frozen=True` freezes the dataclass's
    #: own attributes, not the objects they point at — a plain dict here stays
    #: aliased to ctx.turn_selection, so mutating the source would change this
    #: "immutable" snapshot. It happens to be safe today only because
    #: set_turn_provenance REPLACES the dict rather than mutating it, and an
    #: immutability guarantee must not rest on that.
    turn_selection: Mapping[str, Any] | None

    @staticmethod
    def _freeze(value):
        """Deep read-only view. turn_selection is copied into durable
        proposed-change provenance (tools/proposal.py), so a late mutation would
        misattribute stored data."""
        if isinstance(value, Mapping):
            return MappingProxyType({k: Invocation._freeze(v) for k, v in value.items()})
        if isinstance(value, (list, tuple)):
            return tuple(Invocation._freeze(v) for v in value)
        return value

    def stamp(self) -> FenceStamp:
        """This invocation's fencing identity.

        Raises UnfencedInvocation rather than defaulting: an actor with no
        claimed generation has no belief to fence against, and defaulting would
        submit an effect against a generation nobody claimed. Generation 0 is
        REAL (Workspace.lifecycle_generation has server_default 0), so this
        tests for None, never for falsiness.
        """
        if self.lifecycle_generation is None:
            raise UnfencedInvocation(self.workspace_id)
        return FenceStamp(
            workspace_id=self.workspace_id,
            generation=self.lifecycle_generation,
            active_turn_job_id=self.active_turn_job_id,
            origin_job_id=self.origin_job_id,
        )

    def provenance(self) -> Provenance:
        """The provenance the propose_* effects copy into durable rows.

        origin_job_id is NOT here: it is on the stamp instead (FenceStamp is
        built from this same Invocation, from the same self.origin_job_id), so
        there is exactly one value to disagree with itself.
        """
        return Provenance(
            origin_kind=self.origin_kind,
            origin_session_id=self.origin_session_id,
            triggering_turn_id=self.triggering_turn_id,
            triggering_prompt=self.triggering_prompt,
            created_by=self.created_by,
        )
