# Collaborative Editing — Slice 2b-1: room-aware write kernel + proposal accept Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route **proposal-accept** for a collab-enabled artifact through a **room-aware write kernel** — validate against the *live Yjs room* (not git), apply the accepted edit as an authenticated incremental Yjs update, checkpoint→commit, and record `accepted` + `resulting_commit_sha` — all in **one atomic transaction**. The git-first accept path stays byte-for-byte unchanged for flag-off workspaces.

**Architecture:** The kernel is the single path for a collab-enabled artifact's accept. It reuses today's validation *semantics* (staleness, spec-lock, H2 structure guard) but sources current state from the **serialized live room** and re-implements the staleness check against the **live section SHA** (not `save.py`'s `head != base_commit_sha` git gate, which is unsafe when Yjs leads git). The accepted edit is applied by **mutating the live room's `prosemirror` XmlFragment in place** (clear + repopulate from the new whole-doc PM-JSON), capturing the **incremental** Yjs update via an observer; that update is staged to the durable log and immediately checkpointed by Slice 2a's `checkpoint_artifact`. Because `checkpoint_artifact` reads the log (not the in-memory doc), the kernel **stages the update before checkpointing**, and the whole sequence (FOR UPDATE row lock → staged update → checkpoint commit → `accepted` status) commits as **one transaction**. A connected client converges via the broadcast incremental update (proven in the pre-plan spike).

**Tech Stack:** Python 3.12, FastAPI/Starlette, `pycrdt` 0.14.0, SQLAlchemy 2.x + PostgreSQL, pytest + testcontainers PG. **No new deps.** **No new migration** (2b-1 reuses `yjs_updates`, `collab_artifact_state`, `proposed_changes`).

## Global Constraints

- **Flag stays OFF in prod (unchanged from 2a); 2b-1 is validated only via tests that call `enable_collab`.** The kernel path is taken **only** when `is_collab_enabled(workspace, artifact)`. Flag-off ⇒ the existing git-first `accept_proposed_change` runs, unchanged. The prod flag-flip is Slice 3 (needs the Tiptap editor).
- **Atomicity is mandatory.** The accept's FOR UPDATE row lock, the staged Yjs update, the checkpoint commit + provenance, and the `accepted` status flip **must commit in a single transaction** (one `session.commit()` at the top). This requires a **non-committing** update-staging call (`store.append_update(..., commit=False)`); the default `commit=True` preserves the Slice-1 `room.handle` behavior.
- **Validation source = the serialized live room, never git.** The staleness check compares `ProposedChange.base_section_sha` to the **live** section SHA (from `split_sections(pm_to_markdown(ydoc_to_pm_json(room_doc)))`). This is the key regression guard (spec Testing §): an edit that is *live but not yet checkpointed* (so git HEAD still equals `base_commit_sha`) must still be detected as stale.
- **Lock order (deadlock-free): room `asyncio.Lock` → `ProposedChange` FOR UPDATE (DB) → commit mutex (DB, acquired in the kernel before the spec-lock guard; re-entered by `checkpoint_artifact`).** (This is the *actual* order: the async endpoint enters `room.lock` first, then dispatches the sync body to a worker thread where it takes the FOR UPDATE row lock and finally the commit mutex.) Deadlock-freedom does not rely on the ordering being uniform — it holds because the only `room.lock` holders (the WS connect read and the kernel accept) never already hold a DB lock when acquiring `room.lock`, and the git-first writers (`save_section`, `human_cite`) never take the room lock at all, so no cycle. The async endpoint holds the room lock across a single `run_in_threadpool` sync body that mutates `room.doc` from the worker thread. **The room `asyncio.Lock` alone is NOT sufficient (Codex H2):** it only serializes coroutines on the event loop, while the kernel mutates the doc on a *worker thread*, and the GIL does not make a multi-step pycrdt mutation atomic against a concurrent read. Therefore **every** `room.doc` access — including the SYNC_STEP1 read in `Room.handle` and `Room.initial_message()` at connect — MUST hold the room lock; the kernel holding it across the threadpool call then excludes all doc reads for the mutation's duration.
- **On any failure after the room doc is mutated, EVICT the room** (Slice-1 poison-recovery): the in-memory doc is ahead of the rolled-back log, so the next open rebuilds cleanly from the log.
- **2b-1 scope = `prose_edit` accept only (draft + spec).** For a collab-enabled artifact, a `citation_insert` accept must raise a clear **deferred-to-2b-2** error (409) rather than fall through to the git-first `_accept_citation_insert` (which would write git while Yjs is canonical → divergence). The spec-**lock** *operation* is also 2b-2; 2b-1 only honors the spec-lock **guard** (reject an accept when the spec is locked).
- **Reuse, don't reinvent:** `_load_scoped` (FOR UPDATE + status gate), `split_sections`/`Section.sha` and the structure-guard helper `is_h2_heading_line` (all in `editing.sectionizer`), `save.py`'s exceptions (`SpecLockedError`, `SectionStructureError`), `acquire_commit_mutex` (`drafting.locking`), `checkpoint_artifact`, `_append_node`. The kernel reuses `write_and_commit_files`/`ArtifactVersion`/`WorkspaceEvent` **transitively via `checkpoint_artifact`** (it does not write git directly). It does **not** reuse `StaleSectionError` — the kernel *returns* a `stale` status rather than raising.
- **Tests** under `tests/proposal_assist/collab/` (kernel) and reuse `tests/proposal_assist/proposals/` fixtures for the accept contract. Whole-suite testcontainers may flake — verify via grouped dir runs. `ruff check .` must pass.

---

## File Structure

- Modify `src/call_manager/proposal_assist/collab/yjs_bridge.py` — add `apply_pm_json_to_fragment(doc, pm_json) -> bytes` (in-place fragment replace + incremental-update capture), factored to share `_append_node`.
- Modify `src/call_manager/proposal_assist/collab/store.py` — `append_update(..., commit: bool = True)`.
- Create `src/call_manager/proposal_assist/collab/kernel.py` — the sync kernel core (`kernel_accept_prose`) + `KernelAcceptResult` + `CitationKernelDeferred`.
- Modify `src/call_manager/proposal_assist/collab/room.py` — `Room.ops` refcount + `Room.lock`/`Room.broadcast`; lock the SYNC_STEP1 read in `Room.handle` (Codex H2); `RoomManager.operate(key, *, load)` async context manager (server-op room lifecycle, ops-aware eviction); `RoomManager.release` defers eviction while `room.ops > 0` (Codex H1).
- Modify `src/call_manager/proposal_assist/web/collab_ws.py` — compute `room.initial_message()` under `room.lock` at connect (Codex H2 read-lock).
- Create `src/call_manager/proposal_assist/proposals/kernel_accept.py` — async `accept_via_kernel(...)` orchestration.
- Modify `src/call_manager/proposal_assist/web/proposals.py` — make `accept` async; route to the kernel when collab-enabled, else the git-first path (unchanged) via `run_in_threadpool`.
- Tests: `test_collab_apply_fragment.py`, `test_collab_store_stage.py`, `test_collab_kernel_accept.py` (sync core), `test_collab_kernel_accept_route.py` (async + route + convergence), plus edits to existing accept tests only if the endpoint signature change requires (it must not change behavior).
- Create `docs/superpowers/notes/2026-07-01-collab-slice2b1-notes.md`.

**Interface summary (locked across tasks):**

```
# yjs_bridge.py (added)
def apply_pm_json_to_fragment(doc: pycrdt.Doc, pm_json: dict) -> bytes
    # clears the "prosemirror" XmlFragment and repopulates it from pm_json["content"]
    # inside one transaction; returns the INCREMENTAL update bytes (via observer).

# store.py (changed)
def append_update(session, *, workspace_id, artifact, actor_id, update, commit: bool = True) -> None
    # commit=False stages the row on the caller's transaction (for atomic kernel writes).

# collab/kernel.py
@dataclass
class KernelAcceptResult:
    pc: ProposedChange
    update: bytes | None      # None on stale / no-op (nothing applied); else the staged update
    committed: bool           # checkpoint produced a new commit
    evict: bool               # True if room.doc was mutated (caller must evict on failure)

class CitationKernelDeferred(Exception): ...   # collab-enabled citation_insert accept (2b-2)

def kernel_accept_prose(
    session, *, room_doc: pycrdt.Doc, proposed_change_id, workspace_id,
    approver: User, workspaces_dir,
) -> KernelAcceptResult
    # FOR UPDATE load + status gate; validate vs serialized room_doc; mutate room_doc;
    # stage update (commit=False); checkpoint_artifact; flip status. Caller COMMITS.

# room.py (added / changed)
class Room:
    ops: int                                       # in-flight server-op refcount (Codex H1)
    @property
    def lock(self) -> asyncio.Lock: ...            # public view of the mutation lock
    async def broadcast(self, frame: bytes) -> None  # to all clients of THIS room
class RoomManager:
    @asynccontextmanager
    async def operate(self, key, *, load) -> AsyncIterator[Room]
        # get-or-create the room WITHOUT registering a client; bumps room.ops;
        # on exit evicts iff no clients AND room.ops == 0 (Codex H1).

# proposals/kernel_accept.py
async def accept_via_kernel(
    *, session_factory, workspaces_dir, workspace_id, pc_id, approver, artifact: str,
) -> ProposedChange
```

---

### Task 1: `apply_pm_json_to_fragment` — in-place fragment replace + incremental capture

**Files:** Modify `collab/yjs_bridge.py`; Test `tests/proposal_assist/collab/test_collab_apply_fragment.py`.

**Interfaces:** Consumes `_append_node` (existing). Produces `apply_pm_json_to_fragment(doc, pm_json) -> bytes`. The proven mechanism (pre-plan spike): clear the `"prosemirror"` fragment's children, repopulate from `pm_json["content"]` in one transaction, capture the incremental update via `doc.observe`.

- [ ] **Step 1: Write the failing test** (the spike, as a regression test — a second client synced to the pre-edit state must converge via the incremental update)

```python
# tests/proposal_assist/collab/test_collab_apply_fragment.py
import pycrdt

from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import (
    apply_pm_json_to_fragment,
    pm_json_to_ydoc,
    ydoc_to_pm_json,
)
from call_manager.proposal_assist.editing.sectionizer import split_sections


def test_apply_fragment_mutates_and_client_converges_via_incremental_update():
    md0 = "preamble\n\n## Background\noriginal body.\n\n## Methods\nmethods body.\n"
    room = pm_json_to_ydoc(markdown_to_pm(md0))

    # a client synced to the room's current (pre-edit) state
    client = pycrdt.Doc()
    client.apply_update(room.get_update())
    assert ydoc_to_pm_json(client) == ydoc_to_pm_json(room)

    # replace section 1 (Background) — whole-doc PM-JSON
    secs = split_sections(pm_to_markdown(ydoc_to_pm_json(room)))
    new_text = "## Background\nREWRITTEN.\n\n"
    new_md = "".join(new_text if s.index == 1 else s.text for s in secs)
    new_pm = markdown_to_pm(new_md)

    update = apply_pm_json_to_fragment(room, new_pm)
    assert update is not None and len(update) > 0

    room_md = pm_to_markdown(ydoc_to_pm_json(room))
    assert "REWRITTEN." in room_md and "methods body." in room_md
    # split_sections invariant holds on the mutated serialization
    assert "".join(s.text for s in split_sections(room_md)) == room_md

    # the client converges to the SAME doc using ONLY the incremental update
    client.apply_update(update)
    assert ydoc_to_pm_json(client) == ydoc_to_pm_json(room)


def test_apply_fragment_to_empty_doc_content():
    # replacing with the canonical empty doc must not raise
    room = pm_json_to_ydoc(markdown_to_pm("## X\nbody.\n"))
    update = apply_pm_json_to_fragment(room, {"type": "doc", "content": [{"type": "paragraph"}]})
    assert update is not None
    assert ydoc_to_pm_json(room) == {"type": "doc", "content": [{"type": "paragraph"}]}
```

- [ ] **Step 2: Run — expect FAIL** (`ImportError: cannot import name 'apply_pm_json_to_fragment'`).

- [ ] **Step 3: Implement in `yjs_bridge.py`** (append near `pm_json_to_ydoc`; reuse `_append_node` + `_FRAGMENT_KEY`)

```python
def apply_pm_json_to_fragment(doc: pycrdt.Doc, pm_json: dict) -> bytes:
    """Replace the doc's ``"prosemirror"`` XmlFragment content with ``pm_json``
    (a ProseMirror ``doc`` node) in one transaction, returning the INCREMENTAL
    Yjs update that effects the change.

    Used by the room-aware write kernel to apply a whole-doc replacement (the
    section-scoped edit reassembled into the full doc) to a LIVE room: a client
    synced to the pre-edit state converges by applying this update. The update
    is captured via an observer (NOT ``get_update()``, which encodes full state).
    """
    if pm_json.get("type") != "doc":
        raise ValueError(f"expected a ProseMirror doc node, got type={pm_json.get('type')!r}")
    captured: list[bytes] = []
    sub = doc.observe(lambda event: captured.append(event.update))
    try:
        fragment = doc.get(_FRAGMENT_KEY, type=pycrdt.XmlFragment)
        with doc.transaction():
            while len(fragment.children) > 0:
                del fragment.children[0]
            for block in pm_json.get("content") or []:
                _append_node(fragment, block)
    finally:
        doc.unobserve(sub)
    # One transaction => one observer callback with the coalesced incremental update.
    return captured[-1] if captured else b""
```

- [ ] **Step 4: Run — expect PASS.** `uv run pytest tests/proposal_assist/collab/test_collab_apply_fragment.py -q`

- [ ] **Step 5: Commit** (`feat(collab): apply_pm_json_to_fragment — in-place fragment replace + incremental capture`).

---

### Task 2: `store.append_update(commit=False)` staging variant

**Files:** Modify `collab/store.py`; Test `tests/proposal_assist/collab/test_collab_store_stage.py`.

**Interfaces:** `append_update(session, *, workspace_id, artifact, actor_id, update, commit: bool = True)`. Default preserves the Slice-1 per-update immediate commit (`room.handle`). `commit=False` stages the row on the caller's transaction so the kernel commits everything atomically.

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/collab/test_collab_store_stage.py
from call_manager.proposal_assist.collab import store
from tests.proposal_assist.conftest import make_user, make_workspace


def test_append_update_commit_false_defers_to_caller(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="stg-o@ki.se"))
        s.commit()
        wid = ws.id
    # stage but roll back -> nothing persisted
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=None, update=b"\x01", commit=False
        )
        s.rollback()
    with pg_session_factory() as s:
        assert store.load_updates(s, workspace_id=wid, artifact="draft") == []
    # stage then commit -> persisted; and the same-session read sees its own staged row
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=None, update=b"\x02", commit=False
        )
        assert store.load_updates(s, workspace_id=wid, artifact="draft") == [b"\x02"]  # own uncommitted write
        s.commit()
    with pg_session_factory() as s:
        assert store.load_updates(s, workspace_id=wid, artifact="draft") == [b"\x02"]
```

- [ ] **Step 2: Run — expect FAIL** (`TypeError: append_update() got an unexpected keyword argument 'commit'`).

- [ ] **Step 3: Implement** — change `store.append_update`'s signature + body:

```python
def append_update(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    artifact: str,
    actor_id: int | None,
    update: bytes,
    commit: bool = True,
) -> None:
    """Append one Yjs update for a room, tagged with the sending user.

    ``commit=True`` (default) commits immediately — the Slice-1 ``room.handle``
    per-update persistence contract. ``commit=False`` stages the row on the
    caller's transaction (the write kernel commits the update + checkpoint +
    accept status flip together, atomically)."""
    session.add(
        YjsUpdate(
            workspace_id=workspace_id,
            artifact=artifact,
            actor_id=actor_id,
            update_bytes=update,
        )
    )
    if commit:
        session.commit()
    else:
        session.flush()
```

- [ ] **Step 4: Run — expect PASS**, then re-run `test_collab_store.py` + `test_collab_ws_route.py` to confirm the default path is unchanged.

- [ ] **Step 5: Commit** (`feat(collab): store.append_update commit=False staging variant`).

---

### Task 3: Kernel core — `kernel_accept_prose` (sync, the meat)

**Files:** Create `collab/kernel.py`; Test `tests/proposal_assist/collab/test_collab_kernel_accept.py`.

**Interfaces:** Consumes `_load_scoped` (proposals.accepting), `SpecLockedError`/`SectionStructureError` (editing.save), `is_h2_heading_line`/`split_sections` (editing.sectionizer), `acquire_commit_mutex` (drafting.locking), `ydoc_to_pm_json`/`apply_pm_json_to_fragment` (yjs_bridge), `pm_to_markdown`/`markdown_to_pm` (markdown_bridge), `store.append_update(commit=False)`, `checkpoint_artifact` (collab.checkpoint). Produces `kernel_accept_prose`, `KernelAcceptResult`, `CitationKernelDeferred`.

**The sequence** (all on ONE caller session; caller commits): FOR UPDATE load + status gate → confirm `kind`/artifact routing (defer citation) → serialize `room_doc` → `split_sections` → **stale check vs live section SHA** → spec-lock guard → structure guard → **no-op check** → mutate `room_doc` (capture update) → stage update (`commit=False`, actor=approver) → `checkpoint_artifact` → flip `accepted` + `resulting_commit_sha`.

- [ ] **Step 1: Write the failing tests** (validate against the LIVE room, not git — the regression guard)

```python
# tests/proposal_assist/collab/test_collab_kernel_accept.py
import subprocess
import uuid as uuidlib
from pathlib import Path

from sqlalchemy import select

from call_manager.db.models import ArtifactVersion, ProposedChange, WorkspaceEvent
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.hydrate import build_doc, load_or_hydrate
from call_manager.proposal_assist.collab.kernel import kernel_accept_prose
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import apply_pm_json_to_fragment, ydoc_to_pm_json
from call_manager.proposal_assist.editing.sectionizer import split_sections
from tests.proposal_assist.conftest import make_user, make_workspace


def _init_repo(repo: Path, content: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _live_room(session, wid, workspaces_dir):
    """Hydrate + return the collab room doc (seeded from HEAD, persisted as update #1)."""
    updates = load_or_hydrate(session, workspace_id=wid, artifact="draft", workspaces_dir=workspaces_dir)
    session.commit()
    return build_doc(updates)


def _section_sha(md, index):
    return split_sections(md)[index].sha


def _mk_pc(session, wid, *, section_index, new_text, base_commit_sha, base_section_sha, kind="prose_edit", artifact="draft"):
    pc = ProposedChange(
        id=uuidlib.uuid4(),
        workspace_id=wid,
        status="pending",
        target_artifact=artifact,
        section_index=section_index,
        base_commit_sha=base_commit_sha,
        base_section_sha=base_section_sha,
        kind=kind,
        payload={"new_text": new_text},
        origin_kind="human",
    )
    session.add(pc)
    session.commit()
    return pc.id


def test_kernel_accept_applies_edit_and_checkpoints(pg_session_factory, tmp_path):
    md0 = "## Background\noriginal.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="k-accept@ki.se")
        ws = make_workspace(s, owner=o, current_phase="active")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, md0)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    with pg_session_factory() as s:
        room_doc = _live_room(s, wid, str(tmp_path))
    base_sha = _section_sha(md0, 0)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    with pg_session_factory() as s:
        pc_id = _mk_pc(s, wid, section_index=0, new_text="## Background\nACCEPTED body.\n",
                       base_commit_sha=head, base_section_sha=base_sha)

    with pg_session_factory() as s:
        approver = s.get(type(o), oid)
        res = kernel_accept_prose(
            s, room_doc=room_doc, proposed_change_id=pc_id, workspace_id=wid,
            approver=approver, workspaces_dir=str(tmp_path),
        )
        s.commit()
        assert res.pc.status == "accepted"
        assert res.pc.resulting_commit_sha and res.committed is True
        assert res.update is not None

    # git HEAD now holds the accepted content; the room doc reflects it
    disk = (repo / PROPOSAL_FILENAME).read_text()
    assert "ACCEPTED body." in disk
    assert "".join(sec.text for sec in split_sections(disk)) == disk
    assert "ACCEPTED body." in pm_to_markdown(ydoc_to_pm_json(room_doc))
    with pg_session_factory() as s:
        assert res.pc.resulting_commit_sha == s.get(ProposedChange, pc_id).resulting_commit_sha
        av = s.execute(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid)).scalars().all()
        assert len(av) == 1 and av[0].created_by == "user"
        # the update was tagged to the approver
        rows = store.load_rows(s, workspace_id=wid, artifact="draft")
        assert rows[-1][1] == oid


def test_kernel_accept_detects_staleness_against_LIVE_room_not_git(pg_session_factory, tmp_path):
    # THE regression guard: an edit that is live-but-not-checkpointed leaves git
    # HEAD == base_commit_sha, yet the accept must detect the live section changed.
    md0 = "## Background\noriginal.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="k-stale@ki.se")
        ws = make_workspace(s, owner=o, current_phase="active")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, md0)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    with pg_session_factory() as s:
        room_doc = _live_room(s, wid, str(tmp_path))
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    base_sha = _section_sha(md0, 0)  # proposal's base = the ORIGINAL section

    # a live edit lands in the room + store, but is NOT checkpointed (git HEAD unchanged)
    live_md = pm_to_markdown(ydoc_to_pm_json(room_doc))
    secs = split_sections(live_md)
    edited = "".join("## Background\nlive edit, uncommitted.\n" if x.index == 0 else x.text for x in secs)
    live_update = apply_pm_json_to_fragment(room_doc, markdown_to_pm(edited))
    with pg_session_factory() as s:
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=oid, update=live_update)
    # git HEAD is still the original:
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip() == head

    with pg_session_factory() as s:
        pc_id = _mk_pc(s, wid, section_index=0, new_text="## Background\nproposed.\n",
                       base_commit_sha=head, base_section_sha=base_sha)
    with pg_session_factory() as s:
        approver = s.get(type(o), oid)
        res = kernel_accept_prose(
            s, room_doc=room_doc, proposed_change_id=pc_id, workspace_id=wid,
            approver=approver, workspaces_dir=str(tmp_path),
        )
        s.commit()
        assert res.pc.status == "stale"        # live section SHA != base_section_sha
        assert res.update is None and res.evict is False
    # git untouched, nothing accepted
    assert "proposed." not in (repo / PROPOSAL_FILENAME).read_text()


def test_kernel_accept_noop_proposal_accepts_without_new_commit(pg_session_factory, tmp_path):
    md0 = "## Background\nsame.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="k-noop@ki.se")
        ws = make_workspace(s, owner=o, current_phase="active")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, md0)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    with pg_session_factory() as s:
        room_doc = _live_room(s, wid, str(tmp_path))
    live_md = pm_to_markdown(ydoc_to_pm_json(room_doc))
    live_sec0 = split_sections(live_md)[0]
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    with pg_session_factory() as s:
        # propose EXACTLY the live text -> no-op
        pc_id = _mk_pc(s, wid, section_index=0, new_text=live_sec0.text,
                       base_commit_sha=head, base_section_sha=live_sec0.sha)
    with pg_session_factory() as s:
        approver = s.get(type(o), oid)
        res = kernel_accept_prose(
            s, room_doc=room_doc, proposed_change_id=pc_id, workspace_id=wid,
            approver=approver, workspaces_dir=str(tmp_path),
        )
        s.commit()
        assert res.pc.status == "accepted"     # no-op still accepts
        assert res.committed is False and res.update is None
    with pg_session_factory() as s:
        av = s.execute(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid)).scalars().all()
        assert av == []                        # no new version
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: ...collab.kernel`).

- [ ] **Step 3: Implement `collab/kernel.py`**

```python
# src/call_manager/proposal_assist/collab/kernel.py
"""Room-aware write kernel (Slice 2b-1): accept a prose proposal against the
LIVE Yjs room, not git. Validates staleness against the live section SHA,
applies the accepted edit as an authenticated incremental Yjs update, stages it,
and checkpoints — all on the caller's single transaction (the caller commits)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from pycrdt import Doc
from sqlalchemy.orm import Session

from call_manager.db.models import ProposedChange, User, Workspace
from call_manager.proposal_assist.collab import store
from call_manager.proposal_assist.collab.checkpoint import _head_or_empty, checkpoint_artifact
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import (
    apply_pm_json_to_fragment,
    ydoc_to_pm_json,
)
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex
from call_manager.proposal_assist.editing.save import SectionStructureError, SpecLockedError
from call_manager.proposal_assist.editing.sectionizer import is_h2_heading_line, split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.proposals.accepting import _load_scoped


class CitationKernelDeferred(Exception):
    """A citation_insert accept on a collab-enabled artifact. Routing this through
    the kernel is Slice 2b-2; until then it must NOT fall through to the git-first
    path (which would write git while Yjs is canonical). Endpoint maps to 409."""


@dataclass
class KernelAcceptResult:
    pc: ProposedChange
    update: bytes | None
    committed: bool
    evict: bool


def _live_sections(room_doc: Doc):
    return split_sections(pm_to_markdown(ydoc_to_pm_json(room_doc)))


def kernel_accept_prose(
    session: Session,
    *,
    room_doc: Doc,
    proposed_change_id: uuid.UUID,
    workspace_id: uuid.UUID,
    approver: User,
    workspaces_dir,
) -> KernelAcceptResult:
    pc = _load_scoped(session, proposed_change_id, workspace_id)  # FOR UPDATE
    if pc.status != "pending":
        from call_manager.proposal_assist.proposals.accepting import ProposedChangeNotPending

        raise ProposedChangeNotPending(pc.status)
    if pc.kind != "prose_edit":
        # citation_insert (and any future kind) is 2b-2 on the collab path.
        raise CitationKernelDeferred(pc.kind)

    artifact = pc.target_artifact
    sections = _live_sections(room_doc)
    if not 0 <= pc.section_index < len(sections):
        raise IndexError(f"section_index {pc.section_index} out of range")
    live = sections[pc.section_index]

    # THE regression guard: staleness vs the LIVE section SHA (never git HEAD).
    if live.sha != pc.base_section_sha:
        pc.status = "stale"
        pc.resolved_by = approver.id
        pc.resolved_at = datetime.now(UTC)
        session.flush()
        return KernelAcceptResult(pc=pc, update=None, committed=False, evict=False)

    # Acquire the commit mutex NOW — before reading spec_locked_at (Codex H3).
    # post_lock_spec (workspaces.py:487) sets the lock UNDER this same mutex; checking
    # spec_locked_at before taking the mutex would let a concurrent lock commit between
    # our guard and our checkpoint, accepting into a spec that just got locked. Taking
    # it here freezes the lock state for the rest of our transaction. checkpoint_artifact
    # re-acquires the same xact lock later — Postgres advisory xact locks are re-entrant
    # within a transaction, so that is an instant no-op (no deadlock, no second timeout).
    # Raised BEFORE any doc mutation, so a CommitMutexTimeout needs no room eviction
    # (it is in _CLEAN_ACCEPT_ERRORS).
    acquire_commit_mutex(session, workspace_id)

    # Spec-lock guard (the lock OPERATION is 2b-2; here we only honor an existing lock).
    if artifact == "spec":
        ws = session.get(Workspace, workspace_id)
        session.refresh(ws, ["spec_locked_at"])
        if ws.spec_locked_at is not None:
            raise SpecLockedError(str(workspace_id))

    new_text = pc.payload["new_text"]

    # Structure guard (mirror save.py): non-preamble edits start with an H2 heading;
    # the preamble (index 0) must not.
    first_line = new_text.split("\n", 1)[0] if new_text else ""
    if pc.section_index > 0:
        if not is_h2_heading_line(first_line):
            raise SectionStructureError(
                f"section {pc.section_index} edit must start with an H2 heading (## …)"
            )
    elif new_text and is_h2_heading_line(first_line):
        raise SectionStructureError("preamble (section 0) edit must not start with an H2 heading")

    # Newline glue for a non-final section (mirror save.py).
    if pc.section_index < len(sections) - 1 and not new_text.endswith("\n"):
        new_text += "\n"

    # No-op: proposing exactly the live text — accept at the current checkpoint,
    # no new commit (mirrors save_section's changed=False short-circuit).
    if new_text == live.text:
        repo = workspace_repo_dir(workspaces_dir, workspace_id)
        pc.status = "accepted"
        pc.resolved_by = approver.id
        pc.resolved_at = datetime.now(UTC)
        pc.resulting_commit_sha = _head_or_empty(repo)
        session.flush()
        return KernelAcceptResult(pc=pc, update=None, committed=False, evict=False)

    # Apply: reassemble the whole doc with the section replaced, mutate the live
    # room fragment (capturing the incremental update), stage it, checkpoint.
    new_doc_md = "".join(new_text if s.index == pc.section_index else s.text for s in sections)
    update = apply_pm_json_to_fragment(room_doc, markdown_to_pm(new_doc_md))
    # From here on room_doc is mutated: any failure must evict the room.
    store.append_update(
        session, workspace_id=workspace_id, artifact=artifact,
        actor_id=approver.id, update=update, commit=False,
    )
    result = checkpoint_artifact(
        session, workspace_id=workspace_id, artifact=artifact, workspaces_dir=workspaces_dir
    )
    pc.status = "accepted"
    pc.resolved_by = approver.id
    pc.resolved_at = datetime.now(UTC)
    pc.resulting_commit_sha = result.commit_sha
    session.flush()
    return KernelAcceptResult(pc=pc, update=update, committed=result.committed, evict=True)
```

- [ ] **Step 4: Run — expect PASS** (all three kernel tests). If `is_h2_heading_line` is not exported from `editing.save`, import it from its defining module (verify with `grep -rn "def is_h2_heading_line" src/`).

- [ ] **Step 5: Commit** (`feat(collab): kernel_accept_prose — room-aware prose accept core`).

---

### Task 4: Async orchestration — `RoomManager.operate` + `accept_via_kernel` + route

**Files:** Modify `collab/room.py`; Create `proposals/kernel_accept.py`; Modify `web/proposals.py`; Test `tests/proposal_assist/collab/test_collab_kernel_accept_route.py`.

**Interfaces:** `RoomManager.operate(key, *, load)` (async CM, ops-aware eviction), `Room.lock`/`Room.broadcast(frame)`; `accept_via_kernel(*, session_factory, workspaces_dir, workspace_id, pc_id, approver, artifact)`.

- [ ] **Step 1: Room read-locking + `ops` refcount + accessors + `RoomManager.operate`** (in `room.py`). Add `from contextlib import asynccontextmanager` to the module imports.

  **(a) `Room.__init__`** — add a server-op refcount beside the client set:

```python
    def __init__(self, key: RoomKey) -> None:
        self.key = key
        self.doc = Doc()
        self.clients: set = set()
        self.ops = 0            # in-flight server ops (kernel accepts) — Codex H1
        self.poisoned = False
        self._lock = asyncio.Lock()
```

  **(b) Lock the SYNC_STEP1 read path** (Codex H2). The kernel now mutates `self.doc` from a *worker thread* under `self._lock`; an unlocked read on the event-loop thread would race it. Wrap the STEP1 doc read in the lock (the write path already holds it at `room.py:97`); keep the `send_bytes` OUTSIDE the lock:

```python
        if P.is_sync_step1(message):
            async with self._lock:
                try:
                    reply = P.handle_sync_message(message[1:], self.doc)
                except Exception:
                    logger.warning(
                        "collab.malformed_step1",
                        extra={"event": "collab_malformed_step1", "room": str(self.key)},
                    )
                    return
            if reply is not None:
                await sender.send_bytes(reply)
            return
```

  **(c) Public accessors** — the kernel serializes against `Room.handle` on the same `_lock`, and broadcasts the accept to clients:

```python
    # --- add to class Room ---
    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    async def broadcast(self, frame: bytes) -> None:
        await self._broadcast(frame, exclude=None)
```

  **(d) `RoomManager.operate`** mirrors `acquire`'s exact get-or-create (create under the per-key lock, register under `_lock`) but registers **no client**; it bumps `room.ops` where `acquire` would `clients.add`, and on exit evicts iff **no client AND no other server op** remains. The `ops` guard closes Codex H1: without it, op A's `finally` could pop the room while op B still runs on it, so a client connecting in that window would hydrate a *fresh* room from the log and miss B's not-yet-committed update. Do NOT refactor `acquire`:

```python
    @asynccontextmanager
    async def operate(self, key: RoomKey, *, load: LoadFn):
        """Get-or-create a room for a SERVER operation (no client registered).
        Tracks an in-flight server-op count so eviction is deferred until BOTH
        no clients AND no other server op remain (Codex H1). Does NOT checkpoint
        on evict — the kernel op already checkpointed within its transaction."""
        async with self._lock:
            room = self._rooms.get(key)
            if room is not None:
                room.ops += 1
            else:
                keylock = self._keylocks.setdefault(key, asyncio.Lock())
        if room is None:
            async with keylock:
                async with self._lock:
                    room = self._rooms.get(key)
                if room is None:
                    room = Room(key)
                    for update in await load():   # OUTSIDE the manager lock
                        room.doc.apply_update(update)
                    async with self._lock:
                        self._rooms[key] = room
                        room.ops += 1
                else:
                    async with self._lock:
                        room.ops += 1
        try:
            yield room
        finally:
            async with self._lock:
                room.ops -= 1
                if not room.clients and room.ops == 0 and self._rooms.get(key) is room:
                    self._rooms.pop(key, None)
```

  **(e) `RoomManager.release`** — defer client-eviction while a server op is in flight, so a last-client-leave during an accept cannot pop the room out from under the op (the op's own `operate` exit evicts once `ops` reaches 0). Change only the `evicted =` line:

```python
            room.clients.discard(client)
            evicted = self._rooms.pop(key) if (not room.clients and room.ops == 0) else None
```

  **(f) Connect-site read-lock** (in `web/collab_ws.py`, Codex H2). The connect sends `room.initial_message()` (currently at `collab_ws.py:150`, an unlocked `self.doc` read). Compute it under the room lock, then send outside:

```python
    room = await MANAGER.acquire(key, load=_load, client=websocket)
    try:
        async with room.lock:
            initial = room.initial_message()
        await websocket.send_bytes(initial)
        async for message in websocket.iter_bytes():
            ...
```

  **Implementer note:** `@asynccontextmanager` requires `operate` to be an async generator (`yield` the room, never `return` a value). `Room(key)`, `LoadFn`, and `RoomKey` are the exact idioms from `acquire` (`room.py:152-183`) — the `Room` constructor takes the key positionally and builds its own `Doc`; `room.doc.apply_update` replays the log. The `else: room.ops += 1` branch handles another creator finishing while we waited on `keylock` (mirrors `acquire`'s final `clients.add`). `keylock` is only referenced under `if room is None:`, the same branch that binds it — no unbound-name risk. Confirm against the actual `acquire`/`release` before writing.

- [ ] **Step 2: `proposals/kernel_accept.py`** (async orchestration; holds the room lock across one threadpool sync body; evict-on-failure)

```python
# src/call_manager/proposal_assist/proposals/kernel_accept.py
"""Async orchestration for a room-aware proposal accept (Slice 2b-1)."""

from __future__ import annotations

import uuid

from starlette.concurrency import run_in_threadpool

from call_manager.db.models import ProposedChange, User
from call_manager.proposal_assist.collab import protocol as P
from call_manager.proposal_assist.collab.hydrate import load_or_hydrate
from call_manager.proposal_assist.collab.kernel import (
    CitationKernelDeferred,
    KernelAcceptResult,
    kernel_accept_prose,
)
from call_manager.proposal_assist.collab.room import MANAGER
from call_manager.proposal_assist.drafting.locking import CommitMutexTimeout
from call_manager.proposal_assist.editing.save import SectionStructureError, SpecLockedError
from call_manager.proposal_assist.proposals.accepting import ProposedChangeNotPending

# These are all raised BEFORE any fragment mutation (validation guards, plus the
# commit-mutex acquire that precedes the spec-lock guard — Codex H3), so the room
# doc is untouched and there is nothing to evict. Every OTHER exception may have
# left room.doc mutated while the txn rolled back → evict to rebuild from the log.
_CLEAN_ACCEPT_ERRORS = (
    SpecLockedError,
    SectionStructureError,
    CitationKernelDeferred,
    ProposedChangeNotPending,
    CommitMutexTimeout,
    IndexError,
)


async def accept_via_kernel(
    *, session_factory, workspaces_dir, workspace_id: uuid.UUID, pc_id: uuid.UUID,
    approver: User, artifact: str,
) -> ProposedChange:
    key = (workspace_id, artifact)

    async def _load() -> list[bytes]:
        def _read():
            with session_factory() as s:
                updates = load_or_hydrate(
                    s, workspace_id=workspace_id, artifact=artifact, workspaces_dir=workspaces_dir
                )
                s.commit()
                return updates
        return await run_in_threadpool(_read)

    async with MANAGER.operate(key, load=_load) as room:
        try:
            async with room.lock:
                def _body() -> KernelAcceptResult:
                    with session_factory() as s:
                        res = kernel_accept_prose(
                            s, room_doc=room.doc, proposed_change_id=pc_id,
                            workspace_id=workspace_id, approver=approver,
                            workspaces_dir=workspaces_dir,
                        )
                        # Commit the ONE transaction, then detach the pc for return.
                        s.commit()
                        s.refresh(res.pc)
                        s.expunge(res.pc)
                        return res

                result = await run_in_threadpool(_body)
                if result.update is not None:
                    await room.broadcast(P.create_update_message(result.update))
            return result.pc
        except _CLEAN_ACCEPT_ERRORS:
            raise  # validation error before any mutation — room doc untouched
        except Exception:
            await MANAGER.evict(key)  # room.doc may be ahead of the rolled-back log
            raise
```

  **Implementer note:** `Room.lock`/`Room.broadcast` are the accessors added in Step 1; `room.doc` is the Slice-1 name. The `StaleSectionError` path is **returned, not raised** (status flips to `stale`, txn commits), so it is not in `_CLEAN_ACCEPT_ERRORS` and needs no eviction. Verify `KernelAcceptResult.evict` is still emitted (it is informational for logging; the eviction decision here is exception-based, which is strictly safer).

- [ ] **Step 3: Route the endpoint** (`web/proposals.py`) — make `accept` async; branch on the flag; preserve the git-first path and its exception mapping verbatim. Read the current sync endpoint first; then restructure to:

```python
@router.post("/{workspace_id}/proposed-changes/{pc_id}/accept")
async def accept(workspace_id: uuid.UUID, pc_id: uuid.UUID, request: Request, user=Depends(get_current_user)):  # noqa: B008
    session_factory = request.app.state.session_factory
    workspaces_dir = request.app.state.settings.workspaces_dir

    def _route() -> tuple[bool, str]:
        with session_factory() as s:
            require_member(workspace_id=workspace_id, user=user, session=s)
            pc = s.get(ProposedChange, pc_id)
            if pc is None or pc.workspace_id != workspace_id:
                raise HTTPException(status_code=404, detail="proposed change not found")
            enabled = collab_state.is_collab_enabled(s, workspace_id=workspace_id, artifact=pc.target_artifact)
            return enabled, pc.target_artifact

    enabled, artifact = await run_in_threadpool(_route)

    if enabled:
        try:
            pc = await accept_via_kernel(
                session_factory=session_factory, workspaces_dir=workspaces_dir,
                workspace_id=workspace_id, pc_id=pc_id, approver=user, artifact=artifact,
            )
        except CitationKernelDeferred as exc:
            raise HTTPException(status_code=409, detail="citation accept on the collab editor lands in a later slice") from exc
        except SpecLockedError as exc:
            raise HTTPException(status_code=409, detail="spec is locked — unlock to accept") from exc
        except SectionStructureError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ProposedChangeNotPending as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CommitMutexTimeout as exc:
            raise HTTPException(status_code=429, detail="workspace busy; retry", headers={"Retry-After": "2"}) from exc
        except GitCommitError as exc:
            raise HTTPException(status_code=500, detail="save failed — nothing was written") from exc
        except IndexError as exc:
            raise HTTPException(status_code=404, detail="no such section") from exc
        return _serialize(pc)

    # flag OFF: the existing git-first accept, verbatim, offloaded to a thread.
    return await run_in_threadpool(_accept_git_first, session_factory, workspace_id, pc_id, user, workspaces_dir)
```

  where `_accept_git_first` wraps today's body (open session → `accept_proposed_change` → the existing try/except mapping → `session.commit()` → `_serialize`) with **no behavior change**. Keep the two exception-mapping blocks consistent. Verify `request.app.state.session_factory` exists (used by the collab routes); if the module currently uses a `_session_dep`, switch this route to `session_factory` like `collab_checkpoint.py`.

- [ ] **Step 4: Write the failing integration test** (route + convergence + git-first parity)

```python
# tests/proposal_assist/collab/test_collab_kernel_accept_route.py
# Uses the _app fixture + TestClient. Enable collab; create a repo + a pending
# prose_edit proposal; POST accept; assert 200 accepted + git committed + a
# connected WS client receives the update and converges. Also: a second app
# (flag OFF) accepts via the git-first path unchanged.
```

  Cases (mirror `test_collab_checkpoint_triggers.py` client/cookie/`_init_repo` patterns):
  - **kernel happy path via HTTP:** enable collab, repo + pending prose proposal (base = HEAD section sha), `POST .../accept` as owner → 200, `status=accepted`, `resulting_commit_sha` set, git file shows the accepted text, one `ArtifactVersion`.
  - **live client converges:** connect a WS client to `/api/ws/workspaces/{id}/draft`, sync; from another task `POST accept`; assert the client receives a sync-update frame and, after applying it, `ydoc_to_pm_json` matches the accepted content. (Serialize the ordering with the existing `TestClient` WS pattern.)
  - **stale vs live:** apply a live (uncheckpointed) edit via a WS client first, then accept a proposal whose base is the pre-edit sha → 200 with `status=stale`, git unchanged.
  - **citation deferred:** enable collab, a pending `citation_insert` proposal → `POST accept` → 409.
  - **flag-off parity:** without `enable_collab`, `POST accept` of a prose proposal → the git-first path still returns 200 accepted (existing `test_accepting`-style assertions).

- [ ] **Step 5: Run** the route suite; then re-run the existing `tests/proposal_assist/proposals/` accept suite to confirm the flag-off path is unchanged.

- [ ] **Step 6: Commit** (`feat(collab): route proposal accept through the write kernel when collab-enabled`).

---

### Task 5: Accept race + OpenAPI + regression sweep

**Files:** Test `tests/proposal_assist/collab/test_collab_kernel_accept_route.py` (add); regen OpenAPI.

- [ ] **Step 1: Accept-race test** — two concurrent kernel accepts of the same pending proposal: the `FOR UPDATE` row lock + status gate makes the second a clean `409 not pending` (no double apply, one `ArtifactVersion`). Drive it with two threads against the sync `kernel_accept_prose` sharing a workspace (mirror any existing concurrency test; if deterministic threading is impractical under testcontainers, assert the serialized invariant: after two sequential accepts of the same row, the second raises `ProposedChangeNotPending` and no second commit lands).

- [ ] **Step 2: `RoomManager.operate`/`release` ops-refcount unit tests** (`tests/proposal_assist/collab/test_collab_room_operate.py`, pure asyncio — no DB — so they are deterministic and fast). Assert Codex H1 is closed. Use a fresh `RoomManager()` per test (not the module `MANAGER`) and an async no-op loader `async def _load(): return []`:
  - **overlapping ops share one room, deferred evict:** enter two overlapping `operate(key)` contexts on the same key; assert both yield the *same* `Room` object and `room.ops == 2`; exit the first (`__aexit__`) — assert the room is STILL registered (`mgr.has_room(key)`) because `ops == 1`; exit the second — assert it is now evicted (`not mgr.has_room(key)`).
  - **release during an op does not evict:** inside one `operate(key)` context (`ops == 1`), `await mgr.acquire(key, load=_load, client=c)` then `await mgr.release(key, client=c)`; assert the room survives (a server op is in flight); on `operate` exit assert it is evicted.
  - Drive the overlapping contexts with explicit `__aenter__()`/`__aexit__()` calls (or `AsyncExitStack`) so the interleaving is deterministic — do not rely on `async with` nesting timing.

- [ ] **Step 3: OpenAPI** — the accept route's signature/shape is unchanged (same path, same `dict` response), so `pnpm gen:api` should show **no** `schema.gen.ts` drift. Run `cd frontend && pnpm gen:api && git diff --exit-code src/api/schema.gen.ts` and confirm clean; if the async change altered the response model, regen + commit.

- [ ] **Step 4: Regression sweep** — `uv run pytest tests/proposal_assist/collab/ tests/proposal_assist/proposals/ -q` green; `uv run ruff check .`.

- [ ] **Step 5: Commit** (`test(collab): accept-race + room-op refcount + kernel-accept regression sweep`).

---

### Task 6: Notes

**Files:** Create `docs/superpowers/notes/2026-07-01-collab-slice2b1-notes.md`.

- [ ] **Step 1: Write the notes** — what shipped (the write-kernel prose-accept path; validation vs live room; atomic single-txn accept = FOR UPDATE + staged Yjs update + checkpoint + status; `apply_pm_json_to_fragment` incremental capture; `RoomManager.operate` server-op lifecycle; evict-on-failure; **concurrency hardening from the Codex plan review — room-doc read-locking on the STEP1 read + connect send (H2), `Room.ops` refcount so a concurrent op / last-client-leave never evicts a room out from under an in-flight accept (H1), and commit-mutex-acquired-before-the-spec-lock-guard so a racing `/spec/lock` cannot slip in between the guard and the checkpoint (H3)**); the invariants proven by tests (stale-vs-live regression guard, no-op accepts, race, client convergence, ops-refcount deferred eviction); **flag still OFF in prod**; deferrals — **citation-insert accept + spec-lock operation → 2b-2**; **git-canonical switchover for `save_section` (human edit path) + flag flip → Slice 3**; **Tier-1/Tier-2 post-checkpoint validation** still deferred (the kernel checkpoints via `checkpoint_artifact`, which is provenance-only); agent direct-commit paths (coherence/evaluator) unaffected and out of scope.
  - **Known limitation to record (whole-fragment replace):** the kernel applies an accept by **clearing and repopulating the entire `prosemirror` fragment**, so the captured update deletes every existing CRDT node and creates new ones. A client edit that was made against the pre-accept doc but arrives *after* the accept releases the room lock references now-deleted nodes and is effectively **lost** on merge. This is invisible in 2b-1 (flag off → no concurrent editing clients; the room lock serializes server ops) but must be replaced with a **targeted section-subtree replace** (or accept-rebase) when real co-editing lands in **Slice 3**. Record it as the top Slice-3 kernel follow-up.
  - **Known limitation to record (evict does not disconnect clients — Codex open question):** `MANAGER.evict(key)` (the failure-path recovery) only drops the room from the registry; any already-connected WS tasks keep their reference to the now-orphaned room object (accepted Slice-1 poison debt). The kernel's evict-on-failure leans on this harder, but it is still unreachable in 2b-1 (flag off → no connected clients during a kernel accept). Slice 3 (flag on, live co-editing) must make `evict` actively close/re-sync connected clients. Record it as a Slice-3 prerequisite alongside the whole-fragment-replace follow-up.

- [ ] **Step 2: Commit** (`docs(collab): Slice 2b-1 notes`).

---

## Final verification (before whole-branch review)

```bash
uv run pytest tests/proposal_assist/collab/ tests/proposal_assist/proposals/ -q
uv run ruff check .
cd frontend && pnpm gen:api && git diff --exit-code src/api/schema.gen.ts
```

---

## Self-Review (author checklist — completed)

**1. Spec coverage (2b-1 subset):** server-side atomic accept through the kernel (Task 3/4) ✓; validation against the live room, not git (Task 3 stale-vs-live test) ✓; no-op accept ⇒ accepted at current checkpoint, no new commit (Task 3) ✓; accept race via FOR UPDATE (Task 5) ✓; client convergence via the applied update (Task 4) ✓; spec-lock guard honored (Task 3) ✓. Deferred-and-documented: citation accept + spec-lock operation (2b-2), `save_section` switchover + flag flip (Slice 3), Tier-1/2 re-fire (still open).

**2. Placeholder scan:** the `RoomManager.operate`/`accept_via_kernel` steps carry explicit "verify against actual `room.py` internals / factor `_get_or_create`" implementer notes rather than guessing private names — these are grounded verification points, not hand-waves. All test bodies and the kernel core carry complete code.

**3. Type consistency:** `apply_pm_json_to_fragment(doc, pm_json)->bytes`, `append_update(..., commit=bool)`, `kernel_accept_prose(...)->KernelAcceptResult(pc, update, committed, evict)`, `accept_via_kernel(...)->ProposedChange`, `RoomManager.operate/broadcast` are used identically across Tasks 1–5. `artifact` is the string `"draft"`/`"spec"` throughout.

**4. Atomicity invariant flagged for the implementer:** the kernel writes (staged update + checkpoint provenance + accept status) MUST ride the caller's single transaction — `store.append_update(commit=False)` + `checkpoint_artifact` (caller-committed) + one `session.commit()` in `_body`. A stray commit mid-sequence would release the FOR UPDATE lock early and split the accept. This is the single most important correctness property of the slice.

---

## Codex plan review — folded in (2026-07-01)

Four findings from a Codex review of the committed plan, all verified against the actual code and folded into the tasks above:

- **H1 — `operate`/`release` evict while a concurrent server op still holds the room.** Op A's `finally` (or a last-client `release`) could pop the room while op B is still running on it; a client connecting in that window hydrates a *fresh* room from the log and misses B's not-yet-committed update. **Fix:** `Room.ops` refcount; `operate` bumps it in the same places `acquire` adds a client, and both `operate`-exit and `release` defer eviction until `not clients and ops == 0`. Deterministic asyncio unit test added (Task 5 Step 2).
- **H2 — "single-threaded room doc access" was false for reads.** The kernel mutates `room.doc` on a worker thread under the asyncio lock, but the SYNC_STEP1 read (`Room.handle`) and `Room.initial_message()` at connect read the doc unlocked on the event-loop thread — a genuine cross-thread pycrdt race once the threadpool mutation exists (Slice-1 was safe only because everything ran on one thread). **Fix:** guard both reads with the room lock (Task 4 Step 1 b+f); the kernel holding the lock across the threadpool call then excludes all reads for the mutation. Global-constraint claim corrected.
- **H3 — spec-lock guard read before the commit mutex.** `post_lock_spec` sets the lock *under* the commit mutex; the kernel checked `spec_locked_at` before `checkpoint_artifact` took the mutex, so a lock committed in between was missed. **Fix:** the kernel acquires the commit mutex right after the staleness early-return, before the spec-lock guard; `checkpoint_artifact` re-acquires the same advisory **xact** lock later, which is re-entrant within the transaction (instant no-op, no deadlock/second-timeout). `CommitMutexTimeout` (raised before any doc mutation) added to `_CLEAN_ACCEPT_ERRORS` so it does not trigger a room eviction.
- **L4 — ruff.** Dropped the unused `StaleSectionError` import from `kernel.py` (the stale path *returns*, never raises) and made `_route` return a 2-tuple `(enabled, artifact)` (dead `kind` removed — dispatch happens inside the kernel).

Open question raised by the review — `MANAGER.evict()` does not disconnect already-connected clients — is accepted Slice-1 debt, unreachable in 2b-1 (flag off), and recorded as a Slice-3 prerequisite in the Task 6 notes.
