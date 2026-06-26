from __future__ import annotations

import contextlib

from fastapi import HTTPException

from call_manager.profiles.seed import Member

MAX_MEMBERS = 50
MAX_SEED_LINE = 500
MAX_LIST_ENTRIES = 100
ALLOWED_CURRENCIES = {"", "SEK", "EUR", "USD", "NOK", "DKK"}
BUDGET_MIN = 0
BUDGET_MAX = 10**12
TRL_MIN = 1
TRL_MAX = 9


def split_lines(raw: str) -> list[str]:
    return [ln.strip() for ln in raw.splitlines() if ln.strip()][:MAX_LIST_ENTRIES]


def bounded_int_or_400(raw: str, *, field: str, low: int, high: int) -> int | None:
    """Parse an optional integer; reject out-of-[low,high] with HTTP 400.

    Empty input -> None. Non-numeric input -> None (a user who types "n/a" in
    an optional numeric box shouldn't see a 400). This module is intentionally
    FastAPI-coupled (raises HTTPException), unlike profiles/fields.py's
    framework-agnostic _bounded_int (which raises FieldValidationError)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        v = int(raw)
    except ValueError:
        return None
    if v < low or v > high:
        raise HTTPException(status_code=400, detail=f"{field} out of range ({low}..{high}): {v}")
    return v


def parse_members_strict(orcids: str, names_affiliations: str) -> list[Member]:
    raw_lines = [
        ln.strip() for ln in (orcids.splitlines() + names_affiliations.splitlines()) if ln.strip()
    ]
    if len(raw_lines) > MAX_MEMBERS:
        raise HTTPException(status_code=400, detail=f"too many members (max {MAX_MEMBERS})")
    if any(len(ln) > MAX_SEED_LINE for ln in raw_lines):
        msg = f"member line too long (max {MAX_SEED_LINE} chars)"
        raise HTTPException(status_code=400, detail=msg)
    return parse_members(orcids, names_affiliations)


def parse_members(orcids: str, names_affiliations: str) -> list[Member]:
    members: list[Member] = []
    for raw in orcids.splitlines():
        orcid = raw.strip()
        if orcid:
            with contextlib.suppress(ValueError):
                members.append(Member(orcid=orcid))
    for raw in names_affiliations.splitlines():
        line = raw.strip()
        if not line or "|" not in line:
            continue
        name_part, aff_part = line.split("|", 1)
        name_part = name_part.strip()
        aff_part = aff_part.strip()
        if name_part and aff_part:
            with contextlib.suppress(ValueError):
                members.append(Member(name=name_part, affiliation=aff_part))
    return members
