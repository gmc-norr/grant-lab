"""`grant_lab.net_safety` — the ONE place the application decides whether an
address is safe to open a socket to, and the one DNS-pinned transport.

Why one module (review 2026-10-26, A7 "One SSRF module"): the checks used to
live in two copies — `proposal_assist/references/fetcher.py` (member-typed
reference URLs) and `sources/types.py` (the ingestion adapters). The copies had
drifted into the same gap and had very different test coverage:

  * both refused `ip.is_private or is_loopback or is_link_local or is_multicast
    or is_reserved or is_unspecified` and NEITHER refused 100.64.0.0/10, even
    though the fetcher docstring claimed CGNAT was covered (A7 Low, open since
    the 2026-10-06 review's item 52);
  * the `sources` pinned backend was unit-tested; the fetcher's was not, and a
    mutant that ignored the pin left 210 tests green (M-sec-2).

The real-socket proof for the pin lives beside the fetcher it protects, in
tests/proposal_assist/references/test_fetcher_ssrf.py.
"""

from __future__ import annotations

import ast
import ipaddress
import pathlib
import socket

import httpx
import pytest

from grant_lab.net_safety import (
    SSRF_REFUSAL_MESSAGE,
    BlockedAddress,
    PinnedNetworkBackend,
    ResolutionFailed,
    is_explicitly_blocked,
    is_not_global,
    is_private_ip,
    pinned_transport,
    resolve_public_ip,
)

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "grant_lab"


def _fake_getaddrinfo(*ips):
    def _inner(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]

    return _inner


# --------------------------------------------------------------------------
# The predicate
# --------------------------------------------------------------------------


def test_cgnat_shared_address_space_is_refused():
    """100.64.0.0/10. A7 Low, and the gap both copies shared.

    `ipaddress` reports `is_private=False` for it, so no explicit check matched
    — only `not ip.is_global` does.
    """
    assert is_private_ip("100.64.0.1") is True
    assert is_private_ip("100.127.255.254") is True


def test_the_classic_internal_ranges_are_still_refused():
    for addr in (
        "127.0.0.1",
        "10.0.0.1",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",  # cloud metadata
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "2002:7f00:1::1",  # 6to4 wrapping 127.0.0.1
    ):
        assert is_private_ip(addr) is True, addr


def test_public_addresses_are_allowed():
    for addr in ("8.8.8.8", "93.184.216.34", "1.2.3.4", "2001:4860:4860::8888"):
        assert is_private_ip(addr) is False, addr


def test_an_unparseable_address_is_refused():
    """Fail closed: anything `ipaddress` cannot parse is not connectable."""
    assert is_private_ip("") is True
    assert is_private_ip("not-an-ip") is True
    assert is_private_ip("127.0.0.1:80") is True


def test_each_layer_alone_refuses_rfc1918():
    """Belt AND braces: for 10.0.0.1 both layers say no, independently.

    Asserted per layer so neither can rot silently behind the other — the
    combined predicate would stay green if either half were deleted.
    """
    ip = ipaddress.ip_address("10.0.0.1")
    assert is_not_global(ip) is True
    assert is_explicitly_blocked(ip) is True


def test_neither_layer_subsumes_the_other():
    """Each layer catches an address the other one misses, so deleting either
    is a real loss of coverage and not a tidy-up.

    * 100.64.0.1 (CGNAT): `is_global` False, explicit checks all False.
    * 64:ff9b::7f00:1 (the NAT64 form of 127.0.0.1): `is_global` TRUE, caught
      only by the explicit `is_reserved` check.
    """
    cgnat = ipaddress.ip_address("100.64.0.1")
    assert is_not_global(cgnat) is True
    assert is_explicitly_blocked(cgnat) is False

    nat64 = ipaddress.ip_address("64:ff9b::7f00:1")
    assert is_not_global(nat64) is False
    assert is_explicitly_blocked(nat64) is True

    assert is_private_ip("100.64.0.1") is True
    assert is_private_ip("64:ff9b::7f00:1") is True


# --------------------------------------------------------------------------
# The single resolver
# --------------------------------------------------------------------------


def test_a_public_literal_ip_needs_no_dns(monkeypatch):
    def _explode(*args, **kwargs):
        raise AssertionError("a literal IP must not be resolved")

    monkeypatch.setattr(socket, "getaddrinfo", _explode)
    assert resolve_public_ip("8.8.8.8", 443) == "8.8.8.8"


def test_a_blocked_literal_ip_is_refused_with_the_detail():
    with pytest.raises(BlockedAddress) as exc:
        resolve_public_ip("10.20.30.40", 80)
    # The detail exists for the LOG. web/api/references.py must not put it on
    # the wire (M-sec-1) — tests/test_api_references.py pins that.
    assert "10.20.30.40" in str(exc.value)
    assert "private IP" in str(exc.value)
    # Review 2026-10-26 Minor 4/6: a literal-IP host used to read
    # "refused to connect to '10.20.30.40' → private IP '10.20.30.40'" — the
    # same address twice, in every log line. There is no hostname to name here,
    # so say it once.
    assert str(exc.value).count("10.20.30.40") == 1, str(exc.value)


def test_a_resolved_name_names_both_the_name_and_the_address(monkeypatch):
    """The two-part form is only for the case that HAS two parts: the name the
    member typed and the address it resolved to."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("10.0.0.5"))
    with pytest.raises(BlockedAddress) as exc:
        resolve_public_ip("pacs.hospital.internal", 80)
    assert "pacs.hospital.internal" in str(exc.value)
    assert "10.0.0.5" in str(exc.value)


def test_the_generic_refusal_says_nothing_about_the_address():
    """The one sentence BOTH member-facing layers answer with — the web route
    and the `fetch_reference` agent tool (review 2026-10-26 M-sec-1). It lives
    here, beside the detail it replaces, so there is one string and not two
    literals to drift apart."""
    assert SSRF_REFUSAL_MESSAGE == "That address isn't allowed."
    # A constant, with no %s / {} seam a later edit could thread a detail back
    # through: the whole value of the sentence is that it is the same for every
    # address.
    assert "%" not in SSRF_REFUSAL_MESSAGE
    assert "{" not in SSRF_REFUSAL_MESSAGE


def test_a_name_resolves_to_its_first_public_answer(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34", "8.8.8.8"))
    assert resolve_public_ip("example.com", 443) == "93.184.216.34"


def test_a_mixed_answer_set_is_refused(monkeypatch):
    """One internal answer poisons the whole name: a round-robin record with a
    public and a private address must not be reachable by luck of ordering."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34", "10.0.0.5"))
    with pytest.raises(BlockedAddress):
        resolve_public_ip("mixed.example", 443)


def test_resolution_failure_is_distinguishable_from_a_refusal(monkeypatch):
    """Two different outcomes, because the callers map them differently: the
    fetcher answers 502 for a DNS failure and 400 for a refusal."""

    def _fail(*args, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "nope")

    monkeypatch.setattr(socket, "getaddrinfo", _fail)
    with pytest.raises(ResolutionFailed):
        resolve_public_ip("nowhere.invalid", 443)

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo())
    with pytest.raises(ResolutionFailed):
        resolve_public_ip("empty.invalid", 443)


def test_resolve_public_ip_resolves_exactly_once(monkeypatch):
    calls: list[str] = []

    def counting(host, port, *args, **kwargs):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", counting)
    resolve_public_ip("example.com", 443)
    assert calls == ["example.com"]


# --------------------------------------------------------------------------
# The pinned transport
# --------------------------------------------------------------------------


class _FakeInner:
    def __init__(self):
        self.calls: list[tuple[str, int]] = []

    def connect_tcp(self, host, port, *args, **kwargs):
        self.calls.append((host, port))
        return "conn"

    def connect_unix_socket(self, path, *args, **kwargs):
        return f"unix:{path}"


def test_a_pinned_host_connects_to_its_validated_ip():
    inner = _FakeInner()
    backend = PinnedNetworkBackend(
        inner, {"example.com": "93.184.216.34"}, resolve=resolve_public_ip
    )
    assert backend.connect_tcp("example.com", 443) == "conn"
    assert inner.calls == [("93.184.216.34", 443)]


def test_an_unpinned_host_is_validated_and_pinned_on_the_fly(monkeypatch):
    """Defense in depth: a connect for a host nobody pre-pinned must not fall
    through to the raw re-resolvable hostname — that is the rebinding window
    the pin map exists to close."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    inner = _FakeInner()
    backend = PinnedNetworkBackend(inner, {}, resolve=resolve_public_ip)
    backend.connect_tcp("other.example", 80)
    assert inner.calls == [("93.184.216.34", 80)]


def test_an_unpinned_host_resolving_internal_is_refused(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("10.0.0.1"))
    inner = _FakeInner()
    backend = PinnedNetworkBackend(inner, {}, resolve=resolve_public_ip)
    with pytest.raises(BlockedAddress):
        backend.connect_tcp("rebind.example", 80)
    assert inner.calls == []


def test_the_resolver_is_the_callers_own():
    """Each caller supplies its own error domain — the fetcher raises SSRFError,
    the ingestion adapters raise AdapterError — so the shared backend takes the
    resolver rather than hard-coding one exception type."""

    class _Domain(Exception):
        pass

    def _refuse(host, port):
        raise _Domain(f"{host}:{port}")

    backend = PinnedNetworkBackend(_FakeInner(), {}, resolve=_refuse)
    with pytest.raises(_Domain, match="rebind.example:80"):
        backend.connect_tcp("rebind.example", 80)


def test_everything_else_is_delegated_to_the_real_backend():
    backend = PinnedNetworkBackend(_FakeInner(), {}, resolve=resolve_public_ip)
    assert backend.connect_unix_socket("/tmp/sock") == "unix:/tmp/sock"


def test_pinned_transport_installs_the_backend_on_the_pool():
    transport = pinned_transport({"example.com": "93.184.216.34"}, resolve=resolve_public_ip)
    try:
        assert isinstance(transport, httpx.HTTPTransport)
        backend = transport._pool._network_backend
        assert isinstance(backend, PinnedNetworkBackend)
        assert backend._pin_map == {"example.com": "93.184.216.34"}
    finally:
        transport.close()


# --------------------------------------------------------------------------
# One module, not two
# --------------------------------------------------------------------------


def _definitions(names: set[str]) -> dict[str, list[str]]:
    """Which files under src/grant_lab BIND each of `names` at module scope —
    `def`, `async def`, `class`, or an assignment.

    The assignment arm is not decoration (review 2026-10-26 Minor 3): a second
    copy does not have to be a `def`. ``_is_private_ip = is_private_ip`` is a
    re-export, and ``pinned_transport = _my_own_builder`` shadows the import
    it sits next to — both invisible to a def/class-only walk, and both exactly
    the drift this gate exists to catch.

    Pure `ast` — the application is never imported.
    """
    found: dict[str, list[str]] = {n: [] for n in names}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                and node.name in names
            ):
                found[node.name].append(str(path.relative_to(SRC)))
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    for bound in ast.walk(target):
                        if isinstance(bound, ast.Name) and bound.id in names:
                            found[bound.id].append(str(path.relative_to(SRC)))
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id in names
            ):
                found[node.target.id].append(str(path.relative_to(SRC)))
    return found


def test_the_address_checks_are_defined_in_exactly_one_module():
    """The point of the merge: no second copy to drift.

    Named after every spelling the two copies used, so restoring any of them
    anywhere else in the package turns this red.

    `pinned_transport` is in the set because a re-added local
    ``def pinned_transport`` in `sources/types.py` would SHADOW the
    `net_safety` import sitting above it and quietly take over — the pin is
    the one primitive whose silent replacement nothing else would notice
    (review 2026-10-26 Minor 3). `SSRF_REFUSAL_MESSAGE` is in it because the
    sentence two layers answer with has to be one string, not two literals
    that drift apart.
    """
    found = _definitions(
        {
            "is_private_ip",
            "_is_private_ip",
            "_ip_is_blocked",
            "is_not_global",
            "is_explicitly_blocked",
            "resolve_public_ip",
            "pinned_transport",
            "PinnedNetworkBackend",
            "_PinnedNetworkBackend",
            "SSRF_REFUSAL_MESSAGE",
        }
    )
    for name in (
        "is_private_ip",
        "is_not_global",
        "is_explicitly_blocked",
        "resolve_public_ip",
        "pinned_transport",
        "SSRF_REFUSAL_MESSAGE",
    ):
        assert found[name] == ["net_safety.py"], (name, found[name])
    assert found["PinnedNetworkBackend"] == ["net_safety.py"], found["PinnedNetworkBackend"]
    # The old private spellings are gone, not re-homed.
    for name in ("_is_private_ip", "_ip_is_blocked", "_PinnedNetworkBackend"):
        assert found[name] == [], (name, found[name])


def test_net_safety_imports_nothing_from_the_application():
    """It is a leaf: stdlib + httpx only, so any module can import it without
    dragging a package in (same rule as errors.py)."""
    tree = ast.parse((SRC / "net_safety.py").read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert [m for m in imported if m.startswith("grant_lab")] == [], imported
