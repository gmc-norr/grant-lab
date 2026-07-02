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


def _seed_log(pg_session_factory, wid, artifact, md):
    """Persist a single Yjs update reconstructing `md` into the durable log."""
    seed = pm_json_to_ydoc(markdown_to_pm(md)).get_update()
    with pg_session_factory() as s:
        store.append_update(s, workspace_id=wid, artifact=artifact, actor_id=None, update=seed)
        s.commit()


def test_canonical_from_nonempty_log(pg_session_factory):
    wid = uuid.uuid4()
    md = "## A\n\nBody a.\n"
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

In `src/call_manager/proposal_assist/collab/hydrate.py`:

Replace the imports block + local `_EMPTY_DOC`/`build_doc`/`_markdown_to_pm`/`_read_head_markdown` so hydrate imports them from `canonical` (re-exporting keeps `hydrate.build_doc` etc. valid for any external importer). The new top of the file:

```python
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.canonical import (  # shared, single source
    _EMPTY_DOC,
    build_doc,
    markdown_to_pm_or_empty,
    read_head_markdown,
)
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from call_manager.proposal_assist.paths import workspace_repo_dir
```

Delete hydrate's local `_EMPTY_DOC`, `build_doc`, `_markdown_to_pm`, and `_read_head_markdown` definitions. In `load_or_hydrate`, replace the two call sites:

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

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_canonical.py tests/proposal_assist/collab/test_hydrate.py -q -p no:cacheprovider`
Expected: PASS (new canonical tests + hydrate suite still green after the rewire).

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

Create `tests/proposal_assist/collab/test_canonical_consumers.py` with the read-consumer test. It drives the real agent read path via a `ToolContext`. Reuse the workspace/repo helpers already used by the tools tests (see `tests/proposal_assist/tools/` conftest for `make_tool_context` / repo fixtures; if none exists, construct `ToolContext(workspace_id=..., repo_dir=<tmp git repo>, session_factory=pg_session_factory)` directly).

```python
import uuid

import pytest

from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from call_manager.proposal_assist.tools.proposal import _read_artifact_impl
from call_manager.proposal_assist.tools.registry import ToolContext
from call_manager.proposal_assist.artifacts import Artifact, filename_for
from call_manager.proposal_assist.editing.sectionizer import split_sections


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _write_repo_file(repo_dir, artifact, md):
    (repo_dir / filename_for(artifact)).write_text(md)


@pytest.mark.anyio
async def test_read_collab_on_returns_log_canonical(pg_session_factory, tmp_path):
    wid = uuid.uuid4()
    # Raw HEAD is TIGHT (no blank line after heading); the durable log holds the
    # normalized form. Collab-on read must return the normalized (log) content so
    # its section shas match the kernel's live.sha at accept.
    _write_repo_file(tmp_path, Artifact.DRAFT, "## A\nBody a.\n")
    norm = pm_to_markdown(markdown_to_pm("## A\nBody a.\n"))
    seed = pm_json_to_ydoc(markdown_to_pm(norm)).get_update()
    with pg_session_factory() as s:
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None, update=seed)
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()

    ctx = ToolContext(workspace_id=wid, repo_dir=tmp_path, session_factory=pg_session_factory)
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
    wid = uuid.uuid4()
    raw = "## A\nBody a.\n"  # tight — NOT normalized
    _write_repo_file(tmp_path, Artifact.DRAFT, raw)
    # collab NOT enabled
    ctx = ToolContext(workspace_id=wid, repo_dir=tmp_path, session_factory=pg_session_factory)
    out = await _read_artifact_impl(
        ctx, filename=filename_for(Artifact.DRAFT),
        head_attr="agent_view_head", sections_attr="agent_view_sections",
        proposal_attr="agent_view_proposal",
    )
    assert out["content"] == raw  # byte-for-byte raw HEAD (parity)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_canonical_consumers.py -q -p no:cacheprovider`
Expected: FAIL — `test_read_collab_on_returns_log_canonical` asserts normalized content but the impl returns raw HEAD.

- [ ] **Step 3: Modify `_read_artifact_impl`**

In `tools/proposal.py`, add imports near the top:

```python
from call_manager.proposal_assist.collab import canonical
from call_manager.proposal_assist.collab.state import is_collab_enabled
```

Replace the content read (line 68 `content = path.read_text()`) so collab-on reads the canonical form. The artifact string is derived from `filename` (draft vs spec) — map it via the filename, or accept an `artifact` value the caller already knows. Since `_read_artifact_impl` receives `filename`, resolve the artifact string once:

```python
    # Collab-on: read the ROOM-CANONICAL (normalized) content so the captured
    # base section shas match the kernel's accept-time live.sha (Slice 3a C1).
    # Flag-off: raw HEAD file, byte-for-byte as before (parity).
    artifact = "spec" if filename == SPEC_FILENAME else "draft"
    with ctx.session_factory() as _s:
        _collab = is_collab_enabled(_s, workspace_id=ctx.workspace_id, artifact=artifact)
        if _collab:
            content = canonical.canonical_artifact_markdown(
                _s, workspace_id=ctx.workspace_id, artifact=artifact, repo_dir=ctx.repo_dir
            )
    if not _collab:
        content = path.read_text()
```

Keep the existing `setattr(ctx, head_attr, current_head(...))` above unchanged (HEAD tracking stays; the collab accept keys staleness on section SHA, not `base_commit_sha`). The `sections_attr` / `proposal_attr` lines below already build from `content`, so they now flow from the canonical text. Confirm `SPEC_FILENAME` / `PROPOSAL_FILENAME` are imported in this module (they are — used elsewhere in the file).

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
from call_manager.db.models import ProposedChange, User, Workspace
from call_manager.proposal_assist.proposals.staleness import refresh_staleness


def _seed_ws_with_proposal(pg_session_factory, tmp_path, *, collab, base_from):
    """Create a workspace + a pending prose proposal whose base_section_sha is the
    section sha of `base_from` markdown. Writes tight raw HEAD to disk and seeds
    the durable log with the normalized form when collab=True."""
    raw = "## A\nBody a.\n"
    norm = pm_to_markdown(markdown_to_pm(raw))
    (tmp_path / filename_for(Artifact.DRAFT)).write_text(raw)
    base_md = norm if base_from == "norm" else raw
    base_sha = split_sections(base_md)[0].sha
    with pg_session_factory() as s:
        owner = User(status="active", email=f"stale-{uuid.uuid4().hex[:8]}@ki.se")
        s.add(owner); s.flush()
        ws = Workspace(title="t", owner_id=owner.id, current_phase="active")
        s.add(ws); s.flush()
        if collab:
            seed = pm_json_to_ydoc(markdown_to_pm(norm)).get_update()
            store.append_update(s, workspace_id=ws.id, artifact="draft", actor_id=None, update=seed)
            state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        pc = ProposedChange(
            workspace_id=ws.id, target_artifact="draft", section_index=0,
            base_section_sha=base_sha, status="pending", kind="prose_edit",
            payload={"new_text": "## A\n\nEdited.\n"},
        )
        s.add(pc); s.commit()
        return ws.id, pc.id


def test_sweep_collab_on_does_not_false_flag_normalized_base(pg_session_factory, tmp_path):
    # Collab-on, base captured from the NORMALIZED log form → the sweep must NOT
    # flip it stale (raw HEAD differs from normalized, which is the #1 bug).
    ws_id, pc_id = _seed_ws_with_proposal(pg_session_factory, tmp_path, collab=True, base_from="norm")
    with pg_session_factory() as s:
        refresh_staleness(session=s, workspace_id=ws_id, workspaces_dir=str(tmp_path.parent))
        s.commit()
    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id).status == "pending"


def test_sweep_flag_off_uses_raw_head(pg_session_factory, tmp_path):
    # Flag-off, base captured from RAW head → matches raw sweep → stays pending
    # (proves the sweep still reads raw git when collab is off; parity).
    ws_id, pc_id = _seed_ws_with_proposal(pg_session_factory, tmp_path, collab=False, base_from="raw")
    with pg_session_factory() as s:
        refresh_staleness(session=s, workspace_id=ws_id, workspaces_dir=str(tmp_path.parent))
        s.commit()
    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id).status == "pending"
```

Note: `refresh_staleness` computes `repo_dir = workspace_repo_dir(workspaces_dir, workspace_id)`. The test writes the artifact file directly into `tmp_path`; pass `workspaces_dir` so that `workspace_repo_dir(workspaces_dir, ws_id)` resolves to `tmp_path`. Inspect `workspace_repo_dir`'s layout (`src/call_manager/proposal_assist/paths.py`) and construct the fixture directory to match (e.g. create the file under `<workspaces_dir>/<ws_id>/...`). Adjust the two `write_text` targets and the `workspaces_dir` argument accordingly so `path.exists()` is true for the flag-off branch.

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

Add to `tests/proposal_assist/collab/test_canonical_consumers.py` a test that drives `propose_citation_impl` with an anchor present in the durable log but ABSENT from raw disk `proposal.md`, asserting it does NOT false-reject as "anchor not found" when collab is on. Model it on the existing citation-propose tests (see `tests/proposal_assist/tools/test_citations*.py` for the `ToolContext` + `Reference`/`ReferenceChunk` seeding pattern and the exact `propose_citation_impl` argument names). The core assertion:

```python
# Raw disk proposal.md lacks the anchor; the durable log (collab-on) contains it.
# With collab on, the anchor probe reads the log-canonical text and finds it, so
# propose returns a proposed_change (not {"error": "anchor not found"}).
result = await propose_citation_impl(ctx, ...)  # anchor lives only in the log
assert "error" not in result
```

Include a flag-off counterpart asserting the probe still reads raw disk (parity): with collab off and the anchor only in the log, propose returns the "anchor not found" error (disk is authoritative when flag off).

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
- Consumes: `pycrdt.Doc`/`XmlFragment`/`XmlElement`; `_append_node`, `_append_inline_content`, `_block_holds_inline`, `_node_attrs_for_yjs`, `_FRAGMENT_KEY` (existing module-private helpers).
- Produces:
  - `apply_pm_section_to_fragment(doc: pycrdt.Doc, *, section_index: int, blocks: list[dict]) -> bytes` — replace only `section_index`'s top-level block range with `blocks`; return the incremental Yjs update.
  - `_insert_node(parent, index: int, node: dict) -> pycrdt.XmlElement` — `_append_node`'s positional sibling.

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

Then the section-scoped apply. It maps `section_index` to a top-level block range by counting level-2 heading blocks (fence-safe by construction: a `##` inside a fenced block lives inside a single `code_block` node, never a top-level `heading`), then splices. Boundaries are read directly off the live fragment children (XmlElements), not PM JSON. The pycrdt accessors are verified: `child.tag` is the node type string, and `child.attributes.get("level")` returns the heading level as a float (`2.0`), so compare numerically (`2.0 == 2` is `True`; level-3 `3.0 == 2` is `False`):

```python
def _child_is_h2(child) -> bool:
    return (
        isinstance(child, pycrdt.XmlElement)
        and child.tag == "heading"
        and child.attributes.get("level") == 2
    )
```

Then:

```python
def apply_pm_section_to_fragment(
    doc: pycrdt.Doc, *, section_index: int, blocks: list[dict[str, Any]]
) -> bytes:
    """Replace ONLY ``section_index``'s top-level block range in the ``prosemirror``
    XmlFragment with ``blocks`` (the new section's PM blocks), preserving every
    other section's Yjs identity + any concurrent client edits. Returns the
    incremental Yjs update (captured via observer). Safe because the kernel's
    staleness gate proved the target section unchanged since the proposer read it.
    """
    captured: list[bytes] = []
    sub = doc.observe(lambda event: captured.append(event.update))
    try:
        fragment = doc.get(_FRAGMENT_KEY, type=pycrdt.XmlFragment)
        children = list(fragment.children)
        # Boundary starts using the SAME rule as split_sections (H2 blocks).
        starts = [0] + [i for i, c in enumerate(children) if _child_is_h2(c)]
        if starts[1:2] == [0]:
            starts = starts[1:]
        if not 0 <= section_index < len(starts):
            raise IndexError(
                f"section_index {section_index} out of range ({len(starts)} sections)"
            )
        start = starts[section_index]
        end = starts[section_index + 1] if section_index + 1 < len(starts) else len(children)
        with doc.transaction():
            for i in range(end - 1, start - 1, -1):   # delete range back-to-front
                del fragment.children[i]
            for offset, block in enumerate(blocks):    # insert new blocks at `start`
                _insert_node(fragment, start + offset, block)
    finally:
        doc.unobserve(sub)
    return captured[-1] if captured else b""
```

The three new symbols are `_child_is_h2`, `_insert_node`, and `apply_pm_section_to_fragment` — no other helpers are added.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_yjs_section_replace.py -q -p no:cacheprovider`
Expected: PASS (all four cases).

- [ ] **Step 5: Ruff + commit**

```bash
uv run ruff check src/call_manager/proposal_assist/collab/yjs_bridge.py tests/proposal_assist/collab/test_yjs_section_replace.py
git add src/call_manager/proposal_assist/collab/yjs_bridge.py tests/proposal_assist/collab/test_yjs_section_replace.py
git commit -m "feat(collab): apply_pm_section_to_fragment — section-scoped Yjs replace"
```

---

## Task 6: Kernel accepts use the section-scoped apply (Component 2)

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/kernel.py` (prose apply, lines 124-127; citation apply, line 236)
- Test: `tests/proposal_assist/collab/test_kernel_section_scoped.py`

**Interfaces:**
- Consumes: `apply_pm_section_to_fragment(doc, *, section_index, blocks)` (Task 5); `markdown_to_pm`, `split_sections`.
- Produces: no new symbols; both kernel accepts now mutate only the target section's fragment range.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/collab/test_kernel_section_scoped.py`. It seeds a room, applies an unrelated concurrent edit to a DIFFERENT section directly on the doc, then runs a kernel prose accept on the target section, and asserts the concurrent edit survives. Reuse the kernel-accept harness in `tests/proposal_assist/collab/` (see the existing `test_kernel*.py` for `_seed_room_and_pc` / how they build `room_doc` + a `ProposedChange` and call `kernel_accept_prose`). Core shape:

```python
def test_accept_preserves_concurrent_edit_in_other_section(pg_session_factory, tmp_path, ...):
    # Room doc: "Preamble.\n\n## A\n\nBody a.\n\n## B\n\nBody b.\n"
    # Concurrent client edit lands in section B, THEN a kernel accept edits section A.
    # Assert section B's concurrent edit is still present after the accept.
    ...
    apply_pm_section_to_fragment(room_doc, section_index=2, blocks=_blocks_of("## B\n\nBody b CONCURRENT.\n"))
    # ... kernel_accept_prose on section 1 (## A) with base_section_sha = live A sha ...
    out = pm_to_markdown(ydoc_to_pm_json(room_doc))
    assert "Body a EDITED." in out           # accept applied
    assert "Body b CONCURRENT." in out       # concurrent edit in B survived
```

Also add a straightforward regression asserting a normal single-section prose accept still produces the expected whole-doc content (kernel behavior unchanged for the non-concurrent case), and the same for a citation accept.

- [ ] **Step 2: Run the test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_kernel_section_scoped.py -q -p no:cacheprovider`
Expected: FAIL — the whole-fragment `apply_pm_json_to_fragment` wipes section B's concurrent edit.

- [ ] **Step 3: Switch the prose apply to section-scoped**

In `kernel.py`, replace the prose apply (lines 124-127):

```python
    # Apply ONLY the target section's blocks to the live room fragment (Slice 3a
    # C2): the staleness gate above proved this section unchanged since the
    # proposer read it, so a section-scoped replace clobbers nothing on the target
    # and preserves concurrent edits in every OTHER section. `new_text` is one
    # section's markdown; convert it to PM blocks and splice.
    from call_manager.proposal_assist.collab.yjs_bridge import apply_pm_section_to_fragment

    section_blocks = markdown_to_pm(new_text).get("content") or []
    update = apply_pm_section_to_fragment(
        room_doc, section_index=pc.section_index, blocks=section_blocks
    )
```

(Prefer a top-of-file import over the inline import if `apply_pm_section_to_fragment` is not already imported alongside `apply_pm_json_to_fragment` at line 22 — add it there and drop the local import.)

- [ ] **Step 4: Switch the citation apply to section-scoped**

The citation path builds `result["new_proposal_text"]` (whole-doc marker-bearing markdown); the marker change is confined to the anchor's section (`pc.section_index`). Replace the citation apply (line 236):

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

Confirm `split_sections` is imported in `kernel.py` (it is — used by `_live_sections`).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_kernel_section_scoped.py tests/proposal_assist/collab/ -q -p no:cacheprovider`
Expected: PASS (new test + all existing kernel/collab tests — the whole-doc-content regressions confirm no behavior change for the non-concurrent case).

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

In `tests/proposal_assist/collab/test_collab_permissions.py`, add a serialize test and a connect-straddle test:

```python
@pytest.mark.anyio
async def test_rapid_unlock_then_lock_converges(pg_session_factory, tmp_path):
    # Two signals scheduled back-to-back (unlock then lock) must not leave a stale
    # can_write; the final state matches the last committed DB state (locked).
    ws_id, owner_id, member_id = _seed_ws_owner_member(pg_session_factory)
    key = (ws_id, "spec")
    member_c = FakeClient()

    async def _load():
        return []

    await MANAGER.acquire(key, load=_load, client=member_c,
                          state=ClientState(user_id=member_id, can_write=True))
    try:
        # spec starts unlocked → member writable; then lock it and reconcile twice.
        await refresh_permissions(pg_session_factory, str(tmp_path), workspace_id=ws_id, artifact="spec")
        _lock_spec(pg_session_factory, ws_id, owner_id)
        # Fire two overlapping reconciliations; both read fresh DB (locked) → False.
        import asyncio as _a
        await _a.gather(
            refresh_permissions(pg_session_factory, str(tmp_path), workspace_id=ws_id, artifact="spec"),
            refresh_permissions(pg_session_factory, str(tmp_path), workspace_id=ws_id, artifact="spec"),
        )
        assert MANAGER.get(key).clients[member_c].can_write is False
    finally:
        await MANAGER.evict(key)


@pytest.mark.anyio
async def test_reconcile_client_downgrades_on_locked_spec(pg_session_factory, tmp_path):
    # The connect self-reconcile helper flips a just-registered client's can_write
    # when the DB says it should be read-only.
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
        state = room.clients[member_c]
        async with room.recon_lock:
            await reconcile_client(pg_session_factory, str(tmp_path), room, member_c, state,
                                   workspace_id=ws_id, artifact="spec")
        assert room.clients[member_c].can_write is False
    finally:
        await MANAGER.evict(key)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_permissions.py -k "converges or reconcile_client" -q -p no:cacheprovider`
Expected: FAIL — `reconcile_client` / `room.recon_lock` do not exist.

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

In `web/collab_ws.py`, immediately after line 114 (`room = await MANAGER.acquire(...)`), add:

```python
    # Close the connect-straddle TOCTOU (Slice 3a b): a permission mutation that
    # committed while we were mid-acquire may have fired its refresh BEFORE this
    # client was registered. Re-resolve THIS client now under the recon lock so
    # ordering vs a concurrent refresh is well-defined (either refresh saw us, or
    # we see the committed mutation here).
    from call_manager.proposal_assist.collab.permissions import reconcile_client

    async with room.recon_lock:
        await reconcile_client(
            session_factory, workspaces_dir, room, websocket, state,
            workspace_id=workspace_id, artifact=artifact,
        )
    if state.can_write is False and websocket not in room.clients:
        return  # reconcile revoked + deregistered this client; nothing more to do
```

(Prefer a top-of-file import of `reconcile_client` over the inline import if the module style favors it.) Confirm `websocket`, `state`, `session_factory`, `workspaces_dir`, `workspace_id`, `artifact` are all in scope at that point (they are — lines 63/87/113 and the route params).

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
- Modify: `src/call_manager/proposal_assist/collab/room.py` (`release`, lines 261-272; `operate()` finally, lines 250-256)
- Test: `tests/proposal_assist/collab/test_collab_room.py` (or `test_collab_permissions.py`)

**Interfaces:**
- Consumes: `Room.ops`, `Room.clients`, `RoomManager._rooms`, `on_evict` callback.
- Produces: `Room._pending_on_evict` (callback stashed when `release` cannot evict due to `ops > 0`), invoked by `operate()`-exit when it finally evicts.

**Note:** cluster (d) fail-closed downgrade was implemented in Task 7's `reconcile_client`; this task covers cluster (c) only.

- [ ] **Step 1: Write the failing test**

Add to `tests/proposal_assist/collab/test_collab_room.py`:

```python
@pytest.mark.anyio
async def test_last_client_release_during_op_checkpoints_on_op_exit(...):
    # A room with ONE client and an in-flight server op (ops=1). The last client
    # is released with an on_evict checkpoint; because ops>0 the room is NOT
    # evicted yet and on_evict must NOT run. When the op exits (ops->0, no
    # clients), the room is evicted AND the stashed on_evict fires exactly once.
    key = (uuid.uuid4(), "draft")
    fired = {"n": 0}

    async def _on_evict(_room):
        fired["n"] += 1

    async def _load():
        return []

    client = FakeClient()
    async with MANAGER.operate(key, load=_load) as room:   # ops=1
        room.clients[client] = ClientState(user_id=1, can_write=True)
        await MANAGER.release(key, client=client, on_evict=_on_evict)
        assert MANAGER.get(key) is room     # not evicted (ops>0)
        assert fired["n"] == 0              # on_evict deferred
    # operate() exited: ops->0, no clients → evict + fire stashed on_evict
    assert MANAGER.get(key) is None
    assert fired["n"] == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_room.py -k "checkpoints_on_op_exit" -q -p no:cacheprovider`
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

In `operate()`'s `finally`, run the stashed callback when it evicts:

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

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_room.py -q -p no:cacheprovider`
Expected: PASS (new test + existing room tests — ops-refcount eviction behavior unchanged for the no-pending case).

- [ ] **Step 5: Ruff + commit**

```bash
uv run ruff check src/call_manager/proposal_assist/collab/room.py tests/proposal_assist/collab/test_collab_room.py
git add src/call_manager/proposal_assist/collab/room.py tests/proposal_assist/collab/test_collab_room.py
git commit -m "fix(collab): carry deferred last-leave checkpoint through operate-exit (cluster c)"
```

---

## Task 9: Reconciliation — evict blast-radius confirmation (cluster e)

**Files:**
- Modify: `src/call_manager/proposal_assist/proposals/kernel_accept.py` (clarifying comment on the evict arm, lines 109-114)
- Test: `tests/proposal_assist/collab/test_kernel_accept_evict.py`

**Interfaces:**
- Consumes: `_CLEAN_ACCEPT_ERRORS`, `MANAGER.evict`, `MANAGER.operate`.
- Produces: no new symbols; a regression test pins the blast-radius contract.

- [ ] **Step 1: Write the failing/【characterization】tests**

Create `tests/proposal_assist/collab/test_kernel_accept_evict.py`:

```python
# Two contracts (Slice 3a e):
# 1. A PRE-mutation failure (in _CLEAN_ACCEPT_ERRORS) does NOT evict the room —
#    other clients keep their live doc.
# 2. A POST-mutation failure DOES evict (room.doc may be ahead of the rolled-back
#    log; clients reconnect and rebuild clean from the durable log).


@pytest.mark.anyio
async def test_pre_mutation_failure_does_not_evict(pg_session_factory, tmp_path, monkeypatch):
    # Drive accept_via_kernel with a proposal that trips a pre-mutation guard
    # (e.g. a spec-locked artifact -> SpecLockedError, or an out-of-range
    # section_index -> IndexError). Assert the room is still registered after.
    ...
    assert MANAGER.get(key) is not None   # NOT evicted


@pytest.mark.anyio
async def test_post_mutation_failure_evicts(pg_session_factory, tmp_path, monkeypatch):
    # Force checkpoint_artifact (called AFTER the fragment mutation) to raise a
    # non-clean error; assert the room is evicted (MANAGER.get(key) is None).
    import call_manager.proposal_assist.collab.kernel as kmod
    monkeypatch.setattr(kmod, "checkpoint_artifact",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    ...
    with pytest.raises(RuntimeError):
        await accept_via_kernel(...)
    assert MANAGER.get(key) is None       # evicted (post-mutation recovery)
```

Reuse the kernel-accept harness from `tests/proposal_assist/collab/` (the same fixtures Task 6 uses) to build the `ProposedChange` + enabled collab state. For contract 1, pick the cheapest pre-mutation trip: an `IndexError` via a `section_index` beyond the live section count is in `_CLEAN_ACCEPT_ERRORS` and needs no lock setup.

- [ ] **Step 2: Run the tests to verify they behave as specified**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_kernel_accept_evict.py -q -p no:cacheprovider`
Expected: Both PASS against the CURRENT code (this task characterizes + documents existing correct behavior; if either fails, that is a real defect to fix before proceeding).

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
- C1 (three consumers) → Tasks 1 (helper) + 2 (read) + 3 (staleness/#1) + 4 (citation/#3); empty-doc/#2 → Task 1.
- C2 (section-scoped replace) → Tasks 5 (primitive) + 6 (kernel wiring).
- C3 (a) serialize → Task 7; (b) connect self-reconcile → Task 7; (c) pending checkpoint → Task 8; (d) fail-closed → Task 7 (`reconcile_client`); (e) evict blast-radius → Task 9.
- Parity gate + no-migration + flag-off → Task 10.

**2. Placeholder scan:** Tasks 4, 6, 9 intentionally reference existing per-suite harnesses (`tests/proposal_assist/tools/`, `tests/proposal_assist/collab/`) for `ToolContext`/`Reference`/kernel fixtures rather than re-deriving them, and name the exact assertion each test must make; the implementer wires the seeding via the established fixtures. All production-code steps show complete code.

**3. Type consistency:** `canonical_artifact_markdown(session, *, workspace_id, artifact, repo_dir)` used identically in Tasks 2/3/4. `apply_pm_section_to_fragment(doc, *, section_index, blocks)` defined in Task 5, called identically in Task 6. `reconcile_client(session_factory, workspaces_dir, room, client, state, *, workspace_id, artifact)` defined in Task 7, called in Task 7 (route) + Task 7 test. `Room.recon_lock` / `Room._pending_on_evict` consistent across Tasks 7-8. `is_collab_enabled(session, *, workspace_id, artifact)` used identically in Tasks 2/3/4. Artifact strings are `"draft"`/`"spec"` throughout (`str(Artifact.DRAFT) == "draft"`, verified).
