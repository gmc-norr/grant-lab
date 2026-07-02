# Collaborative Editing — Slice 2b-2: citation-insert accept + spec-lock operation through the write kernel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route the two remaining write paths that still bypass the room-aware kernel for a **collab-enabled** artifact through the kernel invariant: (a) a **`citation_insert` accept** applies the citation to the LIVE Yjs room and co-commits the evidence-ledger row atomically via the checkpoint; (b) the **spec-lock OPERATION** checkpoints the live room (from the durable log) under the commit mutex and records the fresh checkpoint SHA on `SpecLockEvent.artifact_commit_sha`. The flag-off (git-first) paths stay **byte-for-byte unchanged**; the collab flag stays **OFF in prod** (all new behavior is test-only until Slice 3).

> **Scope note (spec-lock, per Codex High):** setting `spec_locked_at` + stamping a fresh checkpoint SHA does **NOT** freeze a live room. Clients already connected retain their connect-time `can_write=True` (`ws_auth.resolve_access` is evaluated at handshake, `room.py:90` gates on that value), so a connected writer could keep mutating a "locked" spec room; and the checkpoint reads the durable log, not the in-memory room, so it can lag an update that has been applied to `room.doc` but whose `yjs_updates` row has not committed. **Neither gap is reachable in 2b-2** — the flag is OFF and every tested path seeds the durable log directly (no live WS sockets). Live-writer blocking on lock (connect-time permission refresh + `evict()`/disconnect of connected clients) is a **Slice-3** concern, bundled with the already-deferred "`evict()` doesn't disconnect." 2b-2 deliberately narrows the spec-lock invariant to **"checkpoint the durable log + record the fresh SHA,"** not "freeze the room."

**Architecture:** ONE invariant — every mutation that writes a collab-enabled artifact's canonical **markdown** content (`proposal.md` / `citations.json`) acquires the per-workspace **commit mutex** and derives git from the LIVE room via `checkpoint_artifact`; there is exactly one serialized writer and git is always a checkpoint of the room. Citation accept reuses the proven prose mechanism (`apply_pm_json_to_fragment` whole-doc reassemble → incremental Yjs update → checkpoint), adding only the citation-specific DB verify/mint/idempotency (shared with the git-first path via an extracted core) and the evidence-ledger co-commit (`checkpoint_artifact(extra_files=…)`). Spec lock/unlock is **not** a markdown mutation; on the collab path it only checkpoints from the **durable log** (exactly as the Slice-2a explicit-checkpoint endpoint already does, so it stays a simple synchronous endpoint) to record a fresh provenance SHA — it does not freeze the room (see Scope note).

**Tech Stack:** Python 3.12, FastAPI/Starlette, `pycrdt` 0.14.0, SQLAlchemy 2.x + PostgreSQL, pytest + testcontainers PG. **No new deps. No new migration** (reuses `yjs_updates`, `collab_artifact_state`, `proposed_changes`, `references`, `reference_chunks`, `spec_lock_events`, and the on-disk `citations.json` ledger).

## Global Constraints

- **Flag stays OFF in prod; 2b-2 is validated only via tests that call `enable_collab`.** A path is taken only when `state.is_collab_enabled(session, workspace_id, artifact=…)`. Flag-off ⇒ the existing git-first path runs, byte-for-byte unchanged. The prod flag-flip is Slice 3.
- **The single invariant (above): one serialized writer per collab artifact, git derived from the room via `checkpoint_artifact` under the commit mutex.** No path may write `proposal.md` / `citations.json` (a collab-enabled artifact's canonical **markdown** content) except through a checkpoint. Spec lock/unlock does NOT write markdown; on the collab path it only checkpoints the durable log to record a fresh provenance SHA (it does **not** freeze the room — see the Goal's Scope note; live-writer blocking is Slice 3).
- **Preserve the `references.citekey` cache write + its `owns_txn` semantics (Codex High).** Today `_insert_citation_locked` step 14 (`citations.py:740-770`) updates `ref.citekey` AFTER the commit — with `owns_txn=True`⇒`session.commit()`, `owns_txn=False`⇒`begin_nested()`+`flush()` (savepoint, so a racing IntegrityError never poisons the caller's accept txn). This is guarded by `test_accept_citation_atomic_when_citekey_cache_branch_runs` (`test_accepting.py:385`, which seeds `citekey=None` to force the branch and spies on `commit` to prove the accept-replay issues ZERO mid-replay commits). The refactor MUST relocate this write into a shared post-commit helper used by BOTH the git-first wrapper and `kernel_accept_citation` — behavior byte-for-byte identical, just moved.
- **Atomicity (citation accept):** the FOR UPDATE pc-row lock, the staged Yjs update, the checkpoint commit of **both** `proposal.md` (from the room) **and** `citations.json` (the new ledger) in ONE git commit, plus the `accepted` status flip, all commit in a **single DB transaction** (one `session.commit()` in the threadpool body). Requires `store.append_update(commit=False)` (built in 2b-1) and `checkpoint_artifact(extra_files=…)` (Task 2).
- **Validation source = the serialized LIVE room, never git/disk.** Citation staleness compares `ProposedChange.base_section_sha` to the live section SHA (`split_sections(pm_to_markdown(ydoc_to_pm_json(room_doc)))[section_index].sha`), identical to prose. The idempotency marker set and `insert_marker` anchor-uniqueness both operate on the **room-serialized whole-doc markdown**, never on disk `proposal.md`.
- **Verify-failures → terminal `stale` (no new exception classes).** Reference-not-found/foreign/tombstoned, chunk-not-found, quote-not-found/ambiguous, and anchor-not-found/ambiguous are all caught inside the kernel and flip the row to `stale` (`evict=False`) — mirroring the git-first path (`accepting.py:216-231`) and prose's `StaleSectionError → stale`. `CommitMutexTimeout` (already `→429` in 2b-1) and `GitCommitError` from the checkpoint (post-mutation → generic evict arm → 500) are unchanged.
- **`CitationKernelDeferred` is DELETED** (the class, its `_CLEAN_ACCEPT_ERRORS` entry, the route's 409 branch, and the `pc.kind != "prose_edit"` gate in the kernel). No collab accept path returns 409-deferred any more.
- **Out of scope (documented as Slice-3 residuals):** `citation_remove` / `citation_update` proposal kinds have **no producers** (only `propose_prose_edit` / `propose_citation_insert` exist) — do not build kernel paths for them. The direct git-first citation tools (`remove_citation` / `update_citation` / `rename_citekey`) commit immediately and never create a `ProposedChange`; they still bypass the room and are a Slice-3 write-kernel concern — do NOT route them here.
- **Flag-off parity is a hard gate.** The full existing `tests/proposal_assist/proposals/test_accepting.py` (git-first citation accept — note the real path is under `proposals/`) and `tests/test_api_spec_lock.py` (spec lock/unlock) suites must stay green **unchanged**. New collab behavior is added as new tests, never by mutating a flag-off assertion.
- **Tests** under `tests/proposal_assist/collab/`; reuse `tests/proposal_assist/` fixtures. Whole-suite testcontainers may flake — verify via grouped dir runs. `ruff check .` must pass (CI runs `ruff check .`, not format).

---

## File Structure

- Modify `src/call_manager/proposal_assist/tools/citations.py` — extract a `compute_citation_insert(...)` core (steps 1–12: DB verify + mint + idempotency + `insert_marker` + evidence-row build) from `_insert_citation_locked`, PLUS a shared post-commit `_write_citekey_cache(session, ref, citekey, *, owns_txn)` helper carrying today's step-14 logic verbatim. `_insert_citation_locked` becomes a thin git-first wrapper (mutex → read disk → core → `write_and_commit_files` → `_write_citekey_cache`) with **identical** behavior. (The core is not "pure" in the no-DB sense — it reads `Reference`/`ReferenceChunk` and resolves the citekey via the session; "pure" here means **no mutex, no git commit, no disk read/write, and no `ref.citekey` mutation**.)
- Modify `src/call_manager/proposal_assist/collab/checkpoint.py` — add `extra_files: dict[str, str] | None = None` to `checkpoint_artifact`, merged into the single `write_and_commit_files` call.
- Modify `src/call_manager/proposal_assist/collab/kernel.py` — add `kernel_accept_citation(...)`; delete the `CitationKernelDeferred` class and the `pc.kind != "prose_edit"` gate; `kernel_accept_prose` unchanged otherwise.
- Modify `src/call_manager/proposal_assist/proposals/kernel_accept.py` — dispatch `_body` by `pc.kind` (prose → `kernel_accept_prose`, citation_insert → `kernel_accept_citation`); drop `CitationKernelDeferred` from imports + `_CLEAN_ACCEPT_ERRORS`. The `_load`/`operate`/`room.lock`/broadcast/evict scaffold is reused verbatim.
- Modify `src/call_manager/proposal_assist/web/proposals.py` — delete the `CitationKernelDeferred → 409` branch + its import.
- Modify `src/call_manager/web/api/workspaces.py` — branch `post_lock_spec` / `post_unlock_spec` on `is_collab_enabled(…, artifact="spec")`: flag-on → checkpoint-the-durable-log-under-the-mutex then stamp the fresh checkpoint SHA (guarding empty-SHA→404, per Codex Medium) + empty-check the post-checkpoint disk file; flag-off → the current body verbatim. (No room freeze — see the Goal's Scope note.)
- Modify `src/call_manager/proposal_assist/proposals/accepting.py` — add a small non-evicting `UnsupportedKernelKind` error (Codex Low) so the dispatcher can reject `citation_remove`/`citation_update` cleanly.
- Tests: `tests/proposal_assist/collab/test_collab_kernel_citation.py` (new), `tests/proposal_assist/collab/test_collab_spec_lock.py` (new); small additions to `test_collab_kernel_accept_route.py` if the route dispatch needs a route-level citation case.
- Create `docs/superpowers/notes/2026-07-02-collab-slice2b2-notes.md`.

**Interface summary (locked across tasks):**

```
# tools/citations.py (added — no mutex, no git, no disk read, no ref.citekey write)
def compute_citation_insert(
    session, *, workspace_id: uuid.UUID, proposal_text: str, ledger: dict,
    ref_uuid: uuid.UUID, anchor: str, chunk_index: int, evidence_quote: str,
    stance: str, confidence: str | None,
) -> dict
    # success  -> {"new_proposal_text": str, "new_ledger": dict, "ev_id": str,
    #              "citekey": str, "ref": Reference}   # ref returned so the caller can
    #                                                   # write the citekey cache post-commit
    # idempotent no-op -> {"idempotent": True, "ev_id": str, "citekey": str}   # no ref/write
    # verify failure -> {"error": "<message>"}   (reference/chunk/quote/anchor)
    # NEVER returns _BUSY_ERROR or _GIT_COMMIT_FAILED_ERROR (no mutex/git here).

# tools/citations.py (added — shared post-commit cache write; today's step-14 logic verbatim)
def _write_citekey_cache(session, ref, citekey, *, owns_txn: bool) -> None
    # no-op if ref.citekey == citekey; else owns_txn=True -> commit; False -> begin_nested()+flush.
    # best-effort (git ledger canonical); never raises out.

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

**Files:** Modify `src/call_manager/proposal_assist/tools/citations.py`; parity via existing `tests/proposal_assist/proposals/test_accepting.py` + a new characterization test.

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
    """compute_citation_insert must NOT commit to git, write disk, take the mutex,
    or mutate ref.citekey. It DOES read the DB (ref/chunk/citekey resolution).
    NB: new_proposal_text is NOT byte-stable across calls — the marker embeds a fresh
    new_ev_id() each call. Assert on the STABLE facts (nothing written, citekey stable,
    ref.citekey untouched); to assert new_proposal_text identity, monkeypatch new_ev_id
    to a fixed value first."""
    from call_manager.proposal_assist.tools.citations import compute_citation_insert
    # (fixture builds a workspace + Reference (citekey=None) + ReferenceChunk with the quote;
    #  see _mk_ref_and_chunk helper below.)
    # head_before = current HEAD; call compute_citation_insert twice;
    # assert: HEAD unchanged, no new files on disk, both results' citekey equal,
    #         ref.citekey is STILL None (the core must not write the cache).
    ...
```

  **Implementer note:** build the ref/chunk fixture from the patterns already in `tests/proposal_assist/proposals/test_accepting.py` (search it for `ReferenceChunk(` and `Reference(`; note the real path is under `proposals/`). The characterization assertion is: after a git-first `_accept_citation_insert`, `_files_in_head(repo)` contains BOTH `proposal.md` and `citations.json`, and the marker is in `proposal.md`. Add this alongside (not replacing) the existing `test_accepting.py` citation cases, which remain the primary parity gate.

- [ ] **Step 2: Extract the core.** In `citations.py`, create `compute_citation_insert(...)` containing **steps 1–12** of the current `_insert_citation_locked` (read `citations.py:586-710`) with these exact changes:
  - It takes `proposal_text: str` and `ledger: dict` as **inputs** (does NOT call `_read_proposal_and_ledger`, does NOT read disk).
  - It does NOT call `acquire_commit_mutex` (step 0 stays in the caller) and does NOT call `write_and_commit_files` (step 13 stays in the caller).
  - **It does NOT run step 14** (the `ref.citekey` cache write) — that moves to `_write_citekey_cache`, called by the caller AFTER the commit/checkpoint (Codex High). The core must not mutate `ref.citekey`.
  - Step 5 idempotency (`_find_idempotent_row(ledger, …, proposal_text=proposal_text)`) → on hit, return `{"idempotent": True, "ev_id": existing["id"], "citekey": existing.get("citekey")}` (NO `commit_sha` — the caller stamps HEAD; NO `ref` — no cache write on idempotent, matching today's early return).
  - Steps 1,2,4 verify failures → return `{"error": "<same message string as today>"}` (reference not found / tombstoned / chunk not found / quote not found / ambiguous / anchor not found / ambiguous). Keep the exact message strings.
  - Success → return `{"new_proposal_text": new_proposal_text, "new_ledger": new_ledger, "ev_id": ev_id, "citekey": citekey, "ref": ref}` (the loaded, session-attached `Reference` — so the caller can run `_write_citekey_cache`).
  - `_resolve_citekey_for_ref(session, ledger=ledger, ref=ref, workspace_id=workspace_id)` stays as-is (it resolves/generates the citekey; it does NOT write `ref.citekey` — the write is step 14 → `_write_citekey_cache`).

- [ ] **Step 3: Add `_write_citekey_cache` — the shared post-commit cache write** (today's step-14 body, `citations.py:740-770`, verbatim; just parameterized):

```python
def _write_citekey_cache(session, ref, citekey, *, owns_txn: bool) -> None:
    """DB cache of the resolved citekey (AFTER git/checkpoint — git ledger is canonical).
    Best-effort: a failure logs and never propagates. owns_txn=True commits here (tool
    path); owns_txn=False uses a SAVEPOINT + flush so a racing IntegrityError cannot
    poison the caller's accept/kernel transaction (the caller commits once)."""
    if ref.citekey == citekey:
        return
    try:
        if owns_txn:
            ref.citekey = citekey
            session.commit()
        else:
            with session.begin_nested():
                ref.citekey = citekey
                session.flush()
    except Exception:  # noqa: BLE001 — cache write must never fail the caller
        if owns_txn:
            session.rollback()
        logger.warning(
            "insert_citation: references.citekey cache update failed for workspace "
            "%s ref %s citekey %s — reconciliation will backfill",
            ref.workspace_id, ref.id, citekey, exc_info=True,
        )
```

- [ ] **Step 4: Rewrite `_insert_citation_locked` as the thin git-first wrapper** (behavior identical to today):

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
    _write_citekey_cache(session, result["ref"], result["citekey"], owns_txn=owns_txn)  # step 14
    return {"ev_id": result["ev_id"], "citekey": result["citekey"], "commit_sha": sha}
```

  **Implementer note:** verify the return shape of the ORIGINAL `_insert_citation_locked` success path (`citations.py` after line 738 — read it) and reproduce it EXACTLY (keys `ev_id`, `citekey`, `commit_sha`, and any `idempotent`). The `owns_txn` param is now threaded into `_write_citekey_cache` (its ONLY remaining use). The relocated step-14 logic must match `citations.py:740-770` byte-for-byte in behavior (the `ref.citekey != citekey` guard, the commit-vs-savepoint branch, the rollback-only-when-owns_txn, the warning log) — `test_accept_citation_atomic_when_citekey_cache_branch_runs` (the commit-spy test) is the gate.

- [ ] **Step 5: Run the parity gate.** `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/proposals/test_accepting.py -q` (all green, unchanged — especially `test_accept_citation_atomic_when_citekey_cache_branch_runs`) + the new `test_compute_citation_insert_is_pure`. Expected: PASS.

- [ ] **Step 6: Commit** (`refactor(cite): extract compute_citation_insert core + shared _write_citekey_cache; _insert_citation_locked = thin git-first wrapper`).

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
  - **corrupt ledger → stale (Codex Medium):** write invalid JSON to `citations.json` on disk, then accept a citation pc → `status=stale`, room NOT evicted, git unchanged (parity with git-first, which maps the same ledger-parse error to terminal stale).
  - **citekey cache on accept (Codex High):** seed the `Reference` with `citekey=None`; after a successful kernel accept, `ref.citekey` equals the minted citekey AND the whole accept lands in ONE transaction (assert the accept path does not commit mid-way — mirror `test_accept_citation_atomic_when_citekey_cache_branch_runs`'s intent, adapted to the kernel's single-`_body`-commit).

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
    try:
        ledger = load_ledger(_cpath.read_text() if _cpath.exists() else None)
    except ValueError:
        # Corrupt citations.json. git-first (_read_proposal_and_ledger, citations.py:135-138)
        # turns this into {"error": ...} which _accept_citation_insert maps to terminal
        # stale — mirror that here (parity) rather than letting the ValueError escape to
        # the generic post-mutation evict arm (room eviction + 500). Codex Medium.
        return _stale()
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
    # Post-commit citekey cache (Codex High) — owns_txn=False: begin_nested()+flush so the
    # FOR UPDATE pc-row lock + advisory mutex stay held; the route's single session.commit
    # persists the checkpoint + status flip + this cache write in ONE transaction. Same
    # helper + semantics as the git-first accept-replay (owns_txn=False).
    _write_citekey_cache(session, result["ref"], result["citekey"], owns_txn=False)
    pc.status = "accepted"
    pc.resolved_by = approver.id
    pc.resolved_at = datetime.now(UTC)
    pc.resulting_commit_sha = cp.commit_sha
    session.flush()
    return KernelAcceptResult(pc=pc, update=update, committed=cp.committed, evict=True)
```

  **Implementer notes:**
  - Add imports: `compute_citation_insert` + `_write_citekey_cache` + `CITATIONS_FILENAME` from `tools/citations.py`; `dump_ledger` + `load_ledger` from `citations/ledger.py` (the canonical ledger module — `dump_ledger`/`load_ledger`/`empty_ledger`/`append_evidence` all live there; `load_ledger(None)` returns `empty_ledger()` = `{"version": LEDGER_VERSION, "evidence": []}`). The `Reference`/`ReferenceChunk` verify+mint helpers are all internal to `compute_citation_insert`.
  - Ledger read: `load_ledger((repo / CITATIONS_FILENAME).read_text() if that path exists else None)` — reuse the canonical loader; do NOT hand-roll the empty shape (its key is `"evidence"`, not `"citations"`). **Wrap it in `try/except ValueError: return _stale()`** so a corrupt `citations.json` mirrors git-first's terminal-stale mapping instead of escaping to the evict arm (Codex Medium).
  - Delete the `CitationKernelDeferred` class and the `if pc.kind != "prose_edit": raise CitationKernelDeferred(...)` gate from `kernel_accept_prose` in this task (the dispatcher in Task 4 guarantees kind).
  - `insert_marker` runs inside `compute_citation_insert` on the WHOLE room markdown → global anchor uniqueness (matches git-first). The reassembled `new_proposal_text` is the whole doc, so `apply_pm_json_to_fragment` gets the full doc (as prose does).

- [ ] **Step 3: Run** the Task-3 tests. Expected: PASS. (If `is_h2`/section indexing surprises arise, mirror `test_collab_kernel_accept.py`'s section-1 lesson — "## …" is section 1, preamble is 0.)

- [ ] **Step 4: Commit** (`feat(collab): kernel_accept_citation — apply citation to live room + co-commit ledger`).

---

### Task 4: Dispatch accept by kind; reject unsupported kinds cleanly; delete `CitationKernelDeferred` end-to-end

**Files:** Modify `src/call_manager/proposal_assist/proposals/kernel_accept.py`, `src/call_manager/proposal_assist/proposals/accepting.py` (add `UnsupportedKernelKind`), `src/call_manager/proposal_assist/web/proposals.py`; Test `tests/proposal_assist/collab/test_collab_kernel_accept_route.py` (add a route-level citation case + a flag-off citation parity case + an unsupported-kind case).

- [ ] **Step 1: Add `UnsupportedKernelKind`** in `accepting.py` (near `CitationAcceptBusy`):

```python
class UnsupportedKernelKind(Exception):
    """A ProposedChange.kind the kernel has no accept path for (citation_remove /
    citation_update — DB-allowed but no producers). Raised at dispatch BEFORE any room
    mutation, so it is non-evicting; the route maps it to 422."""
```

  DB-allowed kinds are `prose_edit`, `citation_insert`, `citation_remove`, `citation_update` (`models.py:1127`). Only `prose_edit` and `citation_insert` have producers (`propose_prose_edit` / `propose_citation_insert`); `kernel_accept_prose` handles `prose_edit` (both `draft` and `spec` target_artifacts). So the dispatch rejects `citation_remove` / `citation_update` (and any unknown kind) rather than letting them hit `kernel_accept_prose`'s `payload["new_text"]` (Codex Low).

- [ ] **Step 2: Write the failing route tests:**
  - **collab citation accept via HTTP:** enable collab, ref+chunk, pending `citation_insert` pc, `POST …/accept` → 200 `accepted`, marker in the checkpointed `proposal.md`, ledger row in HEAD.
  - **flag-off citation parity:** without `enable_collab`, `POST …/accept` of a citation pc still runs the git-first path (existing behavior).
  - **unsupported kind → 422 (Codex Low):** collab-enabled, insert a `citation_remove` pc directly (bypassing the absent producer), `POST …/accept` → **422**, status UNCHANGED (`pending`), room NOT evicted, git unchanged.
  - **(convergence, optional):** a connected WS client receives the citation update and converges (reuse the prose convergence pattern).

- [ ] **Step 3: Dispatch by kind** in `accept_via_kernel` (`kernel_accept.py`) — branch the sync `_body` on `pc.kind`; keep `_load`/`operate`/`room.lock`/broadcast/evict identical:

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
                        elif kind == "prose_edit":
                            res = kernel_accept_prose(
                                s, room_doc=room.doc, proposed_change_id=pc_id,
                                workspace_id=workspace_id, approver=approver,
                                workspaces_dir=workspaces_dir)
                        else:
                            # citation_remove / citation_update / unknown — no kernel path.
                            # Raised BEFORE any room mutation → non-evicting (Codex Low).
                            raise UnsupportedKernelKind(kind)
                        s.commit()
                        s.expunge(res.pc)
                        return res
```

  Update imports: import `kernel_accept_citation` and `UnsupportedKernelKind`; remove `CitationKernelDeferred` from the `kernel` import. In `_CLEAN_ACCEPT_ERRORS`: **remove** `CitationKernelDeferred`, **add** `UnsupportedKernelKind` (so it is treated as a clean, non-evicting error, not the generic post-mutation evict arm).

  **Implementer note:** a cheap extra `s.get(ProposedChange, pc_id)` to read `kind` before dispatch is acceptable (one indexed PK read); the real FOR-UPDATE load happens inside each kernel fn via `_load_scoped`. Alternatively thread `kind` through from the route's `_route` (which already loads the pc) — pick whichever keeps the diff smallest; if threading `kind`, extend `_route`'s return and `accept_via_kernel`'s signature. Prefer NOT expanding the public signature — the local `s.get` is simplest.

- [ ] **Step 4: Route exception mapping** (`web/proposals.py`) — remove the `except CitationKernelDeferred: → 409` block and the `CitationKernelDeferred` import; add `except UnsupportedKernelKind: → HTTPException(422, "cannot accept a <kind> proposal (unsupported)")`. Everything else in the route's exception mapping is unchanged.

- [ ] **Step 5: Run** the route tests + the existing `test_collab_kernel_accept_route.py` (the prose cases must stay green; the old "citation deferred → 409" case, if present, is now replaced by the citation-accepts case — update it, do not leave a dead 409 assertion).

- [ ] **Step 6: Commit** (`feat(collab): route citation_insert accept through the kernel; reject unsupported kinds; drop CitationKernelDeferred`).

---

### Task 5: Spec-lock OPERATION for a collab spec — checkpoint the durable log + record the fresh SHA (NOT a room freeze)

**Files:** Modify `src/call_manager/web/api/workspaces.py` (`post_lock_spec`, `post_unlock_spec`); Test `tests/proposal_assist/collab/test_collab_spec_lock.py` (new).

**Design:** `checkpoint_artifact` rebuilds the doc from the **durable `yjs_updates` log** (not the in-memory room), exactly as the Slice-2a explicit-checkpoint endpoint (`web/collab_checkpoint.py`) already does — so the lock path stays a **synchronous** endpoint, no `MANAGER.operate`/`room.lock` needed. After the checkpoint the on-disk spec file reflects the log, so the empty-check reads disk post-checkpoint.

> **This is NOT a room freeze (Codex High — narrowed invariant).** Setting `spec_locked_at` + stamping a checkpoint SHA does not stop a connected WS client from writing: `can_write` is bound at handshake (`ws_auth.resolve_access`) and `room.py:90` gates on that connect-time value, so a client connected pre-lock keeps writing; and checkpointing the durable log can lag an update applied to `room.doc` but whose `yjs_updates` row hasn't committed. **Both are unreachable in 2b-2** (flag OFF; tests seed the log via `store.append_update`, no live sockets). Live-writer blocking on lock — connect-time `can_write` refresh + `evict()`/disconnect of connected clients — is deferred to **Slice 3** (bundled with the already-listed "`evict()` doesn't disconnect"). Do NOT build room-lock/eviction into this task.

- [ ] **Step 1: Write the failing tests:**
  - **collab lock stamps the fresh checkpoint SHA:** enable collab for `spec`; seed an uncheckpointed live-room spec edit (persist a yjs_update via `store.append_update`); `POST …/spec/lock` (owner) → 200 `locked`; the `SpecLockEvent.artifact_commit_sha` equals the NEW checkpoint SHA (not the prior HEAD), and `proposal-spec.md` on disk now matches the room content.
  - **collab empty-check reads the room, not stale disk:** disk `proposal-spec.md` empty but the room has content → lock SUCCEEDS (post-checkpoint disk is non-empty). (And the converse: room empty → `409 cannot lock an empty spec`.)
  - **collab unlock** stamps a fresh checkpoint SHA too (symmetric).
  - **collab empty-SHA → 404 (Codex Medium):** collab-enabled spec whose repo has no committable HEAD (unborn branch — spec file present but never committed) → `POST …/spec/lock` → **404** (not a lock with `artifact_commit_sha=""`). Same for unlock.
  - **flag-off parity:** `tests/test_api_spec_lock.py` stays green unchanged (branch below must not alter the flag-off body).

- [ ] **Step 2: Implement** — branch each endpoint at the top on the collab flag; flag-off runs the current body verbatim. Flag-on (inside the same `session` + mutex):

```python
    is_collab = collab_state.is_collab_enabled(session, workspace_id=ws.id, artifact="spec")
    try:
        acquire_commit_mutex(session, ws.id)
        if is_collab:
            # Record the current state: checkpoint the durable log under the mutex
            # (re-enters the same xact lock as a no-op). NOT a room freeze — see the
            # Design scope note; connected writers are unaffected until Slice 3.
            cp = checkpoint_artifact(session, workspace_id=ws.id, artifact="spec",
                                     workspaces_dir=settings.workspaces_dir)
            frozen_sha = cp.commit_sha  # the just-written (or current) checkpoint SHA
            if not frozen_sha:
                # No committable HEAD (unborn / repo-less): checkpoint_artifact returns
                # commit_sha="" WITHOUT raising. Mirror the flag-off current_head ->
                # GitCommitError -> 404 mapping rather than stamping an empty SHA and
                # locking anyway (Codex Medium).
                session.rollback()
                raise HTTPException(status_code=404, detail="no proposal-spec.md yet")
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

  Apply the symmetric change to `post_unlock_spec` (checkpoint-then-clear, stamp `frozen_sha` on the unlock `SpecLockEvent`), **including the same `if not frozen_sha: rollback + 404` guard** (Codex Medium — unlock's flag-off path also maps `current_head`'s `GitCommitError` to 404 at `workspaces.py:556-559`). Read both current bodies (`workspaces.py:470-518` / `:521-560`) and preserve every flag-off detail (idempotent re-lock/unlock writes no audit row; ownership check `require_owner` runs first; the existing 404/429 mappings on the flag-off path).

  **Implementer notes:**
  - **New imports needed** in `workspaces.py`: `from ...collab.checkpoint import checkpoint_artifact` and `from ...collab import state as collab_state`. Already imported (verified): `current_head`, `acquire_commit_mutex`, `SpecLockEvent`, `SPEC_FILENAME`, `load_ledger`, `find_markers`, `BUSY_ERROR_MESSAGE` (used by the existing 429 handler), `require_owner`.
  - `require_owner` must run BEFORE the mutex/checkpoint (unchanged position at `workspaces.py:481`).
  - **Empty-SHA guard (Codex Medium):** `checkpoint_artifact` returns `commit_sha=""` (no raise) when there's no committable HEAD (`checkpoint.py:78-79`). The flag-off path instead calls `current_head`, which RAISES `GitCommitError` → 404. So on the collab path, `if not frozen_sha:` must `rollback` + raise the same 404; do NOT stamp `""` and lock. Real git write failures inside `checkpoint_artifact` still raise `GitCommitError` → caught → 500 (mirror `collab_checkpoint.py`); the two failure modes are distinct.
  - The empty-check for a collab spec reads disk **after** the checkpoint (post-checkpoint disk == log). This is correct because a committing checkpoint writes the markdown to disk, and a no-op checkpoint means disk already equals the log. Do NOT read `room.doc` directly (there may be no open room — the log is canonical).
  - `checkpoint_artifact` on a collab-enabled spec with only an unauthored hydration seed no-ops cleanly and returns the current HEAD as `commit_sha` — a lock then stamps that real HEAD (non-empty). Fine.
  - **No room freeze here** (see Design scope note) — this task must not import/call `MANAGER`, `operate`, `room.lock`, or any client-eviction path.

- [ ] **Step 3: Run** the new collab spec-lock tests + `tests/test_api_spec_lock.py` (unchanged green) + `tests/test_api_spec.py`. Expected: PASS.

- [ ] **Step 4: Commit** (`feat(collab): spec-lock checkpoints the durable log + records fresh SHA (not a room freeze)`).

---

### Task 6: Regression sweep + OpenAPI

- [ ] **Step 1: OpenAPI drift** — no route signature/shape changed (accept route body-only; spec-lock endpoints same path/response). Run `cd frontend && EMBEDDING_MODEL=bge-m3 pnpm gen:api && git diff --exit-code src/api/schema.gen.ts` → expect **no drift**; if drift, investigate before committing.

- [ ] **Step 2: Grouped-dir sweep** — `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/ tests/proposal_assist/proposals/ tests/test_api_spec_lock.py tests/test_api_spec.py -q` green; `uv run ruff check .` clean.

- [ ] **Step 3: Commit** (`test(collab): 2b-2 regression sweep`).

---

### Task 7: Notes

- [ ] **Step 1: Write** `docs/superpowers/notes/2026-07-02-collab-slice2b2-notes.md` — what shipped (citation accept through the kernel; the shared `compute_citation_insert` core + `_write_citekey_cache` helper + git-first thin wrapper; `checkpoint_artifact(extra_files)` co-commit; spec-lock **checkpoint-the-log + record fresh SHA**, NOT a room freeze; unsupported-kind dispatch guard; `CitationKernelDeferred` deleted); the invariants proven by tests (citation stale-vs-live, verify-fail→stale, anchor-moved→stale, idempotent no-op, corrupt-ledger→stale, citekey cache written on accept, ledger+marker atomic co-commit, lock stamps fresh SHA + empty-check reads log + empty-SHA→404, unsupported-kind→422, flag-off parity); **flag still OFF in prod**; deferrals — **spec-lock does NOT freeze a live room** (connect-time `can_write` refresh + `evict()`/disconnect of connected clients on lock is Slice-3, Codex High); the direct git-first citation tools (remove/update/rename) and the `citation_remove`/`citation_update` proposal kinds still bypass the room (Slice-3 write-kernel residual); the Slice-3 flag-flip prerequisites carried over from 2b-1 (staleness normalized-vs-raw-git-base SHA; whole-fragment replace loses concurrent edits; `evict()` doesn't disconnect clients); `save_section` git-canonical switchover + Tier-1/2 post-checkpoint re-fire still land at the flag flip.

- [ ] **Step 2: Commit** (`docs(collab): Slice 2b-2 notes`).

---

## Final verification (before whole-branch review)

```bash
EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/ tests/proposal_assist/proposals/ tests/test_api_spec_lock.py tests/test_api_spec.py -q
uv run ruff check .
cd frontend && EMBEDDING_MODEL=bge-m3 pnpm gen:api && git diff --exit-code src/api/schema.gen.ts
```

---

## Self-Review (author checklist — completed)

**1. Scope coverage:** citation_insert accept through the kernel (Tasks 3+4) ✓; ledger co-commit atomic (Task 2) ✓; git-first/kernel share verify+mint (Task 1) ✓; spec-lock op checkpoints the log + stamps fresh SHA (Task 5) ✓; `CitationKernelDeferred` deleted (Tasks 3+4) ✓; flag-off parity gated (Tasks 1+4+5) ✓. Deferred+documented: citation_remove/update (no producers), direct git-first citation tools, spec-lock room-freeze, Slice-3 flag-flip prereqs.

**2. Placeholder scan:** Task 1's core body says "steps 1–12 of the current function" rather than transcribing 150 lines — deliberate (the implementer moves real code, verifying the exact message strings/return shape against the source), matching how 2a/2b-1 handled refactors. All NEW functions (`kernel_accept_citation`, `_write_citekey_cache`, checkpoint extension, dispatch, spec-lock branch) carry complete code.

**3. Type consistency:** `compute_citation_insert(... proposal_text, ledger ...) -> dict` (returns `ref` on success) used identically in Task 1 (wrapper) and Task 3 (kernel); `_write_citekey_cache(session, ref, citekey, *, owns_txn)` used by both callers; `checkpoint_artifact(..., extra_files=…)` used in Tasks 2/3/5; `KernelAcceptResult(pc, update, committed, evict)` reused from 2b-1; `dump_ledger`/`CITATIONS_FILENAME` sourced from `tools/citations.py`.

**4. The load-bearing invariant flagged for the implementer:** the citation accept must be ONE transaction — `append_update(commit=False)` + `checkpoint_artifact(extra_files=ledger)` (caller-committed) + `_write_citekey_cache(owns_txn=False)` + status flip + one `session.commit()` in `_body`. The ledger row and the room marker MUST land in the same `write_and_commit_files` (via `extra_files`) so a git failure rolls back both and evicts the room (no marker-without-row corruption). Proven-in-spike: a minted citation survives the room reassemble→serialize→find_markers loop and a synced client converges byte-identically.

**5. Codex review fold-in (2026-07-02 — all 6 findings verified against source, all real):**
- **High — dropped `references.citekey` cache write** (`citations.py:740-770`, guarded by `test_accepting.py:385`): FIXED — extracted `_write_citekey_cache(owns_txn=…)`, called post-commit by BOTH git-first (Task 1) and kernel (Task 3, `owns_txn=False`); core returns `ref`; parity is the commit-spy test.
- **High — spec-lock doesn't freeze a live room** (`ws_auth.py:99` connect-time `can_write`; `room.py:102` write path; checkpoint lags uncommitted `room.doc`): RESOLVED by **narrowing the invariant** — 2b-2 spec-lock = "checkpoint the log + record fresh SHA," explicitly NOT a freeze; live-writer blocking (connect-time `can_write` refresh + `evict()`/disconnect) deferred to Slice 3. Unreachable in 2b-2 (flag OFF, no live sockets). Task 5 Design + Goal scope note + Notes deferral updated.
- **Medium — kernel didn't handle invalid `citations.json`** (`citations.py:135-138` git-first wraps it): FIXED — Task 3 wraps `load_ledger` in `try/except ValueError: return _stale()` (git-first parity), + test.
- **Medium — collab lock could stamp empty `artifact_commit_sha`** (`checkpoint.py:78-79` returns `""` w/o raising; flag-off maps `current_head` failure → 404): FIXED — Task 5 `if not frozen_sha: rollback + 404` on both lock and unlock, + test.
- **Low — dispatch routed `citation_remove`/`citation_update` into `kernel_accept_prose`** (`models.py:1127` allows them; `payload["new_text"]` KeyError): FIXED — Task 4 enumerates `citation_insert`/`prose_edit`, else raises non-evicting `UnsupportedKernelKind` → route 422, + test.
- **Low — wrong parity path + un-stable `new_proposal_text` assertion**: FIXED — path corrected to `tests/proposal_assist/proposals/test_accepting.py` everywhere (removed the redundant/bogus entry from the sweeps); purity test now asserts stable facts (no git/disk write, citekey stable, `ref.citekey` untouched), noting the marker's fresh `ev_id` (pin `new_ev_id` for byte-identity).
