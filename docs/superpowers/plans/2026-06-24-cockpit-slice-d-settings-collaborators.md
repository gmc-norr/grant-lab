# Cockpit Slice D — Settings + Collaborators Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring workspace settings (title/status/deadline/citation-style) and collaborator management (add/remove members) into the React cockpit, and fix the chat-footer gate-message conflation from Slice B — all additive, no DB schema change, zero Jinja routes touched.

**Architecture:** A new owner-gated `/api` module `web/api/settings.py` (settings GET/PATCH + members POST/DELETE) that **reuses the Jinja validation constants/parser inline** (`_WORKSPACE_STATUSES`, `_CITATION_STYLES`, `_parse_deadline_form`, `_MAX_WORKSPACE_TITLE_LEN`) and the existing `require_owner` guard — no new shared helper. The existing owner-gated `MemberOut`/`list_members` read-model gains `user_id` (additive). `TurnsResponse` gains `is_owner` + `disabled_reason`. The Activity feed gains a `member` category (owner-only visibility — the member-readable feed must not leak owner-gated collaborator emails). Frontend: `api/settings.ts` hooks, a `WorkspaceSettingsModal` (owner-only gear in the TitleBar), an actionable Members sidebar group, and a footer switch on `disabled_reason`.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2 / Postgres (testcontainers) / `uv` / `ruff` / pytest · React 19 + Vite + react-router-dom 7 + TanStack Query 5 + Mantine v9 + Vitest + Testing Library · `openapi-fetch` against `schema.gen.ts`.

## Global Constraints

- **No DB schema change**; alembic head untouched (no migration — `workspaces`, `workspace_viewers`, `workspace_events` all already exist).
- **Touch ZERO Jinja routes; extract NO new shared helper.** Reuse the Jinja module constants/`_parse_deadline_form` + `require_owner` directly and inline the orchestration (mirroring the Jinja `post_settings`/`add_viewer`/`remove_viewer`). The Jinja settings/viewer routes are deleted at Slice G; the short-lived duplication is intentional.
- **New `/api` module** `src/call_manager/web/api/settings.py` mounted via `web/api/__init__.py` (after `files`). Do NOT append to `workspaces.py` (except the additive `MemberOut.user_id` in Task 1).
- **Permissions mirror Jinja:** settings GET/PATCH + member POST/DELETE are **owner-gated** (`require_owner`).
- **Owner-only Activity PII invariant (mandatory, tested):** `member_added`/`member_removed` events store the collaborator email in `payload`, but the member-gated `/activity` endpoint must **exclude** these kinds for non-owner viewers. Collaborator emails stay owner-only (parity with the owner-gated `/api/members`).
- **400 envelope code is `bad_request`** (`web/api/errors.py:57` maps `400 → "bad_request"`), NOT `invalid`. Tests assert `400` / `bad_request`.
- **OpenAPI regen requires `EMBEDDING_MODEL=bge-m3`**; additive diff only. **No `routes.gen.ts` change** (all in-cockpit; no new SPA route).
- **Backend tests:** targeted files only (the whole suite drops the testcontainers Postgres under load). `ruff check` + `ruff format` touched files.
- **Frontend:** Node 22 + `pnpm`; `pnpm exec tsc --noEmit` is the gate; `pnpm vitest run <file>`; `pnpm build`.

## Reuse map (exact signatures — call these; do not reimplement)

- `require_owner(*, workspace_id: uuid.UUID, user, session) -> Workspace` — `proposal_assist/web/_perms.py:17` (404 if missing, 403 `"owner only"` if not owner; filters `soft_deleted_at IS NULL`).
- From `proposal_assist/web/workspaces.py` (module-level): `_WORKSPACE_STATUSES` (`frozenset({"drafting","submitted","awarded","rejected","abandoned"})`), `_CITATION_STYLES`, `_MAX_WORKSPACE_TITLE_LEN` (`500`), `_parse_deadline_form(deadline: str | None) -> datetime | None` (raises `HTTPException(400)` on malformed; `None`/`""` → `None`).
- `Workspace` columns: `title`, `status`, `deadline` (`DateTime(tz)`), `citation_style`, `current_phase`, `owner_id`. `WorkspaceViewer` (`workspace_id`, `user_id`, `added_at`). `WorkspaceEvent` (`workspace_id`, `kind`, `payload` JSONB, `actor_id` nullable, `created_at`). `User` (`id`, `email`).
- `web/api/workspaces.py:859` `MemberOut` + `:864` `list_members` (owner-gated; owner first then viewers ordered by email).
- `web/api/turns.py:145` `list_turns` builds `TurnsResponse`; `is_owner = ws.owner_id == user.id`, `drafting_on = request.app.state.settings.drafting_enabled`, `can_send = bool(is_owner and ws.current_phase=="active" and drafting_on)`, `needs_disclosure = prior_ack is None`.
- `proposal_assist/activity/feed.py`: `_RENDER: dict[str, tuple[category, summary, target]]` (:63), `build_activity_feed(events, actor_names)` (:144); `ActivityRow` carries `category`/`summary`. `proposal_assist/web/activity.py:54` `get_activity` (member-gated; `ActivityRowOut.category` is a closed `Literal`, :39).
- `/api` idioms: `from call_manager.auth.dependencies import get_current_user`; `with request.app.state.session_factory() as session`; handlers `raise HTTPException` (global handler builds `{error:{code,message}}`).
- Frontend: `api`, `unwrap` from `./client` (call `unwrap(await api.GET/POST/PATCH/DELETE(...))`); mutation pattern = `useMutation({ mutationFn: async (v) => unwrap(await api.X(...)), onSuccess/onSettled: () => qc.invalidateQueries({queryKey}) })` (see `api/references.ts`, `api/turns.ts`). Modal precedent: `cockpit/MetadataModal.tsx` (Mantine `Modal opened/onClose` + `Button` calling `mutate(..., {onSuccess: onClose})`).

## Test harness note (backend)

`tests/test_api_settings.py` (new) uses the same helpers as `tests/test_api_files.py`: `from tests.proposal_assist._refs_helpers import add_member, authed_client, build_app` and `from tests.proposal_assist.conftest import make_user, make_workspace`. `build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)`, `authed_client(app, user_id=...)`, `make_workspace(s, owner=user)`, `add_member(s, workspace_id=ws.id, user=member)`. `tests/test_api_turns.py` (existing) uses `_app/_client/_seed` from `tests.test_api_workspaces` + the local `_ack_disclosure` helper.

---

## Task 1: `MemberOut.user_id` (additive)

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py` (`MemberOut` :859, `list_members` :883-886)
- Test: `tests/test_api_workspaces.py` (append)

**Interfaces:**
- Produces: `MemberOut { user_id: int, email: str, role: str }` (additive `user_id`); `list_members` populates it for the owner row and every viewer. Consumed by Task 7/9 (sidebar remove) + Task 3 (member POST return).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_workspaces.py` (it already has a members test + the `_app`/`_client`/`_seed` harness; mirror the existing members test's setup to add a viewer, then assert `user_id` is present and correct). Add:

```python
def test_members_list_includes_user_id(monkeypatch, pg_url, pg_session_factory, tmp_path):
    from call_manager.db.models import User, WorkspaceViewer

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "x\n")
    with pg_session_factory() as s:
        viewer = User(email="viewer-d1@ki.se")
        s.add(viewer)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws_id, user_id=viewer.id))
        s.commit()
        viewer_id = viewer.id
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/members")
    assert r.status_code == 200, r.text
    rows = r.json()
    by_email = {m["email"]: m for m in rows}
    assert by_email["viewer-d1@ki.se"]["user_id"] == viewer_id
    assert all("user_id" in m for m in rows)
```

> If `User(email=...)` requires more fields in this codebase, mirror however the existing `test_api_workspaces.py`/`make_user` builds a user (check the file first). `_seed`'s owner email is whatever the harness uses — assert on the viewer you create.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_workspaces.py::test_members_list_includes_user_id -x -q`
Expected: FAIL — `KeyError: 'user_id'` (field absent).

- [ ] **Step 3: Implement — add `user_id` to `MemberOut` + `list_members`**

In `src/call_manager/web/api/workspaces.py`, change `MemberOut`:

```python
class MemberOut(BaseModel):
    user_id: int
    email: str
    role: str  # "owner" | "member"
```

And in `list_members`, the two `out.append`/`out.extend` lines:

```python
        out: list[MemberOut] = []
        if owner is not None:
            out.append(MemberOut(user_id=owner.id, email=owner.email, role="owner"))
        out.extend(MemberOut(user_id=v.id, email=v.email, role="member") for v in viewers)
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_workspaces.py::test_members_list_includes_user_id -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py
uv run ruff format src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py
git add src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py
git commit -m "feat(api): add user_id to MemberOut (additive) for the cockpit remove control"
```

---

## Task 2: `web/api/settings.py` — settings GET/PATCH + mount

**Files:**
- Create: `src/call_manager/web/api/settings.py`
- Modify: `src/call_manager/web/api/__init__.py` (import + mount)
- Test: `tests/test_api_settings.py` (create)

**Interfaces:**
- Produces: `GET /api/workspaces/{workspace_id}/settings` (owner) → `SettingsOut{title, status, deadline, citation_style}`; `PATCH .../settings` (owner, body `SettingsUpdateIn{title, status, deadline, citation_style}`) → updated `SettingsOut`. `deadline` is a `YYYY-MM-DD` string or `null`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_settings.py`:

```python
"""JSON /api workspace settings + collaborator endpoints (Cockpit Slice D)."""

import uuid

from sqlalchemy import func, select

from call_manager.db.models import Attachment  # noqa: F401  (ensures models import)
from tests.proposal_assist._refs_helpers import add_member, authed_client, build_app
from tests.proposal_assist.conftest import make_user, make_workspace


def _setup(pg_session_factory):
    """Return (ws_id, owner_id, member_id, stranger_id). member is a viewer."""
    with pg_session_factory() as s:
        owner = make_user(s, email="owner@ki.se")
        member = make_user(s, email="member@ki.se")
        stranger = make_user(s, email="stranger@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        add_member(s, workspace_id=ws.id, user=member)
        return ws.id, owner.id, member.id, stranger.id


def test_settings_get_owner_returns_fields(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).get(f"/api/workspaces/{ws_id}/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"title", "status", "deadline", "citation_style"}


def test_settings_get_member_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=member_id).get(f"/api/workspaces/{ws_id}/settings")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_settings_patch_owner_updates(monkeypatch, pg_url, tmp_path, pg_session_factory):
    from call_manager.db.models import Workspace

    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).patch(
        f"/api/workspaces/{ws_id}/settings",
        json={
            "title": "Renamed",
            "status": "submitted",
            "deadline": "2026-09-01",
            "citation_style": "author-year",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Renamed"
    assert r.json()["deadline"] == "2026-09-01"
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        assert ws.title == "Renamed" and ws.status == "submitted"
        assert ws.citation_style == "author-year" and ws.deadline is not None


def test_settings_patch_clears_deadline(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    c = authed_client(app, user_id=owner_id)
    c.patch(
        f"/api/workspaces/{ws_id}/settings",
        json={"title": "T", "status": "drafting", "deadline": "2026-09-01", "citation_style": "vancouver"},
    )
    r = c.patch(
        f"/api/workspaces/{ws_id}/settings",
        json={"title": "T", "status": "drafting", "deadline": None, "citation_style": "vancouver"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["deadline"] is None


def test_settings_patch_bad_status_400(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).patch(
        f"/api/workspaces/{ws_id}/settings",
        json={"title": "T", "status": "bogus", "deadline": None, "citation_style": "vancouver"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_settings_patch_bad_citation_400(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).patch(
        f"/api/workspaces/{ws_id}/settings",
        json={"title": "T", "status": "drafting", "deadline": None, "citation_style": "mla"},
    )
    assert r.status_code == 400


def test_settings_patch_empty_title_400(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).patch(
        f"/api/workspaces/{ws_id}/settings",
        json={"title": "   ", "status": "drafting", "deadline": None, "citation_style": "vancouver"},
    )
    assert r.status_code == 400


def test_settings_patch_member_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=member_id).patch(
        f"/api/workspaces/{ws_id}/settings",
        json={"title": "T", "status": "drafting", "deadline": None, "citation_style": "vancouver"},
    )
    assert r.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_settings.py -x -q`
Expected: FAIL — 404 (routes not mounted).

- [ ] **Step 3: Implement — create the module**

Create `src/call_manager/web/api/settings.py`:

```python
"""JSON /api surface for workspace settings + collaborators (Cockpit Slice D).

OWNER-GATED. Reuses the Jinja validation constants/parser (_WORKSPACE_STATUSES,
_CITATION_STYLES, _parse_deadline_form, _MAX_WORKSPACE_TITLE_LEN) and the
require_owner guard, inlining the same orchestration the Jinja post_settings /
add_viewer / remove_viewer handlers run. No new shared helper; the Jinja routes
are deleted at Slice G. No DB schema change.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import User, WorkspaceEvent, WorkspaceViewer
from call_manager.proposal_assist.web._perms import require_owner
from call_manager.proposal_assist.web.workspaces import (
    _CITATION_STYLES,
    _MAX_WORKSPACE_TITLE_LEN,
    _WORKSPACE_STATUSES,
    _parse_deadline_form,
)
from call_manager.web.api.workspaces import MemberOut

router = APIRouter()


class SettingsOut(BaseModel):
    title: str
    status: str
    deadline: str | None
    citation_style: str


class SettingsUpdateIn(BaseModel):
    title: str
    status: str
    deadline: str | None = None
    citation_style: str


def _settings_out(ws) -> SettingsOut:
    return SettingsOut(
        title=ws.title,
        status=ws.status,
        deadline=ws.deadline.date().isoformat() if ws.deadline else None,
        citation_style=ws.citation_style,
    )


@router.get("/workspaces/{workspace_id}/settings", response_model=SettingsOut)
def get_settings(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SettingsOut:
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        return _settings_out(ws)


@router.patch("/workspaces/{workspace_id}/settings", response_model=SettingsOut)
def update_settings(
    workspace_id: uuid.UUID,
    body: SettingsUpdateIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SettingsOut:
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        if body.status not in _WORKSPACE_STATUSES:
            raise HTTPException(status_code=400, detail="invalid status")
        if body.citation_style not in _CITATION_STYLES:
            raise HTTPException(status_code=400, detail="invalid citation_style")
        title = body.title.strip()
        if not title or len(title) > _MAX_WORKSPACE_TITLE_LEN:
            raise HTTPException(status_code=400, detail="invalid title")
        ws.title = title
        ws.status = body.status
        ws.deadline = _parse_deadline_form(body.deadline)  # HTTPException(400) on malformed
        ws.citation_style = body.citation_style
        session.commit()
        return _settings_out(ws)
```

- [ ] **Step 4: Mount the router**

In `src/call_manager/web/api/__init__.py`, add `settings` to the import and include it after `files`:

```python
from call_manager.web.api import calls, files, me, references, settings, turns, workspaces
```
```python
router.include_router(files.router)
router.include_router(settings.router)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_api_settings.py -q`
Expected: PASS (8 settings tests).

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check src/call_manager/web/api/settings.py src/call_manager/web/api/__init__.py tests/test_api_settings.py
uv run ruff format src/call_manager/web/api/settings.py src/call_manager/web/api/__init__.py tests/test_api_settings.py
git add src/call_manager/web/api/settings.py src/call_manager/web/api/__init__.py tests/test_api_settings.py
git commit -m "feat(api): owner-gated workspace settings GET/PATCH reusing Jinja validation"
```

---

## Task 3: members POST/DELETE + audit events

**Files:**
- Modify: `src/call_manager/web/api/settings.py` (append two routes + `AddMemberIn`)
- Test: `tests/test_api_settings.py` (append)

**Interfaces:**
- Produces: `POST /api/workspaces/{workspace_id}/members` (owner, body `{email}`) → `MemberOut` (201 new / 200 idempotent owner|duplicate); writes `WorkspaceEvent(kind="member_added")`. `DELETE .../members/{user_id}` (owner) → **204** idempotent; writes `WorkspaceEvent(kind="member_removed")` only when a row was deleted.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_settings.py`:

```python
def _add_user(pg_session_factory, email):
    with pg_session_factory() as s:
        u = make_user(s, email=email)
        s.commit()
        return u.id


def test_members_add_owner_201_and_event(monkeypatch, pg_url, tmp_path, pg_session_factory):
    from call_manager.db.models import WorkspaceEvent, WorkspaceViewer

    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    new_id = _add_user(pg_session_factory, "newcollab@ki.se")
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/members", json={"email": "newcollab@ki.se"}
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body == {"user_id": new_id, "email": "newcollab@ki.se", "role": "member"}
    with pg_session_factory() as s:
        assert s.scalar(
            select(func.count())
            .select_from(WorkspaceViewer)
            .where(WorkspaceViewer.workspace_id == ws_id, WorkspaceViewer.user_id == new_id)
        ) == 1
        ev = s.scalar(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id, WorkspaceEvent.kind == "member_added"
            )
        )
        assert ev is not None and ev.actor_id == owner_id
        assert ev.payload["target_user_id"] == new_id
        assert ev.payload["target_email"] == "newcollab@ki.se"


def test_members_add_unknown_email_400_generic(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/members", json={"email": "nobody@ki.se"}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_members_add_duplicate_idempotent_200(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, member_id, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/members", json={"email": "member@ki.se"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == member_id


def test_members_add_owner_email_idempotent_200(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/members", json={"email": "owner@ki.se"}
    )
    assert r.status_code == 200
    assert r.json()["role"] == "owner"


def test_members_add_non_owner_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=member_id).post(
        f"/api/workspaces/{ws_id}/members", json={"email": "member@ki.se"}
    )
    assert r.status_code == 403


def test_members_delete_owner_204_and_event(monkeypatch, pg_url, tmp_path, pg_session_factory):
    from call_manager.db.models import WorkspaceEvent, WorkspaceViewer

    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, member_id, _ = _setup(pg_session_factory)
    c = authed_client(app, user_id=owner_id)
    r = c.delete(f"/api/workspaces/{ws_id}/members/{member_id}")
    assert r.status_code == 204
    with pg_session_factory() as s:
        assert s.scalar(
            select(func.count())
            .select_from(WorkspaceViewer)
            .where(WorkspaceViewer.workspace_id == ws_id, WorkspaceViewer.user_id == member_id)
        ) == 0
        ev = s.scalar(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id, WorkspaceEvent.kind == "member_removed"
            )
        )
        assert ev is not None and ev.payload["target_user_id"] == member_id
    # idempotent: deleting again is still 204, writes no second event
    assert c.delete(f"/api/workspaces/{ws_id}/members/{member_id}").status_code == 204
    with pg_session_factory() as s:
        assert s.scalar(
            select(func.count())
            .select_from(WorkspaceEvent)
            .where(WorkspaceEvent.workspace_id == ws_id, WorkspaceEvent.kind == "member_removed")
        ) == 1


def test_members_delete_non_owner_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, stranger_id = _setup(pg_session_factory)
    r = authed_client(app, user_id=stranger_id).delete(f"/api/workspaces/{ws_id}/members/{member_id}")
    assert r.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_settings.py -k "members" -x -q`
Expected: FAIL — 404/405 (routes absent).

- [ ] **Step 3: Implement — append the two routes**

Append to `src/call_manager/web/api/settings.py` (add `AddMemberIn` near the other models):

```python
class AddMemberIn(BaseModel):
    email: str


@router.post("/workspaces/{workspace_id}/members", response_model=MemberOut, status_code=201)
def add_member(
    workspace_id: uuid.UUID,
    body: AddMemberIn,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),  # noqa: B008
) -> MemberOut:
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        target = session.scalar(select(User).where(User.email == body.email))
        # Generic failure (no "no such user" oracle), mirroring the Jinja add_viewer.
        if target is None:
            raise HTTPException(status_code=400, detail="could not add collaborator")
        if target.id == ws.owner_id:
            response.status_code = 200  # owner already has full access — no-op
            return MemberOut(user_id=target.id, email=target.email, role="owner")
        existing = (
            session.query(WorkspaceViewer)
            .filter_by(workspace_id=ws.id, user_id=target.id)
            .first()
        )
        if existing is not None:
            response.status_code = 200  # idempotent
            return MemberOut(user_id=target.id, email=target.email, role="member")
        session.add(WorkspaceViewer(workspace_id=ws.id, user_id=target.id))
        session.add(
            WorkspaceEvent(
                workspace_id=ws.id,
                kind="member_added",
                actor_id=user.id,
                payload={"target_user_id": target.id, "target_email": target.email},
            )
        )
        session.commit()
        return MemberOut(user_id=target.id, email=target.email, role="member")


@router.delete("/workspaces/{workspace_id}/members/{user_id}", status_code=204)
def remove_member(
    workspace_id: uuid.UUID,
    user_id: int,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> Response:
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        target = session.get(User, user_id)  # resolve email for the audit payload
        deleted = (
            session.query(WorkspaceViewer)
            .filter_by(workspace_id=ws.id, user_id=user_id)
            .delete()
        )
        if deleted:
            session.add(
                WorkspaceEvent(
                    workspace_id=ws.id,
                    kind="member_removed",
                    actor_id=user.id,
                    payload={
                        "target_user_id": user_id,
                        "target_email": target.email if target else None,
                    },
                )
            )
        session.commit()
    return Response(status_code=204)
```

> `body.email` is matched exactly (no lowercasing), mirroring the Jinja `add_viewer`. Emails are stored lowercase (DB CHECK), so a mixed-case input won't match → generic 400; this is parity, not a new behavior.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_settings.py -q`
Expected: PASS (all settings + members tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/call_manager/web/api/settings.py tests/test_api_settings.py
uv run ruff format src/call_manager/web/api/settings.py tests/test_api_settings.py
git add src/call_manager/web/api/settings.py tests/test_api_settings.py
git commit -m "feat(api): add/remove collaborators (owner-gated) with member_added/removed audit"
```

---

## Task 4: `TurnsResponse` — `is_owner` + `disabled_reason`

**Files:**
- Modify: `src/call_manager/web/api/turns.py` (`TurnsResponse` :137, `list_turns` :178-187)
- Test: `tests/test_api_turns.py` (append)

**Interfaces:**
- Produces: `TurnsResponse` gains `is_owner: bool` + `disabled_reason: str | None` (`"not_owner"|"setup"|"drafting_disabled"|"needs_disclosure"|null`), computed in canonical order. `can_send` unchanged. Consumed by Task 10 (footer).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_turns.py` (reuses `_app`/`_client`/`_seed` + the module-level `_ack_disclosure`):

```python
def test_turns_disabled_reason_branches(monkeypatch, pg_url, pg_session_factory, tmp_path):
    from call_manager.db.models import Workspace

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, _, ws_id = _seed(app, pg_session_factory, tmp_path, "x\n")

    # not_owner: a member sees not_owner
    r = _client(app, member_id).get(f"/api/workspaces/{ws_id}/turns")
    assert r.json()["disabled_reason"] == "not_owner"
    assert r.json()["is_owner"] is False

    # setup: owner on a setup-phase workspace
    with pg_session_factory() as s:
        s.get(Workspace, ws_id).current_phase = "setup"
        s.commit()
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/turns")
    assert r.json()["disabled_reason"] == "setup"
    assert r.json()["is_owner"] is True

    # active again
    with pg_session_factory() as s:
        s.get(Workspace, ws_id).current_phase = "active"
        s.commit()

    # drafting_disabled: global kill-switch off (owner, active)
    app.state.settings.drafting_enabled = False
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/turns")
    assert r.json()["disabled_reason"] == "drafting_disabled"
    app.state.settings.drafting_enabled = True

    # needs_disclosure: owner, active, drafting on, no ack yet
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/turns")
    assert r.json()["disabled_reason"] == "needs_disclosure"

    # null: after acking disclosure
    _ack_disclosure(pg_session_factory, ws_id, owner_id)
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/turns")
    assert r.json()["disabled_reason"] is None
    assert r.json()["can_send"] is True
```

> Confirm `_app` exposes `app.state.settings` (it does — `list_turns` reads `request.app.state.settings.drafting_enabled`). If `_seed`'s second return isn't the member id, adjust to however the harness exposes a non-owner member (check `_seed`'s signature in the file).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_turns.py::test_turns_disabled_reason_branches -x -q`
Expected: FAIL — `disabled_reason`/`is_owner` absent from the response.

- [ ] **Step 3: Implement — extend the model + handler**

In `src/call_manager/web/api/turns.py`, add two fields to `TurnsResponse`:

```python
class TurnsResponse(BaseModel):
    turns: list[TurnOut]
    can_send: bool
    is_owner: bool
    disabled_reason: str | None
    needs_disclosure: bool
    phase: str
    running_job_id: str | None
```

In `list_turns`, replace the `is_owner`/`can_send`/`return` tail with:

```python
        is_owner = ws.owner_id == user.id
        drafting_on = request.app.state.settings.drafting_enabled
        can_send = bool(is_owner and ws.current_phase == "active" and drafting_on)
        needs_disclosure = prior_ack is None
        # Canonical order mirrors gate_cloud_llm (setup -> drafting -> disclosure),
        # with not_owner prepended (submit is owner-only). disabled_reason is the
        # authoritative footer driver; can_send is retained for back-compat.
        if not is_owner:
            disabled_reason: str | None = "not_owner"
        elif ws.current_phase == "setup":
            disabled_reason = "setup"
        elif not drafting_on:
            disabled_reason = "drafting_disabled"
        elif needs_disclosure:
            disabled_reason = "needs_disclosure"
        else:
            disabled_reason = None
        return TurnsResponse(
            turns=[TurnOut(**_turn_to_dict(t)) for t in rows],
            can_send=can_send,
            is_owner=is_owner,
            disabled_reason=disabled_reason,
            needs_disclosure=needs_disclosure,
            phase=ws.current_phase,
            running_job_id=str(running) if running else None,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_turns.py -k "disabled_reason or attachment or queues or 403 or 409" -q`
Expected: PASS (new test + existing turns tests unaffected).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/call_manager/web/api/turns.py tests/test_api_turns.py
uv run ruff format src/call_manager/web/api/turns.py tests/test_api_turns.py
git add src/call_manager/web/api/turns.py tests/test_api_turns.py
git commit -m "feat(api): TurnsResponse is_owner + disabled_reason (fixes footer gate conflation)"
```

---

## Task 5: Activity feed — `member` category, email render, owner-only filter

**Files:**
- Modify: `src/call_manager/proposal_assist/activity/feed.py` (`_RENDER` :63, `build_activity_feed` render branch :172)
- Modify: `src/call_manager/proposal_assist/web/activity.py` (`ActivityRowOut.category` :39, `get_activity` :54)
- Test: `tests/proposal_assist/test_activity_feed.py` (append unit) + `tests/proposal_assist/` activity-endpoint test (append PII test — find the existing activity endpoint test file and append there; if none, create `tests/test_api_activity_members.py` using the `build_app` harness)

**Interfaces:**
- Produces: `member_added`/`member_removed` render as category `"member"` with summary `"added collaborator <email>"` / `"removed collaborator <email>"`; the `/activity` endpoint excludes these kinds for non-owner viewers.

- [ ] **Step 1: Write the failing tests**

Append a feed unit test to `tests/proposal_assist/test_activity_feed.py` (match the file's existing event-builder helper; the pattern below constructs a `WorkspaceEvent` directly — adapt to the file's fixture style):

```python
def test_member_added_renders_under_member_category_with_email():
    from call_manager.db.models import WorkspaceEvent
    from call_manager.proposal_assist.activity.feed import build_activity_feed

    ev = WorkspaceEvent(
        workspace_id=__import__("uuid").uuid4(),
        kind="member_added",
        actor_id=1,
        payload={"target_user_id": 2, "target_email": "alice@ki.se"},
    )
    # created_at + id are normally DB-assigned; set them for the pure builder.
    import datetime as _dt
    import uuid as _uuid

    ev.id = _uuid.uuid4()
    ev.created_at = _dt.datetime(2026, 6, 24, tzinfo=_dt.timezone.utc)
    rows = build_activity_feed([ev], {1: "owner@ki.se"})
    assert len(rows) == 1
    assert rows[0].category == "member"
    assert "alice@ki.se" in rows[0].summary
    assert rows[0].actor == "owner@ki.se"
```

For the PII test, append to the existing activity-endpoint test (search `tests/` for the file that hits `GET /api/workspaces/{id}/activity`; it is the one importing/using `build_app` or `_app` and asserting on `/activity`). If you must create one, use the `build_app` harness:

```python
def test_activity_excludes_member_events_for_non_owner(monkeypatch, pg_url, tmp_path, pg_session_factory):
    from call_manager.db.models import WorkspaceEvent
    from tests.proposal_assist._refs_helpers import add_member, authed_client, build_app
    from tests.proposal_assist.conftest import make_user, make_workspace

    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    with pg_session_factory() as s:
        owner = make_user(s, email="o@ki.se")
        member = make_user(s, email="m@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        add_member(s, workspace_id=ws.id, user=member)
        s.add(WorkspaceEvent(
            workspace_id=ws.id, kind="member_added", actor_id=owner.id,
            payload={"target_user_id": member.id, "target_email": "m@ki.se"},
        ))
        s.commit()
        ws_id, owner_id, member_id = ws.id, owner.id, member.id

    owner_items = authed_client(app, user_id=owner_id).get(f"/api/workspaces/{ws_id}/activity").json()["items"]
    assert any(it["category"] == "member" and "m@ki.se" in it["summary"] for it in owner_items)

    member_items = authed_client(app, user_id=member_id).get(f"/api/workspaces/{ws_id}/activity").json()["items"]
    assert all(it["category"] != "member" for it in member_items)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/proposal_assist/test_activity_feed.py -k member -x -q` (and the PII test file)
Expected: FAIL — `member_added` is an unmapped kind (dropped), so no row / no `member` category.

- [ ] **Step 3a: Implement — feed render**

In `src/call_manager/proposal_assist/activity/feed.py`, add two `_RENDER` entries (alongside the others):

```python
    "member_added": ("member", "added collaborator", None),
    "member_removed": ("member", "removed collaborator", None),
```

And in `build_activity_feed`, in the generic-render branch, after `category, summary, target = spec` and before building the `ActivityRow`, append the email for these kinds:

```python
        category, summary, target = spec
        if e.kind in ("member_added", "member_removed"):
            email = (e.payload or {}).get("target_email")
            if email:
                summary = f"{summary} {email}"
        rows.append(
            ActivityRow(
                id=str(e.id),
                category=category,
                actor=_resolve_actor(e, actor_names),
                summary=summary,
                count=1,
                created_at=e.created_at.isoformat(),
                target=target,
            )
        )
        i += 1
```

- [ ] **Step 3b: Implement — response category + owner-only filter**

In `src/call_manager/proposal_assist/web/activity.py`, add `"member"` to the `ActivityRowOut.category` Literal:

```python
    category: Literal[
        "created", "edit", "citation", "turn", "review",
        "uncited", "dismissal", "reference", "rubric", "member",
    ]
```

And in `get_activity`, capture the workspace from the guard and exclude the collaborator kinds in SQL for non-owners (so the LIMIT still yields up to `_ACTIVITY_WINDOW` visible rows):

```python
    ws = require_member(workspace_id=workspace_id, user=user, session=session)
    is_owner = ws.owner_id == user.id
    q = select(WorkspaceEvent).where(WorkspaceEvent.workspace_id == workspace_id)
    if not is_owner:
        q = q.where(WorkspaceEvent.kind.notin_(("member_added", "member_removed")))
    events = list(
        session.scalars(
            q.order_by(WorkspaceEvent.created_at.desc(), WorkspaceEvent.id.desc()).limit(
                _ACTIVITY_WINDOW
            )
        )
    )
```

> Keep the rest of `get_activity` (actor-name resolution + `build_activity_feed` + `return {"items": ...}`) unchanged. `require_member` already returns the `Workspace`; just capture it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/proposal_assist/test_activity_feed.py -k member tests/test_api_activity_members.py -q` (adjust paths to where you placed the tests)
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/call_manager/proposal_assist/activity/feed.py src/call_manager/proposal_assist/web/activity.py
uv run ruff format src/call_manager/proposal_assist/activity/feed.py src/call_manager/proposal_assist/web/activity.py
git add src/call_manager/proposal_assist/activity/feed.py src/call_manager/proposal_assist/web/activity.py tests/
git commit -m "feat(activity): owner-only member_added/removed rows (member category, email render)"
```

---

## Task 6: Regenerate the OpenAPI → TypeScript schema

**Files:**
- Modify: `frontend/src/api/schema.gen.ts` (generated)

**Interfaces:**
- Produces: TS types for `SettingsOut`, `SettingsUpdateIn`, `AddMemberIn`, the new `/api/workspaces/{workspace_id}/settings` + `/members*` paths, `MemberOut.user_id`, `TurnsResponse.is_owner`/`disabled_reason`, and `ActivityRowOut.category` `"member"`. Consumed by Tasks 7-11.

- [ ] **Step 1: Regenerate (mandatory env prefix)**

```bash
EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh
```

- [ ] **Step 2: Verify additive + no routes.gen.ts churn**

```bash
git diff --stat frontend/src/api/schema.gen.ts
git diff frontend/src/api/schema.gen.ts | grep '^-' | grep -v '^---' | grep -v '^-//' || echo "NO removed content lines"
git diff --stat frontend/src/api/routes.gen.ts && echo "routes.gen.ts unchanged check above (expect empty)"
```
Expected: `schema.gen.ts` additions only (new settings/members paths + schemas + the added fields/enum value). Any "removed" line must be an openapi-typescript positional relocation — confirm the symbol still exists elsewhere before proceeding. `routes.gen.ts` empty diff.

- [ ] **Step 3: Type-check + commit**

```bash
cd frontend && pnpm exec tsc --noEmit
cd .. && git add frontend/src/api/schema.gen.ts
git commit -m "chore(api): regen OpenAPI TS types for settings + collaborators"
```

> Unlike the C-media regen, the new fields are all on RESPONSE models (`is_owner`/`disabled_reason`/`user_id`) or new endpoints — existing code reading those responses still type-checks (new fields appearing is non-breaking). If `tsc` nonetheless fails, fix the minimal consumer and note it; do not widen scope.

---

## Task 7: Frontend hooks (`api/settings.ts`)

**Files:**
- Create: `frontend/src/api/settings.ts`
- Test: `frontend/src/api/settings.test.tsx`

**Interfaces:**
- Produces: `useSettings(id)` (query `["settings", id]`, `retry:false`), `useUpdateSettings(id)` (PATCH → invalidates `["settings", id]` + `["cockpit-meta", id]`), `useAddMember(id)` (POST `{email}` → invalidates `["members", id]` + `["cockpit-meta", id]`), `useRemoveMember(id)` (DELETE by `user_id` → same invalidations). Consumed by Tasks 8-9.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/api/settings.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import * as clientMod from "./client";
import { useAddMember, useSettings } from "./settings";

afterEach(() => { vi.restoreAllMocks(); });

function wrap() {
  const qc = new QueryClient();
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

it("useSettings fetches the settings", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { title: "T", status: "drafting", deadline: null, citation_style: "vancouver" },
    error: undefined,
  } as never);
  const { result } = renderHook(() => useSettings("ws-1"), { wrapper: wrap() });
  await waitFor(() => expect(result.current.data?.title).toBe("T"));
});

it("useAddMember POSTs the email", async () => {
  const spy = vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue({ data: { user_id: 2, email: "a@ki.se", role: "member" }, error: undefined } as never);
  const { result } = renderHook(() => useAddMember("ws-1"), { wrapper: wrap() });
  result.current.mutate("a@ki.se");
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(spy).toHaveBeenCalled();
});
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `frontend/`): `pnpm vitest run src/api/settings.test.tsx`
Expected: FAIL — `./settings` has no exports.

- [ ] **Step 3: Implement — create `frontend/src/api/settings.ts`**

```ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";

/** GET /api/workspaces/{id}/settings — OWNER-GATED. Only the owner opens the
 *  settings modal; a non-owner would 403, so do not retry. */
export function useSettings(id: string) {
  return useQuery({
    queryKey: ["settings", id],
    enabled: !!id,
    retry: false,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/settings", {
          params: { path: { workspace_id: id } },
        }),
      ),
  });
}

export function useUpdateSettings(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (vars: {
      title: string;
      status: string;
      deadline: string | null;
      citation_style: string;
    }) =>
      unwrap(
        await api.PATCH("/api/workspaces/{workspace_id}/settings", {
          params: { path: { workspace_id: id } },
          body: vars,
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["settings", id] });
      qc.invalidateQueries({ queryKey: ["cockpit-meta", id] });
    },
  });
}

export function useAddMember(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (email: string) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/members", {
          params: { path: { workspace_id: id } },
          body: { email },
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["members", id] });
      qc.invalidateQueries({ queryKey: ["cockpit-meta", id] });
    },
  });
}

export function useRemoveMember(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (userId: number) =>
      unwrap(
        await api.DELETE("/api/workspaces/{workspace_id}/members/{user_id}", {
          params: { path: { workspace_id: id, user_id: userId } },
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["members", id] });
      qc.invalidateQueries({ queryKey: ["cockpit-meta", id] });
    },
  });
}
```

- [ ] **Step 4: Run test + type-check**

```bash
pnpm vitest run src/api/settings.test.tsx
pnpm exec tsc --noEmit
```
Expected: PASS + exit 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/settings.ts frontend/src/api/settings.test.tsx
git commit -m "feat(spa): settings + collaborator hooks (useSettings/useUpdateSettings/useAddMember/useRemoveMember)"
```

---

## Task 8: `WorkspaceSettingsModal` + owner-only TitleBar gear

**Files:**
- Create: `frontend/src/cockpit/WorkspaceSettingsModal.tsx`
- Modify: `frontend/src/shell/TitleBar.tsx` (owner-only gear + modal state)
- Test: `frontend/src/cockpit/WorkspaceSettingsModal.test.tsx`

**Interfaces:**
- Consumes: `useSettings`, `useUpdateSettings` (Task 7); `useCockpitMeta` (`role`).
- Produces: a Mantine `Modal` editing title/status/deadline/citation_style; opened by a gear button that the TitleBar renders only when `meta.role === "owner"`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/cockpit/WorkspaceSettingsModal.test.tsx`:

```tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkspaceSettingsModal } from "./WorkspaceSettingsModal";

const h = vi.hoisted(() => ({ update: vi.fn(), settings: vi.fn() }));
vi.mock("../api/settings", () => ({
  useSettings: () => h.settings(),
  useUpdateSettings: () => ({ mutate: h.update, isPending: false, isError: false }),
}));

beforeEach(() => {
  h.update.mockReset();
  h.settings.mockReturnValue({
    data: { title: "Proj", status: "drafting", deadline: "2026-09-01", citation_style: "vancouver" },
  });
});
afterEach(() => { vi.restoreAllMocks(); });

describe("WorkspaceSettingsModal", () => {
  it("renders current values and saves a PATCH", async () => {
    render(<WorkspaceSettingsModal id="ws1" opened onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByLabelText(/title/i)).toHaveValue("Proj"));
    fireEvent.change(screen.getByLabelText(/title/i), { target: { value: "Renamed" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    expect(h.update).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Renamed", citation_style: "vancouver" }),
      expect.anything(),
    );
  });
});
```

> Mantine `Modal` renders into a portal; `@testing-library` queries find portal content via `screen`. If the existing test setup needs a `MantineProvider` wrapper for portals/styles, mirror whatever `MetadataModal`'s test (if any) or other Mantine component tests use — check `frontend/src/test/` setup. Mantine `Select` renders an input with the label as its accessible name.

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm vitest run src/cockpit/WorkspaceSettingsModal.test.tsx`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement the modal**

Create `frontend/src/cockpit/WorkspaceSettingsModal.tsx`:

```tsx
import { Button, Modal, Select, TextInput } from "@mantine/core";
import { useEffect, useState } from "react";

import { useSettings, useUpdateSettings } from "../api/settings";

const STATUSES = ["drafting", "submitted", "awarded", "rejected", "abandoned"];
const CITATION_STYLES = ["vancouver", "author-year"];

export function WorkspaceSettingsModal({
  id,
  opened,
  onClose,
}: {
  id: string;
  opened: boolean;
  onClose: () => void;
}) {
  const settings = useSettings(id);
  const update = useUpdateSettings(id);
  const [title, setTitle] = useState("");
  const [status, setStatus] = useState("drafting");
  const [deadline, setDeadline] = useState("");
  const [citationStyle, setCitationStyle] = useState("vancouver");

  // Populate from the loaded settings (and re-sync if they change / modal reopens).
  useEffect(() => {
    const d = settings.data;
    if (!d) return;
    setTitle(d.title);
    setStatus(d.status);
    setDeadline(d.deadline ?? "");
    setCitationStyle(d.citation_style);
  }, [settings.data, opened]);

  return (
    <Modal opened={opened} onClose={onClose} title="Workspace settings">
      <TextInput label="Title" value={title} onChange={(e) => setTitle(e.currentTarget.value)} />
      <Select
        label="Status"
        data={STATUSES}
        value={status}
        onChange={(v) => setStatus(v ?? "drafting")}
        allowDeselect={false}
      />
      <TextInput
        label="Deadline"
        type="date"
        value={deadline}
        onChange={(e) => setDeadline(e.currentTarget.value)}
      />
      <Select
        label="Citation style"
        data={CITATION_STYLES}
        value={citationStyle}
        onChange={(v) => setCitationStyle(v ?? "vancouver")}
        allowDeselect={false}
      />
      {update.isError ? <p className="library-error">Could not save settings.</p> : null}
      <Button
        disabled={update.isPending}
        onClick={() =>
          update.mutate(
            {
              title,
              status,
              deadline: deadline === "" ? null : deadline,
              citation_style: citationStyle,
            },
            { onSuccess: onClose },
          )
        }
      >
        Save
      </Button>
    </Modal>
  );
}
```

- [ ] **Step 4: Implement the TitleBar gear (owner-only)**

In `frontend/src/shell/TitleBar.tsx`, read the role from `meta`, hold modal open-state, and render an owner-only gear next to the title. Read the live file first; the change is: keep `const meta = useCockpitMeta(id).data;`, add `useState` import + the modal, and render the gear when `id && meta?.role === "owner"`. Reference shape:

```tsx
import { useState } from "react";
// ...existing imports...
import { WorkspaceSettingsModal } from "../cockpit/WorkspaceSettingsModal";

export function TitleBar() {
  // ...existing pathname/id/meta/title logic unchanged...
  const [settingsOpen, setSettingsOpen] = useState(false);
  const isOwner = !!id && meta?.role === "owner";
  return (
    <header className={styles.titlebar}>
      <span className={styles.brand}>
        <BrandMark size={18} />
        <span className={styles.app}>Call Manager</span>
      </span>
      <span className={styles.title}>{title}</span>
      {isOwner && (
        <button
          type="button"
          className={styles.settingsGear}
          aria-label="Workspace settings"
          onClick={() => setSettingsOpen(true)}
        >
          ⚙
        </button>
      )}
      {isOwner && (
        <WorkspaceSettingsModal id={id} opened={settingsOpen} onClose={() => setSettingsOpen(false)} />
      )}
    </header>
  );
}
```

> Add a `.settingsGear` class to `TitleBar.module.css` (small, transparent button, pushed to the right — e.g. `margin-left:auto; background:none; border:0; cursor:pointer; color:var(--app-text); font-size:14px;`). Keep the existing `title` element; if the layout already uses `margin-left:auto` somewhere, place the gear so it sits at the right without disturbing the title.

- [ ] **Step 5: Run test + type-check**

```bash
pnpm vitest run src/cockpit/WorkspaceSettingsModal.test.tsx
pnpm exec tsc --noEmit
```
Expected: PASS + exit 0. (If any existing TitleBar test exists, run it too: `pnpm vitest run src/shell/`.)

- [ ] **Step 6: Commit**

```bash
git add frontend/src/cockpit/WorkspaceSettingsModal.tsx frontend/src/cockpit/WorkspaceSettingsModal.test.tsx frontend/src/shell/TitleBar.tsx frontend/src/shell/TitleBar.module.css
git commit -m "feat(spa): workspace settings modal + owner-only TitleBar gear"
```

---

## Task 9: Members sidebar group — actionable add/remove (owner)

**Files:**
- Modify: `frontend/src/cockpit/CockpitSidebar.tsx` (Members group)
- Modify: `frontend/src/cockpit/CockpitSidebar.module.css` (small controls) — only if new classes are needed
- Test: `frontend/src/cockpit/CockpitSidebar.test.tsx` (append or create)

**Interfaces:**
- Consumes: `useAddMember`, `useRemoveMember` (Task 7); the existing `useMembers` (now returns `user_id`).
- Produces: when the owner's `memberRows` is present, each viewer row (`role === "member"`) gets a confirm-gated remove control wired to `useRemoveMember(member.user_id)` (the owner row has none), plus an add-by-email input + button. Non-owners unchanged.

- [ ] **Step 1: Write the failing test**

Append to (or create) `frontend/src/cockpit/CockpitSidebar.test.tsx`. Read the live file first to match its existing mock setup (it mocks the `../api/workspace` + `../api/editor` + `../api/citations` + `../api/evaluator` hooks). Add a `../api/settings` mock and a test:

```tsx
it("owner can remove a viewer (confirm-gated) from the Members group", () => {
  // h.members returns rows incl. user_id; meta role owner; settings hooks mocked.
  h.members.mockReturnValue({
    data: [
      { user_id: 1, email: "owner@ki.se", role: "owner" },
      { user_id: 2, email: "viewer@ki.se", role: "member" },
    ],
  });
  const remove = vi.fn();
  h.removeMember.mockReturnValue({ mutate: remove, isPending: false });
  vi.spyOn(window, "confirm").mockReturnValue(true);
  renderSidebar(); // however the file renders CockpitSidebar within the router/QueryClient
  fireEvent.click(screen.getByRole("button", { name: /remove viewer@ki\.se/i }));
  expect(remove).toHaveBeenCalledWith(2);
});
```

> Match the existing test file's mock-hoisting style. You must add to the hoisted mock object: `members`, `removeMember`, `addMember` (plus seed defaults in `beforeEach`), and a `vi.mock("../api/settings", () => ({ useAddMember: () => h.addMember(), useRemoveMember: () => h.removeMember() }))`. The owner row (`role==="owner"`) must NOT render a remove button — add an assertion `expect(screen.queryByRole("button", { name: /remove owner@ki\.se/i })).toBeNull();` if convenient.

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm vitest run src/cockpit/CockpitSidebar.test.tsx`
Expected: FAIL — no remove button.

- [ ] **Step 3: Implement — make the Members group actionable**

In `frontend/src/cockpit/CockpitSidebar.tsx`: import the hooks + `useState`, and replace the Members group body. Read the live file; the Members `<SidebarGroup>` currently renders `memberRows.map(...)` as static `<li>`s. Change to add a remove button per viewer + an add row:

```tsx
import { useState } from "react";
import { useAddMember, useRemoveMember } from "../api/settings";
// ...inside CockpitSidebar, near the other hooks:
const addMember = useAddMember(id);
const removeMember = useRemoveMember(id);
const [newEmail, setNewEmail] = useState("");
```

Members group body (when `memberRows` present):

```tsx
      <SidebarGroup title="Members" count={memberCount} defaultOpen={false}>
        {memberRows ? (
          <>
            <ul className={styles.outline} aria-label="Members">
              {memberRows.map((m) => (
                <li key={m.email} className={styles.metaRow}>
                  <span className={styles.rowLabel}>{m.email}</span>
                  <span className={styles.metaSub}>{m.role}</span>
                  {m.role === "member" && (
                    <button
                      type="button"
                      className={styles.memberRemove}
                      aria-label={`Remove ${m.email}`}
                      disabled={removeMember.isPending}
                      onClick={() => {
                        if (window.confirm(`Remove ${m.email}?`)) removeMember.mutate(m.user_id);
                      }}
                    >
                      ×
                    </button>
                  )}
                </li>
              ))}
            </ul>
            <div className={styles.memberAdd}>
              <input
                aria-label="Add collaborator by email"
                value={newEmail}
                onChange={(e) => setNewEmail(e.target.value)}
                placeholder="email…"
              />
              <button
                type="button"
                disabled={addMember.isPending || !newEmail.trim()}
                onClick={() =>
                  addMember.mutate(newEmail.trim(), { onSuccess: () => setNewEmail("") })
                }
              >
                Add
              </button>
            </div>
            {addMember.isError && <p className={styles.note}>Could not add collaborator.</p>}
          </>
        ) : (
          <p className={styles.note}>
            {memberCount != null ? `${memberCount} member${memberCount === 1 ? "" : "s"}` : "—"}
          </p>
        )}
      </SidebarGroup>
```

> Add `.memberRemove` / `.memberAdd` classes to `CockpitSidebar.module.css` if absent (minimal: small inline controls). Keep the non-owner branch (`memberCount` fallback) unchanged.

- [ ] **Step 4: Run test + type-check**

```bash
pnpm vitest run src/cockpit/CockpitSidebar.test.tsx
pnpm exec tsc --noEmit
```
Expected: PASS + exit 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/cockpit/CockpitSidebar.tsx frontend/src/cockpit/CockpitSidebar.module.css frontend/src/cockpit/CockpitSidebar.test.tsx
git commit -m "feat(spa): owner add/remove collaborators in the Members sidebar group"
```

---

## Task 10: Chat footer — switch on `disabled_reason`

**Files:**
- Modify: `frontend/src/cockpit/chat/CockpitChat.tsx` (footer branch chain)
- Test: `frontend/src/cockpit/chat/CockpitChat.test.tsx` (append)

**Interfaces:**
- Consumes: `TurnsResponse.disabled_reason` (Task 4/6).
- Produces: the footer renders a message keyed by `disabled_reason`; `null` → `Composer`; `needs_disclosure` → the existing disclosure-ack UI.

- [ ] **Step 1: Write the failing test**

Append to `frontend/src/cockpit/chat/CockpitChat.test.tsx` (read the live file to match its existing `useTurns` mock). Add a test that mocks `useTurns` returning `disabled_reason: "drafting_disabled"` and asserts "Drafting is currently disabled." renders, and one with `disabled_reason: null` (+ `can_send: true`) asserting the Composer (the message is absent / the textarea is present):

```tsx
it("footer shows the drafting-disabled message via disabled_reason", () => {
  h.turns.mockReturnValue({
    data: {
      turns: [], can_send: false, is_owner: true, disabled_reason: "drafting_disabled",
      needs_disclosure: false, phase: "active", running_job_id: null,
    },
    isLoading: false,
  });
  renderChat();
  expect(screen.getByText(/drafting is currently disabled/i)).toBeInTheDocument();
});
```

> Match the existing CockpitChat test's mock shape exactly (it already stubs `useTurns`/`useReferences`/`useAckDisclosure`/`useEventStream`). Add `is_owner`/`disabled_reason` to whatever default `turns.data` the existing tests use so they keep passing.

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm vitest run src/cockpit/chat/CockpitChat.test.tsx`
Expected: FAIL — current footer keys off `can_send`/`phase`, not `disabled_reason`.

- [ ] **Step 3: Implement — switch on `disabled_reason`**

In `frontend/src/cockpit/chat/CockpitChat.tsx`, replace the footer branch chain. Add a defensive fallback so a stale cached payload (pre-regen) still resolves a reason from the old fields:

```tsx
      {d && (
        <footer className={styles.footer}>
          {(() => {
            const reason =
              d.disabled_reason ??
              (!d.can_send ? "not_owner" : d.needs_disclosure ? "needs_disclosure" : null);
            if (reason === "not_owner")
              return <p className={styles.note}>Read-only — only the owner can message the agent.</p>;
            if (reason === "setup")
              return (
                <p className={styles.note}>
                  Finish setup — review the rubric in the legacy workspace before drafting.
                </p>
              );
            if (reason === "drafting_disabled")
              return <p className={styles.note}>Drafting is currently disabled.</p>;
            if (reason === "needs_disclosure")
              return (
                <div className={styles.disclosure}>
                  <p>
                    Proposals are generated by a hosted model. Don&apos;t paste patient-identifiable
                    data.
                  </p>
                  <button type="button" disabled={ack.isPending} onClick={() => { ack.mutate(); }}>
                    I understand
                  </button>
                </div>
              );
            return (
              <Composer
                id={id}
                refs={refs}
                disabled={!!jobId || !!d.running_job_id}
                onSubmitted={setJobId}
              />
            );
          })()}
        </footer>
      )}
```

> Preserve the exact existing message strings + the disclosure block markup/handler. Only the branching changes (from `can_send`/`phase` inference to the `disabled_reason` switch).

- [ ] **Step 4: Run test + type-check**

```bash
pnpm vitest run src/cockpit/chat/CockpitChat.test.tsx
pnpm exec tsc --noEmit
```
Expected: PASS + exit 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/cockpit/chat/CockpitChat.tsx frontend/src/cockpit/chat/CockpitChat.test.tsx
git commit -m "feat(spa): chat footer keys off disabled_reason (fixes owner drafting-disabled message)"
```

---

## Task 11: ActivityPanel — `member` category styling + render test

**Files:**
- Modify: the activity CSS (find where `.category-reference` / `.category-*` rules live — likely `frontend/src/cockpit/ActivityPanel.css` or a global cockpit stylesheet)
- Test: `frontend/src/cockpit/ActivityPanel.test.tsx` (append)

**Interfaces:**
- Produces: a `.category-member` style + a test confirming a `member`-category row renders. `ActivityPanel` already renders `category-${r.category}` generically, so NO component logic change is needed.

- [ ] **Step 1: Write the failing test**

Append to `frontend/src/cockpit/ActivityPanel.test.tsx` (match the existing `useActivity` mock). Add:

```tsx
it("renders a member-category row", () => {
  h.activity.mockReturnValue({
    data: {
      items: [
        { id: "e1", category: "member", actor: "owner@ki.se", summary: "added collaborator a@ki.se", count: 1, created_at: new Date().toISOString(), target: null },
      ],
    },
    isLoading: false,
    isError: false,
  });
  renderPanel(); // however the file renders ActivityPanel
  expect(screen.getByText(/added collaborator a@ki\.se/i)).toBeInTheDocument();
  expect(document.querySelector(".category-member")).not.toBeNull();
});
```

> Match the existing ActivityPanel test's render + mock style (it mocks `../api/activity`'s `useActivity` and passes `onNavigate`). Reuse its helpers.

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm vitest run src/cockpit/ActivityPanel.test.tsx`
Expected: FAIL initially only if the mock/category wiring is new; if the generic render already makes it pass, ADD the CSS class first so the `.category-member` assertion is meaningful, then confirm green. (If it passes immediately because the render is generic, that's fine — the test still guards the contract; ensure the CSS class exists.)

- [ ] **Step 3: Implement — add the CSS class**

Find the existing `.category-reference { … }` (or equivalent per-category) rule and add a sibling:

```css
.category-member { /* match the visual treatment of the other categories */ }
```

(Use the same property pattern the neighboring `.category-*` rules use — e.g. a left-border/accent color. If categories are otherwise unstyled, a no-op rule is acceptable; the point is the class exists for future styling and the test asserts the contract.)

- [ ] **Step 4: Run test + type-check + build**

```bash
pnpm vitest run src/cockpit/ActivityPanel.test.tsx
pnpm exec tsc --noEmit
pnpm build
```
Expected: PASS + exit 0 + build exit 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/cockpit/ActivityPanel.test.tsx frontend/src/cockpit/ActivityPanel.css
git commit -m "feat(spa): activity member-category styling + render test"
```

---

## Final verification (before the whole-branch review)

- [ ] Backend (targeted): `uv run pytest tests/test_api_settings.py tests/test_api_turns.py tests/test_api_workspaces.py -q` — all green.
- [ ] Activity: `uv run pytest tests/proposal_assist/test_activity_feed.py <activity-endpoint-test> -q` — green (incl. the PII test).
- [ ] Regression — Jinja settings/viewer/activity routes UNCHANGED: `uv run pytest tests/proposal_assist/ -k "settings or viewer or activity" -q`.
- [ ] Lint: `uv run ruff check src/call_manager/web/api/ src/call_manager/proposal_assist/activity/ src/call_manager/proposal_assist/web/activity.py` — clean.
- [ ] Frontend: `cd frontend && pnpm exec tsc --noEmit && pnpm vitest run && pnpm build` — tsc 0, all vitest green, build exit 0.
- [ ] Schema additive: `git diff --stat <merge-base> -- frontend/src/api/schema.gen.ts` additions only; `frontend/src/api/routes.gen.ts` EMPTY diff branch-wide.
- [ ] Alembic head unchanged (no migration). No `proposal_assist/web/` route handler edited (only `proposal_assist/web/activity.py` get_activity owner-filter + the imported constants — confirm `workspaces.py` Jinja handlers untouched).

## Self-review notes (spec coverage)

- Spec §4.1/4.2 (settings GET/PATCH) → Task 2. §4.3/4.4 (members POST/DELETE + audit) → Task 3. §4 MemberOut.user_id → Task 1. §4.5 (TurnsResponse) → Task 4. §7 (activity member category + owner-only PII) → Task 5. §6 frontend hooks → Task 7. §6 modal + gear → Task 8. §6 sidebar actionable → Task 9. §6 footer switch → Task 10. §7/§8 frontend activity render → Task 11. §7 regen → Task 6.
- Permissions: settings + member writes `require_owner` (Tasks 2-3 tests cover owner-200/201/204 + member/stranger-403). Owner-only Activity PII invariant tested in Task 5.
- Out of scope (per spec §2): per-workspace drafting toggle, hard delete/archive, settings-edit audit, role changes/invites.
