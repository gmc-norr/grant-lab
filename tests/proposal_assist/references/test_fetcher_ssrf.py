"""SSRF defenses for the references fetcher. Each test exercises one
defense; together they cover the rev4 Codex High #2 attack surface
(scheme allowlist, private-IP block, DNS rebinding, redirect validation,
response-size cap).

Unlike the other reference tests, the `fake_public_dns` fixture is
opted into per-test rather than autouse — the rebinding-validation test
needs to install its own getaddrinfo monkeypatch, so it must not
collide with an autouse fixture."""

import pytest

from call_manager.proposal_assist.references.fetcher import (
    FetchError,
    SSRFError,
    fetch_bytes,
)


def test_rejects_non_http_scheme():
    with pytest.raises(SSRFError, match="scheme"):
        fetch_bytes("file:///etc/passwd")
    with pytest.raises(SSRFError, match="scheme"):
        fetch_bytes("gopher://example.com/x")
    with pytest.raises(SSRFError, match="scheme"):
        fetch_bytes("data:text/plain,hello")


def test_rejects_loopback_hostname():
    with pytest.raises(SSRFError, match="private"):
        fetch_bytes("http://127.0.0.1/")
    with pytest.raises(SSRFError, match="private"):
        fetch_bytes("http://localhost/")  # resolves to 127.0.0.1


def test_rejects_rfc1918_private_ip():
    with pytest.raises(SSRFError, match="private"):
        fetch_bytes("http://10.0.0.1/")
    with pytest.raises(SSRFError, match="private"):
        fetch_bytes("http://192.168.1.1/")
    with pytest.raises(SSRFError, match="private"):
        fetch_bytes("http://172.16.0.1/")


def test_rejects_aws_metadata_ip():
    with pytest.raises(SSRFError, match="private"):
        fetch_bytes("http://169.254.169.254/latest/meta-data/")


def test_rejects_ipv6_loopback_and_link_local():
    with pytest.raises(SSRFError, match="private"):
        fetch_bytes("http://[::1]/")
    with pytest.raises(SSRFError, match="private"):
        fetch_bytes("http://[fe80::1]/")


def test_validator_rejects_resolver_returning_private_ip(monkeypatch):
    """Tests the validation step: if DNS resolution returns a private
    IP for a public-looking hostname, _resolve_and_validate rejects
    before httpx ever opens a socket.

    This test does NOT prove rebinding immunity at the httpx layer.
    The connect-time race remains a documented v1 limitation per the
    spec — httpx re-resolves at connect time, so an attacker who can
    flip DNS responses between the validation lookup and the connect
    lookup can still slip a private IP past this defense. True pinning
    is a v1.1 follow-up (custom httpx.HTTPTransport)."""
    import socket

    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, *args, **kwargs):
        if host == "rebinding.example.com":
            # Return loopback in the first answer — should be rejected.
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("127.0.0.1", 0),
                )
            ]
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(SSRFError, match="private"):
        fetch_bytes("https://rebinding.example.com/")


def test_pinned_connection_resolves_host_exactly_once(httpx_mock, monkeypatch):
    """DNS-rebinding pinning: fetch_bytes resolves+validates the host
    EXACTLY once, then pins the TCP connect to that validated IP. It must
    NOT perform a second hostname resolution at connect time (which an
    attacker with low-TTL DNS could flip to a private IP).

    We count getaddrinfo calls for the host: a second lookup would mean
    the connect path re-resolves (the rebind window). With socket-level
    pinning there is exactly one lookup — the validation lookup — and the
    connection is routed to the pinned IP.

    (pytest-httpx intercepts at the transport layer so no real socket is
    opened; the assertion is on resolution count + that the validated
    public IP is what gets pinned, which is the load-bearing behavior.)"""
    import socket

    from call_manager.proposal_assist.references import fetcher

    calls = {"n": 0}

    def counting_getaddrinfo(host, *args, **kwargs):
        if host == "rebind.attacker.test":
            calls["n"] += 1
            # First (validation) lookup → public; any later lookup → private.
            ip = "8.8.8.8" if calls["n"] == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
        return socket.getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", counting_getaddrinfo)

    # Capture the pin map handed to the transport to prove the validated
    # PUBLIC IP (8.8.8.8) is what gets pinned — not a rebind to 127.0.0.1.
    captured: dict[str, str] = {}
    real_pinned_transport = fetcher._pinned_transport

    def spy_pinned_transport(pin_map):
        captured.update(pin_map)
        return real_pinned_transport(pin_map)

    monkeypatch.setattr(fetcher, "_pinned_transport", spy_pinned_transport)

    httpx_mock.add_response(
        url="https://rebind.attacker.test/paper.pdf",
        content=b"%PDF-pinned",
    )

    data = fetch_bytes("https://rebind.attacker.test/paper.pdf")
    assert data == b"%PDF-pinned"

    # Exactly one resolution — the connect path did NOT re-resolve.
    assert calls["n"] == 1
    # The pinned address is the validated public IP, never the rebind target.
    assert captured["rebind.attacker.test"] == "8.8.8.8"


def test_redirect_to_private_ip_is_rejected(httpx_mock, fake_public_dns):
    """Validate every redirect target, not just the initial URL.
    If the first hop is public but redirects to a private IP, reject."""
    httpx_mock.add_response(
        url="https://public.example.com/",
        status_code=302,
        headers={"location": "http://127.0.0.1/"},
    )
    with pytest.raises(SSRFError, match="private"):
        fetch_bytes("https://public.example.com/")


def test_response_size_cap_enforced(httpx_mock, fake_public_dns):
    """Server claims a 100 MB Content-Length; our 50 MB cap rejects."""
    big = b"x" * (60 * 1024 * 1024)  # 60 MB
    httpx_mock.add_response(
        url="https://public.example.com/big.pdf",
        content=big,
    )
    with pytest.raises(FetchError, match="exceeds"):
        fetch_bytes("https://public.example.com/big.pdf", max_bytes=50 * 1024 * 1024)


def test_size_cap_aborts_during_streaming(httpx_mock, fake_public_dns):
    """Regression: the size cap must be enforced incrementally while
    streaming, not after the whole body is materialised. A body just
    over a small cap is rejected, and the abort error names max_bytes."""
    body = b"y" * 5000
    httpx_mock.add_response(
        url="https://public.example.com/medium.bin",
        content=body,
    )
    with pytest.raises(FetchError, match="max_bytes=1024"):
        fetch_bytes("https://public.example.com/medium.bin", max_bytes=1024)


def test_size_cap_allows_body_at_limit(httpx_mock, fake_public_dns):
    """A body exactly at max_bytes is returned; only strictly-larger
    bodies are rejected (boundary of the streaming check)."""
    body = b"z" * 1024
    httpx_mock.add_response(
        url="https://public.example.com/exact.bin",
        content=body,
    )
    data = fetch_bytes("https://public.example.com/exact.bin", max_bytes=1024)
    assert data == body


def test_successful_fetch_returns_bytes(httpx_mock, fake_public_dns):
    """Sanity: a 200 OK with reasonable size returns the bytes verbatim."""
    httpx_mock.add_response(
        url="https://public.example.com/paper.pdf",
        content=b"%PDF-1.4\nfake pdf bytes",
    )
    data = fetch_bytes("https://public.example.com/paper.pdf")
    assert data == b"%PDF-1.4\nfake pdf bytes"
