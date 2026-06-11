import json
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


def _seed(s, *, external_id, score, expected, pv):
    u = User(email=f"{external_id}@ki.se")
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
            prompt_version=pv,
            raw_response={},
            ranked_at=datetime.now(UTC),
        )
    )
    s.add(GoldenSetTriple(call_id=c.id, profile_id=p.id, expected_score=expected))


def test_history_jsonl_appends_one_line_with_prompt_version_counts(
    pg_session_factory, monkeypatch, tmp_path
):
    monkeypatch.setattr(eval_mod, "_REPORT_PATH", tmp_path / "golden_set_last.json")
    monkeypatch.setattr(eval_mod, "_HISTORY_PATH", tmp_path / "golden_set_history.jsonl")

    with pg_session_factory() as s:
        _seed(s, external_id="a", score=20, expected=10, pv="v4.1")
        _seed(s, external_id="b", score=80, expected=90, pv="v5.0")
        s.commit()

    eval_mod.run_golden_set_eval(session_factory=pg_session_factory)
    eval_mod.run_golden_set_eval(session_factory=pg_session_factory)

    history = (tmp_path / "golden_set_history.jsonl").read_text().splitlines()
    assert len(history) == 2  # one line appended per run
    row = json.loads(history[-1])
    assert row["n_compared"] == 2
    assert row["prompt_version_counts"] == {"v4.1": 1, "v5.0": 1}
    assert "timestamp" in row and "tau" in row
