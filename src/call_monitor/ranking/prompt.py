from __future__ import annotations

import json
from dataclasses import dataclass

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


# ---------------------------------------------------------------------------
# v2 — criterion-rubric prompt (topic_fit / eligibility / exclusions)
# ---------------------------------------------------------------------------

PROMPT_VERSION_V2 = "v2.0"
CALL_TEXT_LIMIT_V2 = 4000


class ParseV2Error(Exception):
    """The LLM response didn't match the v2 schema."""


@dataclass(frozen=True)
class ParsedV2:
    topic_fit_rating: int
    topic_fit_rationale: str
    topic_fit_quoted: str | None
    eligibility_rating: str  # 'pass' | 'fail' | 'unclear'
    eligibility_rationale: str
    eligibility_quoted: str | None
    exclusions_rating: str   # 'pass' | 'fail'
    exclusions_rationale: str
    exclusions_quoted: str | None


_SYSTEM_TEMPLATE_V2 = """\
You are a research-funding analyst for an academic hospital.
Evaluate a funding call against this profile on three criteria.

PROFILE:
  Name: {name}
  Kind: {kind}
  Description: {description}
  Research areas: {areas}
  Hard exclusions: {hard_exclusions}

CRITERIA:
  1. topic_fit (integer 0-5): how well the call's scope aligns with
     the profile's research areas. 5 = direct match on a named area;
     3 = adjacent/applicable; 1 = tangential; 0 = unrelated.
  2. eligibility (pass | fail | unclear): whether a profile of this
     kind would be eligible to apply. fail = clearly ineligible based
     on a stated requirement. unclear = cannot determine from the text.
  3. exclusions (pass | fail): whether the call violates any of the
     profile's hard exclusions.

For topic_fit and for "fail" judgments on eligibility/exclusions,
quote the single sentence from the CALL that most supports your
judgment (verbatim, max 200 chars). If no such sentence exists for
eligibility or exclusions, set quoted_evidence to null.

Output JSON exactly matching this schema:
{{
  "topic_fit":   {{"rating": int, "rationale": str, "quoted_evidence": str}},
  "eligibility": {{"rating": "pass" | "fail" | "unclear",
                   "rationale": str, "quoted_evidence": str | null}},
  "exclusions":  {{"rating": "pass" | "fail",
                   "rationale": str, "quoted_evidence": str | null}}
}}
"""

_USER_TEMPLATE_V2 = """\
CALL:
  Title:     {title}
  Programme: {programme}
  Deadline:  {deadline}
  Budget:    {budget_min}-{budget_max} {currency}
  Body:      {text}
"""


def build_messages_v2(*, profile, call) -> list[dict]:
    """Build the v2 criterion-rubric prompt. Profile block first so the
    Ollama prefix cache is reused across calls scored against the same
    profile.

    Text source falls back: summary → full_text. v1's prompt.py already
    does this; without the fallback, calls with only full_text (e.g.
    PDF-extracted with no summary field) would be sent to the LLM with
    just a title — exactly the eligibility/exclusion text v2 is supposed
    to surface better.
    """
    structured = profile.structured or {}
    sys = _SYSTEM_TEMPLATE_V2.format(
        name=profile.name,
        kind=profile.kind.value if hasattr(profile.kind, "value") else str(profile.kind),
        description=profile.description or "",
        areas=", ".join(structured.get("areas") or []) or "(none)",
        hard_exclusions=", ".join(structured.get("hard_exclusions") or []) or "(none)",
    )
    text_source = (
        call.summary
        or getattr(call, "full_text", None)
        or ""
    )
    if len(text_source) > CALL_TEXT_LIMIT_V2:
        text_source = text_source[:CALL_TEXT_LIMIT_V2]
    user = _USER_TEMPLATE_V2.format(
        title=call.title or "",
        programme=call.programme or "",
        deadline=call.deadline.isoformat() if call.deadline else "(none)",
        budget_min=call.budget_min if call.budget_min is not None else "?",
        budget_max=call.budget_max if call.budget_max is not None else "?",
        currency=call.currency or "",
        text=text_source or "(no text)",
    )
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": user},
    ]


def parse_v2_response(content: str) -> ParsedV2:
    """Parse the LLM's JSON criterion output. Raises ParseV2Error on shape
    violations. Quote verification is done outside (in rank_pair_v2)."""
    try:
        data = json.loads(content)
    except ValueError as e:
        raise ParseV2Error(f"invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ParseV2Error("response is not a JSON object")

    tf = data.get("topic_fit")
    if not isinstance(tf, dict):
        raise ParseV2Error("topic_fit missing or not an object")
    rating = tf.get("rating")
    if isinstance(rating, bool) or not isinstance(rating, int) or not 0 <= rating <= 5:
        raise ParseV2Error(f"topic_fit.rating must be int 0-5; got {rating!r}")

    el = data.get("eligibility")
    if not isinstance(el, dict):
        raise ParseV2Error("eligibility missing or not an object")
    el_rating = el.get("rating")
    if el_rating not in {"pass", "fail", "unclear"}:
        raise ParseV2Error(f"eligibility.rating must be 'pass'|'fail'|'unclear'; got {el_rating!r}")

    ex = data.get("exclusions")
    if not isinstance(ex, dict):
        raise ParseV2Error("exclusions missing or not an object")
    ex_rating = ex.get("rating")
    if ex_rating not in {"pass", "fail"}:
        raise ParseV2Error(f"exclusions.rating must be 'pass'|'fail'; got {ex_rating!r}")

    return ParsedV2(
        topic_fit_rating=rating,
        topic_fit_rationale=str(tf.get("rationale") or ""),
        topic_fit_quoted=str(tf["quoted_evidence"]) if tf.get("quoted_evidence") else None,
        eligibility_rating=el_rating,
        eligibility_rationale=str(el.get("rationale") or ""),
        eligibility_quoted=str(el["quoted_evidence"]) if el.get("quoted_evidence") else None,
        exclusions_rating=ex_rating,
        exclusions_rationale=str(ex.get("rationale") or ""),
        exclusions_quoted=str(ex["quoted_evidence"]) if ex.get("quoted_evidence") else None,
    )
