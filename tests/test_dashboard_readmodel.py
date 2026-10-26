from datetime import UTC, datetime, timedelta

import pytest

from grant_lab.db.models import (
    Call,
    CallRanking,
    Profile,
    ProfileKind,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
)
from grant_lab.web.api.calls import (
    EffectiveOut,
    PaginationOut,
    RankingOut,
    SummaryOut,
)
from grant_lab.web.dashboard import build_dashboard_view, resolve_profile_selection


def _seed(pg_session_factory):
    with pg_session_factory() as s:
        u = User(status="active", email="u@umu.se")
        s.add(u)
        s.commit()
        return u.id


def _seed_user_and_profile(pg_session_factory, *, profile_active=True):
    with pg_session_factory() as s:
        u = User(status="active", email="u@umu.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="Grp",
            kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            level="group",
            description="d",
            structured={},
            created_by=u.id,
            is_active=profile_active,
        )
        s.add(p)
        s.commit()
        return u.id, p.id


def test_resolve_readonly_does_not_persist(pg_session_factory):
    uid = _seed(pg_session_factory)
    with pg_session_factory() as s:
        resolve_profile_selection(s, user_id=uid, raw_profile="999999", persist=False)
        s.commit()
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id is None


def test_build_view_shape(pg_session_factory):
    uid = _seed(pg_session_factory)
    with pg_session_factory() as s:
        view = build_dashboard_view(
            s,
            user_id=uid,
            source=None,
            sort="newest",
            profile_id=None,
            limit=50,
            page=1,
            show_closed=False,
        )
    assert view.ranking == {"pending": 0, "failed": 0}
    assert view.pagination["page"] == 1
    assert isinstance(view.calls, list)
    assert "open_count" in view.summary


def test_absent_profile_reads_stored_default_without_persisting(pg_session_factory):
    uid, pid = _seed_user_and_profile(pg_session_factory)
    with pg_session_factory() as s:
        s.get(User, uid).default_profile_id = pid
        s.commit()
    with pg_session_factory() as s:
        resolved = resolve_profile_selection(s, user_id=uid, raw_profile=None, persist=False)
        s.commit()
    assert resolved == pid
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id == pid  # unchanged


def test_absent_profile_with_inactive_default_resolves_none(pg_session_factory):
    uid, pid = _seed_user_and_profile(pg_session_factory, profile_active=False)
    with pg_session_factory() as s:
        s.get(User, uid).default_profile_id = pid
        s.commit()
    with pg_session_factory() as s:
        resolved = resolve_profile_selection(s, user_id=uid, raw_profile=None, persist=False)
    assert resolved is None


def test_build_view_reports_nonzero_ranking_counts(pg_session_factory):
    uid, pid = _seed_user_and_profile(pg_session_factory)
    with pg_session_factory() as s:
        src = Source(name="Src", slug="src-readmodel-ranking")
        s.add(src)
        s.flush()
        c1 = Call(
            source_id=src.id,
            external_id="ext-pending",
            url="https://example.test/1",
            title="Pending call",
        )
        c2 = Call(
            source_id=src.id,
            external_id="ext-failed",
            url="https://example.test/2",
            title="Failed call",
        )
        s.add_all([c1, c2])
        s.flush()
        # One PENDING job and one unresolved FAILED job (no CallRanking row for
        # its (call_id, profile_id) pair) → counts as {pending: 1, failed: 1}.
        s.add(RankingJob(call_id=c1.id, profile_id=pid, status=RankingJobStatus.PENDING))
        s.add(RankingJob(call_id=c2.id, profile_id=pid, status=RankingJobStatus.FAILED))
        s.commit()
    with pg_session_factory() as s:
        view = build_dashboard_view(
            s,
            user_id=uid,
            source=None,
            sort="newest",
            profile_id=pid,
            limit=50,
            page=1,
            show_closed=False,
        )
    assert view.ranking == {"pending": 1, "failed": 1}


def test_the_failed_banner_counts_distinct_open_calls_not_failed_job_rows(pg_session_factory):
    """Review 2026-10-26 M-core-2 / A3, verbatim probe.

    One OPEN unscored call with ten nightly failures, plus one CLOSED unscored
    call with five. The banner ("N calls couldn't be scored automatically")
    reported 15 -- it counted FAILED `ranking_jobs` ROWS and never looked at the
    deadline. Live, profile 1 showed 97 rows over 9 distinct calls, only 2 of
    them open, and the number grew by about 2 a day.

    BOTH legs of the fixture are load-bearing, and they catch different defects:
      * the ten rows on ONE open call catch the ROW-COUNTING defect -- without
        `count(DISTINCT call_id)` this is 10 (or 15) rather than 1;
      * the closed call catches the CLOSED-CALL defect -- `count(DISTINCT
        call_id)` alone still answers 2, because a call whose deadline has
        passed cannot be scored into anything a researcher can apply for.
    A probe with only the open call would pass on the buggy code as soon as the
    DISTINCT landed, which is why the closed call is not set dressing.
    """
    uid, pid = _seed_user_and_profile(pg_session_factory)
    now = datetime.now(UTC)
    with pg_session_factory() as s:
        src = Source(name="Src", slug="src-readmodel-failed-rows")
        s.add(src)
        s.flush()
        open_call = Call(
            source_id=src.id,
            external_id="ext-open-unscored",
            url="https://example.test/open",
            title="Open unscored call",
            deadline=now + timedelta(days=30),
        )
        closed_call = Call(
            source_id=src.id,
            external_id="ext-closed-unscored",
            url="https://example.test/closed",
            title="Closed unscored call",
            deadline=now - timedelta(days=30),
        )
        s.add_all([open_call, closed_call])
        s.flush()
        # NEITHER call has a CallRanking row, so both clear the existing
        # "unresolved failures only" filter and the old query counted every row.
        for _ in range(10):
            s.add(RankingJob(call_id=open_call.id, profile_id=pid, status=RankingJobStatus.FAILED))
        for _ in range(5):
            s.add(
                RankingJob(call_id=closed_call.id, profile_id=pid, status=RankingJobStatus.FAILED)
            )
        s.commit()
    with pg_session_factory() as s:
        view = build_dashboard_view(
            s,
            user_id=uid,
            source=None,
            sort="newest",
            profile_id=pid,
            limit=50,
            page=1,
            show_closed=False,
        )
    assert view.ranking["failed"] == 1


def test_the_failed_banner_ignores_a_closed_call_even_when_it_is_the_only_one(
    pg_session_factory,
):
    """The closed-call leg of A3, ISOLATED from the row-counting leg.

    Five failed rows but ONE distinct call, and it is closed: `count(DISTINCT
    call_id)` without the open restriction answers 1 here, so this test fails on
    a half-fix that only de-duplicated the rows.
    """
    uid, pid = _seed_user_and_profile(pg_session_factory)
    now = datetime.now(UTC)
    with pg_session_factory() as s:
        src = Source(name="Src", slug="src-readmodel-failed-closed-only")
        s.add(src)
        s.flush()
        closed_call = Call(
            source_id=src.id,
            external_id="ext-closed-only",
            url="https://example.test/closed-only",
            title="Closed unscored call",
            deadline=now - timedelta(days=1),
        )
        s.add(closed_call)
        s.flush()
        for _ in range(5):
            s.add(
                RankingJob(call_id=closed_call.id, profile_id=pid, status=RankingJobStatus.FAILED)
            )
        s.commit()
    with pg_session_factory() as s:
        view = build_dashboard_view(
            s,
            user_id=uid,
            source=None,
            sort="newest",
            profile_id=pid,
            limit=50,
            page=1,
            show_closed=False,
        )
    assert view.ranking["failed"] == 0


def _build(session, *, uid, pid):
    return build_dashboard_view(
        session,
        user_id=uid,
        source=None,
        sort="newest",
        profile_id=pid,
        limit=50,
        page=1,
        show_closed=False,
    )


def test_the_failed_banner_drops_a_call_once_it_has_a_ranking(pg_session_factory):
    """The `~scored.exists()` leg of the failed count, pinned on ONE pair.

    "N calls couldn't be scored automatically" means UNRESOLVED failures: a pair
    that failed on some night and was scored on a later one is not a call the
    reader is missing a score for. `_ranking_counts` has always carried a
    correlated `NOT EXISTS` against `call_rankings` for that, and until this test
    nothing asserted it -- `test_build_view_reports_nonzero_ranking_counts` above
    seeds no ranking at all, so the leg could have been deleted and stayed green.

    It is pinned HERE, and not only as a matter of coverage, because the
    2026-10-26 fix added a JOIN to `calls` to that same query (for the open-call
    restriction) and a JOIN is exactly the edit that can silently de-correlate a
    correlated subquery. Without the leg the banner would count every call whose
    score is merely STALE as one that could not be scored -- which is the
    opposite of what the sibling `ranking_failed` marker was added to say.

    TWO PHASES ON THE IDENTICAL FIXTURE, so the `== 0` cannot pass vacuously: the
    `== 1` immediately before it proves this exact call IS counted while the
    ranking is absent, and the only thing that changes between the two
    assertions is the `call_rankings` row. An `== 0` on its own would also be
    green on an empty table, a wrong profile id, or a query that counts nothing
    at all.
    """
    uid, pid = _seed_user_and_profile(pg_session_factory)
    now = datetime.now(UTC)
    with pg_session_factory() as s:
        src = Source(name="Src", slug="src-readmodel-failed-already-scored")
        s.add(src)
        s.flush()
        call = Call(
            source_id=src.id,
            external_id="ext-open-failed-then-scored",
            url="https://example.test/open-scored",
            title="Open call that failed and was later scored",
            deadline=now + timedelta(days=30),
        )
        s.add(call)
        s.flush()
        call_id = call.id
        s.add(RankingJob(call_id=call_id, profile_id=pid, status=RankingJobStatus.FAILED))
        s.commit()

    # PHASE 1 — positive control. An open call with a FAILED job and NO ranking
    # is exactly what the banner counts, so the population this test reasons
    # about is non-empty.
    with pg_session_factory() as s:
        assert _build(s, uid=uid, pid=pid).ranking["failed"] == 1

    # PHASE 2 — the pair now HAS a score. Nothing else changed: same call, same
    # open deadline, same FAILED job row.
    with pg_session_factory() as s:
        s.add(
            CallRanking(
                call_id=call_id,
                profile_id=pid,
                aggregate_score=71,
                model="test-model",
                prompt_version="v1",
            )
        )
        s.commit()
    with pg_session_factory() as s:
        assert _build(s, uid=uid, pid=pid).ranking["failed"] == 0


def test_the_failed_banner_excludes_only_the_scored_call_not_the_whole_profile(
    pg_session_factory,
):
    """The `~scored.exists()` leg is correlated PER CALL, not per profile.

    Two open calls, one FAILED job each, and a `call_rankings` row for only the
    first. The correct answer is 1. The two ways the leg can be wrong give
    different wrong answers, and this fixture separates them from the right one:

      * leg DELETED  -> 2 (both counted; a stale score reads as no score)
      * leg DE-CORRELATED on `call_id` -- `NOT EXISTS (SELECT 1 FROM
        call_rankings WHERE profile_id = :pid)`, the shape a careless JOIN
        rewrite produces -> 0 (one ranking anywhere in the profile silences the
        whole banner)

    `test_the_failed_banner_drops_a_call_once_it_has_a_ranking` above cannot tell
    those two apart on its own: its fixture has a single call, so per-call and
    per-profile correlation agree there.
    """
    uid, pid = _seed_user_and_profile(pg_session_factory)
    now = datetime.now(UTC)
    with pg_session_factory() as s:
        src = Source(name="Src", slug="src-readmodel-failed-per-call")
        s.add(src)
        s.flush()
        scored = Call(
            source_id=src.id,
            external_id="ext-open-scored",
            url="https://example.test/per-call-scored",
            title="Open call with a score",
            deadline=now + timedelta(days=30),
        )
        unscored = Call(
            source_id=src.id,
            external_id="ext-open-unscored-peer",
            url="https://example.test/per-call-unscored",
            title="Open call with no score",
            deadline=now + timedelta(days=30),
        )
        s.add_all([scored, unscored])
        s.flush()
        s.add(RankingJob(call_id=scored.id, profile_id=pid, status=RankingJobStatus.FAILED))
        s.add(RankingJob(call_id=unscored.id, profile_id=pid, status=RankingJobStatus.FAILED))
        s.add(
            CallRanking(
                call_id=scored.id,
                profile_id=pid,
                aggregate_score=71,
                model="test-model",
                prompt_version="v1",
            )
        )
        s.commit()
    with pg_session_factory() as s:
        assert _build(s, uid=uid, pid=pid).ranking["failed"] == 1


def test_resolve_non_int_profile_raises_400(pg_session_factory):
    # Locks the parity-critical control flow the whitespace fix touched: a
    # non-integer ?profile is a 400 (matches legacy _parse_optional_profile_id).
    import pytest
    from fastapi import HTTPException

    uid = _seed(pg_session_factory)
    with pg_session_factory() as s, pytest.raises(HTTPException) as ei:
        resolve_profile_selection(s, user_id=uid, raw_profile="abc", persist=False)
    assert ei.value.status_code == 400


def test_resolve_whitespace_profile_is_none_not_cleared(pg_session_factory):
    # Whitespace-only strips to empty BEFORE the int() parse → treated as a
    # no-selection (returns None, no 400) and never routes through the
    # clear-default branch (which keys on the UNTRIMMED ""). Parity with legacy.
    uid = _seed(pg_session_factory)
    with pg_session_factory() as s:
        assert resolve_profile_selection(s, user_id=uid, raw_profile="   ", persist=False) is None


def test_view_dicts_match_api_model_fields(pg_session_factory):
    # Guard read-model<->API model key parity: a rename on either side fails here.
    uid = _seed(pg_session_factory)
    with pg_session_factory() as s:
        view = build_dashboard_view(
            s,
            user_id=uid,
            source=None,
            sort="newest",
            profile_id=None,
            limit=50,
            page=1,
            show_closed=False,
        )
    assert set(view.summary) == set(SummaryOut.model_fields)
    assert set(view.ranking) == set(RankingOut.model_fields)
    assert set(view.pagination) == set(PaginationOut.model_fields)
    assert set(view.effective) == set(EffectiveOut.model_fields)


def _seed_call_with_heavy_columns(pg_session_factory, *, ranked):
    """A call carrying every column the dashboard read-model must NOT load.

    `ranked` selects which of `build_dashboard_view`'s two mutually exclusive
    query branches the caller will take, so the defer must be proven on both.
    """
    suffix = "r" if ranked else "u"
    with pg_session_factory() as s:
        u = User(status="active", email=f"heavy-{suffix}@umu.se")
        src = Source(slug=f"heavy-src-{suffix}", name="Heavy")
        s.add_all([u, src])
        s.flush()
        pid = None
        if ranked:
            p = Profile(
                name=f"Grp-{suffix}",
                kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
                level="group",
                description="d",
                structured={},
                created_by=u.id,
                is_active=True,
            )
            s.add(p)
            s.flush()
            pid = p.id
        s.add(
            Call(
                source_id=src.id,
                external_id=f"ext-heavy-{suffix}",
                url=f"https://example.test/heavy-{suffix}",
                title="Heavy call",
                full_text="f" * 5000,
                raw={"blob": "r" * 5000},
                embedding=[0.5] * 1024,
                embedding_model="bge-m3",
            )
        )
        s.commit()
        return u.id, pid


@pytest.mark.parametrize("ranked", [False, True])
def test_dashboard_view_never_loads_the_columns_it_never_renders(pg_session_factory, ranked):
    # `embedding`, `full_text` and `raw` are the ONLY three columns a dashboard
    # row must not load; `CallOut` (web/api/calls.py) renders 18 fields and none
    # of them is one of these. Loading them cost 71-78% of the ORM row-load time
    # at 50-200 rows, all of it spent parsing a 1024-dim vector out of text into
    # Python floats for a payload that never contains it. `summary` in
    # particular must NOT join the deferred list (D10) -- every card renders it,
    # so deferring it buys nothing and costs one SELECT per row.
    #
    # They are deferred with `raiseload=True`, so a future consumer that reads
    # one off a dashboard row fails HERE rather than silently issuing one extra
    # SELECT per row. If you need one of these, drop it from the defer list —
    # do not catch this error.
    from sqlalchemy.exc import InvalidRequestError

    uid, pid = _seed_call_with_heavy_columns(pg_session_factory, ranked=ranked)
    with pg_session_factory() as s:
        view = build_dashboard_view(
            s,
            user_id=uid,
            source=None,
            sort="newest",
            profile_id=pid,
            limit=50,
            page=1,
            show_closed=False,
        )
        assert len(view.calls) == 1
        call, _, _ = view.calls[0]
        # The scalars CallOut actually reads are present and correct.
        assert call.title == "Heavy call"
        assert call.source.slug == f"heavy-src-{'r' if ranked else 'u'}"
        for attr in ("embedding", "full_text", "raw"):
            with pytest.raises(InvalidRequestError):
                getattr(call, attr)
