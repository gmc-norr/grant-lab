# Call Match Release 1 — Plan A: the edition split and the simple shell

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a `match` build edition of the existing app — a plain shell over
today's calls / profiles / account / admin-users pages — with the proposal-assist
half provably absent from both the JS bundle and the API surface.

**Architecture:** One repo, two build editions. The frontend picks `router.tsx`
(full) or `router.simple.tsx` (match) from a Vite env flag at build time; the
backend composes its API router conditionally on a new `APP_EDITION` setting.
Neither edition is a runtime feature flag — a `match` build cannot serve a
proposal-assist endpoint or ship a cockpit chunk, and four gates prove it.

**Tech Stack:** Python 3.12 / FastAPI / pydantic-settings; React 19 + Vite +
react-router-dom + Mantine; pytest (+ testcontainers Postgres); vitest + jsdom.

## Global Constraints

Copied verbatim from the spec and from `CLAUDE.md`. Every task's requirements
implicitly include this section.

- **Test command:** `uv run pytest -n 16 --dist loadfile` (~90 s). **Never
  `-n auto`** — `psutil` is absent, so xdist resolves it to 72 workers.
- **Expected suite result is `3361 passed, 1 skipped`.** The **`1 skipped`** is
  the signal, not the total — it is a real-SDK test gated on `ANTHROPIC_API_KEY`.
  `0 skipped` means a live API call just ran.
- **Frontend tests:** `pnpm -C frontend test run`. **Lint:** `pnpm -C frontend lint`
  (oxlint — there is no eslint config).
- **Never run `ruff format` on a directory.** Format by filename only. Count
  `git status` lines before and after; the delta must be 0 for untouched files.
- **`docs/ARCHITECTURE.md` is generated.** Regenerate in the same commit that
  moves code: `uv run python scripts/gen_architecture.py`. Never hand-edit.
- **`.env.example` is generated** from `Settings` by
  `uv run python scripts/gen_env_example.py`, and pinned by
  `tests/test_env_example.py`. Adding a `Settings` field **requires** regenerating.
- **`frontend/src/routes.gen.ts` and `src/call_manager/web/spa_routes.py` are
  generated** from `frontend/routes.json` by `uv run python scripts/gen_routes.py`.
  `scripts/gen_routes.py --check` fails when they are stale.
- **Commit titles name the change, not the activity.** `feat(edition): gate the
  API router on APP_EDITION` is findable; `Refactor and enhance codebase` is not.
- Every gate in this plan must be **mutation-tested**: break the thing the gate
  protects and confirm the gate goes red. A gate that has never failed is a
  claim, not a test.

## Scope: this is Plan A of four

The spec (`docs/superpowers/specs/2026-09-03-call-match-release-1-design.md`)
covers four independently shippable subsystems. This plan is the first. The
others are **not** in scope here and must not be started inside it:

| Plan | Spec sections | Ships |
| --- | --- | --- |
| **A (this one)** | §5, §9 gates 1/2/4/5, §11 steps 1-2 | The `match` edition: today's pages in a plain shell, cockpit provably gone |
| B — Triage | §4.1, §4.2, §5 card, §11 steps 3-4 | `call_decisions`, save/hide, inbox, match reasons on the list |
| C — Boards | §4.3, §6, §9 gate 3, §11 steps 5-6 | `boards`, the approval queue, the public endpoint, `/b/:slug` |
| D — Extras | §7, §11 steps 7-8 | Share a call by email; change detection and notification |

Plan A ships working software on its own: a deployable release-1 build serving
the calls dashboard, profiles, account settings and admin-users.

## File structure

**Backend**

| File | Responsibility |
| --- | --- |
| `src/call_manager/config.py` | Add the `app_edition` field (modify) |
| `src/call_manager/web/api/__init__.py` | Compose `api_router` conditionally (modify) |
| `src/call_manager/main.py:887-900` | Skip the proposal-assist routers in `match` (modify) |
| `tests/architecture/test_edition_surface.py` | Gate: the `match` API surface (create) |
| `tests/architecture/test_auth_allowlist.py` | Gate 5: the public-route allowlist (create) |

**Frontend**

| File | Responsibility |
| --- | --- |
| `frontend/src/shell/SimpleShell.tsx` | The release-1 shell: top bar + content (create) |
| `frontend/src/shell/SimpleShell.module.css` | Its layout (create) |
| `frontend/src/shell/SimpleShell.test.tsx` | Its behaviour (create) |
| `frontend/src/router.simple.tsx` | The release-1 router (create) |
| `frontend/src/router.simple.test.tsx` | Gate 2: exact route allowlist (create) |
| `frontend/src/edition.ts` | Reads `VITE_APP_EDITION`, one place (create) |
| `frontend/src/App.tsx` | Import the router through the alias (modify) |
| `frontend/src/virtual-router.d.ts` | Type the `virtual:app-router` alias (create) |
| `frontend/vite.config.ts` | Resolve the alias per edition at config time (modify) |
| `frontend/src/routes/CallRow.tsx:52-56` | Remove the "Start drafting" link (modify) |
| `frontend/src/routes/CallDetailBody.tsx:46-50` | Remove the "Start drafting workspace" button (modify) |
| `frontend/src/noCockpitLinks.test.tsx` | Gate 4: no links into the excluded half (create) |
| `scripts/check_match_bundle.mjs` | Gate 1: content-based bundle check (create) |
| `frontend/src/theme/theme.match.css` | The release-1 off-white palette (create) |
| `frontend/src/theme/contrast.test.ts` | Pins the palette's contrast ratios (create) |

---

### Task 1: `APP_EDITION` setting and a conditional API surface

The backend half of the edition split. After this task a `match` process cannot
serve a proposal-assist endpoint, because the routes are never registered.

**Files:**
- Modify: `src/call_manager/config.py` (after the `drafting_enabled` field, ~line 175)
- Modify: `src/call_manager/web/api/__init__.py`
- Modify: `src/call_manager/main.py:887-900`
- Test: `tests/architecture/test_edition_surface.py` (create)

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `Settings.app_edition: Literal["match", "full"]` (default `"full"`),
  env alias `APP_EDITION`. Task 5 mirrors the same two values in the frontend
  flag `VITE_APP_EDITION`. Keep the strings identical — `match` and `full`.

- [ ] **Step 1: Write the failing test**

Create `tests/architecture/test_edition_surface.py`:

```python
"""APP_EDITION=match must not register a proposal-assist route.

The edition is a BUILD/DEPLOY decision, not a runtime feature flag: a match
deployment does not mount the routers at all, so there is no endpoint to reach
with a crafted request. This test is the only thing that makes that claim true.
"""

from __future__ import annotations

import pytest

from call_manager.main import create_app

# Every path prefix the proposal-assist half owns. A match build must register
# NONE of these. Derived from main.py's router list and api/__init__.py.
EXCLUDED_PREFIXES = (
    "/workspaces",
    "/api/workspaces",
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
    }.items():
        monkeypatch.setenv(k, v)


def _paths(app) -> set[str]:
    return {r.path for r in app.routes if hasattr(r, "methods")}


def test_match_edition_registers_no_proposal_assist_route(monkeypatch, _base_env):
    monkeypatch.setenv("APP_EDITION", "match")
    paths = _paths(create_app())
    leaked = sorted(p for p in paths if p.startswith(EXCLUDED_PREFIXES))
    assert leaked == [], f"match edition leaked proposal-assist routes: {leaked}"


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
    assert any(p.startswith("/api/turns") for p in paths)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/architecture/test_edition_surface.py -v
```

Expected: `test_match_edition_registers_no_proposal_assist_route` FAILS with a
long `leaked` list (the routers mount unconditionally today). The other two PASS.

- [ ] **Step 3: Add the setting**

In `src/call_manager/config.py`, immediately after the `drafting_enabled` field
(it ends at the closing `)` around line 175), add:

```python
    app_edition: Literal["match", "full"] = Field(
        default="full",
        alias="APP_EDITION",
        description=(
            "Which product edition this process serves. 'match' is Call Match "
            "release 1: funding-call matching only. It does NOT mount the "
            "proposal-assist routers (workspaces, turns, annotations, rubric, "
            "references, citation_audit, export, files) or the collab websocket, "
            "so those endpoints do not exist rather than returning 403. 'full' is "
            "the complete application. This is a deploy-time decision, not a "
            "runtime toggle; the frontend has a matching build flag "
            "(VITE_APP_EDITION) and the two must agree."
        ),
    )
```

`Literal` is already imported in `config.py` (`spa_mode` uses it) — do not add
a second import.

- [ ] **Step 4: Make the API router composition conditional**

Replace the body of `src/call_manager/web/api/__init__.py` below the imports.
The module currently builds `router` at import time; it must become a function
so the edition can be read. Keep the module-level `router` for the full edition
so existing imports keep working.

```python
from __future__ import annotations

from fastapi import APIRouter

import call_manager.web.api.annotations as annotations_router
from call_manager.web.api import (
    admin,
    admin_golden_set,
    admin_retention,
    admin_users,
    auth,
    calls,
    citation_audit,
    export,
    files,
    me,
    profiles,
    references,
    rubric,
    settings,
    turns,
    workspaces,
)
from call_manager.web.api.errors import STATUS_CODES, ApiError

# Document the error-envelope shape on every status the /api surface actually
# emits, declared on the router so it applies to all ops. Without this, each op
# advertised FastAPI's stock HTTPValidationError ({detail: [...]}) for its 422,
# but the runtime returns {error: {code, message, detail?}} — which broke
# Plan 3's OpenAPI -> TS codegen. Schema-only: the runtime still emits the
# envelope via call_manager.web.api.errors.envelope_dict. The status set is
# DERIVED from STATUS_CODES (single source of truth) so the documented responses
# can't drift from the codes the handlers actually map.

# Mounted in BOTH editions. Call Match release 1 is these plus the non-/api
# health and OAuth routers.
_ALWAYS = (
    auth.router,
    me.router,
    admin.router,
    admin_users.router,
    admin_golden_set.router,
    admin_retention.router,
    calls.router,
    profiles.router,
    settings.router,
)

# Mounted ONLY in the full edition. In a match build these routes are never
# registered, so a request for one gets FastAPI's real 404 — there is no handler
# to reach. Pinned by tests/architecture/test_edition_surface.py.
_FULL_ONLY = (
    annotations_router.router,
    workspaces.router,
    turns.router,
    references.router,
    files.router,
    rubric.router,
    export.router,
    citation_audit.router,
)


def build_api_router(*, edition: str = "full") -> APIRouter:
    """Compose the /api router for one edition. `edition` is Settings.app_edition."""
    r = APIRouter(
        prefix="/api",
        responses={code: {"model": ApiError} for code in STATUS_CODES},
    )
    for sub in _ALWAYS:
        r.include_router(sub)
    if edition != "match":
        for sub in _FULL_ONLY:
            r.include_router(sub)
    return r


# Module-level full-edition router. Kept so existing `from ... import router`
# call sites (and the OpenAPI codegen) keep working unchanged.
router = build_api_router(edition="full")
```

Check the tail of the original file first — if `router.include_router(...)` lines
exist past line 54 for sub-routers not listed above, add them to `_ALWAYS` or
`_FULL_ONLY`. Do not silently drop one.

- [ ] **Step 5: Gate the proposal-assist routers in the app factory**

In `src/call_manager/main.py`, replace lines 887-900:

```python
    app.include_router(health.router)
    app.include_router(oauth_google.router)
    # Proposal assist + collab. Skipped entirely in the match edition (Call Match
    # release 1) so the endpoints do not exist rather than answering 403 —
    # pinned by tests/architecture/test_edition_surface.py.
    if settings.app_edition != "match":
        app.include_router(proposals_router)
        app.include_router(coherence_router)
        app.include_router(evaluator_router)
        app.include_router(review_router)
        app.include_router(versions_router)
        app.include_router(branches_router)
        app.include_router(problems_router)
        app.include_router(uncited_claims_router)
        app.include_router(activity_router)
        app.include_router(collab_ws.router)
        app.include_router(collab_checkpoint.router)
    app.include_router(build_api_router(edition=settings.app_edition))
```

Add the import at the top of `main.py`, beside the existing
`from call_manager.web.api import router as api_router` line (replace it if the
name becomes unused; leave it if other code references it):

```python
from call_manager.web.api import build_api_router
```

- [ ] **Step 6: Run the test to verify it passes**

```bash
uv run pytest tests/architecture/test_edition_surface.py -v
```

Expected: 3 passed.

- [ ] **Step 7: Regenerate the pinned artefacts**

```bash
uv run python scripts/gen_env_example.py
uv run python scripts/gen_architecture.py
```

- [ ] **Step 8: Run the full suite**

```bash
uv run pytest -n 16 --dist loadfile
```

Expected: `3364 passed, 1 skipped` (3361 + the 3 new). If the count differs by
anything other than +3, stop and read the failures — a conditional router is
exactly the change that breaks an unrelated test's assumption about route count.

- [ ] **Step 9: Mutation-test the gate**

Temporarily change `if edition != "match":` to `if True:` in
`web/api/__init__.py`, then:

```bash
uv run pytest tests/architecture/test_edition_surface.py -v
```

Expected: `test_match_edition_registers_no_proposal_assist_route` FAILS naming the
leaked paths. Restore the line, then clear stale bytecode — a same-size restore
inside one second can leave pytest running the mutant:

```bash
find src -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; true
uv run pytest tests/architecture/test_edition_surface.py -v
```

Expected: 3 passed.

- [ ] **Step 10: Commit**

```bash
git add src/call_manager/config.py src/call_manager/web/api/__init__.py \
        src/call_manager/main.py tests/architecture/test_edition_surface.py \
        .env.example docs/ARCHITECTURE.md
git commit -m "feat(edition): gate the proposal-assist API surface on APP_EDITION

A match deployment does not mount the workspaces/turns/annotations/rubric/
references/citation_audit/export/files routers or the collab websocket, so those
endpoints do not exist rather than answering 403. api/__init__.py grows a
build_api_router(edition=...) function; the module-level \`router\` stays as the
full-edition composition so existing imports and the OpenAPI codegen are
unchanged."
```

---

### Task 2: Gate 5 — every route outside a pinned allowlist requires auth

Spec §9 gate 5. Auth here is enforced **per route**, not by middleware, so an
endpoint becomes public by omitting a line — silent in both directions. This gate
is the only thing that would catch an accidental omission. Plan C's public board
endpoint will add exactly one line to the allowlist, and that line will be a
visible diff.

**Files:**
- Test: `tests/architecture/test_auth_allowlist.py` (create)

**Interfaces:**
- Consumes: `Settings.app_edition` from Task 1 (the gate runs against both editions).
- Produces: `PUBLIC_ROUTES`, the reviewed allowlist. Plan C appends
  `("GET", "/api/public/boards/{slug}")` to it.

**Two facts this gate must encode, both measured on 2026-09-03, not guessed:**

1. `require_admin` (`auth/dependencies.py:49`) calls `get_current_user` **in its
   function body**, not as a FastAPI sub-dependency — so FastAPI's dependant tree
   never sees `get_current_user` on an admin route. A gate that looked only for
   `get_current_user` would flag all 21 `/api/admin/*` routes. The auth set is
   `{get_current_user, require_admin}`.
2. With that set, exactly **8** routes are unauthenticated, and all 8 are
   legitimately public. That measured 8 is the allowlist below.

- [ ] **Step 1: Write the failing test**

Create `tests/architecture/test_auth_allowlist.py`:

```python
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
```

- [ ] **Step 2: Run the test**

```bash
uv run pytest tests/architecture/test_auth_allowlist.py -v
```

Expected: 3 passed. **This gate starts green** — it is a regression gate, not a
red-to-green task. If it starts red, a route lost its auth dependency at some
point and you have found a live bug. Stop and report it before changing the
allowlist.

- [ ] **Step 3: Mutation-test the gate — this is the whole value of the task**

Temporarily remove the auth dependency from one route. In
`src/call_manager/web/api/calls.py:384`, delete this line from `get_calls`:

```python
    user: User = Depends(get_current_user),  # noqa: B008
```

That will break the function body (it uses `user`), so also change the
`build_dashboard_view(... user_id=user.id ...)` call in that function to
`user_id=1` just for the mutation.

```bash
uv run pytest tests/architecture/test_auth_allowlist.py -v
```

Expected: `test_no_unlisted_route_is_public` FAILS for **both** editions, naming
`('GET', '/api/calls')`.

Restore both edits, clear bytecode, re-run:

```bash
git checkout src/call_manager/web/api/calls.py
find src -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; true
uv run pytest tests/architecture/test_auth_allowlist.py -v
```

Expected: 3 passed.

- [ ] **Step 4: Commit**

```bash
git add tests/architecture/test_auth_allowlist.py
git commit -m "test(auth): pin the public-route allowlist against silent exposure

Auth is per-route here, not middleware, so a route becomes public by omitting a
line. This walks every route in both editions and asserts each one outside a
reviewed 8-entry allowlist carries get_current_user or require_admin.

Both dependencies count: require_admin calls get_current_user in its BODY, not
as a sub-dependency, so a gate looking only for get_current_user would flag all
21 /api/admin routes. Mutation-tested by stripping the dependency from
GET /api/calls."
```

---

### Task 3: `SimpleShell`

The release-1 chrome: one top bar, content below. No activity bar, no status bar,
no resizable panes, no bottom panel. Same Mantine theme tokens, so it still looks
like the same product.

**Files:**
- Create: `frontend/src/shell/SimpleShell.tsx`
- Create: `frontend/src/shell/SimpleShell.module.css`
- Test: `frontend/src/shell/SimpleShell.test.tsx`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `export function SimpleShell(): JSX.Element` — a pathless layout
  component rendering `<Outlet />`. Task 4 uses it as the layout route element.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/shell/SimpleShell.test.tsx`:

```tsx
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { SimpleShell } from "./SimpleShell";
import { theme } from "../theme/theme";

function renderAt(path: string) {
  const router = createMemoryRouter(
    [
      {
        element: <SimpleShell />,
        children: [
          { index: true, element: <p>calls page</p> },
          { path: "profiles", element: <p>profiles page</p> },
        ],
      },
    ],
    { initialEntries: [path] },
  );
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={new QueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </MantineProvider>,
  );
}

describe("SimpleShell", () => {
  it("renders the child route inside a main landmark", () => {
    renderAt("/");
    expect(screen.getByText("calls page")).toBeInTheDocument();
    expect(screen.getByRole("main")).toBeInTheDocument();
  });

  it("offers the three release-1 destinations as nav links", () => {
    renderAt("/");
    const nav = screen.getByRole("navigation", { name: /main/i });
    for (const label of ["Calls", "Profiles", "Admin"]) {
      expect(within(nav).getByRole("link", { name: label })).toBeInTheDocument();
    }
  });

  it("marks the active destination with aria-current", () => {
    renderAt("/profiles");
    expect(screen.getByRole("link", { name: "Profiles" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("link", { name: "Calls" })).not.toHaveAttribute("aria-current");
  });

  it("renders none of the cockpit shell furniture", () => {
    renderAt("/");
    // The AppShell landmarks that release 1 deliberately drops.
    expect(screen.queryByLabelText("Primary")).not.toBeInTheDocument(); // ActivityBar
    expect(screen.queryByRole("status")).not.toBeInTheDocument(); // StatusBar
    expect(screen.queryByLabelText(/resize/i)).not.toBeInTheDocument(); // panel handles
  });
});
```

Add `within` to the testing-library import:

```tsx
import { render, screen, within } from "@testing-library/react";
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pnpm -C frontend test run src/shell/SimpleShell.test.tsx
```

Expected: FAIL — `Failed to resolve import "./SimpleShell"`.

- [ ] **Step 3: Write the component**

Create `frontend/src/shell/SimpleShell.module.css`:

```css
/* Release-1 chrome: a top bar and a content column. No activity bar, no status
   bar, no resizable panes. Same theme tokens as AppShell so the two editions
   read as the same product. */
.shell {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
  background: var(--app-editor);
}

.topbar {
  display: flex;
  align-items: center;
  gap: 1.5rem;
  flex-wrap: wrap;
  padding: 0 1rem;
  min-height: 48px;
  background: var(--app-titlebar);
  border-bottom: 1px solid var(--app-border);
}

.brand {
  font-weight: 700;
  font-size: 0.95rem;
  color: var(--app-text);
  text-decoration: none;
}

.nav {
  display: flex;
  gap: 0.25rem;
  margin-right: auto;
}

.link {
  padding: 0.4rem 0.7rem;
  border-radius: 4px;
  color: var(--app-muted-chrome);
  text-decoration: none;
  font-size: 0.9rem;
  /* 44px min touch target on a phone; the spec's responsive strategy keeps the
     same DOM across breakpoints for everything but the board and the swipe
     gesture, so the nav must be thumb-usable as-is. */
  min-height: 44px;
  display: inline-flex;
  align-items: center;
}

.link:hover {
  background: var(--app-hover);
  color: var(--app-text);
}

.link[aria-current="page"] {
  color: var(--app-text);
  box-shadow: inset 0 -2px 0 var(--app-accent-bright);
}

.content {
  flex: 1 1 auto;
  min-width: 0;
}
```

Every token above is verified present in `frontend/src/theme/theme.css`
(lines 9-28): `--app-editor`, `--app-titlebar`, `--app-border`, `--app-text`,
`--app-muted-chrome`, `--app-hover`, `--app-accent-bright`.

Create `frontend/src/shell/SimpleShell.tsx`:

```tsx
import { Menu, UnstyledButton } from "@mantine/core";
import { Link, NavLink, Outlet } from "react-router-dom";

import { useSession } from "../api/session";
import styles from "./SimpleShell.module.css";

// Release-1 shell. AppShell's IDE furniture (activity bar, status bar,
// resizable panes, bottom panel) exists to make the proposal-assist cockpit
// coherent; release 1 has no cockpit, so it carries none of it. Keeping the
// theme tokens means the two editions still look like one product.
const DESTINATIONS = [
  { to: "/", label: "Calls", end: true },
  { to: "/profiles", label: "Profiles", end: false },
  { to: "/admin", label: "Admin", end: false, adminOnly: true },
] as const;

export function SimpleShell() {
  // GET /api/me. `capabilities.admin` is the admin flag the SPA reads —
  // RequireAdmin.tsx:13 uses the same field. `user.is_admin` exists on the wire
  // schema but no component reads it; do not introduce a second source.
  const { data: session } = useSession();
  const isAdmin = Boolean(session?.capabilities.admin);

  return (
    <div className={styles.shell}>
      <header className={styles.topbar}>
        <Link to="/" className={styles.brand}>
          Call Match
        </Link>
        <nav className={styles.nav} aria-label="Main">
          {DESTINATIONS.filter((d) => !d.adminOnly || isAdmin).map((d) => (
            <NavLink
              key={d.to}
              to={d.to}
              end={d.end}
              className={styles.link}
              // NavLink sets aria-current="page" itself when active; the test
              // asserts on that rather than on a class name.
            >
              {d.label}
            </NavLink>
          ))}
        </nav>
        <Menu position="bottom-end" withinPortal>
          <Menu.Target>
            <UnstyledButton className={styles.link} aria-label="Account menu">
              {session?.user.email ?? "Account"}
            </UnstyledButton>
          </Menu.Target>
          <Menu.Dropdown>
            <Menu.Item component={Link} to="/me">
              Settings
            </Menu.Item>
          </Menu.Dropdown>
        </Menu>
      </header>
      <main className={styles.content}>
        <Outlet />
      </main>
    </div>
  );
}
```

`useSession` lives in `frontend/src/api/session.ts` (**not** `api/auth.ts`,
which holds `useAuthConfig`/`useLogin`/`useLogout`). `TitleBar.tsx:36` reads the
email as `useSession().data?.user.email` and `RequireAdmin.tsx:13` reads the
admin flag as `data.capabilities.admin` — both verified 2026-09-03.

The Admin link is filtered on `capabilities.admin` so a non-admin does not see a
destination that 403s. The test renders without a session, so `isAdmin` is
false — **update the nav test to render with a mocked admin session**, or drop
"Admin" from the asserted labels and add a separate test for the admin case.
Pick one and make the test match the component; do not leave them disagreeing.

- [ ] **Step 4: Run the test to verify it passes**

```bash
pnpm -C frontend test run src/shell/SimpleShell.test.tsx
```

Expected: 4 passed.

- [ ] **Step 5: Lint**

```bash
pnpm -C frontend lint
```

Expected: no new findings.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/shell/SimpleShell.tsx \
        frontend/src/shell/SimpleShell.module.css \
        frontend/src/shell/SimpleShell.test.tsx
git commit -m "feat(shell): add SimpleShell, the release-1 chrome

One top bar over a content column. No activity bar, status bar, resizable panes
or bottom panel — that furniture exists to make the cockpit coherent and release
1 has no cockpit. Same theme tokens, so both editions read as one product. Nav
links are 44px tall so the same DOM works under a thumb."
```

---

### Task 4: `router.simple.tsx` and gate 2 — an exact route allowlist

Spec §9 gate 2. The existing drift guard collapses each concrete leaf to the
manifest entry that owns it and asserts set equality against `SPA_ROUTES`. That
is the right shape for the full router and **the wrong shape for this one**:
`frontend/routes.json` line 2 already lists `/cockpit`, so a simple router that
still mounted every cockpit path would remain a perfectly valid subset. Subset
catches an unknown path; it cannot catch an excluded one.

**Files:**
- Create: `frontend/src/router.simple.tsx`
- Test: `frontend/src/router.simple.test.tsx`

**Interfaces:**
- Consumes: `SimpleShell` from Task 3.
- Produces: `export const simpleRouter` — a `createBrowserRouter` result. Task 5
  imports it in `App.tsx`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/router.simple.test.tsx`:

```tsx
import type { RouteObject } from "react-router-dom";
import { expect, it } from "vitest";

import { simpleRouter } from "./router.simple";
import { SPA_ROUTES } from "./routes.gen";

// Same leaf walk as router.test.tsx: a pathless layout contributes nothing
// itself, its children inherit the parent base, an index route resolves to the
// parent base.
function leafPaths(routes: RouteObject[], base = ""): string[] {
  const out: string[] = [];
  for (const r of routes) {
    const here =
      r.path == null
        ? base
        : r.path.startsWith("/")
          ? r.path
          : `${base.replace(/\/$/, "")}/${r.path}`;
    if (r.children?.length) out.push(...leafPaths(r.children, here));
    else out.push(r.index ? base || "/" : here);
  }
  return out;
}

// The release-1 route set, written out. This is an ALLOWLIST, not a subset
// check: routes.gen.ts already contains "/cockpit", so a subset assertion would
// happily pass a router that mounted the whole cockpit. Adding a line here is
// the reviewed act.
const RELEASE_1_ROUTES = [
  "/",
  "/calls/:id",
  "/me",
  "/profiles",
  "/profiles/new",
  "/profiles/:id",
  "/profiles/:id/edit",
  "/admin",
  "/admin/users",
  "/admin/users/pending",
  "/login",
  "/register",
  "/auth/pending",
  "/auth/no-access",
  "/auth/error",
];

// Prefixes the match edition must never mount. Spec §3 "explicitly out of
// release 1".
const EXCLUDED_PREFIXES = [
  "/cockpit",
  "/admin/drafting",
  "/admin/golden-set",
  "/admin/deleted-workspaces",
];

it("the simple router mounts exactly the release-1 route set", () => {
  const concrete = leafPaths(simpleRouter.routes as RouteObject[]).filter(
    (p) => !p.includes("*"),
  );
  expect(new Set(concrete)).toEqual(new Set(RELEASE_1_ROUTES));
});

it("the simple router mounts no excluded path", () => {
  const concrete = leafPaths(simpleRouter.routes as RouteObject[]);
  const leaked = concrete.filter((p) =>
    EXCLUDED_PREFIXES.some((x) => p === x || p.startsWith(x + "/")),
  );
  expect(leaked).toEqual([]);
});

it("every release-1 route is owned by the shared SPA manifest", () => {
  // The server decides which paths get index.html from routes.gen.ts. A
  // release-1 route the manifest does not own would 404 on a hard refresh.
  const owned = (p: string) =>
    SPA_ROUTES.some((r) => (r === "/" ? p === "/" : p === r || p.startsWith(r + "/")));
  const unowned = RELEASE_1_ROUTES.filter((p) => !owned(p));
  expect(unowned).toEqual([]);
});
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pnpm -C frontend test run src/router.simple.test.tsx
```

Expected: FAIL — `Failed to resolve import "./router.simple"`.

- [ ] **Step 3: Write the router**

Create `frontend/src/router.simple.tsx`:

```tsx
import { lazy, Suspense } from "react";
import { Center, Group, Loader, Text, Title } from "@mantine/core";
import { createBrowserRouter } from "react-router-dom";

import { AccountNoAccess } from "./routes/AccountNoAccess";
import { AccountPending } from "./routes/AccountPending";
import { AuthError } from "./routes/AuthError";
import { CallDetail } from "./routes/CallDetail";
import { Dashboard } from "./routes/Dashboard";
import { Login } from "./routes/Login";
import { PreAuthLayout } from "./routes/PreAuthLayout";
import { ProfileDetail } from "./routes/ProfileDetail";
import { ProfileEdit } from "./routes/ProfileEdit";
import { ProfileNew } from "./routes/ProfileNew";
import { ProfilesList } from "./routes/ProfilesList";
import { Register } from "./routes/Register";
import { Settings } from "./routes/Settings";
import { SimpleShell } from "./shell/SimpleShell";

// Call Match release 1. This file's import list IS the exclusion mechanism:
// nothing here reaches a cockpit, workspace, editor or collab module, lazily or
// otherwise, so Vite's tree-shaking has no path to that code and never emits it.
//
// Do NOT lean on lazy() for exclusion. router.tsx is only partly lazy —
// CockpitChat and CockpitSidebar are static imports at router.tsx:5-6, as are
// WorkspaceList, WorkspaceCreate and WorkspacesFilters — and in any case a
// lazy() import still emits a chunk. Absence from the import graph is what makes
// the code absent from the build.
//
// Route paths MUST stay inside the manifest's owned set (routes.gen.ts) or a
// hard refresh 404s. Pinned by router.simple.test.tsx.
const AdminLayout = lazy(() =>
  import("./routes/AdminLayout").then((m) => ({ default: m.AdminLayout })),
);
const AdminDashboard = lazy(() =>
  import("./routes/AdminDashboard").then((m) => ({ default: m.AdminDashboard })),
);
const AdminPendingUsers = lazy(() =>
  import("./routes/AdminPendingUsers").then((m) => ({ default: m.AdminPendingUsers })),
);
const AdminUsers = lazy(() =>
  import("./routes/AdminUsers").then((m) => ({ default: m.AdminUsers })),
);

function RouteSpinner() {
  return (
    <Center component="div" h="100%" p="xl">
      <Group gap="sm">
        <Loader size="sm" />
        <Text c="dimmed">Loading…</Text>
      </Group>
    </Center>
  );
}

export const simpleRouter = createBrowserRouter([
  {
    element: <SimpleShell />,
    children: [
      { index: true, element: <Dashboard /> },
      { path: "calls/:id", element: <CallDetail /> },
      { path: "me", element: <Settings /> },
      { path: "profiles", element: <ProfilesList /> },
      { path: "profiles/new", element: <ProfileNew /> },
      { path: "profiles/:id", element: <ProfileDetail /> },
      { path: "profiles/:id/edit", element: <ProfileEdit /> },
      {
        path: "admin",
        element: (
          <Suspense fallback={<RouteSpinner />}>
            <AdminLayout />
          </Suspense>
        ),
        children: [
          {
            index: true,
            element: (
              <Suspense fallback={<RouteSpinner />}>
                <AdminDashboard />
              </Suspense>
            ),
          },
          {
            path: "users",
            element: (
              <Suspense fallback={<RouteSpinner />}>
                <AdminUsers />
              </Suspense>
            ),
          },
          {
            path: "users/pending",
            element: (
              <Suspense fallback={<RouteSpinner />}>
                <AdminPendingUsers />
              </Suspense>
            ),
          },
        ],
      },
      // In-app soft 404 only. The server returns index.html for manifest routes
      // alone, so a typed unknown URL hits FastAPI's real 404, never this.
      {
        path: "*",
        element: (
          <Center component="div" role="alert" p="xl">
            <Title order={1}>Not found</Title>
          </Center>
        ),
      },
    ],
  },
  {
    // Pre-auth pages: a second pathless layout, sibling to SimpleShell. No
    // useSession (the anti-401-loop invariant). An exact path like "login"
    // out-ranks the splat above, so /login matches HERE.
    element: <PreAuthLayout />,
    children: [
      { path: "login", element: <Login /> },
      { path: "register", element: <Register /> },
      { path: "auth/pending", element: <AccountPending /> },
      { path: "auth/no-access", element: <AccountNoAccess /> },
      { path: "auth/error", element: <AuthError /> },
    ],
  },
]);
```

`AdminLayout` may render links to `/admin/drafting`, `/admin/golden-set` and
`/admin/deleted-workspaces` — routes this router does not mount. Task 7's gate
catches that; if it fires here, gate the links in `AdminLayout` on the edition
helper from Task 5 rather than deleting them (the full edition still needs them).

`Dashboard` currently declares `handle: { Sidebar: CallsFilters, RightPane:
CallReadingPane }` in `router.tsx`. `SimpleShell` reads no handles, so the
handles are dropped here — which also keeps `CallsFilters` and `CallReadingPane`
out of the import graph. Filtering returns as part of Plan B's triage screen.

- [ ] **Step 4: Run the test to verify it passes**

```bash
pnpm -C frontend test run src/router.simple.test.tsx
```

Expected: 3 passed. If test 1 fails on a set mismatch, read the diff: either the
router mounts something `RELEASE_1_ROUTES` does not list (decide whether it
belongs, then add it to the list *deliberately*) or the reverse.

- [ ] **Step 5: Mutation-test the gate**

Add a cockpit route to `router.simple.tsx`, inside the `SimpleShell` children:

```tsx
      { path: "cockpit", element: <Title order={1}>leak</Title> },
```

```bash
pnpm -C frontend test run src/router.simple.test.tsx
```

Expected: **both** test 1 (set mismatch) and test 2 (`leaked` non-empty) FAIL.
Test 3 must still pass — proving the point that manifest ownership alone would
have let this through. Remove the line and re-run; expected 3 passed.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/router.simple.tsx frontend/src/router.simple.test.tsx
git commit -m "feat(edition): add router.simple.tsx, the release-1 route set

Its import list IS the exclusion mechanism: nothing here reaches a cockpit,
workspace, editor or collab module, so tree-shaking has no path to that code.
Exclusion does not lean on lazy() — router.tsx is only partly lazy, and a lazy
import still emits a chunk.

The drift guard is an exact allowlist, not a subset check. routes.gen.ts already
contains /cockpit, so a subset assertion would pass a router that mounted the
whole cockpit; the third test pins exactly that gap open."
```

---

### Task 5: The edition flag and the router switch

**Files:**
- Create: `frontend/src/edition.ts`
- Create: `frontend/src/edition.test.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/.env.example` (create if absent)

**Interfaces:**
- Consumes: `simpleRouter` (Task 4), `router` (existing).
- Produces: `export const APP_EDITION: "match" | "full"` and
  `export const isMatchEdition: boolean`. Task 7 may import `isMatchEdition` to
  gate an admin link.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/edition.test.ts`:

```ts
import { expect, it } from "vitest";

import { resolveEdition } from "./edition";

it("defaults to full when the flag is unset", () => {
  expect(resolveEdition(undefined)).toBe("full");
  expect(resolveEdition("")).toBe("full");
});

it("returns match only for the exact string", () => {
  expect(resolveEdition("match")).toBe("match");
});

it("falls back to full for anything unrecognised", () => {
  // A typo must not silently ship the cockpit-excluded build to everyone, and
  // must not silently ship the cockpit into a match deployment either. Full is
  // the safe default: it is the superset, so nothing 404s.
  expect(resolveEdition("Match")).toBe("full");
  expect(resolveEdition("simple")).toBe("full");
  expect(resolveEdition("true")).toBe("full");
});
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pnpm -C frontend test run src/edition.test.ts
```

Expected: FAIL — `Failed to resolve import "./edition"`.

- [ ] **Step 3: Write the module**

Create `frontend/src/edition.ts`:

```ts
// Which product edition this BUILD is. Set at build time via VITE_APP_EDITION
// and read exactly once, here. The backend has a matching APP_EDITION setting
// (src/call_manager/config.py) and the two must agree in a deployment — a match
// frontend against a full backend merely hides features, but a full frontend
// against a match backend renders links to endpoints that do not exist.
export type Edition = "match" | "full";

// Exported for the unit test: import.meta.env is not injectable at runtime.
export function resolveEdition(raw: string | undefined): Edition {
  // Exact match only. A typo falls back to `full`, the superset, so nothing
  // 404s — the failure mode of guessing is worse than the failure mode of
  // ignoring.
  return raw === "match" ? "match" : "full";
}

export const APP_EDITION: Edition = resolveEdition(
  import.meta.env.VITE_APP_EDITION as string | undefined,
);

export const isMatchEdition = APP_EDITION === "match";
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
pnpm -C frontend test run src/edition.test.ts
```

Expected: 3 passed.

- [ ] **Step 5: Switch the router with a Vite alias**

**A ternary over two static imports does not work, and the reason matters.**
`import.meta.env.VITE_APP_EDITION` is inlined as a literal, so Rollup can fold
`"match" === "match" ? simpleRouter : fullRouter` down to one branch — but
folding the *reference* does not remove the *module*. `router.tsx` calls
`createBrowserRouter([...])` at module scope, which is a side effect Rollup must
preserve, so the whole cockpit import graph stays in the bundle and Task 6's gate
can never pass.

Resolve the router at **config** time instead, so only one file ever enters the
graph.

In `frontend/vite.config.ts`, above `export default defineConfig({`:

```ts
// Which router the build compiles against. Resolved here, at config time, so
// exactly ONE router file enters the module graph — the excluded one is never
// read, let alone bundled. Doing this with a runtime ternary would not work:
// both routers construct their router at module scope, a side effect Rollup
// must preserve, so both graphs would ship. Pinned by scripts/check_match_bundle.mjs.
const APP_EDITION = process.env.VITE_APP_EDITION === "match" ? "match" : "full";
const ROUTER_FILE = APP_EDITION === "match" ? "src/router.simple.tsx" : "src/router.tsx";
```

and inside the config object, beside `plugins`:

```ts
  resolve: {
    alias: {
      "virtual:app-router": resolve(__dirname, ROUTER_FILE),
    },
  },
```

`resolve` is already imported from `node:path` at the top of the file.

Give both routers the same export name so the alias is transparent. Append to
`frontend/src/router.simple.tsx`:

```tsx
// The `virtual:app-router` alias imports `router`; router.tsx already exports
// that name. `simpleRouter` stays exported for the direct import in
// router.simple.test.tsx, which must name the file it is pinning.
export { simpleRouter as router };
```

Declare the module for TypeScript. Create `frontend/src/virtual-router.d.ts`:

```ts
// `virtual:app-router` is a build-time alias (vite.config.ts) that resolves to
// router.tsx or router.simple.tsx depending on VITE_APP_EDITION.
declare module "virtual:app-router" {
  import type { createBrowserRouter } from "react-router-dom";
  export const router: ReturnType<typeof createBrowserRouter>;
}
```

Then in `frontend/src/App.tsx`, replace the router import:

```tsx
import { router } from "virtual:app-router";
```

and delete `import { router } from "./router";`. The `<RouterProvider router={router} />`
line does not change.

Vitest reads the same config, so unit tests resolve the alias too — and with
`VITE_APP_EDITION` unset they get the full router, which is what
`router.test.tsx` already expects.

- [ ] **Step 6: Document the flag**

Create or append to `frontend/.env.example`:

```
# Which product edition to build. `match` = Call Match release 1 (funding-call
# matching only; no proposal-assist cockpit in the bundle). `full` = the complete
# application. Anything unrecognised falls back to `full`.
# MUST agree with the backend's APP_EDITION setting in the same deployment.
VITE_APP_EDITION=full
```

- [ ] **Step 7: Both builds must succeed**

```bash
pnpm -C frontend build
VITE_APP_EDITION=match pnpm -C frontend build
```

Expected: both exit 0.

- [ ] **Step 8: Run the whole frontend suite**

```bash
pnpm -C frontend test run
pnpm -C frontend lint
```

Expected: all pass. `App.test.tsx`, if one exists, may need the `Suspense`
boundary accounted for — an async router host renders `null` on first paint.

- [ ] **Step 9: Commit**

```bash
git add frontend/src/edition.ts frontend/src/edition.test.ts \
        frontend/src/virtual-router.d.ts frontend/src/App.tsx \
        frontend/src/router.simple.tsx frontend/vite.config.ts \
        frontend/.env.example
git commit -m "feat(edition): resolve the router per edition via a build-time alias

vite.config.ts points \`virtual:app-router\` at router.tsx or router.simple.tsx
from VITE_APP_EDITION, so exactly one router file enters the module graph.

A runtime ternary would NOT have worked: folding the reference does not remove
the module, and both routers call createBrowserRouter at module scope — a side
effect Rollup must preserve — so both import graphs would have shipped and the
bundle gate could never pass.

Unrecognised values fall back to \`full\`, the superset: the failure mode of
guessing is worse than the failure mode of ignoring."
```

---

### Task 6: Gate 1 — the bundle contains no excluded code

Spec §9 gate 1, corrected. Assert on **emitted content**, not on chunk
filenames: Vite is free to fold an excluded module into a differently-named
chunk, so a gate that greps for `cockpit-*.js` would pass with the code shipped.

**Files:**
- Create: `scripts/check_match_bundle.mjs`
- Modify: `frontend/package.json` (add the script entry)

**Interfaces:**
- Consumes: a completed `VITE_APP_EDITION=match` build in `frontend/dist/`.
- Produces: `pnpm -C frontend check:bundle` — exit 0 clean, exit 1 with the
  offending sentinel and file.

- [ ] **Step 1: Write the checker**

Create `scripts/check_match_bundle.mjs`:

```js
#!/usr/bin/env node
/**
 * Gate 1: prove the match build contains no proposal-assist code.
 *
 * Assert on EMITTED CONTENT, not on chunk filenames. Vite may fold an excluded
 * module into a differently-named chunk at any time — `manualChunks` heuristics,
 * a dependency change, a version bump — so a gate that looked for `cockpit-*.js`
 * would go green while the code shipped. Distinctive string literals from the
 * excluded modules survive minification and are what we search for.
 *
 * Usage:
 *   VITE_APP_EDITION=match pnpm -C frontend build
 *   node scripts/check_match_bundle.mjs frontend/dist
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

// Each sentinel is a string literal that appears in an excluded module and
// nowhere in the release-1 surface. Verified present in a `full` build and
// absent from a `match` build by the self-check below — if a sentinel ever stops
// appearing in the full build it has been renamed, and the gate has silently
// stopped testing anything.
const SENTINELS = [
  "proposal-spec.md", // cockpit spec panel
  "coherence-keeper", // cockpit analysis
  "uncited-claims", // cockpit problems panel
  "Start drafting workspace", // the CallDetail button release 1 removes
  "y-protocols", // the collab CRDT stack
];

const dist = resolve(process.argv[2] ?? "frontend/dist");

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (/\.(js|mjs|css)$/.test(name)) out.push(p);
  }
  return out;
}

const files = walk(dist);
if (files.length === 0) {
  console.error(`ERROR: no JS/CSS emitted under ${dist} — did the build run?`);
  process.exit(1);
}

const hits = [];
for (const f of files) {
  const text = readFileSync(f, "utf8");
  for (const s of SENTINELS) {
    if (text.includes(s)) hits.push({ file: f.slice(dist.length + 1), sentinel: s });
  }
}

if (hits.length > 0) {
  console.error("ERROR: the match bundle contains excluded proposal-assist code:");
  for (const h of hits) console.error(`  ${h.file}  contains  ${JSON.stringify(h.sentinel)}`);
  console.error(
    "\nThe fix is an import, not a chunk name: something reachable from " +
      "router.simple.tsx imports a cockpit/collab module.",
  );
  process.exit(1);
}

console.log(`match bundle clean: ${files.length} files, none of ${SENTINELS.length} sentinels`);
```

- [ ] **Step 2: Verify the sentinels are real — run against a FULL build first**

A sentinel that appears in neither build tests nothing. Prove each one is
present in `full` before trusting its absence in `match`:

```bash
pnpm -C frontend build
node scripts/check_match_bundle.mjs frontend/dist
```

Expected: **exit 1**, listing hits for as many sentinels as possible. Any
sentinel with **no** hit in the full build is dead — replace it. Find a live
replacement with:

```bash
grep -rlo "some-candidate-string" frontend/src/cockpit frontend/src/collab | head
```

Do not proceed until every sentinel in the list fires against the full build.

- [ ] **Step 3: Run against the match build**

```bash
VITE_APP_EDITION=match pnpm -C frontend build
node scripts/check_match_bundle.mjs frontend/dist
```

Expected: exit 0, `match bundle clean: N files, none of 5 sentinels`.

If it exits 1, something in the release-1 graph still reaches the cockpit. Read
the named file and cut the import. In order of likelihood: the `virtual:app-router`
alias did not take effect (check `process.env.VITE_APP_EDITION` is actually set
for the build — `pnpm build:match` sets it, a bare `vite build` does not); a
shared barrel file under `components/` re-exports a cockpit component; or
`AdminLayout` imports a workspace screen.

- [ ] **Step 4: Wire it into package.json**

In `frontend/package.json` `"scripts"`, add:

```json
    "build:match": "VITE_APP_EDITION=match vite build",
    "check:bundle": "node ../scripts/check_match_bundle.mjs dist"
```

- [ ] **Step 5: Mutation-test the gate**

Add a static cockpit import to `frontend/src/router.simple.tsx`:

```tsx
import { CockpitSidebar } from "./cockpit/CockpitSidebar";
```

and reference it so it is not shaken out — add `{ path: "profiles/x", element:
<CockpitSidebar /> }` to the `SimpleShell` children temporarily. Then:

```bash
pnpm -C frontend build:match && pnpm -C frontend check:bundle
```

Expected: **exit 1**, naming a file and a sentinel.

Revert both edits and re-run; expected exit 0.

- [ ] **Step 6: Commit**

```bash
git add scripts/check_match_bundle.mjs frontend/package.json
git commit -m "test(edition): prove the match bundle carries no cockpit code

Greps emitted JS/CSS for string literals from the excluded modules rather than
inspecting chunk filenames. Vite may fold an excluded module into a
differently-named chunk at any time, so a filename check would go green while the
code shipped.

Each sentinel is verified to FIRE against a full build before its absence from a
match build is trusted — a sentinel that appears in neither tests nothing."
```

---

### Task 7: Remove the drafting links and gate 4

Spec §9 gate 4 and §4.1. Two "Start drafting" links point at `/cockpit/new`,
which release 1 excludes. The bundle gate proves the cockpit *code* is absent; it
says nothing about a link pointing at a route the server will 404. **They must be
removed, not disabled** — release 2 adds drafting back on top of the Saved list
Plan B builds.

**Files:**
- Modify: `frontend/src/routes/CallRow.tsx:52-56`
- Modify: `frontend/src/routes/CallDetailBody.tsx:46-50`
- Test: `frontend/src/noCockpitLinks.test.tsx` (create)

**Interfaces:**
- Consumes: `simpleRouter` (Task 4).
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/noCockpitLinks.test.tsx`:

```tsx
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { expect, it } from "vitest";

// Gate 4: no component reachable from the release-1 router may emit an href
// under /cockpit. The bundle gate proves the cockpit CODE is absent; it says
// nothing about a link pointing at a route the server will 404.
//
// This is a source scan rather than a render walk because rendering every
// release-1 screen with every data shape is not tractable, and a link inside a
// conditional branch would be missed by a render test. The tradeoff is that a
// dynamically-built href would slip through — acceptable, because none exist and
// gate 1 would catch the import that made one possible.

const SRC = join(import.meta.dirname ?? __dirname, ".");

// Files that legitimately mention /cockpit because they are the FULL edition.
const EXEMPT = [
  "router.tsx", // the full router mounts the cockpit; that is its job
  "cockpit/", // the cockpit's own source
  "collab/", // the collab editor
  "routes/Cockpit", // Cockpit.tsx, CockpitPlaceholder, etc.
  "routes/Workspace", // WorkspaceList, WorkspaceCreate
  "routes/WorkspacesFilters",
  "noCockpitLinks.test.tsx", // this file
  "router.simple.test.tsx", // names the excluded prefixes on purpose
  ".test.tsx",
  ".test.ts",
];

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (/\.(ts|tsx)$/.test(name)) out.push(p);
  }
  return out;
}

it("no release-1 source emits a /cockpit href", () => {
  const offenders: string[] = [];
  for (const file of walk(SRC)) {
    const rel = file.slice(SRC.length + 1);
    if (EXEMPT.some((e) => rel.includes(e))) continue;
    const text = readFileSync(file, "utf8");
    text.split("\n").forEach((line, i) => {
      if (/["'`]\/cockpit/.test(line)) offenders.push(`${rel}:${i + 1}  ${line.trim()}`);
    });
  }
  expect(offenders).toEqual([]);
});
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pnpm -C frontend test run src/noCockpitLinks.test.tsx
```

Expected: FAIL, listing `routes/CallRow.tsx` and `routes/CallDetailBody.tsx`.
This gate starts red by design — removing the links is what turns it green.

- [ ] **Step 3: Remove the link from `CallRow.tsx`**

Delete the `Anchor` and collapse the `Stack` (which now wraps one child):

```tsx
      <Stack gap="0.3rem" align="flex-end" flex="0 0 auto">
        <MatchBadge score={call.aggregate_score} />
      </Stack>
```

Then remove the now-unused `startHref` computation and the `Link` / `Anchor`
imports if nothing else in the file uses them. `pnpm -C frontend lint` will name
any that are now dead.

`MatchBadge` stays for now — Plan B replaces it with the criterion chip row.

- [ ] **Step 4: Remove the button from `CallDetailBody.tsx`**

Delete lines 46-50 entirely:

```tsx
        <Box mt="sm">
          <Button component={Link} to={`/cockpit/new?${startParams.toString()}`} size="sm">
            Start drafting workspace
          </Button>
        </Box>
```

Then remove the `startParams` construction above it and the `Button` import if
now unused.

**Leave the `{ranking.aggregate_score}` block at lines 57-67 alone.** Spec §4.2
forbids showing the raw score, but removing it is Plan B's task 4 — it belongs
with the chip row that replaces it, not with a link removal.

- [ ] **Step 5: Run the test to verify it passes**

```bash
pnpm -C frontend test run src/noCockpitLinks.test.tsx
```

Expected: 1 passed.

- [ ] **Step 6: Run the full frontend suite**

```bash
pnpm -C frontend test run
pnpm -C frontend lint
```

Expected: all pass. `CallRow.test.tsx` and any `CallDetailBody` test asserting on
"Start drafting" **will fail** — update those assertions to
`queryByRole("link", { name: /start drafting/i })` → `not.toBeInTheDocument()`.
Do not delete the tests; invert them, so the removal stays pinned.

Also re-run the Playwright dashboard spec, which may click the link:

```bash
pnpm -C frontend exec playwright test e2e/dashboard.spec.ts
```

- [ ] **Step 7: Mutation-test the gate**

Add `<Link to="/cockpit/new">x</Link>` to `frontend/src/routes/ProfilesList.tsx`.

```bash
pnpm -C frontend test run src/noCockpitLinks.test.tsx
```

Expected: FAIL naming `routes/ProfilesList.tsx` with its line number. Revert and
re-run; expected 1 passed.

- [ ] **Step 8: Rebuild and re-check the bundle**

Removing "Start drafting workspace" removes one of Task 6's sentinels from the
**full** build too. Confirm the sentinel list still has teeth:

```bash
pnpm -C frontend build && node scripts/check_match_bundle.mjs frontend/dist
```

Expected: exit 1 on the remaining four sentinels. If it now exits 0, the
sentinel list has gone dead — go back to Task 6 Step 2 and replace the dead
entries before committing.

```bash
pnpm -C frontend build:match && pnpm -C frontend check:bundle
```

Expected: exit 0.

- [ ] **Step 9: Commit**

```bash
git add frontend/src/routes/CallRow.tsx frontend/src/routes/CallDetailBody.tsx \
        frontend/src/noCockpitLinks.test.tsx frontend/src/routes/CallRow.test.tsx
git commit -m "feat(edition): remove the two drafting entry points into /cockpit

Both linked to /cockpit/new, which release 1 excludes, so they would have shipped
as buttons that 404. Removed rather than disabled — release 2 adds drafting back
on top of the Saved list.

The gate is a source scan, not a render walk: a link inside a conditional branch
would be missed by rendering, and rendering every screen against every data shape
is not tractable. Mutation-tested by planting a link in ProfilesList."
```

---

---

### Task 8: The off-white ground

Spec §5 "Visual language". The existing app is **dark-only** — a VS Code palette
on `:root`. Release 1 keeps every identity token (teal `#0f766e` / `#14b8a6`, the
deadline bands, the font stack, 4 px radii) but puts the *application* screens on
an off-white ground: it is a reading app, read in a bright office, by people who
are not developers. This is §11 step 2's "re-skin", and it is the only part of
that step that is real work — mounting the pages into `SimpleShell` (Task 4) does
the rest.

**Files:**
- Create: `frontend/src/theme/theme.match.css`
- Create: `frontend/src/theme/contrast.test.ts`
- Modify: `frontend/src/shell/SimpleShell.tsx` (one import)
- Modify: `frontend/src/App.tsx` (Mantine colour scheme)

**Interfaces:**
- Consumes: `APP_EDITION` (Task 5), `SimpleShell` (Task 3).
- Produces: nothing later tasks depend on. Plan B's serif for quoted evidence
  builds on this file.

**The exclusion mechanism is the import graph again.** `theme.match.css` is
imported by `SimpleShell.tsx`, which only the simple router reaches — so the full
build never ships it and cannot accidentally go light.

- [ ] **Step 1: Write the failing contrast test**

Release 1 is read in a bright office by clinicians, and the palette is being
inverted by hand. A token pair that drops below AA is invisible until someone
complains. Create `frontend/src/theme/contrast.test.ts`:

```ts
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// WCAG 2.1 relative luminance + contrast ratio. Hex in, ratio out.
function luminance(hex: string): number {
  const n = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(n.slice(i, i + 2), 16) / 255);
  const f = (c: number) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}

function ratio(a: string, b: string): number {
  const [x, y] = [luminance(a), luminance(b)].sort((m, n) => n - m);
  return (x + 0.05) / (y + 0.05);
}

function tokens(file: string): Record<string, string> {
  const css = readFileSync(join(import.meta.dirname ?? __dirname, file), "utf8");
  const out: Record<string, string> = {};
  for (const m of css.matchAll(/(--app-[\w-]+):\s*(#[0-9a-fA-F]{6})/g)) out[m[1]] = m[2];
  return out;
}

describe("the match edition palette", () => {
  const t = tokens("theme.match.css");

  it("defines every surface and text token it overrides", () => {
    for (const k of [
      "--app-editor",
      "--app-titlebar",
      "--app-sidebar",
      "--app-text",
      "--app-muted",
      "--app-muted-chrome",
      "--app-dim",
      "--app-border",
      "--app-hover",
      "--app-link",
      "--app-bad",
      "--app-warn",
    ]) {
      expect(t[k], `${k} missing from theme.match.css`).toMatch(/^#[0-9a-f]{6}$/i);
    }
  });

  // AA body text on the two surfaces people actually read on.
  it.each([
    ["--app-text", "--app-editor", 4.5],
    ["--app-text", "--app-titlebar", 4.5],
    ["--app-muted", "--app-editor", 4.5],
    ["--app-muted-chrome", "--app-titlebar", 4.5],
    ["--app-link", "--app-editor", 4.5],
    ["--app-bad", "--app-editor", 4.5],
    ["--app-warn", "--app-editor", 4.5],
    // Tertiary text (timestamps) may sit at AA-large.
    ["--app-dim", "--app-editor", 3.0],
  ])("%s on %s clears %sx", (fg, bg, min) => {
    expect(ratio(t[fg], t[bg])).toBeGreaterThanOrEqual(min as number);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pnpm -C frontend test run src/theme/contrast.test.ts
```

Expected: FAIL — `ENOENT ... theme.match.css`.

- [ ] **Step 3: Write the palette**

Create `frontend/src/theme/theme.match.css`:

```css
/* frontend/src/theme/theme.match.css
 *
 * Call Match release 1 puts the APPLICATION screens on an off-white ground
 * (spec §5 "Visual language"). It is a reading app, read in a bright office, by
 * people who are not developers — dark-on-dark is the register release 1 exists
 * to leave behind. The wall board stays dark, where dark is right, so the board
 * route (Plan C) must NOT import this file.
 *
 * Identity is preserved: the teal accent, the deadline bands, the font stack and
 * the 4px radii are the same decisions, re-tuned for contrast on light. Only the
 * surface, text and border tokens are redefined; every other --app-* token
 * inherits from theme.css.
 *
 * Loaded by SimpleShell.tsx, which only router.simple.tsx reaches — so the full
 * build never ships this file and cannot accidentally go light.
 *
 * Contrast is pinned by theme/contrast.test.ts. Do not edit a colour here
 * without running it.
 */

:root {
  color-scheme: light;

  /* Surfaces. Off-white, not pure white: less glare under office lighting, and
     it lets a genuinely white card lift off the page. */
  --app-editor: #faf9f7;
  --app-sidebar: #f2f0ec;
  --app-titlebar: #ffffff;
  --app-tabbar: #f2f0ec;
  --app-activitybar: #f2f0ec;

  /* Text. 15.0:1 on the editor ground — comfortably past AA, which matters
     because this is a screen people read paragraphs on, not a chrome surface.
     Measured, along with every ratio below, by theme/contrast.test.ts. */
  --app-text: #1f2328;
  --app-muted: #5a6169;
  --app-muted-chrome: #5a6169; /* the title bar is white here, so same value */
  --app-dim: #767f88; /* tertiary only (timestamps), never body text */

  --app-border: #e2ded7;
  --app-border-strong: #cfc9c0;
  --app-hover: #eeebe5;
  --app-selected: #d9f0ec;

  /* Identity + semantics, darkened to clear AA on a light ground. The dark
     palette's #14b8a6 measures 2.4:1 here and would have been unreadable. */
  --app-accent: #0f766e;
  --app-accent-bright: #0d9488;
  --app-link: #0f766e;
  --app-statusbar: #0f766e;
  --app-heading: #155e75;
  --app-good: #15803d;
  --app-warn: #a16207; /* the ≤30-day deadline band */
  --app-bad: #b91c1c; /* the ≤14-day deadline band */
}
```

If a ratio comes out under its floor in Step 4, darken the **foreground** token
rather than lightening the ground — the ground is the design decision, the
foreground is the adjustable half.

- [ ] **Step 4: Run the test to verify it passes**

```bash
pnpm -C frontend test run src/theme/contrast.test.ts
```

Expected: 9 passed.

- [ ] **Step 5: Load it from `SimpleShell`**

At the top of `frontend/src/shell/SimpleShell.tsx`, above the CSS-module import:

```tsx
// The match edition's light ground. Imported HERE, not in main.tsx, so it enters
// the module graph only through router.simple.tsx — the full build never sees it.
// Must come before the CSS module so the module's own rules win the cascade.
import "../theme/theme.match.css";
```

- [ ] **Step 6: Switch Mantine's colour scheme with the edition**

The `--app-*` tokens are ours, but Mantine's own components (Menu, Button, Badge)
read `defaultColorScheme`. Leaving it `"dark"` puts dark dropdowns on a light
page. In `frontend/src/App.tsx`:

```tsx
        <MantineProvider theme={theme} defaultColorScheme={APP_EDITION === "match" ? "light" : "dark"}>
```

`APP_EDITION` is already imported from Task 5.

- [ ] **Step 7: Look at it**

No test catches "the light theme loads but every card is still dark". Build and
open it:

```bash
pnpm -C frontend build:match && pnpm -C frontend check:bundle
VITE_APP_EDITION=match pnpm -C frontend dev
```

Walk `/`, a call detail, `/profiles`, `/me`, `/admin`. Confirm: off-white ground,
readable body text, teal actions, deadline bands legible, no dark dropdown and no
dark card. Fix what you see — the contrast test covers tokens, not components.

- [ ] **Step 8: Confirm the full edition did not move**

```bash
pnpm -C frontend build
grep -rl "faf9f7" frontend/dist && echo "LEAK" || echo "clean: light palette absent from the full build"
```

Expected: `clean: ...`. If the hex appears, `theme.match.css` reached the full
graph — something outside `router.simple.tsx` imports `SimpleShell`.

- [ ] **Step 9: Full frontend suite**

```bash
pnpm -C frontend test run
pnpm -C frontend lint
```

Expected: all pass. Existing tests should be unaffected — the full edition's
tokens did not change.

- [ ] **Step 10: Commit**

```bash
git add frontend/src/theme/theme.match.css frontend/src/theme/contrast.test.ts \
        frontend/src/shell/SimpleShell.tsx frontend/src/App.tsx
git commit -m "feat(edition): put the match edition on an off-white ground

Release 1 is a reading app used in a bright office by people who are not
developers; the VS Code dark palette is the register it exists to leave behind.
Identity tokens are preserved and re-tuned for contrast on light — the dark
palette's #14b8a6 measures 2.4:1 on off-white and would have been unreadable.

Loaded from SimpleShell, so it enters the graph only through router.simple.tsx
and the full build cannot accidentally go light. Contrast ratios are pinned by
theme/contrast.test.ts rather than eyeballed."
```

## Definition of done for Plan A

Run all of it, in this order, from a clean tree:

```bash
uv run pytest -n 16 --dist loadfile
uv run python scripts/gen_routes.py --check
uv run python scripts/gen_architecture.py && git diff --exit-code docs/ARCHITECTURE.md
uv run python scripts/gen_env_example.py && git diff --exit-code .env.example
pnpm -C frontend test run
pnpm -C frontend lint
pnpm -C frontend build
grep -rl "faf9f7" frontend/dist && echo "LEAK: the light palette is in the FULL build" || true
pnpm -C frontend build:match && pnpm -C frontend check:bundle
```

Expected: `3364 passed, 1 skipped` from pytest (the **`1 skipped`** is the
signal — `0 skipped` means a live Anthropic call ran), every generator check
clean, all frontend tests green, both builds succeed, the bundle check exits 0.

Then a manual smoke, because no gate here proves the app is usable:

```bash
VITE_APP_EDITION=match pnpm -C frontend build
# serve it against a match backend
APP_EDITION=match SPA_MODE=prod uv run uvicorn call_manager.main:create_app --factory
```

Open it and confirm: the calls list renders, a call detail opens, profiles list
and edit work, `/me` saves, an admin sees `/admin` and `/admin/users`, and
`/cockpit` gives a real 404 rather than a blank screen.

## What Plan A deliberately does not do

Named here so a reviewer does not read them as omissions:

- **No `call_decisions`, no Save, no triage inbox.** Plan B. Release 1 after Plan
  A still hides calls through the existing `user_call_dismissals` path.
- **No match reasons on the list**, and the detail page still shows its raw
  "Aggregate score" block. Plan B removes it, together with the chip row that
  replaces it.
- **No `summary`/`keywords` on `CallOut`.** Plan B, with the expanded card.
- **No boards, no public endpoint, no `/b/:slug`.** Plan C.
- **No serif for quoted evidence.** §5 makes Georgia-on-quotes a design
  requirement, but the quote only reaches the list view in Plan B; setting it
  here would style a block release 1's list does not yet render.
- **No phone-specific gestures.** The spec's responsive strategy keeps one DOM
  for everything except the wall board and the swipe gesture; both are Plan C
  and Plan B respectively.
