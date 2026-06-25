# Cockpit Gap-Closers (Soft-Delete + Turn-Cancel) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the two owner-only Jinja-only workspace lifecycle actions — workspace soft-delete and turn-cancel — to React + typed `/api`, at parity with today's behavior, so the G2 Jinja-UI deletion is a true orphan-removal rather than a feature regression.

**Architecture:** Extract the soft-delete orchestration into a shared `soft_delete_workspace` domain service (next to `create_workspace`); call it from a new owner-only `DELETE /api/workspaces/{id}` AND from the repointed (doomed) Jinja route — whose existing test becomes a free regression net. Add an owner-only `POST /api/workspaces/{id}/turns/{job}/cancel` that reuses `record_terminal_outcome`. Surface both in React: a Danger-zone delete in the owner settings modal and a Cancel affordance in the chat footer. Retarget the 4 kept cascade tests to the `/api` surface.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x (backend); pytest + testcontainers Postgres (backend tests); React 19 + Mantine v9 + TanStack Query + openapi-fetch (frontend); vitest + Testing Library (frontend tests).

**Source spec:** `docs/superpowers/specs/2026-06-25-cockpit-gap-closers-soft-delete-turn-cancel-design.md` (`d3c0b8d`).

## Global Constraints

- **No DB schema change. No new dependency. Additive only** — no behavior change to any existing surface.
- **Re-delete is 404, not idempotent.** `require_owner` filters `soft_deleted_at IS NULL`, so a second delete raises 404 before reaching the service — identical to the Jinja route (same guard, called first). Do **not** add a special "already-deleted" lookup.
- **Soft-delete queued-turn cancel uses `emit_chat=False`** (audit event only, NO `system` chat row) and outcome `"cancelled"` (not `"failed"`). The new `/api` delete test MUST assert "no system chat row" so G2 doesn't silently lose it.
- **Interactive turn-cancel uses `emit_chat=True`** (writes the visible `turn_outcome` row + `cloud_turn_cancelled` event). Race-safety and cross-workspace safety come entirely from `record_terminal_outcome`'s conditional `UPDATE … WHERE status='queued' AND workspace_id=…` — do not add extra guards.
- **Chat cancel clears local `jobId`:** on `{cancelled: true}` the success handler MUST `setJobId(null)` (and clear `streamed`) — the composer's `disabled` gates on the local `jobId` too, otherwise cleared only by the SSE `done` event. On `{cancelled: false}` keep `jobId` and show the "too late" note.
- **OpenAPI regen runs AFTER both endpoints exist**, with the bge-m3 override: `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh` (the `.env` mxbai default logs a stdout WARNING that corrupts `schema.gen.ts`). Expected: two new paths added; `routes.gen.ts` unchanged.
- **Only the 4 cascade tests are retargeted** (coherence, evaluator, review, `uncited_claims/test_soft_delete.py`). Every other `/delete`-touching test is a doomed Jinja-route test G2 deletes wholesale — leave them untouched.
- **The frontend has NO TS linter** — `tsc` is the gate (plus `vitest`, `build`). Backend gate is `ruff` + `pytest`.
- **Testcontainers-under-load flake** (see memory `project_full_test_suite_flaky`): a whole-suite run can drop Postgres (connection-refused / DeadlockDetected on `TRUNCATE…CASCADE`) — that is infra, not a real failure. Re-run any flagged test **isolated** to confirm green.

---

## File Structure

**Backend — created:**
- `tests/test_workspaces_service.py` — unit test for the extracted `soft_delete_workspace` service. (Stays in `tests/` root: it uses only `pg_session_factory` (root conftest) + `tmp_path`, and imports `make_workspace` as a plain function — no proposal_assist fixtures.)
- `tests/proposal_assist/test_api_workspaces_delete.py` — the `DELETE /api/workspaces/{id}` endpoint tests. (MUST live under `tests/proposal_assist/` — it uses `client_authed_owner`/`_app`/`current_user`, which are defined only in `tests/proposal_assist/conftest.py` and are out of scope for `tests/` root.)
- `tests/proposal_assist/test_api_turns_cancel.py` — the `POST /api/workspaces/{id}/turns/{job}/cancel` endpoint tests. (Same fixture-scoping reason — under `tests/proposal_assist/`.)

**Backend — modified:**
- `src/call_manager/workspaces.py` — add `soft_delete_workspace` + its imports; refresh the module docstring (no longer "single entrypoint").
- `tests/proposal_assist/test_web_workspaces_settings.py` — retarget two caplog logger references to `call_manager.workspaces` (Task 1, after the repoint moves the cleanup warning's logger).
- `src/call_manager/proposal_assist/web/workspaces.py` — repoint the `soft_delete` Jinja route to the service; remove the 5 orphaned imports.
- `src/call_manager/web/api/workspaces.py` — add the `DELETE /api/workspaces/{id}` endpoint + imports.
- `src/call_manager/web/api/turns.py` — add `CancelTurnResponse` + the cancel endpoint + import.
- `tests/proposal_assist/coherence/test_soft_delete_cancels_coherence.py`, `…/evaluator/test_soft_delete_cancels_evaluator.py`, `…/review/test_soft_delete_cancels_review.py`, `…/uncited_claims/test_soft_delete.py` — retarget to `DELETE /api/workspaces/{id}`.

**Frontend — modified:**
- `frontend/src/api/settings.ts` — `useDeleteWorkspace`.
- `frontend/src/api/turns.ts` — `useCancelTurn`.
- `frontend/src/cockpit/WorkspaceSettingsModal.tsx` — Danger-zone delete.
- `frontend/src/cockpit/chat/CockpitChat.tsx` — chat cancel affordance.
- `frontend/src/cockpit/chat/CockpitChat.module.css` — `.cancelRow` style.
- `frontend/src/api/schema.gen.ts` — regenerated (Task 5).
- `frontend/src/cockpit/WorkspaceSettingsModal.test.tsx`, `frontend/src/cockpit/chat/CockpitChat.test.tsx` — new assertions.

---

## Task 1: Extract `soft_delete_workspace` service + repoint the Jinja route

**Files:**
- Modify: `src/call_manager/workspaces.py`
- Modify: `src/call_manager/proposal_assist/web/workspaces.py:1251-1344` (the `soft_delete` route)
- Test: `tests/test_workspaces_service.py` (create)

**Interfaces:**
- Produces: `soft_delete_workspace(session: Session, *, workspace_id: uuid.UUID, workspaces_dir: Path) -> None` — sets `soft_deleted_at`, runs the 4 job cascades + queued-turn cancels (`emit_chat=False`), commits, then best-effort scrubs attachment + reference bytes. Consumed by Tasks 2 (the `/api` endpoint) and by the repointed Jinja route.

- [ ] **Step 1: Write the failing unit test**

Create `tests/test_workspaces_service.py`:

```python
"""Unit test for the extracted soft_delete_workspace service (shared by the
JSON DELETE /api/workspaces/{id} endpoint and the doomed Jinja route)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from call_manager.db.models import (
    ChatTurn,
    CoherenceJob,
    EvaluatorJob,
    ReviewRun,
    TurnJob,
    UncitedClaimsJob,
    Workspace,
    WorkspaceEvent,
)
from call_manager.workspaces import soft_delete_workspace
from tests.proposal_assist.conftest import make_workspace


def test_soft_delete_workspace_cascades_and_scrubs(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(CoherenceJob(workspace_id=ws.id, status="queued"))
        s.add(EvaluatorJob(workspace_id=ws.id, status="queued"))
        # ReviewRun.draft_sha is NOT NULL with no server_default — seed it (matches
        # tests/proposal_assist/review/test_soft_delete_cancels_review.py).
        s.add(ReviewRun(workspace_id=ws.id, status="queued", draft_sha="a" * 64))
        s.add(UncitedClaimsJob(workspace_id=ws.id, status="queued"))
        ut = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
        s.add(ut)
        s.flush()
        s.add(
            TurnJob(
                workspace_id=ws.id,
                user_turn_id=ut.id,
                submitted_by_user_id=ws.owner_id,
                status="queued",
                queued_at=datetime.now(UTC),
            )
        )
        s.commit()
        ws_id = ws.id

    # Seed an on-disk attachment byte tree so we can assert it is scrubbed.
    attach_dir = tmp_path / str(ws_id) / "attachments"
    attach_dir.mkdir(parents=True)
    (attach_dir / "blob.bin").write_bytes(b"x")

    with pg_session_factory() as s:
        soft_delete_workspace(s, workspace_id=ws_id, workspaces_dir=tmp_path)

    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is not None
        assert s.scalar(select(CoherenceJob.status).where(CoherenceJob.workspace_id == ws_id)) == "failed"
        assert s.scalar(select(EvaluatorJob.status).where(EvaluatorJob.workspace_id == ws_id)) == "failed"
        # NB: review's cancel helper sets "cancelled" (not "failed" like the other three).
        assert s.scalar(select(ReviewRun.status).where(ReviewRun.workspace_id == ws_id)) == "cancelled"
        assert s.scalar(select(UncitedClaimsJob.status).where(UncitedClaimsJob.workspace_id == ws_id)) == "failed"
        assert s.scalar(select(TurnJob.status).where(TurnJob.workspace_id == ws_id)) == "cancelled"
        # emit_chat=False → audit event but NO system chat row.
        assert s.scalar(select(WorkspaceEvent.id).where(WorkspaceEvent.kind == "cloud_turn_cancelled")) is not None
        assert s.scalar(select(ChatTurn).where(ChatTurn.role == "system")) is None
    assert not attach_dir.exists()  # bytes scrubbed
```

> Note: confirm the model class names (`EvaluatorJob`, `ReviewRun`, `UncitedClaimsJob`) against `src/call_manager/db/models.py`; the cascade helpers prove they exist. If a class name differs, fix the import — do not change the assertion intent.

- [ ] **Step 2: Run the test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/test_workspaces_service.py -v`
Expected: FAIL — `ImportError: cannot import name 'soft_delete_workspace'`.

- [ ] **Step 3: Implement the service**

First refresh the now-stale module docstring at the top of `src/call_manager/workspaces.py` — it opens with "Single entrypoint today: `create_workspace`." Change that opening sentence to name both entrypoints, e.g. "Workspace lifecycle service functions: `create_workspace` and `soft_delete_workspace`." (Keep the rest of the docstring.)

Then, in `src/call_manager/workspaces.py`, replace the import block (lines 17-21) with:

```python
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.db.models import TurnJob, Workspace, WorkspaceEvent, WorkspaceSetupJob
from call_manager.proposal_assist.attachments.storage import AttachmentStorage
from call_manager.proposal_assist.coherence.jobs import cancel_coherence_jobs_for_workspace
from call_manager.proposal_assist.evaluator.jobs import cancel_evaluator_jobs_for_workspace
from call_manager.proposal_assist.references.storage import ReferenceStorage
from call_manager.proposal_assist.review.jobs import cancel_review_runs_for_workspace
from call_manager.proposal_assist.turns.outcomes import record_terminal_outcome
from call_manager.proposal_assist.uncited_claims.jobs import cancel_uncited_claims_jobs_for_workspace

logger = logging.getLogger(__name__)
```

Then append the service function to the end of the file:

```python
def soft_delete_workspace(
    session: Session, *, workspace_id: uuid.UUID, workspaces_dir: Path
) -> None:
    """Soft-delete a workspace: mark it deleted, cancel every active background
    job + queued turn so none can burn cloud tokens after deletion, commit, then
    best-effort scrub attachment + reference bytes from disk.

    Order is deliberate (a prior Codex-finding fix): DB commit FIRST so the
    user-visible 'deleted' state is durable, THEN best-effort FS cleanup. A
    cleanup failure leaves the workspace soft-deleted with harmless orphan files
    (no code reads them; the row-source-of-truth lookup gates serving on a live
    DB row, and a future sweep collects the bytes). Attachment/reference DB rows
    are intentionally preserved for the audit trail; only the on-disk bytes go.

    The caller resolves ownership (require_owner) before calling this.
    """
    ws = session.get(Workspace, workspace_id)
    if ws is not None:
        ws.soft_deleted_at = datetime.now(UTC)
    # Fail any active (queued|running) background pass so it can't claim and burn
    # cloud tokens after the workspace is gone (each claim_next also excludes
    # soft-deleted workspaces; this also unwedges the active-unique slot). Same txn.
    cancel_coherence_jobs_for_workspace(session, workspace_id=workspace_id)
    cancel_evaluator_jobs_for_workspace(session, workspace_id=workspace_id)
    cancel_review_runs_for_workspace(session, workspace_id=workspace_id)
    cancel_uncited_claims_jobs_for_workspace(session, workspace_id=workspace_id)
    # Cancel any QUEUED turns so a deletion-race turn can't dispatch. Route through
    # the helper for audit parity (closes each cloud_turn_sent chain), but
    # emit_chat=False — the workspace is 404 the instant it's soft-deleted, so a
    # viewer-safe row would be dead-on-arrival. 'cancelled' (owner-initiated,
    # never started), not 'failed'.
    queued = session.execute(
        select(TurnJob.id).where(
            TurnJob.workspace_id == workspace_id,
            TurnJob.status == "queued",
        )
    ).all()
    for row in queued:
        record_terminal_outcome(
            session,
            turn_job_id=row.id,
            workspace_id=workspace_id,
            outcome="cancelled",
            expected_status="queued",
            raw_error="workspace soft-deleted",
            emit_chat=False,
        )
    session.commit()
    # Scrub attachment bytes (DB rows remain — audit trail). Best-effort.
    try:
        AttachmentStorage(root=workspaces_dir).delete_workspace(workspace_id=workspace_id)
    except OSError:
        logger.warning(
            "soft_delete_workspace: attachment cleanup failed for workspace %s; "
            "files will be collected by a future sweep",
            workspace_id,
            exc_info=True,
        )
    # Scrub reference bytes (original files + extracted text.txt). Best-effort.
    try:
        ReferenceStorage(root=workspaces_dir).delete_workspace(workspace_id=workspace_id)
    except OSError:
        logger.warning(
            "soft_delete_workspace: reference cleanup failed for workspace %s; "
            "files will be collected by a future sweep",
            workspace_id,
            exc_info=True,
        )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/test_workspaces_service.py -v`
Expected: PASS.

- [ ] **Step 5: Repoint the Jinja `soft_delete` route to the service**

In `src/call_manager/proposal_assist/web/workspaces.py`, replace the entire `soft_delete` route body (the function at `~:1251-1344`) with:

```python
@router.post("/{workspace_id}/delete")
def soft_delete(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
):
    """Soft-delete a workspace (owner-only) and scrub its file bytes.

    Delegates to the shared `soft_delete_workspace` service — the same code path
    the JSON `DELETE /api/workspaces/{id}` endpoint uses. This Jinja route is
    deleted in the G2 teardown; the service is the durable home.
    """
    ws = _require_owner(workspace_id=workspace_id, user=user, session=session)
    soft_delete_workspace(
        session,
        workspace_id=ws.id,
        workspaces_dir=Path(request.app.state.settings.workspaces_dir),
    )
    return RedirectResponse(url="/workspaces", status_code=303)
```

Add the import (with the module's other `call_manager.*` imports):

```python
from call_manager.workspaces import soft_delete_workspace
```

- [ ] **Step 6: Remove the 5 orphaned imports + fix the moved-logger caplog test, then verify lint + the regression net**

(a) The repoint leaves these unused in the Jinja module — delete their import lines: `cancel_coherence_jobs_for_workspace`, `cancel_evaluator_jobs_for_workspace`, `cancel_review_runs_for_workspace`, `cancel_uncited_claims_jobs_for_workspace`, and `ReferenceStorage`. (`record_terminal_outcome` and `AttachmentStorage` are still used by `cancel_turn` / other routes — KEEP them.)

(b) The repoint also moves the best-effort FS-cleanup `OSError` warning from the Jinja module's logger to the service's logger (`call_manager.workspaces`). One existing test pins the OLD logger name and will now match zero records. In `tests/proposal_assist/test_web_workspaces_settings.py`, update BOTH caplog references — the `caplog.set_level(logging.WARNING, logger="call_manager.proposal_assist.web.workspaces")` line (~:465) and the `if rec.name == "call_manager.proposal_assist.web.workspaces"` filter (~:481) — to `"call_manager.workspaces"`. The asserted message substring (`"attachment cleanup failed"`) is preserved by the service (`soft_delete_workspace: attachment cleanup failed for workspace …`), so ONLY the logger name changes. (This is the only test that filters on that logger for the cleanup warning — verified by grep; the other `call_manager.proposal_assist.web.workspaces` references in the test tree are plain function imports.)

Run: `uv run ruff check src/call_manager/proposal_assist/web/workspaces.py src/call_manager/workspaces.py`
Expected: All checks passed! (If ruff still flags an F401, remove exactly what it names; if it flags one of the KEEP symbols, you removed a route's use by mistake — revert and recheck.)

Run the extraction's regression net (the existing Jinja route test + the moved-logger settings test + the 4 not-yet-retargeted cascade tests still drive the repointed route):
Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/test_web_workspaces_delete.py tests/proposal_assist/test_web_workspaces_settings.py tests/proposal_assist/coherence/test_soft_delete_cancels_coherence.py tests/proposal_assist/evaluator/test_soft_delete_cancels_evaluator.py tests/proposal_assist/review/test_soft_delete_cancels_review.py tests/proposal_assist/uncited_claims/test_soft_delete.py -v`
Expected: all PASS (the repoint is behavior-identical; the settings caplog test passes against the new logger name).

- [ ] **Step 7: Verify the app still boots, then commit**

Run: `EMBEDDING_MODEL=bge-m3 uv run python -c "import call_manager.main; print('boot ok')"`
Expected: `boot ok` (no import cycle from the new `call_manager.workspaces` imports).

```bash
git add src/call_manager/workspaces.py src/call_manager/proposal_assist/web/workspaces.py tests/test_workspaces_service.py tests/proposal_assist/test_web_workspaces_settings.py
git commit -m "feat(workspaces): extract soft_delete_workspace service; repoint Jinja route"
```

---

## Task 2: `DELETE /api/workspaces/{id}` endpoint + tests

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py` (append endpoint after `create_workspace_api`, ~:1147; add imports)
- Test: `tests/proposal_assist/test_api_workspaces_delete.py` (create — under `proposal_assist/` so the `client_authed_owner`/`_app`/`current_user` fixtures are in scope)

**Interfaces:**
- Consumes: `soft_delete_workspace(...)` (Task 1); `require_owner` + `create_workspace` are already imported at `web/api/workspaces.py:64-65`.
- Produces: `DELETE /api/workspaces/{workspace_id}` → 204 (owner-only). Consumed by Task 3 (retargeted cascade tests), Task 5 (regen), Task 6 (React hook).

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_api_workspaces_delete.py` (keep UUID objects for `session.get()`/`WHERE`/path interpolation — the codebase idiom, e.g. `test_web_workspaces_delete.py:30`; a UUID interpolates into the URL via `__str__`):

```python
"""DELETE /api/workspaces/{id} — owner-only soft-delete (cockpit gap-closer)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from call_manager.db.models import (
    ChatTurn,
    CoherenceJob,
    TurnJob,
    Workspace,
    WorkspaceEvent,
    WorkspaceViewer,
)
from tests.proposal_assist.conftest import make_user, make_workspace


def test_owner_delete_204_cascades_and_scrubs(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        s.add(CoherenceJob(workspace_id=ws.id, status="queued"))
        ut = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
        s.add(ut)
        s.flush()
        s.add(
            TurnJob(
                workspace_id=ws.id,
                user_turn_id=ut.id,
                submitted_by_user_id=current_user.id,
                status="queued",
                queued_at=datetime.now(UTC),
            )
        )
        s.commit()
        ws_id = ws.id

    attach_dir = tmp_path / str(ws_id) / "attachments"
    attach_dir.mkdir(parents=True)
    (attach_dir / "blob.bin").write_bytes(b"x")

    r = client_authed_owner.delete(f"/api/workspaces/{ws_id}")
    assert r.status_code == 204

    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is not None
        assert s.scalar(select(CoherenceJob.status).where(CoherenceJob.workspace_id == ws_id)) == "failed"
        # Carry forward the emit_chat=False guarantee (G2 deletes the Jinja-route test):
        assert s.scalar(select(TurnJob.status).where(TurnJob.workspace_id == ws_id)) == "cancelled"
        assert s.scalar(select(WorkspaceEvent.id).where(WorkspaceEvent.kind == "cloud_turn_cancelled")) is not None
        assert s.scalar(select(ChatTurn).where(ChatTurn.role == "system")) is None
    assert not attach_dir.exists()


def test_non_owner_delete_403(client_authed_owner, current_user, pg_session_factory):
    with pg_session_factory() as s:
        stranger = make_user(s, email="stranger@ki.se")
        ws = make_workspace(s, owner=stranger)  # current_user has no relationship to it
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.delete(f"/api/workspaces/{ws_id}")
    assert r.status_code == 403
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is None  # untouched


def test_viewer_delete_403(client_authed_owner, current_user, pg_session_factory):
    # A workspace member (viewer) is still not the owner → 403 (spec §4.1: viewer AND stranger).
    with pg_session_factory() as s:
        stranger = make_user(s, email="viewer-owner@ki.se")
        ws = make_workspace(s, owner=stranger)
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.delete(f"/api/workspaces/{ws_id}")
    assert r.status_code == 403
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is None  # untouched


def test_unknown_workspace_delete_404(client_authed_owner, current_user, pg_session_factory):
    # require_owner returns 404 for a workspace that does not exist (spec §4.1).
    assert client_authed_owner.delete(f"/api/workspaces/{uuid.uuid4()}").status_code == 404


def test_redelete_is_404(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id
    assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 204
    # require_owner filters soft_deleted_at IS NULL → second delete is 404 (not idempotent).
    assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 404
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/test_api_workspaces_delete.py -v`
Expected: FAIL — 405 Method Not Allowed (no DELETE route yet) / assertions unreached.

- [ ] **Step 3: Add the endpoint + imports**

In `src/call_manager/web/api/workspaces.py`:

Change the FastAPI import (line 15) to add `Response`:
```python
from fastapi import APIRouter, Depends, HTTPException, Request, Response
```
Add `from pathlib import Path` with the stdlib imports (near line 12).
Change line 65 to also import the service:
```python
from call_manager.workspaces import create_workspace, soft_delete_workspace
```

Append at the end of the file (after `create_workspace_api`):
```python
@router.delete("/workspaces/{workspace_id}", status_code=204)
def soft_delete_workspace_api(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> Response:
    """Owner-only soft-delete. Cascades active jobs + queued turns and scrubs
    file bytes via the shared soft_delete_workspace service. Re-delete of an
    already-soft-deleted workspace → 404 (require_owner filters soft_deleted_at),
    matching the Jinja route — not idempotent, by design."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        soft_delete_workspace(
            session, workspace_id=ws.id, workspaces_dir=Path(settings.workspaces_dir)
        )
    return Response(status_code=204)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/test_api_workspaces_delete.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Lint + commit**

Run: `uv run ruff check src/call_manager/web/api/workspaces.py tests/proposal_assist/test_api_workspaces_delete.py`
Expected: All checks passed!

```bash
git add src/call_manager/web/api/workspaces.py tests/proposal_assist/test_api_workspaces_delete.py
git commit -m "feat(api): DELETE /api/workspaces/{id} owner-only soft-delete"
```

---

## Task 3: Retarget the 4 kept cascade tests to `/api`

**Files:**
- Modify: `tests/proposal_assist/coherence/test_soft_delete_cancels_coherence.py`
- Modify: `tests/proposal_assist/evaluator/test_soft_delete_cancels_evaluator.py`
- Modify: `tests/proposal_assist/review/test_soft_delete_cancels_review.py`
- Modify: `tests/proposal_assist/uncited_claims/test_soft_delete.py`

**Interfaces:**
- Consumes: `DELETE /api/workspaces/{id}` (Task 2). These tests assert *kept* behavior (job cascade on delete), so they follow the kept surface — moving them now keeps G2 a pure deletion.

- [ ] **Step 1: Retarget each test's request line**

In each of the 4 files, replace the Jinja POST + redirect-status assertion:
```python
    r = client_authed_owner.post(f"/workspaces/{ws_id}/delete", follow_redirects=False)
    assert r.status_code in (200, 303)
```
with the `/api` DELETE:
```python
    r = client_authed_owner.delete(f"/api/workspaces/{ws_id}")
    assert r.status_code == 204
```
Also update the inline comment above it (currently "Real soft-delete route is mounted under /workspaces … and 303-redirects.") to: `# Soft-delete now lives at DELETE /api/workspaces/{id} (cockpit gap-closer).` Leave the seeding + the cascade assertions unchanged.

- [ ] **Step 2: Run the 4 retargeted tests to verify they pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/coherence/test_soft_delete_cancels_coherence.py tests/proposal_assist/evaluator/test_soft_delete_cancels_evaluator.py tests/proposal_assist/review/test_soft_delete_cancels_review.py tests/proposal_assist/uncited_claims/test_soft_delete.py -v`
Expected: 4 PASS.

- [ ] **Step 3: Completeness re-grep (no kept test left on the doomed routes)**

Run: `grep -rln 'workspaces/[^"]*/delete"' tests/ | sort`
Confirm every remaining hit is a doomed Jinja-route test (the `test_web_workspaces_*` family, `test_web_references_delete.py`) or the kept `references/{rid}/delete` route (`test_api_references.py`) — NOT a kept workspace-soft-delete test. (Expected: no kept workspace-soft-delete test remains on `/workspaces/{id}/delete`.)

- [ ] **Step 4: Commit**

```bash
git add tests/proposal_assist/coherence/test_soft_delete_cancels_coherence.py tests/proposal_assist/evaluator/test_soft_delete_cancels_evaluator.py tests/proposal_assist/review/test_soft_delete_cancels_review.py tests/proposal_assist/uncited_claims/test_soft_delete.py
git commit -m "test(api): retarget cascade-on-delete tests to DELETE /api/workspaces/{id}"
```

---

## Task 4: `POST /api/workspaces/{id}/turns/{job}/cancel` endpoint + tests

**Files:**
- Modify: `src/call_manager/web/api/turns.py` (add `CancelTurnResponse` + endpoint + import)
- Test: `tests/proposal_assist/test_api_turns_cancel.py` (create — under `proposal_assist/` for the `client_authed_owner`/`current_user` fixtures)

**Interfaces:**
- Consumes: `record_terminal_outcome` (`proposal_assist.turns.outcomes`); `require_owner` already imported at `turns.py:25`.
- Produces: `POST /api/workspaces/{workspace_id}/turns/{job_id}/cancel` → 200 `{"cancelled": bool}`. Consumed by Task 5 (regen) + Task 7 (React hook).

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_api_turns_cancel.py` (keep UUID objects for `session.get()` and path interpolation — codebase idiom):

```python
"""POST /api/workspaces/{id}/turns/{job}/cancel — owner-only queued-turn cancel."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from call_manager.db.models import ChatTurn, TurnJob, WorkspaceEvent
from tests.proposal_assist.conftest import make_user, make_workspace


def _seed_queued_turn(s, ws, user_id):
    ut = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
    s.add(ut)
    s.flush()
    job = TurnJob(
        workspace_id=ws.id,
        user_turn_id=ut.id,
        submitted_by_user_id=user_id,
        status="queued",
        queued_at=datetime.now(UTC),
    )
    s.add(job)
    s.flush()
    return job.id


def test_owner_cancels_queued_turn(client_authed_owner, current_user, pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        jid = _seed_queued_turn(s, ws, current_user.id)
        s.commit()
        ws_id, jid = ws.id, jid
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/turns/{jid}/cancel")
    assert r.status_code == 200
    assert r.json()["cancelled"] is True
    with pg_session_factory() as s:
        assert s.get(TurnJob, jid).status == "cancelled"
        assert s.scalar(select(WorkspaceEvent.id).where(WorkspaceEvent.kind == "cloud_turn_cancelled")) is not None
        # Interactive cancel emits a visible turn_outcome system row (emit_chat=True).
        assert s.scalar(select(ChatTurn).where(ChatTurn.role == "system")) is not None


def test_cancel_running_turn_is_too_late(client_authed_owner, current_user, pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        jid = _seed_queued_turn(s, ws, current_user.id)
        s.get(TurnJob, jid).status = "running"  # already started
        s.commit()
        ws_id, jid = ws.id, jid
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/turns/{jid}/cancel")
    assert r.status_code == 200
    assert r.json()["cancelled"] is False
    with pg_session_factory() as s:
        assert s.get(TurnJob, jid).status == "running"  # unchanged


def test_non_owner_cancel_403(client_authed_owner, current_user, pg_session_factory):
    with pg_session_factory() as s:
        stranger = make_user(s, email="stranger2@ki.se")
        ws = make_workspace(s, owner=stranger)
        jid = _seed_queued_turn(s, ws, stranger.id)
        s.commit()
        ws_id, jid = ws.id, jid
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/turns/{jid}/cancel")
    assert r.status_code == 403
    with pg_session_factory() as s:
        assert s.get(TurnJob, jid).status == "queued"  # untouched


def test_cross_workspace_job_id_no_op(client_authed_owner, current_user, pg_session_factory):
    # A job belonging to workspace B cannot be cancelled via workspace A (both owned
    # by the caller) — record_terminal_outcome's workspace_id predicate guards it.
    with pg_session_factory() as s:
        ws_a = make_workspace(s, owner=current_user)
        ws_b = make_workspace(s, owner=current_user)
        jid_b = _seed_queued_turn(s, ws_b, current_user.id)
        s.commit()
        ws_a_id, jid_b = ws_a.id, jid_b
    r = client_authed_owner.post(f"/api/workspaces/{ws_a_id}/turns/{jid_b}/cancel")
    assert r.status_code == 200
    assert r.json()["cancelled"] is False
    with pg_session_factory() as s:
        assert s.get(TurnJob, jid_b).status == "queued"  # B's job untouched
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/test_api_turns_cancel.py -v`
Expected: FAIL — 405 (no cancel route yet).

- [ ] **Step 3: Add the endpoint + import**

In `src/call_manager/web/api/turns.py`, add the import (with the other `proposal_assist` imports near the top):
```python
from call_manager.proposal_assist.turns.outcomes import record_terminal_outcome
```
Append at the end of the file:
```python
class CancelTurnResponse(BaseModel):
    cancelled: bool


@router.post(
    "/workspaces/{workspace_id}/turns/{job_id}/cancel", response_model=CancelTurnResponse
)
def cancel_turn(
    workspace_id: uuid.UUID,
    job_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> CancelTurnResponse:
    """Owner-only cancel of a QUEUED turn. Race-safe + cross-workspace-safe via
    record_terminal_outcome's conditional UPDATE (status='queued' AND
    workspace_id=…): a job already 'running', already terminal, or owned by
    another workspace → cancelled=False. Writes the cloud_turn_cancelled audit
    event + a visible turn_outcome chat row (emit_chat=True)."""
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        did = record_terminal_outcome(
            session,
            turn_job_id=job_id,
            workspace_id=ws.id,
            outcome="cancelled",
            expected_status="queued",
            raw_error=None,
            emit_chat=True,
        )
        session.commit()
    return CancelTurnResponse(cancelled=did)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/test_api_turns_cancel.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Lint + commit**

Run: `uv run ruff check src/call_manager/web/api/turns.py tests/proposal_assist/test_api_turns_cancel.py`
Expected: All checks passed!

```bash
git add src/call_manager/web/api/turns.py tests/proposal_assist/test_api_turns_cancel.py
git commit -m "feat(api): POST /api/workspaces/{id}/turns/{job}/cancel owner-only"
```

---

## Task 5: OpenAPI regen (additive)

**Files:**
- Modify: `frontend/src/api/schema.gen.ts` (generated)

**Interfaces:**
- Consumes: the two endpoints from Tasks 2 + 4 must exist before this runs.
- Produces: typed paths `DELETE /api/workspaces/{workspace_id}` and `POST /api/workspaces/{workspace_id}/turns/{job_id}/cancel` in `schema.gen.ts`, consumed by Tasks 6 + 7.

- [ ] **Step 1: Regenerate the schema**

Run: `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`
Expected: writes `frontend/src/api/schema.gen.ts` (no stdout warning thanks to the bge-m3 override).

- [ ] **Step 2: Verify the diff is additive (the two new paths only)**

Run: `git --no-pager diff --stat frontend/src/api/schema.gen.ts frontend/src/routes.gen.ts`
Run: `grep -c 'turns/{job_id}/cancel' frontend/src/api/schema.gen.ts`
Expected: `schema.gen.ts` changed; `routes.gen.ts` unchanged (no React route added). The cancel path grep returns ≥1. Spot-check the diff shows additions for the DELETE workspace op + the cancel op and no unrelated removals.

- [ ] **Step 3: tsc still clean, then commit**

Run: `cd frontend && pnpm exec tsc --noEmit` (Expected: clean — no consumers yet)
```bash
git add frontend/src/api/schema.gen.ts
git commit -m "chore(openapi): regen types for delete + turn-cancel endpoints"
```

---

## Task 6: Frontend — `useDeleteWorkspace` + settings-modal Danger zone

**Files:**
- Modify: `frontend/src/api/settings.ts`
- Modify: `frontend/src/cockpit/WorkspaceSettingsModal.tsx`
- Test: `frontend/src/cockpit/WorkspaceSettingsModal.test.tsx`

**Interfaces:**
- Consumes: `DELETE /api/workspaces/{workspace_id}` (typed via Task 5).
- Produces: `useDeleteWorkspace(id)` mutation (invalidates `["workspaces"]`); a "Delete workspace" control in the owner settings modal that confirms, deletes, closes, and navigates to `/cockpit`.

- [ ] **Step 1: Write the failing test**

Replace `frontend/src/cockpit/WorkspaceSettingsModal.test.tsx` with (adds the delete hook + navigate mocks, keeps the existing save test):

```tsx
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../test-utils";
import { WorkspaceSettingsModal } from "./WorkspaceSettingsModal";

const h = vi.hoisted(() => ({ update: vi.fn(), settings: vi.fn(), del: vi.fn(), navigate: vi.fn() }));
vi.mock("../api/settings", () => ({
  useSettings: () => h.settings(),
  useUpdateSettings: () => ({ mutate: h.update, isPending: false, isError: false }),
  useDeleteWorkspace: () => ({ mutate: h.del, isPending: false, isError: false }),
}));
vi.mock("react-router-dom", async (orig) => ({
  ...(await orig<typeof import("react-router-dom")>()),
  useNavigate: () => h.navigate,
}));

beforeEach(() => {
  h.update.mockReset();
  h.del.mockReset();
  h.navigate.mockReset();
  h.settings.mockReturnValue({
    data: { title: "Proj", status: "drafting", deadline: "2026-09-01", citation_style: "vancouver" },
  });
});
afterEach(() => { vi.restoreAllMocks(); });

describe("WorkspaceSettingsModal", () => {
  it("renders current values and saves a PATCH", async () => {
    renderWithProviders(<WorkspaceSettingsModal id="ws1" opened onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByLabelText(/title/i)).toHaveValue("Proj"));
    fireEvent.change(screen.getByLabelText(/title/i), { target: { value: "Renamed" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    expect(h.update).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Renamed", citation_style: "vancouver" }),
      expect.anything(),
    );
  });

  it("deletes after confirm, closes, and navigates to /cockpit", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    h.del.mockImplementation((_vars: unknown, opts?: { onSuccess?: () => void }) => opts?.onSuccess?.());
    const onClose = vi.fn();
    renderWithProviders(<WorkspaceSettingsModal id="ws1" opened onClose={onClose} />);
    fireEvent.click(await screen.findByRole("button", { name: /delete workspace/i }));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("Proj"));
    expect(h.del).toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
    expect(h.navigate).toHaveBeenCalledWith("/cockpit");
  });

  it("does nothing when the delete confirm is declined", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    renderWithProviders(<WorkspaceSettingsModal id="ws1" opened onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: /delete workspace/i }));
    expect(h.del).not.toHaveBeenCalled();
    expect(h.navigate).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && pnpm exec vitest run src/cockpit/WorkspaceSettingsModal.test.tsx`
Expected: FAIL — `useDeleteWorkspace` is not exported / no "Delete workspace" button.

- [ ] **Step 3: Add the `useDeleteWorkspace` hook**

Append to `frontend/src/api/settings.ts`:
```ts
export function useDeleteWorkspace(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await api.DELETE("/api/workspaces/{workspace_id}", {
          params: { path: { workspace_id: id } },
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["workspaces"] });
    },
  });
}
```

- [ ] **Step 4: Add the Danger zone to the modal**

Edit `frontend/src/cockpit/WorkspaceSettingsModal.tsx`:

Add imports at the top:
```tsx
import { useNavigate } from "react-router-dom";

import { useDeleteWorkspace, useSettings, useUpdateSettings } from "../api/settings";
```
(Replace the existing `import { useSettings, useUpdateSettings } from "../api/settings";` line.)

Inside the component, after `const update = useUpdateSettings(id);`:
```tsx
  const del = useDeleteWorkspace(id);
  const navigate = useNavigate();
```

Add the Danger zone just before the closing `</Modal>` (after the Save button):
```tsx
      <div style={{ marginTop: "1.5rem", borderTop: "1px solid var(--mantine-color-red-9)", paddingTop: "0.75rem" }}>
        <p style={{ fontSize: "0.85rem", opacity: 0.8 }}>
          Soft-deletes the workspace and scrubs uploaded file bytes. Audit rows are kept. This
          cannot be undone.
        </p>
        {del.isError ? <p className="library-error">Could not delete workspace.</p> : null}
        <Button
          color="red"
          variant="outline"
          disabled={del.isPending}
          onClick={() => {
            const name = settings.data?.title ?? "";
            if (window.confirm(`Delete "${name}"? This scrubs all uploaded file bytes and cannot be undone.`)) {
              del.mutate(undefined, {
                onSuccess: () => {
                  onClose();
                  navigate("/cockpit");
                },
              });
            }
          }}
        >
          Delete workspace
        </Button>
      </div>
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd frontend && pnpm exec vitest run src/cockpit/WorkspaceSettingsModal.test.tsx`
Expected: 3 PASS.

- [ ] **Step 6: tsc + commit**

Run: `cd frontend && pnpm exec tsc --noEmit`
Expected: clean.
```bash
git add frontend/src/api/settings.ts frontend/src/cockpit/WorkspaceSettingsModal.tsx frontend/src/cockpit/WorkspaceSettingsModal.test.tsx
git commit -m "feat(spa): delete-workspace Danger zone in the settings modal"
```

---

## Task 7: Frontend — `useCancelTurn` + chat cancel affordance

**Files:**
- Modify: `frontend/src/api/turns.ts`
- Modify: `frontend/src/cockpit/chat/CockpitChat.tsx`
- Modify: `frontend/src/cockpit/chat/CockpitChat.module.css`
- Test: `frontend/src/cockpit/chat/CockpitChat.test.tsx`

**Interfaces:**
- Consumes: `POST /api/workspaces/{id}/turns/{job_id}/cancel` (typed via Task 5).
- Produces: `useCancelTurn(id)` mutation; an owner-only "Cancel" control shown while a turn is in flight that clears `jobId` on `{cancelled:true}` and shows a "too late" note on `{cancelled:false}`.

- [ ] **Step 1a: Add `fireEvent` to the test's import (BEFORE writing the bodies)**

The new tests use `fireEvent`, which the file does not yet import. Change `frontend/src/cockpit/chat/CockpitChat.test.tsx:2` from `import { render, screen, waitFor } from "@testing-library/react";` to:
```tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
```
(Do this first — otherwise the Step 2 run errors with `ReferenceError: fireEvent is not defined` instead of the intended "no Cancel button" red, muddying the TDD signal.)

- [ ] **Step 1b: Write the failing tests**

Append to `frontend/src/cockpit/chat/CockpitChat.test.tsx` (reuse the file's existing `clientMod`, `renderChat`):

```tsx
class FakeESNoop {
  onerror = null; onopen: (() => void) | null = null; close = vi.fn();
  addEventListener() {}
  constructor(_u: string) {}
}

// Stateful mock: after a SUCCESSFUL cancel the server read-model drops the
// in-flight job (running_job_id → null), so the post-cancel refetch cannot
// re-set the local jobId. This lets the {cancelled:true} test prove the
// success handler's setJobId(null) actually ran (the effect never clears
// jobId on its own — it only sets it when running_job_id is truthy).
function mockInFlightOwner(cancelled: boolean) {
  let cleared = false;
  vi.stubGlobal("EventSource", FakeESNoop as unknown as typeof EventSource);
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/turns"))
      return Promise.resolve({ data: { turns: [], can_send: true, needs_disclosure: false, phase: "active", is_owner: true, disabled_reason: null, running_job_id: cleared ? null : "job-1" }, error: undefined });
    if (path.endsWith("/references")) return Promise.resolve({ data: [], error: undefined });
    return Promise.resolve({ data: null, error: undefined });
  }) as never);
  vi.spyOn(clientMod.api, "POST").mockImplementation(((path: string) => {
    if (path.endsWith("/cancel")) {
      if (cancelled) cleared = true;
      return Promise.resolve({ data: { cancelled }, error: undefined });
    }
    return Promise.resolve({ data: null, error: undefined });
  }) as never);
}

it("owner sees Cancel while in-flight; {cancelled:true} clears the in-flight state", async () => {
  mockInFlightOwner(true);
  renderChat();
  fireEvent.click(await screen.findByRole("button", { name: /^cancel$/i }));
  expect(clientMod.api.POST).toHaveBeenCalledWith(
    expect.stringContaining("/cancel"),
    expect.objectContaining({ params: { path: { workspace_id: "ws-1", job_id: "job-1" } } }),
  );
  // Cancel disappears ONLY if setJobId(null) ran AND the refetch returned
  // running_job_id:null — proving the local clear (spec §4.3).
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: /^cancel$/i })).not.toBeInTheDocument(),
  );
  expect(screen.queryByText(/too late/i)).not.toBeInTheDocument();
});

it("{cancelled:false} shows the too-late note and KEEPS the turn in-flight", async () => {
  mockInFlightOwner(false);
  renderChat();
  fireEvent.click(await screen.findByRole("button", { name: /^cancel$/i }));
  expect(await screen.findByText(/too late/i)).toBeInTheDocument();
  // jobId retained → the Cancel control stays (composer remains disabled), per spec §4.3.
  expect(screen.getByRole("button", { name: /^cancel$/i })).toBeInTheDocument();
});

it("a viewer does not see the Cancel control", async () => {
  vi.stubGlobal("EventSource", FakeESNoop as unknown as typeof EventSource);
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/turns"))
      return Promise.resolve({ data: { turns: [], can_send: false, needs_disclosure: false, phase: "active", is_owner: false, disabled_reason: "not_owner", running_job_id: "job-1" }, error: undefined });
    return Promise.resolve({ data: [], error: undefined });
  }) as never);
  renderChat();
  expect(await screen.findByText(/read-only/i)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /^cancel$/i })).not.toBeInTheDocument();
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && pnpm exec vitest run src/cockpit/chat/CockpitChat.test.tsx`
Expected: the 3 new tests FAIL (no Cancel button).

- [ ] **Step 3: Add the `useCancelTurn` hook**

Append to `frontend/src/api/turns.ts`:
```ts
export function useCancelTurn(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (jobId: string) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/turns/{job_id}/cancel", {
          params: { path: { workspace_id: id, job_id: jobId } },
        }),
      ),
    onSettled: () => qc.invalidateQueries({ queryKey: ["turns", id] }),
  });
}
```

- [ ] **Step 4: Add the cancel affordance to CockpitChat**

In `frontend/src/cockpit/chat/CockpitChat.tsx`:

Add the import:
```tsx
import { turnStreamUrl, useAckDisclosure, useCancelTurn, useTurns } from "../../api/turns";
```
(Replace the existing turns import line.)

Inside the component, after `const [streamed, setStreamed] = useState<Record<number, Turn>>({});`:
```tsx
  const cancel = useCancelTurn(id);
  const [cancelNote, setCancelNote] = useState<string | null>(null);
```

Replace the final `return (<Composer … />);` in the footer IIFE with:
```tsx
            return (
              <>
                {d.is_owner && (jobId || d.running_job_id) && (
                  <div className={styles.cancelRow}>
                    <span className={styles.note}>Assistant is working…</span>
                    <button
                      type="button"
                      disabled={cancel.isPending}
                      onClick={() => {
                        const jid = jobId ?? d.running_job_id;
                        if (!jid) return;
                        cancel.mutate(jid, {
                          onSuccess: (res) => {
                            if (res?.cancelled) {
                              setJobId(null);
                              setStreamed({});
                              setCancelNote(null);
                            } else {
                              setCancelNote("Too late — the assistant already started.");
                            }
                          },
                        });
                      }}
                    >
                      Cancel
                    </button>
                    {cancelNote && <span className={styles.note}>{cancelNote}</span>}
                  </div>
                )}
                <Composer
                  id={id}
                  refs={refs}
                  disabled={!!jobId || !!d.running_job_id}
                  onSubmitted={setJobId}
                />
              </>
            );
```

- [ ] **Step 5: Add the `.cancelRow` style**

Append to `frontend/src/cockpit/chat/CockpitChat.module.css`:
```css
.cancelRow {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding-bottom: 0.4rem;
  flex-wrap: wrap;
}
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `cd frontend && pnpm exec vitest run src/cockpit/chat/CockpitChat.test.tsx`
Expected: all PASS (existing + 3 new).

- [ ] **Step 7: tsc + build + commit**

Run: `cd frontend && pnpm exec tsc --noEmit && pnpm build`
Expected: clean.
```bash
git add frontend/src/api/turns.ts frontend/src/cockpit/chat/CockpitChat.tsx frontend/src/cockpit/chat/CockpitChat.module.css frontend/src/cockpit/chat/CockpitChat.test.tsx
git commit -m "feat(spa): cancel-turn affordance in the cockpit chat footer"
```

---

## Final verification (whole-branch, before review handoff)

- [ ] Backend boots: `EMBEDDING_MODEL=bge-m3 uv run python -c "import call_manager.main; print('ok')"`.
- [ ] Targeted backend suites green (separate stdout from the benign APScheduler shutdown noise):
  `EMBEDDING_MODEL=bge-m3 uv run pytest tests/test_workspaces_service.py tests/proposal_assist/test_api_workspaces_delete.py tests/proposal_assist/test_api_turns_cancel.py tests/proposal_assist/coherence/test_soft_delete_cancels_coherence.py tests/proposal_assist/evaluator/test_soft_delete_cancels_evaluator.py tests/proposal_assist/review/test_soft_delete_cancels_review.py tests/proposal_assist/uncited_claims/test_soft_delete.py tests/proposal_assist/test_web_workspaces_delete.py tests/proposal_assist/test_web_workspaces_settings.py tests/proposal_assist/test_web_workspaces_cancel.py -v` — any failure that looks like connection-refused / `DeadlockDetected` on `TRUNCATE…CASCADE` is the documented infra flake; re-run that test **isolated** to confirm green.
- [ ] `uv run ruff check src/ tests/` clean.
- [ ] Frontend: `cd frontend && pnpm exec tsc --noEmit && pnpm exec vitest run && pnpm build` all clean.
- [ ] Schema diff additive-only (the two new paths; `routes.gen.ts` unchanged).

---

## Self-Review (completed by plan author)

**Spec coverage:** soft-delete service extraction (Task 1) ✓; `DELETE /api` + 404-not-idempotent + emit_chat=False carry-forward + byte scrub (Task 2) ✓; cascade-test retarget, exactly 4 (Task 3) ✓; `POST cancel` reusing `record_terminal_outcome` + race/IDOR via the helper (Task 4) ✓; OpenAPI regen additive, bge-m3, after endpoints (Task 5) ✓; settings-modal Danger zone with `window.confirm`+title and navigate-to-`/cockpit` (Task 6) ✓; chat cancel with explicit `setJobId(null)` on `{cancelled:true}` + keep-on-`{cancelled:false}` + owner-only visibility (Task 7) ✓. Jinja-route deletion is correctly left to G2 (out of scope here).

**Placeholder scan:** none — every code/edit step carries full content; one verification note (confirm `EvaluatorJob`/`ReviewRun`/`UncitedClaimsJob` class names) is a guard, not a placeholder.

**Type consistency:** `soft_delete_workspace(session, *, workspace_id, workspaces_dir)` is called identically in Task 1 (Jinja route), Task 2 (`/api`); `useDeleteWorkspace`/`useCancelTurn` names match between hook definition (Tasks 6/7) and the modal/chat consumers; `CancelTurnResponse.cancelled` matches the `res?.cancelled` read in Task 7.

**Adversarial-review revisions applied (3 must-fix + 2 should-fix + nits):** (1) the two `/api` test files live under `tests/proposal_assist/` so the `client_authed_owner`/`_app`/`current_user` fixtures are in scope; (2) the service test seeds `ReviewRun(... draft_sha="a"*64)` (NOT-NULL column); (3) Task 1 retargets `test_web_workspaces_settings.py`'s two caplog logger references to `call_manager.workspaces` (the repoint moves the cleanup-warning logger) and adds that test to the regression net + final verification; (4) Task 7's cancel tests are stateful — `{cancelled:true}` asserts the Cancel control disappears (proving `setJobId(null)`), `{cancelled:false}` asserts it stays; (5) `fireEvent` import is an explicit first sub-step. Nits: tests keep UUID objects (codebase idiom), Task 2 adds viewer-403 + unknown-404 cases, module docstring refreshed. Deferred (deliberate): `del.isError`/`isPending` UI-state tests (standard mutation flags) and the drafting-disabled+in-flight cancel edge (pre-existing footer branch; spec-silent).
