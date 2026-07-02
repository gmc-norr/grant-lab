# Collab Slice 3a — Backend Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close all seven known correctness gaps that only manifest with the collab write flag ON, entirely server-side, so Slice 3b can flip the flag safely.

**Architecture:** Three components over the existing `collab/` modules and the proposal read/propose/staleness paths: (1) a centralized read-only `canonical_artifact_markdown` helper routed through the three consumers that compare a captured base against a recomputed live section SHA; (2) a section-scoped Yjs fragment replace so kernel accepts stop clobbering concurrent edits; (3) five reconciliation-robustness fixes on `room.py` / `permissions.py` / `collab_ws.py` / `kernel_accept.py`. No schema, no migration, no frontend, flag stays OFF.

**Tech Stack:** Python 3.12, `uv`, `ruff`, `pytest` (+ testcontainers Postgres), pycrdt, FastAPI. Design spec: `docs/superpowers/specs/2026-07-02-collab-slice3a-backend-hardening-design.md`.

## Global Constraints

- **The collab flag STAYS OFF in prod through Slice 3a.** Every new behavior is reachable only when `is_collab_enabled(...)` is true, which prod never sets.
- **Flag-off parity is a hard gate.** `tests/proposal_assist/proposals/test_accepting.py`, `tests/test_api_spec_lock.py`, and the member/rubric/soft-delete suites MUST stay green **unchanged**. The flag-off proposal-read, staleness-sweep, citation-propose, and accept paths must be byte-for-byte identical after this slice.
- **No migration.** All changes are in-memory / logic; no DB schema touch.
- **No frontend.** All new behavior is exercised by server-side tests using the socket-free `Room`/`RoomManager` and kernel harnesses.
- Run pytest with `EMBEDDING_MODEL=bge-m3`; verify via grouped-dir runs (`tests/proposal_assist/collab/ tests/proposal_assist/ tests/test_api_spec_lock.py`), never the whole suite (testcontainers PG flake under load).
- CI runs `ruff check .` (NOT `ruff format`). Keep imports sorted (I001) and avoid nested-`with` (SIM117) — match each file's existing style.
- Collab tests live under `tests/proposal_assist/collab/`.

## File Structure

**New files:**
- `src/call_manager/proposal_assist/collab/canonical.py` — read-only room-canonical artifact projection + the shared empty-doc canonicalizer (extracted from `hydrate.py`).
- `tests/proposal_assist/collab/test_canonical.py` — canonical helper + divergence-guard tests.
- `tests/proposal_assist/collab/test_yjs_section_replace.py` — `apply_pm_section_to_fragment` unit tests.
- `tests/proposal_assist/collab/test_kernel_section_scoped.py` — concurrent-edit-preservation through the kernel.
- `tests/proposal_assist/collab/test_canonical_consumers.py` — the three C1 consumers behave under collab-on + flag-off parity.

**Modified files:**
- `collab/hydrate.py` — import the shared primitives from `canonical.py` (delete the local copies; re-export keeps its public surface).
- `collab/yjs_bridge.py` — add `apply_pm_section_to_fragment` + `_insert_node`.
- `collab/kernel.py` — prose + citation accept use the section-scoped apply.
- `tools/proposal.py` — `_read_artifact_impl` uses `canonical_artifact_markdown` when collab-on.
- `proposals/staleness.py` — `refresh_staleness` uses `canonical_artifact_markdown` per collab-on artifact.
- `tools/citations.py` — `propose_citation_impl` anchor probe uses `canonical_artifact_markdown` when collab-on.
- `collab/room.py` — `Room._recon_lock`; `release` stashes a pending on-evict; `operate()`-exit runs it.
- `collab/permissions.py` — `reconcile_client` helper; `refresh_permissions` serializes + fail-closed downgrade.
- `proposals/kernel_accept.py` — evict-blast-radius clarifying comment (behavior confirmed by test).
- `web/collab_ws.py` — self-reconcile the client after `acquire`.
- `tests/proposal_assist/collab/test_collab_permissions.py`, `test_collab_ws_route.py` — reconciliation tests.

---

## Task 1: Canonical artifact projection module

**Files:**
- Create: `src/call_manager/proposal_assist/collab/canonical.py`
- Modify: `src/call_manager/proposal_assist/collab/hydrate.py`
- Test: `tests/proposal_assist/collab/test_canonical.py`

**Interfaces:**
- Consumes: `store.load_updates(session, *, workspace_id, artifact) -> list[bytes]`; `markdown_to_pm(md)`, `pm_to_markdown(pm_json)`; `pm_json_to_ydoc(pm_json)`, `ydoc_to_pm_json(doc)`; `Artifact`, `filename_for`.
- Produces:
  - `markdown_to_pm_or_empty(md: str | None) -> dict` — `markdown_to_pm` but maps `None`/whitespace-only to the canonical empty doc.
  - `build_doc(updates: list[bytes]) -> pycrdt.Doc`
  - `read_head_markdown(repo_dir, artifact: str) -> str | None`
  - `canonical_artifact_markdown(session, *, workspace_id: uuid.UUID, artifact: str, repo_dir) -> str`
  - `_EMPTY_DOC` (module constant, shared with hydrate).

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/collab/test_canonical.py`:

```python
import uuid

from call_manager.proposal_assist.collab import canonical, store
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from call_manager.proposal_assist.artifacts import Artifact, filename_for
from tests.proposal_assist.conftest import make_user, make_workspace


def _seed_log(pg_session_factory, wid, artifact, md):
    """Persist a single Yjs update reconstructing `md` into the durable log."""
    seed = pm_json_to_ydoc(markdown_to_pm(md)).get_update()
    with pg_session_factory() as s:
        store.append_update(s, workspace_id=wid, artifact=artifact, actor_id=None, update=seed)
        s.commit()


def test_canonical_from_nonempty_log(pg_session_factory):
    md = "## A\n\nBody a.\n"
    # A real workspace row first — yjs_updates.workspace_id is an FK to workspaces.
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="canon-log@ki.se"))
        s.commit()
        wid = ws.id
    _seed_log(pg_session_factory, wid, "draft", md)
    with pg_session_factory() as s:
        out = canonical.canonical_artifact_markdown(
            s, workspace_id=wid, artifact="draft", repo_dir="/nonexistent"
        )
    # Round-trips to the normalized form (log is canonical; repo_dir is ignored).
    assert "## A" in out and "Body a." in out


def test_canonical_empty_log_normalizes_head(pg_session_factory, tmp_path):
    wid = uuid.uuid4()
    (tmp_path / filename_for(Artifact.DRAFT)).write_text("## A\nBody a.\n")  # tight (no blank line)
    with pg_session_factory() as s:
        out = canonical.canonical_artifact_markdown(
            s, workspace_id=wid, artifact="draft", repo_dir=tmp_path
        )
    # Normalized round-trip inserts the blank line after the heading.
    assert out == pm_to_markdown(markdown_to_pm("## A\nBody a.\n"))


def test_canonical_empty_log_whitespace_head_no_crash(pg_session_factory, tmp_path):
    wid = uuid.uuid4()
    (tmp_path / filename_for(Artifact.DRAFT)).write_text("   \n")
    with pg_session_factory() as s:
        out = canonical.canonical_artifact_markdown(
            s, workspace_id=wid, artifact="draft", repo_dir=tmp_path
        )
    assert out == pm_to_markdown(canonical._EMPTY_DOC)  # no ValueError


def test_canonical_absent_file(pg_session_factory, tmp_path):
    wid = uuid.uuid4()
    with pg_session_factory() as s:
        out = canonical.canonical_artifact_markdown(
            s, workspace_id=wid, artifact="draft", repo_dir=tmp_path
        )
    assert out == pm_to_markdown(canonical._EMPTY_DOC)


def test_markdown_to_pm_or_empty_maps_blank_to_empty_doc():
    assert canonical.markdown_to_pm_or_empty(None) == canonical._EMPTY_DOC
    assert canonical.markdown_to_pm_or_empty("   \n") == canonical._EMPTY_DOC
    assert canonical.markdown_to_pm_or_empty("## A\n")["type"] == "doc"


def test_hydrate_shares_the_canonicalizer():
    # Divergence guard: hydrate must use the SAME empty-doc canonicalizer, else
    # the read projection and the hydration seed drift and the false-stale bug
    # returns on the whitespace-HEAD path.
    from call_manager.proposal_assist.collab import hydrate

    assert hydrate.markdown_to_pm_or_empty is canonical.markdown_to_pm_or_empty
    assert hydrate._EMPTY_DOC is canonical._EMPTY_DOC
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_canonical.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError` / `AttributeError: module ... has no attribute 'canonical_artifact_markdown'`.

- [ ] **Step 3: Create `canonical.py`**

```python
"""Read-only room-canonical artifact projection (Slice 3a).

The durable Yjs update log is the canonical committed state of a collab-enabled
artifact (every accepted edit persists before it is broadcast). This module
derives an artifact's NORMALIZED markdown WITHOUT touching the live in-memory
room and WITHOUT persisting anything — so every consumer that compares a captured
base against a recomputed live section SHA uses the same representation. Callers
gate on ``is_collab_enabled``; the flag-off paths keep reading raw git.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from pycrdt import Doc

from call_manager.proposal_assist.artifacts import Artifact, filename_for
from call_manager.proposal_assist.collab import store
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import ydoc_to_pm_json

_ARTIFACT = {"draft": Artifact.DRAFT, "spec": Artifact.SPEC}

# ProseMirror's ``doc`` node requires ``block+`` (at least one block child), so an
# empty / whitespace-only / absent HEAD markdown has no valid PM representation.
# Canonicalize it to a single empty paragraph — the same seed hydration uses
# (hydrate imports this constant + markdown_to_pm_or_empty so the two cannot drift).
_EMPTY_DOC = {"type": "doc", "content": [{"type": "paragraph"}]}


def markdown_to_pm_or_empty(md: str | None) -> dict:
    """``markdown_to_pm``, but map ``None`` / whitespace-only to ``_EMPTY_DOC``.
    Raw ``markdown_to_pm`` raises ``ValueError`` ("Invalid content for node doc")
    on blank input; this is the single canonicalizer shared with hydration."""
    if md is None or not md.strip():
        return _EMPTY_DOC
    return markdown_to_pm(md)


def build_doc(updates: list[bytes]) -> Doc:
    """Replay Yjs updates into a fresh Doc."""
    doc = Doc()
    for u in updates:
        doc.apply_update(u)
    return doc


def read_head_markdown(repo_dir, artifact: str) -> str | None:
    """The artifact's on-disk HEAD markdown, or ``None`` when the file is absent."""
    path = Path(repo_dir) / filename_for(_ARTIFACT[artifact])
    return path.read_text() if path.exists() else None


def canonical_artifact_markdown(
    session, *, workspace_id: uuid.UUID, artifact: str, repo_dir
) -> str:
    """Room-canonical (normalized) markdown for a collab-enabled artifact.

    Log non-empty -> serialize the durable log (canonical committed state).
    Log empty -> normalize HEAD through the SAME canonicalizer hydration uses
    (whitespace/absent -> empty doc). NEVER writes. Callers MUST gate on
    ``is_collab_enabled``; the flag-off path keeps reading raw git.
    """
    updates = store.load_updates(session, workspace_id=workspace_id, artifact=artifact)
    if updates:
        return pm_to_markdown(ydoc_to_pm_json(build_doc(updates)))
    md = read_head_markdown(repo_dir, artifact)
    return pm_to_markdown(markdown_to_pm_or_empty(md))
```

- [ ] **Step 4: Rewire `hydrate.py` to share the primitives**

In `src/call_manager/proposal_assist/collab/hydrate.py`, keep the module docstring, `from __future__ import annotations`, and `import uuid` (still used by `load_or_hydrate`'s `workspace_id: uuid.UUID` annotation) — this is a surgical edit, NOT a whole-file replace.

Delete hydrate's local `_EMPTY_DOC`, `build_doc`, `_markdown_to_pm`, `_read_head_markdown` (and `_ARTIFACT` if nothing else in hydrate uses it) definitions, and the now-unused imports they needed (`markdown_to_pm`, `Artifact`/`filename_for`, `Path` if no longer referenced). Add the shared imports (re-exporting keeps `hydrate.build_doc` etc. valid for any external importer — e.g. `test_collab_kernel_accept.py` imports `build_doc` from hydrate):

```python
from call_manager.proposal_assist.collab.canonical import (  # shared, single source
    _EMPTY_DOC,
    build_doc,
    markdown_to_pm_or_empty,
    read_head_markdown,
)
```

In `load_or_hydrate`, replace the two call sites:

```python
    md = read_head_markdown(workspace_repo_dir(workspaces_dir, workspace_id), artifact)
    if md is None:
        # Collab enabled but the artifact file has never been committed. Leave the
        # room empty (Slice-1 behavior) rather than persisting a synthetic seed.
        return []
    seed = pm_json_to_ydoc(markdown_to_pm_or_empty(md)).get_update()
```

Keep `hydrate._ARTIFACT` if hydrate still references it elsewhere; otherwise remove it (it now lives in `canonical`). Verify with a grep that no other `hydrate._markdown_to_pm` / `hydrate._read_head_markdown` reference remains:

Run: `grep -rn "_markdown_to_pm\|_read_head_markdown" src/ tests/`
Expected: no references outside `canonical.py`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_canonical.py tests/proposal_assist/collab/test_collab_hydrate.py -q -p no:cacheprovider`
Expected: PASS (new canonical tests + the existing hydrate suite still green after the rewire — the hydrate suite is `test_collab_hydrate.py`).

- [ ] **Step 6: Ruff + commit**

Run: `uv run ruff check src/call_manager/proposal_assist/collab/canonical.py src/call_manager/proposal_assist/collab/hydrate.py tests/proposal_assist/collab/test_canonical.py`
Expected: `All checks passed!`

```bash
git add src/call_manager/proposal_assist/collab/canonical.py src/call_manager/proposal_assist/collab/hydrate.py tests/proposal_assist/collab/test_canonical.py
git commit -m "feat(collab): canonical_artifact_markdown projection + share hydrate canonicalizer"
```

---

## Task 2: Route `_read_artifact_impl` through the canonical helper (C1 read consumer)

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/proposal.py` (`_read_artifact_impl`, ~line 47-92; the `content = path.read_text()` at line 68)
- Test: `tests/proposal_assist/collab/test_canonical_consumers.py`

**Interfaces:**
- Consumes: `canonical.canonical_artifact_markdown(...)`; `state.is_collab_enabled(session, *, workspace_id, artifact) -> bool`.
- Produces: no new symbols; `_read_artifact_impl` now returns room-canonical `content` when collab-on for the artifact.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/collab/test_canonical_consumers.py`. It drives the real agent read path via a `ToolContext`. `ToolContext`'s first four fields (`workspace_id`, `repo_dir`, `session_factory`, `settings`) have NO defaults, so pass `settings=Settings()` (the pattern in `tests/proposal_assist/tools/test_propose_edit.py`). There is no `make_tool_context`/tools-conftest helper — construct `ToolContext` directly. Seeding `yjs_updates`/`collab_artifact_state` requires a real `workspaces` row (FK), so use `make_workspace` for any collab-on/seeding test; the flag-off read test does no DB write and may use a bare uuid.

```python
import subprocess
import uuid
from pathlib import Path

import pytest

from call_manager.config import Settings
from call_manager.db.models import User
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc, ydoc_to_pm_json
from call_manager.proposal_assist.tools.proposal import _read_artifact_impl
from call_manager.proposal_assist.tools.registry import ToolContext
from call_manager.proposal_assist.artifacts import Artifact, filename_for
from call_manager.proposal_assist.collab.kernel import kernel_accept_prose
from call_manager.proposal_assist.editing.sectionizer import split_sections
from tests.proposal_assist.conftest import make_user, make_workspace
from tests.proposal_assist.collab.test_collab_kernel_accept import (
    _git_head, _init_repo, _live_room, _mk_pc,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _ctx(wid, repo_dir, pg_session_factory):
    return ToolContext(
        workspace_id=wid, repo_dir=repo_dir,
        session_factory=pg_session_factory, settings=Settings(),
    )


@pytest.mark.anyio
async def test_read_collab_on_returns_log_canonical(pg_session_factory, tmp_path):
    # Raw HEAD is TIGHT (no blank line after heading); the durable log holds the
    # normalized form. Collab-on read must return the normalized (log) content so
    # its section shas match the kernel's live.sha at accept.
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="canon-read@ki.se"))
        s.commit()
        wid = ws.id
    (tmp_path / filename_for(Artifact.DRAFT)).write_text("## A\nBody a.\n")
    norm = pm_to_markdown(markdown_to_pm("## A\nBody a.\n"))
    seed = pm_json_to_ydoc(markdown_to_pm(norm)).get_update()
    with pg_session_factory() as s:
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None, update=seed)
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()

    ctx = _ctx(wid, tmp_path, pg_session_factory)
    out = await _read_artifact_impl(
        ctx, filename=filename_for(Artifact.DRAFT),
        head_attr="agent_view_head", sections_attr="agent_view_sections",
        proposal_attr="agent_view_proposal",
    )
    assert out["content"] == norm
    # The captured base shas are the NORMALIZED section shas (match kernel live.sha).
    assert ctx.agent_view_sections == {sec.index: sec.sha for sec in split_sections(norm)}


@pytest.mark.anyio
async def test_read_flag_off_returns_raw_head(pg_session_factory, tmp_path):
    wid = uuid.uuid4()   # no DB write on the flag-off path → bare uuid is fine
    raw = "## A\nBody a.\n"  # tight — NOT normalized
    (tmp_path / filename_for(Artifact.DRAFT)).write_text(raw)
    ctx = _ctx(wid, tmp_path, pg_session_factory)   # collab NOT enabled
    out = await _read_artifact_impl(
        ctx, filename=filename_for(Artifact.DRAFT),
        head_attr="agent_view_head", sections_attr="agent_view_sections",
        proposal_attr="agent_view_proposal",
    )
    assert out["content"] == raw  # byte-for-byte raw HEAD (parity)


@pytest.mark.anyio
async def test_read_then_accept_end_to_end_not_false_stale(pg_session_factory, tmp_path):
    # THE end-to-end C1 regression (spec Testing / Component 1): a base captured via
    # the REAL read path (_read_artifact_impl, collab-on) is ACCEPTED by the kernel,
    # not rejected as stale — the exact false-stale bug this slice fixes. Uses a
    # tight raw HEAD so the un-fixed read (raw base) would be flagged stale.
    with pg_session_factory() as s:
        o = make_user(s, email="canon-e2e@ki.se")
        ws = make_workspace(s, owner=o, current_phase="active")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, "## A\nBody a.\n")           # tight (non-normalized) HEAD
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    with pg_session_factory() as s:
        room_doc = _live_room(s, wid, str(tmp_path))   # hydrates normalized log
    # Agent reads via the real tool path → captures the (normalized) base.
    ctx = _ctx(wid, repo, pg_session_factory)
    await _read_artifact_impl(
        ctx, filename=filename_for(Artifact.DRAFT),
        head_attr="agent_view_head", sections_attr="agent_view_sections",
        proposal_attr="agent_view_proposal",
    )
    base_sha = ctx.agent_view_sections[1]          # section 1 = "## A"
    with pg_session_factory() as s:
        pc_id = _mk_pc(s, wid, section_index=1, new_text="## A\n\nAccepted body.\n",
                       base_commit_sha=_git_head(repo), base_section_sha=base_sha)
    with pg_session_factory() as s:
        res = kernel_accept_prose(s, room_doc=room_doc, proposed_change_id=pc_id,
                                  workspace_id=wid, approver=s.get(User, oid),
                                  workspaces_dir=str(tmp_path))
        s.commit()
        assert res.pc.status == "accepted"        # NOT "stale"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_canonical_consumers.py -k "collab_on or end_to_end" -q -p no:cacheprovider`
Expected: FAIL — `test_read_collab_on_returns_log_canonical` asserts normalized content but the impl returns raw HEAD; `test_read_then_accept_end_to_end_not_false_stale` gets a raw base that the kernel rejects as `stale` (status != "accepted"). (`test_read_flag_off_returns_raw_head` already passes — parity.)

- [ ] **Step 3: Modify `_read_artifact_impl`**

In `tools/proposal.py`, add imports near the top:

```python
from call_manager.proposal_assist.collab import canonical
from call_manager.proposal_assist.collab.state import is_collab_enabled
```

Replace the function body from the `path = ctx.repo_dir / filename` line through the `content = path.read_text()` line (the existence guard, the HEAD snapshot, and the content read) with the block below. The change: gate the disk-existence guard to the **flag-off** branch only (collab-on, the durable log is canonical — a non-empty log may have no checkpointed disk file yet, hydrate.py:57-61; matching Task 4's handling), and read canonical content when collab-on:

```python
    artifact = "spec" if filename == SPEC_FILENAME else "draft"
    path = ctx.repo_dir / filename
    with ctx.session_factory() as _s:
        _collab = is_collab_enabled(_s, workspace_id=ctx.workspace_id, artifact=artifact)
        # Flag-off: disk is authoritative — keep the existence guard. Collab-on:
        # the durable log is canonical, so don't gate on the on-disk file.
        if not _collab and not path.exists():
            return {"error": f"{filename} does not exist in this workspace"}
        # HEAD snapshot stays (concurrent-save detection on the flag-off path; moot
        # on the collab path, which keys staleness on section SHA). Resolve HEAD
        # BEFORE reading content (fail-safe ordering invariant).
        try:
            setattr(ctx, head_attr, current_head(ctx.repo_dir))
        except GitCommitError:
            setattr(ctx, head_attr, None)
        if _collab:
            # Room-canonical (normalized) content so the captured base section shas
            # match the kernel's accept-time live.sha (Slice 3a C1).
            content = canonical.canonical_artifact_markdown(
                _s, workspace_id=ctx.workspace_id, artifact=artifact, repo_dir=ctx.repo_dir
            )
        else:
            content = path.read_text()   # raw HEAD, byte-for-byte (parity)
```

The `sections_attr` / `proposal_attr` `setattr`s below already build from `content`, so they now flow from the canonical text when collab-on. Confirm `SPEC_FILENAME`, `current_head`, `GitCommitError`, and `split_sections` are imported in this module (they are — used elsewhere in the file).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_canonical_consumers.py -q -p no:cacheprovider`
Expected: PASS (both collab-on and flag-off).

- [ ] **Step 5: Ruff + commit**

```bash
uv run ruff check src/call_manager/proposal_assist/tools/proposal.py tests/proposal_assist/collab/test_canonical_consumers.py
git add src/call_manager/proposal_assist/tools/proposal.py tests/proposal_assist/collab/test_canonical_consumers.py
git commit -m "feat(collab): read_proposal snapshots room-canonical base when collab-on (C1 read)"
```

---

## Task 3: Route `refresh_staleness` through the canonical helper (C1 sweep consumer, Codex #1)

**Files:**
- Modify: `src/call_manager/proposal_assist/proposals/staleness.py` (`refresh_staleness`, lines 20-58; the per-artifact live-section build at lines 27-32)
- Test: `tests/proposal_assist/collab/test_canonical_consumers.py` (add cases)

**Interfaces:**
- Consumes: `canonical.canonical_artifact_markdown(...)`, `is_collab_enabled(...)`, `split_sections`, `workspace_repo_dir`.
- Produces: no new symbols; the proactive sweep uses canonical live sections for collab-on artifacts.

- [ ] **Step 1: Write the failing test**

Add to `tests/proposal_assist/collab/test_canonical_consumers.py`:

```python
from call_manager.db.models import ProposedChange
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.proposals.staleness import refresh_staleness


def _seed_ws_with_proposal(pg_session_factory, tmp_path, *, collab, base_from):
    """Workspace + a pending prose proposal on section 1 (the '## A' section — its
    sha DIFFERS raw vs normalized; section 0 the empty-ish preamble would be
    identical and the test vacuous). Writes tight raw HEAD to the real workspace
    repo dir (<tmp_path>/<wid>/repo); seeds the normalized log when collab=True."""
    raw = "Preamble.\n\n## A\nBody a.\n"           # tight: no blank line after '##'
    norm = pm_to_markdown(markdown_to_pm(raw))
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email=f"stale-{uuid.uuid4().hex[:8]}@ki.se"))
        s.commit()
        wid = ws.id
    repo_dir = workspace_repo_dir(str(tmp_path), wid)
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / filename_for(Artifact.DRAFT)).write_text(raw)
    base_md = norm if base_from == "norm" else raw
    base_sha = split_sections(base_md)[1].sha        # section 1 = "## A"
    with pg_session_factory() as s:
        if collab:
            seed = pm_json_to_ydoc(markdown_to_pm(norm)).get_update()
            store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None, update=seed)
            state.enable_collab(s, workspace_id=wid, artifact="draft")
        pc = ProposedChange(
            workspace_id=wid, target_artifact="draft", section_index=1,
            base_section_sha=base_sha, base_commit_sha="0" * 40,  # NOT NULL; sweep never reads it
            origin_kind="human",                                  # NOT NULL
            status="pending", kind="prose_edit", payload={"new_text": "## A\n\nEdited.\n"},
        )
        s.add(pc); s.commit()
        return wid, pc.id


def test_sweep_collab_on_does_not_false_flag_normalized_base(pg_session_factory, tmp_path):
    # Collab-on, base captured from the NORMALIZED log form → the sweep must NOT
    # flip it stale (raw HEAD differs from normalized, which is the #1 bug).
    ws_id, pc_id = _seed_ws_with_proposal(pg_session_factory, tmp_path, collab=True, base_from="norm")
    with pg_session_factory() as s:
        refresh_staleness(session=s, workspace_id=ws_id, workspaces_dir=str(tmp_path))
        s.commit()
    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id).status == "pending"


def test_sweep_flag_off_uses_raw_head(pg_session_factory, tmp_path):
    # Flag-off, base captured from RAW head → matches raw sweep → stays pending
    # (proves the sweep still reads raw git when collab is off; parity).
    ws_id, pc_id = _seed_ws_with_proposal(pg_session_factory, tmp_path, collab=False, base_from="raw")
    with pg_session_factory() as s:
        refresh_staleness(session=s, workspace_id=ws_id, workspaces_dir=str(tmp_path))
        s.commit()
    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id).status == "pending"
```

(`refresh_staleness` reads `workspace_repo_dir(workspaces_dir, ws_id) / filename_for(artifact)`; the fixture writes the raw HEAD to exactly that path with `workspaces_dir=str(tmp_path)`, so the flag-off branch's `path.exists()` is true.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_canonical_consumers.py -k sweep -q -p no:cacheprovider`
Expected: FAIL — `test_sweep_collab_on_does_not_false_flag_normalized_base` flips the row to `stale` (raw-git live sha != normalized base).

- [ ] **Step 3: Modify `refresh_staleness`**

In `proposals/staleness.py`, add imports:

```python
from call_manager.proposal_assist.collab import canonical
from call_manager.proposal_assist.collab.state import is_collab_enabled
```

Replace the per-artifact live-section build (lines 27-32) so a collab-enabled artifact's live sections come from the canonical markdown; flag-off stays raw:

```python
    live: dict[str, dict[int, str]] = {}
    for artifact in (Artifact.DRAFT, Artifact.SPEC):
        art = str(artifact)  # "draft" / "spec"
        if is_collab_enabled(session, workspace_id=workspace_id, artifact=art):
            # Collab-on: compare against the ROOM-CANONICAL live sections (Slice 3a
            # C1) so a normalized base is not false-flagged stale before accept.
            md = canonical.canonical_artifact_markdown(
                session, workspace_id=workspace_id, artifact=art, repo_dir=repo_dir
            )
            live[art] = {i: sec.sha for i, sec in enumerate(split_sections(md))}
        else:
            path = repo_dir / filename_for(artifact)
            if path.exists():
                live[art] = {
                    i: sec.sha for i, sec in enumerate(split_sections(path.read_text()))
                }
```

The downstream loop keys on `pc.target_artifact` (already `"draft"`/`"spec"`), which matches `str(artifact)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_canonical_consumers.py -k sweep -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Parity check + ruff + commit**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/proposals/ -q -p no:cacheprovider`
Expected: PASS unchanged (flag-off staleness parity).

```bash
uv run ruff check src/call_manager/proposal_assist/proposals/staleness.py tests/proposal_assist/collab/test_canonical_consumers.py
git add src/call_manager/proposal_assist/proposals/staleness.py tests/proposal_assist/collab/test_canonical_consumers.py
git commit -m "fix(collab): refresh_staleness uses room-canonical live sections when collab-on (C1 sweep)"
```

---

## Task 4: Route `propose_citation_impl` anchor probe through the canonical helper (C1 citation consumer, Codex #3)

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/citations.py` (`propose_citation_impl`, the step-3 read at lines 485-488)
- Test: `tests/proposal_assist/collab/test_canonical_consumers.py` (add a case)

**Interfaces:**
- Consumes: `canonical.canonical_artifact_markdown(...)`, `is_collab_enabled(...)`.
- Produces: no new symbols; the anchor-uniqueness probe reads log-canonical proposal text when collab-on.

- [ ] **Step 1: Write the failing test**

Add two tests to `tests/proposal_assist/collab/test_canonical_consumers.py`, reusing the citation-propose seeding already in `tests/proposal_assist/tools/test_propose_citation.py` (import `ANCHOR`, `CHUNK_TEXT`, and the `_seed_reference` helper from it; `_seed_reference(pg_session_factory, workspace_id, *, chunks=[CHUNK_TEXT])` creates the `Reference` + `ReferenceChunk` and returns the reference id + chunk index — confirm its exact return shape and pass those through). Construct the divergence: write a disk `proposal.md` that LACKS the anchor (via `_init_repo(repo, "preamble\n\n## Methods\nNo citable anchor on disk.\n")`), but seed the durable log with the anchor-BEARING text (`store.append_update` of `pm_json_to_ydoc(markdown_to_pm("preamble\n\n## Methods\nWe argue that " + ANCHOR + " in our population.\n")).get_update()`), then `enable_collab`.

Both tests call `read_proposal_impl(ctx)` first (propose requires the agent to have read — it reads `ctx.agent_view_proposal`), then `propose_citation_impl(ctx, reference_id=<seeded ref id, str>, anchor=ANCHOR, chunk_index=<seeded chunk index>, evidence_quote=CHUNK_TEXT[:40])`, using the shared `_ctx(wid, repo, pg_session_factory)` helper.

- **collab-on** (`enable_collab` called): the anchor probe reads the log-canonical text (which HAS the anchor) → `assert "error" not in result` (a pending `proposed_change` is created).
- **flag-off** (no `enable_collab`): the probe reads raw disk (which LACKS the anchor) → `assert "anchor not found" in result["error"]` (parity — disk stays authoritative).

- [ ] **Step 2: Run the test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_canonical_consumers.py -k citation -q -p no:cacheprovider`
Expected: FAIL — collab-on probe reads raw disk (line 488) and returns "anchor not found".

- [ ] **Step 3: Modify the step-3 read in `propose_citation_impl`**

In `tools/citations.py`, ensure the imports exist:

```python
from call_manager.proposal_assist.collab import canonical
from call_manager.proposal_assist.collab.state import is_collab_enabled
```

Replace the step-3 block (lines 485-488) so the anchor probe reads canonical text when collab-on:

```python
        # 3. Read the live proposal for the anchor-uniqueness probe. Collab-on:
        # read the ROOM-CANONICAL text (Slice 3a C1) so an anchor present in the
        # durable log but not yet checkpointed to disk is not false-rejected.
        # Flag-off: raw disk proposal.md, byte-for-byte as before (parity).
        if is_collab_enabled(session, workspace_id=ctx.workspace_id, artifact="draft"):
            proposal_text = canonical.canonical_artifact_markdown(
                session, workspace_id=ctx.workspace_id, artifact="draft", repo_dir=ctx.repo_dir
            )
        else:
            proposal_path = ctx.repo_dir / PROPOSAL_FILENAME
            if not proposal_path.exists():
                return {"error": f"{PROPOSAL_FILENAME} does not exist in this workspace"}
            proposal_text = proposal_path.read_text()
```

Leave step 6's section keying against `ctx.agent_view_proposal` unchanged — Task 2 already makes that snapshot canonical when collab-on, so the anchor-probe text (this step) and the keying text (the read snapshot) are consistent representations.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_canonical_consumers.py -k citation -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Parity check + ruff + commit**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/tools/ -q -p no:cacheprovider`
Expected: PASS unchanged (flag-off citation-propose parity).

```bash
uv run ruff check src/call_manager/proposal_assist/tools/citations.py tests/proposal_assist/collab/test_canonical_consumers.py
git add src/call_manager/proposal_assist/tools/citations.py tests/proposal_assist/collab/test_canonical_consumers.py
git commit -m "fix(collab): citation anchor probe reads room-canonical text when collab-on (C1 citation)"
```

---

## Task 5: Section-scoped fragment replace primitive

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/yjs_bridge.py` (add `apply_pm_section_to_fragment` + `_insert_node` near `apply_pm_json_to_fragment`, line 225)
- Test: `tests/proposal_assist/collab/test_yjs_section_replace.py`

**Interfaces:**
- Consumes: `pycrdt.Doc`/`XmlFragment`/`XmlElement`; `_append_node`, `_append_inline_content`, `_block_holds_inline`, `_node_attrs_for_yjs`, `_FRAGMENT_KEY`, `pm_to_markdown`, `ydoc_to_pm_json` (existing module symbols); `is_h2_heading_line` (from `editing.sectionizer` — the split_sections boundary predicate).
- Produces:
  - `apply_pm_section_to_fragment(doc: pycrdt.Doc, *, section_index: int, blocks: list[dict]) -> bytes` — replace only `section_index`'s top-level block range with `blocks`; return the incremental Yjs update, or `b""` if nothing changed.
  - `_insert_node(parent, index: int, node: dict) -> pycrdt.XmlElement` — `_append_node`'s positional sibling.
  - `_block_starts_section(block_json: dict) -> bool` — split_sections-rule boundary test (private).

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/collab/test_yjs_section_replace.py`:

```python
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import (
    apply_pm_section_to_fragment,
    pm_json_to_ydoc,
    ydoc_to_pm_json,
)
from call_manager.proposal_assist.editing.sectionizer import split_sections

_DOC = "Preamble.\n\n## A\n\nBody a.\n\n## B\n\nBody b.\n"


def _blocks_of(section_md):
    return markdown_to_pm(section_md).get("content") or []


def test_replace_middle_section_only():
    doc = pm_json_to_ydoc(markdown_to_pm(_DOC))
    # Replace section 1 ("## A ...") with new content.
    new_md = "## A\n\nBody a EDITED.\n"
    update = apply_pm_section_to_fragment(doc, section_index=1, blocks=_blocks_of(new_md))
    assert update  # non-empty incremental update
    out = pm_to_markdown(ydoc_to_pm_json(doc))
    secs = split_sections(out)
    assert "Body a EDITED." in secs[1].text
    assert "Preamble." in secs[0].text          # preamble untouched
    assert "Body b." in secs[2].text            # section B untouched


def test_replace_preamble_section_zero():
    doc = pm_json_to_ydoc(markdown_to_pm(_DOC))
    update = apply_pm_section_to_fragment(doc, section_index=0, blocks=_blocks_of("New preamble.\n"))
    assert update
    secs = split_sections(pm_to_markdown(ydoc_to_pm_json(doc)))
    assert "New preamble." in secs[0].text
    assert secs[1].heading == "A" and secs[2].heading == "B"


def test_replace_last_section():
    doc = pm_json_to_ydoc(markdown_to_pm(_DOC))
    update = apply_pm_section_to_fragment(doc, section_index=2, blocks=_blocks_of("## B\n\nB2.\n"))
    assert update
    secs = split_sections(pm_to_markdown(ydoc_to_pm_json(doc)))
    assert "B2." in secs[2].text
    assert "Body a." in secs[1].text


def test_replace_with_different_block_count():
    doc = pm_json_to_ydoc(markdown_to_pm(_DOC))
    # Section A grows from 1 body paragraph to 2.
    update = apply_pm_section_to_fragment(
        doc, section_index=1, blocks=_blocks_of("## A\n\nP1.\n\nP2.\n")
    )
    assert update
    secs = split_sections(pm_to_markdown(ydoc_to_pm_json(doc)))
    assert "P1." in secs[1].text and "P2." in secs[1].text
    assert "Body b." in secs[2].text            # B still intact after a size change


# --- Regression tests for the boundary rule (these caught real defects) ---

_NO_PREAMBLE = "## A\n\nBody a.\n\n## B\n\nBody b.\n"


def test_no_preamble_doc_section_index_alignment():
    # split_sections ALWAYS emits Section 0 = preamble (possibly empty), so for a
    # doc starting with '## A', section 1 = A. The boundary map MUST match: editing
    # section_index=1 replaces A, NOT B. (Regression: a preamble-collapse would
    # silently splice B — data loss.)
    doc = pm_json_to_ydoc(markdown_to_pm(_NO_PREAMBLE))
    update = apply_pm_section_to_fragment(doc, section_index=1, blocks=_blocks_of("## A\n\nA EDITED.\n"))
    assert update
    out = pm_to_markdown(ydoc_to_pm_json(doc))
    secs = split_sections(out)
    assert secs[1].heading == "A" and "A EDITED." in secs[1].text
    assert secs[2].heading == "B" and "Body b." in secs[2].text   # B untouched
    # And section_index=2 (valid last section) does not raise + targets B.
    doc2 = pm_json_to_ydoc(markdown_to_pm(_NO_PREAMBLE))
    apply_pm_section_to_fragment(doc2, section_index=2, blocks=_blocks_of("## B\n\nB EDITED.\n"))
    assert "B EDITED." in split_sections(pm_to_markdown(ydoc_to_pm_json(doc2)))[2].text


def test_hash_hash_nospace_paragraph_is_a_boundary_like_split_sections():
    # '##NoSpace' is a split_sections boundary (_H2_RE) but a PARAGRAPH in PM, and
    # pm_to_markdown round-trips it unescaped — so the boundary rule must count it
    # (via is_h2_heading_line on the serialized line), matching split_sections.
    md = "Intro.\n\n##NoSpace para\n\n## A\n\nBody.\n"
    doc = pm_json_to_ydoc(markdown_to_pm(md))
    secs_before = split_sections(pm_to_markdown(ydoc_to_pm_json(doc)))
    assert len(secs_before) == 3   # preamble, ##NoSpace, A
    # Editing section 2 (the real '## A') must target A, not run off the end.
    update = apply_pm_section_to_fragment(doc, section_index=2, blocks=_blocks_of("## A\n\nA EDITED.\n"))
    assert update
    secs = split_sections(pm_to_markdown(ydoc_to_pm_json(doc)))
    assert "A EDITED." in secs[2].text
    assert "##NoSpace para" in secs[1].text   # the phantom-boundary section preserved


def test_clear_section_to_empty_blocks():
    # Empty blocks (clearing a section) delete the range and insert nothing; the
    # kernel maps an empty new_text to blocks=[] (see Task 6). Non-final preamble
    # clear keeps the doc valid because other sections remain.
    doc = pm_json_to_ydoc(markdown_to_pm(_DOC))
    update = apply_pm_section_to_fragment(doc, section_index=0, blocks=[])
    assert update
    secs = split_sections(pm_to_markdown(ydoc_to_pm_json(doc)))
    assert secs[0].heading == "A"   # preamble gone → first section is now A
    assert "Body b." in secs[-1].text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_yjs_section_replace.py -q -p no:cacheprovider`
Expected: FAIL with `ImportError: cannot import name 'apply_pm_section_to_fragment'`.

- [ ] **Step 3: Implement `_insert_node` + `apply_pm_section_to_fragment`**

In `yjs_bridge.py`, after `_append_node` add the positional variant (mirrors `_append_node` but inserts; the inserted element is integrated, so its children populate the same way):

```python
def _insert_node(
    parent: pycrdt.XmlFragment | pycrdt.XmlElement, index: int, node: dict[str, Any]
) -> pycrdt.XmlElement:
    """Insert PM ``node`` as a child of ``parent`` at ``index`` (positional sibling
    of ``_append_node``). ``children.insert`` integrates the element, so its own
    children can then be populated identically to the append path."""
    node_type = node["type"]
    attrs = _node_attrs_for_yjs(node_type, node.get("attrs") or {})
    elem = parent.children.insert(index, pycrdt.XmlElement(node_type, dict(attrs)))
    content = node.get("content") or []
    if content and _block_holds_inline(node_type, content):
        _append_inline_content(elem, content)
    else:
        for child in content:
            _append_node(elem, child)
    return elem
```

Then the section-scoped apply. It maps `section_index` to a top-level block range using **exactly the `split_sections` boundary rule** (spec requirement): `is_h2_heading_line` applied to each block's serialized first line — NOT a node-type check. A node-type (`heading`/level-2) check diverges on a `##NoSpace`-style paragraph, which `_H2_RE` treats as a section boundary and `pm_to_markdown` round-trips **unescaped** (`_INLINE_ESCAPE` never escapes `#`), so `split_sections` counts it while a tag check would not → wrong-range splice (verified empirically). Serializing each block and reusing `is_h2_heading_line` (the public `_H2_RE` wrapper) is fence-safe (a fenced `##` lives inside a single `code_block` whose first line is a fence) and matches `split_sections` on every edge (preamble, no-preamble, `##NoSpace`, empty).

```python
from call_manager.proposal_assist.editing.sectionizer import is_h2_heading_line


def _block_starts_section(block_json: dict[str, Any]) -> bool:
    """True if this top-level block begins a section, using the SAME rule as
    split_sections: is_h2_heading_line on the block's serialized first line.
    A node-type (heading level-2) check would diverge on `##NoSpace` paragraphs."""
    first_line = pm_to_markdown({"type": "doc", "content": [block_json]}).split("\n", 1)[0]
    return is_h2_heading_line(first_line)
```

Then (note `_block_starts_section` needs the PM-JSON blocks, so read them once via `ydoc_to_pm_json`; the fragment children are index-aligned with those blocks):

```python
def apply_pm_section_to_fragment(
    doc: pycrdt.Doc, *, section_index: int, blocks: list[dict[str, Any]]
) -> bytes:
    """Replace ONLY ``section_index``'s top-level block range in the ``prosemirror``
    XmlFragment with ``blocks`` (the new section's PM blocks), preserving every
    other section's Yjs identity + any concurrent client edits. Returns the
    incremental Yjs update (captured via observer), or ``b""`` if nothing changed.
    Safe because the kernel's staleness gate proved the target section unchanged
    since the proposer read it.
    """
    captured: list[bytes] = []
    sub = doc.observe(lambda event: captured.append(event.update))
    try:
        fragment = doc.get(_FRAGMENT_KEY, type=pycrdt.XmlFragment)
        blocks_json = ydoc_to_pm_json(doc).get("content") or []
        # Section boundaries via the SAME rule as split_sections. The preamble
        # (section 0) is ALWAYS present — split_sections emits Section 0 even when
        # empty — so section k spans [starts[k], starts[k+1]). NO dedupe: a
        # no-preamble doc yields starts=[0, 0, ...] so section 0 is the empty range
        # [0, 0) and every index aligns with the split-numbered pc.section_index the
        # kernel passes (dropping the leading 0 shifts every section — silent
        # wrong-section corruption).
        starts = [0] + [i for i, b in enumerate(blocks_json) if _block_starts_section(b)]
        if not 0 <= section_index < len(starts):
            raise IndexError(
                f"section_index {section_index} out of range ({len(starts)} sections)"
            )
        start = starts[section_index]
        end = starts[section_index + 1] if section_index + 1 < len(starts) else len(blocks_json)
        with doc.transaction():
            for i in range(end - 1, start - 1, -1):   # delete range back-to-front
                del fragment.children[i]
            for offset, block in enumerate(blocks):    # insert new blocks at `start`
                _insert_node(fragment, start + offset, block)
    finally:
        doc.unobserve(sub)
    return captured[-1] if captured else b""
```

The three new symbols are `_block_starts_section`, `_insert_node`, and `apply_pm_section_to_fragment` — no other helpers are added. (`is_h2_heading_line`, `pm_to_markdown`, and `ydoc_to_pm_json` are imported at module top; `pm_to_markdown`/`ydoc_to_pm_json` already are — add `is_h2_heading_line`.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_yjs_section_replace.py -q -p no:cacheprovider`
Expected: PASS (all seven cases, incl. the no-preamble alignment, `##NoSpace` boundary, and clear-to-empty regressions).

- [ ] **Step 5: Ruff + commit**

```bash
uv run ruff check src/call_manager/proposal_assist/collab/yjs_bridge.py tests/proposal_assist/collab/test_yjs_section_replace.py
git add src/call_manager/proposal_assist/collab/yjs_bridge.py tests/proposal_assist/collab/test_yjs_section_replace.py
git commit -m "feat(collab): apply_pm_section_to_fragment — section-scoped Yjs replace"
```

---

## Task 6: Kernel accepts use the section-scoped apply (Component 2)

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/kernel.py` (imports line 22; prose apply lines 124-127; citation apply line 236)
- Test: `tests/proposal_assist/collab/test_kernel_section_scoped.py`

**Interfaces:**
- Consumes: `apply_pm_section_to_fragment(doc, *, section_index, blocks)` (Task 5); `markdown_to_pm`, `split_sections`, `workspace_repo_dir`, `_head_or_empty` (all already imported in `kernel.py`).
- Produces: no new symbols; both kernel accepts now mutate only the target section's fragment range.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/collab/test_kernel_section_scoped.py`. The flagship test is a **fork race** (a plain pre-applied edit does NOT reproduce C2 — the kernel re-reads live sections and re-includes it; the real race is a client update generated against the pre-accept state and merged AFTER the accept). Reuse the kernel-accept harness helpers from `tests/proposal_assist/collab/test_collab_kernel_accept.py`: `_init_repo(repo, content)`, `_live_room(session, wid, workspaces_dir)`, `_git_head(repo)`, `_mk_pc(session, wid, *, section_index, new_text, base_commit_sha, base_section_sha)`, and `make_user`/`make_workspace` from `tests/proposal_assist/conftest.py`.

```python
import subprocess
from pathlib import Path

import pycrdt
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.collab import state
from call_manager.proposal_assist.collab.kernel import kernel_accept_prose
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc, ydoc_to_pm_json
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.db.models import User
from tests.proposal_assist.conftest import make_user, make_workspace
from tests.proposal_assist.collab.test_collab_kernel_accept import (
    _git_head, _init_repo, _live_room, _mk_pc,
)

_DOC = "Preamble.\n\n## A\n\nBody a.\n\n## B\n\nBody b.\n"


def _fork_edit_last_paragraph(room_doc, text: str) -> bytes:
    """Simulate a client: fork the room state, char-insert `text` into the LAST
    top-level block's XmlText, and return the incremental Yjs update (as a real
    y-prosemirror client would emit). Applied to the room AFTER the accept, it
    reproduces the C2 race."""
    fork = pycrdt.Doc()
    fork.apply_update(room_doc.get_update())
    frag = fork.get("prosemirror", type=pycrdt.XmlFragment)
    xtext = list(list(frag.children)[-1].children)[0]   # last block's XmlText
    cap: list[bytes] = []
    sub = fork.observe(lambda e: cap.append(e.update))
    with fork.transaction():
        xtext.insert(len(xtext), text)
    fork.unobserve(sub)
    return cap[-1]


def _seed(pg_session_factory, tmp_path, doc_md, email):
    with pg_session_factory() as s:
        o = make_user(s, email=email)
        ws = make_workspace(s, owner=o, current_phase="active")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, doc_md)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    with pg_session_factory() as s:
        room_doc = _live_room(s, wid, str(tmp_path))
    return wid, oid, repo, room_doc


def test_accept_preserves_concurrent_edit_in_other_section(pg_session_factory, tmp_path):
    wid, oid, repo, room_doc = _seed(pg_session_factory, tmp_path, _DOC, "k-c2@ki.se")
    # Client generates a char-level edit in section B (last block) vs the pre-accept
    # state; the update arrives AFTER the accept.
    fork_update = _fork_edit_last_paragraph(room_doc, " CONCURRENT")
    live_md = pm_to_markdown(ydoc_to_pm_json(room_doc))
    base_sha = split_sections(live_md)[1].sha   # section 1 = "## A"
    with pg_session_factory() as s:
        pc_id = _mk_pc(s, wid, section_index=1, new_text="## A\n\nA EDITED.\n",
                       base_commit_sha=_git_head(repo), base_section_sha=base_sha)
    with pg_session_factory() as s:
        approver = s.get(User, oid)
        kernel_accept_prose(s, room_doc=room_doc, proposed_change_id=pc_id,
                            workspace_id=wid, approver=approver, workspaces_dir=str(tmp_path))
        s.commit()
    room_doc.apply_update(fork_update)   # concurrent client update merges post-accept
    out = pm_to_markdown(ydoc_to_pm_json(room_doc))
    assert "A EDITED." in out                       # accept applied
    assert "CONCURRENT" in out                       # section-B racing edit SURVIVED


def test_clear_preamble_via_empty_new_text_does_not_crash(pg_session_factory, tmp_path):
    # Empty new_text is a legal preamble clear (kernel.py:106); the section-scoped
    # apply must map it to blocks=[] (raw markdown_to_pm raises on '') and succeed.
    wid, oid, repo, room_doc = _seed(pg_session_factory, tmp_path, _DOC, "k-clear@ki.se")
    live_md = pm_to_markdown(ydoc_to_pm_json(room_doc))
    base_sha = split_sections(live_md)[0].sha   # section 0 = the "Preamble." preamble
    with pg_session_factory() as s:
        pc_id = _mk_pc(s, wid, section_index=0, new_text="",
                       base_commit_sha=_git_head(repo), base_section_sha=base_sha)
    with pg_session_factory() as s:
        approver = s.get(User, oid)
        res = kernel_accept_prose(s, room_doc=room_doc, proposed_change_id=pc_id,
                                  workspace_id=wid, approver=approver, workspaces_dir=str(tmp_path))
        s.commit()
        assert res.pc.status == "accepted"
    assert "Preamble." not in pm_to_markdown(ydoc_to_pm_json(room_doc))


def test_normal_single_section_accept_unchanged(pg_session_factory, tmp_path):
    # Non-concurrent regression: a plain accept still yields the expected content.
    wid, oid, repo, room_doc = _seed(pg_session_factory, tmp_path, _DOC, "k-plain@ki.se")
    live_md = pm_to_markdown(ydoc_to_pm_json(room_doc))
    base_sha = split_sections(live_md)[1].sha
    with pg_session_factory() as s:
        pc_id = _mk_pc(s, wid, section_index=1, new_text="## A\n\nPlain edit.\n",
                       base_commit_sha=_git_head(repo), base_section_sha=base_sha)
    with pg_session_factory() as s:
        approver = s.get(User, oid)
        kernel_accept_prose(s, room_doc=room_doc, proposed_change_id=pc_id,
                            workspace_id=wid, approver=approver, workspaces_dir=str(tmp_path))
        s.commit()
    secs = split_sections(pm_to_markdown(ydoc_to_pm_json(room_doc)))
    assert "Plain edit." in secs[1].text and "Body b." in secs[2].text
```

(A citation-accept section-scoped regression is covered by the existing `test_collab_kernel_citation.py` suite once Task 6 Step 4 lands — Step 5 re-runs it.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_kernel_section_scoped.py -q -p no:cacheprovider`
Expected: `test_accept_preserves_concurrent_edit_in_other_section` FAILs — the current whole-doc `apply_pm_json_to_fragment` deletes every top-level block, tombstoning section B, so the post-accept fork update is lost (`CONCURRENT` absent). (`test_clear_preamble_via_empty_new_text_does_not_crash` also FAILs today with a 500/eviction — see Step 3.)

- [ ] **Step 3: Switch the prose apply to section-scoped (with the empty-`new_text` + empty-update guards)**

In `kernel.py` line 22, change the import: **remove `apply_pm_json_to_fragment`** (Task 6 replaces both its uses → otherwise ruff F401 fails Step 6) and **add `apply_pm_section_to_fragment`**. Then replace the prose apply (lines 124-127):

```python
    # Apply ONLY the target section's blocks to the live room fragment (Slice 3a
    # C2): the staleness gate above proved this section unchanged since the
    # proposer read it, so a section-scoped replace clobbers nothing on the target
    # and preserves concurrent edits in every OTHER section. An empty new_text is a
    # legal preamble clear (see the structure guard above) — raw markdown_to_pm
    # raises on blank input, so map it to NO blocks.
    if new_text.strip():
        section_blocks = markdown_to_pm(new_text).get("content") or []
    else:
        section_blocks = []
    update = apply_pm_section_to_fragment(
        room_doc, section_index=pc.section_index, blocks=section_blocks
    )
    if not update:
        # No net change to the fragment (e.g. a blank edit of an already-empty
        # range). Persisting b"" would poison the durable log — pycrdt
        # apply_update(b"") raises on the next hydration. Accept as a no-op at the
        # current checkpoint (mirrors the new_text == live.text short-circuit above).
        repo = workspace_repo_dir(workspaces_dir, workspace_id)
        pc.status = "accepted"
        pc.resolved_by = approver.id
        pc.resolved_at = datetime.now(UTC)
        pc.resulting_commit_sha = _head_or_empty(repo)
        session.flush()
        return KernelAcceptResult(pc=pc, update=None, committed=False, evict=False)
```

- [ ] **Step 4: Switch the citation apply to section-scoped**

The citation path builds `result["new_proposal_text"]` (whole-doc marker-bearing markdown); the marker change is confined to the anchor's section (`pc.section_index`), which always has content (a marker was inserted), so no empty-guard is needed here. Replace the citation apply (line 236):

```python
    # Section-scoped apply (Slice 3a C2): the marker insertion is confined to the
    # anchor's section; splice just that section's new blocks so concurrent edits
    # elsewhere survive. Extract the changed section's markdown from the whole-doc
    # result and convert to PM blocks.
    new_section_md = split_sections(result["new_proposal_text"])[pc.section_index].text
    section_blocks = markdown_to_pm(new_section_md).get("content") or []
    update = apply_pm_section_to_fragment(
        room_doc, section_index=pc.section_index, blocks=section_blocks
    )
```

`split_sections` is already imported in `kernel.py` (used by `_live_sections`).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_kernel_section_scoped.py tests/proposal_assist/collab/ -q -p no:cacheprovider`
Expected: PASS (new fork-race + preamble-clear + plain-accept tests, plus all existing kernel/citation/collab tests — the existing suites confirm no behavior change for the non-concurrent case).

- [ ] **Step 6: Ruff + commit**

```bash
uv run ruff check src/call_manager/proposal_assist/collab/kernel.py tests/proposal_assist/collab/test_kernel_section_scoped.py
git add src/call_manager/proposal_assist/collab/kernel.py tests/proposal_assist/collab/test_kernel_section_scoped.py
git commit -m "fix(collab): kernel accepts apply section-scoped (preserve concurrent edits, C2)"
```

---

## Task 7: Reconciliation — serialize + connect self-reconcile (cluster a + b)

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/room.py` (`Room.__init__` — add `_recon_lock`)
- Modify: `src/call_manager/proposal_assist/collab/permissions.py` (extract `reconcile_client`; `refresh_permissions` holds the recon lock)
- Modify: `src/call_manager/proposal_assist/web/collab_ws.py` (self-reconcile after `acquire`, ~line 114)
- Test: `tests/proposal_assist/collab/test_collab_permissions.py`, `tests/proposal_assist/collab/test_collab_ws_route.py`

**Interfaces:**
- Consumes: `Room.clients` (dict[client, ClientState]), `_resolve(...)`, `make_checkpoint_on_evict(...)`, `MANAGER.release`/`drop_client`.
- Produces:
  - `Room._recon_lock: asyncio.Lock` (public via a `recon_lock` property, mirroring `Room.lock`).
  - `reconcile_client(session_factory, workspaces_dir, room, client, state, *, workspace_id, artifact) -> None` in `permissions.py` — re-resolve + apply ONE client's access (the per-client body, reused by `refresh_permissions` and the connect path). Assumes the caller holds `room.recon_lock`.

- [ ] **Step 1: Write the failing tests**

In `tests/proposal_assist/collab/test_collab_permissions.py`, add three tests. The serialization property (cluster a) is pinned **deterministically** by asserting the reconcile pass runs UNDER `room.recon_lock` (a gather-race convergence test is non-deterministic and, because each `_resolve` reads fresh committed state, converges either way — it pins nothing). Plus the connect-helper downgrade (b) and the fail-closed-on-resolve-error (d) tests:

```python
@pytest.mark.anyio
async def test_refresh_permissions_holds_recon_lock_during_pass(pg_session_factory, tmp_path, monkeypatch):
    # cluster (a): the reconcile pass runs UNDER room.recon_lock, so overlapping
    # signals serialize (a second pass cannot interleave into a stale can_write).
    import call_manager.proposal_assist.collab.permissions as perm
    ws_id, owner_id, member_id = _seed_ws_owner_member(pg_session_factory)
    key = (ws_id, "spec")
    member_c = FakeClient()

    async def _load():
        return []

    room = await MANAGER.acquire(key, load=_load, client=member_c,
                                 state=ClientState(user_id=member_id, can_write=True))
    held = {"locked": None}
    real_resolve = perm._resolve

    def _spy_resolve(sf, uid, wid, art):
        held["locked"] = room.recon_lock.locked()   # observed DURING the pass
        return real_resolve(sf, uid, wid, art)

    monkeypatch.setattr(perm, "_resolve", _spy_resolve)
    try:
        await refresh_permissions(pg_session_factory, str(tmp_path), workspace_id=ws_id, artifact="spec")
        assert held["locked"] is True   # pass ran while holding the recon lock
    finally:
        await MANAGER.evict(key)


@pytest.mark.anyio
async def test_reconcile_client_downgrades_on_locked_spec(pg_session_factory, tmp_path):
    # (b) The connect self-reconcile helper flips a just-registered client's
    # can_write when the DB says it should be read-only.
    from call_manager.proposal_assist.collab.permissions import reconcile_client
    ws_id, owner_id, member_id = _seed_ws_owner_member(pg_session_factory)
    key = (ws_id, "spec")
    member_c = FakeClient()

    async def _load():
        return []

    room = await MANAGER.acquire(key, load=_load, client=member_c,
                                 state=ClientState(user_id=member_id, can_write=True))
    try:
        _lock_spec(pg_session_factory, ws_id, owner_id)
        st = room.clients[member_c]
        async with room.recon_lock:
            await reconcile_client(pg_session_factory, str(tmp_path), room, member_c, st,
                                   workspace_id=ws_id, artifact="spec")
        assert room.clients[member_c].can_write is False
    finally:
        await MANAGER.evict(key)


@pytest.mark.anyio
async def test_reconcile_client_fails_closed_on_resolve_error(pg_session_factory, tmp_path, monkeypatch):
    # (d) On a resolve error (DB blip), the client is DOWNGRADED to read-only, NOT
    # disconnected — no stale write access leaks, but a transient blip does not
    # drop an innocent editor.
    import call_manager.proposal_assist.collab.permissions as perm
    from call_manager.proposal_assist.collab.permissions import reconcile_client
    ws_id, owner_id, member_id = _seed_ws_owner_member(pg_session_factory)
    key = (ws_id, "spec")
    member_c = FakeClient()

    async def _load():
        return []

    room = await MANAGER.acquire(key, load=_load, client=member_c,
                                 state=ClientState(user_id=member_id, can_write=True))

    def _boom(*_a, **_k):
        raise RuntimeError("db blip")

    monkeypatch.setattr(perm, "_resolve", _boom)
    try:
        st = room.clients[member_c]
        async with room.recon_lock:
            await reconcile_client(pg_session_factory, str(tmp_path), room, member_c, st,
                                   workspace_id=ws_id, artifact="spec")
        assert st.can_write is False           # fail-closed downgrade
        assert member_c.closed is None          # NOT disconnected
        assert member_c in room.clients         # still registered
    finally:
        await MANAGER.evict(key)
```

Also add a **route-level** test to `tests/proposal_assist/collab/test_collab_ws_route.py` that exercises the connect self-reconcile wiring end-to-end (models on the existing `test_read_only_member_update_is_not_persisted`): a member connecting to a **locked-spec** room connects successfully (read works) and its write is dropped — proving the route runs `reconcile_client` at connect without breaking the handshake. (A deterministically-injected mid-acquire straddle is impractical at the route level; the isolated TOCTOU downgrade is unit-tested by `test_reconcile_client_downgrades_on_locked_spec`.)

```python
def test_member_connecting_to_locked_spec_is_read_only(_app, pg_session_factory):
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    from call_manager.db.models import WorkspaceViewer, YjsUpdate

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="ls-owner@ki.se")
        member = make_user(s, email="ls-member@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        ws.spec_locked_at = datetime.now(UTC)          # locked → members read-only on spec
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=member.id))
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="spec")
        s.commit()
        wid, mid = ws.id, member.id
    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, mid))
    try:
        with client.websocket_connect(f"/api/ws/workspaces/{wid}/spec") as wsconn:
            assert P.is_sync_step1(wsconn.receive_bytes())   # connect + read OK
            wsconn.send_bytes(_update_frame("should be dropped"))
    finally:
        client.__exit__(None, None, None)
    with pg_session_factory() as s:
        rows = s.execute(select(YjsUpdate).where(YjsUpdate.workspace_id == wid)).scalars().all()
        assert rows == []   # read-only member's write was dropped
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_permissions.py -k "recon_lock or reconcile_client" -q -p no:cacheprovider`
Expected: FAIL — `reconcile_client` / `room.recon_lock` do not exist (the `recon_lock` spy test AttributeErrors; the two `reconcile_client` tests ImportError). (The new route test needs the Step-5 wiring; it passes once the route imports `reconcile_client`.)

- [ ] **Step 3: Add `Room._recon_lock`**

In `room.py` `Room.__init__` add:

```python
        self._recon_lock = asyncio.Lock()   # serialize permission reconciliation (Slice 3a a/b)
```

And expose it beside the existing `lock` property:

```python
    @property
    def recon_lock(self) -> asyncio.Lock:
        return self._recon_lock
```

- [ ] **Step 4: Extract `reconcile_client` and serialize `refresh_permissions`**

In `permissions.py`, extract the per-client body of `refresh_permissions` into a reusable coroutine and have `refresh_permissions` hold the recon lock across the pass:

```python
async def reconcile_client(session_factory, workspaces_dir, room, client, state, *,
                           workspace_id: uuid.UUID, artifact: str) -> None:
    """Re-resolve ONE client's access and apply the delta. Caller holds
    room.recon_lock. On a resolve error, fail CLOSED (downgrade to read-only) —
    never leave stale write access (Slice 3a d)."""
    try:
        is_member, can_write, ws_deleted = await run_in_threadpool(
            _resolve, session_factory, state.user_id, workspace_id, artifact
        )
    except Exception:
        logger.warning(
            "collab.refresh_permissions_resolve_failed",
            extra={"event": "collab_refresh_resolve_failed", "room": str((workspace_id, artifact))},
            exc_info=True,
        )
        state.can_write = False   # fail-closed downgrade (Slice 3a d)
        return
    if not is_member:
        state.can_write = False
        with contextlib.suppress(Exception):
            await client.close(code=_WS_POLICY_VIOLATION)
        key = (workspace_id, artifact)
        if ws_deleted:
            await MANAGER.drop_client(key, client)
        else:
            await MANAGER.release(
                key, client=client,
                on_evict=make_checkpoint_on_evict(session_factory, workspaces_dir, workspace_id, artifact),
            )
    else:
        state.can_write = can_write


async def refresh_permissions(session_factory, workspaces_dir, *,
                              workspace_id: uuid.UUID, artifact: str) -> None:
    """Reconcile every client in room (workspace_id, artifact). No-op if the room
    isn't live. Serialized per-room via room.recon_lock so overlapping signals
    (e.g. unlock then lock) cannot interleave into a stale can_write (Slice 3a a)."""
    key = (workspace_id, artifact)
    room = MANAGER.get(key)
    if room is None:
        return
    async with room.recon_lock:
        for client, state in list(room.clients.items()):
            await reconcile_client(session_factory, workspaces_dir, room, client, state,
                                   workspace_id=workspace_id, artifact=artifact)
```

This folds the fail-closed downgrade (cluster d) into `reconcile_client`, so Task 8's (d) requirement is satisfied here — Task 8 covers only (c) and the (e) confirmation. Update the note in Task 8 accordingly. Delete the old inline per-client loop body (the `state.can_write = False` fail-open `continue` at permissions.py:106-112 is replaced by the fail-closed downgrade above).

- [ ] **Step 5: Self-reconcile the client after `acquire` in the WS route**

In `web/collab_ws.py`, add `from call_manager.proposal_assist.collab.permissions import reconcile_client` at the top of the file, then insert the self-reconcile as the **first statements inside the existing `try:` block** (line 115, before `async with room.lock:`) — NOT between `acquire` and `try:`. It must be inside the try so the `finally: MANAGER.release(...)` still runs (and cleans up the registered client) if `reconcile_client` raises:

```python
    room = await MANAGER.acquire(key, load=_load, client=websocket, state=state)
    try:
        # Close the connect-straddle TOCTOU (Slice 3a b): a permission mutation that
        # committed while we were mid-acquire may have fired its refresh BEFORE this
        # client was registered. Re-resolve THIS client now, under the recon lock so
        # ordering vs a concurrent refresh is well-defined (either the refresh saw
        # us, or we see the committed mutation here). Inside the try so `finally`
        # still releases the client if this raises.
        async with room.recon_lock:
            await reconcile_client(
                session_factory, workspaces_dir, room, websocket, state,
                workspace_id=workspace_id, artifact=artifact,
            )
        if websocket not in room.clients:
            return  # reconcile revoked + deregistered this client (already closed 1008)
        async with room.lock:
            initial = room.initial_message()
        await websocket.send_bytes(initial)
        async for message in websocket.iter_bytes():
            ...   # (unchanged existing message loop)
```

Confirm `websocket`, `state`, `session_factory`, `workspaces_dir`, `workspace_id`, `artifact` are all in scope (they are — lines 63/87/113 and the route params).

**Lock-acquisition order (deadlock-freedom — spec Open-risks requirement).** Add this as a comment near the `_recon_lock` definition in `room.py`, and keep the invariant: the only acquisition order involving `recon_lock` is **`recon_lock` → (`manager._lock` via `MANAGER.release`/`drop_client`)**. No path acquires `recon_lock` while holding `manager._lock` or `room._lock`, and no path acquires `room._lock` while holding `recon_lock` (the WS route takes `recon_lock` for the self-reconcile, releases it, THEN takes `room._lock` for the initial message; `refresh_permissions` holds only `recon_lock` across its pass). So there is no lock cycle → deadlock-free. Note that `reconcile_client` may run the checkpoint `on_evict` (a threadpool git commit + advisory commit-mutex wait) while holding `recon_lock`, which briefly serializes connects to the same room behind it — acceptable (revokes are rare; connects to OTHER rooms are unaffected).

- [ ] **Step 6: Run the tests to verify they pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_permissions.py tests/proposal_assist/collab/test_collab_ws_route.py -q -p no:cacheprovider`
Expected: PASS (new serialize + reconcile-client tests; existing 2c permission/route tests still green).

- [ ] **Step 7: Ruff + commit**

```bash
uv run ruff check src/call_manager/proposal_assist/collab/room.py src/call_manager/proposal_assist/collab/permissions.py src/call_manager/proposal_assist/web/collab_ws.py tests/proposal_assist/collab/test_collab_permissions.py
git add src/call_manager/proposal_assist/collab/room.py src/call_manager/proposal_assist/collab/permissions.py src/call_manager/proposal_assist/web/collab_ws.py tests/proposal_assist/collab/test_collab_permissions.py tests/proposal_assist/collab/test_collab_ws_route.py
git commit -m "fix(collab): serialize reconciliation + connect self-reconcile + fail-closed downgrade (cluster a/b/d)"
```

---

## Task 8: Reconciliation — pending checkpoint through operate-exit (cluster c)

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/room.py` (`release`, lines 261-272; `operate()` docstring + finally, lines 220-256)
- Test: `tests/proposal_assist/collab/test_collab_room_operate.py` (the existing home for operate/release/evict semantics)

**Interfaces:**
- Consumes: `Room.ops`, `Room.clients`, `RoomManager._rooms`, `on_evict` callback.
- Produces: `Room._pending_on_evict` (callback stashed when `release` cannot evict due to `ops > 0`), invoked by `operate()`-exit when it finally evicts.

**Note:** cluster (d) fail-closed downgrade was implemented in Task 7's `reconcile_client`; this task covers cluster (c) only.

- [ ] **Step 1: Write the failing test**

Add to `tests/proposal_assist/collab/test_collab_room_operate.py` (which uses a **fresh `RoomManager()` per test** — `mgr`, not the module global `MANAGER` — and relies on `asyncio_mode = "auto"`, so no `@pytest.mark.anyio` decorator; import `ClientState`, `RoomManager` are already at the top of that file — add `from tests.proposal_assist.collab.test_collab_room import FakeClient`):

```python
async def test_last_client_release_during_op_checkpoints_on_op_exit():
    # A room with ONE client and an in-flight server op (ops=1). The last client
    # is released with an on_evict checkpoint; because ops>0 the room is NOT
    # evicted yet and on_evict must NOT run. When the op exits (ops->0, no
    # clients), the room is evicted AND the stashed on_evict fires exactly once.
    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")
    fired = {"n": 0}

    async def _on_evict(_room):
        fired["n"] += 1

    async def _load():
        return []

    client = FakeClient()
    async with mgr.operate(key, load=_load) as room:   # ops=1
        room.clients[client] = ClientState(user_id=1, can_write=True)
        await mgr.release(key, client=client, on_evict=_on_evict)
        assert mgr.get(key) is room     # not evicted (ops>0)
        assert fired["n"] == 0          # on_evict deferred
    # operate() exited: ops->0, no clients → evict + fire stashed on_evict
    assert mgr.get(key) is None
    assert fired["n"] == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_room_operate.py -k "checkpoints_on_op_exit" -q -p no:cacheprovider`
Expected: FAIL — `on_evict` never fires (`fired["n"] == 0` after operate exits).

- [ ] **Step 3: Stash + run the pending on_evict**

In `room.py`, add the field in `Room.__init__`:

```python
        self._pending_on_evict = None   # deferred last-leave checkpoint (Slice 3a c)
```

In `release`, when the room cannot be evicted because `ops > 0` but it is now client-empty, stash the callback:

```python
    async def release(self, key: RoomKey, *, client, on_evict=None) -> None:
        async with self._lock:
            room = self._rooms.get(key)
            if room is None:
                return
            room.clients.pop(client, None)
            if not room.clients and room.ops == 0:
                evicted = self._rooms.pop(key)
            else:
                evicted = None
                if not room.clients and on_evict is not None:
                    # Client-empty but an op is in flight: defer the checkpoint to
                    # operate()-exit so a straddling op cannot swallow it (Slice 3a c).
                    room._pending_on_evict = on_evict
        if evicted is not None and on_evict is not None:
            await on_evict(evicted)
```

In `operate()`, update the docstring — the current line "Does NOT checkpoint on evict — the kernel op already checkpointed within its transaction." is now false (operate-exit fires a `release`-deferred checkpoint). Change it to: "Does not create its OWN checkpoint on evict, but runs any checkpoint `release` deferred because this op held `ops > 0` (Slice 3a c)." Then run the stashed callback in the `finally` when it evicts:

```python
        finally:
            pending = None
            async with self._lock:
                room.ops -= 1
                if not room.clients and room.ops == 0 and self._rooms.get(key) is room:
                    self._rooms.pop(key, None)
                    pending = room._pending_on_evict
                    room._pending_on_evict = None
            if pending is not None:
                await pending(room)   # OUTSIDE the manager lock (mirrors release)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_room_operate.py tests/proposal_assist/collab/test_collab_room.py -q -p no:cacheprovider`
Expected: PASS (new test + existing room/operate tests — ops-refcount eviction behavior unchanged for the no-pending case).

- [ ] **Step 5: Ruff + commit**

```bash
uv run ruff check src/call_manager/proposal_assist/collab/room.py tests/proposal_assist/collab/test_collab_room_operate.py
git add src/call_manager/proposal_assist/collab/room.py tests/proposal_assist/collab/test_collab_room_operate.py
git commit -m "fix(collab): carry deferred last-leave checkpoint through operate-exit (cluster c)"
```

---

## Task 9: Reconciliation — evict blast-radius confirmation (cluster e)

**Files:**
- Modify: `src/call_manager/proposal_assist/proposals/kernel_accept.py` (clarifying comment on the evict arm, lines 100-104 — the `except _CLEAN_ACCEPT_ERRORS`/`except Exception` block near the end of `accept_via_kernel`)
- Test: `tests/proposal_assist/collab/test_kernel_accept_evict.py`

**Interfaces:**
- Consumes: `_CLEAN_ACCEPT_ERRORS`, `MANAGER.evict`, `MANAGER.operate`, `accept_via_kernel`.
- Produces: no new symbols; discriminating regression tests pin the blast-radius contract.

**Key harness point:** `accept_via_kernel` uses the module-global `MANAGER` (`kernel_accept.py:61`), and `MANAGER.operate` refcount-evicts a client-empty room on exit. So a test MUST register a real client on the key first (via `MANAGER.acquire`) — otherwise BOTH contracts collapse to "room gone on exit" and neither is discriminating (contract 1 would even fail against correct code). With a client registered, a pre-mutation failure leaves the room (client present, no evict) and a post-mutation failure evicts it (force-closes the client) — the two are now distinguishable.

- [ ] **Step 1: Write the tests (characterization; contract 2 is discriminating)**

Create `tests/proposal_assist/collab/test_kernel_accept_evict.py`:

```python
from pathlib import Path

import pytest

from call_manager.db.models import User
from call_manager.proposal_assist.collab import state
from call_manager.proposal_assist.collab.hydrate import load_or_hydrate
from call_manager.proposal_assist.collab.markdown_bridge import pm_to_markdown
from call_manager.proposal_assist.collab.room import MANAGER, ClientState
from call_manager.proposal_assist.collab.yjs_bridge import ydoc_to_pm_json
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.proposals.kernel_accept import accept_via_kernel
from tests.proposal_assist.conftest import make_user, make_workspace
from tests.proposal_assist.collab.test_collab_kernel_accept import _git_head, _init_repo, _mk_pc
from tests.proposal_assist.collab.test_collab_room import FakeClient


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _seed_repo(pg_session_factory, tmp_path, email):
    with pg_session_factory() as s:
        o = make_user(s, email=email)
        ws = make_workspace(s, owner=o, current_phase="active")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, "Preamble.\n\n## A\n\nBody a.\n")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    return wid, oid, repo


async def _acquire_client(pg_session_factory, tmp_path, wid, oid):
    async def _load():
        with pg_session_factory() as s:
            u = load_or_hydrate(s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path))
            s.commit()
            return u
    client = FakeClient()
    await MANAGER.acquire((wid, "draft"), load=_load, client=client,
                          state=ClientState(user_id=oid, can_write=True))
    return client


@pytest.mark.anyio
async def test_pre_mutation_failure_does_not_evict(pg_session_factory, tmp_path):
    # PRE-mutation IndexError (section_index out of range, in _CLEAN_ACCEPT_ERRORS)
    # → no evict; the registered client keeps its live doc.
    wid, oid, repo = _seed_repo(pg_session_factory, tmp_path, "evict-pre@ki.se")
    key = (wid, "draft")
    client = await _acquire_client(pg_session_factory, tmp_path, wid, oid)
    try:
        with pg_session_factory() as s:
            pc_id = _mk_pc(s, wid, section_index=999, new_text="## X\n\nx.\n",
                           base_commit_sha=_git_head(repo), base_section_sha="deadbeef")
        with pg_session_factory() as s:
            approver = s.get(User, oid)
        with pytest.raises(IndexError):
            await accept_via_kernel(session_factory=pg_session_factory, workspaces_dir=str(tmp_path),
                                    workspace_id=wid, pc_id=pc_id, approver=approver, artifact="draft")
        assert MANAGER.get(key) is not None          # NOT evicted (pre-mutation)
        assert client in MANAGER.get(key).clients      # client kept
        assert client.closed is None
    finally:
        await MANAGER.evict(key)


@pytest.mark.anyio
async def test_post_mutation_failure_evicts(pg_session_factory, tmp_path, monkeypatch):
    # POST-mutation failure (checkpoint_artifact raises AFTER the fragment mutation)
    # → evict. Discriminating: WITH a client registered, the room would survive on
    # exit if the evict arm were removed (client present), so this pins the evict.
    import call_manager.proposal_assist.collab.kernel as kmod
    wid, oid, repo = _seed_repo(pg_session_factory, tmp_path, "evict-post@ki.se")
    key = (wid, "draft")
    client = await _acquire_client(pg_session_factory, tmp_path, wid, oid)
    # base from the live (registered) room so the accept passes staleness + mutates.
    live_md = pm_to_markdown(ydoc_to_pm_json(MANAGER.get(key).doc))
    base_sha = split_sections(live_md)[1].sha   # section 1 = "## A"
    with pg_session_factory() as s:
        pc_id = _mk_pc(s, wid, section_index=1, new_text="## A\n\nEdited.\n",
                       base_commit_sha=_git_head(repo), base_section_sha=base_sha)
    with pg_session_factory() as s:
        approver = s.get(User, oid)

    def _boom(*_a, **_k):
        raise RuntimeError("checkpoint failed")

    monkeypatch.setattr(kmod, "checkpoint_artifact", _boom)
    with pytest.raises(RuntimeError):
        await accept_via_kernel(session_factory=pg_session_factory, workspaces_dir=str(tmp_path),
                                workspace_id=wid, pc_id=pc_id, approver=approver, artifact="draft")
    assert MANAGER.get(key) is None       # evicted (post-mutation recovery)
    assert client.closed == 1011           # evict force-closed the socket
```

- [ ] **Step 2: Run the tests to verify they behave as specified**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_kernel_accept_evict.py -q -p no:cacheprovider`
Expected: Both PASS against the CURRENT code — these characterize existing correct behavior. Contract 2 is discriminating (it would FAIL if the `except Exception: MANAGER.evict` arm were removed, because the registered client would keep the room alive on exit). If either fails as written, that is a real defect to fix before proceeding.

- [ ] **Step 3: Add the clarifying comment**

In `kernel_accept.py`, expand the evict arm's comment (do not change behavior):

```python
        except _CLEAN_ACCEPT_ERRORS:
            raise  # validation error BEFORE any mutation — room doc untouched, no evict
        except Exception:
            # POST-mutation failure only: the fragment was already mutated but the
            # txn rolled back, so room.doc is ahead of the durable log. Evict to
            # force every client to reconnect and rebuild cleanly from the log.
            # Blast radius = all clients on this room; acceptable because (a) accept
            # failures here are rare, (b) reconnect-from-log is lossless (Slice 3a e;
            # re-hydrate-in-place is deferred to a later slice). Pre-mutation trips
            # are enumerated in _CLEAN_ACCEPT_ERRORS above and never reach here.
            await MANAGER.evict(key)  # room.doc may be ahead of the rolled-back log
            raise
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_kernel_accept_evict.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Ruff + commit**

```bash
uv run ruff check src/call_manager/proposal_assist/proposals/kernel_accept.py tests/proposal_assist/collab/test_kernel_accept_evict.py
git add src/call_manager/proposal_assist/proposals/kernel_accept.py tests/proposal_assist/collab/test_kernel_accept_evict.py
git commit -m "test(collab): pin kernel-accept evict blast-radius contract + document (cluster e)"
```

---

## Task 10: Full grouped sweep + parity gate

**Files:** none (verification only).

- [ ] **Step 1: Run the parity-critical grouped sweep**

Run:
```bash
EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/ tests/proposal_assist/ tests/test_api_spec_lock.py -q -p no:cacheprovider
```
Expected: PASS. Confirm the flag-off parity suites are green **unchanged** — `tests/proposal_assist/proposals/test_accepting.py`, `tests/test_api_spec_lock.py`, and the member/rubric/soft-delete suites.

- [ ] **Step 2: Ruff over the whole tree (CI parity)**

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 3: Confirm no schema/migration drift**

Run: `git diff --name-only main...HEAD | grep -i "migrations/\|alembic" || echo "no migrations touched"`
Expected: `no migrations touched` (Slice 3a adds no migration).

- [ ] **Step 4: Confirm the flag is still OFF by default**

Verify nothing in this slice calls `enable_collab` outside tests:

Run: `grep -rn "enable_collab" src/`
Expected: only the definition in `collab/state.py` and any pre-existing test-only wiring — no new production enablement.

---

## Self-Review (author checklist — completed at plan-write time)

**1. Spec coverage:**
- C1 (three consumers) → Tasks 1 (helper) + 2 (read) + 3 (staleness/#1) + 4 (citation/#3); empty-doc/#2 → Task 1; end-to-end read→accept regression → Task 2.
- C2 (section-scoped replace) → Tasks 5 (primitive) + 6 (kernel wiring); boundary rule = `is_h2_heading_line` on serialized blocks (matches `split_sections`, `##NoSpace`-safe); empty-section-body → Tasks 5 + 6.
- C3 (a) serialize → Task 7; (b) connect self-reconcile → Task 7 (+ route test); (c) pending checkpoint → Task 8; (d) fail-closed → Task 7 (`reconcile_client` + test); (e) evict blast-radius → Task 9.
- Parity gate + no-migration + flag-off → Task 10.

**2. Placeholder scan:** Tasks 4 references the existing citation-propose test seeding pattern (naming the exact assertion). All other tasks carry complete, executable test + production code. No `...`-sketch red steps remain.

**3. Type consistency:** `canonical_artifact_markdown(session, *, workspace_id, artifact, repo_dir)` used identically in Tasks 1/2/3/4. `apply_pm_section_to_fragment(doc, *, section_index, blocks)` defined in Task 5, called identically in Task 6. `reconcile_client(session_factory, workspaces_dir, room, client, state, *, workspace_id, artifact)` defined in Task 7, called in Task 7 (route) + Task 7 tests. `Room.recon_lock` / `Room._pending_on_evict` consistent across Tasks 7-8. `is_collab_enabled(session, *, workspace_id, artifact)` used identically in Tasks 2/3/4. Artifact strings are `"draft"`/`"spec"` throughout (`str(Artifact.DRAFT) == "draft"`, verified).

## Fable review fold-in (2026-07-02, 48-agent adversarial review, 40 findings)

This plan was revised after a multi-agent review of the first draft (committed e92161f). All correctness-critical findings were reproduced empirically against the repo venv before folding:

- **Blocker — section-boundary dedupe (no-preamble docs):** the draft's `if starts[1:2] == [0]: starts = starts[1:]` shifted section indices for any doc starting with `##`, silently splicing the wrong section. **Fixed** by deleting the dedupe (`split_sections` always emits an — possibly empty — Section 0; `starts=[0,0,...]` aligns every index). Verified across preamble/no-preamble/`##NoSpace`/empty edges.
- **Major — boundary rule divergence (`##NoSpace`):** a tag-based `_child_is_h2` walk diverges from `split_sections` (which counts `##NoSpace` paragraphs as boundaries; `pm_to_markdown` never escapes `#`). **Fixed** by `_block_starts_section` = `is_h2_heading_line` on each block's serialized first line (the shared `split_sections` predicate). Verified.
- **Major — empty `new_text` + empty-update poison:** raw `markdown_to_pm("")` raises → evict+500; a `b""` update poisons hydration. **Fixed** with a blank→`blocks=[]` guard + an `if not update:` no-op-accept skip (Task 6).
- **Major — false-red tests:** the C2 concurrent-edit test (pre-applied edit survives the whole-doc re-read) and the cluster-(a) serialize test (both refreshes read fresh state → converge either way) pinned nothing. **Fixed** with a fork-then-apply-after race test (verified: whole-doc loses the edit, section-scoped preserves it) and a deterministic "reconcile pass runs under `recon_lock`" assertion.
- **Major — test-harness errors:** FK violations (bare uuid → `make_workspace`), missing NOT NULL `base_commit_sha`/`origin_kind`, vacuous `section_index=0` (→ 1), `ToolContext` missing `settings=Settings()`, Task 9 evict tests missing a registered client (→ non-discriminating), wrong fixture/file names (`test_collab_hydrate.py`, `_mk_pc`/`_live_room`/`_init_repo`), dropped `import uuid`, F401 orphaned `apply_pm_json_to_fragment` import, `test_collab_room_operate.py` local `RoomManager()`. **All fixed** with verified real names.
- **Coverage gaps:** end-to-end C1 read→accept regression, fail-closed (d) resolve-raises test, connect-straddle (b) route test, empty-section-body test, lock-order/deadlock-freedom doc. **All added.**
- **Self-reconcile placement:** moved inside the WS route `try:` so `finally: release` always runs.
- **Refuted (2):** `reconcile_client` mutating `can_write` without `room._lock` (verified not to regress the Slice-2c High#1 under-lock recheck — the revoke sets `can_write=False` before close, and the write path re-checks under `room._lock`); the Task 3 `workspaces_dir` fixture path (now concrete via `workspace_repo_dir`).
