"""APP_EDITION=match must not register a proposal-assist route.

The edition is a BUILD/DEPLOY decision, not a runtime feature flag: a match
deployment does not mount the routers at all, so there is no endpoint to reach
with a crafted request. This test is the only thing that makes that claim true.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grant_lab.main import create_app

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
    # The admin routers behind proposal-assist-only pages, moved out of
    # api/__init__.py's _ALWAYS. LIVE entries, not forward declarations.
    #
    # RE-MEASURED 2026-09-07, after admin_golden_set.router went back into
    # _ALWAYS: these two prefixes now cover exactly the FOUR paths (four
    # operations) the move drops, printed by diffing a full and a match app built
    # in separate interpreters —
    #   /api/admin/drafting                                     GET
    #   /api/admin/workspaces/deleted                           GET
    #   /api/admin/workspaces/deleted/{workspace_id}            GET
    #   /api/admin/workspaces/deleted/{workspace_id}/restore    POST
    # It was six paths / seven operations while /api/admin/golden-set was here:
    # the three golden-set operations (GET + POST on /api/admin/golden-set,
    # DELETE on /api/admin/golden-set/{triple_id}) are served in BOTH editions
    # now, so the entry that declared them would be a forward declaration
    # describing nothing.
    #
    # They are NOT under /api/workspaces (the `/admin` segment comes first),
    # which is why the existing entries missed them and why
    # test_match_edition_drops_only_excluded_routes went red on the 2026-09-04
    # move until they were declared here.
    "/api/admin/drafting",
    "/api/admin/workspaces/deleted",
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


def test_match_serves_the_golden_set_api(monkeypatch, _base_env):
    """G13, backend half. The golden-set PAGE is useless without the endpoint
    behind it, and until the 2026-09-07 decision-notes work the match edition
    served neither: admin_golden_set.router sat in _full_only_routers(), so a
    match build had no handler to reach. Task 4 put the humans' own reasons on
    the candidate rows; match is the only edition that ships, so without this
    they had no reader at all.

    _base_env sets DRAFTING_ENABLED=false because a match app refuses to start
    with drafting enabled (config.py _match_edition_disables_drafting).
    """
    monkeypatch.setenv("APP_EDITION", "match")
    paths = _paths(create_app())
    assert "/api/admin/golden-set" in paths


def test_match_edition_still_serves_the_release_1_surface(monkeypatch, _base_env):
    """The exclusion must not take the product with it."""
    monkeypatch.setenv("APP_EDITION", "match")
    paths = _paths(create_app())
    for required in ("/api/calls", "/api/profiles", "/api/me", "/api/admin/users", "/healthz"):
        assert required in paths, f"match edition dropped {required}"


# The full edition's complete route set, as it stood at the branch base f0aaed0c
# and as it must still stand. Regenerate ONLY when a route change is intended:
#
#   APP_EDITION=full DRAFTING_ENABLED=true SPA_MODE=disabled ... python -c \
#     "from grant_lab.main import create_app; \
#      print('\n'.join(sorted({r.path for r in create_app().routes})))" \
#     > tests/architecture/full_edition_routes.txt
#
# Verified byte-identical to a temp checkout of f0aaed0c on 2026-09-04.
#
# 2026-09-04, profile-visibility Task 3: 105 -> 107. The two added paths are
# /api/profiles/{profile_id}/viewers and /api/profiles/{profile_id}/visibility
# (the sharing endpoints). The count below moves with the file, in the same
# commit, so a route added without intent is still red.
#
# 2026-09-05, Call Match release 1 Task 3: 107 -> 109. The two added paths are
# /api/calls/decisions/counts (the triage rail's counts) and
# /api/calls/{call_id}/save. POST and DELETE /save are ONE line here -- this
# file holds unique PATHS, not operations -- and the four `?view=` triage views
# add none, being a query parameter on the existing /api/calls. Regenerating
# this file alone leaves test_the_baseline_is_not_empty red on purpose: the
# literal below is bumped BY HAND in the same commit, so a route that arrives
# without intent cannot be waved through by a regeneration.
#
# Plan C Task 3 (profile transfer): 109 -> 110. One added path,
# /api/profiles/{profile_id}/owner (PUT). Recorded late -- a whole-branch
# review found this entry missing while the literal and the baseline file were
# both already correct, which is exactly the hole this ledger exists to close:
# the numbers agreeing proves nothing about whether the route was intended.
#
# Plan C Task 5 (corridor boards): 110 -> 112. The two added paths are
# /api/boards (GET list, POST create) and /api/boards/{board_id} (GET, PUT,
# DELETE) -- five operations, two unique paths, matching this file's
# unique-PATHS-not-operations convention above.
#
# Plan C Task 6 (approve, un-approve, reorder, publish): 112 -> 116. Four
# added paths: /api/boards/{board_id}/queue (GET), /api/boards/{board_id}/items
# (GET, POST, PUT), /api/boards/{board_id}/items/{call_id} (DELETE) and
# /api/boards/{board_id}/publish (PUT) -- six operations, four unique paths.
#
# Plan C Task 7 (QR encoding and the userless public endpoint): 116 -> 117.
# One added path: /api/public/boards/{slug} (GET) -- the one unauthenticated
# route, deliberately in its own module (web/api/public_boards.py) and its own
# gate exemption (test_boards_permission_single_source.py).
#
# Board auto-publish Task 6 (the switch, and the four numbers): 117 -> 118. One
# added path, /api/boards/{board_id}/auto-preview (GET) -- the read-only
# preview of what an auto board would carry right now. It is authenticated and
# edit-gated like every other {board_id} board route, so unlike Task 7's public
# endpoint it takes no gate exemption; it is pinned in
# test_boards_permission_single_source.py's `_EXPECTED_ROUTES` instead.
#
# 2026-09-07, decision notes Task 1: 118 -> 119, one added path
# /api/calls/{call_id}/note (PUT) -- the write endpoint for a researcher's own
# reason on a decision they already made. It hangs off the existing
# call_decisions row, so it adds no other path and no migration.
_BASELINE = Path(__file__).with_name("full_edition_routes.txt")


def test_full_edition_route_set_matches_the_committed_baseline(monkeypatch, _base_env):
    """The whole set, not two samples.

    This test used to assert that two hardcoded paths were present, under the name
    "full edition is unchanged" — a full app that had dropped the other 103 routes
    passed it. Two outside reviewers named that independently. The 105-vs-105
    byte-identical comparison against the base commit was real, but it was a
    one-off script run by hand, so nothing in CI held it.
    """
    monkeypatch.setenv("APP_EDITION", "full")
    monkeypatch.setenv("DRAFTING_ENABLED", "true")
    live = sorted({r.path for r in create_app().routes})
    expected = _BASELINE.read_text().split()
    added = sorted(set(live) - set(expected))
    removed = sorted(set(expected) - set(live))
    assert (added, removed) == ([], []), (
        f"the FULL edition's route surface moved: +{added} -{removed}\n"
        "Release 1 is an addition, not a replacement. If the change is intended, "
        f"regenerate {_BASELINE.name} in the same commit."
    )


def test_the_baseline_is_not_empty():
    """A baseline of zero routes would make the comparison above vacuous on an
    app that also served nothing."""
    assert len(_BASELINE.read_text().split()) == 120


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


# --- what release 1 SERVES, not only what it drops -----------------------------
# Every test above constrains the DIFFERENCE between the editions: match must not
# have the excluded prefixes, and match's drops must all be excluded. None of them
# asks whether a route the match edition KEEPS belongs in release 1 — so a new
# proposal-assist router added to _ALWAYS ships in both editions and every gate
# stays green. Measured 2026-09-04: an authenticated `/api/proposals/{id}/draft`
# router placed in _ALWAYS reached the match app and all 63 architecture tests
# passed. That is defect #27's shape recurring (six admin proposal-assist
# endpoints were in _ALWAYS at the time), and #27 was found by reading, not by a
# gate.
#
# So _ALWAYS is pinned by NAME. Adding a router to it is now a deliberate edit in
# two places, which is the point: the second place is where somebody asks whether
# release 1 should serve it.
_ALWAYS_ROUTERS = [
    "auth.router",
    "me.router",
    "admin.router",
    "admin_users.router",
    "calls.router",
    "profiles.router",
    # Boards are a MATCH-edition feature: the corridor screen is release 1's
    # public face.
    "boards.router",
    # The public corridor-board JSON (Task 7): unauthenticated by design, so it
    # ships in match too -- it IS release 1's public face.
    "public_boards.router",
    # The golden set, moved back here 2026-09-07 (decision notes Task 5). The
    # product question this list exists to force: release 1 IS the only edition
    # that ships, and the golden set is the ranking system's own evaluation
    # surface -- the page where a human curates the triples the weekly tau is
    # measured against. Leaving it full-only meant the human reasons Task 4 put
    # on the candidate rows had no reader at all.
    "admin_golden_set.router",
]


def _tuple_elements(name: str) -> list[str]:
    """Source text of each element of a module-level tuple in web/api/__init__.py."""
    import ast

    src = (Path(__file__).resolve().parents[2] / "src/grant_lab/web/api/__init__.py").read_text()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            assert isinstance(node.value, ast.Tuple), f"{name} is not a tuple literal"
            return [ast.unparse(e) for e in node.value.elts]

    # _FULL_ONLY is no longer a module-level tuple: the 2026-09-05 process cut moved
    # it into _full_only_routers() so a match process does not import the full
    # edition's routers at all (tests/architecture/test_match_edition_process_cut.py).
    # The reviewed-set guarantee is unchanged — read the tuple it RETURNS.
    if name == "_FULL_ONLY":
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "_full_only_routers":
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Tuple):
                        return [ast.unparse(e) for e in sub.value.elts]
                raise AssertionError("_full_only_routers() does not return a tuple literal")
    raise AssertionError(f"no module-level {name} and no _full_only_routers() returning it")


def test_the_always_mounted_routers_are_exactly_the_reviewed_set():
    assert _tuple_elements("_ALWAYS") == _ALWAYS_ROUTERS, (
        "the routers mounted in BOTH editions changed. Every entry here is served "
        "to release-1 users, so adding one is a product decision: does the match "
        "edition need it? If yes, add it to _ALWAYS_ROUTERS with that reasoning."
    )


def test_the_full_only_routers_are_exactly_the_reviewed_set():
    """The other direction. Without this, moving a router OUT of _FULL_ONLY into
    _ALWAYS would only trip the test above — and moving one out of _FULL_ONLY
    entirely (dropping it from both editions) would trip nothing at all."""
    assert _tuple_elements("_FULL_ONLY") == [
        "annotations_router.router",
        "workspaces.router",
        "turns.router",
        "references.router",
        "files.router",
        "rubric.router",
        "export.router",
        "citation_audit.router",
        "settings.router",
        # A sub-router carved out of admin.py by this branch: six proposal-assist
        # admin endpoints were in _ALWAYS and therefore served in match (defect
        # #27). All require_admin-gated, so not an authorization hole — but the
        # branch's headline claim is that the surface is ABSENT.
        "admin.drafting_router",
        # admin_golden_set.router is deliberately NOT here: it went back to
        # _ALWAYS on 2026-09-07 and is pinned in _ALWAYS_ROUTERS instead.
        "admin_retention.router",
    ]
