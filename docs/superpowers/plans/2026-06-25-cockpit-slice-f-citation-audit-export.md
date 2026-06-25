# Cockpit Slice F — Citation Audit + Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface citation audit (deterministic + owner-triggered semantic) and Markdown export (raw + composed) in the React cockpit via additive `/api`, then tear down the Jinja routes/templates these replace.

**Architecture:** Two new `/api` modules (`web/api/export.py`, `web/api/citation_audit.py`) over a new thin service `proposal_assist/export.py`, reusing the existing audit engine (`citations/audit.py`) and `app.state.citation_service`. React gets a `Citations` bottom-panel tab, an `Export ▾` draft-toolbar menu, and an audit-row → peek deep-link. Per-slice Jinja teardown of the citations/export surfaces, with OpenAPI regen after the backend AND again after teardown.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy / pytest (+ testcontainers Postgres); React 19 / Mantine v9 / react-router / TanStack Query / openapi-fetch / vitest; `uv`, `ruff`, `pnpm`.

## Global Constraints

- **NO DB schema change.** No alembic migration. The audit tables (`citation_audit_verdicts` 0039, `citation_llm_usage` 0036) and engine already exist.
- **NO new dependency.** Markdown-only export (raw + composed). DOCX/PDF are explicitly out of scope.
- **Reuse `app.state.citation_service`** for the semantic deep-check (built at `main.py:485` via the supported `build_citation_service`). Do NOT add a parallel builder. `semantic_available` is `False` only when `app.state.citation_service is None`.
- **Error envelope is `{error:{code,message,detail?}}`.** Custom codes (`export_blocked`, `citation_ledger_invalid`) MUST be returned via `api_error(status, code, message, detail=...)` from `call_manager.web.api.errors` — NOT `raise HTTPException` (which would map the code to the generic `STATUS_CODES` value). The blocked-refs list rides in `error.detail.blocked`.
- **Corrupt `citations.json` → 409 `citation_ledger_invalid`** on all three new endpoints (audit GET, deep-check POST, composed export). `load_ledger` raises `ValueError`; catch it.
- **Backend tests: targeted files only** (`uv run pytest <file>`), NEVER the whole suite (it drops the testcontainers Postgres under load).
- **Lint per task:** `uv run ruff check <files>` + `uv run ruff format <files>`. F401 (unused import) fails the commit.
- **Frontend gate:** from `frontend/`, `pnpm exec tsc --noEmit` (no ESLint); `pnpm vitest run <file>` for tests; `pnpm build` where asked.
- **OpenAPI regen command:** `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh` (the `.env` mxbai default corrupts the generated TS). After it, `frontend/src/routes.gen.ts` MUST have an empty diff.
- **Export is member-gated** (owner OR viewer); **deep-check is owner-gated**; deterministic audit GET is member-gated.

---

## File Structure

**Backend — create:**
- `src/call_manager/proposal_assist/export.py` — export service (`safe_title`, `get_markdown_export`, `get_composed_export`).
- `src/call_manager/web/api/export.py` — `/api` export routes.
- `src/call_manager/web/api/citation_audit.py` — `/api` audit routes + response models.
- `tests/test_api_export.py`, `tests/test_api_citation_audit.py`.

**Backend — modify:**
- `src/call_manager/web/api/__init__.py` — mount the two routers.
- Teardown (Task 6): delete Jinja routes in `proposal_assist/web/citations.py` + `proposal_assist/web/workspaces.py`, templates, sub-nav links.

**Frontend — create:**
- `frontend/src/api/audit.ts`, `frontend/src/api/export.ts`.
- `frontend/src/cockpit/CitationsPanel.tsx` (+ `.test.tsx`).
- `frontend/src/api/export.test.ts`.

**Frontend — modify:**
- `frontend/src/cockpit/nav.ts` — `BottomTab` += `"citations"`; `NavTarget` += `evidence_id?`.
- `frontend/src/cockpit/BottomPanel.tsx` (+ `.test.tsx`) — Citations tab button + body + badge.
- `frontend/src/cockpit/DraftPanel.tsx` (+ `.test.tsx`) — Export menu.
- `frontend/src/routes/Cockpit.tsx`, `frontend/src/editor/ArtifactSectionEditor.tsx` — peek deep-link.
- `frontend/src/api/schema.gen.ts` — regenerated (Tasks 2 + 6).

---

## Task 1: Export service + `/api` export endpoints

**Files:**
- Create: `src/call_manager/proposal_assist/export.py`
- Create: `src/call_manager/web/api/export.py`
- Modify: `src/call_manager/web/api/__init__.py`
- Test: `tests/test_api_export.py`

**Interfaces:**
- Produces: `safe_title(title: str) -> str`; `get_markdown_export(workspace, workspaces_dir) -> bytes | None`; `get_composed_export(*, workspace, workspaces_dir, session) -> str | None`. Routes `GET /api/workspaces/{id}/export.md`, `GET /api/workspaces/{id}/export-composed.md`.
- Consumes (existing, verified): `build_bibliography(*, proposal_text, ledger, session, workspace_id, style, repo_dir, commit) -> dict` (read `bib["bibliography_md"]`); `compose_export(*, proposal_text, ledger, session, workspace_id, style, bibliography_text) -> str`; `BuildBibliographyBlocked` (has `.blocked: list[dict]` with `{citekey, reason}`) — all in `citations/bibliography.py`. `load_ledger(text|None) -> dict` (raises `ValueError` on corrupt) in `citations/ledger.py`. `workspace_repo_dir(workspaces_dir, id)` in `proposal_assist/paths.py`. `PROPOSAL_FILENAME` in `proposal_assist/artifacts.py`; `CITATIONS_FILENAME` in `proposal_assist/editing/save.py`. `require_member` in `proposal_assist/web/_perms.py`. `api_error` in `web/api/errors.py`. `get_current_user` in `call_manager.auth.dependencies`.

- [ ] **Step 1: Write the service module**

Create `src/call_manager/proposal_assist/export.py`:

```python
"""Export orchestration — raw + composed Markdown. Thin wrappers over the
existing citations helpers, kept OUT of the web/ package so the Jinja routes can
be deleted (Slice F teardown) without touching this module. Consumed by
web/api/export.py."""

from __future__ import annotations

from sqlalchemy.orm import Session

from call_manager.db.models import Workspace
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.citations.bibliography import (
    BuildBibliographyBlocked,  # noqa: F401  (re-exported for the route's except clause)
    build_bibliography,
    compose_export,
)
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.editing.save import CITATIONS_FILENAME
from call_manager.proposal_assist.paths import workspace_repo_dir


def safe_title(title: str) -> str:
    """Sanitize a workspace title for a Content-Disposition filename."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in title)[:80] or "proposal"


def get_markdown_export(workspace: Workspace, workspaces_dir) -> bytes | None:
    """Raw proposal.md bytes; None if there is no draft yet."""
    path = workspace_repo_dir(workspaces_dir, workspace.id) / PROPOSAL_FILENAME
    if not path.exists():
        return None
    return path.read_bytes()


def get_composed_export(*, workspace: Workspace, workspaces_dir, session: Session) -> str | None:
    """Markdown with inline citations rendered + bibliography appended. None if no
    draft. Raises ValueError on corrupt citations.json; raises
    BuildBibliographyBlocked if any cited reference is not citable yet."""
    repo_dir = workspace_repo_dir(workspaces_dir, workspace.id)
    proposal_path = repo_dir / PROPOSAL_FILENAME
    if not proposal_path.exists():
        return None
    citations_path = repo_dir / CITATIONS_FILENAME
    proposal_text = proposal_path.read_text()
    ledger = load_ledger(citations_path.read_text() if citations_path.exists() else None)
    style = workspace.citation_style or "vancouver"
    bib = build_bibliography(
        proposal_text=proposal_text,
        ledger=ledger,
        session=session,
        workspace_id=workspace.id,
        style=style,
        repo_dir=repo_dir,
        commit=False,
    )
    return compose_export(
        proposal_text=proposal_text,
        ledger=ledger,
        session=session,
        workspace_id=workspace.id,
        style=style,
        bibliography_text=bib["bibliography_md"] or None,
    )
```

- [ ] **Step 2: Write the `/api` export routes**

Create `src/call_manager/web/api/export.py`:

```python
"""JSON /api Markdown export (Cockpit Slice F). Member-gated. Reuses the
proposal_assist.export service. The Jinja /workspaces/{id}/export*.md routes are
deleted in the same slice's teardown."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import User
from call_manager.proposal_assist.citations.bibliography import BuildBibliographyBlocked
from call_manager.proposal_assist.export import (
    get_composed_export,
    get_markdown_export,
    safe_title,
)
from call_manager.proposal_assist.web._perms import require_member
from call_manager.web.api.errors import api_error

router = APIRouter()


@router.get("/workspaces/{workspace_id}/export.md")
def export_markdown(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
):
    settings = request.app.state.settings
    with request.app.state.session_factory() as session:
        ws = require_member(workspace_id=workspace_id, user=user, session=session)
        data = get_markdown_export(ws, settings.workspaces_dir)
        title = safe_title(ws.title)
    if data is None:
        return api_error(404, "not_found", "no draft to export yet")
    return Response(
        content=data,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{title}.md"'},
    )


@router.get("/workspaces/{workspace_id}/export-composed.md")
def export_composed(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
):
    settings = request.app.state.settings
    with request.app.state.session_factory() as session:
        ws = require_member(workspace_id=workspace_id, user=user, session=session)
        title = safe_title(ws.title)
        try:
            composed = get_composed_export(
                workspace=ws, workspaces_dir=settings.workspaces_dir, session=session
            )
        except ValueError as exc:
            return api_error(
                409, "citation_ledger_invalid", f"citations.json could not be read: {exc}"
            )
        except BuildBibliographyBlocked as exc:
            blocked = [
                {"citekey": str(b.get("citekey")), "reason": str(b.get("reason"))}
                for b in exc.blocked
            ]
            return api_error(
                409,
                "export_blocked",
                "Some cited references are not citable yet — approve their metadata (or waive it) and retry.",
                detail={"blocked": blocked},
            )
    if composed is None:
        return api_error(404, "not_found", "no draft to export yet")
    return Response(
        content=composed,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{title}-composed.md"'},
    )
```

- [ ] **Step 3: Mount the router**

In `src/call_manager/web/api/__init__.py`, add `export` to the import line and include it. Change the import:

```python
from call_manager.web.api import (
    calls,
    export,
    files,
    me,
    references,
    rubric,
    settings,
    turns,
    workspaces,
)
```

and after `router.include_router(rubric.router)` add:

```python
router.include_router(export.router)
```

- [ ] **Step 4: Write the failing tests**

Create `tests/test_api_export.py`. Mirror the app/seed/client helpers from `tests/test_api_workspaces.py` (verbatim shapes shown):

```python
"""JSON /api Markdown export endpoints (Cockpit Slice F)."""

import uuid

from call_manager.main import create_app
from call_manager.db.models import Reference, User, Workspace, WorkspaceViewer
from call_manager.proposal_assist.citations.ledger import dump_ledger, empty_ledger
from call_manager.proposal_assist.paths import workspace_repo_dir
from fastapi.testclient import TestClient


def _app(monkeypatch, pg_url, tmp_path):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    app = create_app()
    app.state.settings.workspaces_dir = str(tmp_path)
    return app


def _seed(pg_session_factory):
    """owner + viewer + stranger + active workspace. Returns ids + ws_id."""
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        viewer = User(status="active", email="viewer@ki.se")
        stranger = User(status="active", email="stranger@ki.se")
        s.add_all([owner, viewer, stranger])
        s.flush()
        ws = Workspace(title="My Proposal", owner_id=owner.id, current_phase="active")
        s.add(ws)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=viewer.id))
        s.commit()
        return owner.id, viewer.id, stranger.id, ws.id


def _write_repo(tmp_path, ws_id, *, proposal=None, citations=None):
    repo = workspace_repo_dir(tmp_path, ws_id)
    repo.mkdir(parents=True, exist_ok=True)
    if proposal is not None:
        (repo / "proposal.md").write_text(proposal)
    if citations is not None:
        (repo / "citations.json").write_text(citations)
    return repo


def _client(app, user_id):
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=user_id))
    return c


def test_export_markdown_returns_attachment(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(pg_session_factory)
    _write_repo(tmp_path, ws_id, proposal="# Draft\n\nhello world\n")
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/export.md")
    assert r.status_code == 200, r.text
    assert r.text == "# Draft\n\nhello world\n"
    assert 'filename="My_Proposal.md"' in r.headers["content-disposition"]


def test_export_markdown_no_draft_404(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(pg_session_factory)
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/export.md")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_export_markdown_viewer_ok_stranger_forbidden(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, viewer_id, stranger_id, ws_id = _seed(pg_session_factory)
    _write_repo(tmp_path, ws_id, proposal="x\n")
    assert _client(app, viewer_id).get(f"/api/workspaces/{ws_id}/export.md").status_code == 200
    assert _client(app, stranger_id).get(f"/api/workspaces/{ws_id}/export.md").status_code == 403


def test_export_composed_no_citations_ok(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(pg_session_factory)
    _write_repo(
        tmp_path, ws_id, proposal="Hello, no citations.\n", citations=dump_ledger(empty_ledger())
    )
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/export-composed.md")
    assert r.status_code == 200, r.text
    assert "Hello, no citations." in r.text
    assert 'filename="My_Proposal-composed.md"' in r.headers["content-disposition"]


def test_export_composed_corrupt_ledger_409(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(pg_session_factory)
    _write_repo(tmp_path, ws_id, proposal="x\n", citations="{ not valid json")
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/export-composed.md")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "citation_ledger_invalid"


def test_export_composed_blocked_lists_refs(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(pg_session_factory)
    # Seed an UN-approved reference cited by the draft → build_bibliography raises Blocked.
    with pg_session_factory() as s:
        ref = Reference(
            workspace_id=ws_id,
            kind="paper",
            source_type="uploaded",
            filename="seed.pdf",
            storage_path="seed/seed.pdf",
            mime_type="application/pdf",
            citekey="smith2021",
            metadata_status="pending",
        )
        s.add(ref)
        s.flush()
        rid = str(ref.id)
        s.commit()
    ledger = empty_ledger()
    ledger["evidence"].append(
        {"id": "ev-aaaaaaaaaaaa", "citekey": "smith2021", "reference_id": rid}
    )
    _write_repo(
        tmp_path,
        ws_id,
        proposal="We argue [@smith2021]<!-- ev:ev-aaaaaaaaaaaa --> that...\n",
        citations=dump_ledger(ledger),
    )
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/export-composed.md")
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["error"]["code"] == "export_blocked"
    blocked = body["error"]["detail"]["blocked"]
    assert any(b["citekey"] == "smith2021" for b in blocked)
```

> Note for the implementer: the `test_export_composed_blocked_lists_refs` minimal ledger row carries `id`/`citekey`/`reference_id` — enough for `_resolve_cited_entries` to resolve the marker to the unapproved reference. If `build_bibliography` needs additional row fields to reach the blocked branch, reuse `build_evidence_row`/`append_evidence` exactly as `tests/proposal_assist/citations/test_audit_semantic.py` does (open that file and mirror its ledger seeding). Do NOT weaken the assertion.

- [ ] **Step 5: Run tests to verify they fail**

Run: `cd /home/parlar_ai/dev/call-monitor && uv run pytest tests/test_api_export.py -v`
Expected: collection/import error or 404s — `web/api/export.py` not yet mounted / `proposal_assist/export.py` missing.

- [ ] **Step 6: Implement** — Steps 1–3 above are the implementation. Re-run:

Run: `uv run pytest tests/test_api_export.py -v`
Expected: PASS (6 tests).

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check src/call_manager/proposal_assist/export.py src/call_manager/web/api/export.py src/call_manager/web/api/__init__.py tests/test_api_export.py
uv run ruff format src/call_manager/proposal_assist/export.py src/call_manager/web/api/export.py src/call_manager/web/api/__init__.py tests/test_api_export.py
git add src/call_manager/proposal_assist/export.py src/call_manager/web/api/export.py src/call_manager/web/api/__init__.py tests/test_api_export.py
git commit -m "feat(spa): /api Markdown export (raw + composed) + export service (Slice F)"
```

---

## Task 2: `/api` citation-audit endpoints + OpenAPI regen

**Files:**
- Create: `src/call_manager/web/api/citation_audit.py`
- Modify: `src/call_manager/web/api/__init__.py`
- Modify: `frontend/src/api/schema.gen.ts` (regen output)
- Test: `tests/test_api_citation_audit.py`

**Interfaces:**
- Produces: `GET /api/workspaces/{id}/citation-audit` → `CitationAuditOut`; `POST /api/workspaces/{id}/citation-audit/deep` → `DeepCheckOut`. Schemas `AuditIssueOut`, `CitationAuditOut`, `DeepCheckOut` (consumed by `frontend/src/api/audit.ts` in Task 3).
- Consumes (existing, verified signatures): `validate_citations_deterministic(*, proposal_text, ledger, session, workspace_id) -> list[dict]`; `validate_citations_semantic(*, proposal_text, ledger, session, session_factory, workspace_id, citation_service, deterministic_issues, validator_version=VALIDATOR_VERSION) -> list[dict]` — both in `citations/audit.py`. Issue dict keys: `type, severity, citekey, evidence_id, location, detail`. `require_member`/`require_owner` in `proposal_assist/web/_perms.py`.

- [ ] **Step 1: Write the audit module**

Create `src/call_manager/web/api/citation_audit.py`:

```python
"""JSON /api citation audit (Cockpit Slice F).

GET  …/citation-audit       member-gated, deterministic only (free, read-only).
POST …/citation-audit/deep  owner-gated, deterministic + semantic LLM judge,
                            reusing app.state.citation_service (built at startup).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import User
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.citations.audit import (
    validate_citations_deterministic,
    validate_citations_semantic,
)
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.editing.save import CITATIONS_FILENAME
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.web._perms import require_member, require_owner
from call_manager.web.api.errors import api_error

router = APIRouter()


class AuditIssueOut(BaseModel):
    type: str
    severity: str
    citekey: str | None
    evidence_id: str | None
    location: str | None
    detail: str


class CitationAuditOut(BaseModel):
    draft_present: bool
    issues: list[AuditIssueOut]
    error_count: int
    warning_count: int


class DeepCheckOut(BaseModel):
    semantic_available: bool
    checked: int
    cap_exceeded: bool
    error_count: int
    warning_count: int
    issues: list[AuditIssueOut]


def _counts(issues: list[dict]) -> tuple[int, int]:
    err = sum(1 for i in issues if i["severity"] == "error")
    warn = sum(1 for i in issues if i["severity"] == "warning")
    return err, warn


def _load(repo_dir):
    """Return (proposal_text, ledger) or None if there is no draft. Raises ValueError
    on a corrupt citations.json."""
    proposal = repo_dir / PROPOSAL_FILENAME
    if not proposal.exists():
        return None
    citations = repo_dir / CITATIONS_FILENAME
    ledger = load_ledger(citations.read_text() if citations.exists() else None)
    return proposal.read_text(), ledger


@router.get("/workspaces/{workspace_id}/citation-audit", response_model=CitationAuditOut)
def citation_audit(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
):
    settings = request.app.state.settings
    with request.app.state.session_factory() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        try:
            loaded = _load(repo_dir)
        except ValueError as exc:
            return api_error(
                409, "citation_ledger_invalid", f"citations.json could not be read: {exc}"
            )
        if loaded is None:
            return CitationAuditOut(
                draft_present=False, issues=[], error_count=0, warning_count=0
            )
        proposal_text, ledger = loaded
        issues = validate_citations_deterministic(
            proposal_text=proposal_text, ledger=ledger, session=session, workspace_id=workspace_id
        )
        err, warn = _counts(issues)
        return CitationAuditOut(
            draft_present=True, issues=issues, error_count=err, warning_count=warn
        )


@router.post("/workspaces/{workspace_id}/citation-audit/deep", response_model=DeepCheckOut)
def citation_audit_deep(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
):
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    citation_service = request.app.state.citation_service
    semantic_available = citation_service is not None
    with sf() as session:
        require_owner(workspace_id=workspace_id, user=user, session=session)
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        try:
            loaded = _load(repo_dir)
        except ValueError as exc:
            return api_error(
                409, "citation_ledger_invalid", f"citations.json could not be read: {exc}"
            )
        if loaded is None:
            return DeepCheckOut(
                semantic_available=semantic_available,
                checked=0,
                cap_exceeded=False,
                error_count=0,
                warning_count=0,
                issues=[],
            )
        proposal_text, ledger = loaded
        det = validate_citations_deterministic(
            proposal_text=proposal_text, ledger=ledger, session=session, workspace_id=workspace_id
        )
        if semantic_available:
            sem = validate_citations_semantic(
                proposal_text=proposal_text,
                ledger=ledger,
                session=session,
                session_factory=sf,
                workspace_id=workspace_id,
                citation_service=citation_service,
                deterministic_issues=det,
            )
        else:
            sem = []
        issues = det + sem
        checked = len([r for r in (ledger.get("evidence") or []) if r.get("id")])
        cap_exceeded = any(i["type"] == "audit_unavailable" for i in sem)
        err, warn = _counts(issues)
        return DeepCheckOut(
            semantic_available=semantic_available,
            checked=checked,
            cap_exceeded=cap_exceeded,
            error_count=err,
            warning_count=warn,
            issues=issues,
        )
```

- [ ] **Step 2: Mount the router**

In `src/call_manager/web/api/__init__.py`, add `citation_audit` to the import list (alphabetical: after `calls`) and `router.include_router(citation_audit.router)` after the `export` include from Task 1.

- [ ] **Step 3: Write the failing tests**

Create `tests/test_api_citation_audit.py`. Reuse the `_app`/`_client` helpers from Task 1's file shape (copy them in — small and self-contained):

```python
"""JSON /api citation-audit endpoints (Cockpit Slice F)."""

from call_manager.main import create_app
from call_manager.db.models import User, Workspace, WorkspaceViewer
from call_manager.proposal_assist.citations.ledger import dump_ledger, empty_ledger
from call_manager.proposal_assist.citations.service import CitationLLMResult
from call_manager.proposal_assist.paths import workspace_repo_dir
from fastapi.testclient import TestClient

ORPHAN_DRAFT = "We claim X [@ghost]<!-- ev:ev-aaaaaaaaaaaa -->.\n"


def _app(monkeypatch, pg_url, tmp_path):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    app = create_app()
    app.state.settings.workspaces_dir = str(tmp_path)
    return app


def _seed(pg_session_factory):
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        viewer = User(status="active", email="viewer@ki.se")
        s.add_all([owner, viewer])
        s.flush()
        ws = Workspace(title="W", owner_id=owner.id, current_phase="active")
        s.add(ws)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=viewer.id))
        s.commit()
        return owner.id, viewer.id, ws.id


def _write(tmp_path, ws_id, *, proposal=None, citations=None):
    repo = workspace_repo_dir(tmp_path, ws_id)
    repo.mkdir(parents=True, exist_ok=True)
    if proposal is not None:
        (repo / "proposal.md").write_text(proposal)
    if citations is not None:
        (repo / "citations.json").write_text(citations)


def _client(app, user_id):
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=user_id))
    return c


def test_audit_orphan_marker(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, ws_id = _seed(pg_session_factory)
    _write(tmp_path, ws_id, proposal=ORPHAN_DRAFT, citations=dump_ledger(empty_ledger()))
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/citation-audit")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["draft_present"] is True
    types = {i["type"] for i in body["issues"]}
    assert "orphan_marker" in types
    assert body["error_count"] + body["warning_count"] == len(body["issues"])


def test_audit_no_draft(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, ws_id = _seed(pg_session_factory)
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/citation-audit")
    assert r.status_code == 200
    body = r.json()
    assert body["draft_present"] is False
    assert body["issues"] == []


def test_audit_corrupt_ledger_409(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, ws_id = _seed(pg_session_factory)
    _write(tmp_path, ws_id, proposal="x\n", citations="{ not json")
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/citation-audit")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "citation_ledger_invalid"


def test_audit_viewer_can_read(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, viewer_id, ws_id = _seed(pg_session_factory)
    _write(tmp_path, ws_id, proposal="x\n", citations=dump_ledger(empty_ledger()))
    assert (
        _client(app, viewer_id).get(f"/api/workspaces/{ws_id}/citation-audit").status_code == 200
    )


def test_deep_check_viewer_forbidden(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, viewer_id, ws_id = _seed(pg_session_factory)
    _write(tmp_path, ws_id, proposal="x\n", citations=dump_ledger(empty_ledger()))
    r = _client(app, viewer_id).post(f"/api/workspaces/{ws_id}/citation-audit/deep")
    assert r.status_code == 403


def test_deep_check_no_service_deterministic_only(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    app.state.citation_service = None  # offline override → deterministic only
    owner_id, _, ws_id = _seed(pg_session_factory)
    _write(tmp_path, ws_id, proposal=ORPHAN_DRAFT, citations=dump_ledger(empty_ledger()))
    r = _client(app, owner_id).post(f"/api/workspaces/{ws_id}/citation-audit/deep")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["semantic_available"] is False
    assert body["cap_exceeded"] is False
    assert "orphan_marker" in {i["type"] for i in body["issues"]}
```

> Note: a deep-check test that actually exercises the **semantic** path (stub `app.state.citation_service` returning a `CitationLLMResult`, asserting a `semantic_*` issue + `cap_exceeded` on `cap_exceeded=True`) requires a fully *judgeable* citation (live marker + approved reference + chunk with matching hashes). Add `test_deep_check_semantic_flags` and `test_deep_check_cap_exceeded` by mirroring the EXACT seeding in `tests/proposal_assist/citations/test_audit_semantic.py` — reuse its `_StubCitationService`, `_verdict_result`, `_seed_reference`, `_row`/`build_evidence_row`, `append_evidence` helpers, write the resulting `proposal.md` + `dump_ledger(ledger)` into `workspace_repo_dir(tmp_path, ws_id)`, set `app.state.citation_service = _StubCitationService(_verdict_result("contradicts"))`, POST, and assert `semantic_available is True`, `checked == 1`, and a `semantic_*` issue is present (cap test: `_verdict_result(None, cap_exceeded=True, cap_scope="workspace")` → `cap_exceeded is True` and an `audit_unavailable` issue). Open that file first and copy its imports verbatim.

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_citation_audit.py -v`
Expected: FAIL (routes 404 / not mounted).

- [ ] **Step 5: Implement** — Steps 1–2 above. Re-run:

Run: `uv run pytest tests/test_api_citation_audit.py -v`
Expected: PASS.

- [ ] **Step 6: Lint**

```bash
uv run ruff check src/call_manager/web/api/citation_audit.py src/call_manager/web/api/__init__.py tests/test_api_citation_audit.py
uv run ruff format src/call_manager/web/api/citation_audit.py src/call_manager/web/api/__init__.py tests/test_api_citation_audit.py
```

- [ ] **Step 7: Regenerate the OpenAPI TS types**

Run: `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`
Then check the diff: `git --no-pager diff --stat frontend/src/api/schema.gen.ts frontend/src/routes.gen.ts`
Expected: `schema.gen.ts` gains the `CitationAuditOut`/`DeepCheckOut`/`AuditIssueOut` schemas + the two `citation-audit` paths + the two `export*.md` paths; `routes.gen.ts` has an EMPTY diff.

- [ ] **Step 8: Commit**

```bash
git add src/call_manager/web/api/citation_audit.py src/call_manager/web/api/__init__.py tests/test_api_citation_audit.py frontend/src/api/schema.gen.ts
git commit -m "feat(spa): /api citation audit (deterministic + owner deep-check) + OpenAPI regen (Slice F)"
```

---

## Task 3: Citations bottom-panel tab

**Files:**
- Create: `frontend/src/api/audit.ts`
- Create: `frontend/src/cockpit/CitationsPanel.tsx`, `frontend/src/cockpit/CitationsPanel.test.tsx`
- Modify: `frontend/src/cockpit/nav.ts`, `frontend/src/cockpit/BottomPanel.tsx`, `frontend/src/cockpit/BottomPanel.test.tsx`

**Interfaces:**
- Consumes: schema types `CitationAuditOut`/`DeepCheckOut`/`AuditIssueOut` (Task 2); `useCockpitMeta(id)` (`api/workspace.ts`, returns `{ role, current_phase, ... }`); `OnNavigate` (`nav.ts`).
- Produces: `useCitationAudit(id)`, `useDeepCheck(id)` (`api/audit.ts`); `<CitationsPanel id onNavigate />`.

- [ ] **Step 1: Extend nav types**

In `frontend/src/cockpit/nav.ts`, add `"citations"` to `BottomTab` and `evidence_id?` to `NavTarget`:

```typescript
export type TopTab = "draft" | "spec" | "scorecard" | "review";
export type BottomTab = "inbox" | "problems" | "activity" | "library" | "citations";
export type NavTarget = {
  top_tab?: TopTab;
  bottom_tab?: BottomTab;
  section_index?: number;
  evidence_id?: string;
};
export type OnNavigate = (target: NavTarget) => void;
```

- [ ] **Step 2: Write the api hooks**

Create `frontend/src/api/audit.ts`:

```typescript
import { useMutation, useQuery } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type AuditIssue = components["schemas"]["AuditIssueOut"];
export type CitationAudit = components["schemas"]["CitationAuditOut"];
export type DeepCheck = components["schemas"]["DeepCheckOut"];

/** GET /api/workspaces/{id}/citation-audit — deterministic, free, member-gated. */
export function useCitationAudit(id: string) {
  return useQuery({
    queryKey: ["citation-audit", id],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/citation-audit", {
          params: { path: { workspace_id: id } },
        }),
      ),
  });
}

/** POST /api/workspaces/{id}/citation-audit/deep — owner-only semantic judge. */
export function useDeepCheck(id: string) {
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/citation-audit/deep", {
          params: { path: { workspace_id: id } },
        }),
      ),
  });
}
```

- [ ] **Step 3: Write the failing CitationsPanel test**

Create `frontend/src/cockpit/CitationsPanel.test.tsx`:

```tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CitationsPanel } from "./CitationsPanel";

const h = vi.hoisted(() => ({
  audit: { isLoading: false, isError: false, data: undefined as unknown, refetch: vi.fn() },
  deep: { mutate: vi.fn(), isPending: false, data: undefined as unknown },
  meta: { data: { role: "owner" } as { role: string } | undefined },
}));

vi.mock("../api/audit", () => ({
  useCitationAudit: () => h.audit,
  useDeepCheck: () => h.deep,
}));
vi.mock("../api/workspace", () => ({ useCockpitMeta: () => h.meta }));

afterEach(() => {
  h.audit = { isLoading: false, isError: false, data: undefined, refetch: vi.fn() };
  h.deep = { mutate: vi.fn(), isPending: false, data: undefined };
  h.meta = { data: { role: "owner" } };
});

describe("CitationsPanel", () => {
  it("shows the no-draft empty state", () => {
    h.audit.data = { draft_present: false, issues: [], error_count: 0, warning_count: 0 };
    render(<CitationsPanel id="ws1" onNavigate={vi.fn()} />);
    expect(screen.getByText(/no draft yet/i)).toBeInTheDocument();
  });

  it("renders deterministic issues and routes View-source", () => {
    h.audit.data = {
      draft_present: true,
      error_count: 1,
      warning_count: 0,
      issues: [
        {
          type: "orphan_marker",
          severity: "error",
          citekey: "ghost",
          evidence_id: "ev-1",
          location: "§2",
          detail: "marker has no ledger row",
        },
      ],
    };
    const onNavigate = vi.fn();
    render(<CitationsPanel id="ws1" onNavigate={onNavigate} />);
    expect(screen.getByText("orphan_marker")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /view source/i }));
    expect(onNavigate).toHaveBeenCalledWith({ top_tab: "draft", evidence_id: "ev-1" });
  });

  it("shows Deep check for the owner and runs it", () => {
    h.audit.data = { draft_present: true, issues: [], error_count: 0, warning_count: 0 };
    render(<CitationsPanel id="ws1" onNavigate={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /deep check/i }));
    expect(h.deep.mutate).toHaveBeenCalled();
  });

  it("hides Deep check from a viewer", () => {
    h.meta = { data: { role: "viewer" } };
    h.audit.data = { draft_present: true, issues: [], error_count: 0, warning_count: 0 };
    render(<CitationsPanel id="ws1" onNavigate={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /deep check/i })).not.toBeInTheDocument();
  });

  it("supersedes with deep-check results + summary + cap banner", () => {
    h.audit.data = { draft_present: true, issues: [], error_count: 0, warning_count: 0 };
    h.deep.data = {
      semantic_available: true,
      checked: 3,
      cap_exceeded: true,
      error_count: 1,
      warning_count: 0,
      issues: [
        {
          type: "semantic_contradicts",
          severity: "error",
          citekey: "smith2021",
          evidence_id: "ev-2",
          location: "§1",
          detail: "source contradicts the claim",
        },
      ],
    };
    render(<CitationsPanel id="ws1" onNavigate={vi.fn()} />);
    expect(screen.getByText("semantic_contradicts")).toBeInTheDocument();
    expect(screen.getByText(/checked 3/i)).toBeInTheDocument();
    expect(screen.getByText(/throttled/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 4: Run test to verify it fails**

Run: `cd frontend && pnpm vitest run src/cockpit/CitationsPanel.test.tsx`
Expected: FAIL (CitationsPanel does not exist).

- [ ] **Step 5: Implement CitationsPanel**

Create `frontend/src/cockpit/CitationsPanel.tsx`:

```tsx
import { type AuditIssue, useCitationAudit, useDeepCheck } from "../api/audit";
import { useCockpitMeta } from "../api/workspace";
import type { OnNavigate } from "./nav";

export function CitationsPanel({ id, onNavigate }: { id: string; onNavigate: OnNavigate }) {
  const audit = useCitationAudit(id);
  const deep = useDeepCheck(id);
  const meta = useCockpitMeta(id);
  const isOwner = meta.data?.role === "owner";

  if (audit.isLoading) return <p aria-busy="true">Checking citations…</p>;
  if (audit.isError || !audit.data)
    return <p role="alert">Could not run the citation audit.</p>;
  if (!audit.data.draft_present)
    return <p className="citations-empty">No draft yet — citations appear once drafting begins.</p>;

  // Deep-check results (when present) supersede the deterministic baseline.
  const issues: AuditIssue[] = deep.data?.issues ?? audit.data.issues;

  return (
    <div className="citations-panel">
      <div className="citations-toolbar">
        <button type="button" onClick={() => void audit.refetch()}>
          Re-run
        </button>
        {isOwner && (
          <button type="button" onClick={() => deep.mutate()} disabled={deep.isPending}>
            {deep.isPending ? "Deep checking…" : "Deep check"}
          </button>
        )}
        {deep.data && (
          <span className="citations-summary">
            Checked {deep.data.checked} · {deep.data.error_count} error
            {deep.data.error_count === 1 ? "" : "s"} · {deep.data.warning_count} warning
            {deep.data.warning_count === 1 ? "" : "s"}
          </span>
        )}
      </div>
      {deep.data?.cap_exceeded && (
        <p className="citations-cap" role="status">
          Deep check was throttled by the daily AI budget — some citations were not judged.
        </p>
      )}
      {deep.data && !deep.data.semantic_available && (
        <p className="citations-note">
          Semantic checking is unavailable; showing deterministic results only.
        </p>
      )}
      {issues.length === 0 ? (
        <p className="citations-clean">No citation issues found.</p>
      ) : (
        <ul className="citations-issues">
          {issues.map((iss, i) => (
            <li key={i} className={`severity-${iss.severity}`}>
              <span className="issue-type">{iss.type}</span>
              {iss.citekey && <code>{iss.citekey}</code>}
              {iss.location && <span className="issue-loc">{iss.location}</span>}
              <span className="issue-detail">{iss.detail}</span>
              {iss.evidence_id && (
                <button
                  type="button"
                  onClick={() =>
                    onNavigate({ top_tab: "draft", evidence_id: iss.evidence_id ?? undefined })
                  }
                >
                  View source
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd frontend && pnpm vitest run src/cockpit/CitationsPanel.test.tsx`
Expected: PASS (5 tests).

- [ ] **Step 7: Wire the tab into BottomPanel**

In `frontend/src/cockpit/BottomPanel.tsx`: add the import `import { useCitationAudit } from "../api/audit";` and `import { CitationsPanel } from "./CitationsPanel";`. Inside the component, after the `problemsCount` line, add:

```typescript
  const audit = useCitationAudit(id);
  const auditCount = audit.data?.error_count ?? 0;
```

Add the tab button after the Library `<button>`:

```tsx
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "citations"}
            onClick={() => onTab("citations")}
          >
            Citations {auditCount}
          </button>
```

Add the body mount after the Library body line:

```tsx
          {activeTab === "citations" && <CitationsPanel id={id} onNavigate={onNavigate} />}
```

- [ ] **Step 8: Update the BottomPanel test**

In `frontend/src/cockpit/BottomPanel.test.tsx`, add a mock for the new hook so the panel renders without hitting the network, and assert the Citations tab + badge. Add to the existing `vi.mock` block:

```typescript
vi.mock("../api/audit", () => ({ useCitationAudit: () => h.audit }));
vi.mock("./CitationsPanel", () => ({ CitationsPanel: () => <div>citations-body</div> }));
```

Extend the hoisted `h` object with `audit: { data: { error_count: 2 } }`, and add a test:

```tsx
  it("exposes the Citations tab with an error-count badge", () => {
    render(
      <BottomPanel
        id="ws1"
        activeTab="citations"
        onTab={vi.fn()}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
        onNavigate={vi.fn()}
      />,
    );
    expect(screen.getByRole("tab", { name: /citations 2/i })).toBeInTheDocument();
    expect(screen.getByText("citations-body")).toBeInTheDocument();
  });
```

- [ ] **Step 9: Run tests + type-check**

```bash
cd frontend
pnpm vitest run src/cockpit/CitationsPanel.test.tsx src/cockpit/BottomPanel.test.tsx
pnpm exec tsc --noEmit
```
Expected: tests PASS, tsc clean.

- [ ] **Step 10: Commit**

```bash
git add frontend/src/api/audit.ts frontend/src/cockpit/CitationsPanel.tsx frontend/src/cockpit/CitationsPanel.test.tsx frontend/src/cockpit/nav.ts frontend/src/cockpit/BottomPanel.tsx frontend/src/cockpit/BottomPanel.test.tsx
git commit -m "feat(spa): Citations bottom-panel tab — deterministic audit + owner deep-check (Slice F)"
```

---

## Task 4: Export menu on the draft toolbar

**Files:**
- Create: `frontend/src/api/export.ts`, `frontend/src/api/export.test.ts`
- Modify: `frontend/src/cockpit/DraftPanel.tsx`
- Create: `frontend/src/cockpit/DraftPanel.test.tsx`

**Interfaces:**
- Produces: `downloadExport(workspaceId, kind: "raw" | "composed") -> Promise<ExportResult>`.
- Consumes: existing `DraftPanel` shape (`useDraft`/`useSaveSection` from `api/editor`, `ArtifactSectionEditor`).

- [ ] **Step 1: Write the failing export-helper test**

Create `frontend/src/api/export.test.ts`:

```typescript
import { afterEach, describe, expect, it, vi } from "vitest";

import { downloadExport } from "./export";

function mockFetch(res: Partial<Response> & { json?: () => Promise<unknown> }) {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(res));
}

afterEach(() => vi.unstubAllGlobals());

describe("downloadExport", () => {
  it("saves a blob on 200", async () => {
    const click = vi.fn();
    vi.spyOn(document, "createElement").mockReturnValue({
      click,
      remove: vi.fn(),
      href: "",
      download: "",
    } as unknown as HTMLAnchorElement);
    vi.stubGlobal("URL", { createObjectURL: () => "blob:x", revokeObjectURL: vi.fn() });
    mockFetch({
      ok: true,
      status: 200,
      blob: async () => new Blob(["x"]),
      headers: new Headers({ "content-disposition": 'attachment; filename="W.md"' }),
    } as unknown as Response);
    const r = await downloadExport("ws1", "raw");
    expect(r).toEqual({ ok: true });
    expect(click).toHaveBeenCalled();
  });

  it("returns the blocked list on 409 export_blocked", async () => {
    mockFetch({
      ok: false,
      status: 409,
      json: async () => ({
        error: { code: "export_blocked", message: "x", detail: { blocked: [{ citekey: "a", reason: "metadata_unapproved" }] } },
      }),
    } as unknown as Response);
    const r = await downloadExport("ws1", "composed");
    expect(r).toEqual({ ok: false, kind: "blocked", blocked: [{ citekey: "a", reason: "metadata_unapproved" }] });
  });

  it("flags a corrupt ledger on 409 citation_ledger_invalid", async () => {
    mockFetch({
      ok: false,
      status: 409,
      json: async () => ({ error: { code: "citation_ledger_invalid", message: "x" } }),
    } as unknown as Response);
    expect(await downloadExport("ws1", "composed")).toEqual({ ok: false, kind: "ledger_invalid" });
  });

  it("flags no-draft on 404", async () => {
    mockFetch({ ok: false, status: 404, json: async () => ({}) } as unknown as Response);
    expect(await downloadExport("ws1", "raw")).toEqual({ ok: false, kind: "no_draft" });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && pnpm vitest run src/api/export.test.ts`
Expected: FAIL (export.ts missing).

- [ ] **Step 3: Implement the export helper**

Create `frontend/src/api/export.ts`:

```typescript
export type ExportKind = "raw" | "composed";

export type ExportResult =
  | { ok: true }
  | { ok: false; kind: "blocked"; blocked: { citekey: string; reason: string }[] }
  | { ok: false; kind: "ledger_invalid" }
  | { ok: false; kind: "no_draft" }
  | { ok: false; kind: "error" };

function filenameFrom(res: Response, fallback: string): string {
  const cd = res.headers.get("content-disposition") ?? "";
  const m = /filename="?([^"]+?)"?(?:;|$)/.exec(cd);
  return m?.[1] ?? fallback;
}

/** Fetch an /api export and trigger a native download. openapi-fetch is awkward
 *  for binary, so this uses raw fetch (the session cookie is same-origin). */
export async function downloadExport(workspaceId: string, kind: ExportKind): Promise<ExportResult> {
  const path = kind === "raw" ? "export.md" : "export-composed.md";
  const fallback = kind === "raw" ? "proposal.md" : "proposal-composed.md";
  const res = await fetch(`/api/workspaces/${workspaceId}/${path}`, {
    credentials: "same-origin",
  });
  if (res.ok) {
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filenameFrom(res, fallback);
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    return { ok: true };
  }
  if (res.status === 404) return { ok: false, kind: "no_draft" };
  if (res.status === 409) {
    const body = (await res.json().catch(() => null)) as {
      error?: { code?: string; detail?: { blocked?: { citekey: string; reason: string }[] } };
    } | null;
    const code = body?.error?.code;
    if (code === "export_blocked")
      return { ok: false, kind: "blocked", blocked: body?.error?.detail?.blocked ?? [] };
    if (code === "citation_ledger_invalid") return { ok: false, kind: "ledger_invalid" };
  }
  return { ok: false, kind: "error" };
}
```

Run: `cd frontend && pnpm vitest run src/api/export.test.ts`
Expected: PASS (4 tests).

- [ ] **Step 4: Write the failing DraftPanel test**

Create `frontend/src/cockpit/DraftPanel.test.tsx`:

```tsx
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "../test-utils";
import { DraftPanel } from "./DraftPanel";

const h = vi.hoisted(() => ({ download: vi.fn() }));

vi.mock("../api/editor", () => ({
  useDraft: () => ({
    isLoading: false,
    isError: false,
    data: { head: "h", can_edit: true, sections: [] },
    refetch: vi.fn(),
  }),
  useSaveSection: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));
vi.mock("../editor/ArtifactSectionEditor", () => ({
  ArtifactSectionEditor: () => <div>editor</div>,
}));
vi.mock("../api/export", () => ({ downloadExport: h.download }));

afterEach(() => {
  h.download = vi.fn();
});

describe("DraftPanel export menu", () => {
  it("downloads raw markdown", async () => {
    h.download.mockResolvedValue({ ok: true });
    renderWithProviders(<DraftPanel id="ws1" />);
    fireEvent.click(screen.getByRole("button", { name: /export/i }));
    fireEvent.click(await screen.findByText(/^markdown$/i));
    await waitFor(() => expect(h.download).toHaveBeenCalledWith("ws1", "raw"));
  });

  it("surfaces the blocked-refs message", async () => {
    h.download.mockResolvedValue({
      ok: false,
      kind: "blocked",
      blocked: [{ citekey: "smith2021", reason: "metadata_unapproved" }],
    });
    renderWithProviders(<DraftPanel id="ws1" />);
    fireEvent.click(screen.getByRole("button", { name: /export/i }));
    fireEvent.click(await screen.findByText(/markdown \+ citations/i));
    expect(await screen.findByText(/smith2021/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 5: Run test to verify it fails**

Run: `cd frontend && pnpm vitest run src/cockpit/DraftPanel.test.tsx`
Expected: FAIL (no Export button).

- [ ] **Step 6: Implement the Export menu in DraftPanel**

Replace `frontend/src/cockpit/DraftPanel.tsx` with:

```tsx
import { Button, Menu } from "@mantine/core";
import { useState } from "react";

import { useDraft, useSaveSection } from "../api/editor";
import { downloadExport, type ExportKind } from "../api/export";
import { ArtifactSectionEditor } from "../editor/ArtifactSectionEditor";

export function DraftPanel({ id }: { id: string }) {
  const draft = useDraft(id);
  const save = useSaveSection(id);
  const [exportMsg, setExportMsg] = useState<string | null>(null);

  async function onExport(kind: ExportKind) {
    setExportMsg(null);
    const r = await downloadExport(id, kind);
    if (r.ok) return;
    if (r.kind === "blocked")
      setExportMsg(
        `Export blocked — approve these references first: ${r.blocked.map((b) => b.citekey).join(", ")}`,
      );
    else if (r.kind === "ledger_invalid") setExportMsg("citations.json is corrupt — re-save citations.");
    else if (r.kind === "no_draft") setExportMsg("No draft to export yet.");
    else setExportMsg("Export failed.");
  }

  if (draft.isLoading) return <main aria-busy="true">Loading cockpit…</main>;
  if (draft.isError || !draft.data) {
    return (
      <main role="alert">
        <h1>Could not load this workspace</h1>
      </main>
    );
  }
  const { head, can_edit, sections } = draft.data;
  return (
    <div className="draft-shell">
      <div className="draft-toolbar">
        <Menu position="bottom-start">
          <Menu.Target>
            <Button variant="default" size="xs">
              Export ▾
            </Button>
          </Menu.Target>
          <Menu.Dropdown>
            <Menu.Item onClick={() => void onExport("raw")}>Markdown</Menu.Item>
            <Menu.Item onClick={() => void onExport("composed")}>
              Markdown + citations & bibliography
            </Menu.Item>
          </Menu.Dropdown>
        </Menu>
        {exportMsg && (
          <span role="status" className="export-msg">
            {exportMsg}
          </span>
        )}
      </div>
      <ArtifactSectionEditor
        key={`draft:${id}`}
        workspaceId={id}
        queryKey={["draft", id]}
        head={head}
        canEdit={can_edit}
        sections={sections}
        save={save}
        refetch={draft.refetch}
        citations
      />
    </div>
  );
}
```

- [ ] **Step 7: Run tests + type-check**

```bash
cd frontend
pnpm vitest run src/cockpit/DraftPanel.test.tsx src/api/export.test.ts
pnpm exec tsc --noEmit
```
Expected: PASS, tsc clean.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/api/export.ts frontend/src/api/export.test.ts frontend/src/cockpit/DraftPanel.tsx frontend/src/cockpit/DraftPanel.test.tsx
git commit -m "feat(spa): Export menu on the draft toolbar (raw + composed Markdown) (Slice F)"
```

---

## Task 5: Audit-row → citation peek deep-link

**Files:**
- Modify: `frontend/src/routes/Cockpit.tsx`, `frontend/src/cockpit/DraftPanel.tsx`, `frontend/src/editor/ArtifactSectionEditor.tsx`
- Test: extend `frontend/src/editor/ArtifactSectionEditor.test.tsx` (mirror its existing setup)

**Interfaces:**
- Consumes: `NavTarget.evidence_id` (added in Task 3). The existing `ArtifactSectionEditor` peek state (`peekEv` / `setPeekEv` / `useCitationSource`).
- Produces: an `openPeek?: { ev: string; n: number }` prop threaded Cockpit → DraftPanel → ArtifactSectionEditor.

> This is the spec's lowest-priority, independently-droppable task. If the editor test scaffolding proves heavy, ship Tasks 1–4 + 6 and drop this; the audit rows still display citekey/location/detail (the "View source" button simply becomes inert), which is acceptable for v1.

- [ ] **Step 1: Thread `openPeek` through Cockpit**

In `frontend/src/routes/Cockpit.tsx`: add `useRef` to the React import; inside the component add peek state and handle `evidence_id` in `onNavigate`:

```tsx
  const peekNonce = useRef(0);
  const [openPeek, setOpenPeek] = useState<{ ev: string; n: number } | null>(null);

  const onNavigate = (target: NavTarget) => {
    navigate(target); // ?tab + ?sec
    if (target.bottom_tab) {
      setBottomTab(target.bottom_tab);
      setCollapsed(false);
    }
    if (target.evidence_id) {
      peekNonce.current += 1;
      setOpenPeek({ ev: target.evidence_id, n: peekNonce.current });
    }
  };
```

and pass it down: `{tab === "draft" && <DraftPanel id={id} openPeek={openPeek} />}`.

- [ ] **Step 2: Pass `openPeek` through DraftPanel**

In `DraftPanel`, change the signature to `{ id, openPeek }: { id: string; openPeek?: { ev: string; n: number } | null }` and pass `openPeek={openPeek}` to `<ArtifactSectionEditor … />`.

- [ ] **Step 3: Sync peek in ArtifactSectionEditor**

In `frontend/src/editor/ArtifactSectionEditor.tsx`: add `openPeek?: { ev: string; n: number } | null;` to `ArtifactSectionEditorProps`, destructure it, and add an effect after the `peekEv` state:

```tsx
  useEffect(() => {
    if (openPeek) setPeekEv(openPeek.ev);
  }, [openPeek?.ev, openPeek?.n]);
```

(Ensure `useEffect` is imported from `react`.)

- [ ] **Step 4: Write + run the failing test**

In `frontend/src/editor/ArtifactSectionEditor.test.tsx` (mirror the file's existing provider + CodeMirror-mock setup — open it first), add a case: render the editor with `citations` and `openPeek={{ ev: "ev-9", n: 1 }}`, mocking `useCitationSource` to return `{ data: <a source>, isLoading: false }`, and assert the peek section (`aria-label="Citation source peek"`) renders. Run:

`cd frontend && pnpm vitest run src/editor/ArtifactSectionEditor.test.tsx`
Expected: FAIL first (no openPeek handling), then PASS after Step 3.

- [ ] **Step 5: Type-check + commit**

```bash
cd frontend && pnpm exec tsc --noEmit
git add frontend/src/routes/Cockpit.tsx frontend/src/cockpit/DraftPanel.tsx frontend/src/editor/ArtifactSectionEditor.tsx frontend/src/editor/ArtifactSectionEditor.test.tsx
git commit -m "feat(spa): audit-row → citation peek deep-link (Slice F)"
```

---

## Task 6: Jinja teardown + post-teardown OpenAPI regen

**Files:**
- Delete/modify: `src/call_manager/proposal_assist/web/citations.py`, `src/call_manager/proposal_assist/web/workspaces.py` (export route only)
- Delete: `src/call_manager/web/templates/proposal_assist/workspace_citations.html`, `partials/citation_audit_results.html`, `partials/citation_reader.html`
- Modify: `workspace_main.html:19`, `workspace_files.html:13`, `workspace_settings.html:13`, `workspace_references.html:13`, `partials/artifact_pane.html`
- Modify: `frontend/src/api/schema.gen.ts` (regen)
- Migrate/delete: tests bound to the deleted routes

> **Verify reachability before deleting** (the Slice E lesson). Find where `proposal_assist/web/citations.py`'s router is registered (likely `main.py`, not the empty `proposal_assist/web/__init__.py`).

- [ ] **Step 1: Enumerate the citations.py routes**

Run: `cd /home/parlar_ai/dev/call-monitor && grep -n '@router' src/call_manager/proposal_assist/web/citations.py`
Record every route. Slice F replaces: `GET /{id}/citations` (page), `POST /{id}/citations/validate`, `GET /{id}/citations/{ev}/reader`, `GET /{id}/export-composed.md`. The `/api` source-peek + add-citation are in `web/api/workspaces.py` (NOT this file) and STAY.

- [ ] **Step 2: Delete the replaced routes**

- If the four routes above are the ONLY routes in `citations.py`: delete the whole module and remove its `import` + `include_router` registration (grep `citations` in `src/call_manager/main.py` and delete those lines). Confirm: `grep -rn 'web.citations\|web import citations\|from .citations' src/call_manager`.
- If other live routes remain: delete only the four route functions + their now-unused imports.
- In `proposal_assist/web/workspaces.py`: delete the `export_markdown` route (`@router.get("/{workspace_id}/export.md")`, ~lines 1348-1378) and prune any imports it alone used (`FileResponse`?).

- [ ] **Step 3: Delete templates + sub-nav links**

```bash
git rm src/call_manager/web/templates/proposal_assist/workspace_citations.html \
       src/call_manager/web/templates/proposal_assist/partials/citation_audit_results.html \
       src/call_manager/web/templates/proposal_assist/partials/citation_reader.html
```

Remove the `<a href="/workspaces/{{ workspace.id }}/citations">Citations</a>` line from each of: `workspace_main.html` (line 19), `workspace_files.html` (line 13), `workspace_settings.html` (line 13), `workspace_references.html` (line 13). Remove the two "Export …" links in `partials/artifact_pane.html` (the `export.md` + `export-composed.md` anchors). Do NOT delete those whole templates — that is the later workspace_main sweep.

- [ ] **Step 4: Migrate or delete the route's tests**

Run: `grep -rln 'export-composed\|/citations/validate\|citations/.*reader\|export\.md\|/citations"' tests/`
For each test file: migrate assertions to the `/api` equivalents (already covered by `tests/test_api_export.py` + `tests/test_api_citation_audit.py`) or delete the obsolete test with an explicit per-test disposition. Do NOT blanket-delete a file that also tests still-live behavior.

- [ ] **Step 5: Confirm the app imports cleanly + targeted tests pass**

```bash
uv run python -c "from call_manager.main import create_app; create_app(); print('ok')"
uv run pytest tests/test_api_export.py tests/test_api_citation_audit.py -v
uv run ruff check src/call_manager/proposal_assist/web/ src/call_manager/web/api/
```
Expected: `ok` (no orphan imports), tests PASS, lint clean.

- [ ] **Step 6: Re-run the OpenAPI regen AFTER teardown**

The deleted Jinja routes (`export.md`, `export-composed.md`, the citations page/validate/reader) must drop out of the schema.

Run: `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`
Then: `grep -c 'export_markdown_workspaces\|export_composed_workspaces\|citations_validate\|citation_reader' frontend/src/api/schema.gen.ts || true`
Expected: the OLD Jinja operationIds are GONE (only the new `/api` `export.md`/`export-composed.md` paths remain). `frontend/src/routes.gen.ts` empty diff.
Then: `cd frontend && pnpm exec tsc --noEmit && pnpm build`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(spa): tear down Jinja citations + export routes/templates/links + post-teardown OpenAPI regen (Slice F)"
```

---

## Self-Review

**Spec coverage:**
- §4.1 export service → Task 1. §4.3 export endpoints (200/404/409 export_blocked via detail.blocked/409 ledger_invalid) → Task 1. §4.2 reuse `app.state.citation_service` + §4.4 audit GET/deep + corrupt-ledger 409 + DeepCheckOut counts → Task 2. §5.1 Citations tab (deterministic auto, owner deep-check, badge, summary, cap banner) → Task 3. §5.2 Export menu + downloadExport (Content-Disposition filename, 409/404 handling) → Task 4. §5.3 peek deep-link → Task 5. §6 teardown (routes/templates/4 sub-nav links/test migration/post-teardown regen) → Task 6. §7 testing → each task's tests. No DB schema change (no migration task). ✅
- The heavy semantic deep-check + cap tests are flagged for the implementer to mirror `test_audit_semantic.py` rather than reproduced inline — that is the one place exact code depends on helpers whose internals belong to an existing harness; the instruction names the file and the exact symbols to reuse.

**Placeholder scan:** No TBD/TODO. Every code step has runnable code. The two "mirror the existing harness" notes (composed-blocked extra seeding; semantic deep-check tests) point at a named file + named symbols — not vague "add tests".

**Type consistency:** `AuditIssueOut`/`CitationAuditOut`/`DeepCheckOut` field names match between the pydantic models (Task 2), the TS types (`components["schemas"][...]`, Task 3), and the panel usage. `downloadExport(workspaceId, kind)` signature + `ExportResult` union match between `export.ts` (Task 4), its test, and DraftPanel. `NavTarget.evidence_id` added in Task 3, consumed by CitationsPanel (Task 3) and Cockpit (Task 5). `openPeek: { ev, n }` shape consistent across Cockpit/DraftPanel/ArtifactSectionEditor (Task 5). Endpoint paths (`/citation-audit`, `/citation-audit/deep`, `/export.md`, `/export-composed.md`) consistent between backend routes, hooks, and the raw-fetch helper.
