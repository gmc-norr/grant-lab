"""F3b — query ports return detached DTOs, never attached ORM instances."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime

from grant_lab.db.models import Reference, ReferenceChunk
from grant_lab.proposal_assist.port_impl.queries import SqlQueries
from grant_lab.proposal_assist.ports.queries import (
    Queries,
    ReferenceChunkView,
    ReferenceView,
)
from tests.proposal_assist.dto_contract import assert_detached
from tests.proposal_assist.f3b_seeds import seed_workspace


def _seed_reference(session, ws_id, *, tombstoned=False) -> uuid.UUID:
    ref = Reference(
        workspace_id=ws_id,
        kind="paper",
        source_type="uploaded",  # ck_references_source_type: uploaded|crossref|pubmed|arxiv|web
        filename="p.pdf",
        storage_path=f"{ws_id}/p.pdf",
        mime_type="application/pdf",
        title="A Paper",
        citekey="doe2020",
        tombstoned_at=datetime.now(UTC) if tombstoned else None,
    )
    session.add(ref)
    session.flush()
    session.add(
        ReferenceChunk(
            reference_id=ref.id,
            workspace_id=ws_id,
            chunk_index=0,
            section_path="Intro",
            text="the chunk text",
            embedding=[0.0] * 1024,
            embedding_model="bge-m3",
        )
    )
    session.commit()
    return ref.id


def test_the_implementation_satisfies_the_protocol(pg_session_factory):
    assert isinstance(SqlQueries(pg_session_factory), Queries)


def test_get_reference_returns_a_frozen_dto(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=0)
        ref_id = _seed_reference(s, ws_id)

    view = SqlQueries(pg_session_factory).get_reference(ws_id, ref_id)
    assert view == ReferenceView(
        id=ref_id,
        workspace_id=ws_id,
        citekey="doe2020",
        title="A Paper",
        is_tombstoned=False,
    )
    assert dataclasses.is_dataclass(view)


def test_a_tombstoned_reference_is_reported_not_hidden(pg_session_factory):
    """The tool needs to tell "no such reference" from "tombstoned, cannot be
    cited" — they are different messages to the agent."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=0)
        ref_id = _seed_reference(s, ws_id, tombstoned=True)

    assert SqlQueries(pg_session_factory).get_reference(ws_id, ref_id).is_tombstoned


def test_get_reference_scopes_to_the_workspace(pg_session_factory):
    """The port takes the workspace_id rather than trusting the caller to compare
    it afterwards, so a cross-workspace read cannot be forgotten."""
    with pg_session_factory() as s:
        mine = seed_workspace(s, generation=0)
        theirs = seed_workspace(s, generation=0)
        their_ref = _seed_reference(s, theirs)

    assert SqlQueries(pg_session_factory).get_reference(mine, their_ref) is None


def test_get_reference_chunk_returns_the_text_without_the_embedding(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=0)
        ref_id = _seed_reference(s, ws_id)

    chunk = SqlQueries(pg_session_factory).get_reference_chunk(ws_id, ref_id, 0)
    assert chunk == ReferenceChunkView(
        reference_id=ref_id,
        workspace_id=ws_id,
        chunk_index=0,
        section_path="Intro",
        text="the chunk text",
    )
    assert not hasattr(chunk, "embedding")


def test_a_missing_chunk_is_none(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=0)
        ref_id = _seed_reference(s, ws_id)

    assert SqlQueries(pg_session_factory).get_reference_chunk(ws_id, ref_id, 99) is None


def test_get_reference_chunk_scopes_to_the_workspace(pg_session_factory):
    """Same guarantee as test_get_reference_scopes_to_the_workspace, for the
    chunk read: the WHERE scopes on the caller's workspace_id itself, rather
    than trusting the caller to compare it afterwards, so a regression that
    dropped ReferenceChunk.workspace_id == workspace_id from the predicate
    would leak another workspace's chunk instead of failing loudly."""
    with pg_session_factory() as s:
        mine = seed_workspace(s, generation=0)
        theirs = seed_workspace(s, generation=0)
        their_ref = _seed_reference(s, theirs)

    assert SqlQueries(pg_session_factory).get_reference_chunk(mine, their_ref, 0) is None


def test_no_query_result_carries_a_live_session(pg_session_factory):
    """GC-5. object_session() on an attached instance IS the full Session — the
    leak that defeated the forwarding-proxy design. A DTO carries data.

    Recursive (assert_detached): walks lists, tuples, sets, and dicts (keys and
    values) as well as nested dataclasses, not just each result's own top-level
    fields, so a collection field holding raw ORM instances cannot pass through
    untouched. Both current DTOs are flat scalars, so this only exercises the
    top level today — it is the template a later slice extends to richer
    results."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=0)
        ref_id = _seed_reference(s, ws_id)

    q = SqlQueries(pg_session_factory)
    for result in (q.get_reference(ws_id, ref_id), q.get_reference_chunk(ws_id, ref_id, 0)):
        assert_detached(result, type(result).__name__)
