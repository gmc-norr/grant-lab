# Cockpit "Claims" Tab — Claim-Analysis Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-expose the existing background claim-analysis annotation workflow (read findings, insert a suggested citation, dismiss with a reason, reanalyze a section) as a typed `/api` surface + a React "Claims" bottom-panel tab, at parity with the doomed Jinja annotation strip — the 3rd and last Jinja-only feature gap blocking the `workspace_main` G2 teardown.

**Architecture:** A new self-contained `src/call_manager/web/api/annotations.py` router (mounted under `/api`) reuses the **existing** analysis services verbatim (`insert_from_annotation`, `enqueue_section_job`, `_citation_snapshot`) over the same DB tables — no new business logic, no schema change. One prerequisite extraction (Task 0) moves the post-insert re-enqueue helper out of the doomed Jinja module into the kept `analysis/queue.py` so `/api` never imports doomed code. The frontend adds `api/annotations.ts` hooks + a `ClaimsPanel.tsx` wired as the 6th bottom-panel tab.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x / pytest (+ testcontainers Postgres); React 19 + Vite + TanStack Query v5 + `openapi-fetch` (`pnpm`); `ruff` lint/format.

## Global Constraints

- **No DB schema change. No new dependency.** Tables (`UserClaimAnnotation`, `ClaimAnalysisJob`), pipeline, and scheduler ticks all already exist; this slice only re-exposes them.
- **Additive (one extraction aside).** The Jinja `web/annotations.py` routes, `partials/annotation_strip.html`, and the `section_view.html` strip wiring stay wired to the live Jinja editor and are deleted later in **G2** — NOT here. The only touch to the doomed module is the Task 0 extract-and-repoint of the re-enqueue helper.
- **`/api` test files MUST live under `tests/proposal_assist/`.** The `client_authed_owner` / `current_user` / `_app` fixtures are defined only in `tests/proposal_assist/conftest.py`; the root `tests/conftest.py` lacks them. `pg_session_factory` and `tmp_path` are project-wide.
- **`/api` sanitizes any ≥500 response to a generic `internal_error`.** A `CommitMutexTimeout` (busy) must therefore be raised as **429** with `Retry-After`, NOT 503 — the Slice-B convention (`api/turns.py` and `api/workspaces.py` do this).
- **New Pydantic models MUST be namespaced** (`AnnotationsOut`, `ClaimSectionOut`, `ClaimFindingOut`, `ClaimJobOut`, `InsertResultOut`, `DismissReasonIn`, `DismissResultOut`, `ReanalyzeResultOut`). An existing `SectionOut` already lives in `web/api/workspaces.py`; reusing that name makes FastAPI emit `SectionOut1` and scramble the generated TS types.
- **`enqueue_containing_section` (Task 0) MUST import `PROPOSAL_FILENAME` from `call_manager.proposal_assist.artifacts`, NOT from `editing.save`.** `editing/save.py` already imports `enqueue_section_job` from `analysis/queue.py`; a back-import of `editing.save` into `queue.py` is a fatal circular import. `artifacts.py` imports nothing from `call_manager`, so it is cycle-safe. `split_sections` (from `editing.sectionizer`) and `workspace_repo_dir` (from `paths`) are likewise cycle-safe.
- **OpenAPI regen command:** `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh` (the `.env` `mxbai` default logs a WARNING to stdout that corrupts `openapi-typescript`).
- **`frontend/src/routes.gen.ts` MUST be unchanged** — the Claims tab is a bottom-panel tab, not a new React route.
- **Frontend has no TS linter — `tsc` is the gate** (plus `vitest` and `pnpm build`).
- **Frequent commits:** one commit per task (TDD: test → implement → pass → commit).

---

## File Structure

**Backend**
- Create: `src/call_manager/web/api/annotations.py` — the new `/api` router (read-model + 3 actions). Self-contained on kept primitives.
- Modify: `src/call_manager/proposal_assist/analysis/queue.py` — add `enqueue_containing_section` (extracted from the Jinja module).
- Modify: `src/call_manager/proposal_assist/web/annotations.py` — drop the inline `_enqueue_containing_section`, import + call the extracted one, remove 3 now-orphaned imports.
- Modify: `src/call_manager/web/api/__init__.py` — `router.include_router(annotations.router)`.

**Backend tests** (all under `tests/proposal_assist/`)
- Create: `tests/proposal_assist/test_api_annotations_read.py` — read-model (mapping, occurrence, job state, owner flag, stale omission, empty-on-pre-draft).
- Create: `tests/proposal_assist/test_api_annotations_insert.py` — insert success / idempotent / conflict-409 / non-owner-403 / cross-workspace-404.
- Create: `tests/proposal_assist/test_api_annotations_dismiss.py` — dismiss success + audit / empty-422 / race-409 / non-owner-403.
- Create: `tests/proposal_assist/test_api_annotations_reanalyze.py` — enqueue + idempotent dedup / out-of-range-404 / non-owner-403.

**Frontend**
- Create: `frontend/src/api/annotations.ts` — `useAnnotations`, `useInsertAnnotation`, `useDismissAnnotation`, `useReanalyzeSection`.
- Create: `frontend/src/cockpit/ClaimsPanel.tsx` — the panel body.
- Create: `frontend/src/cockpit/ClaimsPanel.test.tsx` — vitest.
- Modify: `frontend/src/cockpit/nav.ts` — extend the `BottomTab` union with `"claims"`.
- Modify: `frontend/src/cockpit/BottomPanel.tsx` — add the 6th tab button + body.

**Generated (Task 5)**
- Modify: `frontend/src/api/schema.gen.ts` — regenerated (additive: new annotation paths).

---

### Task 0: Extract `enqueue_containing_section` into the kept `analysis/queue.py`; repoint the Jinja route

The post-insert re-enqueue helper currently lives inside the **doomed** Jinja `web/annotations.py`. `/api` needs it for insert parity, and importing it from the doomed module would re-create the coupling the teardown removes. Extract it to `analysis/queue.py` (next to `enqueue_section_job`, all kept), behavior byte-identical, and repoint the Jinja route at it — the existing Jinja annotation tests then become the extraction's regression net.

**Files:**
- Modify: `src/call_manager/proposal_assist/analysis/queue.py`
- Modify: `src/call_manager/proposal_assist/web/annotations.py`
- Test (regression net, do not edit): `tests/test_web_annotations.py`

**Interfaces:**
- Produces: `enqueue_containing_section(session: Session, *, workspace_id: uuid.UUID, workspaces_dir: str, commit_sha: str, ev_id: str) -> None` in `analysis/queue.py`. Used by the Jinja insert route (this task) and the `/api` insert endpoint (Task 2).

- [ ] **Step 1: Run the existing Jinja annotation tests to confirm green BEFORE the change (regression baseline)**

Run: `uv run pytest tests/test_web_annotations.py -q`
Expected: PASS (this file is the regression net — it must stay green after the extraction).

- [ ] **Step 2: Add `enqueue_containing_section` to `analysis/queue.py`**

Add these imports to the existing import block at the top of `src/call_manager/proposal_assist/analysis/queue.py` (after the existing `from call_manager.db.models import ClaimAnalysisJob`):

```python
import logging

from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir
```

Add the module-level logger after the `MAX_ATTEMPTS = 3` line:

```python
logger = logging.getLogger(__name__)
```

Add this function directly after `enqueue_section_job` (so the two enqueue helpers sit together):

```python
def enqueue_containing_section(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    workspaces_dir: str,
    commit_sha: str,
    ev_id: str,
) -> None:
    """After a successful citation insert, enqueue a claim-analysis job for the
    section that now contains the new ev_id marker.

    Re-splits the HEAD document, locates the first section whose text contains
    the new marker, and calls enqueue_section_job in the SAME session
    (content-keyed ON CONFLICT DO NOTHING makes this idempotent). The
    reconciliation tick would self-heal eventually; this is immediacy.

    Errors are logged and swallowed — a failed re-enqueue must not roll back
    the committed insert.
    """
    marker_fragment = f"ev:{ev_id}"
    try:
        repo = workspace_repo_dir(workspaces_dir, workspace_id)
        proposal = repo / PROPOSAL_FILENAME
        if not proposal.exists():
            return
        sections = split_sections(proposal.read_text())
        for sec in sections:
            if marker_fragment in sec.text:
                enqueue_section_job(
                    session,
                    workspace_id=workspace_id,
                    section_index=sec.index,
                    section_heading=sec.heading,
                    section_sha=sec.sha,
                    commit_sha=commit_sha,
                )
                session.commit()
                return
    except Exception:
        logger.exception(
            "post-insert re-enqueue failed for workspace %s ev %s", workspace_id, ev_id
        )
```

- [ ] **Step 3: Verify no import cycle was introduced**

Run: `uv run python -c "import call_manager.proposal_assist.analysis.queue; import call_manager.proposal_assist.editing.save; print('ok')"`
Expected: prints `ok` (no `ImportError`/`partially initialized module`). If this fails with a circular-import error, you imported `PROPOSAL_FILENAME` from `editing.save` instead of `artifacts` — fix the import.

- [ ] **Step 4: Repoint the Jinja `web/annotations.py` at the extracted helper and delete the inline copy**

In `src/call_manager/proposal_assist/web/annotations.py`:

1. Change the queue import (line ~44) from:

```python
from call_manager.proposal_assist.analysis.queue import enqueue_section_job
```

to:

```python
from call_manager.proposal_assist.analysis.queue import (
    enqueue_containing_section,
    enqueue_section_job,
)
```

2. **Delete the entire inline `_enqueue_containing_section` function** (the `def _enqueue_containing_section(...)` block, currently lines ~72–111).

3. Remove the 3 now-orphaned imports (they were used ONLY by the deleted helper):

```python
from call_manager.proposal_assist.editing.save import PROPOSAL_FILENAME
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir
```

4. In `post_annotation_insert`, change the call from the private inline helper to the extracted one (keyword `ws_id=` → `workspace_id=`):

```python
    if "idempotent" not in result:
        enqueue_containing_section(
            session,
            workspace_id=workspace_id,
            workspaces_dir=settings.workspaces_dir,
            commit_sha=result["commit_sha"],
            ev_id=result["ev_id"],
        )
```

- [ ] **Step 5: Run ruff to confirm no orphaned imports remain**

Run: `uv run ruff check src/call_manager/proposal_assist/web/annotations.py src/call_manager/proposal_assist/analysis/queue.py`
Expected: no F401 (unused import) errors. If `PROPOSAL_FILENAME`/`split_sections`/`workspace_repo_dir` still flag as unused, you missed deleting an import in Step 4.3.

- [ ] **Step 6: Run the regression net — the Jinja annotation tests must still pass**

Run: `uv run pytest tests/test_web_annotations.py -q`
Expected: PASS, identical to Step 1. In particular `test_insert_post_writes_marker` proves the re-enqueue still fires through the extracted helper.

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/proposal_assist/analysis/queue.py src/call_manager/proposal_assist/web/annotations.py
git commit -m "refactor(analysis): extract enqueue_containing_section to queue.py; repoint Jinja route"
```

---

### Task 1: Backend read-model — `GET /api/workspaces/{id}/annotations` + models + router mount

The read-model: every live draft section with its open findings + the latest analysis-job state + the caller's owner flag. It degrades to `{is_owner, sections: []}` (200) on ANY pre-draft state (no repo / no HEAD / no `proposal.md`) — the deliberate behavior change from the Jinja `get_section_annotations` (which 404s), because the Claims tab is always mounted.

**Files:**
- Create: `src/call_manager/web/api/annotations.py`
- Modify: `src/call_manager/web/api/__init__.py`
- Test: `tests/proposal_assist/test_api_annotations_read.py`

**Interfaces:**
- Consumes: `require_member` (`web/_perms`); `split_sections`, `current_head`, `GitCommitError`, `workspace_repo_dir`, `PROPOSAL_FILENAME`, `_citation_snapshot` (all kept).
- Produces (response model `AnnotationsOut`):
  - `AnnotationsOut{is_owner: bool, sections: list[ClaimSectionOut]}`
  - `ClaimSectionOut{index: int, heading: str, section_sha: str, occurrence: int, job: ClaimJobOut, findings: list[ClaimFindingOut]}`
  - `ClaimJobOut{status: str | None, error: str | None}` — `status ∈ {"pending","failed",None}`
  - `ClaimFindingOut{id: int, kind: str, claim_text: str, evidence_quote: str | None, ref_label: str, can_insert: bool, sentence_ordinal: int}`

**Import discipline (Global Constraint — ruff F401):** this module is built across Tasks 1–4. Each task imports ONLY the symbols it uses, so its own `ruff check` gate passes. Task 1 imports just the read-model's symbols (below). Tasks 2–4 EXTEND the import block when they add their endpoint. `_annotation_or_404` is defined in Task 2 (its first user), not here.

- [ ] **Step 1: Write the failing read-model tests**

Create `tests/proposal_assist/test_api_annotations_read.py`:

```python
"""GET /api/workspaces/{id}/annotations — Claims-tab read-model."""

from __future__ import annotations

from call_manager.db.models import ClaimAnalysisJob, UserClaimAnnotation
from call_manager.proposal_assist.editing.sectionizer import split_sections
from tests.proposal_assist.conftest import make_user, make_workspace
from tests.test_analysis_pipeline import _init_repo, _seed_reference

DOC = "# T\nintro\n## Body\nThis is a factual claim.\n"
CLAIM = "This is a factual claim"
CHUNK_TEXT = "Background. verified words appear here. End."
QUOTE = "verified words"


def _seed(s, tmp_path, owner, *, kind="uncited_supported", with_ref=True):
    ws = make_workspace(s, owner=owner)
    _init_repo(tmp_path, ws.id, DOC)
    sec = split_sections(DOC)[1]
    ref_id = _seed_reference(s, ws.id, CHUNK_TEXT) if with_ref else None
    ann = UserClaimAnnotation(
        workspace_id=ws.id,
        section_sha=sec.sha,
        section_occurrence=0,
        section_heading=sec.heading,
        sentence_ordinal=0,
        char_offset=sec.text.index(CLAIM),
        claim_sha="0" * 64,
        claim_text=CLAIM,
        kind=kind,
        evidence_id="",
        reference_id=ref_id,
        chunk_index=0 if with_ref else None,
        evidence_quote=QUOTE if with_ref else None,
        status="open",
    )
    s.add(ann)
    s.flush()
    return ws, sec, ann.id


def test_read_model_returns_section_with_finding(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, sec, ann_id = _seed(s, tmp_path, current_user)
        s.commit()
        ws_id, sha = ws.id, sec.sha
    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/annotations")
    assert r.status_code == 200
    body = r.json()
    assert body["is_owner"] is True
    # Two live sections (preamble + Body); the finding sits on section 1.
    body_section = next(x for x in body["sections"] if x["index"] == 1)
    assert body_section["section_sha"] == sha
    assert body_section["occurrence"] == 0
    assert len(body_section["findings"]) == 1
    f = body_section["findings"][0]
    assert f["id"] == ann_id
    assert f["kind"] == "uncited_supported"
    assert f["claim_text"] == CLAIM
    assert f["can_insert"] is True
    assert f["ref_label"]  # a non-empty "Family Year"/citekey label


def test_read_model_no_proposal_is_empty_200(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    # A workspace with NO repo / proposal.md — the tab must degrade, not 404/500.
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/annotations")
    assert r.status_code == 200
    assert r.json() == {"is_owner": True, "sections": []}


def test_read_model_omits_stale_and_non_open(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, sec, ann_id = _seed(s, tmp_path, current_user, kind="uncited_no_match", with_ref=False)
        # A stale annotation whose sha is NOT at HEAD must be omitted.
        s.add(
            UserClaimAnnotation(
                workspace_id=ws.id,
                section_sha="d" * 64,  # not a live section
                section_occurrence=0,
                section_heading="ghost",
                sentence_ordinal=0,
                char_offset=0,
                claim_sha="2" * 64,
                claim_text="ghost claim",
                kind="uncited_no_match",
                evidence_id="",
                status="open",
            )
        )
        # A dismissed annotation on the live section must be omitted (status != open).
        s.add(
            UserClaimAnnotation(
                workspace_id=ws.id,
                section_sha=sec.sha,
                section_occurrence=0,
                section_heading=sec.heading,
                sentence_ordinal=5,
                char_offset=0,
                claim_sha="3" * 64,
                claim_text="already handled",
                kind="uncited_no_match",
                evidence_id="",
                status="dismissed",
            )
        )
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/annotations")
    assert r.status_code == 200
    claims = [f["claim_text"] for sec in r.json()["sections"] for f in sec["findings"]]
    assert claims == [CLAIM]  # only the one live, open finding


def test_read_model_reports_pending_job(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, sec, _ = _seed(s, tmp_path, current_user, kind="uncited_no_match", with_ref=False)
        s.add(
            ClaimAnalysisJob(
                workspace_id=ws.id,
                section_index=1,
                section_heading=sec.heading,
                section_sha=sec.sha,
                commit_sha="0" * 40,
                status="queued",
            )
        )
        s.commit()
        ws_id, sha = ws.id, sec.sha
    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/annotations")
    body_section = next(x for x in r.json()["sections"] if x["section_sha"] == sha)
    assert body_section["job"]["status"] == "pending"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/proposal_assist/test_api_annotations_read.py -q`
Expected: FAIL (404 — the route does not exist yet).

- [ ] **Step 3: Create the `/api` module with the read-model**

Create `src/call_manager/web/api/annotations.py`:

```python
"""Claim-analysis "Claims" tab /api surface. Re-exposes the existing annotation
workflow (read findings, insert, dismiss, reanalyze) as typed JSON for the React
cockpit, at parity with the doomed Jinja web/annotations.py strip — but
self-contained on KEPT primitives only (no import of the doomed Jinja helpers).
No new business logic, no schema change."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import (
    ClaimAnalysisJob,
    Reference,
    User,
    UserClaimAnnotation,
)
from call_manager.proposal_assist.citations.gitio import GitCommitError, current_head
from call_manager.proposal_assist.editing.save import PROPOSAL_FILENAME
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.tools.citations import _citation_snapshot
from call_manager.proposal_assist.web._perms import require_member

router = APIRouter()

# ClaimAnalysisJob statuses that mean "analysis is still coming".
_JOB_PENDING_STATUSES = ("queued", "running")


class ClaimFindingOut(BaseModel):
    id: int
    kind: str
    claim_text: str
    evidence_quote: str | None
    ref_label: str
    can_insert: bool
    sentence_ordinal: int


class ClaimJobOut(BaseModel):
    status: str | None  # "pending" | "failed" | None (done / no job)
    error: str | None


class ClaimSectionOut(BaseModel):
    index: int
    heading: str
    section_sha: str
    occurrence: int
    job: ClaimJobOut
    findings: list[ClaimFindingOut]


class AnnotationsOut(BaseModel):
    is_owner: bool
    sections: list[ClaimSectionOut]


@router.get("/workspaces/{workspace_id}/annotations", response_model=AnnotationsOut)
def get_annotations(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> AnnotationsOut:
    """Claims-tab read-model: every live draft section with its open findings +
    the latest analysis-job state. Degrades to {is_owner, sections: []} (200) on
    ANY pre-draft state — no repo / no HEAD / no proposal.md — because the Claims
    tab is always mounted (a strip fragment may 404; an always-present tab cannot)."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_member(workspace_id=workspace_id, user=user, session=session)
        is_owner = ws.owner_id == user.id
        repo = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        proposal = repo / PROPOSAL_FILENAME
        if not proposal.exists():
            return AnnotationsOut(is_owner=is_owner, sections=[])
        try:
            current_head(repo)  # validate the repo has a commit; raises on pre-draft
        except GitCommitError:
            return AnnotationsOut(is_owner=is_owner, sections=[])
        sections = split_sections(proposal.read_text())

        # One query for all open annotations; group by (sha, occurrence).
        anns = list(
            session.scalars(
                select(UserClaimAnnotation)
                .where(
                    UserClaimAnnotation.workspace_id == ws.id,
                    UserClaimAnnotation.status == "open",
                )
                .order_by(UserClaimAnnotation.sentence_ordinal, UserClaimAnnotation.id)
            )
        )
        by_group: dict[tuple[str, int], list[UserClaimAnnotation]] = {}
        for a in anns:
            by_group.setdefault((a.section_sha, a.section_occurrence), []).append(a)

        # One query for "Family Year" ref labels (workspace-scoped — a
        # cross-workspace reference renders as the fallback).
        ref_ids = {a.reference_id for a in anns if a.reference_id is not None}
        labels: dict[uuid.UUID, str] = {}
        if ref_ids:
            for r in session.scalars(
                select(Reference).where(
                    Reference.workspace_id == ws.id,
                    Reference.id.in_(ref_ids),
                )
            ):
                labels[r.id] = _citation_snapshot(r.csl_json, r.citekey or r.title or r.filename)

        # One query for jobs; keep the latest per section_sha.
        latest_job: dict[str, ClaimAnalysisJob] = {}
        for j in session.scalars(
            select(ClaimAnalysisJob)
            .where(ClaimAnalysisJob.workspace_id == ws.id)
            .order_by(ClaimAnalysisJob.queued_at.desc(), ClaimAnalysisJob.id.desc())
        ):
            latest_job.setdefault(j.section_sha, j)

        out_sections: list[ClaimSectionOut] = []
        for sec in sections:
            # occurrence = count of earlier sections with the same sha (mirrors
            # the proven Jinja get_section_annotations mapping).
            occurrence = sum(1 for s in sections[: sec.index] if s.sha == sec.sha)
            group = by_group.get((sec.sha, occurrence), [])
            findings = [
                ClaimFindingOut(
                    id=a.id,
                    kind=a.kind,
                    claim_text=a.claim_text,
                    evidence_quote=a.evidence_quote,
                    ref_label=labels.get(a.reference_id, "reference"),
                    can_insert=(a.kind == "uncited_supported"),
                    sentence_ordinal=a.sentence_ordinal,
                )
                for a in group
            ]
            job = latest_job.get(sec.sha)
            if job is not None and job.status in _JOB_PENDING_STATUSES:
                job_out = ClaimJobOut(status="pending", error=None)
            elif job is not None and job.status == "failed":
                job_out = ClaimJobOut(status="failed", error=job.last_error or "")
            else:
                job_out = ClaimJobOut(status=None, error=None)
            out_sections.append(
                ClaimSectionOut(
                    index=sec.index,
                    heading=sec.heading,
                    section_sha=sec.sha,
                    occurrence=occurrence,
                    job=job_out,
                    findings=findings,
                )
            )
    return AnnotationsOut(is_owner=is_owner, sections=out_sections)
```

- [ ] **Step 4: Mount the router**

In `src/call_manager/web/api/__init__.py`, add `annotations` to the import tuple (alphabetical — before `calls`) and include it:

```python
from call_manager.web.api import (
    annotations,
    calls,
    citation_audit,
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

and after `router.include_router(me.router)`:

```python
router.include_router(annotations.router)
```

- [ ] **Step 5: Run the read-model tests to verify they pass**

Run: `uv run pytest tests/proposal_assist/test_api_annotations_read.py -q`
Expected: PASS (4 tests).

- [ ] **Step 6: Confirm the app boots with the new router mounted**

Run: `uv run python -c "import call_manager.main; call_manager.main.create_app(); print('ok')"`
Expected: prints `ok`.

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check src/call_manager/web/api/annotations.py src/call_manager/web/api/__init__.py
uv run ruff format src/call_manager/web/api/annotations.py
git add src/call_manager/web/api/annotations.py src/call_manager/web/api/__init__.py tests/proposal_assist/test_api_annotations_read.py
git commit -m "feat(api): GET /api/workspaces/{id}/annotations Claims read-model"
```

---

### Task 2: `POST /api/workspaces/{id}/annotations/{annotation_id}/insert`

Owner-only one-click citation insert. Verbatim reuse of the Jinja insert body over `insert_from_annotation`, then (on a genuine, non-idempotent insert) the post-insert `enqueue_containing_section` re-enqueue.

**Files:**
- Modify: `src/call_manager/web/api/annotations.py`
- Test: `tests/proposal_assist/test_api_annotations_insert.py`

**Interfaces:**
- Consumes: `insert_from_annotation` (`analysis.insert`), `enqueue_containing_section` (Task 0), `require_owner`.
- Produces: `_annotation_or_404(session: Session, ws: Workspace, annotation_id: int) -> UserClaimAnnotation` (defined here, reused by Task 3); `InsertResultOut{ev_id: str | None, citekey: str | None, commit_sha: str | None, idempotent: bool}`.

- [ ] **Step 1: Write the failing insert tests**

Create `tests/proposal_assist/test_api_annotations_insert.py`:

```python
"""POST /api/workspaces/{id}/annotations/{ann}/insert — owner-only citation insert."""

from __future__ import annotations

from call_manager.db.models import ClaimAnalysisJob, UserClaimAnnotation
from call_manager.proposal_assist.editing.sectionizer import split_sections
from tests.proposal_assist.conftest import make_user, make_workspace
from tests.test_analysis_pipeline import _init_repo, _seed_reference

DOC = "# T\nintro\n## Body\nThis is a factual claim.\n"
CLAIM = "This is a factual claim"
CHUNK_TEXT = "Background. verified words appear here. End."
QUOTE = "verified words"


def _seed_supported(s, tmp_path, owner):
    ws = make_workspace(s, owner=owner)
    repo = _init_repo(tmp_path, ws.id, DOC)
    sec = split_sections(DOC)[1]
    ref_id = _seed_reference(s, ws.id, CHUNK_TEXT)
    ann = UserClaimAnnotation(
        workspace_id=ws.id,
        section_sha=sec.sha,
        section_occurrence=0,
        section_heading=sec.heading,
        sentence_ordinal=0,
        char_offset=sec.text.index(CLAIM),
        claim_sha="0" * 64,
        claim_text=CLAIM,
        kind="uncited_supported",
        evidence_id="",
        reference_id=ref_id,
        chunk_index=0,
        evidence_quote=QUOTE,
        status="open",
    )
    s.add(ann)
    s.flush()
    return ws, ann.id, repo


def test_owner_insert_writes_marker_and_reenqueues(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, ann_id, repo = _seed_supported(s, tmp_path, current_user)
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/annotations/{ann_id}/insert")
    assert r.status_code == 200
    body = r.json()
    assert body["ev_id"].startswith("ev-")
    assert body["citekey"]
    assert body["commit_sha"]
    assert body["idempotent"] is False
    doc = (repo / "proposal.md").read_text()
    assert "<!-- ev:ev-" in doc
    with pg_session_factory() as s:
        assert s.get(UserClaimAnnotation, ann_id).status == "inserted"
        # post-insert re-enqueue: a queued job for the now-changed section exists.
        job = s.query(ClaimAnalysisJob).filter_by(workspace_id=ws_id, status="queued").first()
        assert job is not None


def test_insert_already_inserted_is_idempotent(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, ann_id, _ = _seed_supported(s, tmp_path, current_user)
        s.get(UserClaimAnnotation, ann_id).status = "inserted"  # already done
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/annotations/{ann_id}/insert")
    assert r.status_code == 200
    assert r.json()["idempotent"] is True


def test_insert_non_supported_kind_409(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    # A no_match annotation is not actionable -> insert_from_annotation raises
    # InsertConflict -> 409.
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, ann_id, _ = _seed_supported(s, tmp_path, current_user)
        s.get(UserClaimAnnotation, ann_id).kind = "uncited_no_match"
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/annotations/{ann_id}/insert")
    assert r.status_code == 409


def test_insert_non_owner_403(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        stranger = make_user(s, email="stranger-ins@ki.se")
        ws, ann_id, _ = _seed_supported(s, tmp_path, stranger)
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/annotations/{ann_id}/insert")
    assert r.status_code == 403


def test_insert_cross_workspace_annotation_404(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    # An annotation id that belongs to workspace B cannot be inserted via A.
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws_a = make_workspace(s, owner=current_user)
        _init_repo(tmp_path, ws_a.id, DOC)
        ws_b, ann_b, _ = _seed_supported(s, tmp_path, current_user)
        s.commit()
        ws_a_id, ann_b_id = ws_a.id, ann_b
    r = client_authed_owner.post(f"/api/workspaces/{ws_a_id}/annotations/{ann_b_id}/insert")
    assert r.status_code == 404
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/proposal_assist/test_api_annotations_insert.py -q`
Expected: FAIL (405/404 — the POST route does not exist yet).

- [ ] **Step 3: Extend the imports for the insert path**

Add `require_owner` to the existing `web._perms` import, and add these imports to `src/call_manager/web/api/annotations.py` (place them with the other `call_manager` imports, keeping the block sorted):

```python
from sqlalchemy.orm import Session  # _annotation_or_404's signature
from call_manager.db.models import Workspace  # extend the existing db.models import tuple
from call_manager.proposal_assist.analysis.insert import InsertConflict, insert_from_annotation
from call_manager.proposal_assist.analysis.queue import enqueue_containing_section
from call_manager.proposal_assist.drafting.locking import BUSY_ERROR_MESSAGE, CommitMutexTimeout
from call_manager.proposal_assist.web._perms import require_member, require_owner
```

(Concretely: add `from sqlalchemy.orm import Session` next to the `from sqlalchemy import select` line; change `from call_manager.proposal_assist.web._perms import require_member` to `import require_member, require_owner`; and add `Workspace` to the `from call_manager.db.models import (...)` tuple — `_annotation_or_404`'s signature needs both `Session` and `Workspace`.)

- [ ] **Step 4: Add `_annotation_or_404` + the insert endpoint**

Append to `src/call_manager/web/api/annotations.py`:

```python
def _annotation_or_404(
    session: Session, ws: Workspace, annotation_id: int
) -> UserClaimAnnotation:
    ann = session.get(UserClaimAnnotation, annotation_id)
    if ann is None or ann.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="annotation not found")
    return ann


class InsertResultOut(BaseModel):
    ev_id: str | None = None
    citekey: str | None = None
    commit_sha: str | None = None
    idempotent: bool = False


@router.post(
    "/workspaces/{workspace_id}/annotations/{annotation_id}/insert",
    response_model=InsertResultOut,
)
def insert_annotation(
    workspace_id: uuid.UUID,
    annotation_id: int,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> InsertResultOut:
    """Insert the citation an uncited_supported annotation suggests, via the
    canonical write path (insert_from_annotation). Owner-only. On a genuine
    (non-idempotent) insert, re-enqueue analysis for the now-changed section."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        ann = _annotation_or_404(session, ws, annotation_id)
        try:
            result = insert_from_annotation(
                session=session,
                workspace=ws,
                user_id=user.id,
                annotation=ann,
                workspaces_dir=settings.workspaces_dir,
            )
            session.commit()
        except InsertConflict as e:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(e)) from e
        except CommitMutexTimeout as e:
            session.rollback()
            # /api sanitizes >=500 -> remap the 503 busy signal to a retriable 429.
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        except GitCommitError as e:
            session.rollback()
            raise HTTPException(
                status_code=500, detail="insert failed — nothing was written"
            ) from e

        if "idempotent" not in result:
            enqueue_containing_section(
                session,
                workspace_id=workspace_id,
                workspaces_dir=settings.workspaces_dir,
                commit_sha=result["commit_sha"],
                ev_id=result["ev_id"],
            )
            return InsertResultOut(
                ev_id=result["ev_id"],
                citekey=result["citekey"],
                commit_sha=result["commit_sha"],
            )
    return InsertResultOut(idempotent=True)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/proposal_assist/test_api_annotations_insert.py -q`
Expected: PASS (5 tests).

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check src/call_manager/web/api/annotations.py
git add src/call_manager/web/api/annotations.py tests/proposal_assist/test_api_annotations_insert.py
git commit -m "feat(api): POST annotations/{id}/insert owner-only citation insert"
```

---

### Task 3: `POST /api/workspaces/{id}/annotations/{annotation_id}/dismiss`

Owner-only dismiss with a required, audited reason. Guarded `UPDATE … WHERE status='open'` (race → 409) + `user_claim_dismissed` `WorkspaceEvent`. JSON body (not Form) since this is `/api`.

**Files:**
- Modify: `src/call_manager/web/api/annotations.py`
- Test: `tests/proposal_assist/test_api_annotations_dismiss.py`

**Interfaces:**
- Consumes: `_annotation_or_404` (Task 2), `require_owner` (imported in Task 2).
- Produces: `DismissReasonIn{reason: str}`, `DismissResultOut{dismissed: bool}`.

- [ ] **Step 1: Write the failing dismiss tests**

Create `tests/proposal_assist/test_api_annotations_dismiss.py`:

```python
"""POST /api/workspaces/{id}/annotations/{ann}/dismiss — owner-only audited dismiss."""

from __future__ import annotations

from sqlalchemy import select

from call_manager.db.models import UserClaimAnnotation, WorkspaceEvent
from tests.proposal_assist.conftest import make_user, make_workspace


def _seed_open(s, owner):
    ws = make_workspace(s, owner=owner)
    ann = UserClaimAnnotation(
        workspace_id=ws.id,
        section_sha="a" * 64,
        section_occurrence=0,
        section_heading="Body",
        sentence_ordinal=0,
        char_offset=0,
        claim_sha="0" * 64,
        claim_text="a claim",
        kind="uncited_no_match",
        evidence_id="",
        status="open",
    )
    s.add(ann)
    s.flush()
    return ws, ann.id


def test_owner_dismiss_with_reason_audits(
    client_authed_owner, current_user, pg_session_factory
):
    with pg_session_factory() as s:
        ws, ann_id = _seed_open(s, current_user)
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.post(
        f"/api/workspaces/{ws_id}/annotations/{ann_id}/dismiss",
        json={"reason": "cited in the cover letter"},
    )
    assert r.status_code == 200
    assert r.json()["dismissed"] is True
    with pg_session_factory() as s:
        ann = s.get(UserClaimAnnotation, ann_id)
        assert ann.status == "dismissed"
        assert ann.dismissed_reason == "cited in the cover letter"
        assert ann.resolved_by_user_id == current_user.id
        ev = s.scalar(
            select(WorkspaceEvent).where(WorkspaceEvent.kind == "user_claim_dismissed")
        )
        assert ev.payload == {"annotation_id": ann_id, "reason": "cited in the cover letter"}


def test_dismiss_empty_reason_422(client_authed_owner, current_user, pg_session_factory):
    with pg_session_factory() as s:
        ws, ann_id = _seed_open(s, current_user)
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.post(
        f"/api/workspaces/{ws_id}/annotations/{ann_id}/dismiss",
        json={"reason": "   "},
    )
    assert r.status_code == 422
    with pg_session_factory() as s:
        assert s.get(UserClaimAnnotation, ann_id).status == "open"  # untouched


def test_dismiss_already_resolved_409(client_authed_owner, current_user, pg_session_factory):
    with pg_session_factory() as s:
        ws, ann_id = _seed_open(s, current_user)
        s.get(UserClaimAnnotation, ann_id).status = "inserted"  # raced
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.post(
        f"/api/workspaces/{ws_id}/annotations/{ann_id}/dismiss",
        json={"reason": "not needed"},
    )
    assert r.status_code == 409


def test_dismiss_non_owner_403(client_authed_owner, current_user, pg_session_factory):
    with pg_session_factory() as s:
        stranger = make_user(s, email="stranger-dis@ki.se")
        ws, ann_id = _seed_open(s, stranger)
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.post(
        f"/api/workspaces/{ws_id}/annotations/{ann_id}/dismiss",
        json={"reason": "x"},
    )
    assert r.status_code == 403
    with pg_session_factory() as s:
        assert s.get(UserClaimAnnotation, ann_id).status == "open"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/proposal_assist/test_api_annotations_dismiss.py -q`
Expected: FAIL (405/404 — route absent).

- [ ] **Step 3: Extend the imports for the dismiss path**

Add to `src/call_manager/web/api/annotations.py` (keep the block sorted):

```python
from datetime import UTC, datetime  # near the top, before the third-party imports
from call_manager.db.models import WorkspaceEvent  # extend the existing db.models import tuple
```

(`require_owner` and `_annotation_or_404` already exist from Task 2.)

- [ ] **Step 4: Add the dismiss endpoint**

Append to `src/call_manager/web/api/annotations.py`:

```python
class DismissReasonIn(BaseModel):
    reason: str


class DismissResultOut(BaseModel):
    dismissed: bool


@router.post(
    "/workspaces/{workspace_id}/annotations/{annotation_id}/dismiss",
    response_model=DismissResultOut,
)
def dismiss_annotation(
    workspace_id: uuid.UUID,
    annotation_id: int,
    body: DismissReasonIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> DismissResultOut:
    """Dismiss an open annotation with a required, audited reason. Owner-only.
    Guarded UPDATE WHERE status='open' (race -> 409) + user_claim_dismissed event."""
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        ann = _annotation_or_404(session, ws, annotation_id)
        reason = body.reason.strip()
        if not reason:
            raise HTTPException(status_code=422, detail="a reason is required")
        updated = (
            session.query(UserClaimAnnotation)
            .filter(
                UserClaimAnnotation.id == ann.id,
                UserClaimAnnotation.status == "open",
            )
            .update(
                {
                    "status": "dismissed",
                    "dismissed_reason": reason,
                    "resolved_by_user_id": user.id,
                    "resolved_at": datetime.now(UTC),
                },
                synchronize_session=False,
            )
        )
        if not updated:
            session.rollback()
            raise HTTPException(status_code=409, detail="annotation is no longer actionable")
        session.add(
            WorkspaceEvent(
                workspace_id=ws.id,
                kind="user_claim_dismissed",
                actor_id=user.id,
                payload={"annotation_id": ann.id, "reason": reason},
            )
        )
        session.commit()
    return DismissResultOut(dismissed=True)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/proposal_assist/test_api_annotations_dismiss.py -q`
Expected: PASS (4 tests).

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check src/call_manager/web/api/annotations.py
git add src/call_manager/web/api/annotations.py tests/proposal_assist/test_api_annotations_dismiss.py
git commit -m "feat(api): POST annotations/{id}/dismiss owner-only audited dismiss"
```

---

### Task 4: `POST /api/workspaces/{id}/sections/{n}/reanalyze`

Owner-only re-enqueue of a section's live content at HEAD. 404 if `n` out of range or no draft. Returns whether a new job row was created (`enqueue_section_job` dedups content-keyed).

**Files:**
- Modify: `src/call_manager/web/api/annotations.py`
- Test: `tests/proposal_assist/test_api_annotations_reanalyze.py`

**Interfaces:**
- Consumes: `enqueue_section_job` (extend the Task-0 `analysis.queue` import); `current_head`, `GitCommitError`, `split_sections`, `workspace_repo_dir`, `PROPOSAL_FILENAME`, `require_owner` (already imported in Tasks 1–2).
- Produces: `ReanalyzeResultOut{enqueued: bool}`.

- [ ] **Step 1: Write the failing reanalyze tests**

Create `tests/proposal_assist/test_api_annotations_reanalyze.py`:

```python
"""POST /api/workspaces/{id}/sections/{n}/reanalyze — owner-only re-enqueue."""

from __future__ import annotations

import re

from call_manager.db.models import ClaimAnalysisJob
from call_manager.proposal_assist.editing.sectionizer import split_sections
from tests.proposal_assist.conftest import make_user, make_workspace
from tests.test_analysis_pipeline import _init_repo

DOC = "# T\nintro\n## Body\nThis is a factual claim.\n"


def test_owner_reanalyze_enqueues(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        _init_repo(tmp_path, ws.id, DOC)
        s.commit()
        ws_id = ws.id
    sha = split_sections(DOC)[1].sha
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/sections/1/reanalyze")
    assert r.status_code == 200
    assert r.json()["enqueued"] is True
    with pg_session_factory() as s:
        job = s.query(ClaimAnalysisJob).filter_by(workspace_id=ws_id).one()
        assert job.status == "queued"
        assert job.section_sha == sha
        assert re.fullmatch(r"[0-9a-f]{40}", job.commit_sha)  # real SHA, not text


def test_reanalyze_idempotent_dedup(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    # A second reanalyze of the SAME content dedups (one queued job per sha).
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        _init_repo(tmp_path, ws.id, DOC)
        s.commit()
        ws_id = ws.id
    r1 = client_authed_owner.post(f"/api/workspaces/{ws_id}/sections/1/reanalyze")
    r2 = client_authed_owner.post(f"/api/workspaces/{ws_id}/sections/1/reanalyze")
    assert r1.json()["enqueued"] is True
    assert r2.json()["enqueued"] is False  # deduped
    with pg_session_factory() as s:
        assert s.query(ClaimAnalysisJob).filter_by(workspace_id=ws_id, status="queued").count() == 1


def test_reanalyze_out_of_range_404(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        _init_repo(tmp_path, ws.id, DOC)
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/sections/99/reanalyze")
    assert r.status_code == 404


def test_reanalyze_non_owner_403(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        stranger = make_user(s, email="stranger-re@ki.se")
        ws = make_workspace(s, owner=stranger)
        _init_repo(tmp_path, ws.id, DOC)
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/sections/1/reanalyze")
    assert r.status_code == 403
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/proposal_assist/test_api_annotations_reanalyze.py -q`
Expected: FAIL (405/404 — route absent).

- [ ] **Step 3: Extend the imports for the reanalyze path**

Change the Task-0 `analysis.queue` import in `src/call_manager/web/api/annotations.py` to also bring in `enqueue_section_job`:

```python
from call_manager.proposal_assist.analysis.queue import (
    enqueue_containing_section,
    enqueue_section_job,
)
```

- [ ] **Step 4: Add the reanalyze endpoint**

Append to `src/call_manager/web/api/annotations.py`:

```python
class ReanalyzeResultOut(BaseModel):
    enqueued: bool


@router.post(
    "/workspaces/{workspace_id}/sections/{n}/reanalyze",
    response_model=ReanalyzeResultOut,
)
def reanalyze_section(
    workspace_id: uuid.UUID,
    n: int,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> ReanalyzeResultOut:
    """Re-enqueue claim analysis for the section's LIVE content at HEAD.
    Owner-only. 404 if n is out of range or there is no draft yet."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        repo = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        proposal = repo / PROPOSAL_FILENAME
        if not proposal.exists():
            raise HTTPException(status_code=404, detail="no such section")
        try:
            head = current_head(repo)
        except GitCommitError as e:
            raise HTTPException(status_code=404, detail="no such section") from e
        sections = split_sections(proposal.read_text())
        if n < 0 or n >= len(sections):
            raise HTTPException(status_code=404, detail="no such section")
        sec = sections[n]
        enqueued = enqueue_section_job(
            session,
            workspace_id=ws.id,
            section_index=sec.index,
            section_heading=sec.heading,
            section_sha=sec.sha,
            commit_sha=head,
        )
        session.commit()
    return ReanalyzeResultOut(enqueued=enqueued)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/proposal_assist/test_api_annotations_reanalyze.py -q`
Expected: PASS (4 tests).

- [ ] **Step 6: Run the whole new backend test set + lint, then commit**

Run: `uv run pytest tests/proposal_assist/test_api_annotations_read.py tests/proposal_assist/test_api_annotations_insert.py tests/proposal_assist/test_api_annotations_dismiss.py tests/proposal_assist/test_api_annotations_reanalyze.py tests/test_web_annotations.py -q`
Expected: PASS (all 4 new files + the Jinja regression net).

```bash
uv run ruff check src/call_manager/web/api/annotations.py
git add src/call_manager/web/api/annotations.py tests/proposal_assist/test_api_annotations_reanalyze.py
git commit -m "feat(api): POST sections/{n}/reanalyze owner-only re-enqueue"
```

---

### Task 5: OpenAPI regen

Regenerate the TS schema so the frontend hooks (Task 6) get typed `/api/workspaces/{id}/annotations` paths. Additive only — `routes.gen.ts` must not change.

**Files:**
- Modify: `frontend/src/api/schema.gen.ts` (generated)

- [ ] **Step 1: Regenerate the schema with the embedding-model override**

Run: `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`
Expected: completes without error; `frontend/src/api/schema.gen.ts` updated.

- [ ] **Step 2: Verify the change is additive and `routes.gen.ts` is untouched**

Run: `git diff --stat frontend/src/api/schema.gen.ts frontend/src/routes.gen.ts`
Expected: `schema.gen.ts` changed (insertions, no path removals); `routes.gen.ts` NOT listed (unchanged).

Run: `git diff frontend/src/api/schema.gen.ts | grep -E "^\-.*\"/api/" || echo "no /api paths removed"`
Expected: prints `no /api paths removed`.

Run (the Jinja annotation paths — `/workspaces/.../annotations` with NO `/api` prefix — already exist in the schema, so check for the NEW `/api`-prefixed key specifically):
`grep -c '"/api/workspaces/{workspace_id}/annotations"' frontend/src/api/schema.gen.ts`
Expected: `1` (0 before regen). Also confirm the new schema models landed:
`grep -cE "AnnotationsOut|ClaimSectionOut|ClaimFindingOut|ClaimJobOut|InsertResultOut|DismissReasonIn|DismissResultOut|ReanalyzeResultOut" frontend/src/api/schema.gen.ts`
Expected: a non-zero count. (If `"/api/...annotations"` is 0, the regen did not pick up the new router — re-check Task 1's mount.)

- [ ] **Step 3: Commit**

```bash
git add frontend/src/api/schema.gen.ts
git commit -m "chore(openapi): regen types for Claims-tab annotation endpoints"
```

---

### Task 6: Frontend API hooks + `nav.ts` tab union

`api/annotations.ts`: the read-model query (conditional polling while a job is pending) + the three mutations with cache invalidation. Extend the `BottomTab` union with `"claims"`.

**Files:**
- Create: `frontend/src/api/annotations.ts`
- Modify: `frontend/src/cockpit/nav.ts`

**Interfaces:**
- Consumes: generated types from `schema.gen.ts` (Task 5); `api`, `unwrap` from `./client`.
- Produces:
  - `useAnnotations(id) -> UseQueryResult<AnnotationsResponse>` — `AnnotationsResponse = components["schemas"]["AnnotationsOut"]`; `refetchInterval` = 3000 only while some section's `job.status === "pending"`, else `false`.
  - `useInsertAnnotation(id) -> UseMutationResult` (vars: `annotationId: number`)
  - `useDismissAnnotation(id) -> UseMutationResult` (vars: `{ annotationId: number; reason: string }`)
  - `useReanalyzeSection(id) -> UseMutationResult` (vars: `sectionIndex: number`)
  - `BottomTab` union now includes `"claims"`.

> **Path-param types are `number`, not `string`.** The backend `{annotation_id}` and `{n}` path params are `int` → openapi-fetch types them `number` in the generated client (same as `{index}` in `api/editor.ts`). Passing a string fails `pnpm tsc -b`. Annotation ids therefore stay `number` end-to-end (`ClaimFindingOut.id` is already `number`); do NOT `String()`-coerce them.

- [ ] **Step 1: Extend the `BottomTab` union**

In `frontend/src/cockpit/nav.ts` change:

```ts
export type BottomTab = "inbox" | "problems" | "activity" | "library" | "citations";
```

to:

```ts
export type BottomTab = "inbox" | "problems" | "activity" | "library" | "citations" | "claims";
```

- [ ] **Step 2: Create the API hooks**

Create `frontend/src/api/annotations.ts`:

```ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type AnnotationsResponse = components["schemas"]["AnnotationsOut"];
export type ClaimSection = components["schemas"]["ClaimSectionOut"];
export type ClaimFinding = components["schemas"]["ClaimFindingOut"];

/** GET /api/workspaces/{id}/annotations — Claims read-model. Polls every 3s
 *  ONLY while some section has a pending analysis job, else not at all. */
export function useAnnotations(id: string) {
  return useQuery({
    queryKey: ["annotations", id],
    enabled: !!id,
    refetchInterval: (query) => {
      const data = query.state.data as AnnotationsResponse | undefined;
      const pending = data?.sections.some((s) => s.job.status === "pending");
      return pending ? 3000 : false;
    },
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/annotations", {
          params: { path: { workspace_id: id } },
        }),
      ),
  });
}

export function useInsertAnnotation(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (annotationId: number) =>
      unwrap(
        await api.POST(
          "/api/workspaces/{workspace_id}/annotations/{annotation_id}/insert",
          { params: { path: { workspace_id: id, annotation_id: annotationId } } },
        ),
      ),
    // Insert changed the document: refetch annotations + the draft + the
    // citation/proposed-change views (prefix-match invalidation).
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["annotations", id] });
      void qc.invalidateQueries({ queryKey: ["draft", id] });
      void qc.invalidateQueries({ queryKey: ["citation-audit", id] });
      void qc.invalidateQueries({ queryKey: ["proposals", id] });
    },
  });
}

export function useDismissAnnotation(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (vars: { annotationId: number; reason: string }) =>
      unwrap(
        await api.POST(
          "/api/workspaces/{workspace_id}/annotations/{annotation_id}/dismiss",
          {
            params: { path: { workspace_id: id, annotation_id: vars.annotationId } },
            body: { reason: vars.reason },
          },
        ),
      ),
    onSettled: () => qc.invalidateQueries({ queryKey: ["annotations", id] }),
  });
}

export function useReanalyzeSection(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (sectionIndex: number) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/sections/{n}/reanalyze", {
          params: { path: { workspace_id: id, n: sectionIndex } },
        }),
      ),
    onSettled: () => qc.invalidateQueries({ queryKey: ["annotations", id] }),
  });
}
```

- [ ] **Step 3: Type-check**

Run (from `frontend/`): `pnpm tsc -b`
Expected: clean. If `components["schemas"]["ClaimSectionOut"]` (etc.) is missing, the OpenAPI regen (Task 5) did not run or the model names drifted — fix before continuing.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/annotations.ts frontend/src/cockpit/nav.ts
git commit -m "feat(spa): annotations API hooks + claims BottomTab union"
```

---

### Task 7: `ClaimsPanel` + wire the 6th bottom-panel tab

The panel renders each live section (heading + job indicator) with its findings; owners get Insert (only when `can_insert`), Dismiss (inline required-reason field), and per-section Reanalyze; viewers see the same list read-only. Wire it into `BottomPanel` as the 6th tab with an open-findings count.

**Files:**
- Create: `frontend/src/cockpit/ClaimsPanel.tsx`
- Create: `frontend/src/cockpit/ClaimsPanel.test.tsx`
- Modify: `frontend/src/cockpit/BottomPanel.tsx`

**Interfaces:**
- Consumes: `useAnnotations`, `useInsertAnnotation`, `useDismissAnnotation`, `useReanalyzeSection` (Task 6); `AnnotationsResponse.is_owner` drives action gating.

- [ ] **Step 1: Write the failing `ClaimsPanel` tests**

Create `frontend/src/cockpit/ClaimsPanel.test.tsx`:

```tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ClaimsPanel } from "./ClaimsPanel";

const h = vi.hoisted(() => ({
  ann: { isLoading: false, isError: false, data: undefined as unknown },
  insert: { mutate: vi.fn(), isPending: false },
  dismiss: { mutate: vi.fn(), isPending: false },
  reanalyze: { mutate: vi.fn(), isPending: false },
}));

vi.mock("../api/annotations", () => ({
  useAnnotations: () => h.ann,
  useInsertAnnotation: () => h.insert,
  useDismissAnnotation: () => h.dismiss,
  useReanalyzeSection: () => h.reanalyze,
}));

const SECTION = (over = {}) => ({
  index: 1,
  heading: "Body",
  section_sha: "a".repeat(64),
  occurrence: 0,
  job: { status: null, error: null },
  findings: [
    {
      id: 7,
      kind: "uncited_supported",
      claim_text: "This is a factual claim",
      evidence_quote: "verified words",
      ref_label: "Smith 2020",
      can_insert: true,
      sentence_ordinal: 0,
    },
  ],
  ...over,
});

afterEach(() => {
  h.ann = { isLoading: false, isError: false, data: undefined };
  h.insert = { mutate: vi.fn(), isPending: false };
  h.dismiss = { mutate: vi.fn(), isPending: false };
  h.reanalyze = { mutate: vi.fn(), isPending: false };
});

describe("ClaimsPanel", () => {
  it("renders a finding with an Insert button for the owner", () => {
    h.ann.data = { is_owner: true, sections: [SECTION()] };
    render(<ClaimsPanel id="ws1" />);
    expect(screen.getByText("This is a factual claim")).toBeInTheDocument();
    expect(screen.getByText("Smith 2020")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /insert/i }));
    expect(h.insert.mutate).toHaveBeenCalledWith(7);
  });

  it("hides actions for a viewer", () => {
    h.ann.data = { is_owner: false, sections: [SECTION()] };
    render(<ClaimsPanel id="ws1" />);
    expect(screen.getByText("This is a factual claim")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /insert/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /dismiss/i })).toBeNull();
  });

  it("dismiss requires a reason then submits", () => {
    h.ann.data = {
      is_owner: true,
      sections: [
        SECTION({
          findings: [
            {
              id: 9,
              kind: "uncited_no_match",
              claim_text: "no match claim",
              evidence_quote: null,
              ref_label: "reference",
              can_insert: false,
              sentence_ordinal: 0,
            },
          ],
        }),
      ],
    };
    render(<ClaimsPanel id="ws1" />);
    fireEvent.click(screen.getByRole("button", { name: /dismiss/i }));
    // Confirm is disabled until a reason is typed.
    const confirm = screen.getByRole("button", { name: /confirm dismiss/i });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByPlaceholderText(/reason/i), {
      target: { value: "cited elsewhere" },
    });
    expect(confirm).not.toBeDisabled();
    fireEvent.click(confirm);
    expect(h.dismiss.mutate).toHaveBeenCalledWith({ annotationId: 9, reason: "cited elsewhere" });
  });

  it("shows analyzing… for a pending job and wires Reanalyze", () => {
    h.ann.data = {
      is_owner: true,
      sections: [SECTION({ findings: [], job: { status: "pending", error: null } })],
    };
    render(<ClaimsPanel id="ws1" />);
    expect(screen.getByText(/analyzing/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /reanalyze/i }));
    expect(h.reanalyze.mutate).toHaveBeenCalledWith(1);
  });

  it("renders the empty state when there are no sections", () => {
    h.ann.data = { is_owner: true, sections: [] };
    render(<ClaimsPanel id="ws1" />);
    expect(screen.getByText(/no draft yet/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run (from `frontend/`): `pnpm vitest run src/cockpit/ClaimsPanel.test.tsx`
Expected: FAIL (cannot resolve `./ClaimsPanel`).

- [ ] **Step 3: Create `ClaimsPanel.tsx`**

Create `frontend/src/cockpit/ClaimsPanel.tsx`:

```tsx
import { useState } from "react";

import {
  type ClaimFinding,
  type ClaimSection,
  useAnnotations,
  useDismissAnnotation,
  useInsertAnnotation,
  useReanalyzeSection,
} from "../api/annotations";

const KIND_LABEL: Record<string, string> = {
  uncited_supported: "Uncited · supported",
  uncited_no_match: "Uncited · no match",
  cited_unsupported: "Cited · unsupported",
};

export function ClaimsPanel({ id }: { id: string }) {
  const ann = useAnnotations(id);
  const insert = useInsertAnnotation(id);
  const dismiss = useDismissAnnotation(id);
  const reanalyze = useReanalyzeSection(id);

  if (ann.isLoading) return <p aria-busy="true">Loading claims…</p>;
  if (ann.isError || !ann.data) return <p role="alert">Could not load claims.</p>;

  const isOwner = ann.data.is_owner;
  if (ann.data.sections.length === 0)
    return <p className="claims-empty">No draft yet — claims appear once drafting begins.</p>;

  return (
    <div className="claims-panel">
      {ann.data.sections.map((sec) => (
        <ClaimSectionView
          key={`${sec.section_sha}:${sec.occurrence}`}
          section={sec}
          isOwner={isOwner}
          onInsert={(fid) => insert.mutate(fid)}
          onDismiss={(fid, reason) => dismiss.mutate({ annotationId: fid, reason })}
          onReanalyze={() => reanalyze.mutate(sec.index)}
        />
      ))}
    </div>
  );
}

function ClaimSectionView({
  section,
  isOwner,
  onInsert,
  onDismiss,
  onReanalyze,
}: {
  section: ClaimSection;
  isOwner: boolean;
  onInsert: (findingId: number) => void;
  onDismiss: (findingId: number, reason: string) => void;
  onReanalyze: () => void;
}) {
  return (
    <section className="claims-section">
      <header className="claims-section-head">
        <h4>{section.heading || "(preamble)"}</h4>
        {section.job.status === "pending" && (
          <span className="claims-job" role="status">
            analyzing…
          </span>
        )}
        {section.job.status === "failed" && (
          <span className="claims-job claims-job-failed" role="status">
            analysis failed{section.job.error ? ` — ${section.job.error}` : ""}
          </span>
        )}
        {isOwner && (
          <button type="button" onClick={onReanalyze}>
            Reanalyze
          </button>
        )}
      </header>
      {section.findings.length === 0 ? (
        <p className="claims-none">No open findings</p>
      ) : (
        <ul className="claims-findings">
          {section.findings.map((f) => (
            <FindingRow
              key={f.id}
              finding={f}
              isOwner={isOwner}
              onInsert={() => onInsert(f.id)}
              onDismiss={(reason) => onDismiss(f.id, reason)}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function FindingRow({
  finding,
  isOwner,
  onInsert,
  onDismiss,
}: {
  finding: ClaimFinding;
  isOwner: boolean;
  onInsert: () => void;
  onDismiss: (reason: string) => void;
}) {
  const [dismissing, setDismissing] = useState(false);
  const [reason, setReason] = useState("");

  return (
    <li className={`claims-finding kind-${finding.kind}`}>
      <span className="claims-kind">{KIND_LABEL[finding.kind] ?? finding.kind}</span>
      <p className="claims-claim">{finding.claim_text}</p>
      {finding.evidence_quote && <blockquote className="claims-quote">{finding.evidence_quote}</blockquote>}
      <span className="claims-ref">{finding.ref_label}</span>
      {isOwner && (
        <div className="claims-actions">
          {finding.can_insert && (
            <button type="button" onClick={onInsert}>
              Insert
            </button>
          )}
          {!dismissing ? (
            <button type="button" onClick={() => setDismissing(true)}>
              Dismiss
            </button>
          ) : (
            <span className="claims-dismiss-form">
              <input
                type="text"
                placeholder="Reason (required)"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
              <button
                type="button"
                aria-label="Confirm dismiss"
                disabled={reason.trim().length === 0}
                onClick={() => onDismiss(reason.trim())}
              >
                Confirm dismiss
              </button>
            </span>
          )}
        </div>
      )}
    </li>
  );
}
```

- [ ] **Step 4: Run the `ClaimsPanel` tests to verify they pass**

Run (from `frontend/`): `pnpm vitest run src/cockpit/ClaimsPanel.test.tsx`
Expected: PASS (5 tests).

- [ ] **Step 5: Wire the 6th tab into `BottomPanel.tsx`**

In `frontend/src/cockpit/BottomPanel.tsx`:

1. Add the import (alphabetical with the other panel imports):

```tsx
import { ClaimsPanel } from "./ClaimsPanel";
```

2. Add the open-findings count near the other counts (after `const auditCount = ...`):

```tsx
  const annotations = useAnnotations(id);
  const claimsCount =
    annotations.data?.sections.reduce((n, s) => n + s.findings.length, 0) ?? 0;
```

and add the matching import at the top:

```tsx
import { useAnnotations } from "../api/annotations";
```

3. Add the tab button after the Citations `<button>`:

```tsx
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "claims"}
            onClick={() => onTab("claims")}
          >
            Claims {claimsCount}
          </button>
```

4. Add the body after the Citations body line:

```tsx
          {activeTab === "claims" && <ClaimsPanel id={id} />}
```

- [ ] **Step 6: Update `BottomPanel.test.tsx` — mandatory (the real `useAnnotations` would throw)**

`BottomPanel.test.tsx` mocks every api hook and renders WITHOUT a `QueryClientProvider`. Now that `BottomPanel` calls `useAnnotations`, the test MUST mock it too — otherwise the real `useQuery` runs and throws "No QueryClient set". Add these two mocks alongside the existing `vi.mock` calls (e.g. after `vi.mock("../api/audit", …)` / `vi.mock("./CitationsPanel", …)`):

```tsx
vi.mock("../api/annotations", () => ({ useAnnotations: () => ({ data: { is_owner: true, sections: [] } }) }));
vi.mock("./ClaimsPanel", () => ({ ClaimsPanel: () => <div>claims-body</div> }));
```

(No existing assertion in the file enumerates the full tab set, so no badge-list assertion needs editing. If you add a Claims-specific assertion, the mocked `sections: []` yields a `Claims 0` badge.)

Run (from `frontend/`): `pnpm vitest run src/cockpit/BottomPanel.test.tsx`
Expected: PASS.

- [ ] **Step 7: Full frontend gate**

Run (from `frontend/`):
```
pnpm tsc -b
pnpm vitest run
pnpm build
```
Expected: tsc clean; vitest all green; build clean.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/cockpit/ClaimsPanel.tsx frontend/src/cockpit/ClaimsPanel.test.tsx frontend/src/cockpit/BottomPanel.tsx
git commit -m "feat(spa): Claims bottom-panel tab (findings + insert/dismiss/reanalyze)"
```

---

## Final Verification (before handing off to review / merge)

- [ ] Backend boots: `uv run python -c "import call_manager.main; call_manager.main.create_app(); print('ok')"`
- [ ] New backend tests + Jinja regression net green:
  `uv run pytest tests/proposal_assist/test_api_annotations_read.py tests/proposal_assist/test_api_annotations_insert.py tests/proposal_assist/test_api_annotations_dismiss.py tests/proposal_assist/test_api_annotations_reanalyze.py tests/test_web_annotations.py -q`
  (If testcontainers Postgres flakes under load — connection-refused / DeadlockDetected on TRUNCATE — re-run the flagged file in isolation; that is infra, not a real failure.)
- [ ] `uv run ruff check src/call_manager/web/api/annotations.py src/call_manager/proposal_assist/analysis/queue.py src/call_manager/proposal_assist/web/annotations.py`
- [ ] Frontend: `cd frontend && pnpm tsc -b && pnpm vitest run && pnpm build`
- [ ] OpenAPI additive: `git diff <merge-base> -- frontend/src/api/schema.gen.ts` shows added annotation paths, no removed `/api` paths; `frontend/src/routes.gen.ts` unchanged.
- [ ] Additive-only: `git diff --stat <merge-base>` touches only the files in the File Structure; the Jinja `web/annotations.py` change is solely the Task 0 extract-and-repoint (no Jinja route/template deleted — that is G2).

---

## Self-Review (completed during planning)

**Spec coverage:** §3.1 read-model → Task 1; insert → Task 2; dismiss → Task 3; reanalyze → Task 4; the §3.1 `enqueue_containing_section` extract-and-repoint prerequisite → Task 0; §3.2 hooks + panel + 6th tab → Tasks 6–7; §6 OpenAPI regen → Task 5; the §3.1 degrade-to-empty behavior change → Task 1 Step 1/Step 3. All covered.

**Type consistency:** the Pydantic model names (`AnnotationsOut`, `ClaimSectionOut`, `ClaimFindingOut`, `ClaimJobOut`, `InsertResultOut`, `DismissReasonIn`, `DismissResultOut`, `ReanalyzeResultOut`) are used identically in the backend (Tasks 1–4), the regen check (Task 5), and the frontend `components["schemas"][…]` references (Task 6). `enqueue_containing_section`'s signature (`workspace_id`, not `ws_id`) matches between its definition (Task 0) and both call sites (Jinja repoint Task 0, `/api` insert Task 2).

**Placeholder scan:** no TBD/"add error handling"/"similar to Task N" — every code step carries complete code; every run step carries the exact command + expected output.

**Post-write adversarial review (verified against the codebase, 4 findings applied):**
1. *Critical (tsc):* the `{annotation_id}` / `{n}` path params are `int` → openapi-fetch types them `number`; the hooks/panel/tests now use `number` end-to-end (confirmed against `api/editor.ts`, which passes a numeric `{index}`). Passing a string would fail `pnpm tsc -b`.
2. *Important (ruff F401):* `web/api/annotations.py` is built across Tasks 1–4; importing the full symbol set in Task 1 would fail Task 1's own `ruff check`. Imports are now added incrementally per task, and `_annotation_or_404` is defined in Task 2 (its first user).
3. *Important (vitest):* `BottomPanel.test.tsx` renders without a `QueryClientProvider` and mocks every api hook; adding `useAnnotations` to `BottomPanel` makes the real hook throw "No QueryClient set". Task 7 Step 6 now mandates mocking `../api/annotations` + `./ClaimsPanel`.
4. *Minor:* the Jinja `/workspaces/.../annotations` paths already exist in `schema.gen.ts`, so a bare `grep annotations` proves nothing — Task 5 now greps the `/api`-prefixed key specifically.

**Verified-and-sound (no change needed):** the read-model occurrence math (`sum earlier same-sha` == pipeline's `occ_idx`); the `enqueue_containing_section` cycle-safe `PROPOSAL_FILENAME`-from-`artifacts` import (Task 0); the `_app.state.settings.workspaces_dir = str(tmp_path)` test idiom (matches the merged `test_api_workspaces_delete.py`); 503→429 busy remap; namespaced model names avoiding the existing `SectionOut`.
