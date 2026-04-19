from __future__ import annotations

import json
import re
from dataclasses import dataclass

# Bump only when the prompt structure changes in a way that warrants
# re-ranking. Stored on every CallRanking row so historical scores stay
# attributable to the regime that produced them.
PROMPT_VERSION = "v3.0"
CALL_TEXT_LIMIT = 16000


class ParseError(Exception):
    """The LLM response didn't match the criterion schema."""


@dataclass(frozen=True)
class Parsed:
    topic_fit_rating: int
    topic_fit_rationale: str
    topic_fit_quoted: str | None
    eligibility_rating: str  # 'pass' | 'fail' | 'unclear'
    eligibility_rationale: str
    eligibility_quoted: str | None
    exclusions_rating: str   # 'pass' | 'fail'
    exclusions_rationale: str
    exclusions_quoted: str | None


_SYSTEM_TEMPLATE = """\
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

_USER_TEMPLATE = """\
CALL:
  Title:     {title}
  Programme: {programme}
  Deadline:  {deadline}
  Budget:    {budget_min}-{budget_max} {currency}
  Body:      {text}
"""


def build_messages(*, profile, call) -> list[dict]:
    """Build the criterion-rubric prompt. Profile block first so the
    Ollama prefix cache is reused across calls scored against the same
    profile.

    Plan F: summary AND full_text are concatenated (when both present)
    so the LLM sees both signals, then truncated to ``CALL_TEXT_LIMIT``
    as a whole. The OR-pick fallback predates Plan F, when full_text was
    rarely populated; with full-text ingestion live, OR-picking would
    silently drop full_text whenever summary is non-empty.
    """
    structured = profile.structured or {}
    sys = _SYSTEM_TEMPLATE.format(
        name=profile.name,
        kind=profile.kind.value if hasattr(profile.kind, "value") else str(profile.kind),
        description=profile.description or "",
        areas=", ".join(structured.get("areas") or []) or "(none)",
        hard_exclusions=", ".join(structured.get("hard_exclusions") or []) or "(none)",
    )
    parts: list[str] = []
    if call.summary:
        parts.append(call.summary)
    full_text = getattr(call, "full_text", None)
    if full_text:
        parts.append(full_text)
    text_source = "\n\n".join(parts) if parts else ""
    if len(text_source) > CALL_TEXT_LIMIT:
        text_source = text_source[:CALL_TEXT_LIMIT]
    user = _USER_TEMPLATE.format(
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


_QUOTED_EVIDENCE_MAX = 200


def _validated_quote(block: dict, criterion: str) -> str | None:
    """Extract quoted_evidence with type validation."""
    if "quoted_evidence" not in block:
        return None
    q = block["quoted_evidence"]
    if q is None:
        return None
    if not isinstance(q, str):
        raise ParseError(
            f"{criterion}.quoted_evidence must be string or null; got {type(q).__name__}"
        )
    return q if q else None  # empty string treated as null


def parse_response(content: str, *, verification_text: str | None = None) -> Parsed:
    """Parse the LLM's JSON criterion output. Raises ParseError on shape
    violations.

    When ``verification_text`` is supplied, each quoted_evidence field is
    truncated to 200 chars and case-insensitive substring-checked against
    that text. Misses are nulled silently — the LLM's rating still stands;
    only the displayed quote is sanitized. The caller is responsible for
    passing the FULL call body (title + summary + full untruncated full_text);
    don't reuse the embedding helper here, which truncates full_text and
    would falsely null quotes from past the truncation point.
    """
    try:
        data = json.loads(content)
    except ValueError as e:
        raise ParseError(f"invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ParseError("response is not a JSON object")

    tf = data.get("topic_fit")
    if not isinstance(tf, dict):
        raise ParseError("topic_fit missing or not an object")
    rating = tf.get("rating")
    if isinstance(rating, bool) or not isinstance(rating, int) or not 0 <= rating <= 5:
        raise ParseError(f"topic_fit.rating must be int 0-5; got {rating!r}")

    el = data.get("eligibility")
    if not isinstance(el, dict):
        raise ParseError("eligibility missing or not an object")
    el_rating = el.get("rating")
    if el_rating not in {"pass", "fail", "unclear"}:
        raise ParseError(f"eligibility.rating must be 'pass'|'fail'|'unclear'; got {el_rating!r}")

    ex = data.get("exclusions")
    if not isinstance(ex, dict):
        raise ParseError("exclusions missing or not an object")
    ex_rating = ex.get("rating")
    if ex_rating not in {"pass", "fail"}:
        raise ParseError(f"exclusions.rating must be 'pass'|'fail'; got {ex_rating!r}")

    # Pre-normalize the haystack once. PDF-derived full_text has hard line
    # breaks, double newlines, and non-breaking spaces; LLMs typically quote
    # in normalized form, so collapse all whitespace runs to a single space
    # for comparison only. The display value returned to the caller is the
    # LLM's original (un-normalized) quote.
    normalized_haystack = (
        re.sub(r"\s+", " ", verification_text).lower()
        if verification_text is not None
        else None
    )

    def _verified_quote(quote: str | None) -> str | None:
        if quote is None:
            return None
        truncated = quote[:_QUOTED_EVIDENCE_MAX]
        if normalized_haystack is None:
            # No verification requested — keep the (truncated) quote as-is.
            return truncated if truncated else None
        # Whitespace-only quotes match almost any haystack but are useless.
        if not truncated.strip():
            return None
        normalized_quote = re.sub(r"\s+", " ", truncated).strip().lower()
        if normalized_quote in normalized_haystack:
            return truncated
        return None  # bogus quote — discarded silently

    return Parsed(
        topic_fit_rating=rating,
        topic_fit_rationale=str(tf.get("rationale") or ""),
        topic_fit_quoted=_verified_quote(_validated_quote(tf, "topic_fit")),
        eligibility_rating=el_rating,
        eligibility_rationale=str(el.get("rationale") or ""),
        eligibility_quoted=_verified_quote(_validated_quote(el, "eligibility")),
        exclusions_rating=ex_rating,
        exclusions_rationale=str(ex.get("rationale") or ""),
        exclusions_quoted=_verified_quote(_validated_quote(ex, "exclusions")),
    )
