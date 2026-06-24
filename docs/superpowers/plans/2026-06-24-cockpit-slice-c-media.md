# Cockpit Slice C-media — Image Attachments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring workspace image attachments into the React cockpit — upload/list/delete via a new bottom-panel Library **Files** section, include images in a chat turn from the Composer, and render image blocks in the transcript — at parity with the Jinja files UI.

**Architecture:** A new **writes-owner-gated** `/api` files router (`web/api/files.py`) that **reuses the existing files service** (`_probe_image_format`, `_read_capped`, `_sanitize_filename`, `AttachmentStorage`, `estimate_image_tokens`) and inlines the same upload flow the Jinja `upload_file` runs. The chat-turn machinery **already supports `attachment_ids`** end-to-end (`submit_user_turn` enforces the 5-cap, builds `{type:"image",attachment_id}` blocks, and the drain resolves them to base64), so §6 is a tiny pass-through: add `attachment_ids` to the JSON `SubmitTurnIn` and forward it. Image bytes are served by the **existing Jinja `GET /workspaces/{id}/files/{aid}`** (cookie-authed) — the React `<img src>` points there; no new `/api` binary-serve endpoint (Slice-G forward-dep). Frontend: `api/files.ts` hooks + a Library Files section + a Composer image affordance (replacing the disabled stub) + a TurnList image branch. **No DB schema change. Zero Jinja routes touched. No shared helper extracted** (Jinja files routes are deleted at Slice G; the short-lived duplication self-resolves).

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2 / Postgres (testcontainers) / Pillow / `uv` / `ruff` / pytest · React 19 + Vite + react-router-dom 7 + TanStack Query 5 + Vitest + Testing Library · `openapi-fetch` against `schema.gen.ts`.

## Global Constraints

- **No DB schema change**; alembic head untouched (the `attachments` table + B7 columns already exist — do NOT add a migration).
- **Touch ZERO Jinja routes and extract NO new shared helper** — reuse the existing files service helpers + `submit_user_turn` directly and inline the thin orchestration (mirroring the Jinja `upload_file`/`delete_file` flow). The Jinja files routes are deleted at Slice G; the short-lived Jinja↔`/api` duplication is intentional and self-resolving.
- **New `/api` module** `src/call_manager/web/api/files.py` mounted via `web/api/__init__.py`. Do NOT append to `workspaces.py`.
- **Files routes write NO `WorkspaceEvent`** (the Jinja `upload_file`/`delete_file` write none — confirmed). There is no audit-literal concern for this slice; do NOT add audit events.
- **Permissions mirror Jinja exactly (note the asymmetry vs references):** **upload + delete are OWNER-gated** (`require_owner`); **list + the image-serve are member/viewer** (`require_member` = owner OR `WorkspaceViewer`). This is intentional parity, not a new policy.
- **Image bytes are served by the existing Jinja `GET /workspaces/{id}/files/{aid}`** (`FileResponse`, `Cache-Control: private, max-age=300`, member-gated, IDOR-404). The React `<img src>` points at it. Do NOT build a new `/api` binary-serve endpoint (Slice G owns that, alongside the references `/raw` forward-dep).
- **The 5-cap is already enforced** by `submit_user_turn` (`MAX_TURN_INCLUSIONS = 5`, `turns/submit.py:75`) across `attachment_ids + reference_ids`, raising `ValueError("too many inclusions: N > 5")`; the `/api` `submit_turn` handler already maps any `ValueError` → `HTTPException(400)`. The frontend additionally disables adding past the combined cap with a hint (spec §10).
- **OpenAPI regen requires `EMBEDDING_MODEL=bge-m3`**; additive diff only.
- **No `routes.gen.ts` change** (Library Files section, Composer, and TurnList are all in-cockpit — no new SPA route).
- **Backend tests:** targeted files only; `ruff check` + `ruff format` touched files.
- **Frontend:** Node 22 + `pnpm`; `pnpm exec tsc --noEmit` is the gate; `pnpm vitest run <file>`; `pnpm build`.

## Reuse map (exact signatures — call these; do not reimplement)

- From `src/call_manager/proposal_assist/web/files.py` (import as-is; module-level): `_ALLOWED_MIMES` (:79, `{image/png,image/jpeg,image/webp,image/gif}`), `_MAX_BYTES` (:76, `5*1024*1024`), `_read_capped(file, max_bytes) -> bytes` (async, :186, raises `HTTPException(400)` over cap), `_probe_image_format(data) -> (fmt, w, h)` (:208, raises `HTTPException(400)` on bad/oversize image; `fmt` is Pillow's uppercase format), `_PIL_FORMAT_TO_MIME` (:98), `_SUFFIX_BY_MIME` (:88), `_sanitize_filename(name, *, default="attachment") -> str` (:126), `_storage_for(request) -> AttachmentStorage` (:116, returns `AttachmentStorage(root=Path(settings.workspaces_dir))`).
- `AttachmentStorage` — `src/call_manager/proposal_assist/attachments/storage.py:53`: `write_atomic(*, workspace_id, attachment_id, suffix, data) -> Path`, `delete_one(*, workspace_id, attachment_id, suffix) -> None` (idempotent), `path_for(*, workspace_id, attachment_id, suffix) -> Path`.
- `estimate_image_tokens(width, height) -> int` — `attachments/tokens.py:32`.
- `Attachment` ORM — `src/call_manager/db/models.py:1156`, table `attachments`. Columns (all NOT NULL unless noted): `id` (UUID PK), `workspace_id`, `kind` (check ∈ {figure,preliminary_data,past_grant,other}), `filename`, `storage_path`, `mime_type`, `description` (nullable), `uploaded_by`, `uploaded_at` (server_default now), and B7 `width_px`/`height_px`/`tokens_estimate` (nullable).
- `submit_user_turn(*, session, workspace_id, content_text, attachment_ids=None, reference_ids=None, submitted_by_user_id, settings, tokens_in=None, workspaces_dir=None) -> TurnJob` — `turns/submit.py:175`. Builds `{"type":"image","attachment_id":str(aid)}` blocks; cap + cross-workspace → `ValueError`.
- `require_owner`/`require_member` — `proposal_assist/web/_perms.py:18/29` (keyword-only; return `Workspace`).
- `/api` idioms (`web/api/turns.py`): `from call_manager.auth.dependencies import get_current_user`; `from call_manager.db.models import Attachment, User`; `with sf() as session` from `request.app.state.session_factory`; handlers `raise HTTPException` (global handler builds the `{error:{code,message}}` envelope).
- Frontend: `api`, `unwrap`, `ApiError`, `redirectToLogin` from `./client`; the **`postMultipart` raw-`fetch` pattern** in `api/references.ts:7` to mirror for image upload; `useReferences` (`api/citations.ts:35`, query key `["references", id]`) as the model for `useFiles`.

---

## Task 1: Files `/api` module (`web/api/files.py`) — list, upload, delete

**Files:**
- Create: `src/call_manager/web/api/files.py`
- Modify: `src/call_manager/web/api/__init__.py` (import + mount)
- Create: `tests/test_api_files.py`

**Interfaces:**
- Produces: `GET /api/workspaces/{workspace_id}/files` (member) → `FileListItemOut[] {id, filename, mime_type, uploaded_at}`; `POST /api/workspaces/{workspace_id}/files` (owner, multipart `file`) → `FileOut {attachment_id, filename, mime_type}`; `DELETE /api/workspaces/{workspace_id}/files/{attachment_id}` (owner) → **204**.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_files.py`:

```python
"""JSON /api image-attachment endpoints (Cockpit Slice C-media)."""

import io
import uuid

from PIL import Image
from sqlalchemy import func, select

from call_manager.db.models import Attachment
from tests.proposal_assist._refs_helpers import add_member, authed_client, build_app
from tests.proposal_assist.conftest import make_user, make_workspace


def _png_bytes(width: int = 4, height: int = 4) -> bytes:
    img = Image.new("RGB", (width, height), color=(0, 128, 64))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


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


def _upload(app, *, user_id, ws_id, name="fig.png", data=None, mime="image/png"):
    return authed_client(app, user_id=user_id).post(
        f"/api/workspaces/{ws_id}/files",
        files={"file": (name, data if data is not None else _png_bytes(), mime)},
    )


def test_api_files_upload_owner_persists_row(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = _upload(app, user_id=owner_id, ws_id=ws_id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mime_type"] == "image/png"
    aid = body["attachment_id"]
    with pg_session_factory() as s:
        att = s.get(Attachment, uuid.UUID(aid))
        assert att is not None
        assert att.kind == "figure"
        assert att.workspace_id == ws_id
        assert att.tokens_estimate is not None


def test_api_files_upload_viewer_is_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory)
    r = _upload(app, user_id=member_id, ws_id=ws_id)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_api_files_upload_non_image_is_400(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = _upload(app, user_id=owner_id, ws_id=ws_id, data=b"not an image", mime="image/png")
    assert r.status_code == 400


def test_api_files_upload_bad_mime_is_400(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = _upload(app, user_id=owner_id, ws_id=ws_id, mime="application/pdf")
    assert r.status_code == 400


def test_api_files_list_member_sees_rows(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, member_id, _ = _setup(pg_session_factory)
    _upload(app, user_id=owner_id, ws_id=ws_id, name="a.png")
    r = authed_client(app, user_id=member_id).get(f"/api/workspaces/{ws_id}/files")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["filename"] == "a.png"
    assert set(rows[0]) == {"id", "filename", "mime_type", "uploaded_at"}


def test_api_files_list_stranger_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, _, stranger_id = _setup(pg_session_factory)
    r = authed_client(app, user_id=stranger_id).get(f"/api/workspaces/{ws_id}/files")
    assert r.status_code == 403


def test_api_files_delete_owner_204_and_idempotent(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    aid = _upload(app, user_id=owner_id, ws_id=ws_id).json()["attachment_id"]
    c = authed_client(app, user_id=owner_id)
    r1 = c.delete(f"/api/workspaces/{ws_id}/files/{aid}")
    assert r1.status_code == 204
    with pg_session_factory() as s:
        n = s.scalar(select(func.count()).select_from(Attachment).where(Attachment.workspace_id == ws_id))
    assert n == 0
    r2 = c.delete(f"/api/workspaces/{ws_id}/files/{aid}")  # already gone
    assert r2.status_code == 204


def test_api_files_delete_viewer_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, member_id, _ = _setup(pg_session_factory)
    aid = _upload(app, user_id=owner_id, ws_id=ws_id).json()["attachment_id"]
    r = authed_client(app, user_id=member_id).delete(f"/api/workspaces/{ws_id}/files/{aid}")
    assert r.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_files.py -x -q`
Expected: FAIL — 404 (routes not mounted).

- [ ] **Step 3: Implement — create the module**

Create `src/call_manager/web/api/files.py`:

```python
"""JSON /api surface for image attachments (Cockpit Library Files, Slice C-media).

Owner-gated writes (upload/delete) + member-gated list, REUSING the existing
files service helpers (_probe_image_format, _read_capped, AttachmentStorage,
estimate_image_tokens) and inlining the Jinja upload_file flow. Image BYTES are
served by the existing Jinja GET /workspaces/{id}/files/{aid} (no new binary
serve here — Slice G owns that). The Jinja files routes coexist until Slice G.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import select

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import Attachment, User
from call_manager.proposal_assist.attachments.tokens import estimate_image_tokens
from call_manager.proposal_assist.web._perms import require_member, require_owner
from call_manager.proposal_assist.web.files import (
    _ALLOWED_MIMES,
    _MAX_BYTES,
    _PIL_FORMAT_TO_MIME,
    _SUFFIX_BY_MIME,
    _probe_image_format,
    _read_capped,
    _sanitize_filename,
    _storage_for,
)

router = APIRouter()


class FileListItemOut(BaseModel):
    id: str
    filename: str
    mime_type: str
    uploaded_at: str


class FileOut(BaseModel):
    attachment_id: str
    filename: str
    mime_type: str


@router.get("/workspaces/{workspace_id}/files", response_model=list[FileListItemOut])
def api_list_files(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> list[FileListItemOut]:
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        rows = session.scalars(
            select(Attachment)
            .where(Attachment.workspace_id == workspace_id)
            .order_by(Attachment.uploaded_at.desc())
        ).all()
        return [
            FileListItemOut(
                id=str(a.id),
                filename=a.filename,
                mime_type=a.mime_type,
                uploaded_at=a.uploaded_at.isoformat(),
            )
            for a in rows
        ]


@router.post("/workspaces/{workspace_id}/files", response_model=FileOut)
async def api_upload_file(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
    file: UploadFile = File(...),  # noqa: B008
) -> FileOut:
    sf = request.app.state.session_factory
    with sf() as session:
        require_owner(workspace_id=workspace_id, user=user, session=session)

    content_type = (file.content_type or "").lower()
    if content_type not in _ALLOWED_MIMES:
        raise HTTPException(status_code=400, detail=f"unsupported content_type: {content_type!r}")
    data = await _read_capped(file, _MAX_BYTES)  # HTTPException(400) over the 5 MB cap
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    fmt, width_px, height_px = _probe_image_format(data)  # HTTPException(400) on bad image
    detected_mime = _PIL_FORMAT_TO_MIME[fmt]
    if detected_mime != content_type:
        raise HTTPException(
            status_code=400,
            detail=(
                f"content_type {content_type!r} does not match the actual "
                f"image format {detected_mime!r}"
            ),
        )

    suffix = _SUFFIX_BY_MIME[content_type]
    aid = uuid.uuid4()
    storage = _storage_for(request)
    safe_filename = _sanitize_filename(file.filename or f"attachment{suffix}")
    relative_storage_path = f"{aid}{suffix}"

    try:
        storage.write_atomic(workspace_id=workspace_id, attachment_id=aid, suffix=suffix, data=data)
    except OSError as e:
        raise HTTPException(status_code=500, detail="storage write failed") from e

    tokens_estimate = estimate_image_tokens(width_px, height_px)
    with sf() as session:
        att = Attachment(
            id=aid,
            workspace_id=workspace_id,
            kind="figure",
            filename=safe_filename,
            storage_path=relative_storage_path,
            mime_type=content_type,
            uploaded_by=user.id,
            width_px=width_px,
            height_px=height_px,
            tokens_estimate=tokens_estimate,
        )
        session.add(att)
        try:
            session.commit()
        except Exception as e:
            session.rollback()
            try:
                storage.delete_one(workspace_id=workspace_id, attachment_id=aid, suffix=suffix)
            except OSError:
                pass  # orphan file harmless — no row references it
            raise HTTPException(status_code=500, detail="failed to persist attachment") from e

    return FileOut(attachment_id=str(aid), filename=safe_filename, mime_type=content_type)


@router.delete("/workspaces/{workspace_id}/files/{attachment_id}", status_code=204)
def api_delete_file(
    workspace_id: uuid.UUID,
    attachment_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> Response:
    sf = request.app.state.session_factory
    suffix = None
    with sf() as session:
        require_owner(workspace_id=workspace_id, user=user, session=session)
        att = session.get(Attachment, attachment_id)
        if att is None or att.workspace_id != workspace_id:
            return Response(status_code=204)  # idempotent: nothing to delete
        suffix = Path(att.storage_path).suffix.lower()
        session.delete(att)  # row-first delete (source of truth)
        session.commit()

    storage = _storage_for(request)
    try:
        storage.delete_one(workspace_id=workspace_id, attachment_id=attachment_id, suffix=suffix)
    except OSError:
        pass  # best-effort unlink; the row is already gone
    return Response(status_code=204)
```

- [ ] **Step 4: Mount the router**

In `src/call_manager/web/api/__init__.py`, add `files` to the import and include it after `references`:

```python
from call_manager.web.api import calls, files, me, references, turns, workspaces
```
```python
router.include_router(references.router)
router.include_router(files.router)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_api_files.py -q`
Expected: PASS (8 tests).

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check src/call_manager/web/api/files.py src/call_manager/web/api/__init__.py tests/test_api_files.py
uv run ruff format src/call_manager/web/api/files.py src/call_manager/web/api/__init__.py tests/test_api_files.py
git add src/call_manager/web/api/files.py src/call_manager/web/api/__init__.py tests/test_api_files.py
git commit -m "feat(api): image-attachment endpoints (list/upload/delete) reusing files service"
```

---

## Task 2: Turn `attachment_ids` pass-through

**Files:**
- Modify: `src/call_manager/web/api/turns.py` (`SubmitTurnIn` + `submit_turn`)
- Test: `tests/test_api_turns.py` (append)

**Interfaces:**
- Produces: `SubmitTurnIn` gains `attachment_ids: list[str] = []`; `submit_turn` forwards `attachment_ids=[uuid.UUID(a) for a in body.attachment_ids]` to `submit_user_turn` (which builds the image blocks + enforces the 5-cap; cross-workspace/over-cap `ValueError` → the existing 400 mapping).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_turns.py` (it already imports `_app`, `_client`, `_seed` from `tests.test_api_workspaces`):

```python
def test_api_submit_turn_with_attachment_builds_image_block(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    import uuid as _uuid

    from call_manager.db.models import Attachment, ChatTurn

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "x\n")
    _ack_disclosure(pg_session_factory, ws_id, owner_id)
    aid = _uuid.uuid4()
    with pg_session_factory() as s:
        s.add(
            Attachment(
                id=aid,
                workspace_id=ws_id,
                kind="figure",
                filename="f.png",
                storage_path=f"{aid}.png",
                mime_type="image/png",
                uploaded_by=owner_id,
                width_px=10,
                height_px=10,
                tokens_estimate=80,
            )
        )
        s.commit()
    r = _client(app, owner_id).post(
        f"/api/workspaces/{ws_id}/turns",
        json={"content": "what is in this figure?", "attachment_ids": [str(aid)]},
    )
    assert r.status_code == 200, r.text
    with pg_session_factory() as s:
        turn = s.scalars(
            select(ChatTurn)
            .where(ChatTurn.workspace_id == ws_id, ChatTurn.role == "user")
            .order_by(ChatTurn.position.desc())
        ).first()
        assert isinstance(turn.content, list)
        assert {"type": "image", "attachment_id": str(aid)} in turn.content


def test_api_submit_turn_cross_workspace_attachment_is_400(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    import uuid as _uuid

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "x\n")
    _ack_disclosure(pg_session_factory, ws_id, owner_id)
    r = _client(app, owner_id).post(
        f"/api/workspaces/{ws_id}/turns",
        json={"content": "hi", "attachment_ids": [str(_uuid.uuid4())]},
    )
    assert r.status_code == 400
```

> If `test_api_turns.py` does not already `from sqlalchemy import select`, add it. **Disclosure gate (do not skip):** `_seed`/`make_workspace` create an `active`-phase workspace, where `submit_turn` returns **403** (not 409) until the owner acks disclosure. Both tests above MUST call `_ack_disclosure(pg_session_factory, ws_id, owner_id)` immediately after `_seed` — the helper is already defined at module scope in this file (≈ line 16) and is used by the existing green `test_submit_turn_queues_job_and_echoes`. Without it the 403 fires before `attachment_ids` is ever exercised, and both asserts (200 / 400) fail for the wrong reason — a false red that does not prove the feature.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_turns.py -k "attachment" -x -q`
Expected: FAIL — `attachment_ids` rejected by the model / not applied.

- [ ] **Step 3: Implement — extend `SubmitTurnIn` + the handler**

In `src/call_manager/web/api/turns.py`, add the field to `SubmitTurnIn`:

```python
class SubmitTurnIn(BaseModel):
    content: str
    reference_ids: list[str] = []
    attachment_ids: list[str] = []
```

And in `submit_turn`, pass it through in the `submit_user_turn(...)` call (alongside `reference_ids=...`):

```python
        job = submit_user_turn(
            session=session,
            workspace_id=workspace_id,
            content_text=body.content,
            attachment_ids=[uuid.UUID(a) for a in body.attachment_ids],
            reference_ids=[uuid.UUID(r) for r in body.reference_ids],
            submitted_by_user_id=user.id,
            settings=settings,
            workspaces_dir=Path(settings.workspaces_dir),
        )
```

> The over-cap (`too many inclusions: N > 5`) and cross-workspace (`attachment X does not belong…`) `ValueError`s are already caught by the handler's `except ValueError → HTTPException(400)`. No new error handling.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_turns.py -k "attachment" -x -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/call_manager/web/api/turns.py tests/test_api_turns.py
uv run ruff format src/call_manager/web/api/turns.py tests/test_api_turns.py
git add src/call_manager/web/api/turns.py tests/test_api_turns.py
git commit -m "feat(api): accept attachment_ids on POST /api/.../turns"
```

---

## Task 3: Regenerate the OpenAPI → TypeScript schema

**Files:**
- Modify: `frontend/src/api/schema.gen.ts` (generated)

**Interfaces:**
- Produces: TS types for `FileListItemOut`, `FileOut`, the new `/api/workspaces/{workspace_id}/files*` paths, and the extended `SubmitTurnIn` (`attachment_ids`). Consumed by Task 4.

- [ ] **Step 1: Regenerate (mandatory env prefix)**

```bash
EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh
```

- [ ] **Step 2: Verify additive**

```bash
git diff --stat frontend/src/api/schema.gen.ts
git diff frontend/src/api/schema.gen.ts | grep '^-' | grep -v '^---' | grep -v '^-//' || echo "NO removed content lines"
```
Expected: additions only (new `files` paths + `FileListItemOut`/`FileOut` + `attachment_ids` on `SubmitTurnIn`). Any line that appears removed must be an openapi-typescript positional relocation (confirm the symbol still exists elsewhere), not a dropped path — verify before proceeding.

- [ ] **Step 3: Type-check + commit**

```bash
cd frontend && pnpm exec tsc --noEmit
cd .. && git add frontend/src/api/schema.gen.ts
git commit -m "chore(api): regen OpenAPI TS types for image attachments"
```

---

## Task 4: Frontend hooks (`api/files.ts`) + extend `useSubmitTurn`

**Files:**
- Create: `frontend/src/api/files.ts`
- Modify: `frontend/src/api/turns.ts` (`useSubmitTurn` accepts `attachment_ids`)
- Test: `frontend/src/api/files.test.tsx`

**Interfaces:**
- Produces: `useFiles(id)` (query `["files", id]` → `FileListItemOut[]`), `useUploadFile(id)` (multipart → `FileOut`, invalidates `["files", id]`), `useDeleteFile(id)` (DELETE → 204, invalidates). `useSubmitTurn` mutationFn accepts `{ content, reference_ids?, attachment_ids? }`. Consumed by Tasks 5-6.
- Consumes: `api`, `unwrap`, `ApiError`, `redirectToLogin` from `./client`; the Task-3 schema types.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/api/files.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import * as clientMod from "./client";
import { useDeleteFile, useFiles } from "./files";

afterEach(() => vi.restoreAllMocks());

function wrap() {
  const qc = new QueryClient();
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

it("useFiles fetches the list", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: [{ id: "a1", filename: "f.png", mime_type: "image/png", uploaded_at: "2026-06-24T00:00:00Z" }],
    error: undefined,
  } as never);
  const { result } = renderHook(() => useFiles("ws-1"), { wrapper: wrap() });
  await waitFor(() => expect(result.current.data?.[0]?.filename).toBe("f.png"));
});

it("useDeleteFile DELETEs", async () => {
  const spy = vi
    .spyOn(clientMod.api, "DELETE")
    .mockResolvedValue({ data: undefined, error: undefined } as never);
  const { result } = renderHook(() => useDeleteFile("ws-1"), { wrapper: wrap() });
  result.current.mutate("a1");
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(spy).toHaveBeenCalled();
});
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `frontend/`): `pnpm vitest run src/api/files.test.tsx`
Expected: FAIL — `./files` has no exports.

- [ ] **Step 3: Implement — create `frontend/src/api/files.ts`**

```ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, redirectToLogin, unwrap } from "./client";
import type { components } from "./schema.gen";

type FileOut = components["schemas"]["FileOut"];

async function postFile(url: string, file: File): Promise<FileOut> {
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch(url, { method: "POST", body: fd, credentials: "same-origin" });
  if (res.status === 401) {
    redirectToLogin();
    throw new ApiError("unauthorized", "session expired");
  }
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as
      | { error?: { code?: string; message?: string } }
      | null;
    throw new ApiError(body?.error?.code ?? "error", body?.error?.message ?? res.statusText);
  }
  return (await res.json()) as FileOut;
}

export function useFiles(id: string) {
  return useQuery({
    queryKey: ["files", id],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/files", {
          params: { path: { workspace_id: id } },
        }),
      ),
  });
}

export function useUploadFile(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => postFile(`/api/workspaces/${id}/files`, file),
    onSettled: () => qc.invalidateQueries({ queryKey: ["files", id] }),
  });
}

export function useDeleteFile(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (attachmentId: string) =>
      unwrap(
        await api.DELETE("/api/workspaces/{workspace_id}/files/{attachment_id}", {
          params: { path: { workspace_id: id, attachment_id: attachmentId } },
        }),
      ),
    onSettled: () => qc.invalidateQueries({ queryKey: ["files", id] }),
  });
}
```

- [ ] **Step 4: Extend `useSubmitTurn`**

In `frontend/src/api/turns.ts`, widen the mutationFn variables + body to carry `attachment_ids`:

```ts
export function useSubmitTurn(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (vars: {
      content: string;
      reference_ids?: string[];
      attachment_ids?: string[];
    }) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/turns", {
          params: { path: { workspace_id: id } },
          body: {
            content: vars.content,
            reference_ids: vars.reference_ids ?? [],
            attachment_ids: vars.attachment_ids ?? [],
          },
        }),
      ),
    onSettled: () => qc.invalidateQueries({ queryKey: ["turns", id] }),
  });
}
```

> Keep the existing hook's exact shape (the `useQueryClient`/`onSettled` lines are already there from Slice B) — only add the `attachment_ids` param and body field. If the real hook differs slightly, preserve it and add only `attachment_ids`.

- [ ] **Step 5: Run test + type-check**

```bash
pnpm vitest run src/api/files.test.tsx
pnpm exec tsc --noEmit
```
Expected: PASS + exit 0.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/files.ts frontend/src/api/turns.ts frontend/src/api/files.test.tsx
git commit -m "feat(spa): file hooks (useFiles/useUploadFile/useDeleteFile) + attachment_ids on useSubmitTurn"
```

---

## Task 5: Library **Files** section

**Files:**
- Modify: `frontend/src/cockpit/LibraryPanel.tsx` (add a Files section)
- Test: `frontend/src/cockpit/LibraryPanel.test.tsx` (append)

**Interfaces:**
- Consumes: `useFiles(id)`, `useUploadFile(id)`, `useDeleteFile(id)` (Task 4).
- Produces: a third `<section aria-label="Files">` after the References section: an upload area (file picker accepting `image/*`) and a list of attachments — thumbnail `<img src=/workspaces/{id}/files/{aid}>`, filename, and a confirm-gated delete (owner). Image bytes come from the existing Jinja serve route.

- [ ] **Step 1: Write the failing test**

Append to `frontend/src/cockpit/LibraryPanel.test.tsx` (extend the existing hoisted-mock header — add `useFiles`, `useUploadFile`, `useDeleteFile` to the `../api/files` mock and seed defaults in `beforeEach`, mirroring the `../api/references` mocks already there):

```tsx
// In the hoisted `h` object add: useFiles: vi.fn(), useUploadFileLib: vi.fn(), useDeleteFile: vi.fn()
// Add a module mock:  vi.mock("../api/files", () => ({ useFiles: h.useFiles, useUploadFile: h.useUploadFileLib, useDeleteFile: h.useDeleteFile }));
// In beforeEach seed: h.useFiles.mockReturnValue({ data: [{ id: "a1", filename: "fig.png", mime_type: "image/png", uploaded_at: "x" }] });
//   h.useUploadFileLib.mockReturnValue({ mutate: vi.fn(), isPending: false });
//   h.useDeleteFile.mockReturnValue({ mutate: vi.fn(), isPending: false });

it("renders the Files section with a thumbnail and filename", () => {
  wrap(<LibraryPanel id="ws1" />);
  expect(screen.getByText("fig.png")).toBeInTheDocument();
  const img = screen.getByRole("img", { name: /fig\.png/i }) as HTMLImageElement;
  expect(img.getAttribute("src")).toBe("/workspaces/ws1/files/a1");
});

it("deletes a file after confirm", () => {
  const del = vi.fn();
  h.useDeleteFile.mockReturnValue({ mutate: del, isPending: false });
  vi.spyOn(window, "confirm").mockReturnValue(true);
  wrap(<LibraryPanel id="ws1" />);
  fireEvent.click(screen.getByRole("button", { name: /delete fig\.png/i }));
  expect(del).toHaveBeenCalledWith("a1");
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm vitest run src/cockpit/LibraryPanel.test.tsx`
Expected: FAIL — no Files section / image / delete button.

- [ ] **Step 3: Implement — add the Files section to `LibraryPanel.tsx`**

Add imports + hooks at the top of the component, and a new `<section aria-label="Files">` after the closing of the `<section aria-label="References">` (still inside `<div className="library-panel">`):

```tsx
import { useFiles, useUploadFile, useDeleteFile } from "../api/files";
// ...inside LibraryPanel, alongside the other hooks:
const files = useFiles(id);
const uploadFile = useUploadFile(id);
const deleteFile = useDeleteFile(id);
const fileRows = Array.isArray(files.data) ? files.data : [];
```

```tsx
      <section aria-label="Files">
        <h3 className="library-section-title">Files</h3>
        <input
          aria-label="Add image file"
          type="file"
          accept="image/png,image/jpeg,image/webp,image/gif"
          disabled={uploadFile.isPending}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) uploadFile.mutate(f);
            e.target.value = "";
          }}
        />
        {fileRows.length === 0 ? (
          <p className="library-msg">No files yet.</p>
        ) : (
          <ul className="library-file-list">
            {fileRows.map((f) => (
              <li key={f.id} className="library-file-row">
                <img
                  className="library-thumb"
                  src={`/workspaces/${id}/files/${f.id}`}
                  alt={f.filename}
                />
                <span className="library-file-name">{f.filename}</span>
                <button
                  aria-label={`Delete ${f.filename}`}
                  disabled={deleteFile.isPending}
                  onClick={() => {
                    if (window.confirm(`Delete ${f.filename}?`)) deleteFile.mutate(f.id);
                  }}
                >
                  delete
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
```

> Owner-vs-viewer: the upload/delete endpoints are owner-gated server-side (a viewer gets 403). v1 shows the controls to everyone and lets the 403 surface (parity with how the Library references actions behave); a future slice can hide them using an `is_owner` read-model field. Note this in the commit.

- [ ] **Step 4: Run test + type-check**

```bash
pnpm vitest run src/cockpit/LibraryPanel.test.tsx
pnpm exec tsc --noEmit
```
Expected: PASS + exit 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/cockpit/LibraryPanel.tsx frontend/src/cockpit/LibraryPanel.test.tsx
git commit -m "feat(spa): Library Files section (upload + thumbnails + delete)"
```

---

## Task 6: Composer image affordance

**Files:**
- Modify: `frontend/src/cockpit/chat/Composer.tsx`
- Test: `frontend/src/cockpit/chat/Composer.test.tsx` (create or append)

**Interfaces:**
- Consumes: `useUploadFile(id)` (Task 4); the extended `useSubmitTurn` (`attachment_ids`).
- Produces: replaces the disabled `📎 Image` stub with a real file input (`accept=image/*`) that uploads via `useUploadFile` → `attachment_id`, shows **removable** image chips, includes `attachment_ids` in `send()`, and disables adding past the **combined** 5-cap (`picked.length + images.length >= 5`) with a hint.

- [ ] **Step 1: Write the failing test**

Create/append `frontend/src/cockpit/chat/Composer.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Composer } from "./Composer";

const h = vi.hoisted(() => ({
  submit: vi.fn(),
  upload: vi.fn(),
  uploadPending: false,
}));
vi.mock("../../api/turns", () => ({
  useSubmitTurn: () => ({ mutate: h.submit, isPending: false }),
}));
vi.mock("../../api/files", () => ({
  useUploadFile: () => ({ mutate: h.upload, isPending: h.uploadPending }),
}));

beforeEach(() => {
  h.submit.mockReset();
  h.upload.mockReset();
  h.uploadPending = false;
});
afterEach(() => vi.restoreAllMocks());

function wrap(ui: ReactNode) {
  return render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);
}

describe("Composer image attachments", () => {
  it("uploads a picked image and includes it in the turn", async () => {
    h.upload.mockImplementation((_file, opts) =>
      opts.onSuccess({ attachment_id: "a1", filename: "fig.png", mime_type: "image/png" }),
    );
    wrap(<Composer id="ws1" refs={[]} disabled={false} onSubmitted={vi.fn()} />);
    const input = screen.getByLabelText(/attach image/i) as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File([new Uint8Array([1])], "fig.png", { type: "image/png" })] },
    });
    await waitFor(() => expect(screen.getByText("fig.png")).toBeInTheDocument());
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "see fig" } });
    fireEvent.click(screen.getByRole("button", { name: /send/i }));
    expect(h.submit).toHaveBeenCalledWith(
      expect.objectContaining({ content: "see fig", attachment_ids: ["a1"] }),
      expect.anything(),
    );
  });

  it("removes an attached image chip", async () => {
    h.upload.mockImplementation((_file, opts) =>
      opts.onSuccess({ attachment_id: "a1", filename: "fig.png", mime_type: "image/png" }),
    );
    wrap(<Composer id="ws1" refs={[]} disabled={false} onSubmitted={vi.fn()} />);
    fireEvent.change(screen.getByLabelText(/attach image/i), {
      target: { files: [new File([new Uint8Array([1])], "fig.png", { type: "image/png" })] },
    });
    await waitFor(() => expect(screen.getByText("fig.png")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /remove fig\.png/i }));
    expect(screen.queryByText("fig.png")).not.toBeInTheDocument();
  });

  it("disables Send while an image upload is in flight", () => {
    // Upload started, not yet resolved: isPending=true, onSuccess hasn't fired,
    // so `images` is still empty. Sending now would post attachment_ids:[] and
    // silently drop the image — the Send guard must block until it settles.
    h.uploadPending = true;
    wrap(<Composer id="ws1" refs={[]} disabled={false} onSubmitted={vi.fn()} />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "hi" } });
    expect(screen.getByRole("button", { name: /send/i })).toBeDisabled();
  });
});
```

> Mirror the prop signature the real `Composer` already declares (`id`, `refs`, `disabled`, `onSubmitted`). If Slice B's `Composer` import path for `useSubmitTurn` is `../../api/turns`, the mock path above is correct; confirm against the file.

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm vitest run src/cockpit/chat/Composer.test.tsx`
Expected: FAIL — no image input / chips / `attachment_ids` in submit.

- [ ] **Step 3: Implement — wire image attachments into `Composer.tsx`**

Add state + the upload hook, replace the disabled `📎 Image` stub (lines ~72-74) with a real file input + a removable-chip row, fold `attachment_ids` into `send()`, and enforce the combined cap. Key pieces:

```tsx
import { useUploadFile } from "../../api/files";
// ...
const [images, setImages] = useState<{ attachment_id: string; filename: string }[]>([]);
const uploadFile = useUploadFile(id);
const inclusions = picked.length + images.length;
const atCap = inclusions >= 5;

function onPickImage(file: File) {
  if (atCap) return;
  uploadFile.mutate(file, {
    onSuccess: (r) =>
      setImages((prev) =>
        prev.some((x) => x.attachment_id === r.attachment_id)
          ? prev
          : [...prev, { attachment_id: r.attachment_id, filename: r.filename }],
      ),
  });
}
```

Replace the disabled stub with:

```tsx
        <label className={styles.attach}>
          <input
            aria-label="Attach image"
            type="file"
            accept="image/png,image/jpeg,image/webp,image/gif"
            disabled={busy || atCap || uploadFile.isPending}
            style={{ display: "none" }}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) onPickImage(f);
              e.target.value = "";
            }}
          />
          📎 Image
        </label>
```

Render image chips (in the `.picked` row, alongside the ref chips) with a remove button:

```tsx
        {images.map((im) => (
          <span key={im.attachment_id} className={styles.imgChip}>
            {im.filename}
            <button
              aria-label={`Remove ${im.filename}`}
              onClick={() => setImages((prev) => prev.filter((x) => x.attachment_id !== im.attachment_id))}
            >
              ×
            </button>
          </span>
        ))}
```

Update `send()` to include `attachment_ids` and clear images on success:

```tsx
  const send = () => {
    if (!text.trim() || busy || uploadFile.isPending) return;
    submit.mutate(
      { content: text, reference_ids: picked, attachment_ids: images.map((i) => i.attachment_id) },
      {
        onSuccess: (result) => {
          setText("");
          setPicked([]);
          setImages([]);
          onSubmitted?.(result.job_id);
        },
      },
    );
  };
```

Also update the **Send button** `disabled` to block while an image is uploading: `disabled={busy || uploadFile.isPending || !text.trim()}` (current code is `disabled={busy || !text.trim()}` at `Composer.tsx:78`). This is correctness, not polish: `images` is only populated in `uploadFile`'s `onSuccess`, so without this guard a user can pick an image and immediately Send (button **or** ⌘/Ctrl+Enter) before the upload resolves — the turn then posts with `attachment_ids: []` and silently drops the image. The `|| uploadFile.isPending` added to the `send()` guard above covers the keyboard path; this covers the button. (The textarea and ref-`<select>` intentionally keep only `busy`, so the user can keep typing while a small image uploads.)

And gate the ref-`<select>` with `disabled={busy || atCap}` — **preserve** the existing `busy` guard at `Composer.tsx:58`; do not replace it with `atCap` alone — plus a small hint when `atCap` (e.g. `{atCap && <span className={styles.hint}>Up to 5 inclusions per turn</span>}`).

> The `.picked` chip row currently only renders when `picked.length > 0`; change that condition to `picked.length > 0 || images.length > 0` so image chips show. Reuse the existing `styles` module (add `attach`/`imgChip`/`hint` classes if missing — keep the existing `.deferred` removal).

- [ ] **Step 4: Run test + type-check**

```bash
pnpm vitest run src/cockpit/chat/Composer.test.tsx
pnpm exec tsc --noEmit
```
Expected: PASS + exit 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/cockpit/chat/Composer.tsx frontend/src/cockpit/chat/Composer.test.tsx
git commit -m "feat(spa): Composer image attachments (upload, removable chips, 5-cap, attachment_ids)"
```

---

## Task 7: TurnList image-block render

**Files:**
- Modify: `frontend/src/cockpit/chat/TurnList.tsx` (thread `id`, add the `image` branch)
- Modify: `frontend/src/cockpit/chat/CockpitChat.tsx` (pass `id` to `TurnList`)
- Test: `frontend/src/cockpit/chat/TurnList.test.tsx` (append)

**Interfaces:**
- Produces: `TurnList` accepts an `id: string` (workspace id) prop; an `image` content block (`{type:"image", attachment_id}`) renders as `<img src=/workspaces/{id}/files/{attachment_id}>` instead of the muted dot fallback.

- [ ] **Step 1: Write the failing test**

Append to `frontend/src/cockpit/chat/TurnList.test.tsx`:

```tsx
it("renders an image block as an img pointing at the files serve route", () => {
  render(
    <TurnList
      id="ws1"
      turns={
        [
          {
            id: "t1",
            role: "user",
            position: 1,
            created_at: "x",
            content: [
              { type: "text", text: "see this" },
              { type: "image", attachment_id: "a1" },
            ],
          },
        ] as never
      }
      refs={[] as never}
    />,
  );
  const img = screen.getByRole("img") as HTMLImageElement;
  expect(img.getAttribute("src")).toBe("/workspaces/ws1/files/a1");
});
```

> The existing `TurnList` tests call `<TurnList turns={...} refs={...} />` without `id`; after Step 3 `id` is required, so update those existing call sites to pass `id="ws1"` (a one-token change per call) when this step makes them fail to type-check.

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm vitest run src/cockpit/chat/TurnList.test.tsx`
Expected: FAIL — no `img` (image block hits the muted fallback) and/or `id` prop unknown.

- [ ] **Step 3: Implement — thread `id` + add the image branch**

In `TurnList.tsx`, add `id` to the component + `TurnItem` props and a render branch before the unknown-block fallback:

```tsx
export function TurnList({ id, turns, refs }: { id: string; turns: Turn[]; refs: Ref[] }) {
  // ...pass id down to each <TurnItem id={id} turn={...} refs={refs} />
}

function TurnItem({ id, turn, refs }: { id: string; turn: Turn; refs: Ref[] }) {
  // ...inside the block map, before the final muted-fallback return:
  if (b.type === "image" && typeof b.attachment_id === "string") {
    return (
      <img
        key={`${turn.id}-${i}`}
        className={styles.chatImage}
        src={`/workspaces/${id}/files/${b.attachment_id}`}
        alt="attached image"
      />
    );
  }
}
```

In `CockpitChat.tsx`, pass `id` to `TurnList`:

```tsx
<TurnList id={id} turns={merged} refs={refs} />
```

> The block type is `unknown` in the generated schema; cast `b` the same way the existing branches do (the file already narrows `b.type`/`b.text`). Add a `chatImage` class to the `styles` module (max-width:100%, rounded) if absent.

- [ ] **Step 4: Run test + type-check + build**

```bash
pnpm vitest run src/cockpit/chat/TurnList.test.tsx
pnpm exec tsc --noEmit
pnpm build
```
Expected: PASS + exit 0 + build exit 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/cockpit/chat/TurnList.tsx frontend/src/cockpit/chat/CockpitChat.tsx frontend/src/cockpit/chat/TurnList.test.tsx
git commit -m "feat(spa): render chat image blocks via the files serve route"
```

---

## Final verification (before the whole-branch review)

- [ ] Backend (targeted): `uv run pytest tests/test_api_files.py tests/test_api_turns.py -q` — all green.
- [ ] Regression — Jinja files routes UNCHANGED, run them to confirm no shared-import break: `uv run pytest tests/proposal_assist/ -k "files or attachment" -q`.
- [ ] Lint: `uv run ruff check src/call_manager/web/api/ tests/test_api_files.py` — clean.
- [ ] Frontend: `cd frontend && pnpm exec tsc --noEmit && pnpm vitest run && pnpm build` — tsc 0, all vitest green, build exit 0.
- [ ] Schema additive: `git diff --stat <merge-base> -- frontend/src/api/schema.gen.ts` shows additions only; `frontend/src/api/routes.gen.ts` EMPTY diff branch-wide.
- [ ] Alembic head unchanged (no migration added).

## Self-review notes (spec coverage)

- Spec §5 (files `/api`) → Task 1 (list member / upload owner / delete owner-204; reuse `_probe_image_format`/`_read_capped`/`AttachmentStorage`/`estimate_image_tokens`; `kind="figure"`; `FileListItemOut` lean; no audit). §6 (turn `attachment_ids`) → Task 2 (pass-through; 5-cap + cross-workspace already enforced). §7 (regen) → Task 3.
- §8 (`api/files.ts` + `useSubmitTurn`) → Task 4. §9 Library Files section → Task 5. §10 (Composer + TurnList) → Tasks 6-7. Image bytes via the existing Jinja serve (Slice-G forward-dep to add `GET /api/.../files/{aid}`, tracked alongside the references `/raw`).
- Permissions: upload/delete `require_owner`, list `require_member` (Task 1 tests cover owner-200 / viewer-403 / stranger-403). No `WorkspaceEvent` for files (matches Jinja).
- Out of scope: dimensions/captions UI, batch-upload progress, paste-to-upload polish, any `/api` binary serve.
