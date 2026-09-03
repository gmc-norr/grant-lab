"""Every route outside a short, reviewed allowlist carries an auth dependency.

Auth in this app is per-route (`Depends(get_current_user)` / `require_admin`),
not middleware. There is no deny-by-default path check, so a route becomes public
by OMITTING a line — invisible in a diff and invisible at runtime. This gate is
the only thing that notices.

NOTE the allowlist is phrased as an allowlist, not as "/api/public/* is the only
unauthenticated prefix". That phrasing is false: authentication cannot precede
login, so /api/auth/login, /api/auth/register and /healthz are unauthenticated
today and must stay so. Adding an entry here is the reviewed act.
"""

from __future__ import annotations

import pytest

from call_manager.auth.dependencies import get_current_user, require_admin
from call_manager.main import create_app

# require_admin calls get_current_user inside its BODY, not as a sub-dependency,
# so FastAPI's dependant tree does not carry get_current_user on admin routes.
# Both count as authentication.
AUTH_DEPENDENCIES = frozenset({get_current_user, require_admin})

# (method, path) pairs that are deliberately public. Every entry is a decision.
PUBLIC_ROUTES = frozenset(
    {
        ("GET", "/healthz"),  # liveness; no user exists yet
        ("GET", "/api/auth/config"),  # which login methods to render
        ("POST", "/api/auth/login"),  # auth cannot precede login
        ("POST", "/api/auth/register"),
        ("POST", "/api/auth/logout"),  # idempotent; clears the cookie
        ("POST", "/auth/google/start"),  # OIDC redirect
        ("GET", "/auth/google/callback"),  # OIDC return leg
        ("GET", "/events/ping"),  # dev-only SSE probe (SPA_MODE=dev)
    }
)


def _auth_free_routes(app) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for route in app.routes:
        methods = getattr(route, "methods", None)
        dependant = getattr(route, "dependant", None)
        if methods is None or dependant is None:
            continue
        calls: set[object] = set()
        stack = [dependant]
        while stack:
            d = stack.pop()
            if d.call is not None:
                calls.add(d.call)
            stack.extend(d.dependencies)
        if calls & AUTH_DEPENDENCIES:
            continue
        for m in methods:
            if m in ("HEAD", "OPTIONS"):
                continue
            out.add((m, route.path))
    return out


@pytest.fixture
def _env(monkeypatch):
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


@pytest.mark.parametrize("edition", ["full", "match"])
def test_no_unlisted_route_is_public(monkeypatch, _env, edition):
    monkeypatch.setenv("APP_EDITION", edition)
    unlisted = sorted(_auth_free_routes(create_app()) - PUBLIC_ROUTES)
    assert unlisted == [], (
        f"{edition} edition: these routes have no auth dependency and are not on "
        f"the reviewed allowlist: {unlisted}"
    )


def test_the_allowlist_has_no_dead_entries(monkeypatch, _env):
    """An allowlist that outlives its routes stops being reviewed.

    /events/ping is mounted only when SPA_MODE=dev, so this check runs in that
    mode to see the full public set.
    """
    monkeypatch.setenv("APP_EDITION", "full")
    monkeypatch.setenv("SPA_MODE", "dev")
    dead = sorted(PUBLIC_ROUTES - _auth_free_routes(create_app()))
    assert dead == [], f"allowlist entries that no longer match a public route: {dead}"
