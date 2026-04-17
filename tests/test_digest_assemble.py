from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from call_monitor.db.models import (
    Call,
    CallRanking,
    DigestRun,
    Profile,
    ProfileKind,
    Source,
    Subscription,
    User,
    UserCallDismissal,
)
from call_monitor.digest.assemble import assemble_for_user


@pytest.fixture()
def now():
    return datetime(2026, 4, 17, 7, 0, tzinfo=UTC)


def _seed_user_with_profile(s, *, email="u@ki.se", profile_name="P", must_include=None):
    u = User(email=email)
    s.add(u)
    s.flush()
    p = Profile(
        name=profile_name, kind=ProfileKind.GROUP, description="", structured={
            "areas": [], "budget_min": None, "budget_max": None,
            "trl_min": None, "trl_max": None,
            "must_include": must_include or [], "hard_exclusions": [],
        }, created_by=u.id,
    )
    s.add(p)
    s.flush()
    s.add(Subscription(user_id=u.id, profile_id=p.id, digest_enabled=True))
    s.flush()
    return u.id, p.id


def _seed_call(s, *, title, score=None, profile_id=None, summary=None,
               keywords=None, deadline=None, published_at=None):
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(
        source_id=src.id, external_id=title, url=f"https://x/{title}",
        title=title, summary=summary, keywords=keywords or [],
        deadline=deadline, published_at=published_at or datetime.now(UTC),
    )
    s.add(c)
    s.flush()
    if score is not None and profile_id is not None:
        s.add(CallRanking(
            call_id=c.id, profile_id=profile_id, score=score,
            rationale="r", model="gemma4:e4b-q8", prompt_version="v1",
            ranked_at=datetime.now(UTC),
        ))
        s.flush()
    return c.id


def test_assemble_returns_none_when_no_subscriptions(pg_session_factory, now):
    with pg_session_factory() as s:
        u = User(email="lonely@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    assert payload is None


def test_assemble_returns_none_when_buckets_empty(pg_session_factory, now):
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s)
        # Seed a call but score it BELOW borderline (40)
        _seed_call(s, title="LowScore", score=40, profile_id=pid)
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    assert payload is None


def test_assemble_top_bucket_takes_top_5_above_threshold(
    pg_session_factory, now
):
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s)
        for i in range(7):
            _seed_call(s, title=f"Top{i}", score=70 + i, profile_id=pid)
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    assert payload is not None
    assert len(payload.sections) == 1
    top_entries = [e for e in payload.sections[0].entries if e.bucket == "top"]
    assert len(top_entries) == 5
    # ordered by score desc
    scores = [e.score for e in top_entries]
    assert scores == sorted(scores, reverse=True)


def test_assemble_must_include_overrides_score(pg_session_factory, now):
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s, must_include=["pathology"])
        # call with low score but matching must-include
        _seed_call(
            s, title="Pathology AI breakthrough", score=20, profile_id=pid,
        )
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    assert payload is not None
    must = [e for e in payload.sections[0].entries if e.bucket == "must_include"]
    assert len(must) == 1
    assert "Pathology" in must[0].title


def test_assemble_must_include_matches_keywords(pg_session_factory, now):
    """Spec line 417: must-include matches title/summary/KEYWORDS."""
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s, must_include=["genomics"])
        # title and summary have no match — only the keywords array does
        _seed_call(
            s, title="Funding announcement", summary="Generic blurb.",
            keywords=["genomics", "biobank"],
            score=20, profile_id=pid,
        )
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    assert payload is not None
    must = [e for e in payload.sections[0].entries if e.bucket == "must_include"]
    assert len(must) == 1


def test_assemble_dedup_must_include_beats_top_in_other_profile(
    pg_session_factory, now
):
    """Pin the bucket-priority decision: when the same call is must_include
    in P1 (low score) and top in P2 (high score), it goes to P1's must-include
    section. This is by design — must-include is the user's hard filter and
    overrides score-based ranking. Spec line 420 says 'highest-scoring profile
    wins' but that's score within bucket; must-include is a separate signal."""
    with pg_session_factory() as s:
        u = User(email="bp@ki.se")
        s.add(u)
        s.flush()
        p_must = Profile(
            name="MustProfile", kind=ProfileKind.GROUP, description="", structured={
                "areas": [], "budget_min": None, "budget_max": None,
                "trl_min": None, "trl_max": None,
                "must_include": ["pathology"], "hard_exclusions": [],
            }, created_by=u.id,
        )
        p_top = Profile(
            name="TopProfile", kind=ProfileKind.GROUP, description="", structured={
                "areas": [], "budget_min": None, "budget_max": None,
                "trl_min": None, "trl_max": None,
                "must_include": [], "hard_exclusions": [],
            }, created_by=u.id,
        )
        s.add_all([p_must, p_top])
        s.flush()
        s.add_all([
            Subscription(user_id=u.id, profile_id=p_must.id, digest_enabled=True),
            Subscription(user_id=u.id, profile_id=p_top.id, digest_enabled=True),
        ])
        cid = _seed_call(s, title="Pathology breakthrough", score=85, profile_id=p_top.id)
        # Also a low-score ranking against p_must so the call appears in both candidate sets
        s.add(CallRanking(
            call_id=cid, profile_id=p_must.id, score=20,
            rationale="r", model="gemma4:e4b-q8", prompt_version="v1",
            ranked_at=datetime.now(UTC),
        ))
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=u.id, now=now)
    must_section = next(sec for sec in payload.sections if sec.profile_name == "MustProfile")
    assert any(
        e.bucket == "must_include" and e.title == "Pathology breakthrough"
        for e in must_section.entries
    )
    # Not in TopProfile section
    top_section = next((sec for sec in payload.sections if sec.profile_name == "TopProfile"), None)
    if top_section is not None:
        assert all(e.title != "Pathology breakthrough" for e in top_section.entries)


def test_assemble_handles_profile_missing_must_include_key(pg_session_factory, now):
    """Older profiles may have structured={} without the must_include key."""
    with pg_session_factory() as s:
        u = User(email="legacy@ki.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="Legacy", kind=ProfileKind.GROUP, description="",
            structured={},  # no must_include key
            created_by=u.id,
        )
        s.add(p)
        s.flush()
        s.add(Subscription(user_id=u.id, profile_id=p.id, digest_enabled=True))
        _seed_call(s, title="X", score=80, profile_id=p.id)
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=u.id, now=now)
    # No crash; the call still lands in the top bucket
    assert payload is not None
    assert any(e.title == "X" for sec in payload.sections for e in sec.entries)


def test_assemble_borderline_only_when_no_top(pg_session_factory, now):
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s)
        _seed_call(s, title="Border1", score=50, profile_id=pid)
        _seed_call(s, title="Border2", score=58, profile_id=pid)
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    assert payload is not None
    border = [e for e in payload.sections[0].entries if e.bucket == "borderline"]
    assert len(border) == 2


def test_assemble_excludes_dismissed(pg_session_factory, now):
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s)
        _seed_call(s, title="V", score=80, profile_id=pid)
        cid_hidden = _seed_call(s, title="H", score=80, profile_id=pid)
        s.add(UserCallDismissal(user_id=uid, call_id=cid_hidden))
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    titles = [e.title for sec in payload.sections for e in sec.entries]
    assert "V" in titles
    assert "H" not in titles


def test_assemble_excludes_already_in_prior_digest(pg_session_factory, now):
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s)
        cid_old = _seed_call(s, title="Old", score=85, profile_id=pid)
        _seed_call(s, title="New", score=80, profile_id=pid)
        s.add(DigestRun(user_id=uid, included_call_ids=[cid_old]))
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    titles = [e.title for sec in payload.sections for e in sec.entries]
    assert "New" in titles
    assert "Old" not in titles


def test_assemble_excludes_past_deadline(pg_session_factory, now):
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s)
        _seed_call(
            s, title="Future", score=80, profile_id=pid,
            deadline=now + timedelta(days=10),
        )
        _seed_call(
            s, title="Past", score=80, profile_id=pid,
            deadline=now - timedelta(days=1),
        )
        _seed_call(s, title="NoDeadline", score=80, profile_id=pid, deadline=None)
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    titles = [e.title for sec in payload.sections for e in sec.entries]
    assert "Future" in titles
    assert "NoDeadline" in titles
    assert "Past" not in titles


def test_assemble_dedup_across_profiles_keeps_highest_score(
    pg_session_factory, now
):
    """Same call ranked against two profiles. Lands once, in the higher
    section. The other profile's section gets an `also_in_profiles` note."""
    with pg_session_factory() as s:
        u = User(email="multi@ki.se")
        s.add(u)
        s.flush()
        p_low = Profile(
            name="LowProfile", kind=ProfileKind.GROUP, description="", structured={
                "areas": [], "budget_min": None, "budget_max": None,
                "trl_min": None, "trl_max": None,
                "must_include": [], "hard_exclusions": [],
            }, created_by=u.id,
        )
        p_high = Profile(
            name="HighProfile", kind=ProfileKind.GROUP, description="", structured={
                "areas": [], "budget_min": None, "budget_max": None,
                "trl_min": None, "trl_max": None,
                "must_include": [], "hard_exclusions": [],
            }, created_by=u.id,
        )
        s.add_all([p_low, p_high])
        s.flush()
        s.add_all([
            Subscription(user_id=u.id, profile_id=p_low.id, digest_enabled=True),
            Subscription(user_id=u.id, profile_id=p_high.id, digest_enabled=True),
        ])
        s.flush()
        cid = _seed_call(s, title="Shared", score=70, profile_id=p_low.id)
        s.add(CallRanking(
            call_id=cid, profile_id=p_high.id, score=85,
            rationale="r", model="gemma4:e4b-q8", prompt_version="v1",
            ranked_at=datetime.now(UTC),
        ))
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=u.id, now=now)
    assert payload is not None
    high_section = next(sec for sec in payload.sections if sec.profile_name == "HighProfile")
    low_section = next((sec for sec in payload.sections if sec.profile_name == "LowProfile"), None)
    shared = [e for e in high_section.entries if e.title == "Shared"]
    assert len(shared) == 1
    assert "LowProfile" in shared[0].also_in_profiles
    # NOT in low section
    if low_section is not None:
        assert all(e.title != "Shared" for e in low_section.entries)


def test_assemble_skips_inactive_profile(pg_session_factory, now):
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s)
        s.get(Profile, pid).is_active = False
        _seed_call(s, title="X", score=80, profile_id=pid)
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    assert payload is None


def test_assemble_skips_when_digest_disabled(pg_session_factory, now):
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s)
        sub = s.execute(select(Subscription).where(Subscription.user_id == uid)).scalar_one()
        sub.digest_enabled = False
        _seed_call(s, title="X", score=80, profile_id=pid)
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    assert payload is None
