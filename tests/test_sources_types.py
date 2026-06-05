from datetime import UTC, datetime

from call_manager.sources.types import (
    AdapterError,
    NormalizedCall,
    SourceAdapter,
    is_safe_fetch_url,
    is_safe_url,
)


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
