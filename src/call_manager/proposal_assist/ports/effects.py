"""Semantic effect commands and the Effects contract — stdlib only.

Spec 3.2. One method per semantic operation, each taking an immutable FenceStamp
and a command object. The method owns the session, the generation check, the
transaction and the commit; the caller sees none of them.

There is deliberately NO generic `fenced_write(...) as session` and NO public
`fenced_execute(ctx, statement)`. The first hands back a session; the second
hands back the ability to compose any statement against any table. Both are the
authority this design removes.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from call_manager.proposal_assist.ports.stamp import FenceStamp


@dataclass(frozen=True, slots=True)
class SubmitScorecard:
    scorecard: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SubmitReview:
    review_run_item_id: uuid.UUID
    markdown: str


@dataclass(frozen=True, slots=True)
class SubmitUncitedClaims:
    claims: list[dict[str, Any]]


@runtime_checkable
class Effects(Protocol):
    """Every method raises WorkspaceGenerationStale when the workspace has moved
    past the stamp's generation, and EffectRejected for a wrong, finished or
    already-submitted target."""

    def submit_scorecard(self, stamp: FenceStamp, cmd: SubmitScorecard) -> None: ...

    def submit_review(self, stamp: FenceStamp, cmd: SubmitReview) -> None: ...

    def submit_uncited_claims(
        self, stamp: FenceStamp, cmd: SubmitUncitedClaims
    ) -> None: ...


#: Spec 3.5 — every effect operation and the ONE mechanism it uses. A named port
#: does not make its internal fence atomic, so each operation declares its
#: mechanism and never upgrades between them. test_every_effect_declares_its_
#: mechanism keeps this table from drifting away from the Protocol.
#:
#: Every F3b operation is DB-only (mechanism 1). The exclusive-mutex mechanism
#: belongs to effects that touch git or the filesystem, which arrive in F3e.
EFFECT_MECHANISMS: Mapping[str, str] = MappingProxyType(
    {
        "submit_scorecard": "shared lease + live_at_generation in the UPDATE",
        "submit_review": "shared lease + live_at_generation in the UPDATE",
        "submit_uncited_claims": "shared lease + live_at_generation in the UPDATE",
    }
)
