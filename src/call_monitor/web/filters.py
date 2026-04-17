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
