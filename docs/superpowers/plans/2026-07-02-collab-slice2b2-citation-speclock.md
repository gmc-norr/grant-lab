# Collaborative Editing — Slice 2b-2: citation-insert accept + spec-lock operation through the write kernel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route the two remaining write paths that still bypass the room-aware kernel for a **collab-enabled** artifact through the kernel invariant: (a) a **`citation_insert` accept** applies the citation to the LIVE Yjs room and co-commits the evidence-ledger row atomically via the checkpoint; (b) the **spec-lock OPERATION** checkpoints the live room under the commit mutex before freezing, so `SpecLockEvent.artifact_commit_sha` reflects the true current state. The flag-off (git-first) paths stay **byte-for-byte unchanged**; the collab flag stays **OFF in prod** (all new behavior is test-only until Slice 3).

**Architecture:** ONE invariant — every mutation that touches (or freezes provenance of) a collab-enabled artifact's canonical content acquires the per-workspace **commit mutex** and derives git from the LIVE room via `checkpoint_artifact`; there is exactly one serialized writer and git is always a checkpoint of the room. Citation accept reuses the proven prose mechanism (`apply_pm_json_to_fragment` whole-doc reassemble → incremental Yjs update → checkpoint), adding only the citation-specific DB verify/mint/idempotency (shared with the git-first path via an extracted pure core) and the evidence-ledger co-commit (`checkpoint_artifact(extra_files=…)`). The spec-lock op checkpoints from the **durable log** (not the in-memory room), exactly as the Slice-2a explicit-checkpoint endpoint already does, so it stays a simple synchronous endpoint.

**Tech Stack:** Python 3.12, FastAPI/Starlette, `pycrdt` 0.14.0, SQLAlchemy 2.x + PostgreSQL, pytest + testcontainers PG. **No new deps. No new migration** (reuses `yjs_updates`, `collab_artifact_state`, `proposed_changes`, `references`, `reference_chunks`, `spec_lock_events`, and the on-disk `citations.json` ledger).

## Global Constraints

- **Flag stays OFF in prod; 2b-2 is validated only via tests that call `enable_collab`.** A path is taken only when `state.is_collab_enabled(session, workspace_id, artifact=…)`. Flag-off ⇒ the existing git-first path runs, byte-for-byte unchanged. The prod flag-flip is Slice 3.
- **The single invariant (above): one serialized writer per collab artifact, git derived from the room via `checkpoint_artifact` under the commit mutex.** No path may write `proposal.md` / `citations.json` / `proposal-spec.md` for a collab-enabled artifact except through a checkpoint.
- **Atomicity (citation accept):** the FOR UPDATE pc-row lock, the staged Yjs update, the checkpoint commit of **both** `proposal.md` (from the room) **and** `citations.json` (the new ledger) in ONE git commit, plus the `accepted` status flip, all commit in a **single DB transaction** (one `session.commit()` in the threadpool body). Requires `store.append_update(commit=False)` (built in 2b-1) and `checkpoint_artifact(extra_files=…)` (Task 2).
- **Validation source = the serialized LIVE room, never git/disk.** Citation staleness compares `ProposedChange.base_section_sha` to the live section SHA (`split_sections(pm_to_markdown(ydoc_to_pm_json(room_doc)))[section_index].sha`), identical to prose. The idempotency marker set and `insert_marker` anchor-uniqueness both operate on the **room-serialized whole-doc markdown**, never on disk `proposal.md`.
- **Verify-failures → terminal `stale` (no new exception classes).** Reference-not-found/foreign/tombstoned, chunk-not-found, quote-not-found/ambiguous, and anchor-not-found/ambiguous are all caught inside the kernel and flip the row to `stale` (`evict=False`) — mirroring the git-first path (`accepting.py:216-231`) and prose's `StaleSectionError → stale`. `CommitMutexTimeout` (already `→429` in 2b-1) and `GitCommitError` from the checkpoint (post-mutation → generic evict arm → 500) are unchanged.
- **`CitationKernelDeferred` is DELETED** (the class, its `_CLEAN_ACCEPT_ERRORS` entry, the route's 409 branch, and the `pc.kind != "prose_edit"` gate in the kernel). No collab accept path returns 409-deferred any more.
- **Out of scope (documented as Slice-3 residuals):** `citation_remove` / `citation_update` proposal kinds have **no producers** (only `propose_prose_edit` / `propose_citation_insert` exist) — do not build kernel paths for them. The direct git-first citation tools (`remove_citation` / `update_citation` / `rename_citekey`) commit immediately and never create a `ProposedChange`; they still bypass the room and are a Slice-3 write-kernel concern — do NOT route them here.
- **Flag-off parity is a hard gate.** The full existing `tests/proposal_assist/test_accepting.py` (git-first citation accept) and `tests/test_api_spec_lock.py` (spec lock/unlock) suites must stay green **unchanged**. New collab behavior is added as new tests, never by mutating a flag-off assertion.
- **Tests** under `tests/proposal_assist/collab/`; reuse `tests/proposal_assist/` fixtures. Whole-suite testcontainers may flake — verify via grouped dir runs. `ruff check .` must pass (CI runs `ruff check .`, not format).

---

## File Structure

- Modify `src/call_manager/proposal_assist/tools/citations.py` — extract a pure `compute_citation_insert(...)` core (steps 1–12: DB verify + mint + idempotency + `insert_marker` + evidence-row build) from `_insert_citation_locked`; `_insert_citation_locked` becomes a thin git-first wrapper (mutex → read disk → core → `write_and_commit_files`) with **identical** behavior.
- Modify `src/call_manager/proposal_assist/collab/checkpoint.py` — add `extra_files: dict[str, str] | None = None` to `checkpoint_artifact`, merged into the single `write_and_commit_files` call.
- Modify `src/call_manager/proposal_assist/collab/kernel.py` — add `kernel_accept_citation(...)`; delete the `CitationKernelDeferred` class and the `pc.kind != "prose_edit"` gate; `kernel_accept_prose` unchanged otherwise.
- Modify `src/call_manager/proposal_assist/proposals/kernel_accept.py` — dispatch `_body` by `pc.kind` (prose → `kernel_accept_prose`, citation_insert → `kernel_accept_citation`); drop `CitationKernelDeferred` from imports + `_CLEAN_ACCEPT_ERRORS`. The `_load`/`operate`/`room.lock`/broadcast/evict scaffold is reused verbatim.
- Modify `src/call_manager/proposal_assist/web/proposals.py` — delete the `CitationKernelDeferred → 409` branch + its import.
- Modify `src/call_manager/web/api/workspaces.py` — branch `post_lock_spec` / `post_unlock_spec` on `is_collab_enabled(…, artifact="spec")`: flag-on → checkpoint-the-room-under-the-mutex then freeze (stamp the fresh checkpoint SHA + empty-check the post-checkpoint disk file); flag-off → the current body verbatim.
- Tests: `tests/proposal_assist/collab/test_collab_kernel_citation.py` (new), `tests/proposal_assist/collab/test_collab_spec_lock.py` (new); small additions to `test_collab_kernel_accept_route.py` if the route dispatch needs a route-level citation case.
- Create `docs/superpowers/notes/2026-07-02-collab-slice2b2-notes.md`.

**Interface summary (locked across tasks):**

```
# tools/citations.py (added — pure, no mutex, no git, no disk read)
def compute_citation_insert(
    session, *, workspace_id: uuid.UUID, proposal_text: str, ledger: dict,
    ref_uuid: uuid.UUID, anchor: str, chunk_index: int, evidence_quote: str,
    stance: str, confidence: str | None,
) -> dict
    # success  -> {"new_proposal_text": str, "new_ledger": dict, "ev_id": str, "citekey": str}
    # idempotent no-op -> {"idempotent": True, "ev_id": str, "citekey": str}
    # verify failure -> {"error": "<message>"}   (reference/chunk/quote/anchor)
    # NEVER returns _BUSY_ERROR or _GIT_COMMIT_FAILED_ERROR (no mutex/git here).

# collab/checkpoint.py (changed — additive param)
def checkpoint_artifact(session, *, workspace_id, artifact, workspaces_dir,
                        extra_files: dict[str, str] | None = None) -> CheckpointResult
    # when it commits, write_and_commit_files gets {fname: md, **(extra_files or {})}.

# collab/kernel.py (added)
def kernel_accept_citation(
    session, *, room_doc, proposed_change_id, workspace_id, approver, workspaces_dir,
) -> KernelAcceptResult
    # staleness vs live section; compute_citation_insert on room markdown + disk ledger;
    # error -> status=stale (evict=False); idempotent -> accepted at HEAD (evict=False);
    # else mutate room + append_update(commit=False) + checkpoint(extra_files=ledger) + accepted.
```

---

### Task 1: Extract `compute_citation_insert` pure core; `_insert_citation_locked` becomes a thin git-first wrapper (no behavior change)

**Files:** Modify `src/call_manager/proposal_assist/tools/citations.py`; parity via existing `tests/proposal_assist/test_accepting.py` + a new characterization test.

**Interfaces:** Produces `compute_citation_insert(session, *, workspace_id, proposal_text, ledger, ref_uuid, anchor, chunk_index, evidence_quote, stance, confidence) -> dict`. Consumed by `_insert_citation_locked` (Task 1) and `kernel_accept_citation` (Task 3).

- [ ] **Step 1: Write the failing characterization test** — pin that a git-first citation accept STILL writes both files in one commit and matches today's behavior (this test must pass before AND after the refactor).

```python
# tests/proposal_assist/collab/test_collab_kernel_citation.py  (Task 1 portion)
import subprocess
from pathlib import Path

from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME


def _files_in_head(repo: Path) -> set[str]:
    out = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
        cwd=repo, capture_output=True, text=True,
    ).stdout
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def test_compute_citation_insert_is_pure(pg_session_factory):
    """compute_citation_insert must NOT touch git/disk/mutex: called twice with the
    same inputs it returns the same new_proposal_text/citekey and writes nothing."""
    from call_manager.proposal_assist.tools.citations import compute_citation_insert
    # (fixture builds a workspace + Reference + ReferenceChunk containing the quote;
    #  see _mk_ref_and_chunk helper below.)
    ...
```

  **Implementer note:** build the ref/chunk fixture from the patterns already in `tests/proposal_assist/test_accepting.py` (search it for `ReferenceChunk(` and `Reference(`). The characterization assertion is: after a git-first `_accept_citation_insert`, `_files_in_head(repo)` contains BOTH `proposal.md` and `citations.json`, and the marker is in `proposal.md`. Add this alongside (not replacing) the existing `test_accepting.py` citation cases, which remain the primary parity gate.

- [ ] **Step 2: Extract the core.** In `citations.py`, create `compute_citation_insert(...)` containing **steps 1–12** of the current `_insert_citation_locked` (read `citations.py:586-710`) with these exact changes:
  - It takes `proposal_text: str` and `ledger: dict` as **inputs** (does NOT call `_read_proposal_and_ledger`, does NOT read disk).
  - It does NOT call `acquire_commit_mutex` (step 0 stays in the caller) and does NOT call `write_and_commit_files` (step 13 stays in the caller).
  - Step 5 idempotency (`_find_idempotent_row(ledger, …, proposal_text=proposal_text)`) → on hit, return `{"idempotent": True, "ev_id": existing["id"], "citekey": existing.get("citekey")}` (NO `commit_sha` — the caller stamps HEAD).
  - Steps 1,2,4 verify failures → return `{"error": "<same message string as today>"}` (reference not found / tombstoned / chunk not found / quote not found / ambiguous / anchor not found / ambiguous). Keep the exact message strings.
  - Success → return `{"new_proposal_text": new_proposal_text, "new_ledger": new_ledger, "ev_id": ev_id, "citekey": citekey}`.
  - `_resolve_citekey_for_ref(session, ledger=ledger, ref=ref, workspace_id=workspace_id)` stays as-is (its `begin_nested` citekey-cache savepoint runs on the caller's session — unchanged).

- [ ] **Step 3: Rewrite `_insert_citation_locked` as the thin git-first wrapper** (behavior identical to today):

```python
def _insert_citation_locked(ctx, session, *, ref_uuid, anchor, chunk_index,
                            evidence_quote, stance, confidence, owns_txn=True):
    try:
        acquire_commit_mutex(session, ctx.workspace_id)
    except CommitMutexTimeout:
        return {"error": _BUSY_ERROR}
    read = _read_proposal_and_ledger(ctx)
    if isinstance(read, dict):
        return read
    proposal_text, ledger = read
    result = compute_citation_insert(
        session, workspace_id=ctx.workspace_id, proposal_text=proposal_text, ledger=ledger,
        ref_uuid=ref_uuid, anchor=anchor, chunk_index=chunk_index,
        evidence_quote=evidence_quote, stance=stance, confidence=confidence,
    )
    if "error" in result:
        return result
    if result.get("idempotent"):
        head = _current_head(ctx.repo_dir)
        if head:
            ctx.agent_view_head = head
        return {"ev_id": result["ev_id"], "citekey": result.get("citekey"),
                "commit_sha": head, "idempotent": True}
    try:
        sha = write_and_commit_files(
            ctx.repo_dir,
            {PROPOSAL_FILENAME: result["new_proposal_text"],
             CITATIONS_FILENAME: dump_ledger(result["new_ledger"])},
            f"cite: add {result['citekey']} [{result['ev_id']}]",
        )
    except GitCommitError:
        logger.exception("insert_citation: atomic commit failed for workspace %s (ev %s)",
                         ctx.workspace_id, result["ev_id"])
        return {"error": _GIT_COMMIT_FAILED_ERROR}
    ctx.agent_view_head = sha
    return {"ev_id": result["ev_id"], "citekey": result["citekey"], "commit_sha": sha}
```

  **Implementer note:** verify the return shape of the ORIGINAL `_insert_citation_locked` success path (`citations.py` after line 738 — read it) and reproduce it EXACTLY (keys `ev_id`, `citekey`, `commit_sha`, and any `idempotent`). The `owns_txn` param is retained for signature compatibility even if now unused inside; check its remaining uses (`_resolve_citekey_for_ref` savepoint semantics) and preserve them.

- [ ] **Step 4: Run the parity gate.** `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/test_accepting.py -q` (all green, unchanged) + the new `test_compute_citation_insert_is_pure`. Expected: PASS.

- [ ] **Step 5: Commit** (`refactor(cite): extract pure compute_citation_insert core; _insert_citation_locked = thin git-first wrapper`).

---

### Task 2: `checkpoint_artifact(extra_files=…)` — co-commit the ledger atomically

**Files:** Modify `src/call_manager/proposal_assist/collab/checkpoint.py`; Test `tests/proposal_assist/collab/test_collab_checkpoint.py` (add).

- [ ] **Step 1: Write the failing test** — a checkpoint with `extra_files={"citations.json": "<json>"}` commits BOTH `proposal.md` (from the room) and `citations.json` in ONE commit, and still writes exactly one `ArtifactVersion` + one `collab_checkpoint` `WorkspaceEvent`.

```python
def test_checkpoint_co_commits_extra_files(pg_session_factory, tmp_path):
    # seed a room with an authored edit (reuse _seed_room_with_edit), then:
    from call_manager.proposal_assist.citations.ledger import dump_ledger, empty_ledger
    payload = dump_ledger(empty_ledger())  # canonical shape: {"version": N, "evidence": []}
    res = checkpoint_artifact(s, workspace_id=wid, artifact="draft",
                              workspaces_dir=str(tmp_path),
                              extra_files={"citations.json": payload})
    s.commit()
    assert res.committed is True
    repo = Path(tmp_path) / str(wid) / "repo"
    assert (repo / "citations.json").read_text() == payload
    assert "citations.json" in _files_in_head(repo) and PROPOSAL_FILENAME in _files_in_head(repo)
```

- [ ] **Step 2: Implement.** In `checkpoint_artifact` (read `checkpoint.py:99-105`), on the committing path build the files dict and pass it through:

```python
    files = {fname: md}
    if extra_files:
        files.update(extra_files)
    commit_sha = write_and_commit_files(
        repo, files, f"collab checkpoint: {artifact} (authors:{author_tag})"
    )
```

  Add `extra_files: dict[str, str] | None = None` to the signature. Do NOT touch the no-op branches (`max_id <= watermark or head == ""` and the seed-only branch) — `extra_files` is only relevant when content is committed, and a citation accept always stages a real update first so it never no-ops. `write_and_commit_files` already accepts a multi-file dict (`gitio.py:30-32`).

- [ ] **Step 3: Run** the new test + the existing `test_collab_checkpoint.py` (unchanged green). Expected: PASS.

- [ ] **Step 4: Commit** (`feat(collab): checkpoint_artifact co-commits extra_files atomically`).

---

### Task 3: `kernel_accept_citation` — apply the citation to the live room + co-commit the ledger

**Files:** Modify `src/call_manager/proposal_assist/collab/kernel.py`; Test `tests/proposal_assist/collab/test_collab_kernel_citation.py` (add).

**Interfaces:** Produces `kernel_accept_citation(session, *, room_doc, proposed_change_id, workspace_id, approver, workspaces_dir) -> KernelAcceptResult`.

- [ ] **Step 1: Write the failing tests** (kernel-direct, mirroring `test_collab_kernel_accept.py`'s harness — `_live_room`, `_mk_pc`, plus a `_mk_ref_and_chunk` helper):
  - **happy path:** collab-enabled draft with a ref+chunk containing the quote; a pending `citation_insert` pc (payload `{reference_id, anchor, chunk_index, evidence_quote, stance, confidence}`, `section_index`+`base_section_sha` from the live room) → `kernel_accept_citation` → `status=accepted`, `res.committed is True`, `res.update is not None`; the checkpointed `proposal.md` contains the marker `[@…]<!-- ev:ev-… -->` in the target section; `citations.json` in HEAD contains the evidence row (matching ev_id); exactly one `ArtifactVersion`.
  - **stale:** apply a live uncheckpointed edit to the target section first (changes its SHA), then accept a citation pc whose `base_section_sha` is the pre-edit SHA → `status=stale`, git unchanged, no ledger row.
  - **verify-fail → stale:** a pc whose `evidence_quote` is absent from the chunk → `status=stale`, git unchanged (mirrors git-first).
  - **anchor moved → stale:** duplicate the anchor string into a *different* section (target section SHA still fresh) → `insert_marker` raises `AnchorError` → caught → `status=stale`.
  - **idempotent no-op:** accept the same citation twice → second is `accepted` with no new `ArtifactVersion` and `resulting_commit_sha == HEAD` (marker set derived from the LIVE room, not disk).

- [ ] **Step 2: Implement** `kernel_accept_citation` (sibling to `kernel_accept_prose`; reuse `_load_scoped`, `_live_sections`, `_head_or_empty`, `acquire_commit_mutex`, `apply_pm_json_to_fragment`, `checkpoint_artifact`):

```python
def kernel_accept_citation(session, *, room_doc, proposed_change_id,
                           workspace_id, approver, workspaces_dir) -> KernelAcceptResult:
    pc = _load_scoped(session, proposed_change_id, workspace_id)  # FOR UPDATE
    if pc.status != "pending":
        from call_manager.proposal_assist.proposals.accepting import ProposedChangeNotPending
        raise ProposedChangeNotPending(pc.status)
    # (kind is guaranteed citation_insert by the dispatcher; assert defensively.)
    artifact = pc.target_artifact
    sections = _live_sections(room_doc)
    if not 0 <= pc.section_index < len(sections):
        raise IndexError(f"section_index {pc.section_index} out of range")
    live = sections[pc.section_index]

    def _stale() -> KernelAcceptResult:
        pc.status = "stale"
        pc.resolved_by = approver.id
        pc.resolved_at = datetime.now(UTC)
        session.flush()
        return KernelAcceptResult(pc=pc, update=None, committed=False, evict=False)

    # Staleness vs the LIVE section SHA (never git) — identical to prose.
    if live.sha != pc.base_section_sha:
        return _stale()

    # Freeze the writer BEFORE minting/reading the ledger (H3 pattern). Re-entered
    # by checkpoint_artifact as an in-txn no-op.
    acquire_commit_mutex(session, workspace_id)

    # Ledger is disk-canonical (NOT in the room); read it under the mutex via the
    # canonical loader (load_ledger(None) -> empty_ledger(), the exact shape the
    # git-first path uses).
    repo = workspace_repo_dir(workspaces_dir, workspace_id)  # positional, per kernel_accept_prose
    _cpath = repo / CITATIONS_FILENAME
    ledger = load_ledger(_cpath.read_text() if _cpath.exists() else None)
    room_md = pm_to_markdown(ydoc_to_pm_json(room_doc))  # whole-doc, room-canonical
    p = pc.payload
    result = compute_citation_insert(
        session, workspace_id=workspace_id, proposal_text=room_md, ledger=ledger,
        ref_uuid=uuid.UUID(str(p["reference_id"])), anchor=p["anchor"],
        chunk_index=p["chunk_index"], evidence_quote=p["evidence_quote"],
        stance=p.get("stance", "supports"), confidence=p.get("confidence"),
    )
    if "error" in result:
        return _stale()  # all verify failures are terminal-stale (git-first parity)
    if result.get("idempotent"):
        pc.status = "accepted"
        pc.resolved_by = approver.id
        pc.resolved_at = datetime.now(UTC)
        pc.resulting_commit_sha = _head_or_empty(repo)
        session.flush()
        return KernelAcceptResult(pc=pc, update=None, committed=False, evict=False)

    # Apply the marker-bearing whole-doc markdown to the live room fragment.
    update = apply_pm_json_to_fragment(room_doc, markdown_to_pm(result["new_proposal_text"]))
    store.append_update(session, workspace_id=workspace_id, artifact=artifact,
                        actor_id=approver.id, update=update, commit=False)
    cp = checkpoint_artifact(
        session, workspace_id=workspace_id, artifact=artifact, workspaces_dir=workspaces_dir,
        extra_files={CITATIONS_FILENAME: dump_ledger(result["new_ledger"])},
    )
    pc.status = "accepted"
    pc.resolved_by = approver.id
    pc.resolved_at = datetime.now(UTC)
    pc.resulting_commit_sha = cp.commit_sha
    session.flush()
    return KernelAcceptResult(pc=pc, update=update, committed=cp.committed, evict=True)
```

  **Implementer notes:**
  - Add imports: `compute_citation_insert` + `CITATIONS_FILENAME` from `tools/citations.py`; `dump_ledger` + `load_ledger` from `citations/ledger.py` (the canonical ledger module — `dump_ledger`/`load_ledger`/`empty_ledger`/`append_evidence` all live there; `load_ledger(None)` returns `empty_ledger()` = `{"version": LEDGER_VERSION, "evidence": []}`). The `Reference`/`ReferenceChunk` verify+mint helpers are all internal to `compute_citation_insert`.
  - Ledger read: `load_ledger((repo / CITATIONS_FILENAME).read_text() if that path exists else None)` — reuse the canonical loader; do NOT hand-roll the empty shape (its key is `"evidence"`, not `"citations"`).
  - Delete the `CitationKernelDeferred` class and the `if pc.kind != "prose_edit": raise CitationKernelDeferred(...)` gate from `kernel_accept_prose` in this task (the dispatcher in Task 4 guarantees kind).
  - `insert_marker` runs inside `compute_citation_insert` on the WHOLE room markdown → global anchor uniqueness (matches git-first). The reassembled `new_proposal_text` is the whole doc, so `apply_pm_json_to_fragment` gets the full doc (as prose does).

- [ ] **Step 3: Run** the Task-3 tests. Expected: PASS. (If `is_h2`/section indexing surprises arise, mirror `test_collab_kernel_accept.py`'s section-1 lesson — "## …" is section 1, preamble is 0.)

- [ ] **Step 4: Commit** (`feat(collab): kernel_accept_citation — apply citation to live room + co-commit ledger`).

---

### Task 4: Dispatch accept by kind; delete `CitationKernelDeferred` end-to-end

**Files:** Modify `src/call_manager/proposal_assist/proposals/kernel_accept.py`, `src/call_manager/proposal_assist/web/proposals.py`; Test `tests/proposal_assist/collab/test_collab_kernel_accept_route.py` (add a route-level citation case + a flag-off citation parity case).

- [ ] **Step 1: Write the failing route tests:**
  - **collab citation accept via HTTP:** enable collab, ref+chunk, pending `citation_insert` pc, `POST …/accept` → 200 `accepted`, marker in the checkpointed `proposal.md`, ledger row in HEAD.
  - **flag-off citation parity:** without `enable_collab`, `POST …/accept` of a citation pc still runs the git-first path (existing behavior).
  - **(convergence, optional):** a connected WS client receives the citation update and converges (reuse the prose convergence pattern).

- [ ] **Step 2: Dispatch by kind** in `accept_via_kernel` (`kernel_accept.py`) — branch the sync `_body` on `pc.kind`; keep `_load`/`operate`/`room.lock`/broadcast/evict identical:

```python
                def _body() -> KernelAcceptResult:
                    with session_factory() as s:
                        pc0 = s.get(ProposedChange, pc_id)
                        kind = pc0.kind if pc0 is not None else None
                        if kind == "citation_insert":
                            res = kernel_accept_citation(
                                s, room_doc=room.doc, proposed_change_id=pc_id,
                                workspace_id=workspace_id, approver=approver,
                                workspaces_dir=workspaces_dir)
                        else:
                            res = kernel_accept_prose(
                                s, room_doc=room.doc, proposed_change_id=pc_id,
                                workspace_id=workspace_id, approver=approver,
                                workspaces_dir=workspaces_dir)
                        s.commit()
                        s.expunge(res.pc)
                        return res
```

  Update imports: import `kernel_accept_citation`; remove `CitationKernelDeferred` from the `kernel` import and from `_CLEAN_ACCEPT_ERRORS`.

  **Implementer note:** a cheap extra `s.get(ProposedChange, pc_id)` to read `kind` before dispatch is acceptable (one indexed PK read); the real FOR-UPDATE load happens inside each kernel fn via `_load_scoped`. Alternatively thread `kind` through from the route's `_route` (which already loads the pc) — pick whichever keeps the diff smallest; if threading `kind`, extend `_route`'s return and `accept_via_kernel`'s signature. Prefer NOT expanding the public signature — the local `s.get` is simplest.

- [ ] **Step 3: Delete the route's 409 branch** (`web/proposals.py`) — remove the `except CitationKernelDeferred: → 409` block and the `CitationKernelDeferred` import. Everything else in the route's exception mapping is unchanged.

- [ ] **Step 4: Run** the route tests + the existing `test_collab_kernel_accept_route.py` (the prose cases must stay green; the old "citation deferred → 409" case, if present, is now replaced by the citation-accepts case — update it, do not leave a dead 409 assertion).

- [ ] **Step 5: Commit** (`feat(collab): route citation_insert accept through the kernel; drop CitationKernelDeferred`).

---

### Task 5: Spec-lock OPERATION through the kernel (checkpoint-then-freeze for a collab spec)

**Files:** Modify `src/call_manager/web/api/workspaces.py` (`post_lock_spec`, `post_unlock_spec`); Test `tests/proposal_assist/collab/test_collab_spec_lock.py` (new).

**Design:** `checkpoint_artifact` rebuilds the doc from the **durable `yjs_updates` log** (not the in-memory room), exactly as the Slice-2a explicit-checkpoint endpoint (`web/collab_checkpoint.py`) already does — so the lock path stays a **synchronous** endpoint, no `MANAGER.operate`/`room.lock` needed. After the checkpoint the on-disk spec file reflects the room, so the empty-check reads disk post-checkpoint.

- [ ] **Step 1: Write the failing tests:**
  - **collab lock stamps the fresh checkpoint SHA:** enable collab for `spec`; seed an uncheckpointed live-room spec edit (persist a yjs_update via `store.append_update`); `POST …/spec/lock` (owner) → 200 `locked`; the `SpecLockEvent.artifact_commit_sha` equals the NEW checkpoint SHA (not the prior HEAD), and `proposal-spec.md` on disk now matches the room content.
  - **collab empty-check reads the room, not stale disk:** disk `proposal-spec.md` empty but the room has content → lock SUCCEEDS (post-checkpoint disk is non-empty). (And the converse: room empty → `409 cannot lock an empty spec`.)
  - **collab unlock** stamps a fresh checkpoint SHA too (symmetric).
  - **flag-off parity:** `tests/test_api_spec_lock.py` stays green unchanged (branch below must not alter the flag-off body).

- [ ] **Step 2: Implement** — branch each endpoint at the top on the collab flag; flag-off runs the current body verbatim. Flag-on (inside the same `session` + mutex):

```python
    is_collab = collab_state.is_collab_enabled(session, workspace_id=ws.id, artifact="spec")
    try:
        acquire_commit_mutex(session, ws.id)
        if is_collab:
            # Freeze the TRUE current state: checkpoint the room (from the durable
            # log) under the mutex — re-enters the same xact lock as a no-op.
            cp = checkpoint_artifact(session, workspace_id=ws.id, artifact="spec",
                                     workspaces_dir=settings.workspaces_dir)
            frozen_sha = cp.commit_sha  # the just-written (or current) checkpoint SHA
        else:
            frozen_sha = current_head(repo_dir)
        session.refresh(ws, ["spec_locked_at", "spec_locked_by"])
        if ws.spec_locked_at is None:
            if (repo_dir / SPEC_FILENAME).read_text().strip() == "":  # post-checkpoint on collab
                raise HTTPException(status_code=409, detail="cannot lock an empty spec")
            ws.spec_locked_at = datetime.now(UTC)
            ws.spec_locked_by = user.id
            session.add(SpecLockEvent(workspace_id=ws.id, action="lock",
                                      actor_id=user.id, artifact_commit_sha=frozen_sha))
        locked_at, locked_by_id = ws.spec_locked_at, ws.spec_locked_by
        session.commit()
    except CommitMutexTimeout as e:
        session.rollback(); raise HTTPException(status_code=429, detail=BUSY_ERROR_MESSAGE,
                                                headers={"Retry-After": "2"}) from e
    except GitCommitError as e:
        # collab checkpoint write failure is a 500 (mirror collab_checkpoint.py), NOT
        # the flag-off "no committable HEAD" 404. Distinguish by is_collab.
        session.rollback()
        if is_collab:
            raise HTTPException(status_code=500, detail="lock failed — try again") from e
        logger.warning("workspace %s: git HEAD unavailable (%s)", workspace_id, e)
        raise HTTPException(status_code=404, detail="no proposal-spec.md yet") from e
```

  Apply the symmetric change to `post_unlock_spec` (checkpoint-then-clear, stamp `frozen_sha` on the unlock `SpecLockEvent`). Read both current bodies (`workspaces.py:470-518` / `:521-560`) and preserve every flag-off detail (idempotent re-lock writes no audit row; ownership check `require_owner` runs first; the existing 404/429 mappings on the flag-off path).

  **Implementer notes:**
  - **New imports needed** in `workspaces.py`: `from ...collab.checkpoint import checkpoint_artifact` and `from ...collab import state as collab_state`. Already imported (verified): `current_head`, `acquire_commit_mutex`, `SpecLockEvent`, `SPEC_FILENAME`, `load_ledger`, `find_markers`, `BUSY_ERROR_MESSAGE` (used by the existing 429 handler), `require_owner`.
  - `require_owner` must run BEFORE the mutex/checkpoint (unchanged position at `workspaces.py:481`).
  - The empty-check for a collab spec reads disk **after** the checkpoint (post-checkpoint disk == room). This is correct because a committing checkpoint writes the room markdown to disk, and a no-op checkpoint means disk already equals the room. Do NOT read `room.doc` directly (there may be no open room — the log is canonical).
  - `checkpoint_artifact` on a collab-enabled spec with only an unauthored hydration seed no-ops cleanly and returns the current HEAD as `commit_sha` — a lock then stamps that real HEAD. Fine.

- [ ] **Step 3: Run** the new collab spec-lock tests + `tests/test_api_spec_lock.py` (unchanged green) + `tests/test_api_spec.py`. Expected: PASS.

- [ ] **Step 4: Commit** (`feat(collab): spec-lock operation checkpoints the live room before freezing`).

---

### Task 6: Regression sweep + OpenAPI

- [ ] **Step 1: OpenAPI drift** — no route signature/shape changed (accept route body-only; spec-lock endpoints same path/response). Run `cd frontend && EMBEDDING_MODEL=bge-m3 pnpm gen:api && git diff --exit-code src/api/schema.gen.ts` → expect **no drift**; if drift, investigate before committing.

- [ ] **Step 2: Grouped-dir sweep** — `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/ tests/proposal_assist/proposals/ tests/proposal_assist/test_accepting.py tests/test_api_spec_lock.py tests/test_api_spec.py -q` green; `uv run ruff check .` clean.

- [ ] **Step 3: Commit** (`test(collab): 2b-2 regression sweep`).

---

### Task 7: Notes

- [ ] **Step 1: Write** `docs/superpowers/notes/2026-07-02-collab-slice2b2-notes.md` — what shipped (citation accept through the kernel; the shared `compute_citation_insert` core + git-first thin wrapper; `checkpoint_artifact(extra_files)` co-commit; spec-lock checkpoint-then-freeze; `CitationKernelDeferred` deleted); the invariants proven by tests (citation stale-vs-live, verify-fail→stale, anchor-moved→stale, idempotent no-op, ledger+marker atomic co-commit, lock stamps fresh SHA + empty-check reads room, flag-off parity); **flag still OFF in prod**; deferrals — the direct git-first citation tools (remove/update/rename) still bypass the room (Slice-3 write-kernel residual); the Slice-3 flag-flip prerequisites carried over from 2b-1 (staleness normalized-vs-raw-git-base SHA; whole-fragment replace loses concurrent edits; `evict()` doesn't disconnect clients); `save_section` git-canonical switchover + Tier-1/2 post-checkpoint re-fire still land at the flag flip.

- [ ] **Step 2: Commit** (`docs(collab): Slice 2b-2 notes`).

---

## Final verification (before whole-branch review)

```bash
EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/ tests/proposal_assist/proposals/ tests/proposal_assist/test_accepting.py tests/test_api_spec_lock.py tests/test_api_spec.py -q
uv run ruff check .
cd frontend && EMBEDDING_MODEL=bge-m3 pnpm gen:api && git diff --exit-code src/api/schema.gen.ts
```

---

## Self-Review (author checklist — completed)

**1. Scope coverage:** citation_insert accept through the kernel (Tasks 3+4) ✓; ledger co-commit atomic (Task 2) ✓; git-first/kernel share verify+mint (Task 1) ✓; spec-lock op checkpoints room + stamps fresh SHA (Task 5) ✓; `CitationKernelDeferred` deleted (Tasks 3+4) ✓; flag-off parity gated (Tasks 1+4+5) ✓. Deferred+documented: citation_remove/update (no producers), direct git-first citation tools, Slice-3 flag-flip prereqs.

**2. Placeholder scan:** Task 1's core body says "steps 1–12 of the current function" rather than transcribing 150 lines — deliberate (the implementer moves real code, verifying the exact message strings/return shape against the source), matching how 2a/2b-1 handled refactors. All NEW functions (`kernel_accept_citation`, checkpoint extension, dispatch, spec-lock branch) carry complete code.

**3. Type consistency:** `compute_citation_insert(... proposal_text, ledger ...) -> dict` used identically in Task 1 (wrapper) and Task 3 (kernel); `checkpoint_artifact(..., extra_files=…)` used in Tasks 2/3/5; `KernelAcceptResult(pc, update, committed, evict)` reused from 2b-1; `dump_ledger`/`CITATIONS_FILENAME` sourced from `tools/citations.py`.

**4. The load-bearing invariant flagged for the implementer:** the citation accept must be ONE transaction — `append_update(commit=False)` + `checkpoint_artifact(extra_files=ledger)` (caller-committed) + status flip + one `session.commit()` in `_body`. The ledger row and the room marker MUST land in the same `write_and_commit_files` (via `extra_files`) so a git failure rolls back both and evicts the room (no marker-without-row corruption). Proven-in-spike: a minted citation survives the room reassemble→serialize→find_markers loop and a synced client converges byte-identically.
