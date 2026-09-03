from datetime import UTC, datetime

from sqlalchemy import select

from call_manager.db.models import (
    Call,
    CallRanking,
    GoldenSetTriple,
    Profile,
    ProfileKind,
    Source,
    User,
)
from call_manager.ranking import eval as eval_mod
from call_manager.ranking.eval import run_golden_set_eval


def _seed_pair(s, *, external_id, score, expected):
    u = s.execute(select(User)).scalars().first()
    if u is None:
        u = User(email="g@umu.se")
        s.add(u)
        s.flush()
    p = Profile(
        name=f"P-{external_id}",
        kind=ProfileKind.ORGANIZATIONAL_UNIT,
        description="d",
        structured={},
        created_by=u.id,
    )
    s.add(p)
    s.flush()
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(
        source_id=src.id,
        external_id=external_id,
        url=f"https://x/{external_id}",
        title=external_id,
        published_at=datetime.now(UTC),
    )
    s.add(c)
    s.flush()
    s.add(
        CallRanking(
            call_id=c.id,
            profile_id=p.id,
            aggregate_score=score,
            aggregate_reason=None,
            rationale="r",
            model="m",
            prompt_version="v4.1",
            raw_response={},
            ranked_at=datetime.now(UTC),
        )
    )
    s.add(GoldenSetTriple(call_id=c.id, profile_id=p.id, expected_score=expected))


def test_eval_reads_triples_from_db_when_no_fixture(pg_session_factory, monkeypatch, tmp_path):
    monkeypatch.setattr(eval_mod, "_REPORT_PATH", tmp_path / "golden_set_last.json")

    with pg_session_factory() as s:
        _seed_pair(s, external_id="a", score=20, expected=10)
        _seed_pair(s, external_id="b", score=60, expected=50)
        _seed_pair(s, external_id="c", score=90, expected=95)
        s.commit()

    report = run_golden_set_eval(session_factory=pg_session_factory)
    assert report.n_fixture == 3
    assert report.n_compared == 3
    assert report.tau == 1.0


def test_eval_db_mode_empty_returns_none_tau(pg_session_factory, monkeypatch, tmp_path):
    monkeypatch.setattr(eval_mod, "_REPORT_PATH", tmp_path / "golden_set_last.json")

    report = run_golden_set_eval(session_factory=pg_session_factory)
    assert report.n_fixture == 0
    assert report.tau is None
