from call_manager.web.call_detail_view import _money_range, _trl_range


def test_money_range_formats_thousands_with_currency():
    assert _money_range(10000, 500000, "EUR") == "10,000–500,000 EUR"


def test_money_range_unspecified():
    assert _money_range(None, None, "EUR") == "(unspecified)"


def test_trl_range_partial_dash():
    assert _trl_range(3, None) == "3–—"
