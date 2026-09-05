import json
from datetime import UTC, datetime

from sqlalchemy import func, select

from grant_lab.db.models import (
    Call,
    CallRanking,
    Profile,
    ProfileKind,
    Source,
    User,
)
from grant_lab.ranking.validate_ev import run_validation


class _FakeOllama:
    """Returns a fixed clean envelope with a 2-digit logprob distribution at
    the rating, so EV extraction succeeds deterministically."""

    model = "gemma4-fake"

    def chat(self, *, messages, response_format=None, logprobs=False, top_logprobs=None):
        import math

        content = (
            '{"topic_fit": {"rating": 4, "rationale": "x"}, '
            '"eligibility": {"rating": "pass"}, "exclusions": {"rating": "pass"}}'
        )
        toks = []
        marker = '"rating":'
        pos = content.index(marker) + len(marker)
        while content[pos] == " ":
            pos += 1
        for i, ch in enumerate(content):
            t = {
                "token": ch,
                "bytes": list(ch.encode()),
                "logprob": -0.01,
                "top_logprobs": [{"token": ch, "logprob": -0.01, "bytes": list(ch.encode())}],
            }
            if i == pos and ch == "4":
                t["top_logprobs"] = [
                    {"token": "4", "logprob": math.log(0.7), "bytes": [52]},
                    {"token": "3", "logprob": math.log(0.3), "bytes": [51]},
                ]
            toks.append(t)
        return {
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "logprobs": {"content": toks},
                    "finish_reason": "stop",
                }
            ],
        }


def _seed_ranked_pair(s, ext):
    u = User(email=f"{ext}@umu.se")
    s.add(u)
    s.flush()
    p = Profile(
        name=f"P{ext}",
        kind=ProfileKind.ORGANIZATIONAL_UNIT,
        description="d",
        structured={"areas": ["AI"]},
        created_by=u.id,
    )
    s.add(p)
    s.flush()
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(
        source_id=src.id,
        external_id=ext,
        url=f"https://x/{ext}",
        title="AI",
        summary="AI funding",
        deadline=datetime(2099, 1, 1, tzinfo=UTC),
        published_at=datetime.now(UTC),
    )
    s.add(c)
    s.flush()
    s.add(
        CallRanking(
            call_id=c.id,
            profile_id=p.id,
            aggregate_score=80,
            aggregate_reason=None,
            rationale="r",
            model="gemma4",
            prompt_version="v4.1",
            raw_response={},
            ranked_at=datetime.now(UTC),
        )
    )


def test_validation_threads_settings_thresholds_into_gates(
    pg_session_factory, tmp_path, monkeypatch
):
    """B11 Defect 5: validate_ev must read the embedding thresholds from
    Settings (pregate + hard_fail) instead of a hardcoded 0.30, and pass BOTH
    into evaluate_gates — otherwise the EV validation gates on a different
    cutoff than production."""
    monkeypatch.setenv("CALL_MANAGER_DATA_DIR", str(tmp_path))
    with pg_session_factory() as s:
        _seed_ranked_pair(s, "x")
        s.commit()

    from grant_lab.ranking import validate_ev as ve_mod

    seen = {}
    original = ve_mod.evaluate_gates

    def _capture(profile, call, **kwargs):
        seen["embedding_threshold"] = kwargs.get("embedding_threshold")
        seen["embedding_hard_fail_threshold"] = kwargs.get("embedding_hard_fail_threshold")
        return original(profile, call, **kwargs)

    monkeypatch.setattr(ve_mod, "evaluate_gates", _capture)

    run_validation(
        session_factory=pg_session_factory,
        client=_FakeOllama(),
        sample=10,
        data_dir=tmp_path,
        embedding_threshold=0.42,
        embedding_hard_fail_threshold=0.25,
    )
    assert seen["embedding_threshold"] == 0.42
    assert seen["embedding_hard_fail_threshold"] == 0.25


def test_validation_writes_report_and_does_not_write_db(pg_session_factory, tmp_path, monkeypatch):
    monkeypatch.setenv("CALL_MANAGER_DATA_DIR", str(tmp_path))
    with pg_session_factory() as s:
        for ext in ("a", "b", "c"):
            _seed_ranked_pair(s, ext)
        s.commit()
        before = s.execute(select(func.max(CallRanking.aggregate_score))).scalar_one()

    report_path = run_validation(
        session_factory=pg_session_factory,
        client=_FakeOllama(),
        sample=10,
        data_dir=tmp_path,
    )
    data = json.loads(report_path.read_text())
    assert data["n_sampled"] == 3
    assert "populated_fraction" in data and "distinct_scores_40_100" in data
    assert "within_one_point_fraction" in data
    # Zero DB writes: scores unchanged, no new rows.
    with pg_session_factory() as s:
        after = s.execute(select(func.max(CallRanking.aggregate_score))).scalar_one()
        n = s.execute(select(func.count()).select_from(CallRanking)).scalar_one()
    assert after == before
    assert n == 3
