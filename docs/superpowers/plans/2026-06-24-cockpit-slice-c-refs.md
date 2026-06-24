# Cockpit Slice C-refs — Reference Management (`/api`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the reference write/management set (add via upload + identifier-fetch, approve/waive metadata, change kind, delete, reprocess) into the React cockpit at parity with the Jinja workspace UI, via additive typed `/api` endpoints and a new bottom-panel **Library** tab.

**Architecture:** A new **writes-only** `/api` router (`web/api/references.py`) whose handlers **reuse the existing reference SERVICE functions directly** (`ingest_bytes`, `detect_source_type`/`dispatch_fetch`, `delete_or_tombstone_reference`, `reprocess_reference`, `generate_citekey`, `_parse_authors`, `_read_capped`) and **inline the thin per-route orchestration** (acquire `limiter.slot()`, the drag-drop dedup SELECT, the 50 MB cap, the audit write, the metadata IntegrityError retry) **mirroring the proven Jinja flow step-for-step**. The existing read endpoint `GET /api/workspaces/{id}/references` is **additively extended** so the Library can render management fields. **Slice C touches ZERO Jinja routes and extracts NO new shared helper** — the whole web app is being retired to React and these Jinja routes are deleted at Slice G; the short-lived Jinja↔`/api` duplication (audit `kind` literals pinned by tests on both sides) collapses to one copy then. Frontend: React Query mutation hooks (`api/references.ts`) + a `LibraryPanel` registered as a fourth bottom-panel tab, sharing the existing `["references", id]` cache with the sidebar.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2 / Postgres (testcontainers) / `uv` / `ruff` / pytest · React 19 + Vite + react-router-dom 7 + TanStack Query 5 + Mantine v9 + Vitest + Testing Library · `openapi-fetch` against `schema.gen.ts`.

## Global Constraints

Copied verbatim from the spec (`docs/superpowers/specs/2026-06-24-cockpit-slice-c-library-design.md` §16). Every task's requirements implicitly include this section.

- **No DB schema change**; alembic head untouched (stays at the current head; do NOT create a migration).
- **Touch ZERO Jinja routes and extract NO new shared helper** — reuse the existing service functions directly and inline the thin per-route orchestration (mirroring the proven Jinja flow). These Jinja reference routes are deleted at Slice G; the short-lived Jinja↔`/api` duplication is intentional and self-resolving.
- **New `/api` module** `src/call_manager/web/api/references.py` mounted via `web/api/__init__.py`; do NOT append to the already-large `workspaces.py`. The **read** `list_references` + `ReferenceListItemOut` stay in `workspaces.py` and are extended **additively** (Task 1).
- **No global `OverCapacity` handler exists** — every ingest/fetch/reprocess handler catches `OverCapacity` locally → `HTTPException(429, …, headers={"Retry-After": "2"})`. The error envelope `{error:{code,message,detail?}}` is produced by the global `StarletteHTTPException` handler; handlers just `raise HTTPException`.
- **Exact audit `kind` strings must match the Jinja routes verbatim** (the Activity feed `proposal_assist/activity/feed.py:73` maps the literals; a drift silently breaks the feed): `reference.added`, `reference.metadata_approved`, `reference_metadata_waived` / `reference_metadata_waiver_cleared` (waiver on/off — **underscore** form, not dots), `reference.kind_changed`, `reference.reprocessed`, `reference.tombstoned` / `reference.deleted`. The §14 audit tests assert these literals.
- **OpenAPI regen requires `EMBEDDING_MODEL=bge-m3`** (the `.env` mxbai default logs a WARNING to stdout that corrupts `openapi-typescript`); additive diff only.
- **No `routes.gen.ts` change** (no new SPA routes — the Library tab is in-cockpit).
- **Permissions mirror Jinja exactly:** references writes are **member**-gated (`require_member` = owner OR `WorkspaceViewer`); lists are member/viewer.
- **Backend tests:** targeted files only (the whole suite drops the testcontainers Postgres under load); `ruff check` + `ruff format` touched files (F401 fails the gate).
- **Frontend:** Node 22 + `pnpm`; `pnpm exec tsc --noEmit` is the gate (strict + `noUnusedLocals`/`noUnusedParameters` — no unused imports/params); `pnpm vitest run <file>`; `pnpm build`.

## Reuse map (exact signatures — call these; do not reimplement)

- `ingest_bytes(*, session_factory, session, storage, embedder, workspace_id, kind, source_type, source_uri, filename, mime_type, data, uploaded_by, source_meta=None, citation_service=None, extract_timeout_seconds=30.0, auto_classify_kind=False) -> IngestResult` — `src/call_manager/proposal_assist/references/ingest.py:307`. `IngestResult{reference_id: uuid.UUID, text_status: str ('done'|'failed'), text_error: str|None}` (`ingest.py:61`).
- `reprocess_reference(*, session_factory, storage, embedder, reference_id, workspace_id, citation_service, extract_timeout_seconds=30.0) -> IngestResult` — `ingest.py:403`. Raises `ReprocessConflict` (`ingest.py:398`) when the ref is not in `text_status='failed'`.
- `suffix_for_mime(mime: str) -> str` — `ingest.py:83`.
- `detect_source_type(input_str: str) -> str` — `references/dispatcher.py:30`; `ValueError` on unrecognized input. `dispatch_fetch(input) -> (bytes, filename, mime_type, source_meta)` (same module). `FetchError` (`fetcher.py:52`), `SSRFError(FetchError)` (`fetcher.py:56`).
- `delete_or_tombstone_reference(*, session, repo_dir, reference_id, workspace_id, storage=None, actor_id=None) -> str` (`"tombstoned"|"deleted"`) — `references/deletion.py:41`; `ValueError` for invalid/cross-workspace/missing; **writes its own `reference.tombstoned`/`reference.deleted` audit row in-tx** (do NOT write an extra one).
- `generate_citekey(csl: dict, workspace_id, session) -> str` — `references/csl.py:300`.
- `OverCapacity` — `references/admission.py:15`; raised by `request.app.state.reference_ingest_limiter.slot()` (a context manager; threading semaphore, non-blocking).
- From `proposal_assist/web/references.py` (import as-is; these are module-level): `_parse_authors(text: str) -> list[dict]` (:447), `_read_capped(upload: UploadFile, max_bytes: int) -> bytes` (async, :113, raises `HTTPException(400)` over cap), `_sanitize_filename(name: str) -> str` (:101), `_storage_for(request) -> ReferenceStorage` (:109), `_ALLOWED_MIMES` (:68), `_MAX_BYTES = 50*1024*1024` (:83).
- `require_member(*, workspace_id, user, session) -> Workspace` — `proposal_assist/web/_perms.py` (404 if ws missing/soft-deleted, 403 if not owner and not viewer).
- `workspace_repo_dir(workspaces_dir, workspace_id)` — `proposal_assist/paths.py`.
- app.state: `session_factory`, `settings` (`.reference_extract_timeout_seconds`, `.workspaces_dir`), `embed_client`, `citation_service`, `reference_ingest_limiter`. Embedder: `OllamaEmbedderAdapter(request.app.state.embed_client)` (`references/embed.py`).
- `/api` idioms (from `web/api/turns.py`): `from call_manager.auth.dependencies import get_current_user`; `from call_manager.db.models import Reference, User, WorkspaceEvent`; handlers are `def`/`async def` with `user: User = Depends(get_current_user)`, `request: Request`, and call `require_member(...)` inside the body; `with sf() as session:` from `request.app.state.session_factory`.

---

## Task 1: Extend the `ReferenceListItemOut` read-model (Library fields)

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py:925-962` (`ReferenceListItemOut` + `list_references`)
- Test: `tests/test_api_workspaces.py` (add one test)

**Interfaces:**
- Produces: `ReferenceListItemOut` gains `metadata_status: str`, `metadata_waiver: bool`, `text_error: str | None = None`, `filename: str`. Consumed by the frontend Library rows (Task 9) via the existing `useReferences` hook; the sidebar `useReferences` is unaffected (ignores extra fields).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_workspaces.py` (reuses the file's existing `_app`/`_seed`/`_client` helpers):

```python
def test_references_list_includes_library_fields(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    from call_manager.db.models import Reference

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "x\n")
    with pg_session_factory() as s:
        s.add(
            Reference(
                workspace_id=ws_id,
                kind="paper",
                source_type="uploaded",
                filename="seed.pdf",
                storage_path="seed/seed.pdf",
                mime_type="application/pdf",
                text_status="failed",
                text_error="Boom: bad pdf",
                metadata_status="pending",
            )
        )
        s.commit()
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/references")
    assert r.status_code == 200
    row = r.json()[0]
    assert row["metadata_status"] == "pending"
    assert row["metadata_waiver"] is False
    assert row["text_error"] == "Boom: bad pdf"
    assert row["filename"] == "seed.pdf"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_workspaces.py -k references_list_includes_library_fields -x -q`
Expected: FAIL — `KeyError`/validation: the response has no `metadata_status` (the model lacks the field).

- [ ] **Step 3: Implement — extend the model and the construction**

In `src/call_manager/web/api/workspaces.py`, replace the `ReferenceListItemOut` class (lines 925-930):

```python
class ReferenceListItemOut(BaseModel):
    id: str
    citekey: str | None = None
    title: str
    kind: str
    text_status: str
    # Slice C (Library) additive fields — sidebar useReferences ignores these.
    metadata_status: str
    metadata_waiver: bool
    text_error: str | None = None
    filename: str
```

And in `list_references`, extend the appended object (lines 953-960):

```python
            out.append(
                ReferenceListItemOut(
                    id=str(ref.id),
                    citekey=ref.citekey,
                    title=str(title),
                    kind=ref.kind,
                    text_status=ref.text_status,
                    metadata_status=ref.metadata_status,
                    metadata_waiver=ref.metadata_waiver,
                    text_error=ref.text_error,
                    filename=ref.filename,
                )
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_workspaces.py -k "references_list_includes_library_fields or draft_returns" -q`
Expected: PASS (the new test + a pre-existing one as a smoke check).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py
uv run ruff format src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py
git add src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py
git commit -m "feat(api): extend ReferenceListItemOut with Library management fields"
```

---

## Task 2: Create `web/api/references.py`, mount it, add `/api` upload

**Files:**
- Create: `src/call_manager/web/api/references.py`
- Modify: `src/call_manager/web/api/__init__.py` (import + mount)
- Create: `tests/test_api_references.py`

**Interfaces:**
- Produces: `router` (an `APIRouter`) with `POST /api/workspaces/{workspace_id}/references` (multipart `file`, `kind`, `auto_classify`) → `IngestResultOut {reference_id, text_status, text_error}`. Module-level `IngestResultOut` model and the module imports are consumed by Tasks 3-6.
- Consumes: the reuse map above.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_references.py`:

```python
"""JSON /api reference write/management endpoints (Cockpit Slice C-refs)."""

from io import BytesIO

from sqlalchemy import func, select

from call_manager.db.models import Reference, WorkspaceEvent
from call_manager.proposal_assist.references.storage import ReferenceStorage
from tests.proposal_assist._refs_helpers import (
    FakeEmbedClient,
    add_member,
    authed_client,
    build_app,
)
from tests.proposal_assist.conftest import make_user, make_workspace
from tests.proposal_assist.references.test_reprocess import _insert_failed_ref


def _real_pdf_bytes() -> bytes:
    from reportlab.pdfgen.canvas import Canvas

    buf = BytesIO()
    c = Canvas(buf)
    c.drawString(72, 720, "A real PDF paragraph for the api reference tests.")
    c.save()
    return buf.getvalue()


def _app(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    app.state.embed_client = FakeEmbedClient()
    app.state.citation_service = None  # skip ingest Phase 3
    return app


def _setup(pg_session_factory, tmp_path):
    """Return (storage, ws_id, owner_id, member_id, stranger_id). member is a viewer."""
    storage = ReferenceStorage(root=tmp_path)
    with pg_session_factory() as s:
        owner = make_user(s, email="owner@ki.se")
        member = make_user(s, email="member@ki.se")
        stranger = make_user(s, email="stranger@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        add_member(s, workspace_id=ws.id, user=member)  # member becomes a viewer
        return storage, ws.id, owner.id, member.id, stranger.id


def _seed_ref(pg_session_factory, storage, ws_id, *, text_status="failed", **flip):
    """Seed one reference via the proven `_insert_failed_ref` (stores real bytes so
    reprocess/delete work). Optionally flip text_status / other fields. Returns id (str)."""
    with pg_session_factory() as s:
        rid = _insert_failed_ref(s, storage, workspace_id=ws_id, data=b"# H\n\nbody")
        if text_status != "failed" or flip:
            ref = s.get(Reference, rid)
            ref.text_status = text_status
            for k, v in flip.items():
                setattr(ref, k, v)
            s.commit()
    return str(rid)


def test_api_upload_creates_reference_and_audits(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_id, _, member_id, _ = _setup(pg_session_factory, tmp_path)
    r = authed_client(app, user_id=member_id).post(
        f"/api/workspaces/{ws_id}/references",
        data={"kind": "paper", "auto_classify": "false"},
        files={"file": ("p.pdf", _real_pdf_bytes(), "application/pdf")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text_status"] == "done"
    rid = body["reference_id"]
    with pg_session_factory() as s:
        ev = s.scalar(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id,
                WorkspaceEvent.kind == "reference.added",
            )
        )
        assert ev is not None
        assert ev.payload["reference_id"] == rid


def test_api_upload_bad_mime_is_400(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_id, _, member_id, _ = _setup(pg_session_factory, tmp_path)
    r = authed_client(app, user_id=member_id).post(
        f"/api/workspaces/{ws_id}/references",
        data={"kind": "paper", "auto_classify": "false"},
        files={"file": ("x.bin", b"data", "application/octet-stream")},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_api_upload_non_member_is_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_id, _, _, stranger_id = _setup(pg_session_factory, tmp_path)
    r = authed_client(app, user_id=stranger_id).post(
        f"/api/workspaces/{ws_id}/references",
        data={"kind": "paper", "auto_classify": "false"},
        files={"file": ("p.pdf", _real_pdf_bytes(), "application/pdf")},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_api_upload_dragdrop_dedup(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)
    c = authed_client(app, user_id=owner_id)
    pdf = _real_pdf_bytes()
    for _ in range(2):
        r = c.post(
            f"/api/workspaces/{ws_id}/references",
            data={"auto_classify": "true"},
            files={"file": ("p.pdf", pdf, "application/pdf")},
        )
        assert r.status_code == 200, r.text
    with pg_session_factory() as s:
        n = s.scalar(select(func.count()).select_from(Reference).where(Reference.workspace_id == ws_id))
    assert n == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_references.py -x -q`
Expected: FAIL — 404 on the POST (route not mounted yet).

- [ ] **Step 3: Implement — create the module**

Create `src/call_manager/web/api/references.py`:

```python
"""JSON /api surface for reference write/management (Cockpit Library, Slice C).

Member-gated mutations that REUSE the existing reference service functions
(ingest_bytes, dispatch_fetch, delete_or_tombstone_reference, reprocess_reference,
generate_citekey, _parse_authors) and inline the same per-route orchestration the
Jinja routes use (limiter slot, dedup, 50 MB cap, audit). The Jinja reference
routes coexist until Slice G deletes them; the short-lived duplication of the
audit `kind` literals is intentional and pinned by tests on both sides.
"""

from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import Reference, User, WorkspaceEvent
from call_manager.proposal_assist.references.admission import OverCapacity
from call_manager.proposal_assist.references.embed import OllamaEmbedderAdapter
from call_manager.proposal_assist.references.ingest import ingest_bytes, suffix_for_mime
from call_manager.proposal_assist.references.kinds import ALLOWED_KINDS
from call_manager.proposal_assist.web._perms import require_member
from call_manager.proposal_assist.web.references import (
    _ALLOWED_MIMES,
    _MAX_BYTES,
    _read_capped,
    _sanitize_filename,
    _storage_for,
)

router = APIRouter()


class _BadUpload(Exception):
    """A 400-class upload validation failure raised from inside the threadpool."""


class IngestResultOut(BaseModel):
    reference_id: str
    text_status: str
    text_error: str | None = None


@router.post("/workspaces/{workspace_id}/references", response_model=IngestResultOut)
async def api_upload_reference(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
    kind: str = Form("paper"),
    auto_classify: bool = Form(False),
    file: UploadFile = File(...),  # noqa: B008
) -> IngestResultOut:
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)

    content_type = (file.content_type or "").lower()
    if content_type not in _ALLOWED_MIMES:
        raise HTTPException(status_code=400, detail=f"unsupported content_type: {content_type!r}")
    if not auto_classify and kind not in ALLOWED_KINDS:
        raise HTTPException(status_code=400, detail=f"invalid kind: {kind!r}")

    data = await _read_capped(file, _MAX_BYTES)  # raises HTTPException(400) over the 50 MB cap
    filename = _sanitize_filename(file.filename or f"reference{suffix_for_mime(content_type)}")

    factory = request.app.state.session_factory
    storage = _storage_for(request)
    embedder = OllamaEmbedderAdapter(request.app.state.embed_client)
    limiter = request.app.state.reference_ingest_limiter
    citation_service = request.app.state.citation_service
    timeout = request.app.state.settings.reference_extract_timeout_seconds

    def _run() -> IngestResultOut:
        with limiter.slot():
            if auto_classify:
                if not data.startswith(b"%PDF-"):
                    raise _BadUpload("drag-drop accepts real PDF files only.")
                sha = hashlib.sha256(data).hexdigest()
                with factory() as sdup:
                    dup = sdup.scalar(
                        select(Reference)
                        .where(
                            Reference.workspace_id == workspace_id,
                            Reference.original_sha256 == sha,
                            Reference.tombstoned_at.is_(None),
                        )
                        .order_by(Reference.created_at.desc())
                    )
                    if dup is not None:
                        return IngestResultOut(
                            reference_id=str(dup.id), text_status=dup.text_status, text_error=None
                        )
            with factory() as own:
                result = ingest_bytes(
                    session_factory=factory,
                    session=own,
                    storage=storage,
                    embedder=embedder,
                    workspace_id=workspace_id,
                    kind="paper" if auto_classify else kind,
                    source_type="uploaded",
                    source_uri=None,
                    filename=filename,
                    mime_type=content_type,
                    data=data,
                    uploaded_by=user.id,
                    citation_service=citation_service,
                    extract_timeout_seconds=timeout,
                    auto_classify_kind=auto_classify,
                )
        with factory() as sev:
            sev.add(
                WorkspaceEvent(
                    workspace_id=workspace_id,
                    kind="reference.added",
                    actor_id=user.id,
                    payload={"reference_id": str(result.reference_id)},
                )
            )
            sev.commit()
        return IngestResultOut(
            reference_id=str(result.reference_id),
            text_status=result.text_status,
            text_error=result.text_error,
        )

    try:
        return await run_in_threadpool(_run)
    except OverCapacity:
        raise HTTPException(
            status_code=429, detail="server busy", headers={"Retry-After": "2"}
        ) from None
    except _BadUpload as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
```

- [ ] **Step 4: Mount the router**

In `src/call_manager/web/api/__init__.py`, add `references` to the import and include it after `turns`:

```python
from call_manager.web.api import calls, me, references, turns, workspaces
```
```python
router.include_router(turns.router)
router.include_router(references.router)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_api_references.py -x -q`
Expected: PASS (4 tests).

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check src/call_manager/web/api/references.py src/call_manager/web/api/__init__.py tests/test_api_references.py
uv run ruff format src/call_manager/web/api/references.py src/call_manager/web/api/__init__.py tests/test_api_references.py
git add src/call_manager/web/api/references.py src/call_manager/web/api/__init__.py tests/test_api_references.py
git commit -m "feat(api): POST /api/workspaces/{id}/references (upload), reuse ingest_bytes"
```

---

## Task 3: `/api` identifier-fetch (DOI / PMID / arXiv / URL)

**Files:**
- Modify: `src/call_manager/web/api/references.py` (append `FetchIn` + handler)
- Test: `tests/test_api_references.py` (append)

**Interfaces:**
- Produces: `POST /api/workspaces/{workspace_id}/references/fetch` body `{input: str, kind: str = "paper"}` → `IngestResultOut`. Error mapping: invalid source → 400, `SSRFError` → 400, `FetchError` → 502, `OverCapacity` → 429.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_references.py`:

```python
def test_api_fetch_invalid_source_is_400(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/references/fetch",
        json={"input": "not a doi or url", "kind": "paper"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_api_fetch_success_audits(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)
    pdf = _real_pdf_bytes()

    def _fake_dispatch(_input):
        return pdf, "fetched.pdf", "application/pdf", {"source": "crossref"}

    monkeypatch.setattr(
        "call_manager.proposal_assist.references.dispatcher.dispatch_fetch", _fake_dispatch
    )
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/references/fetch",
        json={"input": "10.1000/xyz123", "kind": "paper"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["text_status"] == "done"
    with pg_session_factory() as s:
        ev = s.scalar(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id, WorkspaceEvent.kind == "reference.added"
            )
        )
        assert ev is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_references.py -k fetch -x -q`
Expected: FAIL — 404 (route not added yet).

- [ ] **Step 3: Implement — append to `references.py`**

```python
class FetchIn(BaseModel):
    input: str
    kind: str = "paper"


@router.post("/workspaces/{workspace_id}/references/fetch", response_model=IngestResultOut)
async def api_fetch_reference(
    workspace_id: uuid.UUID,
    body: FetchIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> IngestResultOut:
    from call_manager.proposal_assist.references.dispatcher import (
        detect_source_type,
        dispatch_fetch,
    )
    from call_manager.proposal_assist.references.fetcher import FetchError, SSRFError

    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
    if body.kind not in ALLOWED_KINDS:
        raise HTTPException(status_code=400, detail=f"invalid kind: {body.kind!r}")
    try:
        detect_source_type(body.input)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None

    factory = request.app.state.session_factory
    storage = _storage_for(request)
    embedder = OllamaEmbedderAdapter(request.app.state.embed_client)
    limiter = request.app.state.reference_ingest_limiter
    citation_service = request.app.state.citation_service
    timeout = request.app.state.settings.reference_extract_timeout_seconds

    def _run():
        with limiter.slot():
            fetch_data, filename, mime_type, source_meta = dispatch_fetch(body.input)
            with factory() as own:
                result = ingest_bytes(
                    session_factory=factory,
                    session=own,
                    storage=storage,
                    embedder=embedder,
                    workspace_id=workspace_id,
                    kind=body.kind,
                    source_type=source_meta["source"],
                    source_uri=body.input,
                    filename=_sanitize_filename(filename),
                    mime_type=mime_type,
                    data=fetch_data,
                    uploaded_by=user.id,
                    source_meta=source_meta,
                    citation_service=citation_service,
                    extract_timeout_seconds=timeout,
                )
        with factory() as sev:
            sev.add(
                WorkspaceEvent(
                    workspace_id=workspace_id,
                    kind="reference.added",
                    actor_id=user.id,
                    payload={"reference_id": str(result.reference_id)},
                )
            )
            sev.commit()
        return result

    try:
        result = await run_in_threadpool(_run)
    except OverCapacity:
        raise HTTPException(
            status_code=429, detail="server busy", headers={"Retry-After": "2"}
        ) from None
    except SSRFError as e:  # subclass of FetchError — must precede the FetchError arm
        raise HTTPException(status_code=400, detail=str(e)) from None
    except FetchError as e:
        raise HTTPException(status_code=502, detail=f"fetch failed: {e}") from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return IngestResultOut(
        reference_id=str(result.reference_id),
        text_status=result.text_status,
        text_error=result.text_error,
    )
```

> Note: the test monkeypatches `dispatcher.dispatch_fetch`; the handler imports it inside the function body (as the Jinja route does), so the patch is seen at call time.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_references.py -k fetch -x -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/call_manager/web/api/references.py tests/test_api_references.py
uv run ruff format src/call_manager/web/api/references.py tests/test_api_references.py
git add src/call_manager/web/api/references.py tests/test_api_references.py
git commit -m "feat(api): POST /api/.../references/fetch (DOI/PMID/arXiv/URL)"
```

---

## Task 4: `/api` metadata approve (CSL + citekey + IntegrityError retry inlined)

**Files:**
- Modify: `src/call_manager/web/api/references.py` (append `MetadataIn`/`MetadataOut` + handler)
- Test: `tests/test_api_references.py` (append)

**Interfaces:**
- Produces: `POST /api/workspaces/{workspace_id}/references/{reference_id}/metadata` body `{title: str, authors: list[str] = [], year?, venue?, doi?}` → `MetadataOut {citekey, metadata_status, csl_json}`. Errors: missing title → 400, not-found/tombstoned → 404.
- The handler reuses `_parse_authors` (on `"\n".join(authors)`) + `generate_citekey`, and **inlines both IntegrityError-retry mechanics** (the citekey-collision retry lives inside `generate_citekey`; the audit-commit retry is the inline `try/except IntegrityError` loop).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_references.py`:

```python
def test_api_metadata_approve_sets_citekey_and_audits(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)
    rid = _seed_ref(pg_session_factory, storage, ws_id)
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/references/{rid}/metadata",
        json={"title": "On Widgets", "authors": ["Roe, Jane"], "year": "2021"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["metadata_status"] == "approved"
    assert body["citekey"] == "roe2021"
    assert body["csl_json"]["title"] == "On Widgets"
    with pg_session_factory() as s:
        ev = s.scalar(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id,
                WorkspaceEvent.kind == "reference.metadata_approved",
            )
        )
        assert ev is not None and ev.payload["reference_id"] == rid


def test_api_metadata_empty_title_is_400(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)
    rid = _seed_ref(pg_session_factory, storage, ws_id)
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/references/{rid}/metadata",
        json={"title": "   ", "authors": []},
    )
    assert r.status_code == 400


def test_api_metadata_unknown_ref_is_404(monkeypatch, pg_url, tmp_path, pg_session_factory):
    import uuid as _uuid

    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/references/{_uuid.uuid4()}/metadata",
        json={"title": "X", "authors": []},
    )
    assert r.status_code == 404
```

> `_seed_ref` and `_setup` are defined in the Task 2 test-file header (above) — reuse them; do not redefine.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_references.py -k metadata -x -q`
Expected: FAIL — 404/405 (route not added).

- [ ] **Step 3: Implement — append to `references.py`**

```python
class MetadataIn(BaseModel):
    title: str
    authors: list[str] = []
    year: str | None = None
    venue: str | None = None
    doi: str | None = None


class MetadataOut(BaseModel):
    citekey: str | None
    metadata_status: str
    csl_json: dict


@router.post(
    "/workspaces/{workspace_id}/references/{reference_id}/metadata", response_model=MetadataOut
)
def api_approve_metadata(
    workspace_id: uuid.UUID,
    reference_id: uuid.UUID,
    body: MetadataIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> MetadataOut:
    import contextlib

    from sqlalchemy.exc import IntegrityError

    from call_manager.proposal_assist.references.csl import generate_citekey
    from call_manager.proposal_assist.web.references import _parse_authors

    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        ref = session.scalar(select(Reference).where(Reference.id == reference_id))
        if ref is None or ref.workspace_id != workspace_id or ref.tombstoned_at is not None:
            raise HTTPException(status_code=404, detail="reference not found")

        csl: dict = {}
        title = (body.title or "").strip()
        if title:
            csl["title"] = title
        parsed_authors = _parse_authors("\n".join(body.authors))
        if parsed_authors:
            csl["author"] = parsed_authors
        year_raw = (body.year or "").strip()
        if year_raw:
            with contextlib.suppress(ValueError):
                csl["issued"] = {"date-parts": [[int(year_raw)]]}
        venue = (body.venue or "").strip()
        if venue:
            csl["container-title"] = venue
        doi = (body.doi or "").strip()
        if doi:
            csl["DOI"] = doi

        if not csl or not csl.get("title"):
            raise HTTPException(status_code=400, detail="provide at least a title before approving.")

        ref.csl_json = csl
        if isinstance(csl.get("title"), str) and csl["title"]:
            ref.title = csl["title"]
        ref.metadata_status = "approved"
        if ref.citekey is None or ref.citekey.startswith("ref-"):
            new_key = generate_citekey(csl, workspace_id, session)
            if new_key and not new_key.startswith("ref-"):
                ref.citekey = new_key

        try:
            session.add(
                WorkspaceEvent(
                    workspace_id=workspace_id,
                    kind="reference.metadata_approved",
                    actor_id=user.id,
                    payload={"reference_id": str(reference_id)},
                )
            )
            session.commit()
        except IntegrityError:
            session.rollback()
            ref = session.scalar(select(Reference).where(Reference.id == reference_id))
            ref.csl_json = csl
            if isinstance(csl.get("title"), str) and csl["title"]:
                ref.title = csl["title"]
            ref.metadata_status = "approved"
            session.add(
                WorkspaceEvent(
                    workspace_id=workspace_id,
                    kind="reference.metadata_approved",
                    actor_id=user.id,
                    payload={"reference_id": str(reference_id)},
                )
            )
            session.commit()

        return MetadataOut(
            citekey=ref.citekey, metadata_status=ref.metadata_status, csl_json=csl
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_references.py -k metadata -x -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/call_manager/web/api/references.py tests/test_api_references.py
uv run ruff format src/call_manager/web/api/references.py tests/test_api_references.py
git add src/call_manager/web/api/references.py tests/test_api_references.py
git commit -m "feat(api): POST /api/.../references/{id}/metadata (approve + citekey)"
```

---

## Task 5: `/api` waiver + kind

**Files:**
- Modify: `src/call_manager/web/api/references.py` (append)
- Test: `tests/test_api_references.py` (append)

**Interfaces:**
- Produces: `POST .../references/{rid}/waiver` body `{waiver: bool, reason?: str}` → `WaiverOut {metadata_waiver, reason}` (audit `reference_metadata_waived` / `reference_metadata_waiver_cleared`); `PATCH .../references/{rid}/kind` body `{kind: str}` → `KindOut {kind}` (audit `reference.kind_changed`). Bad kind → 400, not-found → 404.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_references.py`:

```python
def test_api_waiver_on_then_off_audits(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)
    rid = _seed_ref(pg_session_factory, storage, ws_id)
    c = authed_client(app, user_id=owner_id)

    r1 = c.post(
        f"/api/workspaces/{ws_id}/references/{rid}/waiver",
        json={"waiver": True, "reason": "preprint"},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["metadata_waiver"] is True
    assert r1.json()["reason"] == "preprint"

    r2 = c.post(f"/api/workspaces/{ws_id}/references/{rid}/waiver", json={"waiver": False})
    assert r2.status_code == 200
    assert r2.json()["metadata_waiver"] is False
    assert r2.json()["reason"] is None

    with pg_session_factory() as s:
        kinds = set(
            s.scalars(
                select(WorkspaceEvent.kind).where(WorkspaceEvent.workspace_id == ws_id)
            ).all()
        )
    assert "reference_metadata_waived" in kinds
    assert "reference_metadata_waiver_cleared" in kinds


def test_api_update_kind_audits_and_validates(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)
    rid = _seed_ref(pg_session_factory, storage, ws_id, kind="paper")
    c = authed_client(app, user_id=owner_id)

    ok = c.patch(f"/api/workspaces/{ws_id}/references/{rid}/kind", json={"kind": "data"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["kind"] == "data"

    bad = c.patch(f"/api/workspaces/{ws_id}/references/{rid}/kind", json={"kind": "nope"})
    assert bad.status_code == 400

    with pg_session_factory() as s:
        ev = s.scalar(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id,
                WorkspaceEvent.kind == "reference.kind_changed",
            )
        )
        assert ev is not None and ev.payload["to"] == "data"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_references.py -k "waiver or update_kind" -x -q`
Expected: FAIL — routes not present.

- [ ] **Step 3: Implement — append to `references.py`**

```python
_WAIVER_MAX_REASON = 500  # mirrors proposal_assist/web/references.py:_MAX_WAIVER_REASON_LEN


class WaiverIn(BaseModel):
    waiver: bool
    reason: str | None = None


class WaiverOut(BaseModel):
    metadata_waiver: bool
    reason: str | None = None


@router.post(
    "/workspaces/{workspace_id}/references/{reference_id}/waiver", response_model=WaiverOut
)
def api_set_waiver(
    workspace_id: uuid.UUID,
    reference_id: uuid.UUID,
    body: WaiverIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> WaiverOut:
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        ref = session.scalar(select(Reference).where(Reference.id == reference_id))
        if ref is None or ref.workspace_id != workspace_id or ref.tombstoned_at is not None:
            raise HTTPException(status_code=404, detail="reference not found")
        ref.metadata_waiver = body.waiver
        ref.metadata_waiver_reason = (
            ((body.reason or "").strip()[:_WAIVER_MAX_REASON] or None) if body.waiver else None
        )
        session.add(
            WorkspaceEvent(
                workspace_id=workspace_id,
                kind="reference_metadata_waived"
                if body.waiver
                else "reference_metadata_waiver_cleared",
                actor_id=user.id,
                payload={
                    "reference_id": str(ref.id),
                    "waiver": body.waiver,
                    "reason": ref.metadata_waiver_reason,
                },
            )
        )
        session.commit()
        return WaiverOut(metadata_waiver=ref.metadata_waiver, reason=ref.metadata_waiver_reason)


class KindIn(BaseModel):
    kind: str


class KindOut(BaseModel):
    kind: str


@router.patch(
    "/workspaces/{workspace_id}/references/{reference_id}/kind", response_model=KindOut
)
def api_update_kind(
    workspace_id: uuid.UUID,
    reference_id: uuid.UUID,
    body: KindIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> KindOut:
    if body.kind not in ALLOWED_KINDS:
        raise HTTPException(status_code=400, detail=f"invalid kind: {body.kind!r}")
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        ref = session.scalar(select(Reference).where(Reference.id == reference_id))
        if ref is None or ref.workspace_id != workspace_id or ref.tombstoned_at is not None:
            raise HTTPException(status_code=404, detail="reference not found")
        old = ref.kind
        ref.kind = body.kind
        session.add(
            WorkspaceEvent(
                workspace_id=workspace_id,
                kind="reference.kind_changed",
                actor_id=user.id,
                payload={"reference_id": str(ref.id), "from": old, "to": body.kind},
            )
        )
        session.commit()
        return KindOut(kind=body.kind)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_references.py -k "waiver or update_kind" -x -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/call_manager/web/api/references.py tests/test_api_references.py
uv run ruff format src/call_manager/web/api/references.py tests/test_api_references.py
git add src/call_manager/web/api/references.py tests/test_api_references.py
git commit -m "feat(api): POST .../waiver + PATCH .../kind reference endpoints"
```

---

## Task 6: `/api` delete + reprocess

**Files:**
- Modify: `src/call_manager/web/api/references.py` (append + extend imports)
- Test: `tests/test_api_references.py` (append)

**Interfaces:**
- Produces: `POST .../references/{rid}/delete` → `DeleteResultOut {outcome: "deleted"|"tombstoned"}` (`delete_or_tombstone_reference` writes its own `reference.tombstoned`/`reference.deleted` audit); not-found → 404. `POST .../references/{rid}/reprocess` → `IngestResultOut` (audit `reference.reprocessed`); `ReprocessConflict` → 409, `OverCapacity` → 429, not-found → 404.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_references.py`:

```python
def test_api_delete_uncited_hard_deletes(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)
    rid = _seed_ref(pg_session_factory, storage, ws_id)  # fresh, uncited → hard delete
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/references/{rid}/delete"
    )
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "deleted"
    with pg_session_factory() as s:
        assert s.get(Reference, rid) is None
        ev = s.scalar(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id, WorkspaceEvent.kind == "reference.deleted"
            )
        )
        assert ev is not None


def test_api_reprocess_conflict_when_not_failed(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)
    rid = _seed_ref(pg_session_factory, storage, ws_id, text_status="done")
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/references/{rid}/reprocess"
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


def test_api_reprocess_failed_ref_audits(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)
    # _seed_ref stores real bytes via _insert_failed_ref and leaves the ref in
    # 'failed', so reprocess re-extracts → 'done' (FakeEmbedClient; Phase 3 skipped).
    rid = _seed_ref(pg_session_factory, storage, ws_id)
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/references/{rid}/reprocess"
    )
    assert r.status_code == 200, r.text
    assert r.json()["text_status"] == "done"
    with pg_session_factory() as s:
        ev = s.scalar(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id, WorkspaceEvent.kind == "reference.reprocessed"
            )
        )
        assert ev is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_references.py -k "delete or reprocess" -x -q`
Expected: FAIL — routes not present.

- [ ] **Step 3: Implement — extend imports + append handlers**

Add to the top-of-file imports in `references.py`:

```python
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.references.deletion import delete_or_tombstone_reference
from call_manager.proposal_assist.references.ingest import ReprocessConflict, reprocess_reference
```

(Merge the two `references.ingest` imports into one: `from ...ingest import ReprocessConflict, ingest_bytes, reprocess_reference, suffix_for_mime`.)

Append:

```python
class DeleteResultOut(BaseModel):
    outcome: str  # "deleted" | "tombstoned"


@router.post(
    "/workspaces/{workspace_id}/references/{reference_id}/delete", response_model=DeleteResultOut
)
def api_delete_reference(
    workspace_id: uuid.UUID,
    reference_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> DeleteResultOut:
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        storage = _storage_for(request)
        repo_dir = workspace_repo_dir(request.app.state.settings.workspaces_dir, workspace_id)
        try:
            outcome = delete_or_tombstone_reference(
                session=session,
                repo_dir=repo_dir,
                reference_id=reference_id,
                workspace_id=workspace_id,
                storage=storage,
                actor_id=user.id,
            )
        except ValueError:
            raise HTTPException(status_code=404, detail="reference not found") from None
    return DeleteResultOut(outcome=outcome)


@router.post(
    "/workspaces/{workspace_id}/references/{reference_id}/reprocess",
    response_model=IngestResultOut,
)
async def api_reprocess_reference(
    workspace_id: uuid.UUID,
    reference_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> IngestResultOut:
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        ref = session.scalar(select(Reference).where(Reference.id == reference_id))
        if ref is None or ref.workspace_id != workspace_id or ref.tombstoned_at is not None:
            raise HTTPException(status_code=404, detail="reference not found")

    factory = request.app.state.session_factory
    storage = _storage_for(request)
    embedder = OllamaEmbedderAdapter(request.app.state.embed_client)
    limiter = request.app.state.reference_ingest_limiter
    citation_service = request.app.state.citation_service
    timeout = request.app.state.settings.reference_extract_timeout_seconds

    def _run():
        with limiter.slot():
            return reprocess_reference(
                session_factory=factory,
                storage=storage,
                embedder=embedder,
                reference_id=reference_id,
                workspace_id=workspace_id,
                citation_service=citation_service,
                extract_timeout_seconds=timeout,
            )

    try:
        result = await run_in_threadpool(_run)
    except OverCapacity:
        raise HTTPException(
            status_code=429, detail="server busy", headers={"Retry-After": "2"}
        ) from None
    except ReprocessConflict:
        raise HTTPException(
            status_code=409, detail="reference is not in a reprocessable state"
        ) from None

    with factory() as sev:
        sev.add(
            WorkspaceEvent(
                workspace_id=workspace_id,
                kind="reference.reprocessed",
                actor_id=user.id,
                payload={"reference_id": str(reference_id)},
            )
        )
        sev.commit()
    return IngestResultOut(
        reference_id=str(result.reference_id),
        text_status=result.text_status,
        text_error=result.text_error,
    )
```

- [ ] **Step 4: Run the whole new test file**

Run: `uv run pytest tests/test_api_references.py -q`
Expected: PASS (all tests across Tasks 2-6).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/call_manager/web/api/references.py tests/test_api_references.py
uv run ruff format src/call_manager/web/api/references.py tests/test_api_references.py
git add src/call_manager/web/api/references.py tests/test_api_references.py
git commit -m "feat(api): POST .../delete + .../reprocess reference endpoints"
```

---

## Task 7: Regenerate the OpenAPI → TypeScript schema

**Files:**
- Modify: `frontend/src/api/schema.gen.ts` (generated)

**Interfaces:**
- Produces: TS types for `IngestResultOut`, `FetchIn`, `MetadataIn`/`MetadataOut`, `WaiverIn`/`WaiverOut`, `KindIn`/`KindOut`, `DeleteResultOut`, the extended `ReferenceListItemOut`, and the new `/api/workspaces/{workspace_id}/references*` paths. Consumed by Task 8.

- [ ] **Step 1: Regenerate (mandatory env prefix)**

Run from the repo root:
```bash
EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh
```

- [ ] **Step 2: Verify the diff is additive only**

```bash
git diff --stat frontend/src/api/schema.gen.ts
git diff frontend/src/api/schema.gen.ts | grep '^-' | grep -v '^---' | grep -v '^-//' || echo "NO removed lines — additive"
```
Expected: only added lines (`metadata_status`/`metadata_waiver`/`text_error`/`filename` on `ReferenceListItemOut`, plus the new schemas/paths). If any existing line was removed, STOP and investigate (likely the `bge-m3` prefix was missing → stdout corruption).

- [ ] **Step 3: Type-check**

```bash
cd frontend && pnpm exec tsc --noEmit
```
Expected: exit 0.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/schema.gen.ts
git commit -m "chore(api): regen OpenAPI TS types for reference write/management"
```

---

## Task 8: Frontend mutation hooks (`api/references.ts`)

**Files:**
- Create: `frontend/src/api/references.ts`
- Test: `frontend/src/api/references.test.tsx`

**Interfaces:**
- Produces: `useUploadReference(id)`, `useFetchReference(id)`, `useApproveMetadata(id)`, `useToggleWaiver(id)`, `useUpdateKind(id)`, `useDeleteReference(id)`, `useReprocessReference(id)` — each a `useMutation` that invalidates `["references", id]` on settle. Upload uses a raw multipart `fetch`; the rest use `openapi-fetch` + `unwrap`. Consumed by Tasks 9-10.
- Consumes: `api`, `unwrap`, `ApiError` from `./client`; the schema types from Task 7.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/api/references.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import * as clientMod from "./client";
import { useFetchReference, useUpdateKind } from "./references";

afterEach(() => vi.restoreAllMocks());

function wrap() {
  const qc = new QueryClient();
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

it("useUpdateKind PATCHes and returns the new kind", async () => {
  const spy = vi
    .spyOn(clientMod.api, "PATCH")
    .mockResolvedValue({ data: { kind: "data" }, error: undefined } as never);
  const { result } = renderHook(() => useUpdateKind("ws-1"), { wrapper: wrap() });
  result.current.mutate({ referenceId: "r1", kind: "data" });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data?.kind).toBe("data");
  expect(spy).toHaveBeenCalled();
});

it("useFetchReference POSTs the identifier", async () => {
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: { reference_id: "r2", text_status: "done", text_error: null },
    error: undefined,
  } as never);
  const { result } = renderHook(() => useFetchReference("ws-1"), { wrapper: wrap() });
  result.current.mutate({ input: "10.1/x", kind: "paper" });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data?.reference_id).toBe("r2");
});
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `frontend/`): `pnpm vitest run src/api/references.test.tsx`
Expected: FAIL — `./references` has no exports.

- [ ] **Step 3: Implement — create `frontend/src/api/references.ts`**

```ts
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, unwrap } from "./client";
import type { components } from "./schema.gen";

type IngestResult = components["schemas"]["IngestResultOut"];

async function postMultipart(url: string, fd: FormData): Promise<IngestResult> {
  const res = await fetch(url, { method: "POST", body: fd, credentials: "same-origin" });
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as
      | { error?: { code?: string; message?: string } }
      | null;
    throw new ApiError(body?.error?.code ?? "error", body?.error?.message ?? res.statusText);
  }
  return (await res.json()) as IngestResult;
}

function invalidateRefs(id: string) {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: ["references", id] });
}

export function useUploadReference(id: string) {
  const onSettled = invalidateRefs(id);
  return useMutation({
    mutationFn: ({ file, kind, autoClassify }: { file: File; kind: string; autoClassify: boolean }) => {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("kind", kind);
      fd.append("auto_classify", String(autoClassify));
      return postMultipart(`/api/workspaces/${id}/references`, fd);
    },
    onSettled,
  });
}

export function useFetchReference(id: string) {
  const onSettled = invalidateRefs(id);
  return useMutation({
    mutationFn: async ({ input, kind }: { input: string; kind: string }) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/references/fetch", {
          params: { path: { workspace_id: id } },
          body: { input, kind },
        }),
      ),
    onSettled,
  });
}

export function useApproveMetadata(id: string) {
  const onSettled = invalidateRefs(id);
  return useMutation({
    mutationFn: async (v: {
      referenceId: string;
      title: string;
      authors: string[];
      year?: string | null;
      venue?: string | null;
      doi?: string | null;
    }) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/references/{reference_id}/metadata", {
          params: { path: { workspace_id: id, reference_id: v.referenceId } },
          body: {
            title: v.title,
            authors: v.authors,
            year: v.year ?? null,
            venue: v.venue ?? null,
            doi: v.doi ?? null,
          },
        }),
      ),
    onSettled,
  });
}

export function useToggleWaiver(id: string) {
  const onSettled = invalidateRefs(id);
  return useMutation({
    mutationFn: async (v: { referenceId: string; waiver: boolean; reason?: string | null }) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/references/{reference_id}/waiver", {
          params: { path: { workspace_id: id, reference_id: v.referenceId } },
          body: { waiver: v.waiver, reason: v.reason ?? null },
        }),
      ),
    onSettled,
  });
}

export function useUpdateKind(id: string) {
  const onSettled = invalidateRefs(id);
  return useMutation({
    mutationFn: async (v: { referenceId: string; kind: string }) =>
      unwrap(
        await api.PATCH("/api/workspaces/{workspace_id}/references/{reference_id}/kind", {
          params: { path: { workspace_id: id, reference_id: v.referenceId } },
          body: { kind: v.kind },
        }),
      ),
    onSettled,
  });
}

export function useDeleteReference(id: string) {
  const onSettled = invalidateRefs(id);
  return useMutation({
    mutationFn: async (referenceId: string) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/references/{reference_id}/delete", {
          params: { path: { workspace_id: id, reference_id: referenceId } },
        }),
      ),
    onSettled,
  });
}

export function useReprocessReference(id: string) {
  const onSettled = invalidateRefs(id);
  return useMutation({
    mutationFn: async (referenceId: string) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/references/{reference_id}/reprocess", {
          params: { path: { workspace_id: id, reference_id: referenceId } },
        }),
      ),
    onSettled,
  });
}
```

> **Implementer:** every generated-client mutation is `async` and `await`s `api.POST`/`api.PATCH` before `unwrap` — `unwrap` is synchronous and expects the resolved `{data,error}` envelope; without the `await` the mutation resolves with `undefined` (matches the existing `unwrap(await api.GET(...))` read-hook pattern). `invalidateRefs` calls `useQueryClient()` so it MUST be invoked at the top of each hook (Rules of Hooks) — it is, above. If `tsc` rejects any `api.POST`/`api.PATCH` path-string or `body` shape, the Task 7 regen is stale or the path string is mistyped; fix the call, not the schema. Confirm `ApiError`/`unwrap`/`api` are exported from `./client` (they are: `client.ts:6/8/34`).

- [ ] **Step 4: Run test + type-check**

```bash
pnpm vitest run src/api/references.test.tsx
pnpm exec tsc --noEmit
```
Expected: PASS + exit 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/references.ts frontend/src/api/references.test.tsx
git commit -m "feat(spa): reference write/management mutation hooks (api/references.ts)"
```

---

## Task 9: Register the **Library** bottom-panel tab + references table

**Files:**
- Modify: `frontend/src/cockpit/nav.ts` (extend `BottomTab`)
- Modify: `frontend/src/cockpit/BottomPanel.tsx` (tab button + render branch)
- Create: `frontend/src/cockpit/LibraryPanel.tsx`
- Test: `frontend/src/cockpit/LibraryPanel.test.tsx`

**Interfaces:**
- Consumes: `useReferences(id)` (existing, `api/citations.ts:35`, returns the Task-1-extended `ReferenceListItemOut[]`, query key `["references", id]`); `useUpdateKind` (Task 8). Cockpit's `bottomTab` state is already typed `BottomTab` (`routes/Cockpit.tsx:18`) — extending the union flows through.
- Produces: a `LibraryPanel({ id }: { id: string })` rendering the references table (label fallback `title → citekey → filename`, kind `<select>` → `useUpdateKind`, `text_status` + `metadata_status` badges, a download link to the Jinja `/raw`). The add area + write actions arrive in Task 10.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/cockpit/LibraryPanel.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LibraryPanel } from "./LibraryPanel";

// Hoisted mock fns (NOT plain factories) so a single Task-10 test can override
// one hook's return via h.useX.mockReturnValue(...). ESM-safe: no require().
const h = vi.hoisted(() => ({
  refs: {
    data: [
      {
        id: "r1",
        title: "",
        citekey: null,
        kind: "paper",
        text_status: "failed",
        metadata_status: "pending",
        metadata_waiver: false,
        text_error: "Boom",
        filename: "paper.pdf",
      },
    ],
  },
  useReferences: vi.fn(),
  useUpdateKind: vi.fn(),
  useDeleteReference: vi.fn(),
  useReprocessReference: vi.fn(),
  useToggleWaiver: vi.fn(),
  useApproveMetadata: vi.fn(),
  useUploadReference: vi.fn(),
  useFetchReference: vi.fn(),
}));

vi.mock("../api/citations", () => ({ useReferences: h.useReferences }));
vi.mock("../api/references", () => ({
  useUpdateKind: h.useUpdateKind,
  useDeleteReference: h.useDeleteReference,
  useReprocessReference: h.useReprocessReference,
  useToggleWaiver: h.useToggleWaiver,
  useApproveMetadata: h.useApproveMetadata,
  useUploadReference: h.useUploadReference,
  useFetchReference: h.useFetchReference,
}));

beforeEach(() => {
  h.useReferences.mockReturnValue(h.refs);
  for (const fn of [
    h.useUpdateKind,
    h.useDeleteReference,
    h.useReprocessReference,
    h.useToggleWaiver,
    h.useApproveMetadata,
    h.useUploadReference,
    h.useFetchReference,
  ]) {
    fn.mockReturnValue({ mutate: vi.fn(), isPending: false });
  }
});

function wrap(ui: ReactNode) {
  return render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);
}

describe("LibraryPanel references table", () => {
  it("falls back to the filename when title and citekey are empty", () => {
    wrap(<LibraryPanel id="ws1" />);
    expect(screen.getByText("paper.pdf")).toBeInTheDocument();
  });

  it("shows the text_status and metadata_status", () => {
    wrap(<LibraryPanel id="ws1" />);
    expect(screen.getByText(/failed/i)).toBeInTheDocument();
    expect(screen.getByText(/pending/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm vitest run src/cockpit/LibraryPanel.test.tsx`
Expected: FAIL — `./LibraryPanel` does not exist.

- [ ] **Step 3: Implement — `LibraryPanel.tsx` (table only)**

Create `frontend/src/cockpit/LibraryPanel.tsx`:

```tsx
import { useReferences } from "../api/citations";
import { useUpdateKind } from "../api/references";

const KINDS = ["paper", "prior_proposal", "analysis", "data"];

function rowLabel(r: { title: string; citekey: string | null; filename: string }): string {
  return r.title || r.citekey || r.filename;
}

export function LibraryPanel({ id }: { id: string }) {
  const refsQ = useReferences(id);
  const updateKind = useUpdateKind(id);
  const refs = Array.isArray(refsQ.data) ? refsQ.data : [];

  if (refsQ.isError) return <p className="library-msg">Could not load references.</p>;
  if (refsQ.isLoading) return <p className="library-msg">Loading…</p>;

  return (
    <div className="library-panel">
      <section aria-label="References">
        <h3 className="library-section-title">References</h3>
        {refs.length === 0 ? (
          <p className="library-msg">No references yet.</p>
        ) : (
          <ul className="library-list">
            {refs.map((r) => (
              <li key={r.id} className="library-row">
                <span className="library-row-label">{rowLabel(r)}</span>
                <select
                  aria-label={`Kind for ${rowLabel(r)}`}
                  value={r.kind}
                  disabled={updateKind.isPending}
                  onChange={(e) =>
                    updateKind.mutate({ referenceId: r.id, kind: e.target.value })
                  }
                >
                  {KINDS.map((k) => (
                    <option key={k} value={k}>
                      {k}
                    </option>
                  ))}
                </select>
                <span className="library-badge" data-status={r.text_status}>
                  {r.text_status}
                </span>
                <span className="library-badge" data-meta={r.metadata_status}>
                  {r.metadata_status}
                </span>
                <a
                  className="library-dl"
                  href={`/workspaces/${id}/references/${r.id}/raw`}
                  target="_blank"
                  rel="noreferrer"
                >
                  download
                </a>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
```

> Mock includes the Task-10 hooks so this file can import them in Task 10 without breaking the test; the table here only uses `useReferences` + `useUpdateKind`.

- [ ] **Step 4: Register the tab**

In `frontend/src/cockpit/nav.ts`:
```ts
export type BottomTab = "inbox" | "problems" | "activity" | "library";
```

In `frontend/src/cockpit/BottomPanel.tsx`, add the tab button alongside the others in the `role="tablist"` div:
```tsx
        <button role="tab" aria-selected={activeTab === "library"} onClick={() => onTab("library")}>
          Library
        </button>
```
and the render branch alongside the others (import `LibraryPanel` at the top):
```tsx
        {activeTab === "library" && <LibraryPanel id={id} />}
```

- [ ] **Step 5: Run test + type-check**

```bash
pnpm vitest run src/cockpit/LibraryPanel.test.tsx
pnpm exec tsc --noEmit
```
Expected: PASS + exit 0.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/cockpit/nav.ts frontend/src/cockpit/BottomPanel.tsx frontend/src/cockpit/LibraryPanel.tsx frontend/src/cockpit/LibraryPanel.test.tsx
git commit -m "feat(spa): Library bottom-panel tab + references table"
```

---

## Task 10: Library add-area, edit-metadata modal, and write actions

**Files:**
- Modify: `frontend/src/cockpit/LibraryPanel.tsx` (add the add-area, the per-row actions, the modal)
- Create: `frontend/src/cockpit/MetadataModal.tsx`
- Test: `frontend/src/cockpit/LibraryPanel.test.tsx` (append)

**Interfaces:**
- Consumes: `useUploadReference`, `useFetchReference`, `useApproveMetadata`, `useToggleWaiver`, `useDeleteReference`, `useReprocessReference` (Task 8); Mantine `Modal`, `TextInput`, `Textarea`, `Button` (`@mantine/core` v9 — available, not yet imported elsewhere).
- Produces: an add area (drag-drop dropzone with bounded-concurrency + 429-backoff queue + file picker with a reference-kind selector + an identifier-fetch input) and per-row actions: **edit metadata** (`MetadataModal` → `useApproveMetadata`), **waiver** toggle (`useToggleWaiver`), **reprocess** (shown when `text_status === "failed"`; on `ApiError.code === "conflict"` show "already processing"), **delete** (confirm → `useDeleteReference`, feedback distinguishes `tombstoned` vs `deleted`).

- [ ] **Step 1: Write the failing tests**

Append to `frontend/src/cockpit/LibraryPanel.test.tsx`:

```tsx
import { fireEvent } from "@testing-library/react";

describe("LibraryPanel actions", () => {
  it("uploads a chosen file via the picker", () => {
    const upload = vi.fn();
    h.useUploadReference.mockReturnValue({ mutate: upload, isPending: false });
    wrap(<LibraryPanel id="ws1" />);
    const input = screen.getByLabelText(/add reference file/i) as HTMLInputElement;
    const file = new File([new Uint8Array([1, 2, 3])], "a.pdf", { type: "application/pdf" });
    fireEvent.change(input, { target: { files: [file] } });
    expect(upload).toHaveBeenCalled();
  });

  it("fetches a reference by identifier", () => {
    const fetchRef = vi.fn();
    h.useFetchReference.mockReturnValue({ mutate: fetchRef, isPending: false });
    wrap(<LibraryPanel id="ws1" />);
    fireEvent.change(screen.getByLabelText(/doi.*pmid.*url/i), { target: { value: "10.1/x" } });
    fireEvent.click(screen.getByRole("button", { name: /^fetch$/i }));
    expect(fetchRef).toHaveBeenCalledWith({ input: "10.1/x", kind: expect.any(String) });
  });

  it("reprocesses a failed reference", () => {
    const reprocess = vi.fn();
    h.useReprocessReference.mockReturnValue({ mutate: reprocess, isPending: false });
    wrap(<LibraryPanel id="ws1" />);
    fireEvent.click(screen.getByRole("button", { name: /reprocess/i }));
    expect(reprocess).toHaveBeenCalledWith("r1");
  });

  it("drop queue starts at most MAX (2) uploads concurrently", () => {
    // mutate never invokes onSuccess/onError, so each upload stays 'in flight'
    // and the queue must hold the remaining files until a settle.
    const mutate = vi.fn();
    h.useUploadReference.mockReturnValue({ mutate, isPending: false });
    wrap(<LibraryPanel id="ws1" />);
    const dz = screen.getByText(/drop pdfs here/i);
    const files = [0, 1, 2, 3, 4].map(
      (i) => new File([new Uint8Array([i])], `f${i}.pdf`, { type: "application/pdf" }),
    );
    fireEvent.drop(dz, { dataTransfer: { files } });
    expect(mutate).toHaveBeenCalledTimes(2); // MAX=2; the other 3 wait
  });
});
```

> Per-test overrides use `h.useX.mockReturnValue(...)` against the hoisted mock fns declared in the Task-9 header (ESM-safe — no `require()`). `beforeEach` re-seeds defaults so an override only affects its own test. The drop-queue test pins the concurrency cap and is the test the Task-3 note promised; with the ref-based queue (next step) it passes.

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm vitest run src/cockpit/LibraryPanel.test.tsx`
Expected: FAIL — no file input / fetch input / reprocess button yet.

- [ ] **Step 3: Implement — the modal**

Create `frontend/src/cockpit/MetadataModal.tsx`:

```tsx
import { Button, Modal, TextInput, Textarea } from "@mantine/core";
import { useState } from "react";
import { useApproveMetadata } from "../api/references";

export function MetadataModal({
  id,
  referenceId,
  opened,
  onClose,
}: {
  id: string;
  referenceId: string;
  opened: boolean;
  onClose: () => void;
}) {
  const approve = useApproveMetadata(id);
  const [title, setTitle] = useState("");
  const [authors, setAuthors] = useState("");
  const [year, setYear] = useState("");
  const [venue, setVenue] = useState("");
  const [doi, setDoi] = useState("");

  return (
    <Modal opened={opened} onClose={onClose} title="Edit reference metadata">
      <TextInput label="Title" value={title} onChange={(e) => setTitle(e.currentTarget.value)} />
      <Textarea
        label="Authors (one per line: Family, Given)"
        value={authors}
        onChange={(e) => setAuthors(e.currentTarget.value)}
      />
      <TextInput label="Year" value={year} onChange={(e) => setYear(e.currentTarget.value)} />
      <TextInput label="Venue" value={venue} onChange={(e) => setVenue(e.currentTarget.value)} />
      <TextInput label="DOI" value={doi} onChange={(e) => setDoi(e.currentTarget.value)} />
      {approve.isError ? <p className="library-error">Could not save — check the title.</p> : null}
      <Button
        disabled={approve.isPending}
        onClick={() =>
          approve.mutate(
            {
              referenceId,
              title,
              authors: authors.split("\n").map((s) => s.trim()).filter(Boolean),
              year,
              venue,
              doi,
            },
            { onSuccess: onClose },
          )
        }
      >
        Save metadata
      </Button>
    </Modal>
  );
}
```

- [ ] **Step 4: Implement — extend `LibraryPanel.tsx`**

Add an add-area above the table and per-row actions. Key pieces (compose into the existing component):

```tsx
import { useRef, useState } from "react";
import { ApiError } from "../api/client";
import {
  useDeleteReference,
  useFetchReference,
  useReprocessReference,
  useToggleWaiver,
  useUploadReference,
} from "../api/references";
import { MetadataModal } from "./MetadataModal";

// Bounded-concurrency + 429-backoff upload queue (ports the Jinja app.js behavior,
// not the code) so a multi-file drop can't hammer the ingest limiter.
// The in-flight counter is a REF (not state) so pump() reads the live value, not
// a stale render closure; `uploading` state only drives the disabled affordance.
function useUploadQueue(id: string) {
  const upload = useUploadReference(id);
  const activeRef = useRef(0);
  const [uploading, setUploading] = useState(false);
  const MAX = 2;

  function enqueue(files: File[], kind: string) {
    const q = [...files];
    const settle = () => {
      activeRef.current -= 1;
      if (activeRef.current === 0 && q.length === 0) setUploading(false);
      pump();
    };
    const pump = () => {
      while (activeRef.current < MAX && q.length > 0) {
        const file = q.shift()!;
        activeRef.current += 1;
        setUploading(true);
        const run = (attempt: number) =>
          upload.mutate(
            { file, kind, autoClassify: true },
            {
              onError: (err) => {
                if (err instanceof ApiError && err.code === "too_many_requests" && attempt < 4) {
                  window.setTimeout(() => run(attempt + 1), 2000 * (attempt + 1));
                  return; // keep the slot held during backoff; do NOT settle
                }
                settle();
              },
              onSuccess: () => settle(),
            },
          );
        run(0);
      }
    };
    pump();
  }
  return { enqueue, isUploading: uploading || upload.isPending };
}
```

Add-area JSX (above the table):

```tsx
      <section aria-label="Add references" className="library-add">
        <input
          aria-label="Add reference file"
          type="file"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) uploadOne.mutate({ file: f, kind: pickerKind, autoClassify: false });
          }}
        />
        <select aria-label="New reference kind" value={pickerKind} onChange={(e) => setPickerKind(e.target.value)}>
          {KINDS.map((k) => (
            <option key={k} value={k}>{k}</option>
          ))}
        </select>
        <div
          className="library-dropzone"
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault();
            queue.enqueue(Array.from(e.dataTransfer.files), "paper");
          }}
        >
          Drop PDFs here
        </div>
        <div className="library-fetch">
          <input
            aria-label="Fetch by DOI / PMID / arXiv / URL"
            value={fetchInput}
            onChange={(e) => setFetchInput(e.target.value)}
          />
          <button onClick={() => fetchRef.mutate({ input: fetchInput, kind: pickerKind })}>Fetch</button>
        </div>
      </section>
```

Per-row actions (extend each `<li>`): an **Edit** button that opens `MetadataModal` for that ref; a **Waiver** toggle (`toggleWaiver.mutate({ referenceId: r.id, waiver: !r.metadata_waiver })`); a **Reprocess** button rendered only when `r.text_status === "failed"` (`reprocess.mutate(r.id)`); a **Delete** button behind `window.confirm` that, on success, surfaces the `{outcome}` (e.g. "Retained (still cited)" for `tombstoned` vs "Deleted" for `deleted`). Wire the hooks at the top of the component:

```tsx
  const uploadOne = useUploadReference(id);
  const queue = useUploadQueue(id);
  const fetchRef = useFetchReference(id);
  const toggleWaiver = useToggleWaiver(id);
  const reprocess = useReprocessReference(id);
  const del = useDeleteReference(id);
  const [pickerKind, setPickerKind] = useState("paper");
  const [fetchInput, setFetchInput] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
```

Delete handler:
```tsx
  function onDelete(refId: string) {
    if (!window.confirm("Delete this reference?")) return;
    del.mutate(refId, {
      onSuccess: (res) =>
        setNote(res.outcome === "tombstoned" ? "Retained (still cited)." : "Deleted."),
    });
  }
```

Render `{editing && <MetadataModal id={id} referenceId={editing} opened onClose={() => setEditing(null)} />}` and a `{note && <p className="library-note">{note}</p>}` line.

> **Implementer:** keep the table markup from Task 9; only ADD the add-area, actions, modal mount, and note. The `reprocess` conflict handling: pass `{ onError }` to `reprocess.mutate` and, when `err instanceof ApiError && err.code === "conflict"`, `setNote("Already processing.")`. Keep `useUploadQueue`'s in-flight counter in the `useRef` shown above (not state) so `pump()` reads the live count, never a stale render closure; the drop-queue cap test is what pins this — keep it green.

- [ ] **Step 5: Run test + type-check + build**

```bash
pnpm vitest run src/cockpit/LibraryPanel.test.tsx
pnpm exec tsc --noEmit
pnpm build
```
Expected: PASS + exit 0 + build exit 0.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/cockpit/LibraryPanel.tsx frontend/src/cockpit/MetadataModal.tsx frontend/src/cockpit/LibraryPanel.test.tsx
git commit -m "feat(spa): Library add-area, edit-metadata modal, and write actions"
```

---

## Final verification (before the whole-branch review)

- [ ] Backend (targeted): `uv run pytest tests/test_api_references.py tests/test_api_workspaces.py -q` — all green.
- [ ] Regression — the Jinja reference routes are UNCHANGED, but run them to confirm no shared-module import broke them: `uv run pytest tests/proposal_assist/ -k references -q`.
- [ ] Lint: `uv run ruff check src/call_manager/web/api/ tests/test_api_references.py` — clean.
- [ ] Frontend: `cd frontend && pnpm exec tsc --noEmit && pnpm vitest run && pnpm build` — tsc 0, all vitest green, build exit 0.
- [ ] Schema additive: `git diff --stat <merge-base> -- frontend/src/api/schema.gen.ts` shows additions only; `frontend/src/api/routes.gen.ts` has an EMPTY diff branch-wide.
- [ ] Alembic head unchanged (no migration added).

## Self-review notes (spec coverage)

- Spec §4 (references `/api`) → Tasks 2-6 (upload, fetch, metadata, waiver, kind, delete, reprocess) + Task 1 (Library read-model). Audit literals asserted in Tasks 2/3/4/5/6. `OverCapacity`→429 + `Retry-After` in upload/fetch/reprocess. SSRF→400 / FetchError→502 in fetch. IntegrityError retry inlined in metadata.
- Spec §8 (hooks) → Task 8. §9 (Library tab + references section) → Tasks 9-10. §7 (regen) → Task 7.
- Out of scope here (Plan C-media): §5 files `/api`, §6 turn `attachment_ids`, §10 Composer/TurnList image rendering, the Files section of the Library tab.
- Permissions: every `/api` write calls `require_member` (Tasks 2-6); non-member 403 asserted in Task 2 (the pattern carries to the rest).
