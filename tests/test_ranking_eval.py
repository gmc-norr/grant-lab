import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from call_monitor.db.models import (
    Call,
    CallRanking,
    Profile,
    ProfileKind,
    Source,
    User,
)
from call_monitor.ranking.eval import (
    GoldenSetReport,
    kendalls_tau,
    run_golden_set_eval,
)


@pytest.fixture(autouse=True)
def _isolate_report_path(monkeypatch, tmp_path):
    """Redirect _REPORT_PATH to tmp_path for every test in this module so
    tests don't pollute <cwd>/data/golden_set_last.json. Tests that need
    to inspect the report file should still override _REPORT_PATH inside
    the test body to a known path inside tmp_path (which this fixture
    has already pointed at). The override is harmless because monkeypatch
    re-applies cleanly inside a fixture scope."""
    from call_monitor.ranking import eval as eval_mod
    monkeypatch.setattr(eval_mod, "_REPORT_PATH", tmp_path / "auto_isolated.json")
    yield


def test_kendalls_tau_perfect_correlation():
    """Identical orderings → tau = 1."""
    assert kendalls_tau([1, 2, 3, 4], [1, 2, 3, 4]) == 1.0


def test_kendalls_tau_perfect_anticorrelation():
    """Reversed orderings → tau = -1."""
    assert kendalls_tau([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0


def test_kendalls_tau_uncorrelated_zero_centered():
    """Random orderings → tau near 0."""
    tau = kendalls_tau([10, 20, 30, 40, 50], [30, 10, 50, 20, 40])
    assert -0.5 < tau < 0.5


def test_kendalls_tau_handles_ties():
    """Ties shouldn't crash."""
    tau = kendalls_tau([1, 1, 2], [1, 2, 2])
    assert -1 <= tau <= 1


def test_run_golden_set_eval_against_seeded_rankings(pg_session_factory, tmp_path):
    """Build a golden set as a JSON file, seed CallRanking under both schemes,
    run eval, verify the report shape."""
    with pg_session_factory() as s:
        u = User(email="ge@ki.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="GE",
            kind=ProfileKind.GROUP,
            description="",
            structured={"areas": [], "hard_exclusions": []},
            created_by=u.id,
            ranking_scheme="v2",
        )
        s.add(p)
        s.flush()
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        cids = []
        # 4 calls with expected scores [80, 60, 40, 20].
        # v1 ranks them [85, 65, 35, 25] (perfect order).
        # v2 ranks them [70, 80, 30, 40] (worse).
        v1_scores = [85, 65, 35, 25]
        v2_scores = [70, 80, 30, 40]
        expected_scores = [80, 60, 40, 20]
        for i, (_e, v1, v2) in enumerate(
            zip(expected_scores, v1_scores, v2_scores, strict=True)
        ):
            c = Call(
                source_id=src.id,
                external_id=f"GE-{i}",
                url=f"https://x/{i}",
                title=f"GE {i}",
                published_at=datetime.now(UTC),
            )
            s.add(c)
            s.flush()
            cids.append(c.id)
            for scheme, score in [("v1", v1), ("v2", v2)]:
                s.add(
                    CallRanking(
                        call_id=c.id,
                        profile_id=p.id,
                        scheme=scheme,
                        aggregate_score=score,
                        rationale="r",
                        model="m",
                        prompt_version=scheme,
                        ranked_at=datetime.now(UTC),
                    )
                )
        s.commit()
        pid = p.id

    # Build a fixture file at tmp_path/golden.json
    golden = [
        {"call_id": cid, "profile_id": pid, "expected_score": e}
        for cid, e in zip(cids, expected_scores, strict=True)
    ]
    fixture_path = tmp_path / "golden.json"
    fixture_path.write_text(json.dumps(golden))

    report = run_golden_set_eval(
        session_factory=pg_session_factory,
        fixture_path=str(fixture_path),
    )
    assert isinstance(report, GoldenSetReport)
    assert report.tau_v1 == 1.0  # perfect correlation
    assert report.tau_v2 < 1.0   # less perfect
    assert report.n_fixture == 4
    assert report.n_v1 == 4      # all 4 triples had v1 rows
    assert report.n_v2 == 4      # all 4 triples had v2 rows


def test_run_golden_set_eval_per_scheme_n_reflects_actual_rows(
    pg_session_factory, tmp_path
):
    """Codex M2: when triples are missing CallRanking rows for a scheme,
    n_<scheme> reports the count of rows actually compared, NOT the
    fixture size. Otherwise /admin would show tau computed on a much
    smaller subset than the displayed Pairs count."""
    with pg_session_factory() as s:
        u = User(email="m2@ki.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="M2",
            kind=ProfileKind.GROUP,
            description="",
            structured={"areas": [], "hard_exclusions": []},
            created_by=u.id,
            ranking_scheme="v2",
        )
        s.add(p)
        s.flush()
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        cids = []
        # Three calls in fixture, but only the first two have v2 rows.
        for i in range(3):
            c = Call(
                source_id=src.id,
                external_id=f"M2-{i}",
                url=f"https://x/{i}",
                title=f"M2 {i}",
                published_at=datetime.now(UTC),
            )
            s.add(c)
            s.flush()
            cids.append(c.id)
        for cid in cids[:2]:
            s.add(
                CallRanking(
                    call_id=cid,
                    profile_id=p.id,
                    scheme="v2",
                    aggregate_score=50,
                    rationale="r",
                    model="m",
                    prompt_version="v2.0",
                    ranked_at=datetime.now(UTC),
                )
            )
        s.commit()
        pid = p.id

    golden = [
        {"call_id": cid, "profile_id": pid, "expected_score": s}
        for cid, s in zip(cids, [80, 60, 40], strict=True)
    ]
    fixture_path = tmp_path / "golden.json"
    fixture_path.write_text(json.dumps(golden))

    report = run_golden_set_eval(
        session_factory=pg_session_factory,
        fixture_path=str(fixture_path),
    )
    assert report.n_fixture == 3
    assert report.n_v1 == 0      # no v1 rows seeded
    assert report.tau_v1 is None
    assert report.n_v2 == 2      # only 2 of 3 fixture triples had v2 rows
    # tau_v2 computed over those 2 rows (could be -1, 0, or 1 with n=2;
    # we just confirm it's not None and the count is honest).
    assert report.tau_v2 is not None


def test_run_golden_set_eval_missing_fixture_returns_zeroed_report(
    pg_session_factory, tmp_path, caplog,
):
    """If the fixture file does not exist, the evaluator returns a fully
    zeroed report (n_fixture=0, n_v1/v2=0, tau_v1/v2=None) and logs a
    structured warning. The whole pipeline must not crash."""
    import logging

    nonexistent = tmp_path / "does-not-exist.json"
    assert not nonexistent.exists()

    with caplog.at_level(logging.WARNING, logger="call_monitor.ranking.eval"):
        report = run_golden_set_eval(
            session_factory=pg_session_factory,
            fixture_path=str(nonexistent),
        )

    assert report == GoldenSetReport(
        n_fixture=0, n_v1=0, tau_v1=None, n_v2=0, tau_v2=None,
    )
    assert any(
        "golden_fixture_missing" in (r.getMessage() or "")
        or "ranking.golden_eval.fixture_missing" in r.message
        for r in caplog.records
    )


def test_run_golden_set_eval_dedups_repeated_triples(pg_session_factory, tmp_path):
    """If the fixture lists the same (call_id, profile_id) twice, dedup so
    tau isn't biased by double-weighting. First occurrence wins."""
    with pg_session_factory() as s:
        u = User(email="dup@ki.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="Dup", kind=ProfileKind.GROUP, description="",
            structured={"areas": [], "hard_exclusions": []},
            created_by=u.id, ranking_scheme="v2",
        )
        s.add(p)
        s.flush()
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id, external_id="DUP", url="https://x", title="DUP",
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        s.add(CallRanking(
            call_id=c.id, profile_id=p.id, scheme="v2",
            aggregate_score=50, rationale="r",
            model="m", prompt_version="v2.0", ranked_at=datetime.now(UTC),
        ))
        s.commit()
        cid, pid = c.id, p.id

    fixture_path = tmp_path / "dups.json"
    # Same (call_id, profile_id) listed three times.
    fixture_path.write_text(json.dumps([
        {"call_id": cid, "profile_id": pid, "expected_score": 80},
        {"call_id": cid, "profile_id": pid, "expected_score": 70},
        {"call_id": cid, "profile_id": pid, "expected_score": 60},
    ]))

    report = run_golden_set_eval(
        session_factory=pg_session_factory, fixture_path=str(fixture_path),
    )
    assert report.n_fixture == 1  # deduped
    assert report.n_v2 == 1


def test_run_golden_set_eval_persists_report_to_disk(
    pg_session_factory, tmp_path, monkeypatch,
):
    """After computing the report, write it to disk so /admin can read
    the latest result without re-running the eval."""
    from call_monitor.ranking import eval as eval_mod
    # Override the persist path to a tmp location.
    monkeypatch.setattr(
        eval_mod, "_REPORT_PATH", tmp_path / "golden_set_last.json",
    )

    # Run with empty fixture → zeroed report; still gets persisted.
    fixture = tmp_path / "empty.json"
    fixture.write_text("[]")
    report = run_golden_set_eval(
        session_factory=pg_session_factory, fixture_path=str(fixture),
    )
    persisted = json.loads((tmp_path / "golden_set_last.json").read_text())
    assert persisted["n_fixture"] == report.n_fixture == 0
    assert persisted["tau_v1"] is None
    assert persisted["tau_v2"] is None


def test_load_last_report_returns_none_when_never_persisted(
    monkeypatch, tmp_path,
):
    """If the report file doesn't exist, load_last_report returns None."""
    from call_monitor.ranking import eval as eval_mod
    monkeypatch.setattr(
        eval_mod, "_REPORT_PATH", tmp_path / "nope.json",
    )
    from call_monitor.ranking.eval import load_last_report
    assert load_last_report() is None


def test_load_last_report_reads_persisted_report(monkeypatch, tmp_path):
    """Round-trip: persist then load returns the same report."""
    from call_monitor.ranking import eval as eval_mod
    p = tmp_path / "last.json"
    monkeypatch.setattr(eval_mod, "_REPORT_PATH", p)

    p.write_text(json.dumps({
        "n_fixture": 50,
        "n_v1": 50,
        "tau_v1": 0.789,
        "n_v2": 48,
        "tau_v2": 0.812,
    }))
    from call_monitor.ranking.eval import load_last_report
    r = load_last_report()
    assert r is not None
    assert r.n_fixture == 50
    assert r.n_v1 == 50
    assert r.tau_v1 == 0.789
    assert r.n_v2 == 48
    assert r.tau_v2 == 0.812


def test_load_last_report_returns_none_on_corrupted_file(monkeypatch, tmp_path):
    """Corrupted JSON in the report file must not crash /admin — return None."""
    from call_monitor.ranking import eval as eval_mod
    p = tmp_path / "corrupt.json"
    p.write_text("{not json at all")  # truncated/invalid JSON
    monkeypatch.setattr(eval_mod, "_REPORT_PATH", p)
    from call_monitor.ranking.eval import load_last_report
    assert load_last_report() is None
