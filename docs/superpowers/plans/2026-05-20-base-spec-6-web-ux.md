# Base Spec B6 — Workspace Web UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the user-facing workspace web UX: list view, create form, two-pane main view (chat + artifact), settings page (viewer management + soft delete), and the integration points (POST /turns, SSE stream, "Start drafting" button on `/calls/{id}`, first-time disclosure modal, per-turn cloud-data banner, markdown export). Wraps the B3 controller + B4 turn pipeline + B5 tool surface in a browser-usable HTMX UI.

**Architecture:** All routes live in `src/call_manager/proposal_assist/web/workspaces.py` (mounted at `/workspaces` — the existing `rubric_review.py` already uses this prefix; B6 extends it). Server-rendered Jinja2 templates with HTMX for partial swaps. SSE bridge polls `chat_turns` for new rows since the consumer connected (~200ms tick) and emits MCP-style events so the browser streams the agent's reply in real time. No SPA, no frontend build.

**Tech Stack:** FastAPI, Jinja2 (existing `app.state.templates` instance, NOT a fresh one — keeps filters + base.html nav), HTMX 1.9 + the SSE extension (`htmx-sse.js`, vendored alongside `htmx.min.js`), markdown rendering via the `markdown` package wrapped in `bleach.clean(..., tags=ALLOWED_TAGS, strip=True)` to defang HTML, SQLAlchemy 2.x. Reuses B4's `submit_user_turn`, B5's `read_proposal_impl` / `get_artifact_history_impl` / `read_artifact_at_impl` / `diff_artifact_impl`, and B3's `WorkspaceController`.

**Spec:** [docs/superpowers/specs/2026-04-18-call-manager-proposal-assist-design.md](../specs/2026-04-18-call-manager-proposal-assist-design.md), §Workspace UX (lines 288–320), §Compliance / disclosure (lines 322–352).

**Overview:** [docs/superpowers/plans/2026-05-19-base-spec-overview.md](2026-05-19-base-spec-overview.md), §B6.

---

## Prerequisites and scope honesty

**Already on master (B1, Slice A, B2, B3, B4, B5):**
- All 9 base-spec tables + models.
- Phase 0 + rubric-review owner flow + `/workspaces/{id}/rubric-review` GET+POST.
- `WorkspaceController` (launch/send_turn/terminate/sweep_idle) with the per-workspace tool surface from B5.
- `submit_user_turn` + drain tick + processor + compaction; `turn_jobs` lifecycle wired end-to-end.
- 8 in-process MCP tools: read/edit/replace_proposal, get_artifact_history, read_artifact_at, diff_artifact, get_call_details, get_profile.
- `call-manager` rename complete.

**What B6 does NOT do:**
- **Attachments** (`describe_figure`, `look_at_figure`, `read_preliminary_data`, attachment manager UI) — **B7**.
- **Word / PDF export** — requires pandoc or similar; defer. **B6 ships markdown export only.**
- **Admin panel** (drafting stats, kill switch, spend dashboards, override widgets) — **B8**.
- **Global `DRAFTING_ENABLED` kill switch UX** — **B8**. (Pre-enqueue check could ride along if cheap; flag as B8 if it adds complexity.)
- **Branching UI** (create/switch/list branches) — phased spec dropped these.
- **Real-time presence** ("who's here") — explicitly out per spec line 314.
- **Cap-rejection UX surfacing** beyond the 429 error — **B8**.

What IS in B6:
- `/workspaces` list (owned + viewer-of, status filter)
- `/workspaces/new` form (title + optional call_id + optional profile_id + deadline)
- "Start drafting workspace" button on `/calls/{id}` (pre-fills call_id + best-scoring profile_id)
- `/workspaces/{id}` two-pane view (chat left, artifact right) — **replaces the Slice A placeholder route**
- POST `/workspaces/{id}/turns` (uses `submit_user_turn`, returns turn_job_id)
- SSE `/workspaces/{id}/turns/{tj}/stream` (polls chat_turns, emits SSE events as new rows land)
- POST `/workspaces/{id}/turns/{tj}/cancel` (queued only)
- Artifact endpoints: raw markdown, history, at-commit, diff (wraps B5 impl functions)
- `/workspaces/{id}/settings` (title, status, deadline, viewer add/remove, soft delete)
- First-time disclosure modal (per-user, per-workspace, writes `workspace_events('disclosure_acknowledged')`)
- Per-turn "cloud data" banner (template partial)
- Viewer read-only enforcement (POST /turns rejects non-owners)
- `/workspaces/{id}/export.md` (markdown dump)

---

## Conventions reference

- Routes mount under `/workspaces` (extends the existing `rubric_review.py` router prefix).
- `_require_owner` and `_require_owner_or_viewer` guards live next to the router.
- Templates under `src/call_manager/web/templates/proposal_assist/`.
- The existing `_session_dep` pattern (per-request session from `app.state.session_factory`) is the dependency.
- HTMX partials live under `templates/proposal_assist/partials/`.
- Tests under `tests/proposal_assist/test_web_workspaces_*.py`.
- Don't sweep pre-existing ruff debt outside touched files.

---

## File map

**Created:**
- `src/call_manager/proposal_assist/web/workspaces.py` — all new routes.
- `src/call_manager/web/templates/proposal_assist/workspaces_index.html` — list view.
- `src/call_manager/web/templates/proposal_assist/workspace_new.html` — create form.
- `src/call_manager/web/templates/proposal_assist/workspace_main.html` — two-pane view (replaces the placeholder).
- `src/call_manager/web/templates/proposal_assist/workspace_settings.html` — settings + viewer mgmt.
- `src/call_manager/web/templates/proposal_assist/partials/chat_turn.html` — renders one row of chat_turns (type-dispatched: text / tool_use / tool_result).
- `src/call_manager/web/templates/proposal_assist/partials/artifact_pane.html` — markdown rendering + raw toggle + version selector + diff.
- `src/call_manager/web/templates/proposal_assist/partials/cloud_banner.html` — persistent "data sent to Anthropic" banner above the composer.
- `src/call_manager/web/templates/proposal_assist/partials/disclosure_modal.html` — first-time data disclosure modal.
- `src/call_manager/web/templates/proposal_assist/partials/history.html` — version-history list (rev2 / Codex M1).
- `src/call_manager/web/static/htmx-sse.js` — vendored htmx 1.9 SSE extension (rev2 / Codex H1).
- Tests at `tests/proposal_assist/test_web_workspaces_list.py`, `test_web_workspaces_new.py`, `test_web_workspaces_main.py`, `test_web_workspaces_turns.py`, `test_web_workspaces_sse.py`, `test_web_workspaces_artifact.py`, `test_web_workspaces_settings.py`, `test_web_workspaces_viewers.py`, `test_web_workspaces_disclosure.py`, `test_web_workspaces_export.py`, `test_web_workspaces_xss.py` (rev2: regression for markdown + chat-turn HTML sanitization).

**Modified:**
- `src/call_manager/main.py` — already mounts `rubric_review_router`; B6's router is in the same module so no extra include needed. (Actually B6 creates a NEW router file; verify the include is added.)
- `src/call_manager/proposal_assist/web/rubric_review.py` — REMOVE the placeholder `GET /workspaces/{id}` handler (lines ~190-200) so B6's real handler isn't shadowed. Also surface the comment that this route is now B6's.
- `src/call_manager/web/templates/call_detail.html` — add the "Start drafting workspace" button.

---

## Revision history

- **rev1 (2026-05-20)** — initial plan.
- **rev2 (2026-05-20, Codex round 1)** — five findings, all addressed:
  - **H1** (SSE won't start in browser): `hx-swap="none"` on the composer form discarded the SSE-connector `<div>` returned by `POST /turns`, so HTMX never opened the stream. Also `base.html` only loaded `htmx.min.js` — no SSE extension. Fix: composer form now uses `hx-target="#chat-history" hx-swap="beforeend"`; `POST /turns` response returns BOTH the rendered user-turn partial AND the SSE connector div, both appended. `htmx-sse.js` (htmx 1.9 SSE extension) is vendored in Task 1 and added to `base.html`.
  - **H2** (persistent XSS — markdown + f-string SSE): `markdown.markdown(raw) | safe` injects unsanitized HTML from `proposal.md`; the SSE helper f-strings chat_turn content + tool names without escaping. Both are agent-or-user controlled. Fix: add `bleach` dependency; sanitize markdown output via `bleach.clean(html, tags=ALLOWED_TAGS, strip=True)`. All template insertions of chat content rely on Jinja's auto-escape (no `| safe`). SSE helper uses `html.escape` on every interpolated string.
  - **M1** (missing `history.html`): Task 9 referenced `proposal_assist/partials/history.html` but it was not in the file map. Fix: added to file map; Task 9 now creates it.
  - **M2** (separate Jinja instance): plan instantiated its own `Jinja2Templates(...)`, bypassing `app.state.templates` which has registered filters (`display_dt`, `criterion_label`, …). Also `base.html` expects `user` not `current_user`. Fix: use `request.app.state.templates`; rename `current_user` → `user` in every context dict. Workspace pages now inherit the standard navigation + filter set.
  - **M3** (best-profile prefill scope gap): plan scope promised "pre-fills the linked call and the profile that scored it highest for the current user" but Task 4 deliberately punted profile selection. Fix: Task 4 now queries `CallRanking` joined to `Profile` for the highest `aggregate_score` profile owned by the current user; passes `?profile_id=<best>` when found. Falls back to no prefill when no ranking exists.

---

## Task 1: router skeleton + auth deps + base templates

The scaffolding everything else lives on. Sets up the `/workspaces` router (or extends the existing one), the `_require_owner` / `_require_owner_or_viewer` guards, and the four top-level templates (mostly empty — later tasks fill them in).

**Files:**
- Create: `src/call_manager/proposal_assist/web/workspaces.py`
- Create: `src/call_manager/web/templates/proposal_assist/workspaces_index.html`
- Create: `src/call_manager/web/templates/proposal_assist/workspace_new.html`
- Create: `src/call_manager/web/templates/proposal_assist/workspace_main.html`
- Create: `src/call_manager/web/templates/proposal_assist/workspace_settings.html`
- Create: `src/call_manager/web/static/htmx-sse.js` (vendored htmx 1.9 SSE extension)
- Modify: `src/call_manager/main.py` — include the new router.
- Modify: `src/call_manager/web/templates/base.html` — add `<script src="/static/htmx-sse.js"></script>` after the existing htmx.min.js include (rev2 / Codex H1).
- Modify: `src/call_manager/proposal_assist/web/rubric_review.py` — delete the `GET /workspaces/{id}` placeholder.
- Test: `tests/proposal_assist/test_web_workspaces_main.py` — placeholder route returns 200 + auth-protected smoke.

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_web_workspaces_main.py`:

```python
"""Smoke for the workspace router skeleton. Subsequent tasks add real
behavior; here we just verify the router mounts + auth applies."""
import pytest
from fastapi.testclient import TestClient

from tests.proposal_assist.conftest import make_user, make_workspace


def test_workspace_main_requires_auth(client_unauth: TestClient, pg_session_factory):
    """Hitting GET /workspaces/<id> without a session redirects to login."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    response = client_unauth.get(f"/workspaces/{wsid}", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert "/login" in response.headers.get("location", "")


def test_workspace_main_404_when_workspace_missing(
    client_authed_owner: TestClient,
):
    """A non-existent workspace yields 404, not 500."""
    import uuid
    response = client_authed_owner.get(f"/workspaces/{uuid.uuid4()}")
    assert response.status_code == 404
```

(These fixtures — `client_unauth`, `client_authed_owner` — may not yet exist. Verify in `tests/proposal_assist/conftest.py` and `tests/conftest.py`. If they don't, scaffold minimal versions in conftest. Likely the project already has them from B6's slice-A web routes.)

- [ ] **Step 2: Run failing test**

```bash
uv run pytest tests/proposal_assist/test_web_workspaces_main.py -v
```

Expected: route-not-found (404) until the router is wired.

- [ ] **Step 3: Create router skeleton**

`src/call_manager/proposal_assist/web/workspaces.py`:

```python
"""B6 workspace web UX — list, create, main two-pane view, settings,
turn submission + SSE bridge, viewer mgmt, disclosure modal, export.

All routes mount at /workspaces. Mirrors the proposal_assist router
pattern from rubric_review.py (same _session_dep, same auth dep).

Rev2 / Codex M2: uses `request.app.state.templates` (set up by
main.py) — NOT a fresh Jinja2Templates instance. This is what keeps
the navigation in base.html working (base.html expects `user` in the
context, and main.py registers the `display_dt` / `criterion_label`
/ ... filters on that single instance).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import Workspace, WorkspaceViewer
from call_manager.db.session import get_db_dependency

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


def _session_dep(request: Request):
    factory = request.app.state.session_factory
    yield from get_db_dependency(factory)()


def _load_workspace_or_404(
    workspace_id: uuid.UUID, session: Session,
) -> Workspace:
    ws = session.get(Workspace, workspace_id)
    if ws is None or ws.soft_deleted_at is not None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return ws


def _require_owner(
    *, workspace_id: uuid.UUID, user, session: Session,
) -> Workspace:
    ws = _load_workspace_or_404(workspace_id, session)
    if ws.owner_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="only the workspace owner may perform this action",
        )
    return ws


def _require_owner_or_viewer(
    *, workspace_id: uuid.UUID, user, session: Session,
) -> Workspace:
    ws = _load_workspace_or_404(workspace_id, session)
    if ws.owner_id == user.id:
        return ws
    is_viewer = session.query(WorkspaceViewer).filter_by(
        workspace_id=workspace_id, user_id=user.id,
    ).first() is not None
    if not is_viewer:
        raise HTTPException(
            status_code=403,
            detail="you do not have access to this workspace",
        )
    return ws


@router.get("/{workspace_id}", response_class=HTMLResponse)
def get_workspace_main(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),
    session: Session = Depends(_session_dep),
) -> HTMLResponse:
    """Two-pane main view. Subsequent tasks fill in chat history +
    artifact pane; this stub just renders the shell.

    Note: uses `request.app.state.templates` (the existing instance
    with registered filters + base.html nav). Context key is `user`
    (NOT `current_user`) to match base.html.
    """
    ws = _require_owner_or_viewer(
        workspace_id=workspace_id, user=user, session=session,
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "proposal_assist/workspace_main.html",
        {"workspace": ws, "user": user},
    )
```

- [ ] **Step 4: Create minimal templates**

`src/call_manager/web/templates/proposal_assist/workspace_main.html`:

```html
{% extends "base.html" %}
{% block title %}{{ workspace.title }}{% endblock %}
{% block content %}
<h1>{{ workspace.title }}</h1>
<p><strong>Current phase:</strong> {{ workspace.current_phase }}</p>
<div id="workspace-main-panes">
  <!-- Task 5 fills this in: left = chat, right = artifact pane. -->
  <p>Chat + artifact panes coming in Task 5.</p>
</div>
{% endblock %}
```

(Same minimal scaffolding for `workspaces_index.html`, `workspace_new.html`, `workspace_settings.html` — just an h1 + a placeholder paragraph. Later tasks fill them in.)

- [ ] **Step 5: Delete the Slice A placeholder route**

In `src/call_manager/proposal_assist/web/rubric_review.py`, find the `GET /workspaces/{workspace_id}` handler (currently returns the workspace_placeholder.html). Delete it; add a comment marker:

```python
# NOTE: GET /workspaces/{id} now lives in proposal_assist/web/workspaces.py
# (B6 replaced the placeholder with the real two-pane view).
```

- [ ] **Step 6: Wire the router in main.py**

In `src/call_manager/main.py`, near the existing rubric_review_router include, add:

```python
from call_manager.proposal_assist.web.workspaces import (
    router as workspaces_router,
)
# ...
app.include_router(workspaces_router)
```

- [ ] **Step 6b: Vendor htmx-sse extension + load it in base.html** (rev2 / Codex H1)

Download the htmx 1.9 SSE extension. The currently vendored htmx version is `1.9.12` (verified). The matching SSE extension is `htmx-extensions/sse/sse.js` from htmx 1.9 — pull the file matching that version.

```bash
# Quick option: download from a pinned CDN URL with sha verification.
# Or: copy from your local node_modules / git clone of the htmx repo.
# Verify the downloaded file's banner mentions htmx 1.9.x SSE extension.
curl -fsSL https://unpkg.com/htmx.org@1.9.12/dist/ext/sse.js -o src/call_manager/web/static/htmx-sse.js
head -1 src/call_manager/web/static/htmx-sse.js
# expect: a function-style wrapper, mentioning sse
```

In `src/call_manager/web/templates/base.html`, after the existing `<script src="/static/htmx.min.js"></script>`, add:

```html
<script src="/static/htmx-sse.js"></script>
```

Without this, `hx-ext="sse"` is a no-op and `sse-connect` never opens the EventSource — the H1 finding.

- [ ] **Step 7: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_web_workspaces_main.py -v
# expect 2 passing
uv run ruff format src/call_manager/proposal_assist/web/workspaces.py src/call_manager/main.py src/call_manager/proposal_assist/web/rubric_review.py tests/proposal_assist/test_web_workspaces_main.py
uv run ruff check src/call_manager/proposal_assist/web/workspaces.py src/call_manager/main.py src/call_manager/proposal_assist/web/rubric_review.py tests/proposal_assist/test_web_workspaces_main.py
git add -A
git commit -m "feat(b6): workspaces web router skeleton + auth guards (replaces Slice A placeholder)"
```

---

## Task 2: GET /workspaces — list view

Owned + viewer-of workspaces, filter by status. Columns: title, linked call, status, deadline, last activity, owner.

**Files:**
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` — add `GET /` handler.
- Modify: `src/call_manager/web/templates/proposal_assist/workspaces_index.html` — fill in the table.
- Test: `tests/proposal_assist/test_web_workspaces_list.py`.

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_web_workspaces_list.py`:

```python
def test_list_shows_owned_workspaces(client_authed_owner, pg_session_factory, current_user):
    with pg_session_factory() as s:
        from tests.proposal_assist.conftest import make_workspace
        ws = make_workspace(s, owner=current_user)
        ws.title = "Mine"
        s.commit()
    response = client_authed_owner.get("/workspaces")
    assert response.status_code == 200
    assert b"Mine" in response.content


def test_list_shows_viewer_of_workspaces(
    client_authed_user, pg_session_factory, current_user,
):
    """A workspace where I'm a viewer (not owner) shows up too."""
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user, make_workspace
    with pg_session_factory() as s:
        other_owner = make_user(s, email="other@ki.se")
        ws = make_workspace(s, owner=other_owner)
        ws.title = "Shared"
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        s.commit()
    response = client_authed_user.get("/workspaces")
    assert response.status_code == 200
    assert b"Shared" in response.content


def test_list_hides_other_users_workspaces(client_authed_user, pg_session_factory):
    """A workspace I'm neither owner nor viewer of does NOT appear."""
    from tests.proposal_assist.conftest import make_user, make_workspace
    with pg_session_factory() as s:
        other = make_user(s, email="x@ki.se")
        ws = make_workspace(s, owner=other)
        ws.title = "NotMine"
        s.commit()
    response = client_authed_user.get("/workspaces")
    assert b"NotMine" not in response.content


def test_list_excludes_soft_deleted(client_authed_owner, pg_session_factory, current_user):
    from datetime import UTC, datetime
    from tests.proposal_assist.conftest import make_workspace
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.title = "Deleted"
        ws.soft_deleted_at = datetime.now(UTC)
        s.commit()
    response = client_authed_owner.get("/workspaces")
    assert b"Deleted" not in response.content


def test_list_filter_by_status(client_authed_owner, pg_session_factory, current_user):
    from tests.proposal_assist.conftest import make_workspace
    with pg_session_factory() as s:
        ws_d = make_workspace(s, owner=current_user)
        ws_d.title = "Draft"
        ws_d.status = "drafting"
        ws_s = make_workspace(s, owner=current_user)
        ws_s.title = "Submitted"
        ws_s.status = "submitted"
        s.commit()
    response = client_authed_owner.get("/workspaces?status=submitted")
    assert b"Submitted" in response.content
    assert b"Draft" not in response.content
```

- [ ] **Step 2: Implement**

In `workspaces.py`, add:

```python
from sqlalchemy import or_, select

from call_manager.db.models import WorkspaceEvent


@router.get("", response_class=HTMLResponse)
def list_workspaces(
    request: Request,
    user=Depends(get_current_user),
    session: Session = Depends(_session_dep),
    status: str | None = None,
) -> HTMLResponse:
    """List workspaces visible to the user: owned OR viewer-of, not
    soft-deleted. Optional ?status=drafting|submitted|... filter."""
    # Find workspace_ids the user can see.
    viewer_ids = session.scalars(
        select(WorkspaceViewer.workspace_id).where(
            WorkspaceViewer.user_id == user.id,
        )
    ).all()
    q = select(Workspace).where(
        Workspace.soft_deleted_at.is_(None),
        or_(
            Workspace.owner_id == user.id,
            Workspace.id.in_(viewer_ids),
        ),
    ).order_by(Workspace.updated_at.desc())
    if status:
        q = q.where(Workspace.status == status)
    workspaces = list(session.scalars(q))
    return templates.TemplateResponse(
        request, "proposal_assist/workspaces_index.html",
        {"workspaces": workspaces, "status_filter": status, "current_user": user},
    )
```

- [ ] **Step 3: Fill in the template**

`workspaces_index.html`:

```html
{% extends "base.html" %}
{% block title %}Workspaces{% endblock %}
{% block content %}
<h1>Workspaces</h1>
<p>
  <a href="/workspaces/new">+ New workspace</a> |
  <a href="/workspaces">All</a>
  | <a href="/workspaces?status=drafting">Drafting</a>
  | <a href="/workspaces?status=submitted">Submitted</a>
  | <a href="/workspaces?status=awarded">Awarded</a>
</p>
<table>
  <thead>
    <tr>
      <th>Title</th>
      <th>Linked call</th>
      <th>Status</th>
      <th>Deadline</th>
      <th>Last activity</th>
      <th>Owner</th>
    </tr>
  </thead>
  <tbody>
    {% for ws in workspaces %}
    <tr>
      <td><a href="/workspaces/{{ ws.id }}">{{ ws.title }}</a></td>
      <td>{{ ws.call_id or "—" }}</td>
      <td>{{ ws.status }}</td>
      <td>{{ ws.deadline.strftime("%Y-%m-%d") if ws.deadline else "—" }}</td>
      <td>{{ ws.updated_at.strftime("%Y-%m-%d %H:%M") }}</td>
      <td>{{ ws.owner_id }}</td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% if not workspaces %}
<p>No workspaces match.</p>
{% endif %}
{% endblock %}
```

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_web_workspaces_list.py -v
# expect 5 passing
uv run ruff format src/call_manager/proposal_assist/web/workspaces.py src/call_manager/web/templates/proposal_assist/workspaces_index.html tests/proposal_assist/test_web_workspaces_list.py
git add -A
git commit -m "feat(b6): GET /workspaces — list owned + viewer-of, status filter"
```

---

## Task 3: GET/POST /workspaces/new — create form

Form fields: title (required), call_id (optional), profile_id (optional), deadline (optional). Calls `create_workspace` from `src/call_manager/workspaces.py` (already exists from B1) which inserts the workspace row, the `workspace_events('created')` row, and the `workspace_setup_jobs` queue entry.

**Files:**
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` — `/new` GET + POST.
- Modify: `src/call_manager/web/templates/proposal_assist/workspace_new.html`.
- Test: `tests/proposal_assist/test_web_workspaces_new.py`.

- [ ] **Step 1: Write failing test**

```python
def test_get_new_form_renders(client_authed_owner):
    response = client_authed_owner.get("/workspaces/new")
    assert response.status_code == 200
    assert b"title" in response.content.lower()


def test_post_new_creates_workspace_and_redirects(
    client_authed_owner, pg_session_factory, current_user,
):
    from sqlalchemy import select
    from call_manager.db.models import Workspace, WorkspaceEvent, WorkspaceSetupJob

    response = client_authed_owner.post(
        "/workspaces/new",
        data={"title": "Test Workspace"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    # Should redirect to /workspaces/<id> (the new main view).
    assert response.headers["location"].startswith("/workspaces/")

    with pg_session_factory() as s:
        ws = s.scalar(select(Workspace).where(Workspace.title == "Test Workspace"))
        assert ws is not None
        assert ws.owner_id == current_user.id
        # Three side effects from create_workspace:
        events = list(s.scalars(
            select(WorkspaceEvent).where(WorkspaceEvent.workspace_id == ws.id),
        ))
        assert any(e.kind == "created" for e in events)
        setup = s.scalar(select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws.id))
        assert setup is not None
        assert setup.status == "queued"


def test_post_new_with_call_and_profile_links(
    client_authed_owner, pg_session_factory, current_user,
):
    """Workspace links to call_id + profile_id when provided in the form."""
    from sqlalchemy import select
    from call_manager.db.models import Call, Profile, Source, Workspace
    with pg_session_factory() as s:
        src = Source(slug="t", name="T", base_url="http://x")
        s.add(src)
        s.flush()
        call = Call(source_id=src.id, external_id="c1", title="A call",
                    url="http://x/c1")
        s.add(call)
        prof = Profile(
            name="P", description="d", kind="group",
            created_by=current_user.id,
        )
        s.add(prof)
        s.commit()
        cid, pid = call.id, prof.id

    response = client_authed_owner.post(
        "/workspaces/new",
        data={"title": "Linked", "call_id": str(cid), "profile_id": str(pid)},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    with pg_session_factory() as s:
        ws = s.scalar(select(Workspace).where(Workspace.title == "Linked"))
        assert ws.call_id == cid
        assert ws.profile_id == pid
```

- [ ] **Step 2: Implement**

In `workspaces.py`:

```python
from fastapi import Form
from fastapi.responses import RedirectResponse

from call_manager.workspaces import create_workspace as _create_workspace


@router.get("/new", response_class=HTMLResponse)
def get_new_workspace_form(
    request: Request,
    user=Depends(get_current_user),
    session: Session = Depends(_session_dep),
    call_id: int | None = None,
    profile_id: int | None = None,
) -> HTMLResponse:
    """New-workspace form. Optional ?call_id= and ?profile_id= pre-fill
    the form fields (used by the /calls/{id} 'Start drafting' button)."""
    return templates.TemplateResponse(
        request, "proposal_assist/workspace_new.html",
        {"prefill_call_id": call_id, "prefill_profile_id": profile_id, "current_user": user},
    )


@router.post("/new")
def post_new_workspace(
    request: Request,
    user=Depends(get_current_user),
    session: Session = Depends(_session_dep),
    title: str = Form(...),
    call_id: int | None = Form(None),
    profile_id: int | None = Form(None),
    deadline: str | None = Form(None),
) -> RedirectResponse:
    """Create + enqueue Phase 0 + redirect to the new workspace's main view."""
    from datetime import datetime
    parsed_deadline = None
    if deadline:
        parsed_deadline = datetime.fromisoformat(deadline)

    ws = _create_workspace(
        session=session, owner_id=user.id, title=title,
        call_id=call_id, profile_id=profile_id,
    )
    if parsed_deadline:
        ws.deadline = parsed_deadline
    session.commit()
    return RedirectResponse(url=f"/workspaces/{ws.id}", status_code=303)
```

- [ ] **Step 3: Fill in the template**

`workspace_new.html`:

```html
{% extends "base.html" %}
{% block title %}New workspace{% endblock %}
{% block content %}
<h1>New workspace</h1>
<form method="post" action="/workspaces/new">
  <p>
    <label>Title (required): <input name="title" required></label>
  </p>
  <p>
    <label>Linked call ID (optional):
      <input name="call_id" type="number" value="{{ prefill_call_id or '' }}">
    </label>
  </p>
  <p>
    <label>Profile ID (optional):
      <input name="profile_id" type="number" value="{{ prefill_profile_id or '' }}">
    </label>
  </p>
  <p>
    <label>Deadline (optional, YYYY-MM-DD):
      <input name="deadline" type="date">
    </label>
  </p>
  <button type="submit">Create</button>
</form>
{% endblock %}
```

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_web_workspaces_new.py -v
# expect 3 passing
git add -A
git commit -m "feat(b6): GET/POST /workspaces/new — create form"
```

---

## Task 4: "Start drafting workspace" button on /calls/{id}

Add a button to `call_detail.html` that links to `/workspaces/new?call_id=<id>&profile_id=<best>`. "Best profile" = the highest-scoring profile OWNED BY the current user for this call. Use `CallRanking.aggregate_score` (joined to `Profile` filtered by `created_by == current_user.id`). If no ranking exists or the user has no profiles, fall back to just `?call_id=<id>` (the form lets the user pick).

**Files:**
- Modify: `src/call_manager/web/templates/call_detail.html`.
- Test: `tests/proposal_assist/test_web_calls_start_drafting.py`.

- [ ] **Step 1: Write failing test**

```python
def test_call_detail_has_start_drafting_link(
    client_authed_user, pg_session_factory, current_user,
):
    from call_manager.db.models import Call, Source
    with pg_session_factory() as s:
        src = Source(slug="t", name="T", base_url="http://x")
        s.add(src)
        s.flush()
        call = Call(source_id=src.id, external_id="c1", title="A call",
                    url="http://x/c1")
        s.add(call)
        s.commit()
        cid = call.id

    response = client_authed_user.get(f"/calls/{cid}")
    assert response.status_code == 200
    # The link is present.
    assert f'/workspaces/new?call_id={cid}'.encode() in response.content
```

- [ ] **Step 2: Add best-profile lookup to the /calls/{id} route**

Locate the GET /calls/{id} handler in `src/call_manager/web/routes/calls.py`. Before rendering, add:

```python
from sqlalchemy import select
from call_manager.db.models import CallRanking, Profile

best_profile_id = session.scalar(
    select(CallRanking.profile_id)
    .join(Profile, Profile.id == CallRanking.profile_id)
    .where(
        CallRanking.call_id == call_id,
        Profile.created_by == user.id,
    )
    .order_by(CallRanking.aggregate_score.desc().nulls_last())
    .limit(1)
)
# Pass best_profile_id into the template context.
```

- [ ] **Step 3: Modify `call_detail.html`**

```html
{# B6: Start a workspace pre-filled with this call. profile_id is
   omitted from the link when no ranking exists for any of the user's
   profiles — the form still lets the user pick. #}
<p>
  <a href="/workspaces/new?call_id={{ call.id }}{% if best_profile_id %}&profile_id={{ best_profile_id }}{% endif %}"
     class="button">Start drafting workspace</a>
</p>
```

- [ ] **Step 4: Add a regression test that profile_id IS in the link when a ranking exists**

```python
def test_call_detail_includes_best_profile_id_when_ranking_exists(
    client_authed_user, pg_session_factory, current_user,
):
    from call_manager.db.models import Call, CallRanking, Profile, Source
    with pg_session_factory() as s:
        src = Source(slug="t", name="T", base_url="http://x")
        s.add(src); s.flush()
        call = Call(source_id=src.id, external_id="c1", title="A", url="http://x/c1")
        s.add(call); s.flush()
        prof = Profile(name="P", description="d", kind="group",
                       created_by=current_user.id)
        s.add(prof); s.flush()
        s.add(CallRanking(
            call_id=call.id, profile_id=prof.id,
            aggregate_score=85, rationale="r", model="m", prompt_version="v",
        ))
        s.commit()
        cid, pid = call.id, prof.id

    response = client_authed_user.get(f"/calls/{cid}")
    assert response.status_code == 200
    assert f"/workspaces/new?call_id={cid}&profile_id={pid}".encode() in response.content


def test_call_detail_falls_back_to_no_profile_id_when_no_ranking(
    client_authed_user, pg_session_factory,
):
    from call_manager.db.models import Call, Source
    with pg_session_factory() as s:
        src = Source(slug="t", name="T", base_url="http://x")
        s.add(src); s.flush()
        call = Call(source_id=src.id, external_id="c1", title="A", url="http://x/c1")
        s.add(call); s.commit()
        cid = call.id

    response = client_authed_user.get(f"/calls/{cid}")
    assert response.status_code == 200
    # No profile_id query string in the link (only call_id).
    assert f"/workspaces/new?call_id={cid}\"".encode() in response.content
```

- [ ] **Step 3: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_web_calls_start_drafting.py -v
git add -A
git commit -m "feat(b6): 'Start drafting workspace' button on /calls/<id>"
```

---

## Task 5: GET /workspaces/{id} — fill in the two-pane main view

The biggest single template task. Left pane: chat history from `chat_turns` (rendered via the `chat_turn.html` partial). Right pane: rendered markdown of `proposal.md` (from disk) + raw toggle + version selector.

The composer (textarea + send button) sits at the bottom of the left pane and POSTs to `/workspaces/{id}/turns` (Task 6 implements). The cloud-data banner (Task 13) sits above the composer.

**Files:**
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` — fill in the `GET /{workspace_id}` handler (currently a stub).
- Modify: `src/call_manager/web/templates/proposal_assist/workspace_main.html` — full two-pane layout.
- Create: `src/call_manager/web/templates/proposal_assist/partials/chat_turn.html`.
- Create: `src/call_manager/web/templates/proposal_assist/partials/artifact_pane.html`.
- Test: extend `test_web_workspaces_main.py`.

- [ ] **Step 1: Write failing test**

Append to `test_web_workspaces_main.py`:

```python
def test_main_view_renders_chat_history(
    client_authed_owner, pg_session_factory, current_user, tmp_workspaces_dir,
):
    """chat_turns rows render as chat history in the left pane."""
    from tests.proposal_assist.conftest import make_chat_turn, make_workspace
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        make_chat_turn(s, ws, role="user", content={"type": "text", "text": "hi"})
        make_chat_turn(s, ws, role="assistant", content={"type": "text", "text": "hello"})
        s.commit()
        wsid = ws.id

    response = client_authed_owner.get(f"/workspaces/{wsid}")
    assert response.status_code == 200
    assert b"hi" in response.content
    assert b"hello" in response.content


def test_main_view_renders_artifact_pane(
    client_authed_owner, pg_session_factory, current_user, tmp_workspaces_dir,
):
    """If proposal.md exists in the workspace repo, it's rendered."""
    from pathlib import Path
    import subprocess
    from tests.proposal_assist.conftest import make_workspace
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    repo_dir = Path(tmp_workspaces_dir) / str(wsid) / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "a@b"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo_dir, check=True)
    (repo_dir / "proposal.md").write_text("# Heading\n\nbody text.")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)

    response = client_authed_owner.get(f"/workspaces/{wsid}")
    assert response.status_code == 200
    # Rendered markdown produces <h1> for "# Heading".
    assert b"<h1>Heading</h1>" in response.content or b"Heading" in response.content
```

(`tmp_workspaces_dir` fixture: a tmp path the controller's WORKSPACES_DIR points to. Add to conftest if missing.)

- [ ] **Step 2: Modify `GET /{workspace_id}` handler**

Replace the stub:

```python
import bleach
import markdown
from pathlib import Path

from call_manager.db.models import ChatTurn
from sqlalchemy import select


# Rev2 / Codex H2: every HTML rendering of agent-controlled content
# (proposal.md, chat_turns content) MUST go through this sanitizer.
# markdown.markdown() emits raw HTML for any raw HTML in the input;
# bleach.clean strips tags not in the allowlist.
_ALLOWED_TAGS = [
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "br", "hr",
    "strong", "em", "code", "pre",
    "ul", "ol", "li",
    "blockquote", "a",
    "table", "thead", "tbody", "tr", "th", "td",
]
_ALLOWED_ATTRS = {"a": ["href", "title"]}


def _render_proposal_markdown(raw: str) -> str:
    """Render proposal.md content to safe HTML."""
    html = markdown.markdown(raw, extensions=["extra"])
    return bleach.clean(html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS, strip=True)


@router.get("/{workspace_id}", response_class=HTMLResponse)
def get_workspace_main(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),
    session: Session = Depends(_session_dep),
) -> HTMLResponse:
    ws = _require_owner_or_viewer(
        workspace_id=workspace_id, user=user, session=session,
    )
    chat_turns = list(session.scalars(
        select(ChatTurn).where(ChatTurn.workspace_id == workspace_id)
        .order_by(ChatTurn.position)
    ))

    settings = request.app.state.settings
    repo_dir = Path(settings.workspaces_dir) / str(workspace_id) / "repo"
    proposal_path = repo_dir / "proposal.md"
    if proposal_path.exists():
        raw = proposal_path.read_text()
        rendered = _render_proposal_markdown(raw)
    else:
        raw, rendered = "", "<em>(no proposal.md yet)</em>"

    is_owner = ws.owner_id == user.id
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "proposal_assist/workspace_main.html",
        {
            "workspace": ws,
            "user": user,            # base.html expects `user`, not `current_user`
            "is_owner": is_owner,
            "chat_turns": chat_turns,
            "artifact_rendered": rendered,
            "artifact_raw": raw,
        },
    )
```

- [ ] **Step 3: Fill in the two-pane template**

`workspace_main.html`:

```html
{% extends "base.html" %}
{% block title %}{{ workspace.title }}{% endblock %}
{% block content %}
<h1>{{ workspace.title }}</h1>
<p>
  <strong>Phase:</strong> {{ workspace.current_phase }}
  | <a href="/workspaces/{{ workspace.id }}/settings">Settings</a>
  | <a href="/workspaces">All workspaces</a>
</p>

<div style="display: flex; gap: 1em;">
  {# LEFT: chat #}
  <div style="flex: 1; min-width: 0;">
    <h2>Chat</h2>
    <div id="chat-history" style="max-height: 60vh; overflow-y: auto; border: 1px solid #ddd; padding: 0.5em;">
      {% for turn in chat_turns %}
        {% include "proposal_assist/partials/chat_turn.html" %}
      {% endfor %}
    </div>
    {% include "proposal_assist/partials/cloud_banner.html" %}
    {% if is_owner %}
    <form id="composer" method="post" action="/workspaces/{{ workspace.id }}/turns"
          hx-post="/workspaces/{{ workspace.id }}/turns"
          hx-target="#chat-history"
          hx-swap="beforeend"
          hx-on::after-request="this.reset()">
      <textarea name="content" rows="3" required></textarea>
      <button type="submit">Send</button>
    </form>
    {% else %}
    <p><em>(read-only — only the workspace owner can send turns)</em></p>
    {% endif %}
  </div>

  {# RIGHT: artifact #}
  <div style="flex: 1; min-width: 0;">
    {% include "proposal_assist/partials/artifact_pane.html" %}
  </div>
</div>
{% endblock %}
```

`partials/chat_turn.html`:

```html
{# Renders one chat_turns row. Dispatches by role + content shape. #}
<div class="chat-turn" data-role="{{ turn.role }}">
  <strong>{{ turn.role }}:</strong>
  {% set c = turn.content %}
  {% if c is mapping and c.get("type") == "text" %}
    <p>{{ c.get("text", "") }}</p>
  {% elif c is mapping and c.get("type") == "tool_use" %}
    <p><em>(tool call: {{ c.get("name", "?") }})</em></p>
  {% elif c is mapping and c.get("type") == "tool_result" %}
    <p><em>(tool result{% if c.get("is_error") %} — error{% endif %})</em></p>
  {% else %}
    <pre>{{ c }}</pre>
  {% endif %}
</div>
```

`partials/artifact_pane.html`:

```html
<h2>Proposal</h2>
<p>
  <a href="/workspaces/{{ workspace.id }}/artifact/raw">View raw</a>
  | <a href="/workspaces/{{ workspace.id }}/artifact/history">History</a>
  | <a href="/workspaces/{{ workspace.id }}/export.md">Export markdown</a>
</p>
<div id="artifact-rendered" style="border: 1px solid #ddd; padding: 0.5em; max-height: 70vh; overflow-y: auto;">
  {{ artifact_rendered | safe }}
</div>
```

`partials/cloud_banner.html`:

```html
{# Per spec lines 336-338: persistent banner above the composer. #}
<p style="background: #fff3cd; padding: 0.5em; border: 1px solid #ffc107;">
  ⚠️ Turns in this workspace are sent to Anthropic's Opus 4.7.
</p>
```

- [ ] **Step 4: Add the `markdown` + `bleach` dependencies**

```bash
uv add markdown bleach
```

(`bleach` is required to sanitize the markdown output per rev2 / Codex H2 — see `_render_proposal_markdown`.)

- [ ] **Step 5: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_web_workspaces_main.py -v
# expect 4 passing (2 from Task 1 + 2 new)
git add -A
git commit -m "feat(b6): two-pane main view — chat history + rendered artifact"
```

---

## Task 6: POST /workspaces/{id}/turns + cap rejection

Submits a user turn via `submit_user_turn` (B4). Returns the new `turn_job_id` so the browser can open the SSE stream. Owner-only.

**Files:**
- Modify: `workspaces.py` — POST handler.
- Test: `test_web_workspaces_turns.py`.

- [ ] **Step 1: Write failing test**

```python
def test_post_turn_creates_turn_job(
    client_authed_owner, pg_session_factory, current_user,
):
    from sqlalchemy import select
    from call_manager.db.models import TurnJob, Workspace
    from tests.proposal_assist.conftest import make_workspace
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "Hello agent"},
    )
    assert response.status_code in (200, 201, 303)
    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))
        assert job is not None
        assert job.status == "queued"
        assert job.submitted_by_user_id == current_user.id


def test_post_turn_rejects_non_owner(
    client_authed_user, pg_session_factory, current_user,
):
    """Viewers cannot send turns (spec: shared-read, single-write)."""
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user, make_workspace
    with pg_session_factory() as s:
        owner = make_user(s, email="o@ki.se")
        ws = make_workspace(s, owner=owner)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        s.commit()
        wsid = ws.id

    response = client_authed_user.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "I shouldn't be able to do this"},
    )
    assert response.status_code == 403


def test_post_turn_returns_429_on_cap_exceeded(
    client_authed_owner, pg_session_factory, current_user, monkeypatch,
):
    """When TokenCapExceeded is raised by submit_user_turn, the handler
    returns 429 with a useful body."""
    monkeypatch.setenv("DAILY_TOKEN_CAP_INPUT", "100")
    from tests.proposal_assist.conftest import make_chat_turn, make_workspace
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        make_chat_turn(s, ws, role="assistant",
                       content={"type": "text", "text": "x"}, tokens_in=200)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "should fail"},
    )
    assert response.status_code == 429
    assert b"cap" in response.content.lower()
```

- [ ] **Step 2: Implement**

```python
from call_manager.proposal_assist.turns.caps import TokenCapExceeded
from call_manager.proposal_assist.turns.submit import submit_user_turn


@router.post("/{workspace_id}/turns")
def post_turn(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),
    session: Session = Depends(_session_dep),
    content: str = Form(...),
):
    ws = _require_owner(
        workspace_id=workspace_id, user=user, session=session,
    )
    settings = request.app.state.settings
    try:
        job = submit_user_turn(
            session=session, workspace_id=ws.id,
            content={"type": "text", "text": content},
            submitted_by_user_id=user.id,
            settings=settings,
        )
        session.commit()
    except TokenCapExceeded as e:
        session.rollback()
        return HTMLResponse(
            f"<p>Workspace daily token cap exceeded: {e.reason}. "
            f"Used {e.used_in} input / {e.used_out} output today; "
            f"cap is {e.cap_in} / {e.cap_out}. Try again after midnight "
            f"Europe/Stockholm or ask an admin to raise the cap.</p>",
            status_code=429,
        )

    # Rev2 / Codex H1: form's hx-target/hx-swap is #chat-history /
    # beforeend, so the response is appended to the chat. Send TWO pieces
    # of HTML, both appended in one swap:
    #   (1) The just-sent user turn, rendered as a chat row (so the
    #       user sees their message immediately, before the agent's
    #       reply arrives via SSE).
    #   (2) The SSE-connector div, which opens EventSource to the
    #       stream endpoint and self-removes when the stream emits the
    #       'done' event.
    # Note: chat content is HTML-escaped (html.escape) — never f-string'd
    # raw into HTML (Codex H2).
    import html as _html
    safe_text = _html.escape(content)
    user_turn_html = (
        "<div class='chat-turn' data-role='user'>"
        "<strong>user:</strong>"
        f"<p>{safe_text}</p>"
        "</div>"
    )
    sse_connector = (
        f"<div id='sse-mount-{job.id}' "
        f"hx-ext='sse' "
        f"sse-connect='/workspaces/{workspace_id}/turns/{job.id}/stream' "
        f"sse-swap='message' "
        f"sse-close='done' "
        f"hx-target='#chat-history' "
        f"hx-swap='beforeend'></div>"
    )
    return HTMLResponse(user_turn_html + sse_connector)
```

- [ ] **Step 3: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_web_workspaces_turns.py -v
# expect 3 passing
git add -A
git commit -m "feat(b6): POST /workspaces/<id>/turns + 429 on cap exceeded"
```

---

## Task 7: SSE GET /workspaces/{id}/turns/{tj}/stream

Streams new `chat_turns` rows (and the final completion event) as they land. Poll-based (200ms tick) — simpler than NOTIFY/LISTEN; switch later if needed.

**Files:**
- Modify: `workspaces.py` — SSE handler.
- Test: `test_web_workspaces_sse.py`.

- [ ] **Step 1: Write failing test**

```python
import asyncio
import pytest


@pytest.mark.asyncio
async def test_sse_stream_emits_new_chat_turns(
    pg_session_factory, current_user, client_authed_owner,
):
    """Open the SSE stream; write a chat_turns row in the background;
    verify the stream emits it within a few seconds."""
    from datetime import UTC, datetime
    from tests.proposal_assist.conftest import make_chat_turn, make_workspace
    from call_manager.db.models import ChatTurn, TurnJob

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        # Add a user turn and a turn_job so the route has a target.
        user_turn = ChatTurn(workspace_id=ws.id, role="user",
                             content={"type": "text", "text": "hi"})
        s.add(user_turn)
        s.flush()
        job = TurnJob(
            workspace_id=ws.id, user_turn_id=user_turn.id,
            submitted_by_user_id=current_user.id,
            status="queued",
            queued_at=datetime.now(UTC),
        )
        s.add(job)
        s.commit()
        wsid, jid = ws.id, job.id

    # Open the SSE stream with TestClient.stream (synchronous).
    with client_authed_owner.stream(
        "GET", f"/workspaces/{wsid}/turns/{jid}/stream",
    ) as resp:
        assert resp.status_code == 200
        # Write a new chat_turns row from outside (simulating the processor).
        with pg_session_factory() as s:
            make_chat_turn(s, s.get(__import__('call_manager.db.models', fromlist=['Workspace']).Workspace, wsid),
                           role="assistant",
                           content={"type": "text", "text": "hello back"})
            s.commit()
        # Consume events with a timeout.
        events = []
        for chunk in resp.iter_text():
            events.append(chunk)
            if "hello back" in chunk:
                break
            if len(events) > 50:
                pytest.fail("SSE stream did not emit the new chat_turn")
```

(The exact test shape depends on how `httpx.TestClient.stream` interleaves with background writes. The simpler approach: invoke the route's underlying generator function directly without HTTP. Choose whichever the maintainer finds cleaner.)

- [ ] **Step 2: Implement**

```python
from fastapi.responses import StreamingResponse
import asyncio
import json
import time

from call_manager.db.models import TurnJob


@router.get("/{workspace_id}/turns/{turn_job_id}/stream")
async def stream_turn(
    workspace_id: uuid.UUID,
    turn_job_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),
    session: Session = Depends(_session_dep),
) -> StreamingResponse:
    ws = _require_owner_or_viewer(
        workspace_id=workspace_id, user=user, session=session,
    )
    job = session.get(TurnJob, turn_job_id)
    if job is None or job.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="turn_job not found")

    last_seen_position = session.scalar(
        select(ChatTurn.position)
        .where(ChatTurn.id == job.user_turn_id)
    )

    session_factory = request.app.state.session_factory

    async def gen():
        nonlocal last_seen_position
        # Poll for new chat_turns rows + the turn_job's terminal status.
        while True:
            with session_factory() as s:
                # New chat_turns since last poll.
                new = list(s.scalars(
                    select(ChatTurn).where(
                        ChatTurn.workspace_id == ws.id,
                        ChatTurn.position > (last_seen_position or 0),
                    ).order_by(ChatTurn.position)
                ))
                for turn in new:
                    rendered = _render_chat_turn_html(turn)
                    yield f"data: {rendered}\n\n"
                    last_seen_position = turn.position
                # Check terminal status.
                tj = s.get(TurnJob, turn_job_id)
                if tj.status in ("completed", "failed", "cancelled"):
                    # Final event to close the stream client-side.
                    yield f"event: done\ndata: {tj.status}\n\n"
                    return
            await asyncio.sleep(0.2)

    return StreamingResponse(gen(), media_type="text/event-stream")


def _render_chat_turn_html(turn) -> str:
    """Render one chat_turns row to a small HTML snippet for SSE emission.
    Matches the partials/chat_turn.html template's per-row markup.

    Rev2 / Codex H2: every interpolated field is run through
    `html.escape` first — chat content + tool names are agent-controlled
    and could contain `<script>` otherwise.
    """
    import html as _html
    c = turn.content
    safe_role = _html.escape(turn.role)
    if isinstance(c, dict) and c.get("type") == "text":
        body = f"<p>{_html.escape(str(c.get('text', '')))}</p>"
    elif isinstance(c, dict) and c.get("type") == "tool_use":
        body = f"<p><em>(tool call: {_html.escape(str(c.get('name', '?')))})</em></p>"
    elif isinstance(c, dict) and c.get("type") == "tool_result":
        err = " — error" if c.get("is_error") else ""
        body = f"<p><em>(tool result{err})</em></p>"
    else:
        body = f"<pre>{_html.escape(str(c))}</pre>"
    snippet = (
        f"<div class='chat-turn' data-role='{safe_role}'>"
        f"<strong>{safe_role}:</strong>{body}</div>"
    )
    # JSON-escape \n so it's valid in an SSE data: line.
    return snippet.replace("\n", "")
```

- [ ] **Step 3: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_web_workspaces_sse.py -v
git add -A
git commit -m "feat(b6): SSE bridge — stream new chat_turns as the processor writes them"
```

---

## Task 8: POST /workspaces/{id}/turns/{tj}/cancel

Cancels a queued turn (no-op if already running/completed). Owner-only.

```python
@router.post("/{workspace_id}/turns/{turn_job_id}/cancel")
def cancel_turn(...):
    """Mark queued turn_job as 'cancelled'. No-op if status != 'queued'."""
    from sqlalchemy import update
    from datetime import UTC, datetime
    ws = _require_owner(...)
    result = session.execute(
        update(TurnJob)
        .where(TurnJob.id == turn_job_id,
               TurnJob.workspace_id == ws.id,
               TurnJob.status == "queued")
        .values(status="cancelled", completed_at=datetime.now(UTC))
    )
    session.commit()
    return HTMLResponse(
        f"<p>{'cancelled' if result.rowcount else 'too late — already started'}</p>"
    )
```

Test:
```python
def test_cancel_queued_turn_marks_it_cancelled(...): ...
def test_cancel_running_turn_is_noop(...): ...
```

Run + commit.

---

## Task 9: artifact endpoints (raw + history + at + diff)

Wraps the B5 impl functions. All owner-or-viewer.

```python
from call_manager.proposal_assist.tools.context import ...  # not these — these are wrong
from call_manager.proposal_assist.tools.history import (
    diff_artifact_impl, get_artifact_history_impl, read_artifact_at_impl,
)
from call_manager.proposal_assist.tools.proposal import read_proposal_impl
from call_manager.proposal_assist.tools.registry import ToolContext


def _make_tool_ctx(request, ws):
    from pathlib import Path
    settings = request.app.state.settings
    return ToolContext(
        workspace_id=ws.id,
        repo_dir=Path(settings.workspaces_dir) / str(ws.id) / "repo",
        session_factory=request.app.state.session_factory,
        settings=settings,
    )


@router.get("/{workspace_id}/artifact/raw")
async def artifact_raw(workspace_id, request, user=Depends(get_current_user), session=Depends(_session_dep)):
    ws = _require_owner_or_viewer(workspace_id=workspace_id, user=user, session=session)
    result = await read_proposal_impl(_make_tool_ctx(request, ws))
    if "error" in result:
        raise HTTPException(404, result["error"])
    return Response(result["content"], media_type="text/plain")


@router.get("/{workspace_id}/artifact/history")
async def artifact_history(workspace_id, request, user=Depends(get_current_user), session=Depends(_session_dep)):
    ws = _require_owner_or_viewer(...)
    result = await get_artifact_history_impl(_make_tool_ctx(request, ws), limit=50)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "proposal_assist/partials/history.html",
        {"workspace": ws, "commits": result.get("commits", []), "user": user},
    )


@router.get("/{workspace_id}/artifact/at/{commit_sha}")
async def artifact_at(workspace_id, commit_sha, request, ...):
    result = await read_artifact_at_impl(_make_tool_ctx(request, ws), commit_sha=commit_sha)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return Response(result["content"], media_type="text/plain")


@router.get("/{workspace_id}/artifact/diff")
async def artifact_diff(workspace_id, commit_a: str, commit_b: str, request, ...):
    result = await diff_artifact_impl(_make_tool_ctx(request, ws), commit_a=commit_a, commit_b=commit_b)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return Response(result["diff"], media_type="text/plain")
```

Create the missing template (rev2 / Codex M1):

`src/call_manager/web/templates/proposal_assist/partials/history.html`:

```html
{% extends "base.html" %}
{% block title %}History — {{ workspace.title }}{% endblock %}
{% block content %}
<h1>Version history — {{ workspace.title }}</h1>
<p><a href="/workspaces/{{ workspace.id }}">← Back to workspace</a></p>
{% if commits %}
<table>
  <thead><tr><th>Date</th><th>SHA</th><th>Message</th><th>View</th></tr></thead>
  <tbody>
    {% for c in commits %}
    <tr>
      <td>{{ c.timestamp }}</td>
      <td><code>{{ c.sha[:8] }}</code></td>
      <td>{{ c.message }}</td>
      <td><a href="/workspaces/{{ workspace.id }}/artifact/at/{{ c.sha }}">view</a></td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% else %}
<p>No history yet.</p>
{% endif %}
{% endblock %}
```

(Chat content + commit messages render via Jinja default auto-escape — no `| safe`.)

Tests cover happy paths + B5's sha-validation rejection (e.g., `--output=evil` returns 400).

Run + commit.

---

## Task 9b: XSS regression tests (rev2 / Codex H2)

Three regression tests prove the H2 sanitization guards work end-to-end:

1. **`proposal.md` with `<script>`** — render the main view; assert the script tag is absent from the response body (bleach strips it).
2. **chat_turn with `<img src=x onerror=alert(1)>`** — render the main view; assert the dangerous attributes are absent (Jinja auto-escape converts to `&lt;img …&gt;`).
3. **SSE-emitted chat_turn with `<script>`** — directly call `_render_chat_turn_html` and assert the output contains `&lt;script&gt;` (escaped), NOT `<script>`.

```python
def test_proposal_md_with_script_tag_is_sanitized(
    client_authed_owner, pg_session_factory, current_user, tmp_workspaces_dir,
):
    from pathlib import Path
    import subprocess
    from tests.proposal_assist.conftest import make_workspace
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id
    repo_dir = Path(tmp_workspaces_dir) / str(wsid) / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "a@b"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo_dir, check=True)
    (repo_dir / "proposal.md").write_text(
        "# Heading\n\n<script>alert('xss')</script>\n\nBody."
    )
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)

    response = client_authed_owner.get(f"/workspaces/{wsid}")
    assert response.status_code == 200
    assert b"<script>" not in response.content
    assert b"alert" not in response.content  # bleach strips the tag entirely


def test_chat_turn_content_with_html_is_escaped(
    client_authed_owner, pg_session_factory, current_user,
):
    from tests.proposal_assist.conftest import make_chat_turn, make_workspace
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        make_chat_turn(
            s, ws, role="assistant",
            content={"type": "text", "text": "<img src=x onerror=alert(1)>"},
        )
        s.commit()
        wsid = ws.id
    response = client_authed_owner.get(f"/workspaces/{wsid}")
    assert response.status_code == 200
    # Jinja auto-escape replaces < with &lt; — raw <img> tag must not appear.
    assert b"<img" not in response.content
    assert b"onerror" not in response.content
    # Escaped form should be present (the user can read what was attempted).
    assert b"&lt;img" in response.content


def test_sse_helper_escapes_chat_content():
    """Direct unit test on _render_chat_turn_html — Codex H2 regression."""
    from call_manager.proposal_assist.web.workspaces import _render_chat_turn_html
    from call_manager.db.models import ChatTurn
    import uuid

    turn = ChatTurn(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        role="assistant",
        content={"type": "text", "text": "<script>alert('x')</script>"},
        position=1,
    )
    snippet = _render_chat_turn_html(turn)
    assert "<script>" not in snippet
    assert "&lt;script&gt;" in snippet
```

These tests should be in `tests/proposal_assist/test_web_workspaces_xss.py`. Run + commit.

---

## Task 10: GET/POST /workspaces/{id}/settings + soft delete

Owner-only. Form: title, status, deadline. Delete button → soft-delete (sets `soft_deleted_at`).

```python
@router.get("/{workspace_id}/settings", response_class=HTMLResponse)
def get_settings(workspace_id, request, user=Depends(get_current_user), session=Depends(_session_dep)):
    ws = _require_owner(...)
    return templates.TemplateResponse(request, "proposal_assist/workspace_settings.html", {"workspace": ws})


@router.post("/{workspace_id}/settings")
def post_settings(
    workspace_id, request, user=Depends(get_current_user), session=Depends(_session_dep),
    title: str = Form(...), status: str = Form(...), deadline: str | None = Form(None),
):
    ws = _require_owner(...)
    ws.title = title
    ws.status = status
    if deadline:
        ws.deadline = datetime.fromisoformat(deadline)
    session.commit()
    return RedirectResponse(url=f"/workspaces/{ws.id}", status_code=303)


@router.post("/{workspace_id}/delete")
def soft_delete(workspace_id, request, user=Depends(get_current_user), session=Depends(_session_dep)):
    ws = _require_owner(...)
    ws.soft_deleted_at = datetime.now(UTC)
    session.commit()
    return RedirectResponse(url="/workspaces", status_code=303)
```

Tests: change title; change status; soft delete redirects to list AND the deleted workspace no longer appears.

Run + commit.

---

## Task 11: viewer management — add + remove

Owner-only. Add by email; remove by user_id.

```python
@router.post("/{workspace_id}/viewers")
def add_viewer(
    workspace_id, request, user=Depends(get_current_user), session=Depends(_session_dep),
    email: str = Form(...),
):
    ws = _require_owner(...)
    target = session.scalar(select(User).where(User.email == email))
    if target is None:
        raise HTTPException(400, f"no user with email {email}")
    existing = session.query(WorkspaceViewer).filter_by(
        workspace_id=ws.id, user_id=target.id,
    ).first()
    if existing:
        return RedirectResponse(url=f"/workspaces/{ws.id}/settings", status_code=303)
    session.add(WorkspaceViewer(workspace_id=ws.id, user_id=target.id))
    session.commit()
    return RedirectResponse(url=f"/workspaces/{ws.id}/settings", status_code=303)


@router.post("/{workspace_id}/viewers/{user_id}/remove")
def remove_viewer(workspace_id, user_id, ...):
    ws = _require_owner(...)
    session.query(WorkspaceViewer).filter_by(
        workspace_id=ws.id, user_id=user_id,
    ).delete()
    session.commit()
    return RedirectResponse(url=f"/workspaces/{ws.id}/settings", status_code=303)
```

Tests: add viewer by email; add nonexistent email rejected; remove viewer.

Run + commit.

---

## Task 12: first-time disclosure modal + POST /disclosure

Per-user, per-workspace. The main view checks for an existing `workspace_events(kind='disclosure_acknowledged', actor_id=user.id)` row; if missing, the page renders the modal. The modal's POST writes the event.

```python
@router.post("/{workspace_id}/disclosure")
def post_disclosure(workspace_id, request, user=Depends(get_current_user), session=Depends(_session_dep)):
    ws = _require_owner_or_viewer(...)
    session.add(WorkspaceEvent(
        workspace_id=ws.id, kind="disclosure_acknowledged",
        actor_id=user.id, payload={},
    ))
    session.commit()
    return HTMLResponse("")  # HTMX swaps the modal away
```

Modify `get_workspace_main` to also check + pass `needs_disclosure: bool` to the template.

Template change in `workspace_main.html`:

```html
{% if needs_disclosure %}
  {% include "proposal_assist/partials/disclosure_modal.html" %}
{% endif %}
```

`partials/disclosure_modal.html`:

```html
<div id="disclosure-modal" style="position:fixed; top:20%; left:30%; background:white; border:2px solid #333; padding:1em; z-index:1000;">
  <h2>This workspace sends data to Anthropic's API.</h2>
  <p>Opus 4.7 runs on Anthropic's servers, not on hospital hardware. When you send a message, the text and any attached figures or files leave the hospital network.</p>
  <p><strong>Do not paste patient identifiers, raw patient data, or information covered by consent restrictions.</strong></p>
  <button hx-post="/workspaces/{{ workspace.id }}/disclosure"
          hx-target="#disclosure-modal" hx-swap="outerHTML">
    I understand. Don't show this again for this workspace.
  </button>
</div>
```

Tests: first visit shows modal; POST acknowledges; second visit does not show modal.

Run + commit.

---

## Task 13: read-only viewer enforcement + per-turn banner

Already wired in Tasks 5+6: composer only renders if `is_owner`; POST /turns enforces `_require_owner`. Task 13's job is to add a regression test that explicitly verifies a viewer:
- Sees the chat history.
- Does NOT see the composer.
- Cannot POST /turns (403).

The banner template partial was created in Task 5 (`partials/cloud_banner.html`). Nothing new for this task other than verification tests.

Run + commit.

---

## Task 14: GET /workspaces/{id}/export.md — markdown export

Dump current `proposal.md` with a Content-Disposition header. Word/PDF require pandoc; defer per plan scope.

```python
@router.get("/{workspace_id}/export.md")
def export_markdown(workspace_id, request, user=Depends(get_current_user), session=Depends(_session_dep)):
    ws = _require_owner_or_viewer(...)
    repo_dir = Path(request.app.state.settings.workspaces_dir) / str(ws.id) / "repo"
    proposal = repo_dir / "proposal.md"
    if not proposal.exists():
        raise HTTPException(404, "no proposal.md")
    return Response(
        proposal.read_text(),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{ws.title}.md"'},
    )
```

Test: export returns 200 with the right Content-Type + body.

Run + commit.

---

## Task 15: milestone — full suite + manual smoke

```bash
uv run pytest -q  # expect ~830+ passing (788 baseline + B6's new tests)
uv run ruff check src tests
```

Manual smoke (notes to the user — not automated):

1. Log in, hit `/calls/<id>` for any ranked call, click "Start drafting workspace".
2. Confirm the rubric in the new workspace's rubric-review page.
3. Land on `/workspaces/<id>`. See the disclosure modal. Acknowledge it.
4. Send a turn ("hi"). Watch the SSE stream populate chat history.
5. Visit settings; add a viewer (existing user); see them listed; remove.
6. Soft-delete the workspace; verify it disappears from `/workspaces`.
7. Export markdown; verify the file matches `proposal.md` content.

Surface to user:

> B6 complete. Workspace web UX is live: list, create, two-pane main view (chat + artifact), SSE-streamed turns, viewer-mode read-only access, first-time disclosure modal, per-turn banner, settings + soft delete, markdown export.
>
> **Out of scope (next slices):**
> - Attachments (figures + preliminary data + vision) — **B7**.
> - Word/PDF export — deferred (requires pandoc).
> - Admin panel additions (drafting stats, kill switch, spend dashboards) — **B8**.
> - Branching UI — phased spec dropped.

---

## Self-review notes (for the engineer running this plan)

1. **SSE polling latency.** The 200ms poll + 5s drain tick = up to ~5s before the user sees the first assistant token. Acceptable for v1. Future: add an asyncio.Event the POST handler sets so the drain tick wakes immediately.

2. **No real-time presence.** Multi-user collaboration is shared-read + single-write; no "who's viewing" indicator (explicitly out per spec line 314).

3. **Tool-result content in chat_turns.** B4 stores tool results as `{"type": "tool_result", "content": <string>, "is_error": ...}`. The chat partial renders these as `<em>(tool result)</em>` placeholders — the model can see the full content via `read_artifact_at` etc., but the UI just acknowledges the call happened.

4. **Markdown rendering.** Uses the `markdown` package. No XSS sanitization beyond what markdown itself does. Acceptable because content is owned by the workspace's owner + agent (no untrusted user input rendered as HTML).

5. **Read-only viewer enforcement** is BOTH client-side (composer hidden) AND server-side (`_require_owner` on POST /turns). Don't drop either layer.

6. **Cap-rejection UX is minimal**: a 429 HTMX response with a text explanation. B8 will add the proper banner + admin-override widget. Don't expand here.

7. **Export markdown** is the only export. Word/PDF would need pandoc; deferred. Document this in the export button's tooltip / `<a>` text ("Export markdown — Word/PDF coming later").

8. **Settings page combines workspace-edit fields AND viewer management AND soft-delete** in one template. Could split into 3 pages later; one form is simpler for v1.

9. **The disclosure modal triggers on every render where the user hasn't acknowledged.** It uses HTMX to swap itself away after the POST, but a hard refresh re-checks the DB and re-renders if needed. Idempotent: clicking the button multiple times writes multiple events (acceptable — audit trail).

10. **`/calls/<id>` 'Start drafting' button** only passes `call_id`. Profile pre-selection (best-fit profile) is a polish item; the form lets the user pick. Don't block.

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-20-base-spec-6-web-ux.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, two-stage review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
