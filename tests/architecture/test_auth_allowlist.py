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

from pathlib import Path

import pytest

from grant_lab.auth.dependencies import get_current_user, require_admin
from grant_lab.main import create_app

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


# SPA_MODE changes the route table: `dev` adds /events/ping and the four FastAPI
# docs routes (main.py:488-499, :906-912). The enforcing direction must run in
# every mode that mounts something, or a route added inside the dev block is
# governed by no test at all. `prod` is deliberately absent: mount_spa raises
# unless src/grant_lab/web/spa/index.html exists (spa_serving.py:37), so a
# prod-mode case would couple this gate to a frontend build artefact.
@pytest.mark.parametrize("spa_mode", ["disabled", "dev"])
@pytest.mark.parametrize("edition", ["full", "match"])
def test_no_unlisted_route_is_public(monkeypatch, _env, edition, spa_mode):
    monkeypatch.setenv("APP_EDITION", edition)
    monkeypatch.setenv("SPA_MODE", spa_mode)
    unlisted = sorted(_auth_free_routes(create_app()) - PUBLIC_ROUTES)
    assert unlisted == [], (
        f"{edition} edition, SPA_MODE={spa_mode}: these routes have no auth "
        f"dependency and are not on the reviewed allowlist: {unlisted}"
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


# Routes `_auth_free_routes` structurally CANNOT judge, and why each is safe.
# The walk needs both `.methods` and `.dependant`; anything missing either is
# skipped in silence. Measured 2026-09-03 across both editions x
# {disabled, dev}: exactly these six, and no others.
#
# Without this pin the module docstring's claim — "every route outside the
# allowlist carries an auth dependency" — is broader than what the gate checks,
# and the next person to add an allowlist entry would trust a guarantee that
# does not exist.
UNJUDGEABLE_ROUTES = frozenset(
    {
        # APIWebSocketRoute: has a dependant, but no `.methods`. It authenticates
        # in its BODY via resolve_ws_user (proposal_assist/web/collab_ws.py:98) —
        # the same shape as require_admin — so a dependency walk could not see it
        # even with the methods filter relaxed. Full edition only.
        #
        # An outside reviewer read this exemption as "so nothing tests that the
        # websocket authenticates". It does not follow, and it is not true: the
        # guarantee is held end-to-end by
        # tests/proposal_assist/collab/test_collab_ws_route.py::test_unauthenticated_ws_is_rejected,
        # which connects without a cookie and asserts WebSocketDisconnect. That
        # link is pinned below so exempting the route here can never be the last
        # word on it.
        "/api/ws/workspaces/{workspace_id}/{artifact}",
        # StaticFiles mount: no dependant. Serves favicon.svg only (main.py:885),
        # deliberately public.
        "/static",
        # FastAPI's own docs, registered through Starlette's add_route, so they
        # have `.methods` but no `.dependant`. Mounted only when SPA_MODE=dev
        # (main.py:488-499); that gating is pinned separately by
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
@pytest.mark.parametrize("edition", ["full", "match"])
def test_the_walk_judges_every_route_it_does_not_pin(monkeypatch, _env, edition, spa_mode):
    """No route may become invisible to the gate without a reviewed line here."""
    monkeypatch.setenv("APP_EDITION", edition)
    monkeypatch.setenv("SPA_MODE", spa_mode)
    invisible = sorted(_unjudgeable_routes(create_app()) - UNJUDGEABLE_ROUTES)
    assert invisible == [], (
        f"{edition} edition, SPA_MODE={spa_mode}: these routes are skipped by "
        f"_auth_free_routes and are not pinned as reviewed exclusions, so the "
        f"gate cannot see whether they are public: {invisible}"
    )


def test_the_exempted_websocket_is_covered_somewhere_else():
    """An exemption is only honest while the thing it exempts is tested elsewhere.

    `/api/ws/...` is in UNJUDGEABLE_ROUTES because a dependency walk structurally
    cannot see body-level authentication. That is a limit of THIS gate, not a gap
    in the guarantee — but nothing connected the two, so deleting the end-to-end
    test would leave the exemption reading as coverage.
    """
    ws_test = Path(__file__).resolve().parents[1] / "proposal_assist/collab/test_collab_ws_route.py"
    assert ws_test.is_file(), ws_test
    src = ws_test.read_text()
    assert "def test_unauthenticated_ws_is_rejected(" in src
    body = src[src.index("def test_unauthenticated_ws_is_rejected(") :]
    body = body[: body.find("\ndef ")]
    assert "websocket_connect" in body and "WebSocketDisconnect" in body, body[:400]
