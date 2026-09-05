from datetime import UTC, datetime

from sqlalchemy import select

from call_manager.db.models import (
    Call,
    CallDecision,
    CallRanking,
    GoldenSetTriple,
    Profile,
    ProfileKind,
    Source,
    Subscription,
    User,
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
        u = User(email="a@umu.se")
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
        u = User(email="sub@umu.se")
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
        s.add(
            CallDecision(
                user_id=u.id,
                profile_id=p.id,
                call_id=c.id,
                state="hidden",
                decided_at=datetime.now(UTC),
            )
        )
        s.commit()
        cands = candidates_dismissed_high_score(s)
        assert any(x.call_id == c.id and x.profile_id == p.id for x in cands)


# ---------------------------------------------------------------------------
# Gate 4 - a SAVED call is not a negative label. Without the state filter the
# golden set mines the calls the user explicitly KEPT as evidence that the model
# scored them too high, which is a silent data-quality bug.
# ---------------------------------------------------------------------------
def test_candidates_dismissed_high_score_ignores_saved(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="saver@umu.se")
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
        c = _mk_call(s, src, "kept")
        s.add(
            CallRanking(
                call_id=c.id,
                profile_id=p.id,
                aggregate_score=88,
                aggregate_reason=None,
                rationale="r",
                model="m",
                prompt_version="v4.1",
                raw_response={},
                ranked_at=datetime.now(UTC),
            )
        )
        s.add(
            CallDecision(
                user_id=u.id,
                profile_id=p.id,
                call_id=c.id,
                state="saved",
                decided_at=datetime.now(UTC),
            )
        )
        s.commit()
        cands = candidates_dismissed_high_score(s)
        assert not any(x.call_id == c.id for x in cands), (
            "a call the user SAVED at score 88 must not be mined as a "
            "dismissed_high_score candidate"
        )


# ---------------------------------------------------------------------------
# Gate 4b - the correlation is on the profile the decision was made FOR, not on
# any profile the decider happens to subscribe to.
# ---------------------------------------------------------------------------
def test_candidates_dismissed_high_score_is_scoped_to_the_decisions_profile(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="two-prof@umu.se")
        s.add(u)
        s.flush()
        profs = []
        for name in ("PA", "PB"):
            pr = Profile(
                name=name,
                kind=ProfileKind.ORGANIZATIONAL_UNIT,
                description="d",
                structured={},
                created_by=u.id,
            )
            s.add(pr)
            s.flush()
            s.add(Subscription(user_id=u.id, profile_id=pr.id, digest_enabled=True))
            profs.append(pr)
        pa, pb = profs
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = _mk_call(s, src, "scoped")
        for pr in (pa, pb):
            s.add(
                CallRanking(
                    call_id=c.id,
                    profile_id=pr.id,
                    aggregate_score=77,
                    aggregate_reason=None,
                    rationale="r",
                    model="m",
                    prompt_version="v4.1",
                    raw_response={},
                    ranked_at=datetime.now(UTC),
                )
            )
        s.add(
            CallDecision(
                user_id=u.id,
                profile_id=pa.id,
                call_id=c.id,
                state="hidden",
                decided_at=datetime.now(UTC),
            )
        )
        s.commit()
        pa_id, pb_id = pa.id, pb.id
        cands = candidates_dismissed_high_score(s)
        hit = {(x.call_id, x.profile_id) for x in cands if x.call_id == c.id}
        assert hit == {(c.id, pa_id)}, (
            "hidden on PA only, so PB's (equally high) ranking is NOT a candidate. The "
            f"old subscription join produced both. Got {sorted(hit)}, pb={pb_id}"
        )


# ---------------------------------------------------------------------------
# Gate 4c (D13) - the widening is INTENDED and pinned. The old query required
# the decider to be SUBSCRIBED to the profile; the new one correlates on
# (call_id, profile_id, state) alone. A hide on profile P by any READER of P is
# a negative label for P - that is what "a decision is per profile" means, and
# the write rule already requires readability. Re-adding a subscription
# predicate reddens exactly this test.
# ---------------------------------------------------------------------------
def test_candidates_dismissed_high_score_sees_a_non_subscribed_readers_hide(pg_session_factory):
    with pg_session_factory() as s:
        owner = User(email="owner-gs@umu.se", status="active")
        reader = User(email="reader-gs@umu.se", status="active")
        s.add_all([owner, reader])
        s.flush()
        p = Profile(
            name="OrgVisible",
            kind=ProfileKind.ORGANIZATIONAL_UNIT,
            description="d",
            structured={},
            created_by=owner.id,
        )
        s.add(p)
        s.flush()
        # The reader is deliberately NOT subscribed: they can READ the profile
        # (it takes the server default 'org' visibility) and nothing more.
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = _mk_call(s, src, "reader-hid")
        s.add(
            CallRanking(
                call_id=c.id,
                profile_id=p.id,
                aggregate_score=80,
                aggregate_reason=None,
                rationale="r",
                model="m",
                prompt_version="v4.1",
                raw_response={},
                ranked_at=datetime.now(UTC),
            )
        )
        s.add(
            CallDecision(
                user_id=reader.id,
                profile_id=p.id,
                call_id=c.id,
                state="hidden",
                decided_at=datetime.now(UTC),
            )
        )
        s.commit()
        assert (
            s.execute(
                select(Subscription).where(Subscription.user_id == reader.id)
            ).scalar_one_or_none()
            is None
        ), "self-check: the reader must NOT be subscribed, or this gate proves nothing"
        pid = p.id
        cands = candidates_dismissed_high_score(s)
        hit = {(x.call_id, x.profile_id) for x in cands if x.call_id == c.id}
        assert hit == {(c.id, pid)}, (
            "a reader who is not subscribed hid this call on the profile; the golden "
            f"set must see it (D13). Got {sorted(hit)}"
        )


def test_candidates_cosine_disagreement_high_cos_low_score(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="c@umu.se")
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
        u = User(email="x@umu.se")
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
