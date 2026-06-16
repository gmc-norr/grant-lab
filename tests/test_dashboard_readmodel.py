from call_manager.db.models import (
    Call,
    Profile,
    ProfileKind,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
)
from call_manager.web.api.calls import (
    EffectiveOut,
    PaginationOut,
    RankingOut,
    SummaryOut,
)
from call_manager.web.dashboard import build_dashboard_view, resolve_profile_selection


def _seed(pg_session_factory):
    with pg_session_factory() as s:
        u = User(status="active", email="u@ki.se")
        s.add(u)
        s.commit()
        return u.id


def _seed_user_and_profile(pg_session_factory, *, profile_active=True):
    with pg_session_factory() as s:
        u = User(status="active", email="u@ki.se")
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
