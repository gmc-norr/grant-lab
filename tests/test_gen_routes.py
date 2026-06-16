import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_generator_emits_python_and_ts(tmp_path, monkeypatch):
    subprocess.run([sys.executable, "scripts/gen_routes.py"], cwd=ROOT, check=True)
    py = (ROOT / "src/call_manager/web/spa_routes.py").read_text()
    ts = (ROOT / "frontend/src/routes.gen.ts").read_text()
    assert "SPA_ROUTES" in py and "is_spa_route" in py
    assert "BACKEND_PREFIXES" in py and "is_backend_route" in py
    assert "SPA_ROUTES" in ts and "BACKEND_PREFIXES" in ts


def test_is_spa_route_is_exact_not_prefix():
    from call_manager.web.spa_routes import is_spa_route

    assert is_spa_route("/app") is True
    assert is_spa_route("/made-up-page") is False
    assert is_spa_route("/") is False
    assert is_spa_route("/api/me") is False
    assert is_spa_route("/calls/123") is False
    assert is_spa_route("/app/anything") is True


def test_is_backend_route_for_proxy_carveout():
    from call_manager.web.spa_routes import is_backend_route

    assert is_backend_route("/api/me") is True
    assert is_backend_route("/calls/123") is True
    assert is_backend_route("/app") is False
