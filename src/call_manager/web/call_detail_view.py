from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException

from call_manager.db.models import Call, CallRanking, Profile


def _parse_optional_profile_id(raw: str | None) -> int | None:
    """HTML <select> filters submit `?profile=` (empty string) when "all"
    is chosen. FastAPI's `int | None` declaration refuses to parse an
    empty string as int — so the param is accepted as `str | None` and
    we coerce here, treating empty/whitespace identically to omission.
    A non-empty non-integer still 400s, since that's an actual bad URL."""
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid profile id: {raw!r}") from e


def _criterion_detail(
    name: str, *, call: Call, profile: Profile, ranking: CallRanking
) -> str | None:
    """One-line "why" string for a deterministic gate criterion. Returns
    None for LLM criteria (topic_fit / eligibility / exclusions) — their
    rationale field already carries a free-text explanation.

    Generated at render time so profile/call edits made AFTER the last
    ranking are reflected immediately — the ratings are point-in-time
    snapshots, the displayed reasoning is "as the rubric looks today"."""
    p = profile.structured or {}
    c_struct = call.structured or {}
    if name == "timeline":
        if call.deadline is None:
            return "No deadline declared by source — treated as PASS."
        delta = call.deadline - datetime.now(UTC)
        days = delta.days
        if days < 0:
            return f"Deadline passed {-days} day(s) ago ({call.deadline.date()})."
        return f"Deadline {call.deadline.date()} — {days} day(s) away (minimum 14 day window)."
    if name == "budget":
        c_min, c_max = call.budget_min, call.budget_max
        p_min, p_max = p.get("budget_min"), p.get("budget_max")
        c_cur = (call.currency or "?").upper()
        p_cur = (profile.budget_currency or "?").upper()
        c_str = _money_range(c_min, c_max, c_cur)
        p_str = _money_range(p_min, p_max, p_cur)
        if c_min is None or c_max is None or p_min is None or p_max is None:
            return f"Call: {c_str} · Profile: {p_str} — incomplete data, gate UNKNOWN."
        if c_cur != p_cur and "?" not in (c_cur, p_cur):
            return f"Call: {c_str} · Profile: {p_str} — currency mismatch."
        return f"Call: {c_str} · Profile: {p_str}."
    if name == "trl":
        c_min, c_max = c_struct.get("trl_min"), c_struct.get("trl_max")
        p_min, p_max = p.get("trl_min"), p.get("trl_max")
        c_str = _trl_range(c_min, c_max)
        p_str = _trl_range(p_min, p_max)
        if None in (c_min, c_max, p_min, p_max):
            return f"Call: {c_str} · Profile: {p_str} — incomplete data, gate UNKNOWN."
        return f"Call: {c_str} · Profile: {p_str}."
    if name == "embedding":
        sim = ranking.embed_similarity
        if sim is None:
            return "No cosine — one or both vectors missing, gate UNKNOWN."
        return f"Cosine similarity {sim:.3f} (pass threshold 0.30)."
    return None


def _money_range(lo: int | None, hi: int | None, currency: str) -> str:
    if lo is None and hi is None:
        return "(unspecified)"
    lo_s = f"{lo:,}" if lo is not None else "—"
    hi_s = f"{hi:,}" if hi is not None else "—"
    return f"{lo_s}–{hi_s} {currency}"


def _trl_range(lo, hi) -> str:
    if lo is None and hi is None:
        return "(unspecified)"
    return f"{lo if lo is not None else '—'}–{hi if hi is not None else '—'}"
