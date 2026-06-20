# Cockpit SP2b-1 — Artifact-aware foundation + Spec tab — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generalize the proposal-assist save/read layer over an artifact enum (`proposal.md` | `proposal-spec.md`) and add the Spec as a first-class **editable, citation-free** tab in the cockpit.

**Architecture:** One `Artifact` StrEnum + `filename_for` consolidates the hardcoded filename. `save_section` gains a defaulted `artifact` param and gates the two draft-only post-commit steps (Tier-1 citation validation, Tier-2 analysis enqueue) behind `artifact is Artifact.DRAFT`; everything else (mutex, staleness, structure guard, provenance) is shared. Parallel `/spec` routes (read/save/init) reuse the draft conventions over a citation-free `SpecOut`. The frontend extracts a reusable `<ArtifactSectionEditor>` from `Cockpit.tsx` with **identity-based (`section.index`) lookup**; `CodeMirrorEditor` gains a `citations` prop so the Spec renders raw editable text.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x / pytest + testcontainers Postgres / `uv` / `ruff`; React 18 / Vite / Vitest / TanStack Query / openapi-typescript / `pnpm` (Node 22 via nvm).

**Spec:** `docs/superpowers/specs/2026-06-20-cockpit-sp2b-1-artifact-aware-spec-tab-design.md` (committed `8db0cb1`).

**Worktree:** all work happens in `.worktrees/cockpit-sp2b-1` (branch `cockpit-sp2b-1`, off master `069c2ce`). Every command's cwd is the worktree root unless noted; frontend commands run from `<worktree>/frontend`.

**Conventions every task follows:**
- Backend tests: `uv run pytest <path>::<name> -v`. The **full** suite is testcontainer-flaky (connection-refused under load) — re-run any failure in isolation before treating it as real.
- Frontend tests: from `frontend/`, `pnpm vitest run <path>` (single) / `pnpm test` (full). Run `pnpm install` after pulling if deps changed.
- Lint: `uv run ruff check <changed.py>` + `uv run ruff format <changed.py>` for Python; `pnpm lint` for TS.
- Commit trailer on every commit: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- TDD: write the failing test, watch it fail, implement minimally, watch it pass, commit.

**Task map to spec §12:** (1) artifacts.py; (2) save_section; (3) `_sections_out` + GET /spec; (4) POST /spec/sections; (5) skeleton + POST /spec init; (6) regen schema; (7) `CodeMirrorEditor citations` prop; (8) extract `<ArtifactSectionEditor>` (identity lookup); (9) Spec hooks + tab + CTA; (10) full verification. (§12's "task 7" is split into 7+8 here so each stays bite-sized.)

---

## File Structure

**Backend — created:**
- `src/call_manager/proposal_assist/artifacts.py` — the `Artifact` enum, `filename_for`, `PROPOSAL_FILENAME`/`SPEC_FILENAME` re-exports, `SPEC_SKELETON`. Single source of truth for artifact↔filename.
- `tests/proposal_assist/test_artifacts.py` — enum/filename unit tests.
- `tests/proposal_assist/test_save_section_artifact.py` — `save_section(artifact=SPEC)` unit tests + draft-regression assertions on the gated steps.
- `tests/test_api_spec.py` — GET/POST `/spec` route tests (mirrors `tests/test_api_workspaces.py`).

**Backend — modified:**
- `src/call_manager/proposal_assist/editing/save.py` — import `PROPOSAL_FILENAME` from `artifacts`; add `artifact` param to `save_section`; gate Tier-1/Tier-2.
- `src/call_manager/proposal_assist/tools/proposal.py` — import `PROPOSAL_FILENAME` from `artifacts` (drop the local literal).
- `src/call_manager/web/api/workspaces.py` — add `_sections_out` helper, `SpecOut`, and the three `/spec` routes; refactor `get_draft` onto `_sections_out`.

**Frontend — created:**
- `frontend/src/editor/ArtifactSectionEditor.tsx` — the reusable section-editing harness extracted from `Cockpit.tsx`, parameterized by `(data, save, queryKey, citations, fullViewLabel, …)`.
- `frontend/src/editor/ArtifactSectionEditor.test.tsx` — identity-lookup regression + citation-gating render tests.
- `frontend/src/cockpit/DraftPanel.tsx` — the draft wrapper (`useDraft` + `<ArtifactSectionEditor citations>`), extracted from Task 8's thin Cockpit.
- `frontend/src/cockpit/SpecPanel.tsx` — the spec wrapper (`useSpec`/`useInitSpec` + empty-state CTA + `<ArtifactSectionEditor citations={false} fullViewLabel="Full spec">`).
- `frontend/src/cockpit/SpecPanel.test.tsx` — empty-state CTA → init → editor shows skeleton; non-empty → editor.

**Frontend — modified:**
- `frontend/src/editor/CodeMirrorEditor.tsx` — add `citations?: boolean` prop gating the `citationPills` extension + the selection bridge.
- `frontend/src/editor/CodeMirrorEditor.test.tsx` — add the citation-gating tests.
- `frontend/src/api/editor.ts` — add `useSpec` / `useSaveSpecSection` / `useInitSpec`.
- `frontend/src/api/editor.test.tsx` — add the spec-hook tests.
- `frontend/src/routes/Cockpit.tsx` — become the `[Draft | Spec]` artifact-tab shell rendering `<DraftPanel>` / `<SpecPanel>` (draft behavior unchanged; default tab = Draft).
- `frontend/src/api/schema.gen.ts` — regenerated for the three `/spec` routes (Task 6).

The Spec is an in-cockpit artifact tab (cockpit design's "center View tab"), so `router.tsx` and `routes.json` (`spa_routes`) are **untouched**.

---

## Task 1: `artifacts.py` + consolidate `PROPOSAL_FILENAME`

**Files:**
- Create: `src/call_manager/proposal_assist/artifacts.py`
- Create: `tests/proposal_assist/test_artifacts.py`
- Modify: `src/call_manager/proposal_assist/editing/save.py:33`
- Modify: `src/call_manager/proposal_assist/tools/proposal.py:23`

The string values `"draft"`/`"spec"` MUST match `ProposedChange.target_artifact`'s CHECK (`db/models.py:958`); `proposal.md`/`proposal-spec.md` match the files seeded by setup (`phases/setup.py:217-221`).

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_artifacts.py`:

```python
from call_manager.proposal_assist.artifacts import (
    PROPOSAL_FILENAME,
    SPEC_FILENAME,
    Artifact,
    filename_for,
)


def test_filename_for_each_artifact():
    assert filename_for(Artifact.DRAFT) == "proposal.md"
    assert filename_for(Artifact.SPEC) == "proposal-spec.md"


def test_enum_values_match_proposed_change_check():
    # These string values are persisted in ProposedChange.target_artifact and
    # must match the DB CHECK ('draft','spec') — db/models.py:958.
    assert Artifact.DRAFT == "draft"
    assert Artifact.SPEC == "spec"


def test_strenum_normalizes_a_raw_string():
    # A raw "draft" string round-trips to the member (used by save_section's
    # entry normalization so a stringly-typed caller can't skip Tier-1/Tier-2).
    assert Artifact("draft") is Artifact.DRAFT
    assert str(Artifact.SPEC) == "spec"


def test_back_compat_constants():
    assert PROPOSAL_FILENAME == "proposal.md"
    assert SPEC_FILENAME == "proposal-spec.md"
```

- [ ] **Step 2: Run it; verify it fails**

Run: `uv run pytest tests/proposal_assist/test_artifacts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'call_manager.proposal_assist.artifacts'`.

- [ ] **Step 3: Create `artifacts.py`**

```python
"""Single source of truth for the proposal-assist artifacts and their files.

`proposal.md` (the draft) and `proposal-spec.md` (the spec) live in one git
repo per workspace. `Artifact`'s string values MUST match the
`ProposedChange.target_artifact` CHECK ('draft','spec') in db/models.py and the
files seeded by phases/setup.py. PROPOSAL_FILENAME is re-exported for the
existing importers in editing/save.py and tools/proposal.py.
"""

from __future__ import annotations

from enum import StrEnum


class Artifact(StrEnum):
    DRAFT = "draft"  # values MUST match ProposedChange.target_artifact (db/models.py:958)
    SPEC = "spec"


_FILENAMES: dict[Artifact, str] = {
    Artifact.DRAFT: "proposal.md",
    Artifact.SPEC: "proposal-spec.md",
}


def filename_for(artifact: Artifact) -> str:
    return _FILENAMES[Artifact(artifact)]


PROPOSAL_FILENAME = filename_for(Artifact.DRAFT)  # back-compat re-export
SPEC_FILENAME = filename_for(Artifact.SPEC)

# The conventional starting Spec (cockpit design §8). A whitespace-only spec is
# seeded with this by POST /spec (Task 5). Leads with `## Aims`, so split_sections
# yields an empty index-0 preamble + 5 H2 sections (6 total).
SPEC_SKELETON = (
    "## Aims\n\n"
    "## Approach\n\n"
    "## Novelty & Significance\n\n"
    "## Expected Impact\n\n"
    "## Key Claims\n"
)
```

(`SPEC_SKELETON` is defined here now so Task 5 only wires it; Task 1's tests don't assert it.)

- [ ] **Step 4: Run it; verify it passes**

Run: `uv run pytest tests/proposal_assist/test_artifacts.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Consolidate the duplicated constant**

In `src/call_manager/proposal_assist/editing/save.py`, replace the literal definition at line 33:

```python
# was: PROPOSAL_FILENAME = "proposal.md"
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME  # noqa: F401  (re-export)
```

Place that import in the existing import block (near line 29, after the `paths` import), and DELETE the `PROPOSAL_FILENAME = "proposal.md"` line. Keep `CITATIONS_FILENAME = "citations.json"` (not artifact-related). **The `# noqa: F401` is load-bearing:** in Task 1 the name is still used locally (save.py:172, :233), but Task 2 replaces those with `filename_for(artifact)`, leaving this a *pure re-export*. Four modules import `PROPOSAL_FILENAME` from `editing.save` (`citations/human_cite.py:44`, `web/annotations.py:47`, `tests/test_api_workspaces.py:21`, `tests/proposal_assist/citations/test_human_cite.py:19`), so the export must survive — without the noqa, ruff (`F` is selected) flags F401 and a worker "fixing" it would break all four. (`RUF100`/unused-noqa is not selected, so the noqa is harmless while the name is still used in Task 1.)

In `src/call_manager/proposal_assist/tools/proposal.py`, replace line 23:

```python
# was: PROPOSAL_FILENAME = "proposal.md"
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
```

Place in the import block (after line 19) and delete the literal. `tests/proposal_assist/test_save_section_no_freeze.py:7` imports `PROPOSAL_FILENAME` from `tools.proposal` — still resolves.

- [ ] **Step 6: Verify nothing regressed**

Run: `uv run pytest tests/proposal_assist/test_save_section_no_freeze.py tests/test_editing_save.py tests/test_api_workspaces.py -v`
Expected: PASS (no import breakage; behavior unchanged).

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff format src/call_manager/proposal_assist/artifacts.py src/call_manager/proposal_assist/editing/save.py src/call_manager/proposal_assist/tools/proposal.py tests/proposal_assist/test_artifacts.py
uv run ruff check src/call_manager/proposal_assist/artifacts.py src/call_manager/proposal_assist/editing/save.py src/call_manager/proposal_assist/tools/proposal.py
git add src/call_manager/proposal_assist/artifacts.py src/call_manager/proposal_assist/editing/save.py src/call_manager/proposal_assist/tools/proposal.py tests/proposal_assist/test_artifacts.py
git commit -m "feat(proposal-assist): Artifact enum + filename_for, consolidate PROPOSAL_FILENAME

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Generalize `save_section` over the artifact

**Files:**
- Modify: `src/call_manager/proposal_assist/editing/save.py:133-283`
- Create: `tests/proposal_assist/test_save_section_artifact.py`

`save_section` keeps its exact contract for the draft; the spec path is the same function **minus** the two draft-only steps. Gating uses `artifact is Artifact.DRAFT` after an entry normalization (`artifact = Artifact(artifact)`) so a stringly-typed caller can't silently skip Tier-1/Tier-2.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/test_save_section_artifact.py`:

```python
import subprocess
from pathlib import Path

from call_manager.db.models import ArtifactVersion, ClaimAnalysisJob, WorkspaceEvent
from call_manager.proposal_assist.artifacts import Artifact
from call_manager.proposal_assist.citations.gitio import _git_env
from call_manager.proposal_assist.editing.save import save_section
from call_manager.proposal_assist.editing.sectionizer import split_sections
from tests.proposal_assist.conftest import make_workspace

SPEC_DOC = "## Aims\nbe great.\n\n## Approach\ndo science.\n"


def _init_spec_repo(repo_dir: Path, content: str) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    env = _git_env()
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True, env=env)
    (repo_dir / "proposal-spec.md").write_text(content)
    subprocess.run(["git", "add", "proposal-spec.md"], cwd=repo_dir, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init spec"], cwd=repo_dir, check=True, env=env)
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True, env=env
    )
    return out.stdout.strip()


def test_spec_save_commits_spec_file_and_skips_tier1_tier2(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        # The draft path would enqueue a ClaimAnalysisJob (Tier-2); the spec path must enqueue none.
        repo = Path(tmp_path) / str(ws.id) / "repo"
        head = _init_spec_repo(repo, SPEC_DOC)
        sec_sha = split_sections(SPEC_DOC)[1].sha  # the "## Aims" section

        result = save_section(
            session=s,
            workspace=ws,
            user_id=ws.owner_id,
            section_index=1,
            new_text="## Aims\nbe truly great.\n",
            base_commit_sha=head,
            base_section_sha=sec_sha,
            workspaces_dir=str(tmp_path),
            artifact=Artifact.SPEC,
        )
        s.flush()

        # proposal-spec.md was rewritten; proposal.md was NOT created.
        assert (repo / "proposal-spec.md").read_text().startswith("## Aims\nbe truly great.")
        assert not (repo / "proposal.md").exists()
        # Spec path skips Tier-1 (no issues) and Tier-2 (no enqueue).
        assert result.issues == []
        assert result.job_enqueued is False
        assert result.changed is True
        assert (
            s.query(ClaimAnalysisJob).filter(ClaimAnalysisJob.workspace_id == ws.id).count() == 0
        )
        # Provenance is tagged spec.
        av = s.query(ArtifactVersion).filter(ArtifactVersion.workspace_id == ws.id).one()
        assert av.message == "user spec edit: Aims"
        ev = s.query(WorkspaceEvent).filter(WorkspaceEvent.workspace_id == ws.id).one()
        assert ev.payload["artifact"] == "spec"


def test_spec_save_still_enforces_structure_guard(pg_session_factory, tmp_path):
    from call_manager.proposal_assist.editing.save import SectionStructureError
    import pytest

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        repo = Path(tmp_path) / str(ws.id) / "repo"
        head = _init_spec_repo(repo, SPEC_DOC)
        sec_sha = split_sections(SPEC_DOC)[1].sha
        with pytest.raises(SectionStructureError):
            save_section(
                session=s,
                workspace=ws,
                user_id=ws.owner_id,
                section_index=1,
                new_text="be great.\n",  # dropped the "## Aims" heading
                base_commit_sha=head,
                base_section_sha=sec_sha,
                workspaces_dir=str(tmp_path),
                artifact=Artifact.SPEC,
            )


def test_raw_draft_string_still_runs_draft_pipeline(pg_session_factory, tmp_path):
    # Normalization: a stringly-typed "draft" must hit Tier-1 (issues computed),
    # not silently degrade to the spec path.
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        doc = "preamble\n\n## Methods\nSee [@ghost].\n"  # bare marker -> a Tier-1 issue
        repo = Path(tmp_path) / str(ws.id) / "repo"
        env = _git_env()
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
        (repo / "proposal.md").write_text(doc)
        subprocess.run(["git", "add", "proposal.md"], cwd=repo, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=repo, check=True, env=env)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True, env=env
        ).stdout.strip()
        sec_sha = split_sections(doc)[1].sha

        result = save_section(
            session=s,
            workspace=ws,
            user_id=ws.owner_id,
            section_index=1,
            new_text="## Methods\nSee [@ghost] again.\n",
            base_commit_sha=head,
            base_section_sha=sec_sha,
            workspaces_dir=str(tmp_path),
            artifact="draft",  # raw string, not the enum member
        )
        assert any(i["type"] == "bare_marker" for i in result.issues)


def test_spec_save_survives_cross_artifact_head_move(pg_session_factory, tmp_path):
    # Spec §7/§10: a DRAFT-only commit advances repo HEAD but does NOT change the
    # spec section's bytes, so a spec save with the pre-draft base_commit_sha + the
    # still-current base_section_sha succeeds (shared predicate: head != base_commit
    # AND live.sha != base_section -> stale; here the section sha is unchanged).
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        repo = Path(tmp_path) / str(ws.id) / "repo"
        head0 = _init_spec_repo(repo, SPEC_DOC)
        sec_sha = split_sections(SPEC_DOC)[1].sha
        # Draft-only git commit -> HEAD moves, spec section bytes unchanged.
        env = _git_env()
        (repo / "proposal.md").write_text("draft body\n")
        subprocess.run(["git", "add", "proposal.md"], cwd=repo, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "draft move"], cwd=repo, check=True, env=env)

        result = save_section(
            session=s,
            workspace=ws,
            user_id=ws.owner_id,
            section_index=1,
            new_text="## Aims\nrevised under a moved HEAD.\n",
            base_commit_sha=head0,  # PRE-draft HEAD (now != current HEAD)
            base_section_sha=sec_sha,  # spec section UNCHANGED -> not stale
            workspaces_dir=str(tmp_path),
            artifact=Artifact.SPEC,
        )
        assert result.changed is True


def test_spec_save_stale_section_raises(pg_session_factory, tmp_path):
    import pytest

    from call_manager.proposal_assist.editing.save import StaleSectionError

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        repo = Path(tmp_path) / str(ws.id) / "repo"
        head0 = _init_spec_repo(repo, SPEC_DOC)
        sec_sha = split_sections(SPEC_DOC)[1].sha
        # First spec save changes the section bytes (HEAD moves, section sha changes).
        save_section(
            session=s,
            workspace=ws,
            user_id=ws.owner_id,
            section_index=1,
            new_text="## Aims\nfirst change.\n",
            base_commit_sha=head0,
            base_section_sha=sec_sha,
            workspaces_dir=str(tmp_path),
            artifact=Artifact.SPEC,
        )
        s.flush()
        # A second save with the ORIGINAL (now stale) base hits
        # head != base_commit AND live.sha != base_section -> StaleSectionError.
        with pytest.raises(StaleSectionError):
            save_section(
                session=s,
                workspace=ws,
                user_id=ws.owner_id,
                section_index=1,
                new_text="## Aims\nconflicting change.\n",
                base_commit_sha=head0,
                base_section_sha=sec_sha,
                workspaces_dir=str(tmp_path),
                artifact=Artifact.SPEC,
            )
```

> **§6 reconcile-safety is structural, not separately tested.** Spec §6/§10 note that the
> analysis/reconcile tick must ignore `proposal-spec.md`. That is guaranteed by construction:
> `analysis/reconcile.py:46` and `analysis/invalidation.py:46` read `proposal.md` by hardcoded
> literal and never touch `proposal-spec.md`. The `assert ... ClaimAnalysisJob ... == 0` above
> proves a *spec save* enqueues no analysis work; it is **not** a stand-in for the reconcile
> claim, which needs no test because no reconcile code path reads the spec file.

- [ ] **Step 2: Run; verify they fail**

Run: `uv run pytest tests/proposal_assist/test_save_section_artifact.py -v`
Expected: FAIL — `save_section() got an unexpected keyword argument 'artifact'`.

- [ ] **Step 3: Generalize `save_section`**

In `src/call_manager/proposal_assist/editing/save.py`, add to the imports (after line 29):

```python
from call_manager.proposal_assist.artifacts import Artifact, filename_for
```

Change the signature (line 133-143) to add the defaulted param as the LAST keyword:

```python
def save_section(
    *,
    session: Session,
    workspace: Workspace,
    user_id: int,
    section_index: int,
    new_text: str,
    base_commit_sha: str,
    base_section_sha: str,
    workspaces_dir: Path | str,
    artifact: Artifact = Artifact.DRAFT,
) -> SaveResult:
```

Immediately after `acquire_commit_mutex(session, workspace.id)` (line 159), normalize:

```python
    artifact = Artifact(artifact)  # normalize a stringly-typed caller
```

Replace the read (line 172) `doc = (repo_dir / PROPOSAL_FILENAME).read_text()` with:

```python
    fname = filename_for(artifact)
    doc = (repo_dir / fname).read_text()
```

Replace the no-change short-circuit (lines 214-225) with the gated form:

```python
    if new_text == live.text:
        issues = (
            _tier1_issues(doc=doc, repo_dir=repo_dir, session=session, workspace_id=workspace.id)
            if artifact is Artifact.DRAFT
            else []
        )
        return SaveResult(
            commit_sha=head,
            head=head,
            section_sha=live.sha,
            issues=issues,
            job_enqueued=False,
            changed=False,
        )
```

Replace the commit + provenance block (lines 229-259) so the filename and the message/payload carry the artifact:

```python
    new_doc = "".join(new_text if s.index == section_index else s.text for s in sections)
    heading = live.heading or "preamble"
    commit_sha = write_and_commit_files(
        repo_dir,
        {fname: new_doc},
        f"user {artifact} edit: {heading} (user:{user_id})",
    )

    session.add(
        ArtifactVersion(
            workspace_id=workspace.id,
            commit_sha=commit_sha,
            created_by="user",
            message=f"user {artifact} edit: {heading}",
        )
    )
    session.add(
        WorkspaceEvent(
            workspace_id=workspace.id,
            kind="user_edit",
            actor_id=user_id,
            payload={
                "section_index": section_index,
                "heading": heading,
                "commit_sha": commit_sha,
                "byte_delta": len(new_doc.encode()) - len(doc.encode()),
                "artifact": str(artifact),
            },
        )
    )
```

(`artifact` is a `StrEnum`, so `f"user {artifact} edit"` renders `"user draft edit"` / `"user spec edit"` and `str(artifact)` is `"draft"`/`"spec"`.)

Replace the Tier-1 + Tier-2 tail (lines 261-283) with the gated form:

```python
    saved = split_sections(new_doc)[section_index]
    if artifact is Artifact.DRAFT:
        # 9. Tier-1 deterministic validation of the NEW document.
        issues = _tier1_issues(
            doc=new_doc, repo_dir=repo_dir, session=session, workspace_id=workspace.id
        )
        # 10. Tier-2: enqueue claim analysis for the saved section's new content.
        job_enqueued = enqueue_section_job(
            session,
            workspace_id=workspace.id,
            section_index=saved.index,
            section_heading=saved.heading,
            section_sha=saved.sha,
            commit_sha=commit_sha,
        )
    else:
        # Spec: no citation validation, no claim analysis (spec §3).
        issues = []
        job_enqueued = False
    return SaveResult(
        commit_sha=commit_sha,
        head=commit_sha,
        section_sha=saved.sha,
        issues=issues,
        job_enqueued=job_enqueued,
        changed=True,
    )
```

- [ ] **Step 4: Run the new + the regression tests; verify pass**

Run: `uv run pytest tests/proposal_assist/test_save_section_artifact.py tests/test_editing_save.py tests/proposal_assist/test_save_section_no_freeze.py -v`
Expected: PASS. (test_editing_save asserts only `ev.kind`/`section_index`/`commit_sha` — the additive `artifact` payload key and the message change don't break it.)

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff format src/call_manager/proposal_assist/editing/save.py tests/proposal_assist/test_save_section_artifact.py
uv run ruff check src/call_manager/proposal_assist/editing/save.py
git add src/call_manager/proposal_assist/editing/save.py tests/proposal_assist/test_save_section_artifact.py
git commit -m "feat(editing): save_section generalized over artifact, draft-only Tier-1/Tier-2 gated

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Shared `_sections_out` builder + `GET /spec`

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py` (imports; add `_sections_out`, `SpecOut`, `get_spec`; refactor `get_draft`)
- Create: `tests/test_api_spec.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api_spec.py`:

```python
"""Cockpit /spec JSON surface (GET/POST/init). Mirrors tests/test_api_workspaces.py:
same _app/_seed/_client helpers and production repo layout."""

from __future__ import annotations

import subprocess

from call_manager.proposal_assist.citations.gitio import _git_env
from call_manager.proposal_assist.paths import workspace_repo_dir

from tests.test_api_workspaces import _app, _client, _seed


def _add_spec(tmp_path, ws_id, content=""):
    """Write + commit proposal-spec.md into the already-initialized repo."""
    repo = workspace_repo_dir(str(tmp_path), ws_id)
    (repo / "proposal-spec.md").write_text(content)
    env = _git_env()
    subprocess.run(["git", "add", "proposal-spec.md"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "seed spec"], cwd=repo, check=True, env=env)


def test_get_spec_returns_sections_no_citations(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft preamble\n")
    _add_spec(tmp_path, ws_id, "## Aims\nbe great.\n")
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/spec")
    assert r.status_code == 200
    body = r.json()
    assert "citations" not in body  # SpecOut has no citations field
    assert body["can_edit"] is True
    assert [s["index"] for s in body["sections"]] == [0, 1]  # empty preamble + Aims
    assert body["sections"][1]["heading"] == "Aims"


def test_get_spec_member_allowed_stranger_forbidden(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, stranger_id, ws_id = _seed(
        app, pg_session_factory, tmp_path, "draft\n"
    )
    _add_spec(tmp_path, ws_id, "## Aims\nx\n")
    assert _client(app, member_id).get(f"/api/workspaces/{ws_id}/spec").status_code == 200
    assert _client(app, stranger_id).get(f"/api/workspaces/{ws_id}/spec").status_code == 403


def test_get_spec_404_when_file_absent(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    # No _add_spec -> proposal-spec.md does not exist.
    assert _client(app, owner_id).get(f"/api/workspaces/{ws_id}/spec").status_code == 404
```

- [ ] **Step 2: Run; verify they fail**

Run: `uv run pytest tests/test_api_spec.py -v`
Expected: FAIL — 404 on every `/spec` GET (route not registered), so the member/stranger assertions fail.

- [ ] **Step 3: Add imports, `SpecOut`, `_sections_out`, and `get_spec`**

In `src/call_manager/web/api/workspaces.py`, add to the imports (with the other proposal_assist imports near line 31):

```python
from call_manager.proposal_assist.artifacts import SPEC_FILENAME
```

Add `SpecOut` next to `DraftOut` (after line 86):

```python
class SpecOut(BaseModel):
    head: str
    can_edit: bool
    sections: list[SectionOut]  # no citations: the spec is citation-free
```

Extract the section-builder. Add this helper just above `get_draft` (before line 178):

```python
def _sections_out(raw: str) -> list[SectionOut]:
    """Split a raw artifact document into the wire SectionOut list. Shared by
    get_draft and get_spec so both produce identical section semantics."""
    return [
        SectionOut(index=s.index, heading=s.heading, text=s.text, sha=s.sha)
        for s in split_sections(raw)
    ]
```

Refactor `get_draft` (lines 199-202) to use it:

```python
        raw = proposal.read_text()
        sections = _sections_out(raw)
        citations = _citation_map(session, settings, workspace_id, raw)
```

Add the read route after `get_draft` (after line 205):

```python
@router.get("/workspaces/{workspace_id}/spec", response_model=SpecOut)
def get_spec(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SpecOut:
    """Read the Spec artifact: outline + section bytes, no citations. Member-gated."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_member(workspace_id=workspace_id, user=user, session=session)
        can_edit = ws.owner_id == user.id
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        spec = repo_dir / SPEC_FILENAME
        if not spec.exists():
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet")
        try:
            head = current_head(repo_dir)
        except GitCommitError as e:
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet") from e
        sections = _sections_out(spec.read_text())
    return SpecOut(head=head, can_edit=can_edit, sections=sections)
```

- [ ] **Step 4: Run the spec + draft-regression tests; verify pass**

Run: `uv run pytest tests/test_api_spec.py tests/test_api_workspaces.py -v`
Expected: PASS (get_draft still green after the `_sections_out` refactor).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff format src/call_manager/web/api/workspaces.py tests/test_api_spec.py
uv run ruff check src/call_manager/web/api/workspaces.py
git add src/call_manager/web/api/workspaces.py tests/test_api_spec.py
git commit -m "feat(api): GET /workspaces/{id}/spec + shared _sections_out builder

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: `POST /spec/sections/{index}`

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py` (add `post_save_spec_section`)
- Modify: `tests/test_api_spec.py` (add save tests)

Reuses `SaveSectionIn` verbatim (`{content, base_commit_sha, base_section_sha}`) and the exact exception→status mapping from `post_save_section` (`workspaces.py:370-403`): `StaleSectionError→409`, `SectionStructureError→400`, `CommitMutexTimeout→429+Retry-After`, `GitCommitError→500`, `IndexError→404`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api_spec.py`:

```python
def test_save_spec_section_owner_commits(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nbe great.\n")
    c = _client(app, owner_id)
    spec = c.get(f"/api/workspaces/{ws_id}/spec").json()
    aims = spec["sections"][1]
    r = c.post(
        f"/api/workspaces/{ws_id}/spec/sections/1",
        json={
            "content": "## Aims\nbe truly great.\n",
            "base_commit_sha": spec["head"],
            "base_section_sha": aims["sha"],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is True
    assert body["issues"] == []  # spec path skips Tier-1
    assert body["job_enqueued"] is False


def test_save_spec_section_structure_guard_is_400(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nbe great.\n")
    c = _client(app, owner_id)
    spec = c.get(f"/api/workspaces/{ws_id}/spec").json()
    aims = spec["sections"][1]
    r = c.post(
        f"/api/workspaces/{ws_id}/spec/sections/1",
        json={
            "content": "be great.\n",  # dropped the ## heading
            "base_commit_sha": spec["head"],
            "base_section_sha": aims["sha"],
        },
    )
    assert r.status_code == 400


def test_save_spec_section_member_forbidden(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nx\n")
    spec = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/spec").json()
    r = _client(app, member_id).post(
        f"/api/workspaces/{ws_id}/spec/sections/1",
        json={
            "content": "## Aims\ny\n",
            "base_commit_sha": spec["head"],
            "base_section_sha": spec["sections"][1]["sha"],
        },
    )
    assert r.status_code == 403
```

- [ ] **Step 2: Run; verify they fail**

Run: `uv run pytest tests/test_api_spec.py -k spec_section -v`
Expected: FAIL — 404/405 (route not registered).

- [ ] **Step 3: Add the save route**

In `src/call_manager/web/api/workspaces.py`, ensure `Artifact` is imported (extend the Task-3 artifacts import):

```python
from call_manager.proposal_assist.artifacts import SPEC_FILENAME, Artifact
```

Add the route after `get_spec`:

```python
@router.post(
    "/workspaces/{workspace_id}/spec/sections/{index}", response_model=SaveResultOut
)
def post_save_spec_section(
    workspace_id: uuid.UUID,
    index: int,
    body: SaveSectionIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SaveResultOut:
    """Save one Spec section via the shared write path (artifact=spec). Owner-gated.
    Same body + status mapping as post_save_section; the spec path skips Tier-1/Tier-2."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        if not (repo_dir / SPEC_FILENAME).exists():
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet")
        try:
            current_head(repo_dir)
        except GitCommitError as e:
            logger.warning("workspace %s: git HEAD unavailable (%s)", workspace_id, e)
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet") from e
        try:
            result = save_section(
                session=session,
                workspace=ws,
                user_id=user.id,
                section_index=index,
                new_text=body.content,
                base_commit_sha=body.base_commit_sha,
                base_section_sha=body.base_section_sha,
                workspaces_dir=settings.workspaces_dir,
                artifact=Artifact.SPEC,
            )
            session.commit()
        except StaleSectionError as e:
            session.rollback()
            raise HTTPException(
                status_code=409, detail="this section changed underneath you — reopen it"
            ) from e
        except SectionStructureError as e:
            session.rollback()
            raise HTTPException(
                status_code=400,
                detail="keep the section's ## heading line — removing or merging sections is done via chat",
            ) from e
        except CommitMutexTimeout as e:
            session.rollback()
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        except GitCommitError as e:
            session.rollback()
            logger.exception("spec save failed for workspace %s section %s", workspace_id, index)
            raise HTTPException(status_code=500, detail="save failed — nothing was written") from e
        except IndexError as e:
            session.rollback()
            raise HTTPException(status_code=404, detail="no such section") from e
    return SaveResultOut.from_result(result)
```

- [ ] **Step 4: Run; verify pass**

Run: `uv run pytest tests/test_api_spec.py -v`
Expected: PASS (all GET + save tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff format src/call_manager/web/api/workspaces.py tests/test_api_spec.py
uv run ruff check src/call_manager/web/api/workspaces.py
git add src/call_manager/web/api/workspaces.py tests/test_api_spec.py
git commit -m "feat(api): POST /workspaces/{id}/spec/sections/{index} (artifact=spec)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: `SPEC_SKELETON` + idempotent `POST /spec` init

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py` (imports; add `post_init_spec`)
- Modify: `tests/test_api_spec.py` (init tests)

`SPEC_SKELETON` already lives in `artifacts.py` (Task 1). Init is a **whole-doc** commit (NOT `save_section`, to sidestep the preamble structure guard), under the commit mutex, only-if-whitespace-only, returning `SpecOut`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api_spec.py`:

```python
from call_manager.db.models import ArtifactVersion  # noqa: E402  (top-of-file is fine too)


def test_init_spec_seeds_six_sections_when_empty(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "")  # whitespace-only spec
    c = _client(app, owner_id)
    r = c.post(f"/api/workspaces/{ws_id}/spec")
    assert r.status_code == 200
    body = r.json()
    # 6 sections: empty index-0 preamble + the 5 H2 sections.
    assert [s["index"] for s in body["sections"]] == [0, 1, 2, 3, 4, 5]
    assert [s["heading"] for s in body["sections"]] == [
        "",
        "Aims",
        "Approach",
        "Novelty & Significance",
        "Expected Impact",
        "Key Claims",
    ]


def test_init_spec_is_idempotent_noop_when_nonempty(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nmy own words.\n")
    c = _client(app, owner_id)
    r = c.post(f"/api/workspaces/{ws_id}/spec")
    assert r.status_code == 200
    # Did not clobber the user's content.
    assert any("my own words." in s["text"] for s in r.json()["sections"])
    # Calling twice on an already-seeded spec adds no new init commit.
    c.post(f"/api/workspaces/{ws_id}/spec")
    with pg_session_factory() as s:
        n = (
            s.query(ArtifactVersion)
            .filter(
                ArtifactVersion.workspace_id == ws_id,
                ArtifactVersion.message == "user spec init",
            )
            .count()
        )
    assert n == 0  # non-empty -> never an init commit


def test_init_spec_seeds_exactly_one_commit(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "")
    c = _client(app, owner_id)
    c.post(f"/api/workspaces/{ws_id}/spec")
    c.post(f"/api/workspaces/{ws_id}/spec")  # second sees content -> no-op
    with pg_session_factory() as s:
        n = (
            s.query(ArtifactVersion)
            .filter(
                ArtifactVersion.workspace_id == ws_id,
                ArtifactVersion.message == "user spec init",
            )
            .count()
        )
    assert n == 1


def test_init_spec_member_forbidden(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "")
    assert _client(app, member_id).post(f"/api/workspaces/{ws_id}/spec").status_code == 403
```

(The mutex guarantees true-concurrency safety; these sequential calls cover the observable idempotency contract — the second call always sees a non-empty file.)

- [ ] **Step 2: Run; verify they fail**

Run: `uv run pytest tests/test_api_spec.py -k init_spec -v`
Expected: FAIL — 404/405 (init route absent).

- [ ] **Step 3: Add the init route + imports**

In `src/call_manager/web/api/workspaces.py`, **add only the NEW names to the EXISTING import lines** — do NOT paste fresh `from … import` lines (a duplicate would trip ruff F811). Concretely, edit four existing import lines so they read:

```python
# db.models line (currently: `Reference, ReferenceChunk, User`) — add ArtifactVersion, WorkspaceEvent:
from call_manager.db.models import ArtifactVersion, Reference, ReferenceChunk, User, WorkspaceEvent
# gitio line (currently: `GitCommitError, current_head`) — add write_and_commit_files:
from call_manager.proposal_assist.citations.gitio import GitCommitError, current_head, write_and_commit_files
# locking line (currently: `BUSY_ERROR_MESSAGE, CommitMutexTimeout`) — add acquire_commit_mutex:
from call_manager.proposal_assist.drafting.locking import BUSY_ERROR_MESSAGE, CommitMutexTimeout, acquire_commit_mutex
# artifacts line (Task 4 made it `SPEC_FILENAME, Artifact`) — add SPEC_SKELETON:
from call_manager.proposal_assist.artifacts import SPEC_FILENAME, SPEC_SKELETON, Artifact
```

(`current_head`, `GitCommitError`, `BUSY_ERROR_MESSAGE`, `CommitMutexTimeout` are already imported from Tasks 3-4 / the shipped file — only the bolded new names are additions.)

Add the route after `post_save_spec_section`:

```python
@router.post("/workspaces/{workspace_id}/spec", response_model=SpecOut)
def post_init_spec(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SpecOut:
    """Idempotent Spec initialization (owner-gated). If proposal-spec.md is
    whitespace-only, seed SPEC_SKELETON as ONE whole-doc commit (sidestepping the
    preamble structure guard); otherwise no-op. Returns the SpecOut either way."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        spec = repo_dir / SPEC_FILENAME
        if not spec.exists():
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet")
        try:
            acquire_commit_mutex(session, ws.id)
            raw = spec.read_text()  # read UNDER the mutex so a concurrent init serializes
            if raw.strip() == "":
                commit_sha = write_and_commit_files(
                    repo_dir, {SPEC_FILENAME: SPEC_SKELETON}, "user spec init"
                )
                session.add(
                    ArtifactVersion(
                        workspace_id=ws.id,
                        commit_sha=commit_sha,
                        created_by="user",
                        message="user spec init",
                    )
                )
                session.add(
                    WorkspaceEvent(
                        workspace_id=ws.id,
                        kind="user_edit",
                        actor_id=user.id,
                        payload={"artifact": "spec", "commit_sha": commit_sha},
                    )
                )
            session.commit()  # releases the mutex; persists rows iff we seeded
        except CommitMutexTimeout as e:
            session.rollback()
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        except GitCommitError as e:
            session.rollback()
            logger.exception("spec init failed for workspace %s", workspace_id)
            raise HTTPException(status_code=500, detail="spec init failed — nothing was written") from e
        head = current_head(repo_dir)
        sections = _sections_out(spec.read_text())
    return SpecOut(head=head, can_edit=True, sections=sections)
```

- [ ] **Step 4: Run; verify pass**

Run: `uv run pytest tests/test_api_spec.py -v`
Expected: PASS (GET + save + init).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff format src/call_manager/web/api/workspaces.py tests/test_api_spec.py
uv run ruff check src/call_manager/web/api/workspaces.py
git add src/call_manager/web/api/workspaces.py tests/test_api_spec.py
git commit -m "feat(api): idempotent POST /workspaces/{id}/spec skeleton init

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Regenerate the OpenAPI schema for the `/spec` routes

**Files:**
- Modify: `frontend/src/api/schema.gen.ts` (generated)

The generator reads the live FastAPI schema; the `EMBEDDING_MODEL=bge-m3` prefix avoids the mxbai WARNING-to-stdout that corrupts the output (the `.env` default model would break the codegen).

- [ ] **Step 1: Regenerate**

From `frontend/`:

```bash
EMBEDDING_MODEL=bge-m3 pnpm gen:api
```

- [ ] **Step 2: Verify the routes + model landed**

Run (from `frontend/`) — each path key is emitted exactly once as an object key (same style as the existing `"/api/workspaces/{workspace_id}/draft": {`):

```bash
grep -c '"/api/workspaces/{workspace_id}/spec":' src/api/schema.gen.ts
grep -c '"/api/workspaces/{workspace_id}/spec/sections/{index}":' src/api/schema.gen.ts
grep -c "SpecOut" src/api/schema.gen.ts
```

Expected: `1`, `1`, and `≥ 1` respectively. (`GET`+`POST /spec` share one path-key object; the sections route is its own key.)

- [ ] **Step 3: Typecheck (no drift)**

From `frontend/`:

```bash
pnpm exec tsc --noEmit
```

Expected: PASS (the generated types compile; nothing references them yet, so this just proves the regen is well-formed).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/schema.gen.ts
git commit -m "chore(frontend): regen OpenAPI types for /spec routes

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: `CodeMirrorEditor` `citations` prop

**Files:**
- Modify: `frontend/src/editor/CodeMirrorEditor.tsx:34-204`
- Modify: `frontend/src/editor/CodeMirrorEditor.test.tsx` (add a citation-gating test)

Today `citationPills(...)` is mounted unconditionally (`CodeMirrorEditor.tsx:123`). Add `citations?: boolean` (default `true`, preserving every current mount). When `false`, omit `citationPills` AND skip the selection bridge — the Spec mounts `citations={false}` for raw editable text. `citations` is read once at mount (it is stable per editor instance — draft and spec are distinct mounts).

- [ ] **Step 1: Write the failing test**

Add to `frontend/src/editor/CodeMirrorEditor.test.tsx` (match the file's existing imports/helpers — it already renders `<CodeMirrorEditor>` and reaches the view via the `.cm-editor` host). A representative test:

```tsx
it("citations={false} renders marker text raw with no citation pill", async () => {
  const markerDoc = "## Aims\nSee [@smith2021]<!-- ev:ev-abc123 --> here.\n";
  const { container } = render(
    <CodeMirrorEditor
      value={markerDoc}
      onChange={() => {}}
      requireHeading={true}
      citations={false}
    />,
  );
  await waitFor(() => expect(container.querySelector(".cm-editor")).toBeTruthy());
  // No atomic citation widget was built.
  expect(container.querySelector(".cm-citation-pill")).toBeNull();
  // The raw marker characters are present in the document text.
  expect(container.querySelector(".cm-content")?.textContent).toContain("[@smith2021]");
});

it("citations default (true) still renders the citation pill", async () => {
  const markerDoc = "## Aims\nSee [@smith2021]<!-- ev:ev-abc123 --> here.\n";
  const { container } = render(
    <CodeMirrorEditor value={markerDoc} onChange={() => {}} requireHeading={true} />,
  );
  await waitFor(() => expect(container.querySelector(".cm-citation-pill")).toBeTruthy());
});
```

The existing test file imports only `import { render, cleanup } from "@testing-library/react";` (line 1) — **add `waitFor`** so it becomes `import { render, cleanup, waitFor } from "@testing-library/react";`. Both new tests use `render`'s returned `container` and `waitFor`.

- [ ] **Step 2: Run; verify the first test fails**

Run (from `frontend/`): `pnpm vitest run src/editor/CodeMirrorEditor.test.tsx`
Expected: the `citations={false}` test FAILS — a `.cm-citation-pill` is rendered because pills mount unconditionally.

- [ ] **Step 3: Add the prop and gate the extensions + bridge**

In `frontend/src/editor/CodeMirrorEditor.tsx`, **replace the `CodeMirrorEditorProps` type (lines 34-44) in full** with (adds the `citations` field):

```tsx
export type CodeMirrorEditorProps = {
  value: string;
  onChange: (next: string) => void;
  /** true for a non-preamble section: the leading "## " heading may not be deleted. */
  requireHeading: boolean;
  readOnly?: boolean;
  /** Wired FOR SP2a-4 select-to-cite; defaults to a no-op. */
  onPillClick?: (citekey: string, evId: string) => void;
  /** SP2a-4b: the current non-empty editor selection (or null when collapsed). */
  onSelectionChange?: (sel: { from: number; to: number; text: string } | null) => void;
  /** Citation layer (pills + select-to-cite bridge). Default true preserves the
   *  draft editor; the Spec passes false for raw, citation-free text. Read once
   *  at mount — stable per editor instance. */
  citations?: boolean;
};
```

Destructure it (line 74-81) with the default:

```tsx
export function CodeMirrorEditor({
  value,
  onChange,
  requireHeading,
  readOnly = false,
  onPillClick = () => {},
  onSelectionChange = () => {},
  citations = true,
}: CodeMirrorEditorProps) {
```

Build the extensions array conditionally. **Replace the ENTIRE `const view = new EditorView({ … })` statement (lines 116-154 — from `const view = new EditorView({` through its closing `});`)** with the block below. The provided block hoists the extensions into a `const extensions` array (so `citationPills` is included only when `citations`, and the selection-bridge block is gated) and then re-creates `const view`. Do NOT replace only the inner `extensions: [ … ]` array — that would leave a dangling/duplicate `new EditorView(...)` and break syntax. The `viewRef.current = view;`, the test-only `__cmView` exposure, the cleanup `return () => {...}`, and the separate value/readOnly effects below all stay UNCHANGED:

```tsx
    const extensions = [
      history(),
      keymap.of([...defaultKeymap, ...historyKeymap]),
      markdown(),
    ];
    if (citations) {
      extensions.push(
        citationPills((citekey, evId) => onPillClickRef.current?.(citekey, evId)),
      );
    }
    extensions.push(
      guard,
      cmTheme,
      EditorView.lineWrapping,
      readOnlyComp.current.of([
        EditorState.readOnly.of(readOnly),
        EditorView.editable.of(!readOnly),
      ]),
      EditorView.updateListener.of((u) => {
        const localDocChange =
          u.docChanged && u.transactions.some((t) => !t.annotation(Transaction.remote));
        if (localDocChange) {
          onChangeRef.current(u.state.doc.toString());
        }
        // Selection bridge: only when the citation layer is on (select-to-cite is
        // draft-only). Skip Transaction.remote echoes so a controlled value push
        // never rewrites the held selection.
        if (citations) {
          const remoteEcho = u.docChanged && !localDocChange;
          if ((u.selectionSet || u.docChanged) && !remoteEcho) {
            const sel = u.state.selection.main;
            const text = u.state.sliceDoc(sel.from, sel.to);
            if (text && !u.state.facet(EditorState.readOnly)) {
              onSelectionChangeRef.current?.({ from: sel.from, to: sel.to, text });
            } else {
              onSelectionChangeRef.current?.(null);
            }
          }
        }
      }),
    );
    const view = new EditorView({
      state: EditorState.create({ doc: value, extensions }),
      parent: hostRef.current,
    });
```

(The mount effect keeps its `[]` deps; `citations` is captured at mount. Document via the prop comment that it is stable per instance.)

- [ ] **Step 4: Run; verify pass**

Run (from `frontend/`): `pnpm vitest run src/editor/CodeMirrorEditor.test.tsx`
Expected: PASS (both new tests + every existing editor test — the default path is unchanged).

- [ ] **Step 5: Lint + commit**

```bash
# from frontend/
pnpm lint
git add frontend/src/editor/CodeMirrorEditor.tsx frontend/src/editor/CodeMirrorEditor.test.tsx
git commit -m "feat(editor): CodeMirrorEditor citations prop gates pills + selection bridge

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Extract `<ArtifactSectionEditor>` (identity-based lookup); rewire `Cockpit.tsx`

**Files:**
- Create: `frontend/src/editor/ArtifactSectionEditor.tsx`
- Create: `frontend/src/editor/ArtifactSectionEditor.test.tsx`
- Modify: `frontend/src/routes/Cockpit.tsx` (thin wrapper)

The current `Cockpit.tsx` body becomes `<ArtifactSectionEditor>`, parameterized by `(workspaceId, data, save, queryKey, refetch, citations)`. **Behavior-preserving for the draft** (`Cockpit.test.tsx` stays green). The active section/buffer is resolved by `section.index` (a `byIndex` Map), not array position. Citation UI (Cite button, composer, peek, pinned) is gated on `citations`; the `useCitationSource` hooks run unconditionally but are inert when `peekEv`/`pinnedEv` are `null`.

- [ ] **Step 1: Write the failing identity-lookup test**

Create `frontend/src/editor/ArtifactSectionEditor.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { ArtifactSectionEditor } from "./ArtifactSectionEditor";

afterEach(() => vi.restoreAllMocks());

function renderEditor(sections: { index: number; heading: string; text: string; sha: string }[]) {
  const qc = new QueryClient();
  const save = { mutateAsync: vi.fn(), isPending: false };
  const { container } = render(
    <QueryClientProvider client={qc}>
      <ArtifactSectionEditor
        workspaceId="ws-1"
        queryKey={["spec", "ws-1"]}
        head={"a".repeat(40)}
        canEdit={true}
        sections={sections}
        save={save}
        refetch={() => {}}
        citations={false}
      />
    </QueryClientProvider>,
  );
  return { save, container };
}

it("resolves the active section by section.index, not array position", async () => {
  // Array order is REVERSED relative to section.index. Positional lookup would
  // open the wrong section; identity lookup opens the right one.
  const sections = [
    { index: 1, heading: "Approach", text: "## Approach\nB.\n", sha: "s1" },
    { index: 0, heading: "", text: "preamble.\n", sha: "s0" },
  ];
  const { container } = renderEditor(sections);
  // Outline lists by section.index; click the "Approach" (index 1) item.
  fireEvent.click(screen.getByRole("button", { name: /Approach/ }));
  // The editor's .cm-content shows section 1's text, though it is array element 0.
  // Assert via .cm-content textContent (the codebase idiom): the markdown() extension
  // splits a heading into sibling spans, so getByText(/## Approach/) would not match.
  await waitFor(() =>
    expect(container.querySelector(".cm-content")?.textContent).toContain("## Approach"),
  );
});

it("citations={false} renders no Cite control", () => {
  renderEditor([{ index: 0, heading: "", text: "x\n", sha: "s0" }]);
  expect(screen.queryByRole("button", { name: /cite selection/i })).toBeNull();
});
```

- [ ] **Step 2: Run; verify it fails**

Run (from `frontend/`): `pnpm vitest run src/editor/ArtifactSectionEditor.test.tsx`
Expected: FAIL — module `./ArtifactSectionEditor` does not exist.

- [ ] **Step 3: Create `ArtifactSectionEditor.tsx`**

This is the current `Cockpit.tsx` body with three parameterizations: (a) `queryKey` replaces the literal `["draft", id]`; (b) `byIndex` Map replaces positional `sections[...]`; (c) all citation UI is gated on `citations`.

```tsx
import { useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { useCitationSource } from "../api/citations";
import { ApiError } from "../api/client";
import { CitationComposer } from "../cockpit/CitationComposer";
import { SourceViewer } from "../cockpit/SourceViewer";
import { CodeMirrorEditor } from "./CodeMirrorEditor";

type Section = { index: number; heading: string; text: string; sha: string };

type SaveVars = {
  index: number;
  content: string;
  base_commit_sha: string;
  base_section_sha: string;
};
type SaveResultLike = { head: string; section_sha: string };

type Tab = { kind: "section"; index: number } | { kind: "full" };

export type ArtifactSectionEditorProps = {
  workspaceId: string;
  /** TanStack cache key for this artifact, e.g. ["draft", id] | ["spec", id]. */
  queryKey: [string, string];
  head: string;
  canEdit: boolean;
  sections: Section[];
  save: { mutateAsync: (v: SaveVars) => Promise<SaveResultLike>; isPending: boolean };
  refetch: () => void | Promise<unknown>;
  /** Draft passes true (pills + select-to-cite). Spec passes false. */
  citations?: boolean;
  /** Label for the whole-artifact read-only View tab. Default keeps the draft
   *  test's `/full draft/i` query green; the Spec passes "Full spec". */
  fullViewLabel?: string;
};

function saveErrorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    const prefix = `${e.code}: `;
    return e.message.startsWith(prefix) ? e.message.slice(prefix.length) : e.message;
  }
  return "Save failed — please try again.";
}

export function ArtifactSectionEditor({
  workspaceId,
  queryKey,
  head,
  canEdit,
  sections,
  save,
  refetch,
  citations = false,
  fullViewLabel = "Full draft",
}: ArtifactSectionEditorProps) {
  const qc = useQueryClient();
  const [buffers, setBuffers] = useState<Record<number, string>>({});
  const [lastSectionIndex, setLastSectionIndex] = useState(0);
  const [tab, setTab] = useState<Tab>({ kind: "section", index: 0 });
  const [staleSection, setStaleSection] = useState<number | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [peekEv, setPeekEv] = useState<string | null>(null);
  const [pinnedEv, setPinnedEv] = useState<string | null>(null);
  const [selection, setSelection] = useState<{ from: number; to: number; text: string } | null>(
    null,
  );
  const [composerOpen, setComposerOpen] = useState(false);
  // Inert when peekEv/pinnedEv are null (the hook disables a null id). Always
  // called to keep hook order stable; only ever non-null when citations is on.
  const peek = useCitationSource(workspaceId, peekEv);
  const pinned = useCitationSource(workspaceId, pinnedEv);

  // Identity-based lookup: resolve a section by its stable `index`, never by
  // array position (which is only equal by coincidence today).
  const byIndex = useMemo(() => new Map(sections.map((s) => [s.index, s])), [sections]);

  const fullText = sections.map((s) => s.text).join("");
  const activeIndex = tab.kind === "section" ? tab.index : -1;
  const activeSection = activeIndex >= 0 ? byIndex.get(activeIndex) : undefined;
  const editorValue =
    tab.kind === "full" ? fullText : (buffers[activeIndex] ?? activeSection?.text ?? "");

  const isDirty = (idx: number) =>
    buffers[idx] !== undefined && buffers[idx] !== byIndex.get(idx)?.text;

  function selectSection(index: number) {
    setTab({ kind: "section", index });
    setLastSectionIndex(index);
    setStaleSection(null);
    setSaveError(null);
    setSelection(null);
    setComposerOpen(false);
  }

  function onEditorChange(next: string) {
    if (activeIndex < 0) return;
    setSaveError(null);
    setBuffers((b) => ({ ...b, [activeIndex]: next }));
  }

  function reloadSection(index: number) {
    setBuffers((b) => {
      const next = { ...b };
      delete next[index];
      return next;
    });
    setStaleSection(null);
    void refetch();
  }

  async function onSave() {
    if (!activeSection) return;
    const index = activeSection.index;
    const content = buffers[index] ?? activeSection.text;
    setSaveError(null);
    try {
      const result = await save.mutateAsync({
        index,
        content,
        base_commit_sha: head,
        base_section_sha: activeSection.sha,
      });
      qc.setQueryData(queryKey, (prev: { head: string; sections: Section[] } | undefined) => {
        if (!prev) return prev;
        return {
          ...prev,
          head: result.head,
          sections: prev.sections.map((s) =>
            s.index === index ? { ...s, text: content, sha: result.section_sha } : s,
          ),
        };
      });
      setBuffers((b) => {
        const next = { ...b };
        delete next[index];
        return next;
      });
      setStaleSection(null);
    } catch (e) {
      if (e instanceof ApiError && e.code === "conflict") {
        setStaleSection(index);
        return;
      }
      setStaleSection(null);
      setSaveError(saveErrorMessage(e));
    }
  }

  const onPillClick = (_citekey: string, evId: string) => setPeekEv(evId);

  return (
    <main className="cockpit">
      <nav aria-label="Outline">
        <ul>
          {sections.map((s) => (
            <li key={s.index}>
              <button
                type="button"
                aria-current={tab.kind === "section" && tab.index === s.index}
                onClick={() => selectSection(s.index)}
              >
                {(s.heading || "preamble") + (isDirty(s.index) ? " •" : "")}
              </button>
            </li>
          ))}
        </ul>
      </nav>

      <section className="cockpit-main">
        <div role="tablist" aria-label="View">
          <button
            type="button"
            role="tab"
            aria-selected={tab.kind === "section"}
            onClick={() => selectSection(lastSectionIndex)}
          >
            Section
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab.kind === "full"}
            onClick={() => setTab({ kind: "full" })}
          >
            {fullViewLabel}
          </button>
        </div>

        {staleSection !== null && (
          <p role="alert">
            This section changed underneath you. Your edits are kept.{" "}
            <button type="button" onClick={() => reloadSection(staleSection)}>
              Reload (discard my edits)
            </button>{" "}
            <button type="button" onClick={() => setStaleSection(null)}>
              Keep editing
            </button>
          </p>
        )}

        {saveError !== null && (
          <p role="alert">
            {saveError}{" "}
            <button type="button" onClick={() => setSaveError(null)}>
              Dismiss
            </button>
          </p>
        )}

        <CodeMirrorEditor
          value={editorValue}
          onChange={tab.kind === "full" ? () => {} : onEditorChange}
          requireHeading={tab.kind === "section" && activeIndex > 0}
          readOnly={tab.kind === "full" || !canEdit}
          citations={citations}
          onPillClick={citations ? onPillClick : undefined}
          onSelectionChange={citations && tab.kind === "section" ? setSelection : undefined}
        />

        {canEdit && tab.kind === "section" && (
          <div className="cockpit-section-actions">
            <button
              type="button"
              onClick={onSave}
              disabled={save.isPending || !isDirty(activeIndex)}
            >
              Save section
            </button>
            {citations && (
              <button
                type="button"
                className="cite-selection-btn"
                disabled={!selection || isDirty(activeIndex)}
                title={isDirty(activeIndex) ? "Save this section first" : undefined}
                onClick={() => setComposerOpen(true)}
              >
                Cite selection
              </button>
            )}
          </div>
        )}
      </section>

      {citations && peekEv !== null && (
        <section className="cockpit-peek" aria-label="Citation source peek">
          {peek.isLoading && <p aria-busy="true">Loading source…</p>}
          {peek.isError && <p role="alert">Could not load the cited source.</p>}
          {peek.data && (
            <SourceViewer
              source={peek.data}
              pinned={false}
              onClose={() => setPeekEv(null)}
              onPin={() => {
                setPinnedEv(peekEv);
                setPeekEv(null);
              }}
              onNavigate={(evId) => setPeekEv(evId)}
            />
          )}
        </section>
      )}

      {citations && pinnedEv !== null && (
        <aside className="cockpit-pinned" aria-label="Pinned source">
          {pinned.isLoading && <p aria-busy="true">Loading source…</p>}
          {pinned.isError && <p role="alert">Could not load the cited source.</p>}
          {pinned.data && (
            <SourceViewer
              source={pinned.data}
              pinned
              onClose={() => setPinnedEv(null)}
              onPin={() => {}}
              onNavigate={(evId) => setPinnedEv(evId)}
            />
          )}
        </aside>
      )}

      {citations && composerOpen && selection && activeSection && (
        <section className="cockpit-composer">
          <CitationComposer
            workspaceId={workspaceId}
            claim={{
              section_index: activeSection.index,
              base_commit_sha: head,
              base_section_sha: activeSection.sha,
              anchor: selection.text,
            }}
            onDone={() => {
              setComposerOpen(false);
              setSelection(null);
              void refetch();
            }}
            onCancel={() => setComposerOpen(false)}
          />
        </section>
      )}
    </main>
  );
}
```

- [ ] **Step 4: Run the new test; verify pass**

Run (from `frontend/`): `pnpm vitest run src/editor/ArtifactSectionEditor.test.tsx`
Expected: PASS.

- [ ] **Step 5: Rewire `Cockpit.tsx` as a thin wrapper**

Replace the entire body of `frontend/src/routes/Cockpit.tsx` with:

```tsx
import { useParams } from "react-router-dom";

import { useDraft, useSaveSection } from "../api/editor";
import { ArtifactSectionEditor } from "../editor/ArtifactSectionEditor";

export function Cockpit() {
  const { id = "" } = useParams();
  const draft = useDraft(id);
  const save = useSaveSection(id);

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
    <ArtifactSectionEditor
      workspaceId={id}
      queryKey={["draft", id]}
      head={head}
      canEdit={can_edit}
      sections={sections}
      save={save}
      refetch={draft.refetch}
      citations
    />
  );
}
```

- [ ] **Step 6: Run the draft regression suite; verify green**

Run (from `frontend/`): `pnpm vitest run src/routes/Cockpit.test.tsx src/editor/ArtifactSectionEditor.test.tsx`
Expected: PASS — `Cockpit.test.tsx` is the load-bearing regression guard; the rendered draft behavior is unchanged. The draft keeps the `fullViewLabel` default `"Full draft"`, so `Cockpit.test.tsx`'s `getByRole("tab", { name: /full draft/i })` and `/^section$/i` queries need NO edits.

- [ ] **Step 7: Lint + commit**

```bash
# from frontend/
pnpm lint
git add frontend/src/editor/ArtifactSectionEditor.tsx frontend/src/editor/ArtifactSectionEditor.test.tsx frontend/src/routes/Cockpit.tsx
git commit -m "refactor(editor): extract ArtifactSectionEditor with identity-based section lookup

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: Spec API hooks + in-cockpit Spec tab + empty-state CTA

**Files:**
- Modify: `frontend/src/api/editor.ts` (add `useSpec` / `useSaveSpecSection` / `useInitSpec`)
- Modify: `frontend/src/api/editor.test.tsx` (hook tests)
- Create: `frontend/src/cockpit/DraftPanel.tsx` (the draft wrapper extracted from Task 8's thin Cockpit)
- Create: `frontend/src/cockpit/SpecPanel.tsx`
- Create: `frontend/src/cockpit/SpecPanel.test.tsx`
- Modify: `frontend/src/routes/Cockpit.tsx` (becomes the `[Draft | Spec]` artifact-tab shell)

The Spec is an **in-cockpit artifact tab** (the cockpit design's "center View tab"), NOT a separate URL route — so no `router.tsx` / `routes.json` (`spa_routes`) manifest change, and it is reached by in-app navigation. The new `Draft`/`Spec` tabs are `role="tab"` but do not collide with `Cockpit.test.tsx`'s anchored queries (`/^section$/i`, `/full draft/i`).

- [ ] **Step 1: Write the failing hook tests**

Add to `frontend/src/api/editor.test.tsx` (reuse the file's `wrapper` + `clientMod` spy pattern):

```tsx
it("useSpec returns the unwrapped spec (no citations field)", async () => {
  const SPEC = {
    head: "a".repeat(40),
    can_edit: true,
    sections: [{ index: 0, heading: "", text: "", sha: "x" }],
  };
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: SPEC, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useSpec("ws-1"), { wrapper: wrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data).toEqual(SPEC);
});

it("useInitSpec invalidates ['spec', id] on success", async () => {
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: { head: "b".repeat(40), can_edit: true, sections: [] },
    error: undefined,
  } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useInitSpec("ws-1"), { wrapper: wrapper(qc) });
  await result.current.mutateAsync();
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["spec", "ws-1"] });
});
```

Import the new hooks at the top of the test file: `import { useDraft, useSaveSection, useSpec, useInitSpec } from "./editor";`

- [ ] **Step 2: Run; verify fail**

Run (from `frontend/`): `pnpm vitest run src/api/editor.test.tsx`
Expected: FAIL — `useSpec`/`useInitSpec` are not exported.

- [ ] **Step 3: Add the hooks**

Append to `frontend/src/api/editor.ts`:

```ts
export type SaveSpecSectionVars = {
  index: number;
  content: string;
  base_commit_sha: string;
  base_section_sha: string;
};

/** GET /api/workspaces/{id}/spec — outline + section bytes, citation-free. */
export function useSpec(id: string) {
  return useQuery({
    queryKey: ["spec", id],
    queryFn: async () =>
      unwrap(await api.GET("/api/workspaces/{workspace_id}/spec", { params: { path: { workspace_id: id } } })),
  });
}

/** POST /api/workspaces/{id}/spec/sections/{index}. Invalidates ['spec', id]. */
export function useSaveSpecSection(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (vars: SaveSpecSectionVars) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/spec/sections/{index}", {
          params: { path: { workspace_id: id, index: vars.index } },
          body: {
            content: vars.content,
            base_commit_sha: vars.base_commit_sha,
            base_section_sha: vars.base_section_sha,
          },
        }),
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["spec", id] }),
  });
}

/** POST /api/workspaces/{id}/spec — idempotent skeleton init. Invalidates ['spec', id]. */
export function useInitSpec(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(await api.POST("/api/workspaces/{workspace_id}/spec", { params: { path: { workspace_id: id } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["spec", id] }),
  });
}
```

- [ ] **Step 4: Run hook tests; verify pass**

Run (from `frontend/`): `pnpm vitest run src/api/editor.test.tsx`
Expected: PASS.

- [ ] **Step 5: Write the failing `SpecPanel` test**

Create `frontend/src/cockpit/SpecPanel.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { SpecPanel } from "./SpecPanel";

afterEach(() => vi.restoreAllMocks());

function renderPanel() {
  const qc = new QueryClient();
  render(
    <QueryClientProvider client={qc}>
      <SpecPanel id="ws-1" />
    </QueryClientProvider>,
  );
}

const EMPTY = {
  data: { head: "a".repeat(40), can_edit: true, sections: [{ index: 0, heading: "", text: "", sha: "x" }] },
  error: undefined,
};
const SEEDED = {
  data: {
    head: "b".repeat(40),
    can_edit: true,
    sections: [
      { index: 0, heading: "", text: "", sha: "p" },
      { index: 1, heading: "Aims", text: "## Aims\n\n", sha: "a" },
    ],
  },
  error: undefined,
};

it("empty spec shows the skeleton CTA, then init reveals sections", async () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue(EMPTY as never);
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue(SEEDED as never);
  renderPanel();
  const cta = await screen.findByRole("button", { name: /Start spec from skeleton/i });
  // After init's onSuccess invalidates ['spec', id], the refetch returns the seeded spec.
  get.mockResolvedValue(SEEDED as never);
  fireEvent.click(cta);
  await waitFor(() => expect(post).toHaveBeenCalled());
  await screen.findByText(/Aims/);
});

it("a non-empty spec renders the editor with a 'Full spec' tab", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(SEEDED as never);
  renderPanel();
  await screen.findByRole("tab", { name: /full spec/i });
});
```

- [ ] **Step 6: Run; verify fail**

Run (from `frontend/`): `pnpm vitest run src/cockpit/SpecPanel.test.tsx`
Expected: FAIL — `./SpecPanel` does not exist.

- [ ] **Step 7: Create `DraftPanel.tsx` and `SpecPanel.tsx`**

`DraftPanel.tsx` is exactly the body of Task 8's thin `Cockpit.tsx`, moved into its own component (so `Cockpit` can host both panels). Create `frontend/src/cockpit/DraftPanel.tsx`:

```tsx
import { useDraft, useSaveSection } from "../api/editor";
import { ArtifactSectionEditor } from "../editor/ArtifactSectionEditor";

export function DraftPanel({ id }: { id: string }) {
  const draft = useDraft(id);
  const save = useSaveSection(id);
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
    <ArtifactSectionEditor
      workspaceId={id}
      queryKey={["draft", id]}
      head={head}
      canEdit={can_edit}
      sections={sections}
      save={save}
      refetch={draft.refetch}
      citations
    />
  );
}
```

Create `frontend/src/cockpit/SpecPanel.tsx`. A whitespace-only spec (every section's text is whitespace — a fresh spec is one empty preamble section) shows the CTA; otherwise the `<ArtifactSectionEditor citations={false}>` with the `Full spec` view label:

```tsx
import { useInitSpec, useSaveSpecSection, useSpec } from "../api/editor";
import { ArtifactSectionEditor } from "../editor/ArtifactSectionEditor";

export function SpecPanel({ id }: { id: string }) {
  const spec = useSpec(id);
  const save = useSaveSpecSection(id);
  const init = useInitSpec(id);

  if (spec.isLoading) return <main aria-busy="true">Loading spec…</main>;
  if (spec.isError || !spec.data) {
    return (
      <main role="alert">
        <h1>Could not load the spec</h1>
      </main>
    );
  }
  const { head, can_edit, sections } = spec.data;
  // Explicit skeleton CTA — no silent commit-on-open. Same whitespace-only
  // predicate the init endpoint uses, so the CTA and the server seed agree.
  const isEmpty = sections.every((s) => s.text.trim() === "");
  if (isEmpty) {
    return (
      <main className="spec-empty">
        <h1>Spec</h1>
        <p>This proposal has no spec yet.</p>
        <button type="button" disabled={!can_edit || init.isPending} onClick={() => init.mutate()}>
          Start spec from skeleton
        </button>
      </main>
    );
  }
  return (
    <ArtifactSectionEditor
      workspaceId={id}
      queryKey={["spec", id]}
      head={head}
      canEdit={can_edit}
      sections={sections}
      save={save}
      refetch={spec.refetch}
      citations={false}
      fullViewLabel="Full spec"
    />
  );
}
```

- [ ] **Step 8: Rewrite `Cockpit.tsx` as the `[Draft | Spec]` artifact-tab shell**

Replace the entire body of `frontend/src/routes/Cockpit.tsx` (the thin draft wrapper from Task 8) with:

```tsx
import { useState } from "react";
import { useParams } from "react-router-dom";

import { DraftPanel } from "../cockpit/DraftPanel";
import { SpecPanel } from "../cockpit/SpecPanel";

export function Cockpit() {
  const { id = "" } = useParams();
  // Artifact-level tab: the Spec is a "center View tab" of the cockpit, not a
  // separate URL. Default to Draft so the existing draft UX (and Cockpit.test.tsx)
  // is unchanged on open.
  const [artifact, setArtifact] = useState<"draft" | "spec">("draft");
  return (
    <div className="cockpit-shell">
      <div role="tablist" aria-label="Artifact">
        <button
          type="button"
          role="tab"
          aria-selected={artifact === "draft"}
          onClick={() => setArtifact("draft")}
        >
          Draft
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={artifact === "spec"}
          onClick={() => setArtifact("spec")}
        >
          Spec
        </button>
      </div>
      {artifact === "draft" ? <DraftPanel id={id} /> : <SpecPanel id={id} />}
    </div>
  );
}
```

- [ ] **Step 9: Run; verify pass (incl. the draft regression guard)**

Run (from `frontend/`): `pnpm vitest run src/cockpit/SpecPanel.test.tsx src/api/editor.test.tsx src/routes/Cockpit.test.tsx`
Expected: PASS. `Cockpit.test.tsx` stays green: the default tab is Draft, so `<DraftPanel>` renders the same UI; the new `Draft`/`Spec` tabs don't match the anchored `/^section$/i` or `/full draft/i` queries, and no test does an unnamed `getByRole("tab")` / `getByRole("tablist")`. (If a test asserted the literal root element type, point it at `main.cockpit`, which `DraftPanel` still renders — no such assertion is expected.)

- [ ] **Step 10: Lint + commit**

```bash
# from frontend/
pnpm lint
git add frontend/src/api/editor.ts frontend/src/api/editor.test.tsx frontend/src/cockpit/DraftPanel.tsx frontend/src/cockpit/SpecPanel.tsx frontend/src/cockpit/SpecPanel.test.tsx frontend/src/routes/Cockpit.tsx
git commit -m "feat(spec): in-cockpit Spec tab + DraftPanel/SpecPanel + skeleton CTA

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 10: Full verification

**Files:** none (verification only).

- [ ] **Step 1: Backend — the touched suites**

Run:

```bash
uv run pytest tests/proposal_assist/test_artifacts.py tests/proposal_assist/test_save_section_artifact.py tests/proposal_assist/test_save_section_no_freeze.py tests/test_editing_save.py tests/test_api_workspaces.py tests/test_api_spec.py -v
```

Expected: all PASS. (Any connection-refused failure is testcontainer flakiness — re-run the specific file in isolation before treating it as real.)

- [ ] **Step 2: Backend — ruff clean**

```bash
uv run ruff check src/call_manager/proposal_assist/artifacts.py src/call_manager/proposal_assist/editing/save.py src/call_manager/proposal_assist/tools/proposal.py src/call_manager/web/api/workspaces.py
uv run ruff format --check src/call_manager/proposal_assist/artifacts.py src/call_manager/proposal_assist/editing/save.py src/call_manager/proposal_assist/tools/proposal.py src/call_manager/web/api/workspaces.py
```

Expected: no findings.

- [ ] **Step 3: Frontend — full vitest + lint + typecheck**

From `frontend/`:

```bash
pnpm test
pnpm lint
pnpm exec tsc --noEmit
```

Expected: all PASS — especially `Cockpit.test.tsx` (draft regression guard), `CodeMirrorEditor.test.tsx`, `ArtifactSectionEditor.test.tsx`, `SpecPanel.test.tsx`, `editor.test.tsx`.

- [ ] **Step 4: Schema no-drift re-check**

From `frontend/`:

```bash
EMBEDDING_MODEL=bge-m3 pnpm gen:api
git diff --exit-code src/api/schema.gen.ts
```

Expected: exit 0 (the committed schema already matches the live routes — no drift).

- [ ] **Step 5: Final commit (only if Step 4 produced changes)**

If `git diff` showed drift (it should not), commit the regenerated schema:

```bash
git add frontend/src/api/schema.gen.ts
git commit -m "chore(frontend): schema no-drift re-sync

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

Then proceed to **superpowers:finishing-a-development-branch** to merge `cockpit-sp2b-1`.

---

## Out of scope (deferred — do NOT build here)

Per spec §11: the lifecycle migration + spec-lock (SP2b-2); agent `propose_*`/`accept` for `target=spec` (`accepting.py:75` still guards `target_artifact != "draft"`); `tools/history.py` artifact parameterization; citations in the Spec; `proposal-plan.md` retirement; the coherence-keeper, evaluator sessions, Scorecard/Readiness (SP2b-2/-3); a per-artifact Versions UI.
