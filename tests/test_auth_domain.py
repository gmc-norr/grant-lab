import pytest

from call_manager.auth.domain import domain_allowed, extract_domain, normalize_domain


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


def test_extract_domain_strips_trailing_root_dot():
    # A trailing root dot is a valid FQDN form; it must normalize to the same
    # domain so `alice@ki.se.` is treated as ki.se, not a distinct domain.
    assert extract_domain("alice@ki.se.") == "ki.se"
    assert domain_allowed("alice@ki.se.", ["ki.se"]) is True


def test_normalize_domain_idna_canonicalizes_unicode():
    # A Unicode homoglyph domain encodes to punycode (xn--), so it can never
    # match an ASCII allowlist entry by sight.
    # "kі.se" uses a Cyrillic 'і' (U+0456) in place of ASCII 'i'.
    homoglyph = "kі.se"
    assert normalize_domain(homoglyph).startswith("xn--")
    assert domain_allowed(f"attacker@{homoglyph}", ["ki.se"]) is False


def test_normalize_domain_rejects_empty():
    with pytest.raises(ValueError):
        normalize_domain("")
    with pytest.raises(ValueError):
        normalize_domain(".")
