"""`pinned_transport_for` returns None as its REFUSAL signal — unsafe scheme,
a private/blocked IP in any DNS answer, or DNS failure.

Both API adapters passed that None straight into `httpx.Client(transport=...)`,
where None does not mean "no transport" — it means "build the default one".
Measured: `httpx.Client(transport=None)._transport` is a real, UNPINNED
`httpx.HTTPTransport`. So the exact case the SSRF check rejected was the case
that connected with no pinning at all, and a transient `gaierror` silently
reopened the DNS-rebinding window the pin exists to close.

`sources/extraction.py` gets this right (`if fetch_transport is None: return
None`) — these two did not.

The production branch is only reached when no transport is injected, and every
other test injects one, which is why nothing caught it.
"""

from datetime import UTC, datetime

import pytest

from grant_lab.sources.eu_portal import EUPortalAdapter
from grant_lab.sources.types import AdapterError
from grant_lab.sources.vinnova import VinnovaAdapter


@pytest.mark.parametrize(
    ("name", "make"),
    [
        ("eu_portal", lambda url: EUPortalAdapter(base_url=url)),
        ("vinnova", lambda url: VinnovaAdapter(base_url=url)),
    ],
)
def test_a_refused_ssrf_target_aborts_instead_of_connecting_unpinned(name, make, monkeypatch):
    # `pinned_transport_for` refuses. The adapter must NOT reach httpx.
    import grant_lab.sources.eu_portal as eu
    import grant_lab.sources.vinnova as vin

    for mod in (eu, vin):
        monkeypatch.setattr(mod, "pinned_transport_for", lambda _url: None)

    opened = []

    def boom(*a, **kw):
        opened.append(kw.get("transport"))
        raise AssertionError(f"{name}: opened a client after the SSRF check refused")

    monkeypatch.setattr(eu.httpx, "Client", boom)
    monkeypatch.setattr(vin.httpx, "Client", boom)

    # No transport injected — this is the production branch.
    adapter = make("https://rebind.example")
    with pytest.raises(AdapterError) as ei:
        list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert opened == []
    # The message must name the cause, or an operator sees a bare fetch failure.
    assert "pin" in str(ei.value).lower() or "unsafe" in str(ei.value).lower()


def test_httpx_treats_none_as_build_the_default_transport():
    # The premise the two adapters got wrong, pinned so it cannot silently
    # change under us: None is not "no transport".
    import httpx

    with httpx.Client(transport=None) as c:
        assert isinstance(c._transport, httpx.HTTPTransport)
