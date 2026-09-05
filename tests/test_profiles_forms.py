import pytest
from fastapi import HTTPException

from grant_lab.profiles.forms import (
    ALLOWED_CURRENCIES,
    BUDGET_MAX,
    bounded_int_or_400,
    parse_members_strict,
    split_lines,
)


def test_bounded_int_or_400_parses_and_bounds():
    assert bounded_int_or_400("", field="budget_min", low=0, high=BUDGET_MAX) is None
    assert bounded_int_or_400("n/a", field="budget_min", low=0, high=BUDGET_MAX) is None
    assert bounded_int_or_400("5", field="trl_min", low=1, high=9) == 5
    with pytest.raises(HTTPException) as ei:
        bounded_int_or_400("99", field="trl_min", low=1, high=9)
    assert ei.value.status_code == 400


def test_split_lines_strips_and_caps():
    assert split_lines("a\n  b  \n\n c ") == ["a", "b", "c"]


def test_parse_members_strict_rejects_too_many():
    with pytest.raises(HTTPException) as ei:
        parse_members_strict("\n".join(f"0000-0001-0000-{i:04d}" for i in range(60)), "")
    assert ei.value.status_code == 400


def test_allowed_currencies_includes_blank_and_sek():
    assert "" in ALLOWED_CURRENCIES and "SEK" in ALLOWED_CURRENCIES
