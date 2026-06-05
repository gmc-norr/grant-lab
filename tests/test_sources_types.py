import socket
from datetime import UTC, datetime

from call_manager.sources.types import (
    AdapterError,
    NormalizedCall,
    SourceAdapter,
    _PinnedNetworkBackend,
    is_safe_fetch_url,
    is_safe_url,
    pinned_transport_for,
    resolve_pinned_ip,
)


def _fake_getaddrinfo(*ips):
    """Return a getaddrinfo replacement yielding the given IP strings."""

    def _inner(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]

    return _inner


def test_normalized_call_minimal_fields():
    c = NormalizedCall(
        external_id="X-1",
        url="https://example.org/x-1",
        title="Some title",
    )
    assert c.summary is None
    assert c.keywords == []
    assert c.raw == {}


def test_adapter_error_is_exception():
    e = AdapterError("network blip")
    assert isinstance(e, Exception)


def test_is_safe_url_scheme_check():
    assert is_safe_url("https://example.org/x") is True
    assert is_safe_url("http://example.org/x") is True
    assert is_safe_url("javascript:alert(1)") is False
    assert is_safe_url("ftp://example.org/x") is False


def test_is_safe_fetch_url_rejects_literal_internal_ips():
    """SSRF chokepoint: literal IPs in private/loopback/link-local ranges are
    rejected without DNS. 169.254.169.254 is the cloud-metadata endpoint."""
    assert is_safe_fetch_url("http://127.0.0.1/") is False
    assert is_safe_fetch_url("http://10.0.0.5/secret") is False
    assert is_safe_fetch_url("http://192.168.1.1/") is False
    assert is_safe_fetch_url("http://169.254.169.254/latest/meta-data/") is False
    assert is_safe_fetch_url("http://[::1]/") is False


def test_is_safe_fetch_url_rejects_bad_scheme():
    assert is_safe_fetch_url("javascript:alert(1)") is False
    assert is_safe_fetch_url("file:///etc/passwd") is False


def test_is_safe_fetch_url_rejects_unresolvable_host():
    # A host in the reserved .invalid TLD never resolves → not fetchable.
    assert is_safe_fetch_url("https://nonexistent.invalid/x") is False


def test_is_safe_fetch_url_allows_public_host():
    # example.com resolves to a public address.
    assert is_safe_fetch_url("https://example.com/x") is True


def test_resolve_pinned_ip_returns_literal_public_ip():
    assert resolve_pinned_ip("https://8.8.8.8/") == "8.8.8.8"


def test_resolve_pinned_ip_rejects_literal_private_ip():
    assert resolve_pinned_ip("http://127.0.0.1/") is None
    assert resolve_pinned_ip("http://169.254.169.254/latest/meta-data/") is None


def test_resolve_pinned_ip_rejects_bad_scheme_and_unresolvable():
    assert resolve_pinned_ip("file:///etc/passwd") is None
    assert resolve_pinned_ip("https://nonexistent.invalid/x") is None


def test_resolve_pinned_ip_returns_resolved_public_ip(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    assert resolve_pinned_ip("https://example.com/x") == "93.184.216.34"


def test_resolve_pinned_ip_rejects_dns_rebind_to_private(monkeypatch):
    """DNS-rebinding defense: a name that resolves to a private/metadata
    address yields None, so the connection is never pinned/attempted — even if
    the name LOOKS public. A mixed public+private answer set is also refused."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("169.254.169.254"))
    assert resolve_pinned_ip("https://rebind.example/x") is None

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34", "10.0.0.5"))
    assert resolve_pinned_ip("https://mixed.example/x") is None


def test_pinned_network_backend_rewrites_connect_host():
    """The pinned backend forces connect_tcp to the validated IP while leaving
    everything else (TLS SNI, Host header — derived from the origin) untouched."""

    class _FakeInner:
        def __init__(self):
            self.calls = []

        def connect_tcp(self, host, port, *args, **kwargs):
            self.calls.append((host, port))
            return "conn"

    inner = _FakeInner()
    backend = _PinnedNetworkBackend(inner, {"example.com": "93.184.216.34"})
    assert backend.connect_tcp("example.com", 443) == "conn"
    # A host not in the pin map passes through unchanged.
    backend.connect_tcp("other.example", 80)
    assert inner.calls == [("93.184.216.34", 443), ("other.example", 80)]


def test_pinned_transport_for_unsafe_returns_none(monkeypatch):
    assert pinned_transport_for("http://127.0.0.1/") is None
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("10.0.0.1"))
    assert pinned_transport_for("https://rebind.example/") is None


def test_pinned_transport_for_safe_builds_pinned_transport(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    transport = pinned_transport_for("https://example.com/x")
    assert transport is not None
    # The pool's network backend is our pinning wrapper holding the validated IP.
    backend = transport._pool._network_backend
    assert isinstance(backend, _PinnedNetworkBackend)
    assert backend._pin_map == {"example.com": "93.184.216.34"}
    transport.close()


def test_protocol_check():
    """A class that implements .slug and .fetch_new_calls satisfies SourceAdapter."""

    class FakeAdapter:
        slug = "fake"

        def fetch_new_calls(self, since):
            yield NormalizedCall(external_id="X", url="u", title="t")

    fake = FakeAdapter()
    # runtime_checkable Protocol; isinstance check
    assert isinstance(fake, SourceAdapter)
    out = list(fake.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert out[0].external_id == "X"
