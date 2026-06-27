from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from call_manager.db.models import (
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
from call_manager.digest.assemble import assemble_for_user


@pytest.fixture()
def now():
    return datetime(2026, 4, 17, 7, 0, tzinfo=UTC)


def _seed_user_with_profile(s, *, email="u@ki.se", profile_name="P", must_include=None):
    u = User(email=email, status="active")
    s.add(u)
    s.flush()
    p = Profile(
        name=profile_name,
        kind=ProfileKind.ORGANIZATIONAL_UNIT,
        description="",
        structured={
            "areas": [],
            "budget_min": None,
            "budget_max": None,
            "trl_min": None,
            "trl_max": None,
            "must_include": must_include or [],
            "hard_exclusions": [],
        },
        created_by=u.id,
    )
    s.add(p)
    s.flush()
    s.add(Subscription(user_id=u.id, profile_id=p.id, digest_enabled=True))
    s.flush()
    return u.id, p.id


def _seed_call(
    s,
    *,
    title,
    score=None,
    profile_id=None,
    summary=None,
    keywords=None,
    deadline=None,
    published_at=None,
    full_text=None,
):
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(
        source_id=src.id,
        external_id=title,
        url=f"https://x/{title}",
        title=title,
        summary=summary,
        full_text=full_text,
        keywords=keywords or [],
        deadline=deadline,
        published_at=published_at or datetime.now(UTC),
    )
    s.add(c)
    s.flush()
    if score is not None and profile_id is not None:
        s.add(
            CallRanking(
                call_id=c.id,
                profile_id=profile_id,
                aggregate_score=score,
                rationale="r",
                model="gemma4:e4b-q8",
                prompt_version="v2.0",
                ranked_at=datetime.now(UTC),
            )
        )
        s.flush()
    return c.id


def test_assemble_returns_none_when_no_subscriptions(pg_session_factory, now):
    with pg_session_factory() as s:
        u = User(email="lonely@ki.se", status="active")
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


def test_assemble_top_bucket_takes_top_5_above_threshold(pg_session_factory, now):
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
            s,
            title="Pathology AI breakthrough",
            score=20,
            profile_id=pid,
        )
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    assert payload is not None
    must = [e for e in payload.sections[0].entries if e.bucket == "must_include"]
    assert len(must) == 1
    assert "Pathology" in must[0].title


def test_assemble_must_include_unranked_call_still_surfaced(pg_session_factory, now):
    """OUTER JOIN: a must-include match with NO CallRanking row should still
    appear in the must_include bucket (score=None, rationale=None)."""
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s, must_include=["genomics"])
        # No score / no profile_id passed → no CallRanking row created.
        _seed_call(s, title="Genomics call (unranked)")
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    assert payload is not None
    must = [e for e in payload.sections[0].entries if e.bucket == "must_include"]
    assert len(must) == 1
    assert must[0].title == "Genomics call (unranked)"
    assert must[0].score is None
    assert must[0].rationale is None


def test_assemble_must_include_finds_match_in_full_text(pg_session_factory, now):
    """Codex Plan F regression: after Plan F moved Vinnova's substantive
    prose out of `summary` and into `full_text`, the digest must-include
    bucket MUST surface calls where the keyword appears only in the body
    text. Otherwise the user's hard filter silently misses real matches."""
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s, must_include=["ferroptosis"])
        # Title/summary deliberately don't contain the keyword — only full_text does.
        _seed_call(
            s,
            title="A study on cell death",
            summary="Brief abstract with no special terms.",
            full_text="Detailed paragraph mentioning ferroptosis as a key mechanism.",
        )
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    assert payload is not None
    must = [e for e in payload.sections[0].entries if e.bucket == "must_include"]
    assert len(must) == 1
    assert must[0].title == "A study on cell death"


def test_assemble_must_include_matches_keywords(pg_session_factory, now):
    """Spec line 417: must-include matches title/summary/KEYWORDS."""
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s, must_include=["genomics"])
        # title and summary have no match — only the keywords array does
        _seed_call(
            s,
            title="Funding announcement",
            summary="Generic blurb.",
            keywords=["genomics", "biobank"],
            score=20,
            profile_id=pid,
        )
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    assert payload is not None
    must = [e for e in payload.sections[0].entries if e.bucket == "must_include"]
    assert len(must) == 1


def test_assemble_dedup_must_include_beats_top_in_other_profile(pg_session_factory, now):
    """Pin the bucket-priority decision: when the same call is must_include
    in P1 (low score) and top in P2 (high score), it goes to P1's must-include
    section. This is by design — must-include is the user's hard filter and
    overrides score-based ranking. Spec line 420 says 'highest-scoring profile
    wins' but that's score within bucket; must-include is a separate signal."""
    with pg_session_factory() as s:
        u = User(email="bp@ki.se", status="active")
        s.add(u)
        s.flush()
        p_must = Profile(
            name="MustProfile",
            kind=ProfileKind.ORGANIZATIONAL_UNIT,
            description="",
            structured={
                "areas": [],
                "budget_min": None,
                "budget_max": None,
                "trl_min": None,
                "trl_max": None,
                "must_include": ["pathology"],
                "hard_exclusions": [],
            },
            created_by=u.id,
        )
        p_top = Profile(
            name="TopProfile",
            kind=ProfileKind.ORGANIZATIONAL_UNIT,
            description="",
            structured={
                "areas": [],
                "budget_min": None,
                "budget_max": None,
                "trl_min": None,
                "trl_max": None,
                "must_include": [],
                "hard_exclusions": [],
            },
            created_by=u.id,
        )
        s.add_all([p_must, p_top])
        s.flush()
        s.add_all(
            [
                Subscription(user_id=u.id, profile_id=p_must.id, digest_enabled=True),
                Subscription(user_id=u.id, profile_id=p_top.id, digest_enabled=True),
            ]
        )
        cid = _seed_call(s, title="Pathology breakthrough", score=85, profile_id=p_top.id)
        # Also a low-score ranking against p_must so the call appears in both candidate sets
        s.add(
            CallRanking(
                call_id=cid,
                profile_id=p_must.id,
                aggregate_score=20,
                rationale="r",
                model="gemma4:e4b-q8",
                prompt_version="v2.0",
                ranked_at=datetime.now(UTC),
            )
        )
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
        u = User(email="legacy@ki.se", status="active")
        s.add(u)
        s.flush()
        p = Profile(
            name="Legacy",
            kind=ProfileKind.ORGANIZATIONAL_UNIT,
            description="",
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


def test_assemble_prior_ids_bounded_to_78_weeks(pg_session_factory, now):
    """A DEADLINE-BEARING DigestRun row older than the 78-week window should not
    contribute to the bounded prior_ids set, so a still-open call re-surfaces
    (the bound limits how many stale ids we load over years). Rolling
    NULL-deadline calls are instead excluded UNBOUNDED — see
    test_assemble_rolling_call_not_resent_after_78_weeks."""
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s)
        # Deadline-bearing + still open, so it's eligible and NOT caught by the
        # unbounded rolling-call (NULL-deadline) exclusion.
        cid_ancient = _seed_call(
            s, title="Ancient", score=85, profile_id=pid, deadline=now + timedelta(weeks=4)
        )
        # A 79-week-old DigestRun: just past the bound, so it drops out of
        # prior_ids and the call is surfaced again.
        ancient_run = DigestRun(
            user_id=uid,
            included_call_ids=[cid_ancient],
            sent_at=now - timedelta(weeks=79),
        )
        s.add(ancient_run)
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    titles = [e.title for sec in payload.sections for e in sec.entries]
    assert "Ancient" in titles


def test_assemble_rolling_call_not_resent_after_78_weeks(pg_session_factory, now):
    """Regression: a rolling (NULL-deadline) call previously sent MORE than 78
    weeks ago must NOT be re-sent. Rolling calls never expire, so the 78-week
    prior_ids window can't bound them — they would re-surface as 'new' once the
    DigestRun ages out. They are now excluded unbounded by id."""
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s)
        cid = _seed_call(s, title="Rolling", score=85, profile_id=pid)  # deadline=None
        s.add(
            DigestRun(
                user_id=uid,
                included_call_ids=[cid],
                sent_at=now - timedelta(weeks=80),
            )
        )
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    titles = [] if payload is None else [e.title for sec in payload.sections for e in sec.entries]
    assert "Rolling" not in titles


def test_assemble_long_deadline_call_not_resent_after_26_weeks(pg_session_factory, now):
    """Regression: a call with a long (9-12 month) deadline that was sent in a
    digest 30 weeks ago must NOT be re-sent. The old 26-week prior_ids bound
    dropped it while `Call.deadline > now` still kept it eligible, causing a
    duplicate send. The 78-week window keeps it excluded."""
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s)
        # Long-deadline call: still open (deadline ~5 months out), but it was
        # first surfaced 30 weeks ago — outside the OLD 26-week window, inside
        # the new 78-week one.
        cid = _seed_call(
            s,
            title="LongDeadline",
            score=85,
            profile_id=pid,
            deadline=now + timedelta(weeks=20),
        )
        s.add(
            DigestRun(
                user_id=uid,
                included_call_ids=[cid],
                sent_at=now - timedelta(weeks=30),
            )
        )
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    titles = [] if payload is None else [e.title for sec in payload.sections for e in sec.entries]
    assert "LongDeadline" not in titles


def test_assemble_excludes_past_deadline(pg_session_factory, now):
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s)
        _seed_call(
            s,
            title="Future",
            score=80,
            profile_id=pid,
            deadline=now + timedelta(days=10),
        )
        _seed_call(
            s,
            title="Past",
            score=80,
            profile_id=pid,
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


def test_assemble_dedup_across_profiles_keeps_highest_score(pg_session_factory, now):
    """Same call ranked against two profiles. Lands once, in the higher
    section. The other profile's section gets an `also_in_profiles` note."""
    with pg_session_factory() as s:
        u = User(email="multi@ki.se", status="active")
        s.add(u)
        s.flush()
        p_low = Profile(
            name="LowProfile",
            kind=ProfileKind.ORGANIZATIONAL_UNIT,
            description="",
            structured={
                "areas": [],
                "budget_min": None,
                "budget_max": None,
                "trl_min": None,
                "trl_max": None,
                "must_include": [],
                "hard_exclusions": [],
            },
            created_by=u.id,
        )
        p_high = Profile(
            name="HighProfile",
            kind=ProfileKind.ORGANIZATIONAL_UNIT,
            description="",
            structured={
                "areas": [],
                "budget_min": None,
                "budget_max": None,
                "trl_min": None,
                "trl_max": None,
                "must_include": [],
                "hard_exclusions": [],
            },
            created_by=u.id,
        )
        s.add_all([p_low, p_high])
        s.flush()
        s.add_all(
            [
                Subscription(user_id=u.id, profile_id=p_low.id, digest_enabled=True),
                Subscription(user_id=u.id, profile_id=p_high.id, digest_enabled=True),
            ]
        )
        s.flush()
        cid = _seed_call(s, title="Shared", score=70, profile_id=p_low.id)
        s.add(
            CallRanking(
                call_id=cid,
                profile_id=p_high.id,
                aggregate_score=85,
                rationale="r",
                model="gemma4:e4b-q8",
                prompt_version="v2.0",
                ranked_at=datetime.now(UTC),
            )
        )
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


def test_assemble_must_include_whitespace_term_does_not_match_all(pg_session_factory, now):
    """A whitespace-only must_include term must not ilike-match every call."""
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s, must_include=["   "])
        _seed_call(s, title="Random", score=20, profile_id=pid)
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    # No must-include match → score 20 is below borderline → nothing to send
    assert payload is None


def test_assemble_must_include_escapes_like_wildcards(pg_session_factory, now):
    """'100%' must match literally, not as a wildcard."""
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s, must_include=["100%"])
        _seed_call(
            s,
            title="Funding with 100% match rate",
            score=20,
            profile_id=pid,
        )
        _seed_call(
            s,
            title="Unrelated call",
            score=20,
            profile_id=pid,
        )
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    assert payload is not None
    titles = [e.title for sec in payload.sections for e in sec.entries]
    assert "Funding with 100% match rate" in titles
    assert "Unrelated call" not in titles


def test_assemble_must_include_word_boundary_no_substring(pg_session_factory, now):
    """B11 Defect 1 (digest side): a must_include term must NOT match as a bare
    substring inside a longer word — the digest path uses the same word-boundary
    semantics as the ranking path. The off-topic call that contains the term
    only inside another word is NOT surfaced; a call with the standalone token
    IS. (Runs on real Postgres so the ~* word-boundary regex is exercised.)"""
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s, must_include=["rna"])
        # 'rna' appears only inside 'turnaround' / 'internal' — must NOT match.
        _seed_call(
            s,
            title="Internal turnaround programme",
            summary="An internal study",
            score=20,
            profile_id=pid,
        )
        # Standalone 'RNA' token (case-insensitive) — MUST match.
        _seed_call(
            s,
            title="RNA sequencing grant",
            summary="x",
            score=20,
            profile_id=pid,
        )
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    assert payload is not None
    must = [e for e in payload.sections[0].entries if e.bucket == "must_include"]
    titles = [e.title for e in must]
    assert "RNA sequencing grant" in titles
    assert "Internal turnaround programme" not in titles


def test_assemble_must_include_short_term_ignored(pg_session_factory, now):
    """B11 Defect 1 (digest side): a sub-minimum-length term ('ai') is ignored
    so it can never flood the digest (also fixes the <3-char trigram perf
    finding). The call with a standalone 'AI' token is NOT must-included."""
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s, must_include=["ai"])
        _seed_call(s, title="AI funding call", summary="x", score=20, profile_id=pid)
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    # 'ai' term dropped → no must_include match → score 20 < borderline → nothing.
    assert payload is None


def test_assemble_must_include_word_boundary_matches_keywords(pg_session_factory, now):
    """B11 Defect 1: word-boundary semantics also apply to the keywords field —
    a standalone keyword token still matches, a substring-only one does not."""
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s, must_include=["genomics"])
        _seed_call(
            s,
            title="Funding announcement",
            summary="Generic blurb.",
            keywords=["genomics", "biobank"],
            score=20,
            profile_id=pid,
        )
        # 'genomics' only as a substring of 'metagenomics' keyword → no match.
        _seed_call(
            s,
            title="Other announcement",
            summary="Generic blurb.",
            keywords=["metagenomicsx"],
            score=20,
            profile_id=pid,
        )
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    assert payload is not None
    titles = [e.title for sec in payload.sections for e in sec.entries]
    assert "Funding announcement" in titles
    assert "Other announcement" not in titles


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


# ---------------------------------------------------------------------------
# Defect 3: DIGEST_TOTAL_MUST_INCLUDE_CAP — whole-payload cap across profiles
# ---------------------------------------------------------------------------

def test_total_must_include_cap_across_profiles(pg_session_factory, now):
    """With 3 profiles each having 20 must_include matches the total must_include
    count across all sections must not exceed DIGEST_TOTAL_MUST_INCLUDE_CAP (50).
    Non-must_include entries are never trimmed by this cap."""
    from call_manager.digest.assemble import DIGEST_TOTAL_MUST_INCLUDE_CAP

    with pg_session_factory() as s:
        # Create a single user subscribed to 3 profiles, each with a distinct
        # must_include keyword that matches a pool of 20 calls.
        u = User(email="multicap@ki.se", status="active")
        s.add(u)
        s.flush()

        pids = []
        keywords = ["alphaterm", "betaterm", "gammaterm"]
        for kw in keywords:
            p = Profile(
                name=f"Profile-{kw}",
                kind=ProfileKind.ORGANIZATIONAL_UNIT,
                description="",
                structured={
                    "areas": [],
                    "budget_min": None,
                    "budget_max": None,
                    "trl_min": None,
                    "trl_max": None,
                    "must_include": [kw],
                    "hard_exclusions": [],
                },
                created_by=u.id,
            )
            s.add(p)
            s.flush()
            s.add(Subscription(user_id=u.id, profile_id=p.id, digest_enabled=True))
            s.flush()
            pids.append(p.id)

        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        # 20 calls per keyword (3 × 20 = 60 total), all matching via title
        for kw, pid in zip(keywords, pids, strict=True):
            for i in range(20):
                c = Call(
                    source_id=src.id,
                    external_id=f"{kw}-call-{i}",
                    url=f"https://x/{kw}-{i}",
                    title=f"{kw} funding opportunity {i}",
                    summary="",
                    keywords=[],
                    published_at=datetime.now(UTC),
                )
                s.add(c)
                s.flush()
                # score each call at 20 (below top/borderline) to keep them in must_include only
                s.add(
                    CallRanking(
                        call_id=c.id,
                        profile_id=pid,
                        aggregate_score=20,
                        rationale="r",
                        model="gemma4:e4b-q8",
                        prompt_version="v2.0",
                        ranked_at=datetime.now(UTC),
                    )
                )
        s.commit()
        uid = u.id

    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)

    assert payload is not None
    total_must_include = sum(
        sum(1 for e in sec.entries if e.bucket == "must_include")
        for sec in payload.sections
    )
    assert total_must_include <= DIGEST_TOTAL_MUST_INCLUDE_CAP, (
        f"Expected total must_include entries <= {DIGEST_TOTAL_MUST_INCLUDE_CAP}, "
        f"got {total_must_include}"
    )
