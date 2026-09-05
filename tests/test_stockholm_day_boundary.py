"""The daily-spend caps hinge on "when did today start", and no test could
falsify it.

There were TWO byte-different copies of `_stockholm_day_start_utc`
(`turns/caps.py:95` and `citations/usage.py:29`), each calling `datetime.now()`
INSIDE itself — untestable by construction, since a test cannot choose the
instant. `grep -rn _stockholm_day_start_utc tests/` returned **zero** hits, and
mutating both to plain UTC midnight left the whole suite green at 4084 passed.

The harm is specific. Stockholm is UTC+1/+2, so local midnight is 22:00 or 23:00
UTC the PREVIOUS day. A UTC-midnight boundary therefore starts counting two
hours late: everything spent between 00:00 and 02:00 Stockholm falls in
"yesterday" and is invisible to the cap for the rest of the day — up to 2x the
configured hosted spend.

`digest/send.py:week_start(now)` already had the right shape: it takes `now`, so
it is testable, and it is tested. This is that shape, applied to the day.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from grant_lab.timeutil import STOCKHOLM, stockholm_day_start_utc


@pytest.mark.parametrize(
    ("local_now", "expected_utc"),
    [
        # CEST (UTC+2): local midnight is 22:00 UTC the previous day.
        ("2026-07-01 01:00", "2026-06-30 22:00"),
        ("2026-07-01 13:00", "2026-06-30 22:00"),
        # CET (UTC+1): local midnight is 23:00 UTC the previous day.
        ("2026-01-15 00:30", "2026-01-14 23:00"),
        ("2026-01-15 23:59", "2026-01-14 23:00"),
        # Spring forward (2026-03-29, 02:00 -> 03:00 local): the day still
        # starts at local 00:00, which is 23:00 UTC on the 28th (still CET).
        ("2026-03-29 12:00", "2026-03-28 23:00"),
        # Autumn back (2026-10-25, 03:00 -> 02:00 local): still CEST at local
        # midnight, so 22:00 UTC on the 24th.
        ("2026-10-25 12:00", "2026-10-24 22:00"),
    ],
)
def test_the_day_starts_at_stockholm_midnight_not_utc_midnight(local_now, expected_utc):
    now = datetime.strptime(local_now, "%Y-%m-%d %H:%M").replace(tzinfo=STOCKHOLM)
    expected = datetime.strptime(expected_utc, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
    assert stockholm_day_start_utc(now) == expected


def test_a_00_30_spend_still_counts_at_midday():
    # THE regression, computed rather than assumed. Later the same day, UTC
    # midnight lands AFTER Stockholm midnight, so the window starts two hours
    # late and the 00:00-02:00 local band drops out of the cap for the rest of
    # the day. Measured for 2026-07-01 (CEST):
    #
    #   stockholm day start  2026-06-30 22:00 UTC
    #   utc       day start  2026-07-01 00:00 UTC
    #   a 00:30-local row    2026-06-30 22:30 UTC  -> inside one, outside the other
    now = datetime(2026, 7, 1, 13, 0, tzinfo=STOCKHOLM)
    spend_row = datetime(2026, 7, 1, 0, 30, tzinfo=STOCKHOLM)

    day_start = stockholm_day_start_utc(now)
    assert spend_row >= day_start, "a 00:30-local spend row must count toward today"

    utc_day_start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    assert spend_row < utc_day_start, "the mutation this test exists to catch"


def test_the_window_always_contains_the_instant_it_was_asked_about():
    # A day start later than `now` would make the current spend uncountable.
    # Checked across the year, both DST sides, and the small hours.
    for month in range(1, 13):
        for hour in (0, 1, 2, 12, 23):
            now = datetime(2026, month, 15, hour, 30, tzinfo=STOCKHOLM)
            assert stockholm_day_start_utc(now) <= now, f"{month=} {hour=}"


def test_it_accepts_an_instant_which_is_what_makes_it_testable():
    # The two originals called datetime.now() internally, so no test could pick
    # the instant — which is why zero tests referenced them. Keep that property:
    # the result must depend only on the INSTANT, not on how it is expressed.
    instant = datetime(2026, 7, 1, 5, 0, tzinfo=UTC)
    assert stockholm_day_start_utc(instant) == stockholm_day_start_utc(
        instant.astimezone(STOCKHOLM)
    )
    assert stockholm_day_start_utc(instant) == stockholm_day_start_utc(
        instant.astimezone(ZoneInfo("America/New_York"))
    )
    # ...and a genuinely different Stockholm day must differ.
    assert stockholm_day_start_utc(instant) != stockholm_day_start_utc(
        instant.replace(day=2)
    )


def test_omitting_the_instant_uses_the_clock():
    # The production callers pass nothing; that path must still work and agree
    # with the explicit form.
    assert stockholm_day_start_utc() == stockholm_day_start_utc(datetime.now(UTC))
