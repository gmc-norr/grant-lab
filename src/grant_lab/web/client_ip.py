from __future__ import annotations

import hashlib

from fastapi import HTTPException, Request

# Hard cap matches users.email VARCHAR(320). Reject oversized inputs
# *before* they reach the rate limiter so an attacker can't grow the
# limiter dict with junk keys (memory-amplification DoS).
MAX_EMAIL_LEN = 320


def email_fingerprint(email: str) -> str:
    """Short hex of sha256(email). Safe to log — doesn't expose the
    address but lets an operator correlate events across log lines."""
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:12]


def client_ip(request: Request) -> str:
    """Resolve the caller's IP.

    When `settings.trusted_proxy` is True, we read the client IP from
    X-Forwarded-For. Each trusted reverse-proxy hop *appends* the
    connecting-peer IP (Nginx's `$proxy_add_x_forwarded_for` and most
    proxies on default settings), so with `trusted_proxy_hops = N` the
    real client is the Nth entry from the right. Anything to the LEFT of
    that is client-supplied and untrusted — taking it would be a free
    rate-limit bypass.

    `trusted_proxy_hops` MUST match the actual deployment topology. If it
    is too small, an attacker's spoofed XFF entry leaks through; if it is
    too large (more hops claimed than exist), every external client
    collapses into one bucket. We therefore reject (400) an XFF that has
    fewer entries than the configured hop count rather than silently
    falling back to a spoofable or shared value.

    Otherwise we use `request.client` directly and fail loudly (400) if
    it's absent — silently bucketing unknown clients into a shared
    "0.0.0.0" key breaks the rate limiter for all legitimate traffic.
    """
    settings = request.app.state.settings
    if getattr(settings, "trusted_proxy", False):
        xff = request.headers.get("x-forwarded-for")
        if xff:
            hops = getattr(settings, "trusted_proxy_hops", 1)
            # Keep only non-empty entries (tolerate stray whitespace / trailing
            # commas), preserving left-to-right order.
            entries = [e.strip() for e in xff.split(",") if e.strip()]
            if len(entries) >= hops:
                # Nth-from-the-right: index -hops.
                return entries[-hops]
            # Fewer entries than trusted hops — the header doesn't match the
            # configured topology. Don't trust a value we can't place; fail
            # loudly rather than mis-bucket.
            raise HTTPException(status_code=400, detail="malformed X-Forwarded-For")
    if request.client is None:
        raise HTTPException(status_code=400, detail="cannot determine client")
    return request.client.host
