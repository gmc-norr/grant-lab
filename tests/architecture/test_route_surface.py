"""The app's route surface: what it serves, and that the removed workspace half is absent.

There is one build. The proposal workspace and the match/full edition split left
master on 2026-11-03 (docs/superpowers/specs/2026-11-03-workspace-removal-design.md);
the workspace is frozen on the `workspace` branch. Nothing here may serve any of it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI, WebSocket
from starlette.routing import WebSocketRoute

# Prefixes the removed workspace owned. No route may be registered under any of them.
# A blocklist only catches an ANTICIPATED return; the baseline equality below is the
# self-maintaining half (any unintended route fails it).
REMOVED_PREFIXES = (
    "/workspaces",
    "/api/workspaces",
    "/api/ws/",
    "/api/turns",
    "/api/annotations",
    "/api/rubric",
    "/api/references",
    "/api/citation-audit",
    "/api/export",
    "/api/files",
    "/api/admin/drafting",
    "/api/admin/workspaces",
)


@pytest.fixture
def _base_env(monkeypatch):
    for k, v in {
        "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5999/nope",
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "umu.se",
        "APP_BASE_URL": "http://testserver",
        "SPA_MODE": "disabled",
    }.items():
        monkeypatch.setenv(k, v)


def _paths(app) -> set[str]:
    """HTTP route paths only."""
    return {r.path for r in app.routes if hasattr(r, "methods")}


def _ws_paths(app) -> set[str]:
    """WebSocket routes. FastAPI's APIWebSocketRoute subclasses Starlette's
    WebSocketRoute and carries no `.methods`, so a methods filter drops it."""
    return {r.path for r in app.routes if isinstance(r, WebSocketRoute)}


@pytest.mark.parametrize("spa_mode", ["disabled", "dev"])
def test_no_removed_workspace_route_is_registered(make_app, monkeypatch, _base_env, spa_mode):
    monkeypatch.setenv("SPA_MODE", spa_mode)
    app = make_app()
    leaked = sorted(p for p in _paths(app) | _ws_paths(app) if p.startswith(REMOVED_PREFIXES))
    assert leaked == [], f"a removed workspace route is registered: {leaked}"


@pytest.mark.parametrize("spa_mode", ["disabled", "dev"])
def test_no_websocket_route_is_registered(make_app, monkeypatch, _base_env, spa_mode):
    """Live editing was the only websocket. A new one is a product decision."""
    monkeypatch.setenv("SPA_MODE", spa_mode)
    assert _ws_paths(make_app()) == set()


def test_the_websocket_helper_can_see_a_websocket_route():
    """Guards the guard: without this, the test above passes on a helper that
    sees nothing."""
    app = FastAPI()

    @app.websocket("/probe")
    async def _probe(ws: WebSocket):  # pragma: no cover - never connected
        await ws.close()

    assert _ws_paths(app) == {"/probe"}


def test_the_app_serves_the_golden_set_api(make_app, _base_env):
    assert "/api/admin/golden-set" in _paths(make_app())


def test_the_app_serves_the_release_1_surface(make_app, _base_env):
    paths = _paths(make_app())
    for required in (
        "/api/calls",
        "/api/profiles",
        "/api/me",
        "/api/admin/users",
        "/api/boards",
        "/api/public/boards/{slug}",
        "/healthz",
    ):
        assert required in paths, f"the app dropped {required}"


# The app's complete route set (unique PATHS, not operations; HTTP, websocket and
# mounts). Regenerate ONLY when a route change is intended, and bump
# _BASELINE_SIZE BY HAND in the same commit, so a regeneration alone cannot wave
# through a route that arrived without intent:
#
#   DATABASE_URL=postgresql+psycopg://u:p@localhost:5999/nope \
#   SESSION_SECRET=test-secret-at-least-16-chars ALLOWED_EMAIL_DOMAINS=umu.se \
#   APP_BASE_URL=http://testserver SPA_MODE=disabled uv run python -c \
#     "from grant_lab.config import Settings; Settings.model_config['env_file'] = None; \
#      from grant_lab.main import create_app; \
#      print('\n'.join(sorted({r.path for r in create_app().routes})))" \
#     > tests/architecture/app_routes.txt
#
# 2026-11-03, workspace removal: 120 -> 51. The old full_edition_routes.txt held
# the FULL edition; all 69 paths that left are under REMOVED_PREFIXES, and none
# was added.
_BASELINE = Path(__file__).with_name("app_routes.txt")
_BASELINE_SIZE = 51


def test_the_route_set_matches_the_committed_baseline(make_app, _base_env):
    live = sorted({r.path for r in make_app().routes})
    expected = _BASELINE.read_text().split()
    added = sorted(set(live) - set(expected))
    removed = sorted(set(expected) - set(live))
    assert (added, removed) == ([], []), (
        f"the route surface moved: +{added} -{removed}\n"
        f"If intended, regenerate {_BASELINE.name} and bump _BASELINE_SIZE in the same commit."
    )


def test_the_baseline_is_not_empty():
    assert len(_BASELINE.read_text().split()) == _BASELINE_SIZE


# The routers web/api/__init__.py mounts, pinned by NAME: adding one is a product
# decision, made in two places so a reviewer sees it.
_ROUTER_TUPLE = "_ROUTERS"  # the module-level tuple name in web/api/__init__.py
_ROUTERS = [
    "auth.router",
    "me.router",
    "admin.router",
    "admin_users.router",
    "calls.router",
    "profiles.router",
    "boards.router",
    "public_boards.router",
    "admin_golden_set.router",
]


def _tuple_elements(name: str) -> list[str]:
    src = (Path(__file__).resolve().parents[2] / "src/grant_lab/web/api/__init__.py").read_text()
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            assert isinstance(node.value, ast.Tuple), f"{name} is not a tuple literal"
            return [ast.unparse(e) for e in node.value.elts]
    raise AssertionError(f"no module-level tuple {name} in web/api/__init__.py")


def test_the_mounted_routers_are_exactly_the_reviewed_set():
    assert _tuple_elements(_ROUTER_TUPLE) == _ROUTERS
