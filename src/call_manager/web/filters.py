"""Jinja filters that present DB-stored UTC timestamps in Europe/Stockholm.

Per design spec: "All timestamp columns are TIMESTAMPTZ. Adapters must
normalize source timestamps to UTC before yielding a NormalizedCall.
The web layer converts to Europe/Stockholm for display; the DB stores
UTC." These filters are the canonical web-layer conversion point —
templates should never render a `datetime` directly via `strftime`.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_STOCKHOLM = ZoneInfo("Europe/Stockholm")


def display_dt(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M %Z") -> str:
    """Render a tz-aware UTC datetime in Europe/Stockholm wall time.

    Returns the literal string '—' for None so templates can write
    `{{ call.deadline | display_dt }}` without a defensive `if`.
    """
    if value is None:
        return "—"
    return value.astimezone(_STOCKHOLM).strftime(fmt)


def display_date(value: datetime | None) -> str:
    """Render the calendar date in Europe/Stockholm — strips the time
    component. Used where only the day matters (publication date)."""
    if value is None:
        return "—"
    return value.astimezone(_STOCKHOLM).strftime("%Y-%m-%d")


# Internal codes used for `CallRanking.aggregate_reason` and the
# `CallRankingCriterion.criterion` PK. Kept as compact snake-case in the
# DB; rendered as proper labels for the UI. Unmapped values fall through
# to a Title-cased version of the raw code so a future criterion doesn't
# disappear — it just looks slightly less polished until we add it here.
_CRITERION_LABELS: dict[str, str] = {
    "topic_fit": "Topic fit",
    "eligibility": "Eligibility",
    "exclusions": "Exclusions",
    "timeline": "Timeline",
    "budget": "Budget",
    "trl": "Technology Readiness Level",
    "embedding": "Topic similarity",
}


def criterion_label(value: str | None) -> str:
    if value is None:
        return "—"
    return _CRITERION_LABELS.get(value, value.replace("_", " ").title())


def note_category_heading(value: str | None) -> str:
    """Pretty heading for an applicant_notes category. Stored vocabulary
    is single-token snake_case ('budget', 'ip', 'data', etc.) — UI wants
    a fuller, capitalized heading per card. Unknown categories pass
    through Title-cased so a future Sonnet-invented label isn't silently
    swallowed."""
    if not value:
        return "Note"
    mapping = {
        "deadline": "Deadline",
        "eligibility": "Eligibility",
        "budget": "Budget & funding",
        "team": "Team & consortium",
        "ip": "IP & licensing",
        "data": "Data & open access",
        "evaluation": "Evaluation criteria",
        "process": "Process & timing",
        "other": "Other",
    }
    return mapping.get(value, value.replace("_", " ").title())


def phase_label(value: str | None) -> str:
    """Human-readable label for `Workspace.current_phase`. Stored values are
    the two-mode lifecycle ('setup','active'); the UI chip should read as
    words. Unknown values pass through unchanged so a future mode isn't
    swallowed into a blank chip."""
    if not value:
        return "—"
    return {
        "setup": "Setting up",
        "active": "Active",
    }.get(value, value)


def model_label(value: str | None) -> str:
    """Pretty name for the LLM that produced a CallRanking. Stored value
    is the raw model ID (e.g. 'claude-sonnet-4-6', 'gemma4:e4b-it-q4_K_M');
    UI wants something a non-engineer can read.

    Unmapped models fall through to the raw string so a new backend isn't
    silently mislabelled — easier to spot in the UI than a hardcoded
    'Unknown'.
    """
    if not value:
        return "—"
    pretty = {
        "claude-sonnet-4-6": "Sonnet 4.6",
        "claude-sonnet-4-7": "Sonnet 4.7",
        "claude-opus-4-6": "Opus 4.6",
        "claude-opus-4-7": "Opus 4.7",
        "claude-haiku-4-5": "Haiku 4.5",
    }.get(value)
    if pretty:
        return pretty
    if value.startswith("gemma4:e4b-it-q4"):
        return "Gemma 4 E4B (Q4_K_M, local)"
    if value.startswith("gemma4:e4b-it-q8"):
        return "Gemma 4 E4B (Q8, local)"
    if value.startswith("gemma4"):
        return f"Gemma 4 ({value}, local)"
    return value
