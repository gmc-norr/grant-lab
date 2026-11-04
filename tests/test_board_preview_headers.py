import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from grant_lab.web import spa_serving
from grant_lab.web.middleware import SecurityHeadersMiddleware, build_csp


@pytest.mark.parametrize(
    "path,framed",
    [
        ("/b/draft-board", True),
        ("/boards/1?preview=1", True),
        ("/boards/1", False),
        ("/boards/1?preview=0", False),
        ("/profiles/1?preview=1", False),
        ("/login", False),
    ],
)
def test_only_board_displays_allow_same_origin_framing(monkeypatch, tmp_path, path, framed):
    (tmp_path / "index.html").write_text("<!doctype html><title>Preview</title>")
    monkeypatch.setattr(spa_serving, "SPA_DIR", tmp_path)
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware, csp=build_csp(), hsts=True)
    spa_serving.mount_spa(app, spa_mode="prod")
    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == 200
    assert response.headers["x-frame-options"] == ("SAMEORIGIN" if framed else "DENY")
    assert ("frame-ancestors 'self'" if framed else "frame-ancestors 'none'") in response.headers[
        "content-security-policy"
    ]
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "strict-transport-security" in response.headers
