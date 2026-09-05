import pytest

from grant_lab.auth.domain import domain_allowed, extract_domain, normalize_domain


@pytest.mark.parametrize(
    "email,expected",
    [
        ("alice@umu.se", "umu.se"),
        ("Bob@UMU.SE", "umu.se"),
        ("carol+tag@regionvasterbotten.se", "regionvasterbotten.se"),
    ],
)
def test_extract_domain(email, expected):
    assert extract_domain(email) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "not-an-email",  # no @
        "x@y@umu.se",  # multiple @
        "@umu.se",  # empty local part
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
        ("alice@umu.se", ["umu.se"], True),
        ("alice@UMU.SE", ["umu.se"], True),
        ("alice@umu.se", ["regionvasterbotten.se"], False),
        # subdomain does NOT match parent — explicit listing required
        ("alice@intern.umu.se", ["umu.se"], False),
        ("alice@intern.umu.se", ["intern.umu.se", "umu.se"], True),
    ],
)
def test_domain_allowed(email, allowed, ok):
    assert domain_allowed(email, allowed) is ok


def test_extract_domain_strips_trailing_root_dot():
    # A trailing root dot is a valid FQDN form; it must normalize to the same
    # domain so `alice@umu.se.` is treated as umu.se, not a distinct domain.
    assert extract_domain("alice@umu.se.") == "umu.se"
    assert domain_allowed("alice@umu.se.", ["umu.se"]) is True


def test_normalize_domain_idna_canonicalizes_unicode():
    # A Unicode homoglyph domain encodes to punycode (xn--), so it can never
    # match an ASCII allowlist entry by sight.
    # "kі.se" uses a Cyrillic 'і' (U+0456) in place of ASCII 'i'.
    homoglyph = "kі.se"
    assert normalize_domain(homoglyph).startswith("xn--")
    assert domain_allowed(f"attacker@{homoglyph}", ["umu.se"]) is False


def test_normalize_domain_rejects_empty():
    with pytest.raises(ValueError):
        normalize_domain("")
    with pytest.raises(ValueError):
        normalize_domain(".")
