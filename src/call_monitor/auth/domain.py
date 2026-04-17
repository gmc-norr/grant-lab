def extract_domain(email: str) -> str:
    """Return the lower-cased domain of an email address.

    Strict: exactly one `@`, both local and domain parts non-empty. Rejects
    multi-`@` addresses (`x@y@ki.se`) so we never write malformed addresses
    to the users table or hand them to the email API.
    """
    parts = email.split("@")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"not a valid email address: {email!r}")
    return parts[1].lower()


def domain_allowed(email: str, allowed_domains: list[str]) -> bool:
    try:
        domain = extract_domain(email)
    except ValueError:
        return False
    normalized = {d.lower() for d in allowed_domains}
    return domain in normalized
