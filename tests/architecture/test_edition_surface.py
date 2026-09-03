"""APP_EDITION=match must not register a proposal-assist route.

The edition is a BUILD/DEPLOY decision, not a runtime feature flag: a match
deployment does not mount the routers at all, so there is no endpoint to reach
with a crafted request. This test is the only thing that makes that claim true.
"""

from __future__ import annotations

import pytest

from call_manager.main import create_app

# Path prefixes the proposal-assist half owns. A match build must register NONE
# of these.
#
# Measured 2026-09-03: /api/workspaces alone catches all 64 routes the match
# edition drops — every proposal-assist router in main.py carries
# prefix="/api/workspaces", and api/__init__.py's _FULL_ONLY routers do too.
# The other eight entries match nothing today. They are kept as forward
# declarations, NOT as evidence of eight independent guards.
#
# A prefix blocklist can only catch an ANTICIPATED leak, so
# test_match_edition_drops_only_excluded_routes below pins the stronger,
# self-maintaining property: match's surface is a strict subset of full's,
# and every path full has that match lacks is under one of these prefixes.
# A future proposal-assist router at an unanticipated prefix fails THAT test.
EXCLUDED_PREFIXES = (
    "/workspaces",
    "/api/workspaces",
    # The collab websocket. NOT under /api/workspaces — this entry is the
    # second live one, and test_match_edition_drops_only_excluded_routes is
    # what found it missing.
    "/api/ws/",
    "/api/turns",
    "/api/annotations",
    "/api/rubric",
    "/api/references",
    "/api/citation-audit",
    "/api/export",
    "/api/files",
)


@pytest.fixture
def _base_env(monkeypatch):
    for k, v in {
        "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5999/nope",
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "umu.se",
        "APP_BASE_URL": "http://testserver",
        "SPA_MODE": "disabled",
        # APP_EDITION=match refuses to start with drafting enabled (config.py
        # _match_edition_disables_drafting), and it defaults to True.
        "DRAFTING_ENABLED": "false",
    }.items():
        monkeypatch.setenv(k, v)


def _paths(app) -> set[str]:
    """HTTP route paths only. WebSocket routes have no `.methods` — see _ws_paths."""
    return {r.path for r in app.routes if hasattr(r, "methods")}


def _ws_paths(app) -> set[str]:
    """WebSocket route paths.

    Starlette's APIWebSocketRoute carries no `.methods`, so a filter written as
    `hasattr(r, "methods")` silently drops it. Measured on this app: exactly one
    such route exists, /api/ws/workspaces/{workspace_id}/{artifact}, and it is
    the collab websocket the match edition must not mount. Without this helper
    the edition gate would pass with the websocket wide open.
    """
    return {r.path for r in app.routes if not hasattr(r, "methods") and hasattr(r, "path")}


def test_match_edition_registers_no_proposal_assist_route(monkeypatch, _base_env):
    monkeypatch.setenv("APP_EDITION", "match")
    paths = _paths(create_app())
    leaked = sorted(p for p in paths if p.startswith(EXCLUDED_PREFIXES))
    assert leaked == [], f"match edition leaked proposal-assist routes: {leaked}"


def test_match_edition_mounts_no_collab_websocket(monkeypatch, _base_env):
    """The collab WS is mounted in main.py, not in the api router, and it is
    invisible to a `.methods` filter — so it needs its own assertion."""
    monkeypatch.setenv("APP_EDITION", "match")
    ws = _ws_paths(create_app())
    leaked = sorted(p for p in ws if "/ws/" in p or "collab" in p)
    assert leaked == [], f"match edition leaked a websocket route: {leaked}"


def test_full_edition_still_mounts_the_collab_websocket(monkeypatch, _base_env):
    """Guards the guard: if the WS moves or is renamed, the match assertion above
    becomes vacuously true and nobody would notice."""
    monkeypatch.setenv("APP_EDITION", "full")
    ws = _ws_paths(create_app())
    assert any("/ws/" in p for p in ws), f"no websocket route found at all: {sorted(ws)}"


def test_match_edition_still_serves_the_release_1_surface(monkeypatch, _base_env):
    """The exclusion must not take the product with it."""
    monkeypatch.setenv("APP_EDITION", "match")
    paths = _paths(create_app())
    for required in ("/api/calls", "/api/profiles", "/api/me", "/api/admin/users", "/healthz"):
        assert required in paths, f"match edition dropped {required}"


def test_full_edition_is_unchanged(monkeypatch, _base_env):
    """Default and explicit `full` both keep every route."""
    monkeypatch.setenv("APP_EDITION", "full")
    paths = _paths(create_app())
    assert "/api/workspaces" in paths
    # Turns are mounted at /api/workspaces/{workspace_id}/turns — there is no
    # top-level /api/turns prefix in this app (measured, not assumed).
    assert "/api/workspaces/{workspace_id}/turns" in paths


def test_match_edition_refuses_drafting_enabled(monkeypatch, _base_env):
    monkeypatch.setenv("APP_EDITION", "match")
    monkeypatch.setenv("DRAFTING_ENABLED", "true")
    with pytest.raises(Exception, match="DRAFTING_ENABLED"):
        create_app()


def test_match_edition_accepts_drafting_disabled(monkeypatch, _base_env):
    monkeypatch.setenv("APP_EDITION", "match")
    monkeypatch.setenv("DRAFTING_ENABLED", "false")
    create_app()  # must not raise


def test_match_edition_drops_only_excluded_routes(monkeypatch, _base_env):
    """The self-maintaining half of the gate.

    EXCLUDED_PREFIXES can only catch a leak somebody anticipated. This one needs
    no list kept current: it builds both editions and asserts (a) match's surface
    is a strict subset of full's — the match build may not INVENT a route — and
    (b) every path full has that match lacks is under a declared excluded prefix.
    A future proposal-assist router mounted at an unanticipated prefix, which the
    blocklist would wave through, fails here.
    """
    monkeypatch.setenv("APP_EDITION", "full")
    full = _paths(create_app()) | _ws_paths(create_app())
    monkeypatch.setenv("APP_EDITION", "match")
    match = _paths(create_app()) | _ws_paths(create_app())

    invented = sorted(match - full)
    assert invented == [], f"match edition registered routes full does not have: {invented}"

    dropped = full - match
    assert dropped, "match dropped NOTHING — the edition split is not doing anything"
    undeclared = sorted(p for p in dropped if not p.startswith(EXCLUDED_PREFIXES))
    assert undeclared == [], (
        "match dropped routes that no EXCLUDED_PREFIXES entry declares. Either the "
        f"drop is wrong or the prefix list is stale: {undeclared}"
    )
