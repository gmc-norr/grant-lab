"""Non-persisting EV validation. Re-runs the local rubric on a sample of
already-ranked pairs, computes the three P3 acceptance metrics, writes a
timestamped JSON report under CALL_MANAGER_DATA_DIR. Zero DB writes — this
gates the v5.0 version flip without touching production scores (spec §3.1).

    uv run python -m call_manager.ranking.validate_ev --sample 50
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from call_manager.db.models import Call, CallRanking, Profile
from call_manager.ranking.aggregate import LLMResult, aggregate
from call_manager.ranking.criteria import evaluate_gates
from call_manager.ranking.ev import extract_topic_expected
from call_manager.ranking.prompt import build_messages, parse_response
from call_manager.ranking.rank_pair import _build_verification_text, _safe_cosine

_logger = logging.getLogger(__name__)


def run_validation(*, session_factory, client, sample: int, data_dir: Path) -> Path:
    """Sample up to `sample` LLM-scored pairs (stratified by score), re-run the
    local rubric with logprobs in memory, compare EV-score vs argmax-score.
    Returns the path of the written report. No DB writes."""
    rows: list[tuple[int, int]] = []
    with session_factory() as session:
        # Stratify: order by score so the sample spans the range, then take a
        # bounded slice. Only pairs that actually ran the LLM (have a non-empty
        # model and weren't gate-zeroed) are meaningful.
        candidates = session.execute(
            select(CallRanking.call_id, CallRanking.profile_id)
            .where(CallRanking.model != "", CallRanking.aggregate_reason.is_(None))
            .order_by(CallRanking.aggregate_score)
            .limit(sample)
        ).all()
        rows = [(r.call_id, r.profile_id) for r in candidates]

    results = []
    now = datetime.now(UTC)
    for call_id, profile_id in rows:
        with session_factory() as session:
            call = session.get(Call, call_id)
            profile = session.get(Profile, profile_id)
            if call is None or profile is None:
                continue
            messages = build_messages(profile=profile, call=call, enable_analysis=False)
            embed_sim = _safe_cosine(profile, call)
            gates = evaluate_gates(
                profile,
                call,
                now=now,
                embed_similarity=embed_sim,
                embedding_threshold=0.30,
            )
            try:
                envelope = client.chat(
                    messages=messages,
                    response_format={"type": "json_object"},
                    logprobs=True,
                    top_logprobs=10,
                )
                content = envelope["choices"][0]["message"]["content"]
                parsed = parse_response(
                    content,
                    verification_text=_build_verification_text(call),
                )
            except Exception as e:  # noqa: BLE001 — validation must not crash mid-run
                _logger.warning(
                    "validate_ev.pair_failed call=%s profile=%s err=%s",
                    call_id,
                    profile_id,
                    e,
                )
                continue
            ev = extract_topic_expected(envelope, parsed.topic_fit_rating)
            argmax_score = aggregate(
                gates,
                LLMResult(
                    topic_argmax=parsed.topic_fit_rating,
                    eligibility=parsed.eligibility_rating,
                    exclusions=parsed.exclusions_rating,
                ),
            ).score
            ev_score = aggregate(
                gates,
                LLMResult(
                    topic_argmax=parsed.topic_fit_rating,
                    eligibility=parsed.eligibility_rating,
                    exclusions=parsed.exclusions_rating,
                    topic_expected=ev,
                ),
            ).score
            results.append(
                {
                    "call_id": call_id,
                    "profile_id": profile_id,
                    "argmax": parsed.topic_fit_rating,
                    "ev": ev,
                    "argmax_score": argmax_score,
                    "ev_score": ev_score,
                }
            )

    metrics = _metrics(results)
    data_dir.mkdir(parents=True, exist_ok=True)
    out = data_dir / f"ev_validation_{now.strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(
        json.dumps(
            {
                "timestamp": now.isoformat(),
                "n_sampled": len(results),
                **metrics,
                "pairs": results,
            },
            indent=2,
        )
    )
    _logger.info("validate_ev.done n=%s metrics=%s", len(results), metrics)
    return out


def _metrics(results: list[dict]) -> dict:
    n = len(results)
    if n == 0:
        return {
            "populated_fraction": 0.0,
            "distinct_scores_40_100": 0,
            "within_one_point_fraction": 1.0,
        }
    populated = sum(1 for r in results if r["ev"] is not None)
    distinct = len({r["ev_score"] for r in results if 40 <= r["ev_score"] <= 100})
    within = sum(1 for r in results if r["ev"] is None or abs(r["ev"] - r["argmax"]) <= 1.0)
    return {
        "populated_fraction": populated / n,
        "distinct_scores_40_100": distinct,
        "within_one_point_fraction": within / n,
    }


def _build_client():
    from call_manager.config import Settings
    from call_manager.ranking.ollama_client import OllamaClient

    s = Settings()
    return OllamaClient(base_url=s.ollama_url, model=s.gemma_model)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="call_manager.ranking.validate_ev")
    parser.add_argument("--sample", type=int, default=50)
    args = parser.parse_args(argv)

    from call_manager.config import Settings
    from call_manager.db.session import make_engine, make_session_factory

    settings = Settings()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    data_dir = Path(os.environ.get("CALL_MANAGER_DATA_DIR", "data")).resolve()
    try:
        out = run_validation(
            session_factory=session_factory,
            client=_build_client(),
            sample=args.sample,
            data_dir=data_dir,
        )
    finally:
        engine.dispose()
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
