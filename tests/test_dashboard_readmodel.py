import pytest

from grant_lab.db.models import (
    Call,
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
        call, _ = view.calls[0]
        # The scalars CallOut actually reads are present and correct.
        assert call.title == "Heavy call"
        assert call.source.slug == f"heavy-src-{'r' if ranked else 'u'}"
        for attr in ("embedding", "full_text", "raw"):
            with pytest.raises(InvalidRequestError):
                getattr(call, attr)
