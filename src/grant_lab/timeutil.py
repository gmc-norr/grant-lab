"""Time boundaries the whole app agrees on.

A function that decides when the current period started must TAKE the instant,
never read the clock itself, so a test can pass one. `digest/send.py:week_start(now)`
has the same shape for the week.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

STOCKHOLM = ZoneInfo("Europe/Stockholm")

__all__ = ["STOCKHOLM", "stockholm_day_start_utc"]


def stockholm_day_start_utc(now: datetime | None = None) -> datetime:
    """UTC instant of 00:00 Europe/Stockholm on the day containing `now`.

    `now` defaults to the current time; production callers pass nothing, and
    tests pass an instant. That parameter is the point of this function.

    Stockholm is UTC+1/+2, so local midnight is 22:00 or 23:00 UTC the PREVIOUS
    day. Using UTC midnight instead would start the window two hours late and
    make everything spent between 00:00 and 02:00 Stockholm invisible to the
    daily caps for the rest of the day — up to 2x the configured hosted spend.
    Deriving it from local time also keeps it correct across both DST changes,
    where a fixed offset would not be.
    """
    now_se = (now or datetime.now(UTC)).astimezone(STOCKHOLM)
    midnight_se = now_se.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_se.astimezone(UTC)
