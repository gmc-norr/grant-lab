from datetime import UTC, datetime

from sqlalchemy import select

from call_manager.db.models import (
    Call,
    CallRanking,
    GoldenSetTriple,
    Profile,
    ProfileKind,
    Source,
    Subscription,
    User,
    UserCallDismissal,
)
from call_manager.ranking.golden_set import (
    add_triple,
    candidates_cosine_disagreement,
    candidates_dismissed_high_score,
    delete_triple,
    list_triples,
)


def _mk_call(s, src, ext):
    c = Call(
        source_id=src.id,
        external_id=ext,
        url=f"https://x/{ext}",
        title=ext,
        published_at=datetime.now(UTC),
    )
    s.add(c)
    s.flush()
    return c


def test_add_list_delete_roundtrip(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="a@ki.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="P",
            kind=ProfileKind.ORGANIZATIONAL_UNIT,
            description="d",
            structured={},
            created_by=u.id,
        )
        s.add(p)
        s.flush()
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = _mk_call(s, src, "c1")
        s.commit()
        tid = add_triple(
            s, call_id=c.id, profile_id=p.id, expected_score=70, note="solid", created_by=u.id
        )
        s.commit()
        rows = list_triples(s)
        assert len(rows) == 1
        assert rows[0].expected_score == 70
        assert rows[0].call_title == "c1"
        assert rows[0].profile_name == "P"
        delete_triple(s, triple_id=tid)
        s.commit()
        assert list_triples(s) == []


def test_candidates_dismissed_high_score(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="sub@ki.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="P",
            kind=ProfileKind.ORGANIZATIONAL_UNIT,
            description="d",
            structured={},
            created_by=u.id,
        )
        s.add(p)
        s.flush()
        s.add(Subscription(user_id=u.id, profile_id=p.id, digest_enabled=True))
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = _mk_call(s, src, "hot")
        s.add(
            CallRanking(
                call_id=c.id,
                profile_id=p.id,
                aggregate_score=72,
                aggregate_reason=None,
                rationale="r",
                model="m",
                prompt_version="v4.1",
                raw_response={},
                ranked_at=datetime.now(UTC),
            )
        )
        s.add(UserCallDismissal(user_id=u.id, call_id=c.id, dismissed_at=datetime.now(UTC)))
        s.commit()
        cands = candidates_dismissed_high_score(s)
        assert any(x.call_id == c.id and x.profile_id == p.id for x in cands)


def test_candidates_cosine_disagreement_high_cos_low_score(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="c@ki.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="P",
            kind=ProfileKind.ORGANIZATIONAL_UNIT,
            description="d",
            structured={},
            created_by=u.id,
        )
        s.add(p)
        s.flush()
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = _mk_call(s, src, "dis")
        s.add(
            CallRanking(
                call_id=c.id,
                profile_id=p.id,
                aggregate_score=15,
                aggregate_reason=None,
                rationale="r",
                model="m",
                prompt_version="v4.1",
                raw_response={},
                embed_similarity=0.72,
                ranked_at=datetime.now(UTC),
            )
        )
        s.commit()
        cands = candidates_cosine_disagreement(s)
        assert any(x.call_id == c.id for x in cands)


def test_candidates_exclude_existing_triples(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="x@ki.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="P",
            kind=ProfileKind.ORGANIZATIONAL_UNIT,
            description="d",
            structured={},
            created_by=u.id,
        )
        s.add(p)
        s.flush()
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = _mk_call(s, src, "dup")
        s.add(
            CallRanking(
                call_id=c.id,
                profile_id=p.id,
                aggregate_score=15,
                aggregate_reason=None,
                rationale="r",
                model="m",
                prompt_version="v4.1",
                raw_response={},
                embed_similarity=0.80,
                ranked_at=datetime.now(UTC),
            )
        )
        s.add(GoldenSetTriple(call_id=c.id, profile_id=p.id, expected_score=50))
        s.commit()
        assert candidates_cosine_disagreement(s) == []
