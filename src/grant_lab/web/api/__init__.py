from __future__ import annotations

from typing import Literal

from fastapi import APIRouter

# ONLY the six both-edition routers are imported eagerly. The full-only ones are
# imported inside _full_only_routers() because this is a PACKAGE __init__: any
# `import grant_lab.web.api.<anything>` runs it, so an eager import here pulled the
# whole proposal-assist stack — pycrdt, prosemirror, claude_agent_sdk — into a match
# process that never serves those routes. Pinned by
# tests/architecture/test_match_edition_process_cut.py, which boots each edition in
# a FRESH interpreter and reads sys.modules.
from grant_lab.web.api import (
    admin,
    admin_users,
    auth,
    boards,
    calls,
    me,
    profiles,
    public_boards,
)
from grant_lab.web.api.errors import STATUS_CODES, ApiError

# Document the error-envelope shape on every status the /api surface actually
# emits, declared on the router so it applies to all ops. Without this, each op
# advertised FastAPI's stock HTTPValidationError ({detail: [...]}) for its 422,
# but the runtime returns {error: {code, message, detail?}} — which broke
# Plan 3's OpenAPI -> TS codegen. Schema-only: the runtime still emits the
# envelope via grant_lab.web.api.errors.envelope_dict. The status set is
# DERIVED from STATUS_CODES (single source of truth) so the documented responses
# can't drift from the codes the handlers actually map.

# Mounted in BOTH editions. Call Match release 1 is these plus the non-/api
# health and OAuth routers.
_ALWAYS = (
    auth.router,
    me.router,
    admin.router,
    admin_users.router,
    calls.router,
    profiles.router,
    # Boards are a MATCH-edition feature: the corridor screen is release 1's
    # public face. Eager import is safe ONLY while neither module imports
    # anything under grant_lab.proposal_assist -- this file is a package
    # __init__, so an import here runs in every process.
    boards.router,
    public_boards.router,
)


# Mounted ONLY in the full edition. In a match build these routes are never
# registered, so a request for one gets FastAPI's real 404 — there is no handler
# to reach. Pinned by tests/architecture/test_edition_surface.py.
def _full_only_routers() -> tuple:
    """Import and return the full-edition routers. Called only when edition != match.

    Deferred deliberately: importing these at module scope is what made
    APP_EDITION=match a route cut rather than a process cut.
    """
    import grant_lab.web.api.annotations as annotations_router
    from grant_lab.web.api import (
        admin_golden_set,
        admin_retention,
        citation_audit,
        export,
        files,
        references,
        rubric,
        settings,
        turns,
        workspaces,
    )

    return (
        annotations_router.router,
        workspaces.router,
        turns.router,
        references.router,
        files.router,
        rubric.router,
        export.router,
        citation_audit.router,
        # Despite the module name, every route in web/api/settings.py is
        # workspace-scoped (/workspaces/{id}/settings and /workspaces/{id}/members*)
        # — it is the proposal-assist workspace settings router, not app settings.
        # Leaving it in _ALWAYS leaked three /api/workspaces paths into a match build.
        settings.router,
        # The three ADMIN routers behind proposal-assist-only pages. They were in
        # _ALWAYS, which left six require_admin-gated proposal-assist endpoints live
        # in a deployment that does not serve proposal assist — including the
        # MUTATING /api/admin/workspaces/deleted/{id}/restore. Not an authorization
        # hole (every one is admin-gated), but the branch's claim is that the surface
        # is ABSENT. The pages they back — AdminDrafting.tsx, AdminGoldenSet.tsx,
        # AdminDeletedWorkspaces.tsx, AdminDeletedWorkspaceDetail.tsx — are the only
        # frontend consumers and router.simple.tsx mounts none of them. The
        # AdminDashboard golden-set panel reads `golden_set_report` off
        # /api/admin/dashboard, a different endpoint that stays in _ALWAYS.
        admin.drafting_router,
        admin_golden_set.router,
        admin_retention.router,
    )


def build_api_router(*, edition: Literal["match", "full"] = "full") -> APIRouter:
    """Compose the /api router for one edition. `edition` is Settings.app_edition."""
    r = APIRouter(
        prefix="/api",
        responses={code: {"model": ApiError} for code in STATUS_CODES},
    )
    for sub in _ALWAYS:
        r.include_router(sub)
    if edition != "match":
        for sub in _full_only_routers():
            r.include_router(sub)
    return r
