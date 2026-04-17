import httpx
import pytest

from call_monitor.email.client import (
    EmailTransportError,
    ResendClient,
    SendResult,
)


def _mock(handler):
    return httpx.MockTransport(handler)


def test_resend_success_returns_message_id():
    def handler(req):
        assert req.method == "POST"
        assert "/emails" in str(req.url)
        return httpx.Response(200, json={"id": "msg_abc"})
    client = ResendClient(api_key="key", from_addr="bot@ki.se", transport=_mock(handler))
    r = client.send(to="u@ki.se", subject="hi", html="<p>hi</p>", text="hi")
    assert isinstance(r, SendResult)
    assert r.ok
    assert r.message_id == "msg_abc"
    assert r.bounce_reason is None


def test_resend_invalid_email_is_synchronous_bounce():
    def handler(req):
        return httpx.Response(
            422,
            json={"name": "validation_error", "message": "Invalid email address"},
        )
    client = ResendClient(api_key="key", from_addr="bot@ki.se", transport=_mock(handler))
    r = client.send(to="bad", subject="hi", html="<p>hi</p>", text="hi")
    assert not r.ok
    assert r.bounce_reason and "invalid" in r.bounce_reason.lower()


def test_resend_5xx_raises_transport_error():
    def handler(req):
        return httpx.Response(503)
    client = ResendClient(api_key="key", from_addr="bot@ki.se", transport=_mock(handler))
    with pytest.raises(EmailTransportError):
        client.send(to="u@ki.se", subject="hi", html="<p>hi</p>", text="hi")


def test_resend_rate_limit_is_transport_error_not_bounce():
    """Resend rate-limits the SENDER, not recipients. Don't count as bounce
    or we'd auto-disable users for our own quota issue."""
    def handler(req):
        return httpx.Response(
            429, json={"name": "rate_limit_exceeded", "message": "too many"},
        )
    client = ResendClient(api_key="key", from_addr="bot@ki.se", transport=_mock(handler))
    with pytest.raises(EmailTransportError):
        client.send(to="u@ki.se", subject="hi", html="<p>hi</p>", text="hi")


def test_resend_network_error_raises_transport_error():
    def handler(req):
        raise httpx.ConnectError("boom")
    client = ResendClient(api_key="key", from_addr="bot@ki.se", transport=_mock(handler))
    with pytest.raises(EmailTransportError):
        client.send(to="u@ki.se", subject="hi", html="<p>hi</p>", text="hi")


def test_resend_missing_field_is_transport_error_not_bounce():
    """missing_field is a sender-side payload regression (our code forgot a
    required field). Don't count as per-recipient bounce — we'd auto-disable
    valid users for our own config error."""
    def handler(req):
        return httpx.Response(
            422, json={"name": "missing_field", "message": "from is required"},
        )
    client = ResendClient(api_key="k", from_addr="bot@ki.se", transport=_mock(handler))
    with pytest.raises(EmailTransportError):
        client.send(to="u@ki.se", subject="s", html="<p></p>", text="")


def test_resend_authorization_header_set():
    captured = {}
    def handler(req):
        captured["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json={"id": "x"})
    client = ResendClient(api_key="my-key", from_addr="bot@ki.se", transport=_mock(handler))
    client.send(to="u@ki.se", subject="s", html="<p></p>", text="")
    assert captured["auth"] == "Bearer my-key"
