"""SSRF defenses for the references fetcher. Each test exercises one
defense; together they cover the rev4 Codex High #2 attack surface
(scheme allowlist, private-IP block, DNS rebinding, redirect validation,
response-size cap).

Unlike the other reference tests, the `fake_public_dns` fixture is
opted into per-test rather than autouse — the rebinding-validation test
needs to install its own getaddrinfo monkeypatch, so it must not
collide with an autouse fixture."""

import pytest

from grant_lab.proposal_assist.references.fetcher import (
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


def test_rejects_cgnat_shared_address_space():
    """Carrier-grade NAT (100.64.0.0/10) is refused (review 2026-10-26 A7 Low;
    10-06 item 52 — the module docstring promised CGNAT and the code did not).

    Asserted against `_resolve_and_validate` rather than `fetch_bytes` on
    purpose: a literal IP takes the no-DNS shortcut either way, but if the guard
    is missing `fetch_bytes` would open a real TCP connection to 100.64.0.1 and
    put a stray SYN on the wire. This form proves the refusal with no socket in
    either direction.
    """
    from grant_lab.proposal_assist.references import fetcher

    with pytest.raises(SSRFError, match="private"):
        fetcher._resolve_and_validate("100.64.0.1", 80)
    with pytest.raises(SSRFError, match="private"):
        fetcher._resolve_and_validate("100.127.255.254", 443)


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

    from grant_lab.proposal_assist.references import fetcher

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


@pytest.fixture
def local_http_server():
    """A real HTTP server on 127.0.0.1 and an EPHEMERAL port (port 0, then read
    back what the OS assigned — never a fixed port, so this can never collide
    with the dev app on :8000/:5173 or with a parallel xdist worker).

    Yields the port. Teardown runs in a `finally`, so the listener is closed
    even when the test fails — a stray listener would keep the run alive.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_GET(self):  # noqa: N802  (stdlib handler naming)
            body = b"%PDF-local"
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # keep pytest's output clean

    # Constructed BEFORE the test body installs its getaddrinfo stub: binding
    # calls socket.getfqdn(), and a stubbed resolver has no business answering
    # the server's own bind.
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_pinned_connect_targets_the_validated_ip_on_a_real_socket(local_http_server, monkeypatch):
    """The DNS-rebinding pin, proved against a REAL socket.

    review 2026-10-26 M-sec-2: the pin worked but had no test that could fail.
    `test_pinned_connection_resolves_host_exactly_once` above runs under
    pytest-httpx, which REPLACES the transport — so `_PinnedNetworkBackend.
    connect_tcp` never executes there, and a mutant that ignored the pin
    (`connect_tcp(host, ...)` instead of `connect_tcp(pinned, ...)`) left 210
    tests green. This test opens a real TCP connection through the pinned
    transport, so that mutant fails here.

    Shape:
      * a real server on 127.0.0.1:<ephemeral>;
      * `getaddrinfo` answers the FIRST lookup for `pinned.test` with 127.0.0.1
        and raises on any SECOND lookup — a second lookup IS the rebinding
        window, and is exactly what an ignored pin produces;
      * 127.0.0.1 is allowed through the one seam the resolver consults
        (`net_safety.is_private_ip`), because a local server cannot listen on a
        public address. Every other address keeps the production answer.

    Asserts one lookup, the pin map that was built, and that the socket layer
    was handed the PINNED IP rather than the hostname.
    """
    import socket as socket_mod

    from grant_lab import net_safety
    from grant_lab.proposal_assist.references import fetcher

    port = local_http_server
    host = "pinned.test"

    lookups: list[str] = []
    real_getaddrinfo = socket_mod.getaddrinfo

    def one_shot_getaddrinfo(h, p, *args, **kwargs):
        if h != host:
            return real_getaddrinfo(h, p, *args, **kwargs)
        lookups.append(h)
        if len(lookups) > 1:
            raise socket_mod.gaierror(
                socket_mod.EAI_NONAME,
                f"second lookup for {h!r} — the connection pin was ignored",
            )
        return [(socket_mod.AF_INET, socket_mod.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(socket_mod, "getaddrinfo", one_shot_getaddrinfo)

    real_is_private_ip = net_safety.is_private_ip
    monkeypatch.setattr(
        net_safety,
        "is_private_ip",
        lambda ip_str: False if ip_str == "127.0.0.1" else real_is_private_ip(ip_str),
    )

    # Record what the pinning backend hands DOWN to the real network backend.
    # Spying on the pinning backend's OWN argument would be useless: httpcore
    # always passes it the origin hostname. The pin is the value it forwards.
    inner_connects: list[tuple[str, int]] = []
    pin_maps: list[dict[str, str]] = []
    real_builder = fetcher._pinned_transport

    def recording_transport(pin_map):
        pin_maps.append(dict(pin_map))
        transport = real_builder(pin_map)
        pinning_backend = transport._pool._network_backend
        inner = pinning_backend._inner

        class _Recorder:
            def connect_tcp(self, h, p, *args, **kwargs):
                inner_connects.append((h, p))
                return inner.connect_tcp(h, p, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(inner, name)

        pinning_backend._inner = _Recorder()
        return transport

    monkeypatch.setattr(fetcher, "_pinned_transport", recording_transport)

    body = fetch_bytes(f"http://{host}:{port}/x.pdf")

    assert body == b"%PDF-local"
    # Exactly ONE resolution: the validation lookup. A second one is the rebind.
    assert lookups == [host]
    assert pin_maps == [{host: "127.0.0.1"}]
    # The socket was opened to the PINNED literal IP, not the re-resolvable name.
    assert inner_connects == [("127.0.0.1", port)]


# ---------------------------------------------------------------------------
# review 2026-10-26 Minor 4 — the cause chain survives the domain translation
# ---------------------------------------------------------------------------


def test_a_refusal_keeps_the_net_safety_error_as_its_cause():
    """`_resolve_and_validate` re-homes `net_safety`'s errors in this module's
    domain. `raise ... from None` severed the chain, so a traceback showed only
    the translated error and nothing about which check fired.

    The response body is a different question — it stays generic (M-sec-1).
    This is the server-side traceback.
    """
    from grant_lab.net_safety import BlockedAddress
    from grant_lab.proposal_assist.references import fetcher

    with pytest.raises(SSRFError) as exc:
        fetcher._resolve_and_validate("10.20.30.40", 80)
    assert isinstance(exc.value.__cause__, BlockedAddress), exc.value.__cause__


def test_a_dns_failure_keeps_the_gaierror_in_its_cause_chain(monkeypatch):
    """The whole argument for keeping the detail is debuggability, and the
    resolver's own `socket.gaierror` is the most debuggable thing in it:
    EAI_NONAME and EAI_AGAIN are different operational problems.

    `net_safety` already chains `gaierror -> ResolutionFailed`; the fetcher's
    `from None` dropped the pair. Asserted two links deep so restoring either
    `from None` reddens it.
    """
    import socket

    from grant_lab.net_safety import ResolutionFailed
    from grant_lab.proposal_assist.references import fetcher

    def _fail(*args, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _fail)
    with pytest.raises(FetchError) as exc:
        fetcher._resolve_and_validate("nowhere.hospital.internal", 80)
    cause = exc.value.__cause__
    assert isinstance(cause, ResolutionFailed), cause
    assert isinstance(cause.__cause__, socket.gaierror), cause.__cause__
