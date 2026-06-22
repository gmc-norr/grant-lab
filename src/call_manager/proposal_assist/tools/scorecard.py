"""submit_scorecard tool impl: the evaluator's ONLY tool. Validates the agent's
per-criterion judgments against the workspace rubric (index identity + name
cross-check), computes the deterministic overall band, and writes the scorecard
JSONB onto the running evaluator_jobs row. Mirrors the propose_* impl contract:
returns {"error": ...} for every expected failure, never raises."""

from __future__ import annotations

from typing import Any

from call_manager.db.models import EvaluatorJob, Workspace
from call_manager.proposal_assist.evaluator.scoring import compute_overall_band
from call_manager.proposal_assist.tools.registry import ToolContext

_BANDS = ("strong", "adequate", "at_risk", "gap")


def _effective_weight(raw: Any) -> tuple[float, float]:
    weight = float(raw) if isinstance(raw, (int, float)) else 0.0
    return weight, (weight if weight > 0 else 1.0)


async def submit_scorecard_impl(
    ctx: ToolContext, *, criteria: list[dict[str, Any]], summary: str
) -> dict[str, Any]:
    with ctx.session_factory() as session:
        ws = session.get(Workspace, ctx.workspace_id)
        rubric = list(ws.rubric_criteria) if ws and ws.rubric_criteria else []
        n = len(rubric)

        # Index identity: must cover 0..n-1 exactly once.
        indexes = [c.get("index") for c in criteria]
        if sorted(i for i in indexes if isinstance(i, int)) != list(range(n)) or len(indexes) != n:
            return {
                "error": f"scorecard must cover criterion indexes 0..{n - 1} exactly once; "
                f"got {indexes}"
            }

        out: list[dict[str, Any]] = []
        for c in criteria:
            idx = c["index"]
            name = c.get("name")
            if name != rubric[idx].get("name"):
                return {
                    "error": f"criterion {idx}: name {name!r} does not match rubric "
                    f"{rubric[idx].get('name')!r}"
                }
            band = c.get("band")
            if band not in _BANDS:
                return {"error": f"criterion {idx}: band must be one of {_BANDS}, got {band!r}"}
            note = (c.get("note") or "").strip()
            if not note:
                return {"error": f"criterion {idx}: note must be a non-empty paragraph"}
            deep_link = c.get("deep_link")
            if deep_link is not None:
                art = deep_link.get("artifact")
                si = deep_link.get("section_index")
                count = {
                    "draft": ctx.eval_draft_section_count,
                    "spec": ctx.eval_spec_section_count,
                }.get(art)
                if count is None or not isinstance(si, int) or not (0 <= si < count):
                    return {"error": f"criterion {idx}: invalid deep_link {deep_link!r}"}
            weight, eff = _effective_weight(rubric[idx].get("weight"))
            out.append(
                {
                    "index": idx,
                    "name": name,
                    "band": band,
                    "note": note,
                    "deep_link": deep_link,
                    "weight": weight,
                    "effective_weight": eff,
                }
            )

        overall = compute_overall_band([(c["band"], c["effective_weight"]) for c in out])
        scorecard = {"criteria": out, "overall_band": overall, "summary": (summary or "").strip()}

        # Guard the write: row-lock the target and require it is THIS workspace's
        # still-running evaluator job, so a stale/mismatched ctx.origin_job_id (or a
        # job the reaper/cancel already failed) can never have a scorecard written
        # onto it. (origin_kind isn't a job column; ctx.origin_kind is fixed to
        # 'evaluator_session' by build_evaluator_adapter — the enforceable guards are
        # workspace identity + running status.)
        job = session.get(EvaluatorJob, ctx.origin_job_id, with_for_update=True)
        if job is None or job.workspace_id != ctx.workspace_id or job.status != "running":
            return {"error": "scorecard target is not an active evaluator run for this workspace"}
        job.scorecard = scorecard
        session.commit()
    return {"stored": True, "overall_band": overall}
