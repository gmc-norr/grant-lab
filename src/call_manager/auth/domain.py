def normalize_domain(domain: str) -> str:
    """Canonicalize a domain for allowlist comparison.

    Lower-case, strip a trailing root dot, and IDNA-encode to punycode. The
    IDNA step is the security-relevant part: a Unicode homoglyph domain (e.g.
    `ki.se` written with a Cyrillic lookalike) encodes to its `xn--` punycode
    form, so it can only ever match an allowlist entry that is *also* that
    punycode form — never an ASCII look-alike. Applied identically to the
    configured allowlist (`config._split_csv`) and to the address being
    checked, so both sides are compared in the same canonical space.

    Raises ValueError on an empty or un-encodable domain.
    """
    d = domain.strip().lower().rstrip(".")
    if not d:
        raise ValueError("empty domain")
    try:
        # ASCII domains pass through unchanged; Unicode becomes punycode.
        return d.encode("idna").decode("ascii")
    except (UnicodeError, ValueError) as e:
        raise ValueError(f"invalid domain: {domain!r}") from e


def extract_domain(email: str) -> str:
    """Return the normalized (lower-cased, IDNA-canonical) domain of an email.

    Strict: exactly one `@`, both local and domain parts non-empty. Rejects
    multi-`@` addresses (`x@y@ki.se`) so we never write malformed addresses
    to the users table or hand them to the email API. The domain is run
    through `normalize_domain`, so an IDN homoglyph can't impersonate an
    ASCII allowlist entry.
    """
    parts = email.split("@")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"not a valid email address: {email!r}")
    return normalize_domain(parts[1])


def domain_allowed(email: str, allowed_domains: list[str]) -> bool:
    try:
        domain = extract_domain(email)
    except ValueError:
        return False
    normalized = set()
    for d in allowed_domains:
        try:
            normalized.add(normalize_domain(d))
        except ValueError:
            # A malformed allowlist entry can never match a valid address;
            # skip it rather than letting it crash the check.
            continue
    return domain in normalized
