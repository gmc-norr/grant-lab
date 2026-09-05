"""Content-keyed verdict cache: key derivation + best-effort store/get."""

from sqlalchemy import select

from grant_lab.db.models import CitationAuditVerdict
from grant_lab.proposal_assist.citations.verdict_cache import (
    build_cache_key,
    get_verdict,
    store_verdict,
)
from tests.proposal_assist.conftest import make_user, make_workspace

_BASE = dict(
    claim_hash="claim",
    chunk_content_hash="chunk",
    evidence_quote_hash="quote",
    stance="supports",
    validator_version="v1",
)


def test_build_cache_key_deterministic():
    assert build_cache_key(**_BASE) == build_cache_key(**_BASE)


def test_build_cache_key_changes_per_component():
    base = build_cache_key(**_BASE)
    for field, other in (
        ("claim_hash", "claim2"),
        ("chunk_content_hash", "chunk2"),
        ("evidence_quote_hash", "quote2"),
        ("stance", "relevant"),
        ("validator_version", "v2"),
    ):
        variant = build_cache_key(**{**_BASE, field: other})
        assert variant != base, f"changing {field} must change the key"


def test_build_cache_key_no_boundary_collision():
    # NUL-join prevents "ab"+"c" colliding with "a"+"bc".
    a = build_cache_key(
        claim_hash="ab",
        chunk_content_hash="c",
        evidence_quote_hash="q",
        stance="supports",
        validator_version="v1",
    )
    b = build_cache_key(
        claim_hash="a",
        chunk_content_hash="bc",
        evidence_quote_hash="q",
        stance="supports",
        validator_version="v1",
    )
    assert a != b


def test_store_then_get_round_trip(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    store_verdict(
        pg_session_factory,
        workspace_id=ws_id,
        cache_key="k1",
        verdict="supports",
        rationale="the quote states the claim",
        validator_version="v1",
    )

    with pg_session_factory() as s:
        row = get_verdict(s, workspace_id=ws_id, cache_key="k1")
        assert row is not None
        assert row.verdict == "supports"
        assert row.rationale == "the quote states the claim"
        assert row.validator_version == "v1"


def test_get_verdict_miss_returns_none(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id
        assert get_verdict(s, workspace_id=ws_id, cache_key="nope") is None


def test_workspace_isolation(pg_session_factory):
    with pg_session_factory() as s:
        ws_a = make_workspace(s, owner=make_user(s, email="a@umu.se"))
        ws_b = make_workspace(s, owner=make_user(s, email="b@umu.se"))
        s.commit()
        a_id, b_id = ws_a.id, ws_b.id

    store_verdict(
        pg_session_factory,
        workspace_id=a_id,
        cache_key="shared",
        verdict="supports",
        rationale=None,
        validator_version="v1",
    )

    with pg_session_factory() as s:
        assert get_verdict(s, workspace_id=a_id, cache_key="shared") is not None
        assert get_verdict(s, workspace_id=b_id, cache_key="shared") is None


def test_store_verdict_best_effort_swallows_constraint_violation(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    # "bogus" violates the verdict CheckConstraint — store_verdict must NOT raise.
    store_verdict(
        pg_session_factory,
        workspace_id=ws_id,
        cache_key="bad",
        verdict="bogus",
        rationale=None,
        validator_version="v1",
    )

    with pg_session_factory() as s:
        assert get_verdict(s, workspace_id=ws_id, cache_key="bad") is None


def test_get_verdict_returns_newest(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    for verdict in ("supports", "contradicts"):
        store_verdict(
            pg_session_factory,
            workspace_id=ws_id,
            cache_key="dup",
            verdict=verdict,
            rationale=None,
            validator_version="v1",
        )

    with pg_session_factory() as s:
        rows = s.scalars(
            select(CitationAuditVerdict).where(
                CitationAuditVerdict.workspace_id == ws_id,
                CitationAuditVerdict.cache_key == "dup",
            )
        ).all()
        assert len(rows) == 2  # idempotent re-store appends a new row
        assert get_verdict(s, workspace_id=ws_id, cache_key="dup").verdict == "contradicts"
