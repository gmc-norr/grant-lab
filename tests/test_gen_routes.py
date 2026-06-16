import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_generated_artifacts_in_sync_and_well_formed():
    # --check regenerates in memory and exits non-zero if the committed
    # artifacts are stale vs frontend/routes.json; it writes nothing, so a
    # forgotten regenerate fails here instead of being silently repaired (the
    # old test ran the writing generator against the real tree, masking drift).
    subprocess.run([sys.executable, "scripts/gen_routes.py", "--check"], cwd=ROOT, check=True)
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


def test_root_owns_only_itself_after_plan3_flip(monkeypatch):
    # Plan 3 flips SPA_ROUTES to ("/",). The load-bearing root special-case must
    # own ONLY "/" — never collapse to match-everything (a naive
    # `path.startswith(r.rstrip("/") + "/")` would, since "/" → "" → matches all
    # paths). Pin it now, before Plan 3 ships the flip: a regression to the naive
    # form passes every other test but fails this one.
    import call_manager.web.spa_routes as sr

    monkeypatch.setattr(sr, "SPA_ROUTES", ("/",))
    assert sr.is_spa_route("/") is True
    assert sr.is_spa_route("/api/x") is False
    assert sr.is_spa_route("/calls/1") is False
    assert sr.is_spa_route("/made-up-page") is False


def test_is_backend_route_for_proxy_carveout():
    from call_manager.web.spa_routes import is_backend_route

    assert is_backend_route("/api/me") is True
    assert is_backend_route("/calls/123") is True
    assert is_backend_route("/app") is False
