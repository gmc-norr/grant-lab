# Cockpit SP2a-4b — Select-to-Cite (Write Path) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a workspace owner cite a source directly from the cockpit editor — select the claim span, pick a reference, select the supporting span, commit — inserting one `[@key]<!-- ev:id -->` marker as a direct git commit.

**Architecture:** A new **synchronous** core `insert_citation_for_user` (mirroring the proven `insert_from_annotation`, reusing the shared marker/ledger/commit helpers) behind a **sync** owner-gated endpoint `POST /api/workspaces/{id}/citations`; two member-gated JSON reads (`references` list, `references/{id}/chunks`) feed the picker. Concurrency rests on the per-workspace commit mutex + `save_section`'s section-staleness predicate (NOT the deleted `_turn_in_flight` freeze). The frontend adds a CodeMirror selection bridge, a citation composer, and TanStack-Query hooks.

**Tech Stack:** Python 3.12 · FastAPI · SQLAlchemy 2.x · pytest + testcontainers Postgres · git · React 18 · TanStack Query 5 · CodeMirror 6 · openapi-fetch/openapi-typescript · Vitest 2 + jsdom · pnpm.

**Spec:** `docs/superpowers/specs/2026-06-18-cockpit-sp2a-4b-select-to-cite-design.md` (v2.1, Codex-reviewed). **Branch/worktree:** all work in `/home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b` (branch `cockpit-sp2a-4b`, off master `12b0a6b`). Every subagent MUST `cd` into that worktree and confirm `git branch --show-current` prints `cockpit-sp2a-4b` before any git op; `git add` only the files each commit step names.

---

## Context the engineer needs (read first)

**The human-cite is a direct commit** — only AI citations are proposals. We reuse the *low-level* helpers that the agent path and the annotation path already share, NOT the agent's async `insert_citation_impl` wrapper:
- `markers.insert_marker(doc, anchor, citekey, ev_id)` → inserts the marker immediately **after** the anchor substring; raises `AnchorError` if the anchor occurs 0 or >1 times in the whole proposal (overlaps with existing markers ignored).
- `ledger.build_evidence_row(**kwargs)` (single authoritative v2 constructor), `ledger.append_evidence`, `ledger.load_ledger`, `ledger.dump_ledger`, `ledger.new_ev_id`.
- `gitio.write_and_commit_files(repo, {name: text}, message)` → atomic single commit; `gitio.current_head(repo)`.
- `drafting.locking.acquire_commit_mutex(session, workspace_id)` → raises `CommitMutexTimeout` (the busy signal).
- `tools.citations._resolve_citekey_for_ref(session, ledger=, ref=, workspace_id=)` and `tools.citations._citation_snapshot(csl_json, citekey)` (reused so human + agent citekeys/snapshots match — house precedent: `analysis/insert.py` imports these privates the same way).
- `editing.sectionizer.split_sections(doc)` → `list[Section]` where `Section` has `.index/.heading/.text/.sha`.
- `quotes.normalize_for_quote_match(s)` (for the claim-context hash only — the quote itself is verified by exact slice).

**Staleness predicate (mirror `save_section`, `editing/save.py:180`):**
```python
if head != base_commit_sha and live.sha != base_section_sha:
    raise <stale>   # HEAD moving is fine IF this section's bytes are unchanged
```

**`/api` conventions** (`web/api/workspaces.py`, `web/api/__init__.py`): router is `APIRouter()` under prefix `/api`. Handlers `raise HTTPException`; the global handler renders `{error:{code,message}}`. Status→code: 403→`forbidden`, 404→`not_found`, 409→`conflict`, 422→`validation_error`, 429→`too_many_requests`, 400→`bad_request`. Reads gate with `require_member`, owner-only writes with `require_owner` (both `proposal_assist/web/_perms.py`). Sessions via `request.app.state.session_factory`, settings via `request.app.state.settings`. The commit-mutex-busy → 429 + `Retry-After` mapping is already used by the save route.

**Test infra** (shell state does NOT persist between Bash calls — make each command self-contained):
- Backend one file: `cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b && uv run pytest <path> -q` (testcontainers Postgres; re-run a `connection refused` once in isolation — it's infra flakiness).
- Frontend: `export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b/frontend && nvm use >/dev/null && pnpm exec vitest run <path>` (run `pnpm install` once first — fresh worktree).
- Schema regen: `cd …/frontend && EMBEDDING_MODEL=bge-m3 pnpm gen:api` (the `.env` mxbai model logs a stdout WARNING that corrupts codegen — memory `project_openapi_codegen_stdout_gotcha`).

**Existing test helpers to reuse** (`tests/test_api_workspaces.py`): `_app`, `_seed` (owner+member+stranger+ws+repo), `_client`, `_seed_source_reference` (Reference + ReferenceChunk + text.txt), `_commit_ledger`, `_init_repo` (from `tests/test_editing_save.py`), and `capture_base(repo_dir, section_index) -> (head, section_sha)` (from `proposal_assist/proposals/proposing.py`).

---

## File Structure

| File | Responsibility | Task |
|------|----------------|------|
| `src/call_manager/proposal_assist/citations/human_cite.py` | **Create** — sync `insert_citation_for_user` core + `CiteStale/CiteInvalid/CiteRefNotFound` | T1 |
| `tests/proposal_assist/citations/test_human_cite.py` | **Create** — core unit tests | T1 |
| `src/call_manager/web/api/workspaces.py` | **Modify** — `POST .../citations` + request model; `GET .../references`; `GET .../references/{id}/chunks` + models | T2,T3,T4 |
| `tests/test_api_workspaces.py` | **Modify** — endpoint tests for the three routes | T2,T3,T4 |
| `frontend/src/api/schema.gen.ts` | **Regenerate** | T5 |
| `frontend/src/editor/CodeMirrorEditor.tsx` | **Modify** — `onSelectionChange` prop | T6 |
| `frontend/src/editor/CodeMirrorEditor.test.tsx` | **Modify** — selection-bridge test | T6 |
| `frontend/src/api/citations.ts` | **Modify** — `useReferences`, `useReferenceChunks`, `useAddCitation` | T7 |
| `frontend/src/api/citations.test.tsx` | **Modify** — hook tests | T7 |
| `frontend/src/cockpit/CitationComposer.tsx` | **Create** — pick-reference → select-span → confirm | T8 |
| `frontend/src/cockpit/CitationComposer.test.tsx` | **Create** — composer tests | T8 |
| `frontend/src/routes/Cockpit.tsx` | **Modify** — selection state, Cite affordance, composer wiring | T9 |
| `frontend/src/routes/Cockpit.test.tsx` | **Modify** — end-to-end cite flow tests | T9 |
| `frontend/src/styles/cockpit.css` | **Modify** — composer + Cite-button styles | T9 |

---

## Task 1: Backend core `insert_citation_for_user`

A sync function that validates staleness + reference + chunk + exact-offset quote, inserts the marker after the (unique) anchor, builds the evidence row, and commits `proposal.md` + `citations.json` atomically. Caller owns the transaction (provenance rows added, not committed here).

**Files:**
- Create: `src/call_manager/proposal_assist/citations/human_cite.py`
- Test: `tests/proposal_assist/citations/test_human_cite.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/citations/test_human_cite.py`:

```python
"""Unit tests for the human select-to-cite core (SP2a-4b)."""

from __future__ import annotations

import subprocess
import uuid

import pytest

from call_manager.db.models import Reference, ReferenceChunk, User, Workspace
from call_manager.proposal_assist.citations.gitio import _git_env, current_head
from call_manager.proposal_assist.citations.human_cite import (
    CiteInvalid,
    CiteRefNotFound,
    CiteStale,
    insert_citation_for_user,
)
from call_manager.proposal_assist.citations.ledger import CITATIONS_FILENAME, load_ledger
from call_manager.proposal_assist.editing.save import PROPOSAL_FILENAME
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir


def _init_repo(root, ws_id, doc):
    repo = workspace_repo_dir(root, ws_id)
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=_git_env())
    (repo / PROPOSAL_FILENAME).write_text(doc)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=_git_env())
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, env=_git_env())
    return repo


def _seed_ws_ref(pg_session_factory, *, chunk_text, chunk_index=0):
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(title="W", owner_id=owner.id, current_phase="spec")
        s.add(ws)
        s.flush()
        ref = Reference(
            workspace_id=ws.id, kind="paper", source_type="uploaded",
            title="Smith 2021", filename="smith.pdf", storage_path="r/smith.pdf",
            mime_type="application/pdf", metadata_status="approved",
        )
        s.add(ref)
        s.flush()
        s.add(ReferenceChunk(
            reference_id=ref.id, workspace_id=ws.id, chunk_index=chunk_index,
            section_path=None, text=chunk_text, embedding=[0.0] * 1024,
            embedding_model="fake",
        ))
        s.commit()
        return owner.id, ws.id, ref.id


def _base(repo, section_index):
    head = current_head(repo)
    return head, split_sections((repo / PROPOSAL_FILENAME).read_text())[section_index].sha


def _call(
    session,
    ws,
    *,
    workspaces_dir,
    base_commit_sha,
    base_section_sha,
    reference_id,
    anchor="The sky is blue.",
    section_index=1,
    chunk_index=0,
    evidence_quote="azure",
    quote_start=0,
    quote_end=5,
    stance="supports",
    user_id=1,
):
    return insert_citation_for_user(
        session=session,
        workspace=ws,
        user_id=user_id,
        section_index=section_index,
        base_commit_sha=base_commit_sha,
        base_section_sha=base_section_sha,
        anchor=anchor,
        reference_id=reference_id,
        chunk_index=chunk_index,
        evidence_quote=evidence_quote,
        quote_start=quote_start,
        quote_end=quote_end,
        stance=stance,
        workspaces_dir=workspaces_dir,
    )


DOC = "preamble\n\n## Claims\nThe sky is blue.\n"


def test_happy_path_inserts_marker_and_ledger(pg_session_factory, tmp_path):
    owner_id, ws_id, ref_id = _seed_ws_ref(pg_session_factory, chunk_text="azure today")
    repo = _init_repo(tmp_path, ws_id, DOC)
    bc, bs = _base(repo, 1)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        out = _call(
            s, ws, reference_id=ref_id, anchor="The sky is blue.",
            evidence_quote="azure", quote_start=0, quote_end=5,
            base_commit_sha=bc, base_section_sha=bs, workspaces_dir=tmp_path, user_id=owner_id,
        )
        s.commit()
    assert set(out) == {"ev_id", "citekey", "commit_sha"}
    new_doc = (repo / PROPOSAL_FILENAME).read_text()
    assert f"[@{out['citekey']}]<!-- ev:{out['ev_id']} -->" in new_doc
    # marker trails the anchor
    assert "The sky is blue.[@" in new_doc
    ledger = load_ledger((repo / CITATIONS_FILENAME).read_text())
    row = next(r for r in ledger["evidence"] if r["id"] == out["ev_id"])
    assert row["evidence_quote"] == "azure"
    assert row["evidence_quote_offsets"] == [0, 5]
    # claim_context is the LIVE claim sentence around the marker (canonical
    # extractor), with the trailing terminator stripped — NOT the raw anchor.
    assert row["claim_context"]["text"] == "The sky is blue"


def test_repeated_span_uses_the_selected_occurrence(pg_session_factory, tmp_path):
    """Two identical substrings in one chunk: the stored offsets pin the user's
    actual occurrence (the exact-offset design — no false 'ambiguous' failure)."""
    owner_id, ws_id, ref_id = _seed_ws_ref(pg_session_factory, chunk_text="blue ... blue")
    repo = _init_repo(tmp_path, ws_id, DOC)
    bc, bs = _base(repo, 1)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        out = _call(  # the SECOND "blue" is at code points 9..13
            s, ws, reference_id=ref_id, anchor="The sky is blue.",
            evidence_quote="blue", quote_start=9, quote_end=13,
            base_commit_sha=bc, base_section_sha=bs, workspaces_dir=tmp_path,
        )
        s.commit()
    ledger = load_ledger((repo / CITATIONS_FILENAME).read_text())
    row = next(r for r in ledger["evidence"] if r["id"] == out["ev_id"])
    assert row["evidence_quote_offsets"] == [9, 13]
    assert row["evidence_quote"] == "blue"


def test_stale_section_raises(pg_session_factory, tmp_path):
    owner_id, ws_id, ref_id = _seed_ws_ref(pg_session_factory, chunk_text="azure sky")
    repo = _init_repo(tmp_path, ws_id, DOC)
    bc, bs = _base(repo, 1)
    # Change the cited section AND move HEAD underneath the captured base.
    (repo / PROPOSAL_FILENAME).write_text("preamble\n\n## Claims\nThe sky is grey.\n")
    subprocess.run(["git", "commit", "-aqm", "x"], cwd=repo, check=True, env=_git_env())
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        with pytest.raises(CiteStale):
            _call(s, ws, reference_id=ref_id, anchor="The sky is blue.",
                  evidence_quote="azure", quote_start=0, quote_end=5,
                  base_commit_sha=bc, base_section_sha=bs, workspaces_dir=tmp_path)


def test_other_section_changed_still_succeeds(pg_session_factory, tmp_path):
    """save_section predicate: HEAD moved, but the CITED section is unchanged."""
    owner_id, ws_id, ref_id = _seed_ws_ref(pg_session_factory, chunk_text="azure sky")
    doc = "preamble\n\n## Claims\nThe sky is blue.\n\n## Other\nuntouched.\n"
    repo = _init_repo(tmp_path, ws_id, doc)
    bc, bs = _base(repo, 1)  # the Claims section
    # Edit a DIFFERENT section (index 2), moving HEAD; Claims bytes unchanged.
    (repo / PROPOSAL_FILENAME).write_text(
        "preamble\n\n## Claims\nThe sky is blue.\n\n## Other\nCHANGED.\n"
    )
    subprocess.run(["git", "commit", "-aqm", "other"], cwd=repo, check=True, env=_git_env())
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        out = _call(s, ws, reference_id=ref_id, anchor="The sky is blue.",
                    evidence_quote="azure", quote_start=0, quote_end=5,
                    base_commit_sha=bc, base_section_sha=bs, workspaces_dir=tmp_path)
        s.commit()
    assert "ev_id" in out


def test_anchor_ambiguous_raises_invalid(pg_session_factory, tmp_path):
    owner_id, ws_id, ref_id = _seed_ws_ref(pg_session_factory, chunk_text="azure sky")
    repo = _init_repo(tmp_path, ws_id, "preamble\n\n## Claims\nfoo. foo.\n")
    bc, bs = _base(repo, 1)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        with pytest.raises(CiteInvalid):
            _call(s, ws, reference_id=ref_id, anchor="foo.",
                  evidence_quote="azure", quote_start=0, quote_end=5,
                  base_commit_sha=bc, base_section_sha=bs, workspaces_dir=tmp_path)


def test_quote_mismatch_raises_invalid(pg_session_factory, tmp_path):
    owner_id, ws_id, ref_id = _seed_ws_ref(pg_session_factory, chunk_text="azure sky")
    repo = _init_repo(tmp_path, ws_id, DOC)
    bc, bs = _base(repo, 1)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        with pytest.raises(CiteInvalid):
            _call(s, ws, reference_id=ref_id, anchor="The sky is blue.",
                  evidence_quote="NOTMATCHING", quote_start=0, quote_end=5,
                  base_commit_sha=bc, base_section_sha=bs, workspaces_dir=tmp_path)


def test_codepoint_offsets_with_non_bmp_char(pg_session_factory, tmp_path):
    """An emoji (non-BMP) before the selection: code-point offsets slice correctly."""
    chunk = "🚀 azure sky"  # "🚀" is ONE code point (index 0); " " at 1; "azure" at 2..7
    owner_id, ws_id, ref_id = _seed_ws_ref(pg_session_factory, chunk_text=chunk)
    repo = _init_repo(tmp_path, ws_id, DOC)
    bc, bs = _base(repo, 1)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        out = _call(s, ws, reference_id=ref_id, anchor="The sky is blue.",
                    evidence_quote="azure", quote_start=2, quote_end=7,
                    base_commit_sha=bc, base_section_sha=bs, workspaces_dir=tmp_path)
        s.commit()
    ledger = load_ledger((repo / CITATIONS_FILENAME).read_text())
    row = next(r for r in ledger["evidence"] if r["id"] == out["ev_id"])
    assert row["evidence_quote"] == "azure"
    assert row["evidence_quote_offsets"] == [2, 7]


def test_foreign_reference_raises_not_found(pg_session_factory, tmp_path):
    owner_id, ws_id, _ = _seed_ws_ref(pg_session_factory, chunk_text="azure sky")
    repo = _init_repo(tmp_path, ws_id, DOC)
    bc, bs = _base(repo, 1)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        with pytest.raises(CiteRefNotFound):
            _call(s, ws, reference_id=uuid.uuid4(), anchor="The sky is blue.",
                  evidence_quote="azure", quote_start=0, quote_end=5,
                  base_commit_sha=bc, base_section_sha=bs, workspaces_dir=tmp_path)


def test_anchor_outside_validated_section_raises_stale(pg_session_factory, tmp_path):
    """Security: a valid (section_index, base_section_sha) for section 1, but an
    anchor that only exists in section 2, must NOT land the marker in section 2 —
    the anchor-in-live.text guard rejects it (the marker may only land in the
    section whose bytes were validated)."""
    owner_id, ws_id, ref_id = _seed_ws_ref(pg_session_factory, chunk_text="azure sky")
    doc = "preamble\n\n## Claims\nThe sky is blue.\n\n## Other\nElsewhere only.\n"
    repo = _init_repo(tmp_path, ws_id, doc)
    bc, bs = _base(repo, 1)  # validate section 1 (Claims)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        with pytest.raises(CiteStale):
            _call(s, ws, reference_id=ref_id, anchor="Elsewhere only.",  # lives in section 2
                  evidence_quote="azure", quote_start=0, quote_end=5,
                  base_commit_sha=bc, base_section_sha=bs, workspaces_dir=tmp_path)
    assert "ev:" not in (repo / PROPOSAL_FILENAME).read_text()  # nothing committed
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b && uv run pytest tests/proposal_assist/citations/test_human_cite.py -q`
Expected: FAIL — `ModuleNotFoundError: …citations.human_cite`.

- [ ] **Step 3: Implement the core**

Create `src/call_manager/proposal_assist/citations/human_cite.py`:

```python
"""Human select-to-cite (SP2a-4b): a direct citation commit initiated by the
workspace owner from the cockpit editor. Mirrors analysis/insert.py
(insert_from_annotation) but takes an explicit anchor + chunk-local quote
offsets instead of a UserClaimAnnotation, applies save_section's section
staleness predicate, and deliberately omits the deleted _turn_in_flight freeze
(concurrency = commit mutex + section staleness). Caller owns the transaction:
provenance rows are added but NOT committed here (mirrors editing/save.py).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from call_manager.db.models import (
    ArtifactVersion,
    Reference,
    ReferenceChunk,
    Workspace,
    WorkspaceEvent,
)
from call_manager.proposal_assist.citations import claims
from call_manager.proposal_assist.citations.gitio import current_head, write_and_commit_files
from call_manager.proposal_assist.citations.ledger import (
    CITATIONS_FILENAME,
    append_evidence,
    build_evidence_row,
    dump_ledger,
    load_ledger,
    new_ev_id,
)
from call_manager.proposal_assist.citations.markers import AnchorError, find_markers, insert_marker
from call_manager.proposal_assist.citations.quotes import normalize_for_quote_match
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex
from call_manager.proposal_assist.editing.save import PROPOSAL_FILENAME
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.tools.citations import _citation_snapshot, _resolve_citekey_for_ref

logger = logging.getLogger(__name__)

_ALLOWED_STANCES = frozenset({"supports", "contradicts", "background"})


class CiteError(RuntimeError):
    """Base; str(exc) is user-facing (the endpoint echoes it)."""


class CiteStale(CiteError):
    """Cited section moved, or the anchor is no longer in the saved text (-> 409)."""


class CiteInvalid(CiteError):
    """Bad offsets/quote/chunk, ambiguous anchor, or bad stance (-> 422)."""


class CiteRefNotFound(CiteError):
    """Reference missing / foreign / tombstoned (-> 404)."""


def insert_citation_for_user(
    *,
    session: Session,
    workspace: Workspace,
    user_id: int,
    section_index: int,
    base_commit_sha: str,
    base_section_sha: str,
    anchor: str,
    reference_id: uuid.UUID,
    chunk_index: int,
    evidence_quote: str,
    quote_start: int,
    quote_end: int,
    stance: str = "supports",
    workspaces_dir: Path | str,
) -> dict:
    """Insert a human-selected citation. Returns {ev_id, citekey, commit_sha}.

    Raises CiteStale / CiteInvalid / CiteRefNotFound (mapped by the endpoint to
    409 / 422 / 404), CommitMutexTimeout (429) and GitCommitError (500). The
    caller owns the transaction (this adds provenance rows but does not commit)."""
    if stance not in _ALLOWED_STANCES:
        raise CiteInvalid(f"invalid stance {stance!r}")
    if not anchor:
        raise CiteInvalid("select a claim span to cite")

    # 1. Serialize against every other git mutation of this workspace.
    acquire_commit_mutex(session, workspace.id)

    # 2. Live document under the mutex + save_section's staleness predicate
    #    (HEAD moving is fine as long as THIS section's bytes are unchanged).
    repo = workspace_repo_dir(workspaces_dir, workspace.id)
    proposal_path = repo / PROPOSAL_FILENAME
    if not proposal_path.exists():
        raise CiteStale("this document changed — reopen and re-cite")
    head = current_head(repo)
    doc = proposal_path.read_text()
    sections = split_sections(doc)
    if not 0 <= section_index < len(sections):
        raise CiteStale("this section changed — reopen and re-cite")
    live = sections[section_index]
    if head != base_commit_sha and live.sha != base_section_sha:
        raise CiteStale("this section changed — reopen and re-cite")
    # The anchor MUST belong to the validated section. insert_marker searches the
    # whole document, so without this a stale/forged (section_index,
    # base_section_sha) pair could land the marker in a DIFFERENT, unvalidated
    # section. Requiring `anchor in live.text` + insert_marker's whole-doc
    # uniqueness guarantees the single match is the one in this section.
    if anchor not in live.text:
        raise CiteStale("this section changed — reopen and re-cite")

    # 3. Reference: workspace-scoped, not tombstoned.
    ref = session.get(Reference, reference_id)
    if ref is None or ref.workspace_id != workspace.id or ref.tombstoned_at is not None:
        raise CiteRefNotFound("that source is no longer available")

    # 4. Chunk (owned ref) + EXACT chunk-local offset verification. The client
    #    sends Unicode code-point offsets; chunk.text is code-point indexed, so
    #    the slice is authoritative and we store it verbatim.
    chunk = (
        session.query(ReferenceChunk)
        .filter(
            ReferenceChunk.reference_id == ref.id,
            ReferenceChunk.chunk_index == chunk_index,
            ReferenceChunk.workspace_id == workspace.id,
        )
        .one_or_none()
    )
    if chunk is None:
        raise CiteInvalid("the selected source passage is unavailable")
    if not (0 <= quote_start < quote_end <= len(chunk.text)):
        raise CiteInvalid("the selected passage is out of range — reselect it")
    sliced = chunk.text[quote_start:quote_end]
    if sliced != evidence_quote:
        raise CiteInvalid("the selected passage changed — reselect it")
    if not sliced.strip():
        raise CiteInvalid("select a non-empty passage")

    # 5. Ledger + citekey + marker. The anchor must be UNIQUE in the whole
    #    proposal; ambiguous -> 422, otherwise (not found) -> 409 (stale).
    citations_path = repo / CITATIONS_FILENAME
    try:
        ledger = load_ledger(citations_path.read_text() if citations_path.exists() else None)
    except ValueError as exc:
        raise CiteInvalid(f"citations.json is invalid: {exc}") from exc
    ev_id = new_ev_id()
    citekey = _resolve_citekey_for_ref(session, ledger=ledger, ref=ref, workspace_id=workspace.id)
    try:
        new_doc = insert_marker(doc, anchor, citekey, ev_id)
    except AnchorError as exc:
        if "ambiguous" in str(exc):
            raise CiteInvalid(
                "your selected claim appears more than once — select a longer, unique span"
            ) from exc
        raise CiteStale("this section changed — reopen and re-cite") from exc

    # 6. Claim context — the LIVE claim sentence around the just-inserted marker,
    #    via the canonical extractor (claims.py: insert/update/validate MUST share
    #    claim_sentence_around, so a fresh insert never immediately audits as
    #    claim_drift). Fall back to the bare anchor only if extraction degenerates.
    inserted = next((m for m in find_markers(new_doc) if m["ev_id"] == ev_id), None)
    claim_context_text = (
        claims.claim_sentence_around(new_doc, inserted["start"], inserted["end"])
        if inserted is not None
        else ""
    )
    if not claim_context_text:
        claim_context_text = anchor

    # 7. Evidence row (exact stored offsets).
    now_iso = datetime.now(UTC).isoformat()
    row = build_evidence_row(
        ev_id=ev_id,
        citekey=citekey,
        reference_id=str(ref.id),
        chunk_index=chunk_index,
        section_path=chunk.section_path,
        source_content_hash=ref.original_sha256,
        chunk_content_hash=hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
        evidence_quote=sliced,
        evidence_quote_offsets=[quote_start, quote_end],
        stance=stance,
        confidence=None,
        claim_context_text=claim_context_text,
        claim_context_hash=hashlib.sha256(
            normalize_for_quote_match(claim_context_text).encode("utf-8")
        ).hexdigest(),
        citation_snapshot=_citation_snapshot(ref.csl_json, citekey),
        created_at=now_iso,
        updated_at=now_iso,
    )
    new_ledger = append_evidence(ledger, row)

    # 8. Atomic commit — proposal.md + citations.json in ONE commit.
    commit_sha = write_and_commit_files(
        repo,
        {PROPOSAL_FILENAME: new_doc, CITATIONS_FILENAME: dump_ledger(new_ledger)},
        f"user select-to-cite: {citekey} (user:{user_id})",
    )

    # 9. Provenance (committed by the caller, atomically with the mutex release).
    session.add(
        ArtifactVersion(
            workspace_id=workspace.id,
            commit_sha=commit_sha,
            created_by="user",
            message=f"user select-to-cite: {citekey}",
        )
    )
    session.add(
        WorkspaceEvent(
            workspace_id=workspace.id,
            kind="user_citation_insert",
            actor_id=user_id,
            payload={"ev_id": ev_id, "citekey": citekey, "section_index": section_index},
        )
    )
    if ref.citekey != citekey:
        ref.citekey = citekey
    return {"ev_id": ev_id, "citekey": citekey, "commit_sha": commit_sha}
```

NOTE: the keyword-only signature places `workspaces_dir` after `stance` (which has a default). Because every parameter is keyword-only (`*,`), a defaulted param before a non-defaulted one is legal Python — do not reorder.

- [ ] **Step 4: Run to verify they pass**

Run: `cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b && uv run pytest tests/proposal_assist/citations/test_human_cite.py -q`
Expected: PASS (9 passed). Re-run once in isolation on a `connection refused`.

- [ ] **Step 5: Lint + commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b
uv run ruff check src/call_manager/proposal_assist/citations/human_cite.py tests/proposal_assist/citations/test_human_cite.py
git add src/call_manager/proposal_assist/citations/human_cite.py tests/proposal_assist/citations/test_human_cite.py
git commit -m "feat(citations): insert_citation_for_user core for human select-to-cite

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `POST /api/workspaces/{id}/citations` endpoint

Owner-gated **sync** endpoint: validate body, 404-precheck, call the core, own the transaction, map `CiteStale/CiteInvalid/CiteRefNotFound/CommitMutexTimeout/GitCommitError` to HTTP.

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py`
- Test: `tests/test_api_workspaces.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api_workspaces.py` (helpers `_app/_seed/_client/_seed_source_reference/_commit_ledger` already exist; `capture_base` is imported as `_capture` near the save tests):

```python
def _cite_body(bc, bs, rid, **over):
    body = {
        "section_index": 1, "base_commit_sha": bc, "base_section_sha": bs,
        "anchor": "original.", "reference_id": str(rid),
        "chunk_index": 0, "evidence_quote": "azure", "quote_start": 0, "quote_end": 5,
        "stance": "supports",
    }
    body.update(over)
    return body


def test_post_citation_happy_path(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n")
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="azure sky")
    bc, bs = _capture(tmp_path, ws_id, 1)
    c = _client(app, owner_id)
    r = c.post(f"/api/workspaces/{ws_id}/citations", json=_cite_body(bc, bs, rid))
    assert r.status_code == 200, r.text
    b = r.json()
    assert set(b) == {"ev_id", "citekey", "commit_sha"}
    saved = (workspace_repo_dir(tmp_path, ws_id) / PROPOSAL_FILENAME).read_text()
    assert f"original.[@{b['citekey']}]<!-- ev:{b['ev_id']} -->" in saved


def test_post_citation_member_is_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, member_id, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n")
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="azure sky")
    bc, bs = _capture(tmp_path, ws_id, 1)
    c = _client(app, member_id)
    r = c.post(f"/api/workspaces/{ws_id}/citations", json=_cite_body(bc, bs, rid))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_post_citation_stale_is_409(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n")
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="azure sky")
    bc, bs = _capture(tmp_path, ws_id, 1)
    repo = workspace_repo_dir(tmp_path, ws_id)
    (repo / PROPOSAL_FILENAME).write_text("preamble\n\n## Methods\nCHANGED text.\n")
    subprocess.run(["git", "commit", "-aqm", "x"], cwd=repo, check=True, env=_git_env())
    c = _client(app, owner_id)
    r = c.post(f"/api/workspaces/{ws_id}/citations", json=_cite_body(bc, bs, rid, anchor="original."))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


def test_post_citation_double_submit_second_is_409(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n")
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="azure sky")
    bc, bs = _capture(tmp_path, ws_id, 1)
    c = _client(app, owner_id)
    body = _cite_body(bc, bs, rid)
    r1 = c.post(f"/api/workspaces/{ws_id}/citations", json=body)
    assert r1.status_code == 200, r1.text
    r2 = c.post(f"/api/workspaces/{ws_id}/citations", json=body)  # same stale base
    assert r2.status_code == 409
    saved = (workspace_repo_dir(tmp_path, ws_id) / PROPOSAL_FILENAME).read_text()
    assert saved.count("ev:") == 1  # exactly one marker


def test_post_citation_quote_mismatch_is_422(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n")
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="azure sky")
    bc, bs = _capture(tmp_path, ws_id, 1)
    c = _client(app, owner_id)
    r = c.post(f"/api/workspaces/{ws_id}/citations", json=_cite_body(bc, bs, rid, evidence_quote="WRONG"))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_post_citation_foreign_reference_is_404(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n")
    bc, bs = _capture(tmp_path, ws_id, 1)
    c = _client(app, owner_id)
    r = c.post(f"/api/workspaces/{ws_id}/citations", json=_cite_body(bc, bs, uuid.uuid4()))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_post_citation_busy_is_429(monkeypatch, pg_url, pg_session_factory, tmp_path):
    from call_manager.proposal_assist.drafting.locking import BUSY_ERROR_MESSAGE, CommitMutexTimeout

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n")
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="azure sky")
    bc, bs = _capture(tmp_path, ws_id, 1)

    def _boom(**kwargs):
        raise CommitMutexTimeout("busy")

    monkeypatch.setattr("call_manager.web.api.workspaces.insert_citation_for_user", _boom)
    c = _client(app, owner_id)
    r = c.post(f"/api/workspaces/{ws_id}/citations", json=_cite_body(bc, bs, rid))
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "too_many_requests"
    assert r.headers.get("Retry-After") == "2"
```

(Confirm `_git_env` is imported in this test file; the SP2a-4a tests already import it inside helpers — add a module-level `from call_manager.proposal_assist.citations.gitio import _git_env` if not present.)

- [ ] **Step 2: Run to verify they fail**

Run: `cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b && uv run pytest tests/test_api_workspaces.py -q -k post_citation`
Expected: FAIL (405/404 — route not defined).

- [ ] **Step 3: Implement the endpoint**

In `src/call_manager/web/api/workspaces.py`, add imports:

```python
from pydantic import Field  # extend the existing pydantic import
from call_manager.proposal_assist.citations.human_cite import (
    CiteInvalid,
    CiteRefNotFound,
    CiteStale,
    insert_citation_for_user,
)
from call_manager.proposal_assist.citations.gitio import GitCommitError  # already imported
```

Add the request model (near the other models):

```python
class AddCitationIn(BaseModel):
    section_index: int
    base_commit_sha: str
    base_section_sha: str
    anchor: str = Field(min_length=1)
    reference_id: uuid.UUID
    chunk_index: int
    evidence_quote: str = Field(min_length=1)
    quote_start: int = Field(ge=0)
    quote_end: int = Field(ge=0)
    stance: str = "supports"


class AddCitationOut(BaseModel):
    ev_id: str
    citekey: str
    commit_sha: str
```

Add the route (after `post_save_section`):

```python
@router.post("/workspaces/{workspace_id}/citations", response_model=AddCitationOut)
def post_citation(
    workspace_id: uuid.UUID,
    body: AddCitationIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> AddCitationOut:
    """Human select-to-cite: a direct citation commit (owner-gated). Sync handler
    (FastAPI threadpools it) calling the sync insert_citation_for_user core; this
    handler owns the transaction (single commit/rollback, mutex released on it)."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        # 404 precheck (mirror the save route): proposal.md + git HEAD must exist.
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        if not (repo_dir / PROPOSAL_FILENAME).exists():
            raise HTTPException(status_code=404, detail="no proposal.md yet")
        try:
            current_head(repo_dir)
        except GitCommitError as e:
            raise HTTPException(status_code=404, detail="no proposal.md yet") from e
        try:
            result = insert_citation_for_user(
                session=session,
                workspace=ws,
                user_id=user.id,
                section_index=body.section_index,
                base_commit_sha=body.base_commit_sha,
                base_section_sha=body.base_section_sha,
                anchor=body.anchor,
                reference_id=body.reference_id,
                chunk_index=body.chunk_index,
                evidence_quote=body.evidence_quote,
                quote_start=body.quote_start,
                quote_end=body.quote_end,
                stance=body.stance,
                workspaces_dir=settings.workspaces_dir,
            )
            session.commit()
        except CiteStale as e:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(e)) from e
        except CiteRefNotFound as e:
            session.rollback()
            raise HTTPException(status_code=404, detail=str(e)) from e
        except CiteInvalid as e:
            session.rollback()
            raise HTTPException(status_code=422, detail=str(e)) from e
        except CommitMutexTimeout as e:
            session.rollback()
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        except GitCommitError as e:
            session.rollback()
            logger.exception("user citation insert failed for workspace %s", workspace_id)
            raise HTTPException(status_code=500, detail="citation failed — nothing was written") from e
    return AddCitationOut(**result)
```

Confirm `422` is in the `/api` router's documented `STATUS_CODES` (`web/api/__init__.py` / `web/api/errors.py`). If 422 is absent from `STATUS_CODES`, add it (mapping to `validation_error`) so the OpenAPI response set matches what this route emits — the runtime envelope already uses `validation_error` for 422 (`errors.py`).

- [ ] **Step 4: Run to verify they pass**

Run: `cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b && uv run pytest tests/test_api_workspaces.py -q -k post_citation`
Expected: PASS (7 passed).

- [ ] **Step 5: Lint + commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b
uv run ruff check src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py
git add src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py
git commit -m "feat(api): POST /api/workspaces/{id}/citations (human select-to-cite)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `GET /api/workspaces/{id}/references` (picker list)

Member-gated list of non-tombstoned references for the composer's reference picker.

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py`
- Test: `tests/test_api_workspaces.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api_workspaces.py`:

```python
def test_references_list_member_gated_excludes_tombstoned(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, stranger_id, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    live = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="azure")
    dead = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="grey")
    from datetime import UTC, datetime
    with pg_session_factory() as s:
        s.get(Reference, dead).tombstoned_at = datetime.now(UTC)
        s.commit()
    # member can read; tombstoned excluded
    r = _client(app, member_id).get(f"/api/workspaces/{ws_id}/references")
    assert r.status_code == 200, r.text
    ids = {x["id"] for x in r.json()}
    assert str(live) in ids and str(dead) not in ids
    one = next(x for x in r.json() if x["id"] == str(live))
    assert one["title"] == "The Smith 2021 Study"
    assert one["text_status"] == "done"
    # stranger forbidden
    assert _client(app, stranger_id).get(f"/api/workspaces/{ws_id}/references").status_code == 403
```

(`_seed_source_reference` defaults `text_status="done"` and `title="The Smith 2021 Study"`; confirm those defaults — adjust the assertion if the helper differs.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b && uv run pytest tests/test_api_workspaces.py -q -k references_list`
Expected: FAIL (404 — route not defined).

- [ ] **Step 3: Implement**

In `workspaces.py` add the import `from sqlalchemy import select` (already present from SP2a-4a) and the model + route:

```python
class ReferenceListItemOut(BaseModel):
    id: str
    citekey: str | None = None
    title: str
    kind: str
    text_status: str


@router.get("/workspaces/{workspace_id}/references", response_model=list[ReferenceListItemOut])
def list_references(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> list[ReferenceListItemOut]:
    """The workspace's citable references for the select-to-cite picker. Member-gated."""
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        refs = session.scalars(
            select(Reference)
            .where(Reference.workspace_id == workspace_id, Reference.tombstoned_at.is_(None))
            .order_by(Reference.created_at.desc())
        ).all()
        out: list[ReferenceListItemOut] = []
        for ref in refs:
            csl = ref.csl_json or ref.csl_json_extracted or {}
            csl_title = csl.get("title") if isinstance(csl, dict) else None
            title = ref.title or (csl_title if isinstance(csl_title, str) else None) or ref.filename
            out.append(
                ReferenceListItemOut(
                    id=str(ref.id),
                    citekey=ref.citekey,
                    title=str(title),
                    kind=ref.kind,
                    text_status=ref.text_status,
                )
            )
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b && uv run pytest tests/test_api_workspaces.py -q -k references_list`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b
uv run ruff check src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py
git add src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py
git commit -m "feat(api): GET /api/workspaces/{id}/references for the cite picker

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: `GET /api/workspaces/{id}/references/{ref_id}/chunks`

Member-gated, ownership-guarded chunk list for the source-selection pane.

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py`
- Test: `tests/test_api_workspaces.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api_workspaces.py`:

```python
def test_reference_chunks_ordered_and_guarded(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, stranger_id, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    # Seed a reference with two chunks (chunk_index 1 added after 0 to test ordering).
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="chunk zero")
    with pg_session_factory() as s:
        s.add(ReferenceChunk(
            reference_id=rid, workspace_id=ws_id, chunk_index=1, section_path="## B",
            text="chunk one", embedding=[0.0] * 1024, embedding_model="fake",
        ))
        s.commit()
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/references/{rid}/chunks")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reference_id"] == str(rid)
    assert [c["chunk_index"] for c in body["chunks"]] == [0, 1]
    assert body["chunks"][0]["text"] == "chunk zero"
    # stranger forbidden
    assert _client(app, stranger_id).get(
        f"/api/workspaces/{ws_id}/references/{rid}/chunks"
    ).status_code == 403


def test_reference_chunks_foreign_reference_is_404(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n")
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/references/{uuid.uuid4()}/chunks")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b && uv run pytest tests/test_api_workspaces.py -q -k reference_chunks`
Expected: FAIL (404 route).

- [ ] **Step 3: Implement**

In `workspaces.py` add the models + route:

```python
class ChunkOut(BaseModel):
    chunk_index: int
    section_path: str | None = None
    text: str


class ReferenceChunksOut(BaseModel):
    reference_id: str
    title: str
    chunks: list[ChunkOut]


@router.get(
    "/workspaces/{workspace_id}/references/{reference_id}/chunks",
    response_model=ReferenceChunksOut,
)
def get_reference_chunks(
    workspace_id: uuid.UUID,
    reference_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> ReferenceChunksOut:
    """A reference's chunks (verbatim text), for selecting a supporting span.
    Member-gated; ownership-guarded (cross-workspace reference -> 404)."""
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        ref = session.get(Reference, reference_id)
        if ref is None or ref.workspace_id != workspace_id:
            raise HTTPException(status_code=404, detail="reference not found")
        csl = ref.csl_json or ref.csl_json_extracted or {}
        csl_title = csl.get("title") if isinstance(csl, dict) else None
        title = ref.title or (csl_title if isinstance(csl_title, str) else None) or ref.filename
        rows = session.scalars(
            select(ReferenceChunk)
            .where(
                ReferenceChunk.reference_id == ref.id,
                ReferenceChunk.workspace_id == workspace_id,
            )
            .order_by(ReferenceChunk.chunk_index)
        ).all()
        chunks = [
            ChunkOut(chunk_index=c.chunk_index, section_path=c.section_path, text=c.text)
            for c in rows
        ]
    return ReferenceChunksOut(reference_id=str(reference_id), title=str(title), chunks=chunks)
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b && uv run pytest tests/test_api_workspaces.py -q -k reference_chunks`
Expected: PASS.

- [ ] **Step 5: Full backend file + commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b
uv run ruff check src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py
uv run pytest tests/test_api_workspaces.py -q
git add src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py
git commit -m "feat(api): GET /api/workspaces/{id}/references/{id}/chunks for span selection

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Regenerate `schema.gen.ts`

- [ ] **Step 1: Install deps (fresh worktree) + regenerate**

```bash
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b/frontend
nvm use >/dev/null
pnpm install
EMBEDDING_MODEL=bge-m3 pnpm gen:api
```
Expected: `wrote frontend/src/api/schema.gen.ts`.

- [ ] **Step 2: Verify the new paths/types + tsc**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b/frontend
grep -c "citations\"" src/api/schema.gen.ts   # the POST path key
grep -c "AddCitationOut\|ReferenceChunksOut\|ReferenceListItemOut" src/api/schema.gen.ts
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; nvm use >/dev/null && pnpm exec tsc --noEmit && echo TSC_OK
```
Expected: counts ≥ 1; `TSC_OK`.

- [ ] **Step 3: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b
git add frontend/src/api/schema.gen.ts
git commit -m "chore(spa): regen schema.gen.ts for select-to-cite endpoints

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Editor selection bridge (`onSelectionChange`)

Expose the editor's current non-empty selection so the cockpit can offer "Cite selection". The selection's `text` becomes the anchor (a substring search — no offset-encoding concerns here; code-point handling is only for the *source* chunk in T8).

**Files:**
- Modify: `frontend/src/editor/CodeMirrorEditor.tsx`
- Test: `frontend/src/editor/CodeMirrorEditor.test.tsx`

- [ ] **Step 1: Write the failing test**

Append to `frontend/src/editor/CodeMirrorEditor.test.tsx`:

```tsx
it("fires onSelectionChange with the selected text, and null when cleared", () => {
  const calls: Array<{ from: number; to: number; text: string } | null> = [];
  const { container } = render(
    <CodeMirrorEditor
      value={"## Methods\nThe sky is blue.\n"}
      onChange={() => {}}
      requireHeading
      onSelectionChange={(s) => calls.push(s)}
    />,
  );
  const view = (container.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: import("@codemirror/view").EditorView;
  }).__cmView!;
  const doc = view.state.doc.toString();
  const from = doc.indexOf("The sky");
  const to = from + "The sky is blue.".length;
  view.dispatch({ selection: { anchor: from, head: to } });
  expect(calls.at(-1)).toEqual({ from, to, text: "The sky is blue." });
  view.dispatch({ selection: { anchor: to, head: to } }); // collapse
  expect(calls.at(-1)).toBeNull();
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; nvm use >/dev/null && pnpm exec vitest run src/editor/CodeMirrorEditor.test.tsx`
Expected: FAIL (`onSelectionChange` not a prop; nothing pushed).

- [ ] **Step 3: Implement**

In `frontend/src/editor/CodeMirrorEditor.tsx`:

1. Extend the props type:
```tsx
  /** Wired FOR SP2a-4 select-to-cite; defaults to a no-op. */
  onPillClick?: (citekey: string, evId: string) => void;
  /** SP2a-4b: the current non-empty editor selection (or null when collapsed). */
  onSelectionChange?: (sel: { from: number; to: number; text: string } | null) => void;
```

2. In the destructured params add `onSelectionChange = () => {}`, and add a ref next to the others:
```tsx
  const onSelectionChangeRef = useRef(onSelectionChange);
  onSelectionChangeRef.current = onSelectionChange;
```

3. Replace the `updateListener` extension with:
```tsx
          EditorView.updateListener.of((u) => {
            if (u.docChanged && u.transactions.some((t) => !t.annotation(Transaction.remote))) {
              onChangeRef.current(u.state.doc.toString());
            }
            if (u.selectionSet || u.docChanged) {
              const sel = u.state.selection.main;
              if (!sel.empty && !u.state.facet(EditorState.readOnly)) {
                onSelectionChangeRef.current?.({
                  from: sel.from,
                  to: sel.to,
                  text: u.state.sliceDoc(sel.from, sel.to),
                });
              } else {
                onSelectionChangeRef.current?.(null);
              }
            }
          }),
```

- [ ] **Step 4: Run to verify it passes**

Run: `export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b/frontend && nvm use >/dev/null && pnpm exec vitest run src/editor/CodeMirrorEditor.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS; tsc clean.

- [ ] **Step 5: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b
git add frontend/src/editor/CodeMirrorEditor.tsx frontend/src/editor/CodeMirrorEditor.test.tsx
git commit -m "feat(spa): editor onSelectionChange bridge for select-to-cite

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Cite API hooks

Add `useReferences`, `useReferenceChunks`, `useAddCitation` to the existing citations hook module.

**Files:**
- Modify: `frontend/src/api/citations.ts`
- Test: `frontend/src/api/citations.test.tsx`

- [ ] **Step 1: Write the failing tests**

Append to `frontend/src/api/citations.test.tsx`:

```tsx
import { useReferences, useReferenceChunks, useAddCitation } from "./citations";

describe("useReferences / useReferenceChunks / useAddCitation", () => {
  it("useReferences returns the unwrapped list", async () => {
    const refs = [{ id: "r1", citekey: "smith2021", title: "Smith", kind: "paper", text_status: "done" }];
    vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: refs, error: undefined } as never);
    const qc = new QueryClient();
    const { result } = renderHook(() => useReferences("ws-1"), { wrapper: wrapper(qc) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(refs);
  });

  it("useReferenceChunks is disabled until a reference id is chosen", () => {
    const spy = vi.spyOn(clientMod.api, "GET");
    const qc = new QueryClient();
    const { result } = renderHook(() => useReferenceChunks("ws-1", null), { wrapper: wrapper(qc) });
    expect(result.current.fetchStatus).toBe("idle");
    expect(spy).not.toHaveBeenCalled();
  });

  it("useAddCitation POSTs the body and resolves the result", async () => {
    const out = { ev_id: "ev-1", citekey: "smith2021", commit_sha: "a".repeat(40) };
    const spy = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: out, error: undefined } as never);
    const qc = new QueryClient();
    const { result } = renderHook(() => useAddCitation("ws-1"), { wrapper: wrapper(qc) });
    const body = {
      section_index: 1, base_commit_sha: "a".repeat(40), base_section_sha: "s",
      anchor: "claim.", reference_id: "r1", chunk_index: 0,
      evidence_quote: "azure", quote_start: 0, quote_end: 5, stance: "supports",
    };
    await expect(result.current.mutateAsync(body)).resolves.toEqual(out);
    expect(spy).toHaveBeenCalledOnce();
  });
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b/frontend && nvm use >/dev/null && pnpm exec vitest run src/api/citations.test.tsx`
Expected: FAIL (exports missing).

- [ ] **Step 3: Implement**

Append to `frontend/src/api/citations.ts` (add `useMutation`, `useQueryClient` to the `@tanstack/react-query` import):

```tsx
export type AddCitationVars = {
  section_index: number;
  base_commit_sha: string;
  base_section_sha: string;
  anchor: string;
  reference_id: string;
  chunk_index: number;
  evidence_quote: string;
  quote_start: number;
  quote_end: number;
  stance: string;
};

/** GET /api/workspaces/{id}/references — references for the cite picker. */
export function useReferences(id: string) {
  return useQuery({
    queryKey: ["references", id],
    queryFn: async () =>
      unwrap(await api.GET("/api/workspaces/{workspace_id}/references", {
        params: { path: { workspace_id: id } },
      })),
  });
}

/** GET /api/workspaces/{id}/references/{refId}/chunks — disabled until a ref is picked. */
export function useReferenceChunks(id: string, refId: string | null) {
  return useQuery({
    queryKey: ["reference-chunks", id, refId],
    enabled: refId !== null,
    queryFn: async () =>
      unwrap(await api.GET("/api/workspaces/{workspace_id}/references/{reference_id}/chunks", {
        params: { path: { workspace_id: id, reference_id: refId as string } },
      })),
  });
}

/** POST /api/workspaces/{id}/citations — human select-to-cite (direct commit). */
export function useAddCitation(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (vars: AddCitationVars) =>
      unwrap(await api.POST("/api/workspaces/{workspace_id}/citations", {
        params: { path: { workspace_id: id } },
        body: vars,
      })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["draft", id] }),
  });
}
```

- [ ] **Step 4: Run to verify they pass**

Run: `export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b/frontend && nvm use >/dev/null && pnpm exec vitest run src/api/citations.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS; tsc clean.

- [ ] **Step 5: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b
git add frontend/src/api/citations.ts frontend/src/api/citations.test.tsx
git commit -m "feat(spa): useReferences/useReferenceChunks/useAddCitation hooks

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: `CitationComposer` component + code-point offset helper

The right-pane composer: pick a reference → render its chunks verbatim → select a supporting span (captured as chunk-local **code-point** offsets) → confirm → POST.

**Files:**
- Create: `frontend/src/cockpit/selection.ts` (pure offset helper)
- Create: `frontend/src/cockpit/CitationComposer.tsx`
- Test: `frontend/src/cockpit/CitationComposer.test.tsx`

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/cockpit/CitationComposer.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { CitationComposer } from "./CitationComposer";
import { toCodePointOffset } from "./selection";

function wrap(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

describe("toCodePointOffset", () => {
  it("counts code points before a UTF-16 index (non-BMP aware)", () => {
    expect(toCodePointOffset("🚀 azure", 0)).toBe(0); // before the emoji
    expect(toCodePointOffset("🚀 azure", 2)).toBe(1); // after the 2-unit emoji -> 1 code point
    expect(toCodePointOffset("🚀 azure", 4)).toBe(3); // "🚀 a" -> 3 code points
  });
});

describe("CitationComposer", () => {
  const claim = {
    section_index: 1, base_commit_sha: "a".repeat(40), base_section_sha: "s", anchor: "claim.",
  };

  function mockApi() {
    vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
      if (path.endsWith("/chunks")) {
        return Promise.resolve({
          data: { reference_id: "r1", title: "Smith", chunks: [
            { chunk_index: 0, section_path: null, text: "the azure sky" },
          ] },
          error: undefined,
        });
      }
      return Promise.resolve({
        data: [{ id: "r1", citekey: "smith2021", title: "Smith", kind: "paper", text_status: "done" }],
        error: undefined,
      });
    }) as never);
  }

  it("picks a reference, selects a span and POSTs the citation", async () => {
    mockApi();
    const post = vi
      .spyOn(clientMod.api, "POST")
      .mockResolvedValue({ data: { ev_id: "ev-1", citekey: "smith2021", commit_sha: "a".repeat(40) }, error: undefined } as never);
    const onDone = vi.fn();
    const qc = new QueryClient();
    render(
      <QueryClientProvider client={qc}>
        <CitationComposer workspaceId="ws-1" claim={claim} onDone={onDone} onCancel={() => {}} />
      </QueryClientProvider>,
      { wrapper: undefined },
    );
    // pick the reference
    fireEvent.click(await screen.findByRole("button", { name: /Smith/ }));
    const pre = await screen.findByText("the azure sky");
    // stub a DOM selection of "azure" (utf16 4..9) within the chunk node
    const range = { startOffset: 4, endOffset: 9, toString: () => "azure" };
    vi.spyOn(window, "getSelection").mockReturnValue({
      rangeCount: 1,
      getRangeAt: () => ({ ...range, startContainer: pre.firstChild, endContainer: pre.firstChild }),
      toString: () => "azure",
    } as never);
    fireEvent.mouseUp(pre);
    // confirm
    fireEvent.click(await screen.findByRole("button", { name: /add citation/i }));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    const body = (post.mock.calls[0][1] as { body: Record<string, unknown> }).body;
    expect(body).toMatchObject({
      reference_id: "r1", chunk_index: 0, evidence_quote: "azure",
      quote_start: 4, quote_end: 9, anchor: "claim.", section_index: 1,
    });
    await waitFor(() => expect(onDone).toHaveBeenCalled());
  });
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b/frontend && nvm use >/dev/null && pnpm exec vitest run src/cockpit/CitationComposer.test.tsx`
Expected: FAIL (modules missing).

- [ ] **Step 3: Implement the helper**

Create `frontend/src/cockpit/selection.ts`:

```ts
/** Convert a UTF-16 code-unit offset into a Unicode code-point offset, so the
 *  offsets we send match Python's code-point indexing of the same string
 *  (a non-BMP char counts as 1 code point but 2 UTF-16 units). */
export function toCodePointOffset(text: string, utf16Offset: number): number {
  return [...text.slice(0, utf16Offset)].length;
}
```

- [ ] **Step 4: Implement the composer**

Create `frontend/src/cockpit/CitationComposer.tsx`:

```tsx
import { useState } from "react";

import { ApiError } from "../api/client";
import { useAddCitation, useReferenceChunks, useReferences } from "../api/citations";
import { toCodePointOffset } from "./selection";

export type ClaimContext = {
  section_index: number;
  base_commit_sha: string;
  base_section_sha: string;
  anchor: string;
};

type Props = {
  workspaceId: string;
  claim: ClaimContext;
  onDone: () => void;
  onCancel: () => void;
};

type Span = { chunk_index: number; quote_start: number; quote_end: number; evidence_quote: string };

/** Right-pane select-to-cite composer: pick a reference, select a supporting
 *  span in its (verbatim) chunk text, confirm -> POST a direct citation. */
export function CitationComposer({ workspaceId, claim, onDone, onCancel }: Props) {
  const refs = useReferences(workspaceId);
  const [refId, setRefId] = useState<string | null>(null);
  const chunks = useReferenceChunks(workspaceId, refId);
  const [span, setSpan] = useState<Span | null>(null);
  const add = useAddCitation(workspaceId);
  const [error, setError] = useState<string | null>(null);

  // Read the DOM selection inside a chunk <pre>; store chunk-local code-point offsets.
  function captureSelection(chunkIndex: number, chunkText: string) {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;
    const range = sel.getRangeAt(0);
    const quote = sel.toString();
    if (!quote) {
      setSpan(null);
      return;
    }
    const start = toCodePointOffset(chunkText, range.startOffset);
    const end = toCodePointOffset(chunkText, range.endOffset);
    const [lo, hi] = start <= end ? [start, end] : [end, start];
    setSpan({ chunk_index: chunkIndex, quote_start: lo, quote_end: hi, evidence_quote: quote });
  }

  async function submit() {
    if (!span) return;
    setError(null);
    try {
      await add.mutateAsync({
        section_index: claim.section_index,
        base_commit_sha: claim.base_commit_sha,
        base_section_sha: claim.base_section_sha,
        anchor: claim.anchor,
        reference_id: refId as string,
        chunk_index: span.chunk_index,
        evidence_quote: span.evidence_quote,
        quote_start: span.quote_start,
        quote_end: span.quote_end,
        stance: "supports",
      });
      onDone();
    } catch (e) {
      setError(
        e instanceof ApiError
          ? e.message.replace(`${e.code}: `, "")
          : "Could not add the citation — please try again.",
      );
    }
  }

  return (
    <aside className="cite-composer" aria-label="Add a citation">
      <header className="cite-composer-head">
        <strong>Cite:</strong> <span className="cite-composer-claim">{claim.anchor}</span>
        <button type="button" aria-label="Cancel" onClick={onCancel}>✕</button>
      </header>

      {refId === null ? (
        <div className="cite-composer-refs">
          <p>Pick a reference:</p>
          {refs.isLoading && <p aria-busy="true">Loading references…</p>}
          <ul>
            {(refs.data ?? []).map((r) => (
              <li key={r.id}>
                <button
                  type="button"
                  disabled={r.text_status !== "done"}
                  onClick={() => setRefId(r.id)}
                >
                  {r.title}
                  {r.text_status !== "done" ? " (not indexed yet)" : ""}
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <div className="cite-composer-source">
          <button type="button" onClick={() => { setRefId(null); setSpan(null); }}>
            ‹ back to references
          </button>
          <p>Select the supporting passage:</p>
          {chunks.isLoading && <p aria-busy="true">Loading source…</p>}
          {(chunks.data?.chunks ?? []).map((c) => (
            <pre
              key={c.chunk_index}
              className="cite-composer-chunk"
              onMouseUp={() => captureSelection(c.chunk_index, c.text)}
            >
              {c.text}
            </pre>
          ))}
          {chunks.data && chunks.data.chunks.length === 0 && (
            <p className="cite-composer-empty">This source isn’t indexed yet.</p>
          )}
          {span && (
            <p className="cite-composer-confirm">
              Quote: <q>{span.evidence_quote}</q>{" "}
              <button type="button" disabled={add.isPending} onClick={submit}>
                Add citation
              </button>
            </p>
          )}
          {error !== null && <p role="alert">{error}</p>}
        </div>
      )}
    </aside>
  );
}
```

- [ ] **Step 5: Run to verify they pass**

Run: `export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b/frontend && nvm use >/dev/null && pnpm exec vitest run src/cockpit/CitationComposer.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS (4 passed across the two describes); tsc clean.

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b
git add frontend/src/cockpit/selection.ts frontend/src/cockpit/CitationComposer.tsx frontend/src/cockpit/CitationComposer.test.tsx
git commit -m "feat(spa): CitationComposer (pick reference, select span, code-point offsets)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: Wire select-to-cite into Cockpit + styles

Hold the editor selection, gate a "Cite selection" control on a saved (non-dirty) section, open the composer, and refresh the draft on success.

**Files:**
- Modify: `frontend/src/routes/Cockpit.tsx`
- Modify: `frontend/src/styles/cockpit.css`
- Test: `frontend/src/routes/Cockpit.test.tsx`

- [ ] **Step 1: Write the failing test**

Append to `frontend/src/routes/Cockpit.test.tsx` (reuses `draftPayload`/`renderCockpit`/`mockGetByPath` helpers; add a path-aware GET mock for references/chunks):

```tsx
it("select-to-cite: selecting a saved claim opens the composer and adds a citation", async () => {
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/references")) {
      return Promise.resolve({ data: [{ id: "r1", citekey: "k", title: "Smith", kind: "paper", text_status: "done" }], error: undefined });
    }
    if (path.endsWith("/chunks")) {
      return Promise.resolve({ data: { reference_id: "r1", title: "Smith", chunks: [{ chunk_index: 0, section_path: null, text: "the azure sky" }] }, error: undefined });
    }
    return Promise.resolve(draftPayload(true) as never); // /draft
  }) as never);
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { ev_id: "ev-1", citekey: "k", commit_sha: "a".repeat(40) }, error: undefined } as never);
  const { container } = renderCockpit();
  fireEvent.click(await screen.findByRole("button", { name: /Methods/ }));
  // make a selection in the (clean) section via the editor handle
  const view = (document.querySelector(".cm-editor") as HTMLElement & {
    __cmView?: import("@codemirror/view").EditorView;
  }).__cmView!;
  const docText = view.state.doc.toString();
  const f = docText.indexOf("body");
  act(() => view.dispatch({ selection: { anchor: f, head: f + 5 } })); // "body." incl. period
  // the Cite control appears (section is not dirty)
  fireEvent.click(await screen.findByRole("button", { name: /cite selection/i }));
  fireEvent.click(await screen.findByRole("button", { name: /Smith/ }));
  const pre = await screen.findByText("the azure sky");
  vi.spyOn(window, "getSelection").mockReturnValue({
    rangeCount: 1,
    getRangeAt: () => ({ startOffset: 4, endOffset: 9, startContainer: pre.firstChild, endContainer: pre.firstChild, toString: () => "azure" }),
    toString: () => "azure",
  } as never);
  fireEvent.mouseUp(pre);
  fireEvent.click(await screen.findByRole("button", { name: /add citation/i }));
  await waitFor(() => expect(post).toHaveBeenCalledOnce());
  const body = (post.mock.calls[0][1] as { body: Record<string, unknown> }).body;
  expect(body).toMatchObject({ reference_id: "r1", evidence_quote: "azure", anchor: "body." });
});
```

(NOTE: `draftPayload(true)`'s section 1 text is `"## Methods\nbody.\n"`; selecting `"body"` and the `.` makes the anchor `"body."` — confirm against the helper and adjust the indices/anchor assertion if its text differs.)

- [ ] **Step 2: Run to verify it fails**

Run: `export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b/frontend && nvm use >/dev/null && pnpm exec vitest run src/routes/Cockpit.test.tsx`
Expected: FAIL (no "Cite selection" control).

- [ ] **Step 3: Implement the wiring**

In `frontend/src/routes/Cockpit.tsx`:

1. Imports:
```tsx
import { CitationComposer } from "../cockpit/CitationComposer";
```

2. State (after the existing `pinnedEv` state):
```tsx
  // SP2a-4b select-to-cite: the editor's current selection (claim span) + whether
  // the composer is open. The Cite control is gated on a SAVED (non-dirty) section
  // because the anchor must exist in the on-disk proposal.md.
  const [selection, setSelection] = useState<{ from: number; to: number; text: string } | null>(null);
  const [composerOpen, setComposerOpen] = useState(false);
```

3. Pass `onSelectionChange` to `<CodeMirrorEditor>` (next to `onPillClick`):
```tsx
          onSelectionChange={tab.kind === "section" ? setSelection : undefined}
```

4. Add the Cite control next to the Save button (inside the `can_edit && tab.kind === "section"` block), and clear selection/composer in `selectSection` (add `setSelection(null); setComposerOpen(false);` alongside the existing resets):
```tsx
        {can_edit && tab.kind === "section" && (
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
```

5. Render the composer pane (after the `cockpit-pinned` block), passing the active section's base shas as the claim context:
```tsx
        {composerOpen && selection && activeSection && (
          <section className="cockpit-composer" aria-label="Add a citation">
            <CitationComposer
              workspaceId={id}
              claim={{
                section_index: activeSection.index,
                base_commit_sha: head,
                base_section_sha: activeSection.sha,
                anchor: selection.text,
              }}
              onDone={() => {
                setComposerOpen(false);
                setSelection(null);
                void draft.refetch();
              }}
              onCancel={() => setComposerOpen(false)}
            />
          </section>
        )}
```

- [ ] **Step 4: Add styles**

Append to `frontend/src/styles/cockpit.css`:

```css
.cite-selection-btn {
  margin-left: 0.5rem;
}
.cockpit-composer {
  min-width: 20rem;
  max-width: 30rem;
}
.cite-composer {
  border: 1px solid var(--cm-border);
  border-radius: 8px;
  background: var(--cm-surface);
  box-shadow: var(--cm-shadow-sm);
  padding: 0.75rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.cite-composer-head {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
}
.cite-composer-head button {
  margin-left: auto;
}
.cite-composer-claim {
  color: var(--cm-muted);
  font-style: italic;
}
.cite-composer-chunk {
  white-space: pre-wrap;
  border: 1px solid var(--cm-border);
  border-radius: 4px;
  padding: 0.4rem;
  font-size: 0.9em;
  cursor: text;
  max-height: 40vh;
  overflow: auto;
}
.cite-composer-confirm {
  background: var(--cm-primary-soft);
  border: 1px solid var(--cm-primary-border);
  border-radius: 4px;
  padding: 0.4rem;
}
```

- [ ] **Step 5: Run to verify it passes**

Run: `export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b/frontend && nvm use >/dev/null && pnpm exec vitest run src/routes/Cockpit.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS (all existing + the new cite test); tsc clean.

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b
git add frontend/src/routes/Cockpit.tsx frontend/src/routes/Cockpit.test.tsx frontend/src/styles/cockpit.css
git commit -m "feat(spa): wire select-to-cite into the cockpit (Cite selection + composer)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 10: Full verification

- [ ] **Step 1: Backend (isolation)**

Run:
```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b
uv run pytest tests/proposal_assist/citations/test_human_cite.py tests/test_api_workspaces.py -q
uv run ruff check src/call_manager/proposal_assist/citations/human_cite.py src/call_manager/web/api/workspaces.py tests/proposal_assist/citations/test_human_cite.py tests/test_api_workspaces.py
```
Expected: all pass; `All checks passed!` (re-run a `connection refused` in isolation).

- [ ] **Step 2: Frontend + tsc**

Run:
```bash
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b/frontend
nvm use >/dev/null
pnpm exec tsc --noEmit && pnpm exec vitest run
```
Expected: tsc clean; all suites pass.

- [ ] **Step 3: Schema-drift gate**

Run:
```bash
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b/frontend
nvm use >/dev/null
EMBEDDING_MODEL=bge-m3 pnpm gen:api
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-4b
git diff --exit-code frontend/src/api/schema.gen.ts && echo "NO DRIFT"
```
Expected: `NO DRIFT`.

- [ ] **Step 4: Deferred note**

The Playwright e2e in `frontend/e2e/cockpit.spec.ts` stays `test.skip` (needs a running backend + seeded fixture). The select-to-cite flow is covered by component + endpoint tests; a live e2e is deferred to the SP2a e2e hardening pass.

---

## Out of scope (later)

Quote-optional / chunk-level "cite whole section"; source-first "cite at cursor"; the discuss/comment toolbar actions; the AI propose→accept citation review path; making the 4a peek prefer the ledger's stored offsets.
