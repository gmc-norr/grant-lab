"""F3c — the DTO contract (spec 4.2).

The import gate cannot see this one. A query port that returns an ATTACHED ORM
instance hands the tool object_session(instance) — the full Session — without the
tool importing anything at all. So detachment gets its own test, over every
method the Queries Protocol declares.
"""

from __future__ import annotations

import uuid

import pytest

from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.port_impl.queries import SqlQueries
from call_manager.proposal_assist.ports.queries import Queries
from tests.proposal_assist.dto_contract import assert_detached
from tests.proposal_assist.f3b_seeds import seed_workspace


def _seed(session, ws_id) -> uuid.UUID:
    ref = Reference(
        workspace_id=ws_id,
        kind="paper",
        source_type="uploaded",
        filename="p.pdf",
        storage_path=f"{ws_id}/p.pdf",
        mime_type="application/pdf",
        title="A Paper",
        citekey="doe2020",
        tombstoned_at=None,
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


#: One case per Queries method: a callable taking (queries, workspace_id,
#: reference_id) and returning the result to inspect. Every case must return a
#: NON-None result, or it proves nothing — a method returning None trivially
#: carries no session.
CASES = {
    "get_reference": lambda q, ws, ref: q.get_reference(ws, ref),
    "get_reference_chunk": lambda q, ws, ref: q.get_reference_chunk(ws, ref, 0),
    "get_workspace_snapshot": lambda q, ws, ref: q.get_workspace_snapshot(ws),
}


def test_every_query_method_has_a_contract_case():
    """Completeness. A new query port added without a case here would ship with
    its detachment unproven, and nothing else in the tree would notice.

    `__protocol_attrs__`, not `vars(Queries)`: `vars` is one class `__dict__` and does
    not walk the MRO, so the day the read surface is split into a base Protocol every
    inherited method silently stops needing a case — AND any case still covering one
    is reported as `stale`, actively instructing the author to delete working
    coverage. `__protocol_attrs__` is typing-internal but stable in 3.12, which this
    project pins; if it ever disappears the failure is a loud AttributeError, not a
    silent pass.
    """
    declared = set(Queries.__protocol_attrs__)
    assert set(CASES) == declared, (
        f"uncovered: {sorted(declared - set(CASES))}; "
        f"stale: {sorted(set(CASES) - declared)}"
    )


@pytest.mark.parametrize("name", sorted(CASES))
def test_every_query_result_is_recursively_free_of_orm_instances(
    name, pg_session_factory
):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=0)
        ref_id = _seed(s, ws_id)

    result = CASES[name](SqlQueries(pg_session_factory), ws_id, ref_id)
    assert result is not None, f"{name} returned None — the case proves nothing"
    assert_detached(result, name)


def test_the_walker_finds_an_orm_instance_hidden_in_a_collection(pg_session_factory):
    """The walker's own proof. A flat DTO of scalars would pass any checker; what
    matters is that a collection field cannot smuggle one through."""
    import dataclasses

    @dataclasses.dataclass(frozen=True)
    class Carrier:
        items: list

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=0)
        _seed(s, ws_id)
        attached = s.query(Reference).first()
        with pytest.raises(AssertionError) as e:
            assert_detached(Carrier(items=[attached]), "carrier")
    assert "carrier.items[0]" in str(e.value)


def test_the_walker_terminates_on_a_cycle():
    """A self-referential structure must fail loudly rather than exhaust the
    stack. Not reachable through today's flat DTOs; the walker is the template
    every future query result goes through."""
    node: dict = {"name": "a"}
    node["self"] = node
    with pytest.raises(RecursionError):
        assert_detached(node, "node")


def test_the_walker_rejects_a_container_it_cannot_see_inside(pg_session_factory):
    """Fail-closed. An ordinary object is not a dataclass, a dict or a sequence, so a
    shape-blind walker returns without descending — and an attached Reference one
    attribute deep sails through, where `object_session(o.ref)` is the Session this
    contract exists to deny."""

    class Opaque:
        def __init__(self, ref):
            self.ref = ref

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=0)
        _seed(s, ws_id)
        attached = s.query(Reference).first()
        with pytest.raises(AssertionError) as e:
            assert_detached(Opaque(attached), "opaque")
    assert "opaque: the walker cannot see inside Opaque" in str(e.value)


def test_the_walker_rejects_a_lazy_iterator():
    """Rejection, not attribute-recursion, is the only sound answer for an iterator:
    the walker cannot look inside one without consuming the caller's own result. A
    query port that yields rows is exactly the shape that must not pass quietly."""
    with pytest.raises(AssertionError) as e:
        assert_detached((v for v in [1]), "rows")
    assert "cannot see inside generator" in str(e.value)
