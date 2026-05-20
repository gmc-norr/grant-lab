import pytest

from call_manager.auth.domain import domain_allowed, extract_domain


@pytest.mark.parametrize(
    "email,expected",
    [
        ("alice@ki.se", "ki.se"),
        ("Bob@KI.SE", "ki.se"),
        ("carol+tag@karolinska.se", "karolinska.se"),
    ],
)
def test_extract_domain(email, expected):
    assert extract_domain(email) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "not-an-email",  # no @
        "x@y@ki.se",  # multiple @
        "@ki.se",  # empty local part
        "alice@",  # empty domain
        "",  # empty
    ],
)
def test_extract_domain_rejects_malformed(bad):
    with pytest.raises(ValueError):
        extract_domain(bad)


@pytest.mark.parametrize(
    "email,allowed,ok",
    [
        ("alice@ki.se", ["ki.se"], True),
        ("alice@KI.SE", ["ki.se"], True),
        ("alice@ki.se", ["karolinska.se"], False),
        # subdomain does NOT match parent — explicit listing required
        ("alice@intern.ki.se", ["ki.se"], False),
        ("alice@intern.ki.se", ["intern.ki.se", "ki.se"], True),
    ],
)
def test_domain_allowed(email, allowed, ok):
    assert domain_allowed(email, allowed) is ok
