from __future__ import annotations

from call_monitor.db.models import Call, Profile

# Bump only when the prompt structure changes in a way that warrants
# re-ranking. Stored on every CallRanking row so historical scores stay
# attributable to the regime that produced them.
PROMPT_VERSION = "v1"

_SYSTEM_TEMPLATE = """\
You are a research-funding analyst for an academic hospital.
Score how relevant a funding call is to this profile.

PROFILE:
  Name: {name}
  Kind: {kind}
  Description: {description}
  Structured constraints:
    Areas: {areas}
    Budget range: {budget_range}
    TRL: {trl_range}
    Must include (force-include keywords): {must_include}
    Hard exclusions: {hard_exclusions}

Output a single JSON object with two keys:
  {{"score": <integer 0-100>, "rationale": "<2-3 sentences>"}}

Hard exclusions: if the call clearly hits one, score MUST be <= 10.
"""

_USER_TEMPLATE = """\
CALL:
  Title: {title}
  Programme: {programme}
  Deadline: {deadline}
  Budget: {budget}
  Summary: {summary}
"""


def _fmt_list(value) -> str:
    if not value:
        return "(none)"
    return ", ".join(str(v) for v in value)


def _budget_range(structured: dict) -> str:
    lo, hi = structured.get("budget_min"), structured.get("budget_max")
    if lo is None and hi is None:
        return "(unspecified)"
    return f"{lo or '—'}–{hi or '—'}"


def _trl_range(structured: dict) -> str:
    lo, hi = structured.get("trl_min"), structured.get("trl_max")
    if lo is None and hi is None:
        return "(unspecified)"
    return f"{lo or '—'}–{hi or '—'}"


def _summary_text(call: Call) -> str:
    """Prefer summary; fall back to first 2000 chars of full_text."""
    if call.summary:
        return call.summary
    if call.full_text:
        return call.full_text[:2000]
    return "(no summary)"


def build_messages(profile: Profile, call: Call) -> list[dict]:
    s = profile.structured or {}
    system = _SYSTEM_TEMPLATE.format(
        name=profile.name,
        kind=profile.kind.value if hasattr(profile.kind, "value") else profile.kind,
        description=profile.description or "(no description)",
        areas=_fmt_list(s.get("areas")),
        budget_range=_budget_range(s),
        trl_range=_trl_range(s),
        must_include=_fmt_list(s.get("must_include")),
        hard_exclusions=_fmt_list(s.get("hard_exclusions")),
    )

    deadline = (
        call.deadline.isoformat()
        if call.deadline is not None
        else "(no deadline)"
    )
    budget = (
        f"{call.budget_min or '—'}–{call.budget_max or '—'} {call.currency or ''}"
        if (call.budget_min or call.budget_max)
        else "(unspecified)"
    )
    user = _USER_TEMPLATE.format(
        title=call.title,
        programme=call.programme or "(unspecified)",
        deadline=deadline,
        budget=budget,
        summary=_summary_text(call),
    )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
