"""Every route outside a short, reviewed allowlist carries an auth dependency.

Auth in this app is per-route (`Depends(get_current_user)` / `require_admin`),
not middleware. There is no deny-by-default path check, so a route becomes public
by OMITTING a line — invisible in a diff and invisible at runtime. This gate is
the only thing that notices.

NOTE the allowlist is phrased as an allowlist, not as "/api/public/* is the only
unauthenticated prefix". That phrasing is false: authentication cannot precede
login, so /api/auth/login, /api/auth/register and /healthz are unauthenticated
today and must stay so. Adding an entry here is the reviewed act.

This is an AUTHENTICATION gate, not an authorization one: require_admin and
get_current_user both satisfy it, so swapping require_admin for
get_current_user on an /api/admin route stays green here. Route-level
authorization is each endpoint's own business.

The walk can only judge a route that carries both `.methods` and
`.dependant`. Everything else is pinned in UNJUDGEABLE_ROUTES below and
asserted there, so a route cannot become invisible to this gate silently.
"""

from __future__ import annotations

import pytest

from grant_lab.auth.dependencies import get_current_user, require_admin

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
        # The corridor board. Deliberately unauthenticated: it is a screen on a
        # wall, and §6's payload carries no score, no rationale and no profile
        # identity. Rate-limited at 60/15min per IP and cached ~5 minutes.
        ("GET", "/api/public/boards/{slug}"),
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
    }.items():
        monkeypatch.setenv(k, v)


# SPA_MODE changes the route table: `dev` adds the four FastAPI docs routes
# (main.py, create_app). The enforcing direction must run in every mode that
# mounts something, or a route added inside the dev block is governed by no test
# at all. `prod` is deliberately absent: mount_spa raises
# unless src/grant_lab/web/spa/index.html exists (spa_serving.py:37), so a
# prod-mode case would couple this gate to a frontend build artefact.
@pytest.mark.parametrize("spa_mode", ["disabled", "dev"])
def test_no_unlisted_route_is_public(make_app, monkeypatch, _env, spa_mode):
    monkeypatch.setenv("SPA_MODE", spa_mode)
    unlisted = sorted(_auth_free_routes(make_app()) - PUBLIC_ROUTES)
    assert unlisted == [], (
        f"SPA_MODE={spa_mode}: these routes have no auth "
        f"dependency and are not on the reviewed allowlist: {unlisted}"
    )


def test_the_allowlist_has_no_dead_entries(make_app, monkeypatch, _env):
    """An allowlist that outlives its routes stops being reviewed.

    Runs in SPA_MODE=dev, the mode that mounts the most routes.
    """
    monkeypatch.setenv("SPA_MODE", "dev")
    dead = sorted(PUBLIC_ROUTES - _auth_free_routes(make_app()))
    assert dead == [], f"allowlist entries that no longer match a public route: {dead}"


# Routes `_auth_free_routes` structurally CANNOT judge, and why each is safe.
# The walk needs both `.methods` and `.dependant`; anything missing either is
# skipped in silence. Measured 2026-09-03 across both editions x
# {disabled, dev}: exactly six. The workspace's collab websocket left with the
# workspace on 2026-11-03, leaving exactly these five, and no others.
#
# Without this pin the module docstring's claim — "every route outside the
# allowlist carries an auth dependency" — is broader than what the gate checks,
# and the next person to add an allowlist entry would trust a guarantee that
# does not exist.
UNJUDGEABLE_ROUTES = frozenset(
    {
        # StaticFiles mount: no dependant. Serves favicon.svg only (main.py's
        # /static mount), deliberately public.
        "/static",
        # FastAPI's own docs, registered through Starlette's add_route, so they
        # have `.methods` but no `.dependant`. Mounted only when SPA_MODE=dev
        # (main.py, create_app); that gating is pinned separately by
        # tests/test_docs_gated_in_prod.py.
        "/docs",
        "/docs/oauth2-redirect",
        "/openapi.json",
        "/redoc",
    }
)


def _unjudgeable_routes(app) -> set[str]:
    """Paths the auth walk skips because they lack `.methods` or `.dependant`."""
    return {
        getattr(r, "path", repr(r))
        for r in app.routes
        if getattr(r, "methods", None) is None or getattr(r, "dependant", None) is None
    }


@pytest.mark.parametrize("spa_mode", ["disabled", "dev"])
def test_the_walk_judges_every_route_it_does_not_pin(make_app, monkeypatch, _env, spa_mode):
    """No route may become invisible to the gate without a reviewed line here."""
    monkeypatch.setenv("SPA_MODE", spa_mode)
    invisible = sorted(_unjudgeable_routes(make_app()) - UNJUDGEABLE_ROUTES)
    assert invisible == [], (
        f"SPA_MODE={spa_mode}: these routes are skipped by "
        f"_auth_free_routes and are not pinned as reviewed exclusions, so the "
        f"gate cannot see whether they are public: {invisible}"
    )


def test_the_unjudgeable_pins_have_no_dead_entries(make_app, monkeypatch, _env):
    """An exemption that outlives its route stops being reviewed. dev mounts the docs."""
    monkeypatch.setenv("SPA_MODE", "dev")
    dead = sorted(UNJUDGEABLE_ROUTES - _unjudgeable_routes(make_app()))
    assert dead == [], f"exemptions with no matching route: {dead}"
