"""SSRF defenses for the references fetcher. Each test exercises one
defense; together they cover the rev4 Codex High #2 attack surface
(scheme allowlist, private-IP block, DNS rebinding, redirect validation,
response-size cap)."""

import socket

import pytest

from call_manager.proposal_assist.references.fetcher import (
    FetchError,
    SSRFError,
    fetch_bytes,
)


@pytest.fixture
def fake_public_dns(monkeypatch):
    """Pretend *.example.com resolves to a real public IP (8.8.8.8) for
    httpx_mock tests that don't (and can't) hit real DNS. Other hostnames
    fall through to the real resolver. We use 8.8.8.8 rather than a
    TEST-NET address because Python's ipaddress.is_private treats
    documentation ranges (192.0.2/24, 198.51.100/24, 203.0.113/24) as
    private, which would defeat the test."""
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, *args, **kwargs):
        if host.endswith(".example.com") or host == "example.com":
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("8.8.8.8", 0),
                )
            ]
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return fake_getaddrinfo


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


def test_successful_fetch_returns_bytes(httpx_mock, fake_public_dns):
    """Sanity: a 200 OK with reasonable size returns the bytes verbatim."""
    httpx_mock.add_response(
        url="https://public.example.com/paper.pdf",
        content=b"%PDF-1.4\nfake pdf bytes",
    )
    data = fetch_bytes("https://public.example.com/paper.pdf")
    assert data == b"%PDF-1.4\nfake pdf bytes"
