def normalize_domain(domain: str) -> str:
    """Canonicalize a domain for allowlist comparison.

    Lower-case, strip a trailing root dot, and IDNA-encode to punycode. The
    IDNA step is the security-relevant part: a Unicode homoglyph domain (e.g.
    `umu.se` written with a Cyrillic lookalike) encodes to its `xn--` punycode
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
    multi-`@` addresses (`x@y@umu.se`) so we never write malformed addresses
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


def sign_in_allowed(user, allowed_domains: list[str]) -> bool:
    """May this EXISTING account sign in, or keep using a session?

    A non-admin needs an allowed email domain; an administrator does not
    (decided 2026-10-31, security review S3). Password login used to skip the
    domain check entirely, so a member admitted under a domain later removed
    from ALLOWED_EMAIL_DOMAINS kept signing in. Admins are exempt because the
    install has active admins on personal and partner addresses that a strict
    check would lock out; the exemption follows the role, so a demoted admin
    outside the list loses access at the next request.

    Applied by password login and get_current_user. Google sign-in stays strict
    for everyone: it is also the path that creates accounts, and it checks the
    domain before it knows who the user is.
    """
    return bool(user.is_admin) or domain_allowed(user.email, allowed_domains)
