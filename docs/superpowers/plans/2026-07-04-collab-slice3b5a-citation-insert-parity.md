# Collab Slice 3b-5a — Citation-insert parity (Ask/Edit/Cite in the collab editor) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the CM6 select-to-cite / selection-assist experience to parity in the Tiptap collab editor — a floating Ask/Edit/Cite toolbar that works while collaborative editing is live, routing citations through the live Yjs room instead of git.

**Architecture:** Six coordinated pieces. Backend: (A) `get_draft` serves room-canonical sections/citations under collab; (B) `build_selection_block` verifies Ask/Edit against the room; (C) a pc-free citation-core helper extracted from `kernel_accept_citation`, driven by a new **async** orchestrator `direct_cite_via_kernel` that mirrors `accept_via_kernel` (room lock, threadpool DB body, broadcast-outside-lock, post-mutation evict); (D) `compute_citation_insert` gains optional exact quote offsets. Frontend: (E) a `TiptapSelectionToolbar` + a pure anchor-derivation helper. The feature flag stays OFF; every change is inert unless the artifact is collab-enabled.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2 / pycrdt / pytest+testcontainers; React 19 / Tiptap v3 / ProseMirror / Yjs / vitest.

**Design spec:** `docs/superpowers/specs/2026-07-04-collab-slice3b5a-citation-insert-parity-design.md` (read it first — §4.A–F map to Tasks below).

**Base:** current master (`fc611e3`). Built via detached SDD in worktree `.worktrees/collab-slice3b5a`, branch `collab-slice3b5a`. Stop-before-merge.

## Global Constraints

- **Feature flag STAYS OFF.** No `enable_collab`/flag flip anywhere in `src/`. This slice is inert with the flag off (activates only for collab-enabled artifacts; prod has none until 3b-5d).
- **NO database migration, NO schema change.** All backend changes are additive.
- **CM6 flag-off parity is a hard gate.** Every git-canonical branch stays byte-for-byte: `get_draft` git read, `build_selection_block` git verify, `insert_citation_for_user` git commit, `compute_citation_insert` with offsets absent, `post_citation` flag-off owner-only path.
- **Python tests:** always `EMBEDDING_MODEL=bge-m3 uv run pytest <paths> -p no:cacheprovider -q`. Run grouped dirs, NEVER the whole suite. **Always include `tests/test_api_spec.py`** in the final sweep. Redirect to a log + echo the exit code (`> /tmp/x.log 2>&1; echo EXIT=$?`); never pipe pytest through `tail`.
- **Frontend:** `pnpm` inside `frontend/`. `pnpm tsc --noEmit` must stay clean. No new dep expected; any fresh `pnpm add` uses `--config.minimumReleaseAge=0`.
- **`git add` EXPLICIT paths only** (never `-A`/`.`). Commit per task with the message shown.
- **Do NOT** invoke `/code-review`, `/ultrareview`, or any billing slash command.
- **Cite = any writer** via `resolve_access(...artifact="draft").can_write`; **Edit = owner** (`can_edit`); **Ask = member**. Three distinct predicates — never collapse them.

---

### Task 1: `compute_citation_insert` — optional exact quote offsets + full bounds

Pure, isolated change: let the citation core accept an exact evidence-quote span (deterministic disambiguation) while leaving the offset-less callers untouched.

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/citations.py` (`compute_citation_insert`, currently `def` at :576; quote-verify block at :632-638)
- Test: `tests/proposal_assist/test_compute_citation_insert_offsets.py` (new)

**Interfaces:**
- Produces: `compute_citation_insert(..., quote_start: int | None = None, quote_end: int | None = None)`. When BOTH are ints, validate `0 <= quote_start < quote_end <= len(chunk.text)`, `chunk.text[quote_start:quote_end] == evidence_quote`, and non-empty; use them instead of `find_quote_offsets`. When either is None, behavior is exactly as today.

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/test_compute_citation_insert_offsets.py
import uuid
import pytest
from call_manager.proposal_assist.tools.citations import compute_citation_insert
from call_manager.proposal_assist.citations.ledger import empty_ledger
from tests.proposal_assist.conftest import make_user, make_workspace


def _seed_ref(session, wid, chunk_text: str) -> uuid.UUID:
    """Minimal Reference + ReferenceChunk(index 0) in this workspace."""
    from call_manager.db.models import Reference, ReferenceChunk
    ref = Reference(workspace_id=wid, title="R", kind="url", source_url="http://x")
    session.add(ref)
    session.flush()
    session.add(ReferenceChunk(reference_id=ref.id, workspace_id=wid, chunk_index=0, text=chunk_text))
    session.flush()
    return ref.id


def test_exact_offsets_disambiguate_repeated_quote(pg_session_factory):
    # "sky" occurs twice → offset-less path is ambiguous; exact offsets pick one.
    with pg_session_factory() as s:
        o = make_user(s, email="cci@ki.se"); ws = make_workspace(s, owner=o); s.commit()
        wid = ws.id
        rid = _seed_ref(s, wid, "blue sky and grey sky")
        prop = "preamble\n\n## Methods\nThe claim here.\n"
        # offset-less → ambiguous error
        amb = compute_citation_insert(
            s, workspace_id=wid, proposal_text=prop, ledger=empty_ledger(),
            ref_uuid=rid, anchor="The claim here.", chunk_index=0,
            evidence_quote="sky", stance="supports", confidence=None)
        assert "error" in amb and "ambiguous" in amb["error"]
        # exact offsets of the SECOND "sky" (index 18..21) → success
        ok = compute_citation_insert(
            s, workspace_id=wid, proposal_text=prop, ledger=empty_ledger(),
            ref_uuid=rid, anchor="The claim here.", chunk_index=0,
            evidence_quote="sky", quote_start=18, quote_end=21,
            stance="supports", confidence=None)
        assert "error" not in ok
        assert ok["ev_id"] and ok["citekey"]


@pytest.mark.parametrize("qs,qe,frag", [
    (0, 99, "out of range"),          # quote_end > len(chunk.text)
    (5, 3, "out of range"),           # start >= end
    (0, 1, "not found"),              # slice "b" != evidence_quote "blue"
])
def test_exact_offsets_bounds_rejected(pg_session_factory, qs, qe, frag):
    with pg_session_factory() as s:
        o = make_user(s, email="cci2@ki.se"); ws = make_workspace(s, owner=o); s.commit()
        wid = ws.id
        rid = _seed_ref(s, wid, "blue sky")
        res = compute_citation_insert(
            s, workspace_id=wid, proposal_text="p\n\n## H\nThe claim here.\n",
            ledger=empty_ledger(), ref_uuid=rid, anchor="The claim here.",
            chunk_index=0, evidence_quote="blue", quote_start=qs, quote_end=qe,
            stance="supports", confidence=None)
        assert "error" in res and frag in res["error"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/test_compute_citation_insert_offsets.py -p no:cacheprovider -q > /tmp/t1.log 2>&1; echo EXIT=$?`
Expected: FAIL — `compute_citation_insert() got an unexpected keyword argument 'quote_start'`.

- [ ] **Step 3: Implement**

In `src/call_manager/proposal_assist/tools/citations.py`, add the two optional params to the signature and replace the quote-verify block (:632-638):

```python
def compute_citation_insert(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    proposal_text: str,
    ledger: dict,
    ref_uuid: uuid.UUID,
    anchor: str,
    chunk_index: int,
    evidence_quote: str,
    stance: str,
    confidence: str | None,
    quote_start: int | None = None,
    quote_end: int | None = None,
) -> dict[str, Any]:
    ...
    # 4. Quote-verify against the raw chunk text.
    if quote_start is not None and quote_end is not None:
        # Exact-offset path (direct human cite): full bounds + equality + non-empty,
        # mirroring human_cite.insert_citation_for_user (slice-equality alone accepts
        # quote_end > len via Python suffix slicing).
        if not (0 <= quote_start < quote_end <= len(chunk.text)):
            return {"error": "evidence quote offsets out of range"}
        start, end = quote_start, quote_end
        sliced = chunk.text[start:end]
        if sliced != evidence_quote:
            return {"error": "evidence quote not found in chunk"}
        if not sliced.strip():
            return {"error": "evidence quote is empty"}
    else:
        try:
            start, end = find_quote_offsets(chunk.text, evidence_quote)
        except QuoteNotFound:
            return {"error": "evidence quote not found in chunk"}
        except QuoteAmbiguous:
            return {"error": "evidence quote is ambiguous (occurs multiple times) in chunk"}
```

(Leave the rest of the function unchanged — `start`/`end` flow downstream as before.)

- [ ] **Step 4: Run test to verify it passes**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/test_compute_citation_insert_offsets.py -p no:cacheprovider -q > /tmp/t1.log 2>&1; echo EXIT=$?`
Expected: PASS. Then regression-check the existing citation-core tests:
`EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/ -k "citation or cite" -p no:cacheprovider -q > /tmp/t1b.log 2>&1; echo EXIT=$?` → all pass (offset-less callers unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/tools/citations.py tests/proposal_assist/test_compute_citation_insert_offsets.py
git commit -m "feat(collab): compute_citation_insert accepts validated exact quote offsets"
```

---

### Task 2: Extract pc-free citation-core helper `apply_citation_to_room`

Pull the "mutex → ledger → compute_citation_insert → section-anchor pre-check → section apply → append_update → checkpoint+ledger → citekey cache" tail out of `kernel_accept_citation` into a reusable helper that returns the Yjs update + result and raises typed errors, so both the pc-accept path and the new direct-cite path share ONE implementation.

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/kernel.py` (extract from `kernel_accept_citation` :162-280; add `apply_citation_to_room` + `CitationApplyResult`)
- Test: `tests/proposal_assist/collab/test_apply_citation_to_room.py` (new)

**Interfaces:**
- Consumes: `compute_citation_insert(..., quote_start, quote_end)` (Task 1); `CiteStale`/`CiteInvalid`/`CiteRefNotFound` from `...citations.human_cite`; `anchor_occurrences` from `...citations.markers`.
- Produces:
  ```python
  @dataclass
  class CitationApplyResult:
      update: bytes | None   # None on idempotent hit
      ev_id: str
      citekey: str | None
      commit_sha: str
      committed: bool
      idempotent: bool

  def apply_citation_to_room(
      session, *, room_doc, workspace_id, section_index, base_section_sha,
      anchor, reference_id, chunk_index, evidence_quote,
      quote_start=None, quote_end=None, stance="supports", confidence=None,
      actor_id, workspaces_dir, require_anchor_in_section=False,
  ) -> CitationApplyResult
  ```
  Raises `CiteStale` (section moved / anchor absent / any compute error on the pc path is remapped by the caller), `CiteInvalid` (ambiguous anchor / bad quote), `CiteRefNotFound` (ref missing/foreign/tombstoned). `require_anchor_in_section=True` adds the direct-user pre-check.

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/collab/test_apply_citation_to_room.py
import uuid
from pathlib import Path
import pytest

from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.hydrate import build_doc, load_or_hydrate
from call_manager.proposal_assist.collab.kernel import apply_citation_to_room
from call_manager.proposal_assist.collab.markdown_bridge import pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import ydoc_to_pm_json
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.citations.human_cite import CiteStale
from call_manager.proposal_assist.editing.save import PROPOSAL_FILENAME, CITATIONS_FILENAME
from call_manager.proposal_assist.citations.ledger import load_ledger
from tests.proposal_assist.conftest import make_user, make_workspace
# Reuse the citation-kernel harness helpers.
from tests.proposal_assist.collab.test_collab_kernel_citation import _init_repo, _seed_reference, _git_head


def _live_room(session, wid, workspaces_dir):
    updates = load_or_hydrate(session, workspace_id=wid, artifact="draft", workspaces_dir=workspaces_dir)
    session.commit()
    return build_doc(updates)


def test_apply_citation_to_room_inserts_marker_and_cocommits_ledger(pg_session_factory, tmp_path):
    md0 = "preamble\n\n## Methods\nThe claim here.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="acr@ki.se"); ws = make_workspace(s, owner=o, current_phase="active"); s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"; _init_repo(repo, md0)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft"); s.commit()
        rid = _seed_reference(s, wid, chunk_text="azure sky")
    with pg_session_factory() as s:
        room_doc = _live_room(s, wid, str(tmp_path))
    base_sha = split_sections(pm_to_markdown(ydoc_to_pm_json(room_doc)))[1].sha
    with pg_session_factory() as s:
        res = apply_citation_to_room(
            s, room_doc=room_doc, workspace_id=wid, section_index=1,
            base_section_sha=base_sha, anchor="The claim here.", reference_id=rid,
            chunk_index=0, evidence_quote="azure", quote_start=0, quote_end=5,
            actor_id=oid, workspaces_dir=str(tmp_path), require_anchor_in_section=True)
        s.commit()
    assert res.update is not None and res.ev_id and res.citekey
    disk = (repo / PROPOSAL_FILENAME).read_text()
    assert f"[@{res.citekey}]<!-- ev:{res.ev_id} -->" in disk
    ledger = load_ledger((repo / CITATIONS_FILENAME).read_text())
    assert ledger["evidence"][0]["id"] == res.ev_id


def test_apply_citation_stale_section_sha_raises(pg_session_factory, tmp_path):
    md0 = "preamble\n\n## Methods\nThe claim here.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="acr2@ki.se"); ws = make_workspace(s, owner=o, current_phase="active"); s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"; _init_repo(repo, md0)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft"); s.commit()
        rid = _seed_reference(s, wid, chunk_text="azure sky")
    with pg_session_factory() as s:
        room_doc = _live_room(s, wid, str(tmp_path))
    with pg_session_factory() as s:
        with pytest.raises(CiteStale):
            apply_citation_to_room(
                s, room_doc=room_doc, workspace_id=wid, section_index=1,
                base_section_sha="deadbeef" * 8, anchor="The claim here.", reference_id=rid,
                chunk_index=0, evidence_quote="azure", quote_start=0, quote_end=5,
                actor_id=oid, workspaces_dir=str(tmp_path), require_anchor_in_section=True)


def test_apply_citation_anchor_absent_from_section_raises(pg_session_factory, tmp_path):
    md0 = "preamble\n\n## Methods\nThe claim here.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="acr3@ki.se"); ws = make_workspace(s, owner=o, current_phase="active"); s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"; _init_repo(repo, md0)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft"); s.commit()
        rid = _seed_reference(s, wid, chunk_text="azure sky")
    with pg_session_factory() as s:
        room_doc = _live_room(s, wid, str(tmp_path))
    base_sha = split_sections(pm_to_markdown(ydoc_to_pm_json(room_doc)))[1].sha
    with pg_session_factory() as s:
        with pytest.raises(CiteStale):   # anchor not in section 1
            apply_citation_to_room(
                s, room_doc=room_doc, workspace_id=wid, section_index=1,
                base_section_sha=base_sha, anchor="NOT PRESENT ANYWHERE", reference_id=rid,
                chunk_index=0, evidence_quote="azure", quote_start=0, quote_end=5,
                actor_id=oid, workspaces_dir=str(tmp_path), require_anchor_in_section=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_apply_citation_to_room.py -p no:cacheprovider -q > /tmp/t2.log 2>&1; echo EXIT=$?`
Expected: FAIL — `ImportError: cannot import name 'apply_citation_to_room'`.

- [ ] **Step 3: Implement the helper + refactor `kernel_accept_citation` to call it**

In `src/call_manager/proposal_assist/collab/kernel.py`, add imports and the helper. Move the body from `kernel_accept_citation` (:207-277) into it. Key adaptations: staleness/anchor/compute errors **raise** (not `_stale()`); return `CitationApplyResult`.

```python
from dataclasses import dataclass
from call_manager.proposal_assist.citations.human_cite import CiteStale, CiteInvalid, CiteRefNotFound
from call_manager.proposal_assist.citations.markers import anchor_occurrences


@dataclass
class CitationApplyResult:
    update: bytes | None
    ev_id: str
    citekey: str | None
    commit_sha: str
    committed: bool
    idempotent: bool


def apply_citation_to_room(
    session, *, room_doc, workspace_id, section_index, base_section_sha,
    anchor, reference_id, chunk_index, evidence_quote,
    quote_start=None, quote_end=None, stance="supports", confidence=None,
    actor_id, workspaces_dir, require_anchor_in_section=False,
) -> CitationApplyResult:
    """Apply a citation marker to the LIVE room + co-commit the ledger. Shared by
    kernel_accept_citation (pc path) and direct_cite_via_kernel (direct path).
    Staleness is vs the LIVE section SHA (never git). Raises CiteStale/CiteInvalid/
    CiteRefNotFound; the caller decides the HTTP/pc mapping. Caller owns the txn."""
    sections = _live_sections(room_doc)
    if not 0 <= section_index < len(sections):
        raise IndexError(f"section_index {section_index} out of range")
    live = sections[section_index]
    if live.sha != base_section_sha:
        raise CiteStale("this section changed — reopen and re-cite")
    if require_anchor_in_section and anchor_occurrences(live.text, anchor) == 0:
        # Direct-user protection: the anchor must exist in the section the user
        # selected, before the whole-doc insert_marker inside compute_citation_insert.
        raise CiteStale("this section changed — reopen and re-cite")

    acquire_commit_mutex(session, workspace_id)
    repo = workspace_repo_dir(workspaces_dir, workspace_id)
    _cpath = repo / CITATIONS_FILENAME
    try:
        ledger = load_ledger(_cpath.read_text() if _cpath.exists() else None)
    except ValueError as exc:
        raise CiteInvalid("citations.json is invalid") from exc

    room_md = pm_to_markdown(ydoc_to_pm_json(room_doc))
    result = compute_citation_insert(
        session, workspace_id=workspace_id, proposal_text=room_md, ledger=ledger,
        ref_uuid=uuid.UUID(str(reference_id)), anchor=anchor, chunk_index=chunk_index,
        evidence_quote=evidence_quote, stance=stance, confidence=confidence,
        quote_start=quote_start, quote_end=quote_end)
    if "error" in result:
        err = result["error"]
        if "reference not found" in err or "tombstoned" in err:
            raise CiteRefNotFound(err)
        if "ambiguous" in err:
            raise CiteInvalid(err)
        raise CiteStale(err)  # not-found / anchor-not-found / offsets → terminal stale
    if result.get("idempotent"):
        return CitationApplyResult(
            update=None, ev_id=result["ev_id"], citekey=result.get("citekey"),
            commit_sha=_head_or_empty(repo), committed=False, idempotent=True)

    new_section_md = split_sections(result["new_proposal_text"])[section_index].text
    section_blocks = markdown_to_pm(new_section_md).get("content") or []
    update = apply_pm_section_to_fragment(room_doc, section_index=section_index, blocks=section_blocks)
    store.append_update(session, workspace_id=workspace_id, artifact="draft",
                        actor_id=actor_id, update=update, commit=False)
    cp = checkpoint_artifact(
        session, workspace_id=workspace_id, artifact="draft", workspaces_dir=workspaces_dir,
        extra_files={CITATIONS_FILENAME: dump_ledger(result["new_ledger"])})
    _write_citekey_cache(session, result["ref"], result["citekey"], owns_txn=False)
    return CitationApplyResult(
        update=update, ev_id=result["ev_id"], citekey=result["citekey"],
        commit_sha=cp.commit_sha, committed=cp.committed, idempotent=False)
```

Then refactor `kernel_accept_citation` (behavior-preserving — all verify failures still map to `_stale()`):

```python
def kernel_accept_citation(session, *, room_doc, proposed_change_id, workspace_id, approver, workspaces_dir):
    pc = _load_scoped(session, proposed_change_id, workspace_id)
    if pc.status != "pending":
        from call_manager.proposal_assist.proposals.accepting import ProposedChangeNotPending
        raise ProposedChangeNotPending(pc.status)

    def _stale() -> KernelAcceptResult:
        pc.status = "stale"; pc.resolved_by = approver.id; pc.resolved_at = datetime.now(UTC)
        session.flush()
        return KernelAcceptResult(pc=pc, update=None, committed=False, evict=False)

    p = pc.payload
    try:
        res = apply_citation_to_room(
            session, room_doc=room_doc, workspace_id=workspace_id,
            section_index=pc.section_index, base_section_sha=pc.base_section_sha,
            anchor=p["anchor"], reference_id=p["reference_id"], chunk_index=p["chunk_index"],
            evidence_quote=p["evidence_quote"], quote_start=None, quote_end=None,
            stance=p.get("stance", "supports"), confidence=p.get("confidence"),
            actor_id=approver.id, workspaces_dir=workspaces_dir,
            require_anchor_in_section=False)  # pc was section-validated at propose time
    except (CiteStale, CiteInvalid, CiteRefNotFound):
        return _stale()  # git-first parity: every verify failure is terminal-stale
    if res.idempotent:
        pc.status = "accepted"; pc.resolved_by = approver.id; pc.resolved_at = datetime.now(UTC)
        pc.resulting_commit_sha = res.commit_sha; session.flush()
        return KernelAcceptResult(pc=pc, update=None, committed=False, evict=False)
    pc.status = "accepted"; pc.resolved_by = approver.id; pc.resolved_at = datetime.now(UTC)
    pc.resulting_commit_sha = res.commit_sha; session.flush()
    return KernelAcceptResult(pc=pc, update=res.update, committed=res.committed, evict=True)
```

Confirm `IndexError` still propagates pre-mutation (it is raised before the mutex; `accept_via_kernel` lists it in `_CLEAN_ACCEPT_ERRORS`).

- [ ] **Step 4: Run tests to verify they pass**

Run the new test + the existing kernel-citation regression (helper extraction must be behavior-preserving):
```bash
EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab/test_apply_citation_to_room.py \
  tests/proposal_assist/collab/test_collab_kernel_citation.py \
  tests/proposal_assist/collab/test_collab_kernel_accept_route.py \
  -p no:cacheprovider -q > /tmp/t2.log 2>&1; echo EXIT=$?
```
Expected: PASS (both new + existing).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/collab/kernel.py tests/proposal_assist/collab/test_apply_citation_to_room.py
git commit -m "refactor(collab): extract apply_citation_to_room from kernel_accept_citation"
```

---

### Task 3: `direct_cite_via_kernel` async orchestrator

New async orchestration for a direct (pc-free) human cite against the live room, mirroring `accept_via_kernel` exactly: `MANAGER.operate` → `room.lock` → threadpool DB body → `RoomEvicted`-before-commit → broadcast-outside-lock → clean-error vs post-mutation-evict contract.

**Files:**
- Create: `src/call_manager/proposal_assist/citations/collab_cite.py`
- Test: `tests/proposal_assist/collab/test_direct_cite_via_kernel.py` (new)

**Interfaces:**
- Consumes: `apply_citation_to_room` (Task 2); `MANAGER`, `load_or_hydrate`, `protocol as P`, `RoomEvicted` (from `...proposals.kernel_accept`), `run_in_threadpool`.
- Produces: `async def direct_cite_via_kernel(*, session_factory, workspaces_dir, workspace_id, actor_id, section_index, base_section_sha, anchor, reference_id, chunk_index, evidence_quote, quote_start, quote_end, stance) -> dict` → `{ev_id, citekey, commit_sha}`. Raises `CiteStale`/`CiteInvalid`/`CiteRefNotFound`/`CommitMutexTimeout`/`IndexError`/`RoomEvicted` clean (pre-mutation); evicts the room on any other (post-mutation) exception.

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/collab/test_direct_cite_via_kernel.py
import uuid
from pathlib import Path
import pytest

from call_manager.proposal_assist.collab import state
from call_manager.proposal_assist.collab.room import MANAGER
from call_manager.proposal_assist.collab.markdown_bridge import pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import ydoc_to_pm_json
from call_manager.proposal_assist.collab.hydrate import build_doc, load_or_hydrate
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.editing.save import PROPOSAL_FILENAME, CITATIONS_FILENAME
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.citations.human_cite import CiteStale
from call_manager.proposal_assist.citations.collab_cite import direct_cite_via_kernel
from tests.proposal_assist.conftest import make_user, make_workspace
from tests.proposal_assist.collab.test_collab_kernel_citation import _init_repo, _seed_reference


def _base_sha(session, wid, workspaces_dir, section_index=1):
    updates = load_or_hydrate(session, workspace_id=wid, artifact="draft", workspaces_dir=workspaces_dir)
    session.commit()
    room_doc = build_doc(updates)
    return split_sections(pm_to_markdown(ydoc_to_pm_json(room_doc)))[section_index].sha


@pytest.mark.anyio
async def test_direct_cite_applies_and_returns(pg_session_factory, tmp_path):
    md0 = "preamble\n\n## Methods\nThe claim here.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="dck@ki.se"); ws = make_workspace(s, owner=o, current_phase="active"); s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"; _init_repo(repo, md0)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft"); s.commit()
        rid = _seed_reference(s, wid, chunk_text="azure sky")
    with pg_session_factory() as s:
        bsha = _base_sha(s, wid, str(tmp_path))
    out = await direct_cite_via_kernel(
        session_factory=pg_session_factory, workspaces_dir=str(tmp_path), workspace_id=wid,
        actor_id=oid, section_index=1, base_section_sha=bsha, anchor="The claim here.",
        reference_id=rid, chunk_index=0, evidence_quote="azure", quote_start=0, quote_end=5,
        stance="supports")
    assert set(out) == {"ev_id", "citekey", "commit_sha"}
    disk = (repo / PROPOSAL_FILENAME).read_text()
    assert f"[@{out['citekey']}]<!-- ev:{out['ev_id']} -->" in disk
    assert load_ledger((repo / CITATIONS_FILENAME).read_text())["evidence"][0]["id"] == out["ev_id"]


@pytest.mark.anyio
async def test_direct_cite_stale_does_not_evict(pg_session_factory, tmp_path):
    md0 = "preamble\n\n## Methods\nThe claim here.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="dck2@ki.se"); ws = make_workspace(s, owner=o, current_phase="active"); s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"; _init_repo(repo, md0)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft"); s.commit()
        rid = _seed_reference(s, wid, chunk_text="azure sky")
    with pytest.raises(CiteStale):
        await direct_cite_via_kernel(
            session_factory=pg_session_factory, workspaces_dir=str(tmp_path), workspace_id=wid,
            actor_id=oid, section_index=1, base_section_sha="dead" * 16, anchor="The claim here.",
            reference_id=rid, chunk_index=0, evidence_quote="azure", quote_start=0, quote_end=5,
            stance="supports")
    assert MANAGER.get((wid, "draft")) is None  # clean abort → no lingering room


@pytest.mark.anyio
async def test_direct_cite_post_mutation_failure_evicts(pg_session_factory, tmp_path, monkeypatch):
    md0 = "preamble\n\n## Methods\nThe claim here.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="dck3@ki.se"); ws = make_workspace(s, owner=o, current_phase="active"); s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"; _init_repo(repo, md0)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft"); s.commit()
        rid = _seed_reference(s, wid, chunk_text="azure sky")
    with pg_session_factory() as s:
        bsha = _base_sha(s, wid, str(tmp_path))
    # Fail AFTER the fragment mutation (during checkpoint) → room.doc ahead of log → evict.
    import call_manager.proposal_assist.collab.kernel as kmod
    def _boom(*a, **k): raise RuntimeError("disk full")
    monkeypatch.setattr(kmod, "checkpoint_artifact", _boom)
    with pytest.raises(RuntimeError):
        await direct_cite_via_kernel(
            session_factory=pg_session_factory, workspaces_dir=str(tmp_path), workspace_id=wid,
            actor_id=oid, section_index=1, base_section_sha=bsha, anchor="The claim here.",
            reference_id=rid, chunk_index=0, evidence_quote="azure", quote_start=0, quote_end=5,
            stance="supports")
    assert MANAGER.get((wid, "draft")) is None  # evicted (poison recovery)
```

(If `@pytest.mark.anyio` isn't the project's async marker, match the existing async collab tests — grep `pytest.mark` in `tests/proposal_assist/collab/test_collab_room_operate.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_direct_cite_via_kernel.py -p no:cacheprovider -q > /tmp/t3.log 2>&1; echo EXIT=$?`
Expected: FAIL — `ModuleNotFoundError: ...citations.collab_cite`.

- [ ] **Step 3: Implement the orchestrator**

```python
# src/call_manager/proposal_assist/citations/collab_cite.py
"""Async orchestration for a direct (pc-free) human cite against the live Yjs room.
Sibling to proposals.kernel_accept.accept_via_kernel — same room-lock / threadpool /
broadcast-outside-lock / post-mutation-evict contract."""
from __future__ import annotations

import uuid
from starlette.concurrency import run_in_threadpool

from call_manager.proposal_assist.collab import protocol as P
from call_manager.proposal_assist.collab.hydrate import load_or_hydrate
from call_manager.proposal_assist.collab.kernel import apply_citation_to_room
from call_manager.proposal_assist.collab.room import MANAGER
from call_manager.proposal_assist.citations.human_cite import CiteStale, CiteInvalid, CiteRefNotFound
from call_manager.proposal_assist.drafting.locking import CommitMutexTimeout
from call_manager.proposal_assist.proposals.kernel_accept import RoomEvicted

_CLEAN_CITE_ERRORS = (CiteStale, CiteInvalid, CiteRefNotFound, CommitMutexTimeout, IndexError, RoomEvicted)


async def direct_cite_via_kernel(
    *, session_factory, workspaces_dir, workspace_id: uuid.UUID, actor_id: int,
    section_index, base_section_sha, anchor, reference_id, chunk_index,
    evidence_quote, quote_start, quote_end, stance="supports",
) -> dict:
    key = (workspace_id, "draft")

    async def _load() -> list[bytes]:
        def _read():
            with session_factory() as s:
                updates = load_or_hydrate(s, workspace_id=workspace_id, artifact="draft",
                                          workspaces_dir=workspaces_dir)
                s.commit()
                return updates
        return await run_in_threadpool(_read)

    async with MANAGER.operate(key, load=_load) as room:
        try:
            async with room.lock:
                def _body() -> tuple[bytes | None, dict]:
                    with session_factory() as s:
                        res = apply_citation_to_room(
                            s, room_doc=room.doc, workspace_id=workspace_id,
                            section_index=section_index, base_section_sha=base_section_sha,
                            anchor=anchor, reference_id=reference_id, chunk_index=chunk_index,
                            evidence_quote=evidence_quote, quote_start=quote_start,
                            quote_end=quote_end, stance=stance, actor_id=actor_id,
                            workspaces_dir=workspaces_dir, require_anchor_in_section=True)
                        if room.evicted:
                            raise RoomEvicted(str(key))  # tombstoned mid-cite — abort before commit
                        s.commit()
                        return res.update, {"ev_id": res.ev_id, "citekey": res.citekey,
                                            "commit_sha": res.commit_sha}
                update, out = await run_in_threadpool(_body)
            if update is not None and not room.evicted:
                await room.broadcast(P.create_update_message(update))
            return out
        except _CLEAN_CITE_ERRORS:
            raise  # pre-mutation — room doc untouched, no evict
        except Exception:
            await MANAGER.evict(key)  # room.doc may be ahead of the rolled-back log
            raise
```

- [ ] **Step 4: Run test to verify it passes**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_direct_cite_via_kernel.py -p no:cacheprovider -q > /tmp/t3.log 2>&1; echo EXIT=$?`
Expected: PASS (apply + return; stale-no-evict; post-mutation-evict).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/citations/collab_cite.py tests/proposal_assist/collab/test_direct_cite_via_kernel.py
git commit -m "feat(collab): direct_cite_via_kernel async orchestrator (room apply + evict contract)"
```

---

### Task 4: `post_citation` async endpoint dispatch (flag-off unchanged / flag-on any-writer)

Make the endpoint collab-aware: async handler, threadpooled route precheck, `can_write` gate + `direct_cite_via_kernel` when collab is on; the current sync owner-only git path unchanged (threadpooled) when off.

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py` (`post_citation` :849-910)
- Test: `tests/test_api_workspaces.py` (add collab cases beside `test_post_citation_*`)

**Interfaces:**
- Consumes: `direct_cite_via_kernel` (Task 3); `resolve_access` (`...collab.ws_auth`); `collab_state.is_collab_enabled`; existing `insert_citation_for_user` (git path, unchanged).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_api_workspaces.py` (reusing `_app`, `_seed`, `_client`, `_seed_source_reference`, `_capture`, `_cite_body`; a member is the 2nd return of `_seed`). Enable collab and cite room-canonically. Because collab changes the section SHA source, capture the base SHA from the room (helper below).

```python
def _room_base_sha(app, pg_session_factory, tmp_path, ws_id, section_index=1):
    from call_manager.proposal_assist.collab.canonical import canonical_artifact_markdown
    from call_manager.proposal_assist.editing.sectionizer import split_sections
    from call_manager.proposal_assist.paths import workspace_repo_dir
    with pg_session_factory() as s:
        raw = canonical_artifact_markdown(s, workspace_id=ws_id, artifact="draft",
                                          repo_dir=workspace_repo_dir(tmp_path, ws_id))
    return split_sections(raw)[section_index].sha


def _enable_collab(pg_session_factory, ws_id):
    from call_manager.proposal_assist.collab import state
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=ws_id, artifact="draft"); s.commit()


def test_post_citation_collab_any_writer_allowed(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, _stranger_id, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n")
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="azure sky")
    _enable_collab(pg_session_factory, ws_id)
    bc, _ = _capture(tmp_path, ws_id, 1)          # git head (base_commit_sha, not the gate)
    bs = _room_base_sha(app, pg_session_factory, tmp_path, ws_id)
    c = _client(app, member_id)                   # a NON-owner writer
    r = c.post(f"/api/workspaces/{ws_id}/citations", json=_cite_body(bc, bs, rid))
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"ev_id", "citekey", "commit_sha"}


def test_post_citation_flag_off_still_owner_only(monkeypatch, pg_url, pg_session_factory, tmp_path):
    # Parity: with collab OFF, a member is still 403 (unchanged git path).
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, _s, ws_id = _seed(app, pg_session_factory, tmp_path,
                                           "preamble\n\n## Methods\noriginal.\n")
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="azure sky")
    bc, bs = _capture(tmp_path, ws_id, 1)
    r = _client(app, member_id).post(f"/api/workspaces/{ws_id}/citations", json=_cite_body(bc, bs, rid))
    assert r.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/test_api_workspaces.py -k "post_citation" -p no:cacheprovider -q > /tmp/t4.log 2>&1; echo EXIT=$?`
Expected: the collab test FAILS (member currently 403 even with collab on, because the handler is owner-only git-only).

- [ ] **Step 3: Implement the async dispatch**

Replace `post_citation` (:849-910). Keep the current sync body as an inner function so flag-off logic is byte-for-byte; add the collab branch.

```python
@router.post("/workspaces/{workspace_id}/citations", response_model=AddCitationOut)
async def post_citation(
    workspace_id: uuid.UUID, body: AddCitationIn, request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> AddCitationOut:
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)

    def _route() -> bool:
        with sf() as session:
            require_member(workspace_id=workspace_id, user=user, session=session)  # 404 if not a member
            if not (repo_dir / PROPOSAL_FILENAME).exists():
                raise HTTPException(status_code=404, detail="no proposal.md yet")
            try:
                current_head(repo_dir)
            except GitCommitError as e:
                raise HTTPException(status_code=404, detail="no proposal.md yet") from e
            return collab_state.is_collab_enabled(session, workspace_id=workspace_id, artifact="draft")

    enabled = await run_in_threadpool(_route)

    if enabled:
        def _gate() -> None:
            with sf() as session:
                _, can_write = resolve_access(session, user=user, workspace_id=workspace_id, artifact="draft")
                if not can_write:
                    raise HTTPException(status_code=403, detail="not permitted to write this draft")
        await run_in_threadpool(_gate)
        try:
            out = await direct_cite_via_kernel(
                session_factory=sf, workspaces_dir=settings.workspaces_dir, workspace_id=workspace_id,
                actor_id=user.id, section_index=body.section_index, base_section_sha=body.base_section_sha,
                anchor=body.anchor, reference_id=body.reference_id, chunk_index=body.chunk_index,
                evidence_quote=body.evidence_quote, quote_start=body.quote_start,
                quote_end=body.quote_end, stance=body.stance)
        except CiteStale as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except CiteRefNotFound as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except CiteInvalid as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except RoomEvicted as e:
            raise HTTPException(status_code=409, detail="workspace busy; retry the citation") from e
        except CommitMutexTimeout as e:
            raise HTTPException(status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}) from e
        except GitCommitError as e:
            logger.exception("collab citation insert failed for workspace %s", workspace_id)
            raise HTTPException(status_code=500, detail="citation failed — nothing was written") from e
        return AddCitationOut(**out)

    # Flag-off: the existing owner-only git path, byte-for-byte (threadpooled).
    def _git() -> AddCitationOut:
        with sf() as session:
            ws = require_owner(workspace_id=workspace_id, user=user, session=session)
            try:
                result = insert_citation_for_user(
                    session=session, workspace=ws, user_id=user.id, section_index=body.section_index,
                    base_commit_sha=body.base_commit_sha, base_section_sha=body.base_section_sha,
                    anchor=body.anchor, reference_id=body.reference_id, chunk_index=body.chunk_index,
                    evidence_quote=body.evidence_quote, quote_start=body.quote_start,
                    quote_end=body.quote_end, stance=body.stance, workspaces_dir=settings.workspaces_dir)
                session.commit()
            except CiteStale as e:
                session.rollback(); raise HTTPException(status_code=409, detail=str(e)) from e
            except CiteRefNotFound as e:
                session.rollback(); raise HTTPException(status_code=404, detail=str(e)) from e
            except CiteInvalid as e:
                session.rollback(); raise HTTPException(status_code=422, detail=str(e)) from e
            except CommitMutexTimeout as e:
                session.rollback()
                raise HTTPException(status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}) from e
            except GitCommitError as e:
                session.rollback(); logger.exception("user citation insert failed for workspace %s", workspace_id)
                raise HTTPException(status_code=500, detail="citation failed — nothing was written") from e
        return AddCitationOut(**result)

    return await run_in_threadpool(_git)
```

Add imports at top of `workspaces.py`: `from starlette.concurrency import run_in_threadpool`, `from call_manager.proposal_assist.collab.ws_auth import resolve_access`, `from call_manager.proposal_assist.citations.collab_cite import direct_cite_via_kernel`, `from call_manager.proposal_assist.proposals.kernel_accept import RoomEvicted` (verify none already imported).

> Note: the flag-off precheck now runs `require_member` in `_route` (was `require_owner` inline). Ownership is still enforced by `require_owner` inside `_git`, so a non-owner with collab OFF still gets 403 — parity preserved (asserted by `test_post_citation_flag_off_still_owner_only`).

- [ ] **Step 4: Run tests to verify they pass**

```bash
EMBEDDING_MODEL=bge-m3 uv run pytest tests/test_api_workspaces.py -k "post_citation" \
  -p no:cacheprovider -q > /tmp/t4.log 2>&1; echo EXIT=$?
```
Expected: PASS — all existing `post_citation` error-path tests (flag-off) unchanged + the two new collab cases.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py
git commit -m "feat(collab): post_citation routes to the live room for collab drafts (any writer)"
```

---

### Task 5: `get_draft` room-canonical read under collab

Serve `DraftOut` sections + citation map from the room (via `canonical_artifact_markdown`) when the draft is collab-enabled, so `base_section_sha` matches the kernel's live-section guard and the Outline/pills stop showing stale git content.

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py` (`get_draft` :249-284)
- Test: `tests/test_api_workspaces.py` (add a collab draft-read case)

**Interfaces:**
- Consumes: `canonical_artifact_markdown` (`...collab.canonical`), already imported? verify; `collab_state.is_collab_enabled`.

- [ ] **Step 1: Write the failing test**

```python
def test_draft_collab_returns_room_canonical_sections(monkeypatch, pg_url, pg_session_factory, tmp_path):
    from call_manager.proposal_assist.collab import state, store
    from call_manager.proposal_assist.collab.hydrate import build_doc, load_or_hydrate
    from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
    from call_manager.proposal_assist.collab.yjs_bridge import apply_pm_json_to_fragment
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _m, _s, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=ws_id, artifact="draft"); s.commit()
    # Hydrate then apply a LIVE, uncheckpointed edit to the room (git stays "original.").
    with pg_session_factory() as s:
        updates = load_or_hydrate(s, workspace_id=ws_id, artifact="draft", workspaces_dir=str(tmp_path))
        s.commit()
        room = build_doc(updates)
        upd = apply_pm_json_to_fragment(room, markdown_to_pm("preamble\n\n## Methods\nLIVE EDITED.\n"))
        store.append_update(s, workspace_id=ws_id, artifact="draft", actor_id=owner_id, update=upd, commit=True)
    body = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/draft").json()
    joined = "".join(sec["text"] for sec in body["sections"])
    assert "LIVE EDITED." in joined          # room-canonical, NOT git "original."
    assert "original." not in joined
```

- [ ] **Step 2: Run test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/test_api_workspaces.py -k "draft_collab_returns_room" -p no:cacheprovider -q > /tmp/t5.log 2>&1; echo EXIT=$?`
Expected: FAIL — the read shows git `original.`, not `LIVE EDITED.`.

- [ ] **Step 3: Implement**

In `get_draft` (:249), replace the `raw = proposal.read_text()` line with a collab branch (keep `head`/`can_edit`/`can_write`/`collab_enabled` logic; only the `raw` source changes):

```python
        collab_enabled = collab_state.is_collab_enabled(session, workspace_id=workspace_id, artifact="draft")
        if collab_enabled:
            raw = canonical_artifact_markdown(
                session, workspace_id=workspace_id, artifact="draft", repo_dir=repo_dir)
        else:
            raw = proposal.read_text()
        sections = _sections_out(raw)
        citations = _citation_map(session, settings, workspace_id, raw)
```

Add `from call_manager.proposal_assist.collab.canonical import canonical_artifact_markdown` at the top if not present. (`head = current_head(repo_dir)` stays as-is — git HEAD is fine as `base_commit_sha`; the section SHA is the collab guard.)

- [ ] **Step 4: Run tests to verify they pass**

```bash
EMBEDDING_MODEL=bge-m3 uv run pytest tests/test_api_workspaces.py -k "draft" \
  tests/proposal_assist/test_readmodel_collab_fields.py \
  -p no:cacheprovider -q > /tmp/t5.log 2>&1; echo EXIT=$?
```
Expected: PASS — new collab case + existing git `test_draft_*` (flag-off) unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py
git commit -m "feat(collab): get_draft serves room-canonical sections+citations under collab"
```

---

### Task 6: `build_selection_block` collab-canonical verification (Ask/Edit)

Verify Ask/Edit selections against the room (not stale git) when the draft is collab-enabled, so a live-but-uncheckpointed Edit stops false-rejecting with 409.

**Files:**
- Modify: `src/call_manager/proposal_assist/turns/selection.py` (`build_selection_block` :41)
- Modify: `src/call_manager/web/api/turns.py` (call site :146-158)
- Test: `tests/proposal_assist/turns/test_selection.py` (add a collab-sections case)

**Interfaces:**
- Produces: `build_selection_block(*, repo_dir, sel, sections=None)` — when `sections` (a `list` of `split_sections` items) is provided, verify against it; else read git `repo_dir/PROPOSAL_FILENAME` (unchanged default).
- Consumes (route): `canonical_artifact_markdown` + `split_sections` when `is_collab_enabled(...draft)`.

- [ ] **Step 1: Write the failing test**

```python
def test_collab_sections_override_verifies_modify(tmp_path):
    # git doc has "original."; the collab room override has "LIVE." A modify against
    # the LIVE section verifies (not 409) when we pass sections explicitly.
    from call_manager.proposal_assist.editing.sectionizer import split_sections
    git_doc = "p\n\n## Approach\noriginal.\n"
    live_doc = "p\n\n## Approach\nDo it carefully now.\n"
    repo = _repo(tmp_path, git_doc)
    live_secs = split_sections(live_doc)
    frm = live_secs[1].text.index("carefully"); to = frm + len("carefully")
    block = build_selection_block(
        repo_dir=repo,
        sel=_sel(repo, live_doc, 1, frm, to, "carefully", rt="modify", base_sha=live_secs[1].sha),
        sections=live_secs)
    assert block["verified"] is True
    assert block["request_type"] == "modify" and block["anchor"] == "carefully"
```

(`_sel`'s `base_sha` must be the LIVE section sha; check `_sel`'s signature — it takes `base_sha=`. If it derives sha from the git doc, pass `base_sha=live_secs[1].sha` explicitly as above.)

- [ ] **Step 2: Run test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/turns/test_selection.py -k "collab_sections_override" -p no:cacheprovider -q > /tmp/t6.log 2>&1; echo EXIT=$?`
Expected: FAIL — `build_selection_block() got an unexpected keyword argument 'sections'`.

- [ ] **Step 3: Implement**

In `selection.py`, add the optional param and use it:

```python
def build_selection_block(*, repo_dir: Path, sel: SelectionInput, sections=None) -> dict:
    if sections is None:
        proposal = repo_dir / PROPOSAL_FILENAME
        sections = split_sections(proposal.read_text()) if proposal.exists() else []
    live = sections[sel.section_index] if 0 <= sel.section_index < len(sections) else None
    # ... rest unchanged ...
```

In `turns.py` (call site :144-158), compute room sections when collab is on and pass them:

```python
        selection_block = None
        if sc is not None:
            repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
            sections = None
            if sc.target_artifact == "draft" and collab_state.is_collab_enabled(
                session, workspace_id=workspace_id, artifact="draft"):
                raw = canonical_artifact_markdown(
                    session, workspace_id=workspace_id, artifact="draft", repo_dir=repo_dir)
                sections = split_sections(raw)
            try:
                selection_block = build_selection_block(
                    repo_dir=repo_dir,
                    sel=SelectionInput(request_type=sc.request_type, section_index=sc.section_index,
                        base_commit_sha=sc.base_commit_sha, base_section_sha=sc.base_section_sha,
                        anchor=sc.anchor, from_offset=sc.from_offset, to_offset=sc.to_offset),
                    sections=sections)
            except SelectionConflictError as e:
                raise HTTPException(status_code=409, detail=str(e)) from e
```

Add imports to `turns.py`: `collab_state` (`from call_manager.proposal_assist.collab import state as collab_state`), `canonical_artifact_markdown`, `split_sections` (verify the SelectionContext field is `target_artifact`; if absent, default to draft).

- [ ] **Step 4: Run tests to verify they pass**

```bash
EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/turns/test_selection.py \
  tests/proposal_assist/ -k "selection or turn" -p no:cacheprovider -q > /tmp/t6.log 2>&1; echo EXIT=$?
```
Expected: PASS — new case + existing git selection tests unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/turns/selection.py src/call_manager/web/api/turns.py tests/proposal_assist/turns/test_selection.py
git commit -m "feat(collab): verify Ask/Edit selections against the room under collab"
```

---

### Task 7: Frontend — pure claim-anchor derivation helper

A pure function that turns the current ProseMirror selection into `{sectionIndex, anchor, singleSection, headingOnly}`, unit-tested against a real collab-schema doc. Keeps the trickiest logic out of the component.

**Files:**
- Create: `frontend/src/collab/selectionAnchor.ts`
- Test: `frontend/src/collab/selectionAnchor.test.ts` (new)

**Interfaces:**
- Produces: `deriveClaimAnchor(state: EditorState): { sectionIndex: number; anchor: string; singleSection: boolean; headingOnly: boolean } | null`. `null` when the selection is empty. `sectionIndex` = count of level-2 headings among top-level nodes strictly before the block containing `$from` (section 0 = preamble before the first H2). `singleSection` = `$from` and `$to` share that section. `headingOnly` = the selection lies entirely within heading node(s). `anchor` = `doc.textBetween(from, to, "\n\n", "")`.

- [ ] **Step 1: Write the failing test**

```typescript
// frontend/src/collab/selectionAnchor.test.ts
import { describe, it, expect } from "vitest";
import { getSchema } from "@tiptap/core";
import { EditorState, TextSelection } from "prosemirror-state";
import { collabEditorExtensions } from "./CitationNode";
import { deriveClaimAnchor } from "./selectionAnchor";

const schema = getSchema(collabEditorExtensions);

/** doc: preamble para, ## A, para "Alpha claim.", ## B, para "Beta claim." */
function makeDoc() {
  const h = (level: number, t: string) => schema.nodes.heading.create({ level }, schema.text(t));
  const p = (t: string) => schema.nodes.paragraph.create(null, schema.text(t));
  return schema.nodes.doc.create(null, [
    p("preamble"), h(2, "A"), p("Alpha claim."), h(2, "B"), p("Beta claim."),
  ]);
}

function selectText(doc: ReturnType<typeof makeDoc>, needle: string) {
  let found: { from: number; to: number } | null = null;
  doc.descendants((node, pos) => {
    if (found || !node.isText || !node.text) return;
    const i = node.text.indexOf(needle);
    if (i >= 0) found = { from: pos + i, to: pos + i + needle.length };
  });
  if (!found) throw new Error("needle not found");
  const state = EditorState.create({ schema, doc });
  return state.apply(state.tr.setSelection(TextSelection.create(doc, found.from, found.to)));
}

describe("deriveClaimAnchor", () => {
  it("returns section 1 anchor for a claim in the first H2 section", () => {
    const r = deriveClaimAnchor(selectText(makeDoc(), "Alpha claim."));
    expect(r).toEqual({ sectionIndex: 1, anchor: "Alpha claim.", singleSection: true, headingOnly: false });
  });
  it("returns section 2 for a claim in the second H2 section", () => {
    const r = deriveClaimAnchor(selectText(makeDoc(), "Beta claim."));
    expect(r?.sectionIndex).toBe(2);
  });
  it("flags a heading-only selection", () => {
    const r = deriveClaimAnchor(selectText(makeDoc(), "A"));
    expect(r?.headingOnly).toBe(true);
  });
  it("returns null for an empty selection", () => {
    const doc = makeDoc();
    const state = EditorState.create({ schema, doc });
    expect(deriveClaimAnchor(state)).toBeNull();
  });
});
```

(If `collabEditorExtensions` isn't exported from `CitationNode.tsx`, import it from wherever `CollabEditor.tsx` builds the extension list — grep `collabEditorExtensions`.)

- [ ] **Step 2: Run test to verify it fails**

Run (in `frontend/`): `pnpm vitest run src/collab/selectionAnchor.test.ts > /tmp/f7.log 2>&1; echo EXIT=$?`
Expected: FAIL — cannot resolve `./selectionAnchor`.

- [ ] **Step 3: Implement**

```typescript
// frontend/src/collab/selectionAnchor.ts
import type { EditorState } from "prosemirror-state";

export interface ClaimAnchor {
  sectionIndex: number;
  anchor: string;
  singleSection: boolean;
  headingOnly: boolean;
}

/** Index of the H2 "section" (0 = preamble before the first H2) that contains the
 * given top-level position, matching Python split_sections' convention. */
function sectionIndexAt(state: EditorState, pos: number): number {
  const doc = state.doc;
  const topIndex = doc.resolve(pos).index(0); // which top-level child contains pos
  let count = 0;
  for (let i = 0; i < topIndex; i++) {
    const child = doc.child(i);
    if (child.type.name === "heading" && child.attrs.level === 2) count += 1;
  }
  return count;
}

export function deriveClaimAnchor(state: EditorState): ClaimAnchor | null {
  const { from, to, empty } = state.selection;
  if (empty) return null;
  const anchor = state.doc.textBetween(from, to, "\n\n", "");
  if (!anchor) return null;
  const secFrom = sectionIndexAt(state, from);
  const secTo = sectionIndexAt(state, Math.max(from, to - 1));
  // heading-only: no non-heading textblock with real text overlaps the range.
  let headingOnly = true;
  state.doc.nodesBetween(from, to, (node) => {
    if (node.isTextblock && node.type.name !== "heading" && node.textContent.trim()) {
      headingOnly = false;
    }
  });
  return { sectionIndex: secFrom, anchor, singleSection: secFrom === secTo, headingOnly };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `frontend/`): `pnpm vitest run src/collab/selectionAnchor.test.ts > /tmp/f7.log 2>&1; echo EXIT=$?` then `pnpm tsc --noEmit > /tmp/f7tsc.log 2>&1; echo EXIT=$?`
Expected: PASS + tsc clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/collab/selectionAnchor.ts frontend/src/collab/selectionAnchor.test.ts
git commit -m "feat(collab): pure claim-anchor derivation from a ProseMirror selection"
```

---

### Task 8: Frontend — `TiptapSelectionToolbar` + wire into `CollabEditor`

A floating Ask/Edit/Cite toolbar for the collab editor: three distinct gates, transport-settled guard, single-section/heading constraints, Ask/Edit → `useSubmitTurn`, Cite → `CitationComposer`.

**Files:**
- Create: `frontend/src/collab/TiptapSelectionToolbar.tsx`
- Modify: `frontend/src/collab/CollabEditor.tsx` (mount the toolbar; pass `provider`, `editor`, `draft` meta)
- Test: `frontend/src/collab/TiptapSelectionToolbar.test.tsx` (new)

**Interfaces:**
- Consumes: `deriveClaimAnchor` (Task 7); `useSubmitTurn` (`../api/turns`); `CitationComposer` (`../cockpit/CitationComposer`); provider `status`/`isSettled()` (Task-3 spec §4.E); `DraftOut` `head`/`can_edit`/`can_write` + `sections[i].sha`.
- Props: `{ editor: Editor; provider: CollabProvider; workspaceId: string; head: string; sections: { sha: string }[]; canEdit: boolean; canWrite: boolean }`.

- [ ] **Step 1: Write the failing test**

```typescript
// frontend/src/collab/TiptapSelectionToolbar.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TiptapSelectionToolbar } from "./TiptapSelectionToolbar";

// Minimal editor stub exposing a selection + doc via deriveClaimAnchor's shape.
vi.mock("./selectionAnchor", () => ({
  deriveClaimAnchor: vi.fn(() => ({ sectionIndex: 1, anchor: "Alpha claim.", singleSection: true, headingOnly: false })),
}));

function renderTB(over: Record<string, unknown> = {}) {
  const editor = { state: {}, on: vi.fn(), off: vi.fn() } as never;
  const provider = { status: "connected", isSettled: () => true } as never;
  const props = {
    editor, provider, workspaceId: "w1", head: "a".repeat(40),
    sections: [{ sha: "0".repeat(64) }, { sha: "1".repeat(64) }],
    canEdit: true, canWrite: true, ...over,
  };
  return render(
    <MantineProvider defaultColorScheme="dark">
      <QueryClientProvider client={new QueryClient()}>
        <TiptapSelectionToolbar {...(props as never)} />
      </QueryClientProvider>
    </MantineProvider>);
}

describe("TiptapSelectionToolbar gates", () => {
  it("shows Ask + Edit + Cite for an owner-writer with a settled provider", () => {
    renderTB();
    expect(screen.getByRole("button", { name: /ask/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /edit/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /cite/i })).toBeInTheDocument();
  });
  it("hides Edit for a non-owner writer, keeps Cite", () => {
    renderTB({ canEdit: false });
    expect(screen.queryByRole("button", { name: /edit/i })).toBeNull();
    expect(screen.getByRole("button", { name: /cite/i })).toBeInTheDocument();
  });
  it("hides Cite for a non-writer, keeps Ask", () => {
    renderTB({ canWrite: false, canEdit: false });
    expect(screen.queryByRole("button", { name: /cite/i })).toBeNull();
    expect(screen.getByRole("button", { name: /ask/i })).toBeInTheDocument();
  });
  it("disables Cite/Edit when the provider is not settled", () => {
    renderTB({ provider: { status: "offline", isSettled: () => false } as never });
    expect(screen.getByRole("button", { name: /cite/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /edit/i })).toBeDisabled();
  });
});
```

(The editor-selection wiring — `editor.on("selectionUpdate")` → recompute `deriveClaimAnchor(editor.state)` → floating position — is exercised via the mock. Match the render helper style of `SelectionToolbar.test.tsx`/`CitationComposer.test.tsx`, which inline `MantineProvider + QueryClientProvider`.)

- [ ] **Step 2: Run test to verify it fails**

Run (in `frontend/`): `pnpm vitest run src/collab/TiptapSelectionToolbar.test.tsx > /tmp/f8.log 2>&1; echo EXIT=$?`
Expected: FAIL — cannot resolve `./TiptapSelectionToolbar`.

- [ ] **Step 3: Implement the toolbar + wire it in**

Create `TiptapSelectionToolbar.tsx`. Skeleton (fill the Ask/Edit menu presets by reusing the same option set as `SelectionToolbar.tsx` — import or duplicate the presets):

```tsx
import { useEffect, useState } from "react";
import { Button, Group, Menu, Paper } from "@mantine/core";
import type { Editor } from "@tiptap/core";
import { useSubmitTurn } from "../api/turns";
import { CitationComposer } from "../cockpit/CitationComposer";
import { deriveClaimAnchor, type ClaimAnchor } from "./selectionAnchor";
import type { CollabProvider } from "./collabProvider";

interface Props {
  editor: Editor; provider: CollabProvider; workspaceId: string;
  head: string; sections: { sha: string }[]; canEdit: boolean; canWrite: boolean;
}

export function TiptapSelectionToolbar(props: Props) {
  const { editor, provider } = props;
  const [claim, setClaim] = useState<ClaimAnchor | null>(null);
  const [citeOpen, setCiteOpen] = useState(false);
  const submit = useSubmitTurn(props.workspaceId);

  useEffect(() => {
    const update = () => setClaim(deriveClaimAnchor(editor.state));
    editor.on("selectionUpdate", update);
    return () => { editor.off("selectionUpdate", update); };
  }, [editor]);

  if (!claim) return null;
  const settled = provider.status === "connected" && provider.isSettled();
  const sectionSha = props.sections[claim.sectionIndex]?.sha ?? "";
  const writeReady = settled && claim.singleSection && !claim.headingOnly && !!sectionSha;

  const runTurn = (requestType: "ask" | "modify", instruction: string) =>
    submit.mutate({
      content: instruction,
      selection_context: {
        request_type: requestType, target_artifact: "draft", section_index: claim.sectionIndex,
        base_commit_sha: props.head, base_section_sha: sectionSha, anchor: claim.anchor,
        from_offset: 0, to_offset: claim.anchor.length,  // best-effort; anchor text is authoritative
      },
    });

  return (
    <Paper role="toolbar" shadow="sm" p={4} withBorder>
      <Group gap={4}>
        {/* Ask — any member */}
        <Menu>
          <Menu.Target><Button size="xs" variant="subtle">Ask</Button></Menu.Target>
          <Menu.Dropdown>
            <Menu.Item onClick={() => runTurn("ask", `Explain: ${claim.anchor}`)}>Explain</Menu.Item>
          </Menu.Dropdown>
        </Menu>
        {/* Edit — owner (can_edit); disabled unless write-ready */}
        {props.canEdit && (
          <Menu>
            <Menu.Target><Button size="xs" variant="subtle" disabled={!writeReady}>Edit</Button></Menu.Target>
            <Menu.Dropdown>
              <Menu.Item onClick={() => runTurn("modify", `Shorten: ${claim.anchor}`)}>Shorten</Menu.Item>
            </Menu.Dropdown>
          </Menu>
        )}
        {/* Cite — any writer (can_write); disabled unless write-ready */}
        {props.canWrite && (
          <Button size="xs" variant="subtle" disabled={!writeReady} onClick={() => setCiteOpen(true)}>Cite</Button>
        )}
      </Group>
      {citeOpen && (
        <CitationComposer
          workspaceId={props.workspaceId}
          claim={{ section_index: claim.sectionIndex, base_commit_sha: props.head,
                   base_section_sha: sectionSha, anchor: claim.anchor }}
          onClose={() => setCiteOpen(false)}
          onDone={() => setCiteOpen(false)}
        />
      )}
    </Paper>
  );
}
```

> Check `CitationComposer`'s real prop shape (`frontend/src/cockpit/CitationComposer.tsx` — `ClaimContext = {section_index, base_commit_sha, base_section_sha, anchor}`) and adapt the prop names to match exactly; do not invent props. Check `useSubmitTurn`'s mutation variable shape from `SelectionToolbar.tsx` and mirror it. Buttons need accessible names ("Ask"/"Edit"/"Cite") for the tests.

Wire into `CollabEditor.tsx`: after the editor is created and `me`/draft meta resolved, render `<TiptapSelectionToolbar editor={editor} provider={provider} workspaceId={...} head={draft.head} sections={draft.sections} canEdit={draft.can_edit} canWrite={draft.can_write} />`. The draft read-model is already available where `collab_enabled` is read (the panel that mounts `CollabEditor`); thread `head`/`sections`/`can_edit`/`can_write` down as props if `CollabEditor` doesn't already have them.

- [ ] **Step 4: Run tests to verify they pass**

Run (in `frontend/`):
```bash
pnpm vitest run src/collab/TiptapSelectionToolbar.test.tsx src/collab/CollabEditor.test.tsx > /tmp/f8.log 2>&1; echo EXIT=$?
pnpm tsc --noEmit > /tmp/f8tsc.log 2>&1; echo EXIT=$?
```
Expected: PASS + tsc clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/collab/TiptapSelectionToolbar.tsx frontend/src/collab/TiptapSelectionToolbar.test.tsx frontend/src/collab/CollabEditor.tsx
git commit -m "feat(collab): Ask/Edit/Cite selection toolbar in the Tiptap collab editor"
```

---

### Task 9: Final verification sweep

Full flag-off parity + collab-path green + no schema drift. No new code — this is the gate.

- [ ] **Step 1: Backend grouped sweep (incl. `tests/test_api_spec.py`)**

```bash
EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/ tests/test_api_workspaces.py tests/test_api_spec.py \
  -p no:cacheprovider -q > /tmp/sweep_be.log 2>&1; echo EXIT=$?
```
Expected: all pass. `test_api_spec.py` confirms the OpenAPI schema is unchanged (AddCitationIn/Out untouched; the sync→async handler swap does not alter the schema).

- [ ] **Step 2: Frontend sweep**

```bash
cd frontend && pnpm tsc --noEmit > /tmp/sweep_tsc.log 2>&1; echo EXIT=$? && pnpm vitest run > /tmp/sweep_fe.log 2>&1; echo EXIT=$?
```
Expected: tsc clean; all vitest green.

- [ ] **Step 3: Lint + no-migration + parity checks**

```bash
uv run ruff check . > /tmp/sweep_ruff.log 2>&1; echo EXIT=$?
git -C . diff --name-only master...HEAD | grep -i "migrations/versions" && echo "MIGRATION PRESENT (FAIL)" || echo "no migration OK"
git -C . grep -n "enable_collab" -- 'src/**' | grep -v "def enable_collab" && echo "flag flip in src (FAIL)" || echo "no flag flip in src OK"
```
Expected: ruff clean; no migration; no `enable_collab` caller in `src/` (only the definition in `state.py`).

- [ ] **Step 4: Write the SDD completion report**

Write `/home/parlar_ai/dev/call-monitor/.worktrees/collab-slice3b5a/SDD_REPORT_3b5a.md`: tasks + commit SHAs (`git log --oneline`), backend pass count, frontend tsc+vitest, ruff, no-migration confirmation, CM6 flag-off parity confirmation, and any deviations. Do NOT merge, do NOT push. Print `SDD_3b5a_DONE`.

---

## Self-Review (author)

- **Spec coverage:** §4.A→Task 5; §4.B→Task 6; §4.C→Tasks 2+3+4; §4.D→Task 1; §4.E→Tasks 7+8; §4.F→Task 4; §5 evict contract→Task 3; §6 parity→Tasks 4/5/9; §7 tests distributed. All covered.
- **Type consistency:** `apply_citation_to_room` (Task 2) is consumed by Task 3 with matching kwargs; `direct_cite_via_kernel` (Task 3) consumed by Task 4 with matching kwargs; `deriveClaimAnchor` (Task 7) shape consumed by Task 8. `compute_citation_insert` optional offsets (Task 1) used in Tasks 2/3.
- **Placeholders:** none — every code step shows real code. Two explicit "verify the real signature and adapt" notes (CitationComposer props, useSubmitTurn variable shape, SelectionContext.target_artifact field) point at concrete files to check; the implementer must confirm names against the code, not invent them.
- **Known risk to watch in review:** Task 2's error remapping (compute error strings → CiteStale/CiteInvalid/CiteRefNotFound) must preserve `kernel_accept_citation`'s "all verify failures → _stale()" behavior — the existing kernel-citation suite is the guard (run in Task 2 Step 4). Task 3's post-mutation-evict is the highest-risk new behavior — its injected-failure test is mandatory.
