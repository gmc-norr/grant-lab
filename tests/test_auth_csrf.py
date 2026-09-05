"""Origin/Referer CSRF middleware — layered on top of SameSite=Lax."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from grant_lab.auth.csrf import OriginCSRFMiddleware


def _mk_app(base_url: str = "http://testserver") -> FastAPI:
    app = FastAPI()
    app.add_middleware(OriginCSRFMiddleware, expected_base_url=base_url)

    @app.get("/ping")
    def _ping():
        return {"ok": True}

    @app.post("/do")
    def _do():
        return {"ok": True}

    return app


def test_post_without_origin_or_referer_is_allowed():
    """TestClient / curl / SSR callers don't send Origin — must not be
    refused. Browsers always send Origin on cross-site POST, so a missing
    Origin implies a non-browser caller, which isn't a CSRF threat."""
    app = _mk_app()
    with TestClient(app) as c:
        r = c.post("/do")
    assert r.status_code == 200


def test_post_with_matching_origin_is_allowed():
    app = _mk_app("http://testserver")
    with TestClient(app) as c:
        r = c.post("/do", headers={"Origin": "http://testserver"})
    assert r.status_code == 200


def test_post_with_mismatched_origin_is_rejected():
    """A cross-site Origin is the hallmark of a CSRF attempt."""
    app = _mk_app("http://testserver")
    with TestClient(app) as c:
        r = c.post("/do", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_post_with_null_origin_is_rejected():
    """Sandboxed iframes and data: URLs surface as `Origin: null`. A
    legitimate same-origin form never produces this."""
    app = _mk_app("http://testserver")
    with TestClient(app) as c:
        r = c.post("/do", headers={"Origin": "null"})
    assert r.status_code == 403


def test_post_falls_back_to_referer_when_origin_absent():
    """Some configurations strip Origin; Referer is the next-best signal."""
    app = _mk_app("http://testserver")
    with TestClient(app) as c:
        ok = c.post("/do", headers={"Referer": "http://testserver/profiles"})
        bad = c.post("/do", headers={"Referer": "https://evil.example/x"})
    assert ok.status_code == 200
    assert bad.status_code == 403


def test_get_is_never_checked():
    """Safe methods (per RFC 7231 §4.2.1) mustn't be side-effectful, so
    there's nothing CSRF can exploit. Never gate them."""
    app = _mk_app("http://testserver")
    with TestClient(app) as c:
        r = c.get("/ping", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200


def test_loopback_aliases_are_treated_as_same_origin():
    """`localhost`, `127.0.0.1`, and `[::1]` all route to the same host.
    A dev box configured for one must accept the others, or VSCode port
    forwarding (which usually presents `127.0.0.1` to the browser even
    when the app is configured for `localhost`) breaks every POST."""
    app = _mk_app("http://localhost:8000")
    with TestClient(app) as c:
        for origin in (
            "http://localhost:8000",
            "http://127.0.0.1:8000",
            "http://[::1]:8000",
        ):
            r = c.post("/do", headers={"Origin": origin})
            assert r.status_code == 200, f"rejected {origin}"
    # But a non-loopback host with the same port is still cross-origin.
    app = _mk_app("http://localhost:8000")
    with TestClient(app) as c:
        r = c.post("/do", headers={"Origin": "http://192.168.1.5:8000"})
    assert r.status_code == 403


def test_origin_matches_when_port_included():
    """`example.com:8000` must match `example.com:8000` but not bare
    `example.com` — port is part of the same-origin definition."""
    app = _mk_app("http://example.com:8000")
    with TestClient(app) as c:
        ok = c.post("/do", headers={"Origin": "http://example.com:8000"})
        bad = c.post("/do", headers={"Origin": "http://example.com"})
    assert ok.status_code == 200
    assert bad.status_code == 403


def test_scheme_downgrade_origin_is_rejected():
    """An http:// Origin must NOT match an https:// configured origin — scheme
    is part of the same-origin definition; ignoring it would let a network MITM
    forge a same-origin-looking request over plaintext."""
    app = _mk_app("https://example.com")
    with TestClient(app) as c:
        r = c.post("/do", headers={"Origin": "http://example.com"})
    assert r.status_code == 403


def test_malformed_origin_port_is_rejected_not_500():
    """A garbled Origin port must yield a clean 403 mismatch, not a 500 from
    urlparse's lazy .port ValueError."""
    app = _mk_app("https://example.com")
    with TestClient(app) as c:
        r = c.post("/do", headers={"Origin": "https://example.com:notaport"})
    assert r.status_code == 403
