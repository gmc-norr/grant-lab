from datetime import UTC, datetime

from call_monitor.sources.types import AdapterError, NormalizedCall, SourceAdapter


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
