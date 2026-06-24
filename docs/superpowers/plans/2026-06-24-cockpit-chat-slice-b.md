# Cockpit Chat (Slice B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an interactive, SSE-streamed agent chat to the React cockpit as a persistent right pane, reusing the existing propose-only turn engine via a new JSON/SSE `/api` surface; user turns produce proposals that flow into the existing cockpit Inbox.

**Architecture:** Additive `/api` JSON+SSE endpoints (`web/api/turns.py`) that delegate to the existing `submit_user_turn` / turn-stream / disclosure logic (no business-logic duplication, no schema change). A right-pane shell region (`handle.RightPane`, mirroring Phase-3a's `handle.Sidebar`) renders `CockpitChat`, which reads history via React Query and live turns via the existing `useEventStream`. Proposals land in the already-built Inbox.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2 / Postgres (testcontainers) / `uv` / `ruff`; React 19 / Vite / TanStack Query 5 / `openapi-fetch` / Vitest; SSE via `text/event-stream` + browser `EventSource`.

## Global Constraints

- **Source spec:** `docs/superpowers/specs/2026-06-24-cockpit-chat-slice-design.md` (Codex-revised).
- **No schema change.** Reuse `chat_turns`, `turn_jobs`, `workspace_events`. Alembic head stays unchanged.
- **Gate order is canonical and must not be reordered:** owner → setup(409) → drafting(503) → disclosure(403) → caps(429). The setup/drafting/disclosure trio is enforced by the shared `gate_cloud_llm()` (`proposal_assist/web/_cloud_gate.py`), reused — not re-copied.
- **Error envelope:** raised `HTTPException(status, detail=...)` on an `/api` path is auto-wrapped by the app handler into `{error:{code,message}}` where `code` = the generic status name (409→`conflict`, 403→`forbidden`, …). The frontend gates **proactively from the read-model flags** (`phase`/`needs_disclosure`/`can_send`); submit 4xx are backstops (show message + refetch the read-model).
- **SSE resume contract:** the stream emits an `id: <position>` line on every `turn` event and honors `?since=<position>`.
- **v1 scope:** text + reference inclusions only. **No image upload** (composer shows a disabled, labeled image affordance). Workspace list/create (Slice A), rubric gate (Slice E), Jinja teardown (Slice G) are out of scope.
- **Backend tests are TARGETED** (`uv run pytest tests/test_api_turns.py::<id> -v`) — never the whole suite (testcontainers flakes under load). Docker must be running.
- **OpenAPI regen:** `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh` from the repo/worktree root (the env prefix avoids the mxbai WARNING corrupting `schema.gen.ts`).
- **Frontend gate is `tsc`** (no eslint): `pnpm exec tsc --noEmit`; tests `pnpm vitest run <file>`; build `pnpm build`. Node 22 via nvm + pnpm.
- **editorStore hygiene:** any test mounting cockpit components calls `resetEditorStore()` + `vi.restoreAllMocks()` in `afterEach`.

---

## File Structure

**Backend (new):**
- `src/call_manager/web/api/turns.py` — the JSON+SSE turns router (submit, history read-model, stream, disclosure). Mounted under `/api`.
- `tests/test_api_turns.py` — endpoint + stream-generator tests (reuses `_app`/`_seed`/`_client` from `tests/test_api_workspaces.py`).

**Backend (modify):**
- `src/call_manager/web/api/__init__.py` — `router.include_router(turns.router)`.

**Frontend (new):**
- `frontend/src/api/turns.ts` — `useTurns`, `useSubmitTurn`, `useAckDisclosure` hooks + `turnStreamUrl(id, jobId)`.
- `frontend/src/cockpit/chat/CockpitChat.tsx` — the `handle.RightPane` region (header, transcript, footer-gate, live stream wiring).
- `frontend/src/cockpit/chat/TurnList.tsx` — transcript renderer (`TurnItem` typed dispatch).
- `frontend/src/cockpit/chat/Composer.tsx` — textarea + reference picker + send.
- `frontend/src/cockpit/chat/CockpitChat.module.css` — chat styling (`--app-*` tokens).
- `frontend/src/cockpit/chat/*.test.tsx` — per-component vitest.

**Frontend (modify):**
- `frontend/src/shell/AppShell.tsx` — add a `RightPane` shell region (mirrors `Sidebar`).
- `frontend/src/router.tsx` — add `RightPane: CockpitChat` to the cockpit route handle.
- `frontend/src/api/events.ts` — reset the `lastId` cursor when `url` changes (hardening).
- `frontend/src/api/schema.gen.ts` — regenerated (additive).

---

## Task 1: JSON submit endpoint (the integration spike)

The riskiest integration — driving the existing propose-only engine through a new `/api` JSON endpoint — done first. If this task's tests pass, the core path is proven.

**Files:**
- Create: `src/call_manager/web/api/turns.py`
- Modify: `src/call_manager/web/api/__init__.py`
- Test: `tests/test_api_turns.py`

**Interfaces:**
- Consumes: `require_owner(workspace_id=, user=, session=) -> Workspace` (`proposal_assist.web._perms`); `gate_cloud_llm(*, ws, request, session, user, disclosure_action)` (`proposal_assist.web._cloud_gate`); `submit_user_turn(*, session, workspace_id, content_text, reference_ids, submitted_by_user_id, settings, workspaces_dir) -> TurnJob` (`proposal_assist.turns.submit`); `TokenCapExceeded`, `GlobalTokenCapExceeded` (`proposal_assist.turns.caps`); `get_current_user` (`auth.dependencies`); `ChatTurn`, `TurnJob` (`db.models`).
- Produces: `POST /api/workspaces/{workspace_id}/turns` → `SubmitTurnResponse { job_id: str, user_turn: TurnOut }`; the helper `_turn_to_dict(turn) -> dict`; Pydantic models `TurnOut`, `SubmitTurnIn`, `SubmitTurnResponse` reused by later tasks.

- [ ] **Step 1: Write the failing tests** — create `tests/test_api_turns.py`:

```python
"""Cockpit chat JSON/SSE turns surface (Slice B). Reuses the workspace test
harness (_app/_seed/_client) so the app, session factory, and session cookies
match the rest of the /api suite."""

from __future__ import annotations

import uuid

from call_manager.db.models import ChatTurn, TurnJob, WorkspaceEvent, Workspace
from tests.test_api_workspaces import _app, _client, _seed


def _ack_disclosure(pg_session_factory, ws_id, user_id):
    with pg_session_factory() as s:
        s.add(WorkspaceEvent(workspace_id=ws_id, kind="disclosure_acknowledged", actor_id=user_id))
        s.commit()


def test_submit_turn_queues_job_and_echoes(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    _ack_disclosure(pg_session_factory, ws_id, owner_id)
    r = _client(app, owner_id).post(f"/api/workspaces/{ws_id}/turns", json={"content": "Draft Methods"})
    assert r.status_code == 200
    body = r.json()
    assert body["user_turn"]["role"] == "user"
    assert "job_id" in body
    with pg_session_factory() as s:
        job = s.get(TurnJob, uuid.UUID(body["job_id"]))
        assert job is not None and job.status == "queued"
        ut = s.get(ChatTurn, uuid.UUID(body["user_turn"]["id"]))
        assert ut is not None and ut.role == "user"


def test_submit_turn_non_owner_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, member_id, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    _ack_disclosure(pg_session_factory, ws_id, member_id)
    r = _client(app, member_id).post(f"/api/workspaces/{ws_id}/turns", json={"content": "hi"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_submit_turn_setup_phase_409(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    _ack_disclosure(pg_session_factory, ws_id, owner_id)
    with pg_session_factory() as s:
        s.get(Workspace, ws_id).current_phase = "setup"
        s.commit()
    r = _client(app, owner_id).post(f"/api/workspaces/{ws_id}/turns", json={"content": "hi"})
    assert r.status_code == 409  # setup beats disclosure (canonical order)


def test_submit_turn_needs_disclosure_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    r = _client(app, owner_id).post(f"/api/workspaces/{ws_id}/turns", json={"content": "hi"})
    assert r.status_code == 403  # no disclosure ack, but phase=active so it's the disclosure gate


def test_submit_turn_token_cap_429(monkeypatch, pg_url, pg_session_factory, tmp_path):
    from call_manager.proposal_assist.turns.caps import TokenCapExceeded

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    _ack_disclosure(pg_session_factory, ws_id, owner_id)

    def _boom(**kwargs):
        raise TokenCapExceeded("daily cap")

    monkeypatch.setattr("call_manager.web.api.turns.submit_user_turn", _boom)
    r = _client(app, owner_id).post(f"/api/workspaces/{ws_id}/turns", json={"content": "hi"})
    assert r.status_code == 429
```

- [ ] **Step 2: Run — Expected: FAIL** (404 — router not yet mounted).

Run: `uv run pytest tests/test_api_turns.py -k submit -v`

- [ ] **Step 3: Implement** — create `src/call_manager/web/api/turns.py`:

```python
"""Cockpit chat JSON/SSE turns surface (Slice B). Thin /api layer over the
existing propose-only turn engine: submit reuses submit_user_turn + the shared
cloud gate; the stream is the JSON analogue of the Jinja SSE bridge. No new
business logic, no schema change."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import ChatTurn, TurnJob, User
from call_manager.proposal_assist.turns.caps import GlobalTokenCapExceeded, TokenCapExceeded
from call_manager.proposal_assist.turns.submit import submit_user_turn
from call_manager.proposal_assist.web._cloud_gate import gate_cloud_llm
from call_manager.proposal_assist.web._perms import require_owner

router = APIRouter()

_DISCLOSURE_ACTION = "sending a message to the drafting assistant"


class TurnOut(BaseModel):
    id: str
    role: str
    content: Any  # JSONB: str | {type:text,...} | list[block]
    created_at: str
    position: int


class SubmitTurnIn(BaseModel):
    content: str
    reference_ids: list[str] = []


class SubmitTurnResponse(BaseModel):
    job_id: str
    user_turn: TurnOut


def _turn_to_dict(turn: ChatTurn) -> dict:
    return {
        "id": str(turn.id),
        "role": turn.role,
        "content": turn.content,
        "created_at": turn.created_at.isoformat(),
        "position": turn.position,
    }


@router.post("/workspaces/{workspace_id}/turns", response_model=SubmitTurnResponse)
def submit_turn(
    workspace_id: uuid.UUID,
    body: SubmitTurnIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SubmitTurnResponse:
    """Enqueue a user turn (owner-only). Canonical gate order: owner →
    setup/drafting/disclosure (shared gate_cloud_llm) → caps."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        gate_cloud_llm(
            ws=ws, request=request, session=session, user=user,
            disclosure_action=_DISCLOSURE_ACTION,
        )
        try:
            job = submit_user_turn(
                session=session,
                workspace_id=workspace_id,
                content_text=body.content,
                reference_ids=[uuid.UUID(r) for r in body.reference_ids],
                submitted_by_user_id=user.id,
                settings=settings,
                workspaces_dir=Path(settings.workspaces_dir),
            )
        except (TokenCapExceeded, GlobalTokenCapExceeded) as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except ValueError as exc:  # cross-workspace reference id
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        session.commit()
        session.refresh(job)
        user_turn = session.get(ChatTurn, job.user_turn_id)
        return SubmitTurnResponse(job_id=str(job.id), user_turn=TurnOut(**_turn_to_dict(user_turn)))
```

Then mount it in `src/call_manager/web/api/__init__.py` — add `turns` to the import and `router.include_router(turns.router)`:

```python
from call_manager.web.api import calls, me, turns, workspaces
# ...
router.include_router(workspaces.router)
router.include_router(turns.router)
```

- [ ] **Step 4: Run — Expected: PASS**

Run: `uv run pytest tests/test_api_turns.py -k submit -v`

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/call_manager/web/api/turns.py src/call_manager/web/api/__init__.py tests/test_api_turns.py
uv run ruff format src/call_manager/web/api/turns.py tests/test_api_turns.py
git add src/call_manager/web/api/turns.py src/call_manager/web/api/__init__.py tests/test_api_turns.py
git commit -m "feat(api): POST /workspaces/{id}/turns JSON submit (reuses propose-only engine)"
```

---

## Task 2: Turn-history read-model

**Files:**
- Modify: `src/call_manager/web/api/turns.py`
- Test: `tests/test_api_turns.py`

**Interfaces:**
- Consumes: `require_member(workspace_id=, user=, session=) -> Workspace`; `WorkspaceEvent`, `Workspace` (`db.models`); `_turn_to_dict`, `TurnOut` (Task 1).
- Produces: `GET /api/workspaces/{workspace_id}/turns` → `TurnsResponse { turns: list[TurnOut], can_send: bool, needs_disclosure: bool, phase: str, running_job_id: str | None }`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_api_turns.py`:

```python
def test_turns_read_model_owner(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/turns")
    assert r.status_code == 200
    b = r.json()
    assert b["phase"] == "active"
    assert b["needs_disclosure"] is True         # no ack yet
    assert b["can_send"] is True                  # owner + active + drafting_enabled
    assert b["turns"] == []
    assert b["running_job_id"] is None


def test_turns_read_model_member_cannot_send(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, member_id, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    b = _client(app, member_id).get(f"/api/workspaces/{ws_id}/turns").json()
    assert b["can_send"] is False                 # viewer


def test_turns_read_model_non_member_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, _, stranger_id, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    r = _client(app, stranger_id).get(f"/api/workspaces/{ws_id}/turns")
    assert r.status_code == 403
```

- [ ] **Step 2: Run — Expected: FAIL** (404, no route).

Run: `uv run pytest tests/test_api_turns.py -k read_model -v`

- [ ] **Step 3: Implement** — append to `src/call_manager/web/api/turns.py` (add `from sqlalchemy import select` and `WorkspaceEvent` + `require_member` to the imports):

```python
class TurnsResponse(BaseModel):
    turns: list[TurnOut]
    can_send: bool
    needs_disclosure: bool
    phase: str
    running_job_id: str | None


@router.get("/workspaces/{workspace_id}/turns", response_model=TurnsResponse)
def list_turns(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> TurnsResponse:
    """Chat read-model. The frontend gates the composer proactively from these
    flags (mirrors the submit gate order), so a 4xx is only ever a race backstop."""
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_member(workspace_id=workspace_id, user=user, session=session)
        rows = list(
            session.scalars(
                select(ChatTurn)
                .where(ChatTurn.workspace_id == workspace_id)
                .order_by(ChatTurn.position)
            )
        )
        prior_ack = session.scalar(
            select(WorkspaceEvent.id).where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.kind == "disclosure_acknowledged",
                WorkspaceEvent.actor_id == user.id,
            ).limit(1)
        )
        running = session.scalar(
            select(TurnJob.id)
            .where(TurnJob.workspace_id == workspace_id, TurnJob.status.in_(("queued", "running")))
            .order_by(TurnJob.queued_at.desc())
            .limit(1)
        )
        is_owner = ws.owner_id == user.id
        can_send = bool(is_owner and ws.current_phase == "active" and request.app.state.settings.drafting_enabled)
        return TurnsResponse(
            turns=[TurnOut(**_turn_to_dict(t)) for t in rows],
            can_send=can_send,
            needs_disclosure=prior_ack is None,
            phase=ws.current_phase,
            running_job_id=str(running) if running else None,
        )
```

Update the import line in `turns.py`:
```python
from call_manager.proposal_assist.web._perms import require_member, require_owner
from sqlalchemy import select
from call_manager.db.models import ChatTurn, TurnJob, User, WorkspaceEvent
```

- [ ] **Step 4: Run — Expected: PASS**

Run: `uv run pytest tests/test_api_turns.py -k read_model -v`

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/call_manager/web/api/turns.py tests/test_api_turns.py
git add src/call_manager/web/api/turns.py tests/test_api_turns.py
git commit -m "feat(api): GET /workspaces/{id}/turns read-model (history + gate flags)"
```

---

## Task 3: JSON SSE turn stream

**Files:**
- Modify: `src/call_manager/web/api/turns.py`
- Test: `tests/test_api_turns.py`

**Interfaces:**
- Consumes: `TERMINAL_STATUSES` pattern + the polling structure from `proposal_assist/web/workspaces.py:_stream_turn_generator`; `_turn_to_dict` (Task 1); `require_member`.
- Produces: `GET /api/workspaces/{workspace_id}/turns/{job_id}/stream?since=<int>` → `text/event-stream`; the async generator `_json_turn_stream(*, session_factory, workspace_id, turn_job_id, since_position=None, poll_seconds=0.2, request=None)` yielding SSE strings.

- [ ] **Step 1: Write the failing test** — append to `tests/test_api_turns.py`:

```python
import asyncio


def test_json_turn_stream_emits_turns_then_done(monkeypatch, pg_url, pg_session_factory, tmp_path):
    from call_manager.db.models import ChatTurn as CT
    from call_manager.web.api.turns import _json_turn_stream

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    # Seed a user turn + a completed job + one assistant turn after it.
    with pg_session_factory() as s:
        u = CT(workspace_id=ws_id, role="user", content={"type": "text", "text": "hi"})
        s.add(u)
        s.flush()
        job = TurnJob(workspace_id=ws_id, user_turn_id=u.id, status="completed")
        s.add(job)
        s.add(CT(workspace_id=ws_id, role="assistant", content={"type": "text", "text": "ok"}))
        s.commit()
        job_id, u_pos = job.id, u.position

    async def _collect():
        out = []
        async for chunk in _json_turn_stream(
            session_factory=pg_session_factory, workspace_id=ws_id,
            turn_job_id=job_id, since_position=u_pos, poll_seconds=0.0, request=None,
        ):
            out.append(chunk)
        return out

    chunks = asyncio.run(_collect())
    joined = "".join(chunks)
    assert "event: turn" in joined
    assert "id: " in joined                 # SSE id line = position (resume cursor)
    assert '"role": "assistant"' in joined or '"role":"assistant"' in joined
    assert "event: done" in joined
    assert "completed" in joined
```

- [ ] **Step 2: Run — Expected: FAIL** (`_json_turn_stream` undefined).

Run: `uv run pytest tests/test_api_turns.py -k stream -v`

- [ ] **Step 3: Implement** — append to `src/call_manager/web/api/turns.py` (add `import asyncio`, `import json`, `from fastapi.responses import StreamingResponse`):

```python
_TERMINAL = frozenset({"completed", "failed", "cancelled"})


async def _json_turn_stream(
    *, session_factory, workspace_id, turn_job_id, since_position=None, poll_seconds=0.2, request=None
):
    """JSON analogue of the Jinja _stream_turn_generator: emit one `turn` event
    (with an `id:` cursor = ChatTurn.position) per new row, then a terminal
    `done` event. Honors since_position so useEventStream can resume on reconnect."""
    with session_factory() as s:
        job = s.get(TurnJob, turn_job_id)
        if job is None or job.workspace_id != workspace_id:
            yield "event: done\ndata: {\"status\": \"missing\"}\n\n"
            return
        last_seen = since_position if since_position is not None else (
            s.scalar(select(ChatTurn.position).where(ChatTurn.id == job.user_turn_id)) or 0
        )
    while True:
        if request is not None and await request.is_disconnected():
            return
        with session_factory() as s:
            tj = s.get(TurnJob, turn_job_id)
            terminal = tj is None or tj.status in _TERMINAL
            rows = list(
                s.scalars(
                    select(ChatTurn)
                    .where(ChatTurn.workspace_id == workspace_id, ChatTurn.position > last_seen)
                    .order_by(ChatTurn.position)
                )
            )
            for turn in rows:
                payload = json.dumps(_turn_to_dict(turn))
                yield f"id: {turn.position}\nevent: turn\ndata: {payload}\n\n"
                last_seen = turn.position
            if terminal:
                status = tj.status if tj is not None else "missing"
                yield f"event: done\ndata: {json.dumps({'status': status})}\n\n"
                return
        await asyncio.sleep(poll_seconds)


@router.get("/workspaces/{workspace_id}/turns/{job_id}/stream", include_in_schema=False)
def stream_turn(
    workspace_id: uuid.UUID,
    job_id: uuid.UUID,
    request: Request,
    since: int | None = None,
    user: User = Depends(get_current_user),  # noqa: B008
) -> StreamingResponse:
    """SSE stream of new chat turns for one job (owner OR viewer). EventSource
    can't set headers, so auth is the session cookie + require_member."""
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
    return StreamingResponse(
        _json_turn_stream(
            session_factory=sf, workspace_id=workspace_id, turn_job_id=job_id,
            since_position=since, request=request,
        ),
        media_type="text/event-stream",
    )
```

(`include_in_schema=False`: a streaming endpoint has no JSON body model, and the SPA builds its URL by hand — keeping it out of OpenAPI avoids a bogus generated type.)

- [ ] **Step 4: Run — Expected: PASS**

Run: `uv run pytest tests/test_api_turns.py -k stream -v`

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/call_manager/web/api/turns.py tests/test_api_turns.py
git add src/call_manager/web/api/turns.py tests/test_api_turns.py
git commit -m "feat(api): SSE JSON turn stream with id:position resume cursor"
```

---

## Task 4: Disclosure endpoint + OpenAPI regen

**Files:**
- Modify: `src/call_manager/web/api/turns.py`
- Test: `tests/test_api_turns.py`
- Regenerate: `frontend/src/api/schema.gen.ts`

**Interfaces:**
- Consumes: `require_member`; `WorkspaceEvent`.
- Produces: `POST /api/workspaces/{workspace_id}/disclosure` → `DisclosureResponse { acknowledged: bool }`. After this task the OpenAPI types for all four endpoints exist.

- [ ] **Step 1: Write the failing test** — append to `tests/test_api_turns.py`:

```python
def test_disclosure_ack_idempotent_and_flips_flag(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    c = _client(app, owner_id)
    assert c.get(f"/api/workspaces/{ws_id}/turns").json()["needs_disclosure"] is True
    r1 = c.post(f"/api/workspaces/{ws_id}/disclosure")
    assert r1.status_code == 200 and r1.json()["acknowledged"] is True
    r2 = c.post(f"/api/workspaces/{ws_id}/disclosure")  # idempotent
    assert r2.status_code == 200
    assert c.get(f"/api/workspaces/{ws_id}/turns").json()["needs_disclosure"] is False
    with pg_session_factory() as s:
        n = len(list(s.scalars(
            select(WorkspaceEvent.id).where(
                WorkspaceEvent.workspace_id == ws_id,
                WorkspaceEvent.kind == "disclosure_acknowledged",
                WorkspaceEvent.actor_id == owner_id,
            )
        )))
        assert n == 1  # not duplicated
```

- [ ] **Step 2: Run — Expected: FAIL** (404).

Run: `uv run pytest tests/test_api_turns.py -k disclosure -v`

- [ ] **Step 3: Implement** — append to `src/call_manager/web/api/turns.py`:

```python
class DisclosureResponse(BaseModel):
    acknowledged: bool


@router.post("/workspaces/{workspace_id}/disclosure", response_model=DisclosureResponse)
def ack_disclosure(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> DisclosureResponse:
    """Record the per-actor cloud-LLM disclosure acknowledgment (idempotent)."""
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        prior = session.scalar(
            select(WorkspaceEvent.id).where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.kind == "disclosure_acknowledged",
                WorkspaceEvent.actor_id == user.id,
            ).limit(1)
        )
        if prior is None:
            session.add(WorkspaceEvent(workspace_id=workspace_id, kind="disclosure_acknowledged", actor_id=user.id))
            session.commit()
    return DisclosureResponse(acknowledged=True)
```

- [ ] **Step 4: Run — Expected: PASS**

Run: `uv run pytest tests/test_api_turns.py -k disclosure -v`

- [ ] **Step 5: Run the full turns suite + regen types**

```bash
uv run pytest tests/test_api_turns.py -v
EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh
git diff --stat frontend/src/api/schema.gen.ts   # expect ONLY additive turns paths/schemas
```
Expected: all turns tests green; `schema.gen.ts` gains the 3 documented turns paths (submit/list/disclosure) + their schemas, no deletions.

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check src/call_manager/web/api/turns.py tests/test_api_turns.py
git add src/call_manager/web/api/turns.py tests/test_api_turns.py frontend/src/api/schema.gen.ts
git commit -m "feat(api): disclosure ack endpoint + regen OpenAPI types for turns"
```

---

## Task 5: Frontend API hooks + useEventStream cursor-reset hardening

**Files:**
- Create: `frontend/src/api/turns.ts`
- Modify: `frontend/src/api/events.ts`
- Test: `frontend/src/api/turns.test.ts`, `frontend/src/api/events.test.ts`

**Interfaces:**
- Consumes: `api`, `unwrap` (`./client`); `useQuery`, `useMutation`, `useQueryClient` (`@tanstack/react-query`).
- Produces: `useTurns(id)`, `useSubmitTurn(id)`, `useAckDisclosure(id)`, `turnStreamUrl(id, jobId)` from `frontend/src/api/turns.ts`; `useEventStream` resets its cursor on `url` change.

- [ ] **Step 1: Write the failing test (cursor reset)** — append to `frontend/src/api/events.test.ts`:

```tsx
it("resets the since cursor when the url changes (no stale cursor across job_ids)", () => {
  const instances: { url: string }[] = [];
  const captured: { ping?: (e: MessageEvent) => void } = {};
  class FakeES {
    url: string;
    onerror: ((e: unknown) => void) | null = null;
    onopen: (() => void) | null = null;
    removeEventListener = vi.fn();
    close = vi.fn();
    addEventListener(type: string, fn: (e: MessageEvent) => void) {
      if (type === "ping") captured.ping = fn;
    }
    constructor(u: string) {
      this.url = u;
      instances.push({ url: u });
    }
  }
  vi.stubGlobal("EventSource", FakeES as unknown as typeof EventSource);
  const { rerender } = renderHook(({ url }) => useEventStream(url, { ping: () => {} }), {
    initialProps: { url: "/api/workspaces/ws-1/turns/job-a/stream" },
  });
  captured.ping?.({ lastEventId: "42", data: "{}" } as MessageEvent);
  // Switch to a DIFFERENT job stream — the old cursor must NOT leak in.
  rerender({ url: "/api/workspaces/ws-1/turns/job-b/stream" });
  expect(instances[1].url).not.toContain("since=42");
});
```

- [ ] **Step 2: Run — Expected: FAIL** (the stale `since=42` leaks; current ref persists across url change).

Run: `pnpm vitest run src/api/events.test.ts`

- [ ] **Step 3: Implement the hardening** — in `frontend/src/api/events.ts`, reset the cursor at the top of the effect (the effect re-runs on `url` change):

```tsx
  useEffect(() => {
    let es: EventSource | null = null;
    lastId.current = null; // reset the resume cursor per url (job_id): no stale ?since across streams

    const open = () => {
```

- [ ] **Step 4: Run — Expected: PASS** (both the new test and the existing `?since=` test, which sets its cursor AFTER mount, stay green).

Run: `pnpm vitest run src/api/events.test.ts`

- [ ] **Step 5: Write the failing hooks test** — create `frontend/src/api/turns.test.ts`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "./client";
import { turnStreamUrl, useTurns } from "./turns";

afterEach(() => vi.restoreAllMocks());

function wrap() {
  const qc = new QueryClient();
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

it("useTurns fetches the read-model", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { turns: [], can_send: true, needs_disclosure: false, phase: "active", running_job_id: null },
    error: undefined,
  } as never);
  const { result } = renderHook(() => useTurns("ws-1"), { wrapper: wrap() });
  await waitFor(() => expect(result.current.data?.can_send).toBe(true));
});

it("turnStreamUrl builds the SSE path", () => {
  expect(turnStreamUrl("ws-1", "job-9")).toBe("/api/workspaces/ws-1/turns/job-9/stream");
});
```

- [ ] **Step 6: Run — Expected: FAIL** (`./turns` missing).

Run: `pnpm vitest run src/api/turns.test.ts`

- [ ] **Step 7: Implement** — create `frontend/src/api/turns.ts`:

```tsx
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";

export function useTurns(id: string) {
  return useQuery({
    queryKey: ["turns", id],
    enabled: !!id,
    queryFn: async () =>
      unwrap(await api.GET("/api/workspaces/{workspace_id}/turns", { params: { path: { workspace_id: id } } })),
  });
}

export function useSubmitTurn(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (vars: { content: string; reference_ids?: string[] }) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/turns", {
          params: { path: { workspace_id: id } },
          body: { content: vars.content, reference_ids: vars.reference_ids ?? [] },
        }),
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["turns", id] }),
  });
}

export function useAckDisclosure(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(await api.POST("/api/workspaces/{workspace_id}/disclosure", { params: { path: { workspace_id: id } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["turns", id] }),
  });
}

export function turnStreamUrl(id: string, jobId: string): string {
  return `/api/workspaces/${id}/turns/${jobId}/stream`;
}
```

- [ ] **Step 8: Run — Expected: PASS**

Run: `pnpm vitest run src/api/turns.test.ts src/api/events.test.ts && pnpm exec tsc --noEmit`

- [ ] **Step 9: Commit**

```bash
git add frontend/src/api/turns.ts frontend/src/api/turns.test.ts frontend/src/api/events.ts frontend/src/api/events.test.ts
git commit -m "feat(spa): turns API hooks + useEventStream cursor-reset-on-url-change"
```

---

## Task 6: AppShell right-pane region + CockpitChat shell

**Files:**
- Modify: `frontend/src/shell/AppShell.tsx`, `frontend/src/router.tsx`
- Create: `frontend/src/cockpit/chat/CockpitChat.tsx`, `frontend/src/cockpit/chat/CockpitChat.module.css`
- Test: `frontend/src/shell/AppShell.test.tsx`, `frontend/src/cockpit/chat/CockpitChat.test.tsx`

**Interfaces:**
- Consumes: `useMatches` (react-router); `useParams`; `useTurns` (Task 5).
- Produces: `AppShell` renders a right-pane region from the active route's `handle.RightPane`; the cockpit route sets `RightPane: CockpitChat`; `CockpitChat` exported from `frontend/src/cockpit/chat/CockpitChat.tsx`.

- [ ] **Step 1: Write the failing test** — create `frontend/src/cockpit/chat/CockpitChat.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../../api/client";
import { resetEditorStore } from "../editorStore";
import { CockpitChat } from "./CockpitChat";

afterEach(() => {
  vi.restoreAllMocks();
  resetEditorStore();
});

function renderChat() {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { turns: [], can_send: true, needs_disclosure: false, phase: "active", running_job_id: null },
    error: undefined,
  } as never);
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/cockpit/ws-1"]}>
        <Routes>
          <Route path="/cockpit/:id" element={<CockpitChat />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

it("renders the chat pane header", async () => {
  renderChat();
  expect(await screen.findByRole("complementary", { name: /chat/i })).toBeInTheDocument();
});
```

- [ ] **Step 2: Run — Expected: FAIL** (no `CockpitChat`).

Run: `pnpm vitest run src/cockpit/chat/CockpitChat.test.tsx`

- [ ] **Step 3: Implement the shell region.** In `frontend/src/shell/AppShell.tsx`, add a right-pane handle type + lookup + region (mirroring the existing `Sidebar`). Add alongside `SidebarHandle`:

```tsx
type RightPaneHandle = { RightPane?: ComponentType };
```
After the `SidebarContent` lookup, add:
```tsx
const RightPaneContent = [...matches]
  .reverse()
  .map((m) => (m.handle as RightPaneHandle | undefined)?.RightPane)
  .find((R): R is ComponentType => Boolean(R));
```
And render it as a sibling of the main `<Outlet>` region (right of it), after the main content block:
```tsx
{RightPaneContent && <RightPaneContent />}
```
(`CockpitChat` renders its own `<aside>` landmark, so AppShell just mounts it — no extra wrapper needed.)

In `frontend/src/router.tsx`, extend the cockpit route handle:
```tsx
import { CockpitChat } from "./cockpit/chat/CockpitChat";
// ...
{ path: "cockpit/:id", element: <Cockpit />, handle: { Sidebar: CockpitSidebar, RightPane: CockpitChat } },
```

Create `frontend/src/cockpit/chat/CockpitChat.tsx`:

```tsx
import { useParams } from "react-router-dom";

import { useTurns } from "../../api/turns";
import styles from "./CockpitChat.module.css";

export function CockpitChat() {
  const { id = "" } = useParams();
  const turns = useTurns(id);
  return (
    <aside className={styles.chat} aria-label="Chat">
      <header className={styles.header}>Chat</header>
      <div className={styles.body}>
        {turns.isLoading && <p className={styles.note}>Loading…</p>}
        {/* TurnList + footer-gate land in Tasks 7-8 */}
      </div>
    </aside>
  );
}
```

Create `frontend/src/cockpit/chat/CockpitChat.module.css`:
```css
.chat {
  width: 320px;
  display: flex;
  flex-direction: column;
  background: var(--app-panel);
  border-left: 1px solid var(--app-border);
  min-height: 0;
}
.header {
  padding: 6px 10px;
  font-weight: 600;
  color: var(--app-text);
  border-bottom: 1px solid var(--app-border);
}
.body { flex: 1; overflow-y: auto; padding: 8px; min-height: 0; }
.note { color: var(--app-dim); }
```

- [ ] **Step 4: Run — Expected: PASS**

Run: `pnpm vitest run src/cockpit/chat/CockpitChat.test.tsx`

- [ ] **Step 5: Keep AppShell tests green.** `AppShell.test.tsx`'s render helpers already wrap `QueryClientProvider` (Phase 3b). Add one assertion that a route with `handle.RightPane` mounts it; render at a route whose handle includes a trivial `RightPane: () => <aside aria-label="Chat" />` and assert it appears. Run:

`pnpm vitest run src/shell/AppShell.test.tsx`
Expected: PASS (existing + new assertion).

- [ ] **Step 6: tsc + commit**

```bash
pnpm exec tsc --noEmit
git add frontend/src/shell/AppShell.tsx frontend/src/shell/AppShell.test.tsx frontend/src/router.tsx frontend/src/cockpit/chat/
git commit -m "feat(spa): AppShell right-pane region + CockpitChat shell (handle.RightPane)"
```

---

## Task 7: Transcript rendering (TurnList / TurnItem)

**Files:**
- Create: `frontend/src/cockpit/chat/TurnList.tsx`
- Modify: `frontend/src/cockpit/chat/CockpitChat.tsx`, `CockpitChat.module.css`
- Test: `frontend/src/cockpit/chat/TurnList.test.tsx`

**Interfaces:**
- Consumes: `useReferences(id)` (`../../api/citations`); the `TurnOut` shape (`{id, role, content, created_at, position}`) from `useTurns`.
- Produces: `TurnList({ turns, refs })` rendering a typed transcript; `TurnItem` dispatch.

- [ ] **Step 1: Write the failing test** — create `frontend/src/cockpit/chat/TurnList.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { TurnList } from "./TurnList";

const refs = [{ id: "r1", citekey: "smith2021", title: "Smith 2021", kind: "paper", text_status: "done" }];

it("renders user text, assistant text, a compact tool line, and a reference chip", () => {
  render(
    <TurnList
      refs={refs as never}
      turns={[
        { id: "1", role: "user", position: 1, created_at: "", content: [{ type: "text", text: "Use this" }, { type: "reference", reference_id: "r1" }] },
        { id: "2", role: "tool", position: 2, created_at: "", content: { type: "tool_use", tool_name: "read_proposal" } },
        { id: "3", role: "assistant", position: 3, created_at: "", content: { type: "text", text: "Proposed an edit." } },
        { id: "4", role: "assistant", position: 4, created_at: "", content: { type: "turn_outcome", text: "Proposed edit → Methods" } },
      ] as never}
    />,
  );
  expect(screen.getByText("Use this")).toBeInTheDocument();
  expect(screen.getByText(/smith2021/)).toBeInTheDocument();   // reference chip resolved from refs
  expect(screen.getByText(/read_proposal/)).toBeInTheDocument(); // compact tool line
  expect(screen.getByText("Proposed an edit.")).toBeInTheDocument();
  expect(screen.getByText(/Proposed edit → Methods/)).toBeInTheDocument(); // outcome chip
});

it("falls back to a muted chip for an unknown reference id", () => {
  render(
    <TurnList
      refs={[] as never}
      turns={[{ id: "1", role: "user", position: 1, created_at: "", content: [{ type: "reference", reference_id: "gone" }] }] as never}
    />,
  );
  expect(screen.getByText(/reference/i)).toBeInTheDocument();
});
```

- [ ] **Step 2: Run — Expected: FAIL** (no `TurnList`).

Run: `pnpm vitest run src/cockpit/chat/TurnList.test.tsx`

- [ ] **Step 3: Implement** — create `frontend/src/cockpit/chat/TurnList.tsx`:

```tsx
import styles from "./CockpitChat.module.css";

type Ref = { id: string; citekey: string | null; title: string };
type Turn = { id: string; role: string; content: unknown; position: number };
type Block = Record<string, unknown>;

function blocks(content: unknown): Block[] {
  if (Array.isArray(content)) return content as Block[];
  if (content && typeof content === "object") return [content as Block];
  if (typeof content === "string") return [{ type: "text", text: content }];
  return [];
}

function RefChip({ id, refs }: { id: string; refs: Ref[] }) {
  const r = refs.find((x) => x.id === id);
  return <span className={styles.refChip}>{r ? (r.citekey ?? r.title) : "reference"}</span>;
}

function TurnItem({ turn, refs }: { turn: Turn; refs: Ref[] }) {
  const bs = blocks(turn.content);
  // Tool rows render as one compact, low-emphasis line.
  if (turn.role === "tool") {
    const name = bs.map((b) => (b.tool_name as string) || (b.type as string)).join(" · ");
    return <div className={styles.tool}>⚙ {name}</div>;
  }
  const cls = turn.role === "user" ? styles.userTurn : styles.assistantTurn;
  return (
    <div className={cls}>
      {bs.map((b, i) => {
        if (b.type === "turn_outcome") return <div key={i} className={styles.outcome}>✓ {String(b.text ?? "")}</div>;
        if (b.type === "reference") return <RefChip key={i} id={String(b.reference_id)} refs={refs} />;
        if (b.type === "text" || typeof b.text === "string") return <span key={i}>{String(b.text ?? "")}</span>;
        return <span key={i} className={styles.note}>·</span>; // unknown block: muted, never throws
      })}
    </div>
  );
}

export function TurnList({ turns, refs }: { turns: Turn[]; refs: Ref[] }) {
  return (
    <div className={styles.transcript}>
      {turns.map((t) => (
        <TurnItem key={t.id} turn={t} refs={refs} />
      ))}
    </div>
  );
}
```

Wire it into `CockpitChat.tsx` (replace the body placeholder), resolving references defensively:
```tsx
import { useReferences } from "../../api/citations";
import { TurnList } from "./TurnList";
// inside CockpitChat, after const turns = useTurns(id):
const references = useReferences(id);
const refs = Array.isArray(references.data) ? references.data : [];
// in the body:
{turns.data && <TurnList turns={turns.data.turns} refs={refs} />}
```

Add the styles to `CockpitChat.module.css`:
```css
.transcript { display: flex; flex-direction: column; gap: 8px; }
.userTurn { align-self: flex-end; background: var(--app-selected); color: var(--app-text); border-radius: 8px 8px 2px 8px; padding: 5px 8px; max-width: 85%; }
.assistantTurn { align-self: flex-start; background: var(--app-elevated); color: var(--app-text); border-radius: 8px 8px 8px 2px; padding: 5px 8px; max-width: 90%; }
.tool { color: var(--app-dim); font-size: 0.85em; font-style: italic; }
.outcome { color: var(--app-accent); font-weight: 600; }
.refChip { display: inline-block; background: var(--app-selected); color: var(--app-link); border-radius: 10px; padding: 0 6px; margin: 0 2px; font-size: 0.85em; }
```
(If `--app-elevated`/`--app-selected` aren't defined, substitute the nearest existing token from `frontend/src/theme/theme.css` — grep `--app-` there.)

- [ ] **Step 4: Run — Expected: PASS**

Run: `pnpm vitest run src/cockpit/chat/TurnList.test.tsx`

- [ ] **Step 5: tsc + commit**

```bash
pnpm exec tsc --noEmit
git add frontend/src/cockpit/chat/
git commit -m "feat(spa): chat transcript renderer (typed blocks + reference chips)"
```

---

## Task 8: Composer + footer gates + disclosure

**Files:**
- Create: `frontend/src/cockpit/chat/Composer.tsx`
- Modify: `frontend/src/cockpit/chat/CockpitChat.tsx`, `CockpitChat.module.css`
- Test: `frontend/src/cockpit/chat/CockpitChat.test.tsx` (extend)

**Interfaces:**
- Consumes: `useSubmitTurn(id)`, `useAckDisclosure(id)` (Task 5); `useReferences(id)`.
- Produces: `Composer({ id, refs, disabled })`; `CockpitChat` footer renders exactly one of: read-only note / setup gate / disabled note / `DisclosureGate` / `Composer`, in the canonical precedence.

- [ ] **Step 1: Write the failing tests** — extend `frontend/src/cockpit/chat/CockpitChat.test.tsx` with a flag-driven mock helper:

```tsx
function mockTurns(over: Partial<{ can_send: boolean; needs_disclosure: boolean; phase: string; role: string }>) {
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/turns"))
      return Promise.resolve({ data: { turns: [], can_send: over.can_send ?? true, needs_disclosure: over.needs_disclosure ?? false, phase: over.phase ?? "active", running_job_id: null }, error: undefined });
    if (path.endsWith("/references")) return Promise.resolve({ data: [], error: undefined });
    return Promise.resolve({ data: null, error: undefined });
  }) as never);
}

it("shows the composer for an owner on an active acked workspace", async () => {
  mockTurns({ can_send: true, needs_disclosure: false });
  renderChat();
  expect(await screen.findByPlaceholderText(/message the agent/i)).toBeInTheDocument();
});

it("shows the disclosure gate (not composer) when not acked", async () => {
  mockTurns({ can_send: true, needs_disclosure: true });
  renderChat();
  expect(await screen.findByRole("button", { name: /i understand/i })).toBeInTheDocument();
  expect(screen.queryByPlaceholderText(/message the agent/i)).not.toBeInTheDocument();
});

it("shows the setup gate (not the disclosure gate) on a setup-phase workspace", async () => {
  mockTurns({ phase: "setup", needs_disclosure: true, can_send: false });
  renderChat();
  expect(await screen.findByText(/finish setup/i)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /i understand/i })).not.toBeInTheDocument();
});

it("read-only for a non-owner (can_send false, phase active)", async () => {
  mockTurns({ can_send: false, needs_disclosure: false, phase: "active" });
  renderChat();
  expect(await screen.findByText(/read-only/i)).toBeInTheDocument();
});
```
(`renderChat` no longer sets its own mock — let each test call `mockTurns` first; remove the `vi.spyOn` from `renderChat`.)

- [ ] **Step 2: Run — Expected: FAIL** (no composer/gates).

Run: `pnpm vitest run src/cockpit/chat/CockpitChat.test.tsx`

- [ ] **Step 3: Implement** — create `frontend/src/cockpit/chat/Composer.tsx`:

```tsx
import { useState } from "react";

import { useSubmitTurn } from "../../api/turns";
import styles from "./CockpitChat.module.css";

type Ref = { id: string; citekey: string | null; title: string; text_status?: string };

export function Composer({ id, refs, disabled }: { id: string; refs: Ref[]; disabled: boolean }) {
  const [text, setText] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const submit = useSubmitTurn(id);
  const citable = refs.filter((r) => r.text_status === "done");
  const busy = disabled || submit.isPending;

  const send = () => {
    if (!text.trim() || busy) return;
    submit.mutate(
      { content: text, reference_ids: picked },
      { onSuccess: () => { setText(""); setPicked([]); } },
    );
  };

  return (
    <div className={styles.composer}>
      {picked.length > 0 && (
        <div className={styles.picked}>
          {picked.map((pid) => {
            const r = citable.find((x) => x.id === pid);
            return <span key={pid} className={styles.refChip}>{r?.citekey ?? r?.title ?? "ref"}</span>;
          })}
        </div>
      )}
      <textarea
        className={styles.textarea}
        placeholder="Message the agent…"
        value={text}
        disabled={busy}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send(); }}
      />
      <div className={styles.affordances}>
        {citable.length > 0 && (
          <select
            className={styles.refSelect}
            value=""
            disabled={busy}
            onChange={(e) => { if (e.target.value && !picked.includes(e.target.value)) setPicked([...picked, e.target.value]); }}
          >
            <option value="">📚 Add reference…</option>
            {citable.map((r) => <option key={r.id} value={r.id}>{r.citekey ?? r.title}</option>)}
          </select>
        )}
        <span className={styles.deferred} title="Image attachments arrive with the Files slice">📎 Image</span>
        <button type="button" className={styles.send} disabled={busy || !text.trim()} onClick={send}>Send</button>
      </div>
      {submit.isError && <p className={styles.error}>{(submit.error as Error)?.message ?? "Couldn't send."}</p>}
    </div>
  );
}
```

In `CockpitChat.tsx`, add the footer with the canonical precedence (after the transcript), reading `useTurns` flags + `useAckDisclosure`:
```tsx
import { useAckDisclosure } from "../../api/turns";
import { Composer } from "./Composer";
// inside CockpitChat:
const ack = useAckDisclosure(id);
const d = turns.data;
// ...in the JSX, after the transcript block:
{d && (
  <footer className={styles.footer}>
    {d.phase === "active" && !d.can_send ? (
      <p className={styles.note}>Read-only — only the owner can message the agent.</p>
    ) : d.phase === "setup" ? (
      <p className={styles.note}>Finish setup — review the rubric in the legacy workspace before drafting.</p>
    ) : !d.can_send ? (
      <p className={styles.note}>Drafting is currently disabled.</p>
    ) : d.needs_disclosure ? (
      <div className={styles.disclosure}>
        <p>Proposals are generated by a hosted model. Don't paste patient-identifiable data.</p>
        <button type="button" disabled={ack.isPending} onClick={() => ack.mutate()}>I understand</button>
      </div>
    ) : (
      <Composer id={id} refs={refs} disabled={false} />
    )}
  </footer>
)}
```
Note the precedence: a `setup` workspace has `can_send=false`, so the read-only branch (which requires `phase==="active"`) is skipped and the setup branch wins — matching the backend gate order (setup before disclosure). Add `.composer/.textarea/.affordances/.send/.deferred/.refSelect/.picked/.error/.footer/.disclosure` styles to `CockpitChat.module.css` using `--app-*` tokens (mirror the Composer mockup: textarea on `--app-editor`, send button `--app-accent`).

- [ ] **Step 4: Run — Expected: PASS**

Run: `pnpm vitest run src/cockpit/chat/CockpitChat.test.tsx`

- [ ] **Step 5: tsc + commit**

```bash
pnpm exec tsc --noEmit
git add frontend/src/cockpit/chat/
git commit -m "feat(spa): chat composer + footer gates (precedence: viewer/setup/disabled/disclosure)"
```

---

## Task 9: Live streaming wiring

**Files:**
- Modify: `frontend/src/cockpit/chat/CockpitChat.tsx`
- Test: `frontend/src/cockpit/chat/CockpitChat.test.tsx` (extend with a `FakeES`)

**Interfaces:**
- Consumes: `useEventStream` (`../../api/events`), `turnStreamUrl` (Task 5), `useQueryClient`.
- Produces: live turn streaming — append streamed turns (deduped by `position`); on submit, open the stream for the new `job_id`; on history load with a non-null `running_job_id`, re-attach; on `done`, invalidate `["turns",id]`, `["proposed-changes",id,"draft"]`, `["proposed-changes",id,"spec"]`, `["draft",id]` (refreshing the editor + the Inbox badge).

- [ ] **Step 1: Write the failing test** — extend `CockpitChat.test.tsx`:

```tsx
it("streams turns from a running job and invalidates on done", async () => {
  const captured: { turn?: (e: MessageEvent) => void; done?: (e: MessageEvent) => void } = {};
  class FakeES {
    onerror = null; onopen: (() => void) | null = null; close = vi.fn();
    addEventListener(type: string, fn: (e: MessageEvent) => void) {
      if (type === "turn") captured.turn = fn;
      if (type === "done") captured.done = fn;
    }
    constructor(_u: string) {}
  }
  vi.stubGlobal("EventSource", FakeES as unknown as typeof EventSource);
  // running_job_id set → CockpitChat re-attaches the stream on mount.
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/turns"))
      return Promise.resolve({ data: { turns: [], can_send: true, needs_disclosure: false, phase: "active", running_job_id: "job-1" }, error: undefined });
    return Promise.resolve({ data: [], error: undefined });
  }) as never);
  renderChat();
  // Stream one assistant turn → it appears in the transcript.
  captured.turn?.({ data: JSON.stringify({ id: "a1", role: "assistant", position: 5, created_at: "", content: { type: "text", text: "streamed!" } }) } as MessageEvent);
  expect(await screen.findByText("streamed!")).toBeInTheDocument();
  // done closes cleanly (no throw).
  captured.done?.({ data: JSON.stringify({ status: "completed" }) } as MessageEvent);
});
```

- [ ] **Step 2: Run — Expected: FAIL** (no streaming; "streamed!" never appears).

Run: `pnpm vitest run src/cockpit/chat/CockpitChat.test.tsx`

- [ ] **Step 3: Implement** — in `CockpitChat.tsx`, track the active `job_id` + locally-streamed turns, merge them with the fetched history (dedupe by `position`), and wire `useEventStream`:

```tsx
import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { useEventStream } from "../../api/events";
import { turnStreamUrl, useSubmitTurn } from "../../api/turns";
// inside CockpitChat (id, turns, refs as before):
const qc = useQueryClient();
const [jobId, setJobId] = useState<string | null>(null);
const [streamed, setStreamed] = useState<Record<number, Turn>>({}); // keyed by position (dedupe)

// Re-attach to an in-flight turn after a reload.
useEffect(() => {
  if (turns.data?.running_job_id) setJobId(turns.data.running_job_id);
}, [turns.data?.running_job_id]);

useEventStream(
  jobId ? turnStreamUrl(id, jobId) : "",
  {
    turn: (data) => { const t = data as Turn; setStreamed((m) => ({ ...m, [t.position]: t })); },
    done: () => {
      setJobId(null);
      setStreamed({});
      qc.invalidateQueries({ queryKey: ["turns", id] });
      qc.invalidateQueries({ queryKey: ["proposed-changes", id, "draft"] });
      qc.invalidateQueries({ queryKey: ["proposed-changes", id, "spec"] });
      qc.invalidateQueries({ queryKey: ["draft", id] });
    },
  },
);

const merged = useMemo(() => {
  const byPos = new Map<number, Turn>();
  for (const t of turns.data?.turns ?? []) byPos.set(t.position, t as Turn);
  for (const t of Object.values(streamed)) byPos.set(t.position, t);
  return [...byPos.values()].sort((a, b) => a.position - b.position);
}, [turns.data?.turns, streamed]);
```
Pass `merged` to `<TurnList turns={merged} refs={refs} />`. When `useEventStream` is called with `""` (no job), it builds `new URL("", origin)` → harmless EventSource on the SPA origin that the cleanup closes; gate it instead by only mounting the stream when `jobId` is set — wrap the hook usage so it no-ops on empty url. **Implementation detail:** keep the hook call unconditional (Rules of Hooks) but make `turnStreamUrl` return `""` when `!jobId` and have `useEventStream` early-return inside its effect when `url === ""`. Add to `events.ts` at the top of the effect: `if (!url) return;` (before opening). After a successful submit, set the job id: in the `Composer`'s `onSuccess`, lift the returned `job_id` up via a callback prop `onSubmitted(jobId)` → `setJobId`.

Wire the submit→stream handoff: give `Composer` an `onSubmitted?: (jobId: string) => void` prop; in its `submit.mutate` success, call `onSubmitted?.(result.job_id)` (the mutation returns `{job_id,...}`). In `CockpitChat`, pass `onSubmitted={setJobId}`.

- [ ] **Step 4: Run — Expected: PASS**

Run: `pnpm vitest run src/cockpit/chat/CockpitChat.test.tsx`

- [ ] **Step 5: Full frontend gate + commit**

```bash
pnpm exec tsc --noEmit && pnpm vitest run && pnpm build
git add frontend/src/cockpit/chat/ frontend/src/api/events.ts
git commit -m "feat(spa): live turn streaming + propose→inbox invalidation; empty-url guard"
```

---

## Self-Review (completed)

- **Spec coverage:** §4.1 read-model → Task 2; §4.2 submit + gate order → Task 1; §4.3 SSE `id:`/`since=` → Task 3; §4.4 disclosure → Task 4; §5 typed blocks + client-side ref resolution → Task 7; §6 `useEventStream` + cursor-reset → Tasks 5/9; §7 right-pane shell region + components + precedence → Tasks 6/8; §8 edge states → Tasks 2/8; §9 no-migration/targeted-tests → all backend tasks. **Deviation (noted):** §5's outcome chip "deep-links to the Inbox tab" is implemented as a **live Inbox-badge update via invalidation** (Task 9), not a tab-switch — the chat is a shell region and can't reach Cockpit's local `bottomTab` across the AppShell boundary without lifting it to a global channel; click-to-focus-Inbox is deferred. The spec's specific error codes (`workspace_setup` etc.) are **not** emitted (the `/api` handler maps status→generic code); the frontend gates from the read-model instead, which the spec already designates as the mechanism.
- **Placeholder scan:** none — every code step is concrete.
- **Type consistency:** `TurnOut`/`SubmitTurnResponse`/`TurnsResponse`/`DisclosureResponse` consistent backend↔tests; `useTurns`/`useSubmitTurn`/`useAckDisclosure`/`turnStreamUrl` consistent across Tasks 5/8/9; the `Turn`/`Ref`/`Block` shapes consistent across Tasks 7/8/9.
