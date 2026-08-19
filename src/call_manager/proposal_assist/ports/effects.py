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
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from call_manager.proposal_assist.ports.stamp import FenceStamp, Provenance


@dataclass(frozen=True, slots=True)
class SubmitScorecard:
    scorecard: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SubmitReview:
    review_run_item_id: uuid.UUID
    #: Stored verbatim (`port_impl/effects.py::SqlEffects.submit_review`) — this
    #: layer does NOT strip or reject an empty result. That is the tool
    #: boundary's job (`tools/review_submit.py:16-18`): a converter that drops
    #: the tool's strip-and-reject step loses it silently.
    markdown: str


@dataclass(frozen=True, slots=True)
class SubmitUncitedClaims:
    claims: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ProposeArtifactEdit:
    """A pending full-text replacement of ONE section.

    base_commit_sha / base_section_sha come from the agent's READ-TIME snapshot
    and are passed in by the caller. They must never be recomputed live: a human
    edit landed since the read would silently become the base, and accept would
    then clobber it.
    """

    target_artifact: str
    section_index: int
    new_text: str
    base_commit_sha: str
    base_section_sha: str
    provenance: Provenance
    #: Draft only. Folded into the payload as selection_anchor/_from/_to.
    selection: Mapping[str, Any] | None = field(default=None)


@dataclass(frozen=True, slots=True)
class ProposeCitationInsert:
    """A pending citation insert. ev_id, citekey and offsets are minted
    authoritatively at ACCEPT time, not stored here."""

    section_index: int
    base_commit_sha: str
    base_section_sha: str
    reference_id: uuid.UUID
    anchor: str
    chunk_index: int
    evidence_quote: str
    stance: str
    confidence: str | None
    provenance: Provenance


@runtime_checkable
class Effects(Protocol):
    """Every method raises WorkspaceGenerationStale when the workspace has moved
    past the stamp's generation, and EffectRejected for a wrong, finished or
    already-submitted target.

    A THIRD exception escapes and it is NOT in ports/errors.py:
    `CommitMutexTimeout` (`drafting/locking.py`), raised by the shared lease
    when an exclusive holder outlasts `acquire_shared_lease`'s 10s cap. It
    means "busy, retry", not "failed": web routes answer 429 + Retry-After: 2
    with BUSY_ERROR_MESSAGE, and `tools/citations.py:793` already returns that
    same string to the agent.

    A converter that catches only the two errors above therefore DOWNGRADES a
    transient busy into an internal error — `tools/registry.py::_safe_tool`
    catches every unnamed exception and returns "internal error". F3d owns the
    decision of how a fenced tool NAMES this one: ports/ is stdlib-only, and
    BUSY_ERROR_MESSAGE lives beside the class in drafting/locking.py, so both
    would have to move for a converted tool to import neither.
    """

    def submit_scorecard(self, stamp: FenceStamp, cmd: SubmitScorecard) -> None: ...

    def submit_review(self, stamp: FenceStamp, cmd: SubmitReview) -> None: ...

    def submit_uncited_claims(
        self, stamp: FenceStamp, cmd: SubmitUncitedClaims
    ) -> None: ...

    def propose_artifact_edit(
        self, stamp: FenceStamp, cmd: ProposeArtifactEdit
    ) -> uuid.UUID: ...

    def propose_citation_insert(
        self, stamp: FenceStamp, cmd: ProposeCitationInsert
    ) -> uuid.UUID: ...


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
        "propose_artifact_edit": "shared lease + INSERT … SELECT … WHERE live_at_generation",
        "propose_citation_insert": "shared lease + INSERT … SELECT … WHERE live_at_generation",
    }
)
