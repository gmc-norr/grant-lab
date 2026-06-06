# Citation Evidence Ledger & Mutation Tools (Slice 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax. Full two-stage review (spec + quality) on every task.

**Goal:** A git-tracked, content-frozen evidence ledger (`citations.json` v2) with inline `[@citekey]<!-- ev:<id> -->` markers in `proposal.md`, mutated only through atomic tools (`insert_citation`/`remove_citation`/`update_citation`/`rename_citekey`) that commit both files in one git commit; plus citekey assignment, reference tombstoning, and retrieval surfacing.

**Architecture:** Slice 2 of [the spec](../specs/2026-06-06-citation-provenance-evidence-ledger-design.md), building on Slice 1 (metadata + `CitationLLMService`). The ledger lives in the per-workspace git repo (`{workspaces_dir}/{wid}/repo/citations.json`) beside `proposal.md`. The **git ledger is canonical** for the citekey↔reference binding; `references.citekey` is a derived cache. Mutations hold the per-workspace advisory lock, write+stage both files, `git commit` once, then refresh the DB cache; reconciliation repairs a partial failure. Semantic audit (`validate_citations`, `list_unsupported_claims`) is Slice 3 — NOT here.

**Tech Stack:** Python 3.12, SQLAlchemy/Alembic, `subprocess` git (list-arg, no shell), MCP `@tool` (`claude_agent_sdk`), pytest with `pg_session_factory` + a git `_init_repo(tmp_path)` fixture (no LLM needed — Slice 2 tools are deterministic).

**Key existing pieces (from integration report):**
- Proposal git artifact + commit helper: `tools/proposal.py` (`_write_and_commit`, `PROPOSAL_FILENAME`, git env, scoped `git reset HEAD -- proposal.md` rollback). `read/edit/replace_proposal_impl`.
- Tool registration: `tools/registry.py` (`ToolContext{workspace_id,repo_dir,session_factory,settings,citation_service}`, `_collect_tools`, `@tool` + `_wrap`/`_mcp_ok`/`_mcp_error`). `allowed_tools` list in `drafting/claude_adapter.py`.
- `ReferenceChunk(reference_id, chunk_index, section_path, text, workspace_id)` — `(reference_id,chunk_index)` is the locator; `sha256(text)` is the chunk hash. `Reference.citekey`/`original_sha256`. Partial unique index `uq_references_workspace_citekey`.
- Lock: `drafting/locking.py` (`workspace_lock_session`, `acquire/release_workspace_lock`, `workspace_lock_key`).
- Migration head: `0037` → Slice 2 adds `0038`.
- Citekey helper already exists from Slice 1B: `references/csl.py::generate_citekey(csl, workspace_id, session)`. Reuse it (don't reimplement).

---

## File Structure
- `src/call_manager/proposal_assist/citations/ledger.py` — **new**: pure `citations.json` v2 model (load/save/create, `ev` ids, append/remove/update/find). No git/DB.
- `src/call_manager/proposal_assist/citations/markers.py` — **new**: pure marker helpers (insert marker after a unique anchor; remove/rewrite markers by `ev` id / citekey; parse markers).
- `src/call_manager/proposal_assist/citations/gitio.py` — **new**: `write_and_commit_files(repo_dir, files: dict[str, str|None], message) -> sha` — atomic multi-file commit with two-file rollback (generalize `proposal.py`'s pattern; `None` value = delete/omit file). Reused by all citation tools.
- `src/call_manager/proposal_assist/tools/citations.py` — **new**: the four tool impls (`insert/remove/update/rename_citekey`), each lock-guarded + atomic.
- `src/call_manager/proposal_assist/tools/references.py` — **modify**: surface `chunk_index` + `citekey` in `search_references`/`read_reference_section` output.
- `src/call_manager/proposal_assist/tools/registry.py` — **modify**: register the 4 tools (schemas) in `_collect_tools`.
- `src/call_manager/proposal_assist/drafting/claude_adapter.py` — **modify**: add the 4 names to `allowed_tools`.
- `src/call_manager/db/models.py` + `alembic/versions/0038_reference_tombstone.py` — **new/modify**: `references.tombstoned_at` + tombstone-aware filtering.
- `src/call_manager/proposal_assist/references/{ingest,...}` / web — **modify**: tombstone-vs-hard-delete policy where references are deletable (audit for an existing delete path; if none, expose the policy as a helper used by future UI).
- Tests under `tests/proposal_assist/citations/` and `tests/proposal_assist/tools/`.

---

### Task 1 — `citations.json` v2 ledger module (pure)
**Files:** new `citations/ledger.py`; new `tests/proposal_assist/citations/test_ledger.py`.
Pure functions over a dict (callers own file I/O). TDD.
- `EMPTY_LEDGER = {"version": 2, "evidence": []}`; `load_ledger(text|None) -> dict` (absent/empty → EMPTY; validate `version==2`); `dump_ledger(dict) -> str` (stable, indented JSON).
- `new_ev_id() -> "ev-<12hex>"` (uses `uuid.uuid4().hex[:12]`).
- `append_evidence(ledger, row) -> ledger'`; `remove_evidence(ledger, ev_id) -> (ledger', removed_bool)`; `update_evidence(ledger, ev_id, **fields) -> (ledger', updated_bool)`; `find_by_id`, `find_by_citekey`, `citekeys_in_use(ledger) -> set`.
- Evidence row shape (per spec §ledger v2): `{id, citekey, reference_id, locator:{chunk_index,section_path}, source_content_hash, chunk_content_hash, evidence_quote, evidence_quote_offsets, stance, confidence, claim_context:{text,hash}, citation_snapshot, created_at, updated_at}`. (Timestamps passed in by callers — do NOT call Date.now in the module; accept an `at` arg.)
- Tests: round-trip dump/load; version guard; append/remove/update/find; `citekeys_in_use`.
- Commit `feat(citations): citations.json v2 ledger module`.

### Task 2 — Retrieval surfacing
**Files:** modify `tools/references.py`; extend `tests/proposal_assist/tools/test_references_impls.py`.
- Add `ReferenceChunk.chunk_index` + `Reference.citekey` to `search_references_impl` SELECT; include `"chunk_index": row.chunk_index` and `"citekey": row.citekey` (when set) in each result dict.
- In `read_reference_section_impl`, include the `chunk_index` values for the returned chunks (e.g. `"chunk_indexes": [c.chunk_index ...]`).
- Tests: results include `chunk_index` (and `citekey` when the ref has one).
- Commit `feat(citations): surface chunk_index + citekey in reference retrieval`.

### Task 3 — Atomic multi-file git commit helper
**Files:** new `citations/gitio.py`; new `tests/proposal_assist/citations/test_gitio.py`.
- `write_and_commit_files(repo_dir, files: dict[str,str|None], message, env=None) -> str` (returns sha). For each path: capture pre-write content (or `None` if absent); write (or, if value is `None`, leave/remove). `git add <paths>` (list-arg). `git commit -q -m <message>`. `git rev-parse HEAD`. On `CalledProcessError`: restore every file to its captured pre-write state (delete any that were newly created), `git reset HEAD -- <paths>` (scoped; never `--hard`), re-raise. Mirror `proposal.py`'s env + no-shell pattern. (Empty-commit guard: if nothing changed, decide — likely return current HEAD sha without committing.)
- Tests (use the `_init_repo` fixture): commits two files in ONE commit (one new sha, both staged); commit failure restores BOTH files (proposal restored, citations.json removed if it was new) and leaves unrelated tracked files untouched; sha returned matches HEAD.
- Commit `feat(citations): atomic multi-file git commit helper`.

### Task 4 — marker helpers (pure)
**Files:** new `citations/markers.py`; new `tests/proposal_assist/citations/test_markers.py`.
- `MARKER_RE` matching `[@<citekey>]<!-- ev:<id> -->`. `insert_marker(text, anchor, citekey, ev_id) -> text'`: anchor must occur EXACTLY once (raise `AnchorError` on 0 or >1); insert `[@citekey]<!-- ev:ev_id -->` immediately after the anchor occurrence. `remove_marker(text, ev_id) -> (text', removed_bool)`. `rename_marker_citekey(text, old, new) -> (text', count)` (rewrite the `[@old]` part of every marker whose citekey==old, leaving its `ev` id). `find_markers(text) -> list[{citekey, ev_id, span}]`.
- Tests: unique-anchor insert; 0/>1 anchor → AnchorError; remove by ev id; rename rewrites only matching citekeys; multiple same-key markers handled per-occurrence.
- Commit `feat(citations): proposal marker insert/remove/rename helpers`.

### Task 5 — `insert_citation` tool (keystone)
**Files:** new `tools/citations.py`; new `tests/proposal_assist/tools/test_citations_insert.py`.
`insert_citation_impl(ctx, *, reference_id, anchor, chunk_index, evidence_quote, stance="supports", confidence=None) -> dict`. Order (all under `workspace_lock_session(ctx.session_factory, ctx.workspace_id)`):
1. Load `proposal.md` (repo_dir) + `citations.json` (or empty).
2. Validate anchor uniqueness via `markers.insert_marker` dry-run (or locate first) — reject (`{"error":...}`) on 0/>1.
3. Load the `ReferenceChunk` (by reference_id+chunk_index, workspace-scoped); reject if missing or the reference is tombstoned. Quote-verify: `evidence_quote` must be a normalized substring of `chunk.text` exactly once → record `evidence_quote_offsets`; reject if absent/ambiguous.
4. Resolve/assign citekey: reuse `ref.citekey` if set; else `generate_citekey` from `ref.csl_json or ref.csl_json_extracted or {}` and stage it for the DB cache.
5. Build the evidence row: `ev_id=new_ev_id()`, `chunk_content_hash=sha256(chunk.text)`, `source_content_hash=ref.original_sha256`, `claim_context={text:<anchor sentence>, hash:sha256(...)}`, `citation_snapshot=<short rendered citation>`, timestamps.
6. New `proposal.md` = `insert_marker(text, anchor, citekey, ev_id)`; new `citations.json` = dump(append_evidence(...)).
7. `write_and_commit_files(repo_dir, {proposal.md, citations.json}, msg)` → sha. THEN update `references.citekey` cache in DB (best-effort; git canonical). Return `{ev_id, citekey, commit_sha}`. Idempotent: identical `(reference_id, anchor, chunk_index, evidence_quote)` returns existing ev id (check before writing).
Tests: happy path (marker in proposal.md, row in citations.json, both in one commit); anchor 0/>1 → error, no write; quote absent/ambiguous → error; tombstoned ref → error; idempotent re-insert; citekey reuse on second cite of same ref.
- Commit `feat(citations): insert_citation tool (atomic marker + ledger)`.

### Task 6 — `remove_citation` + `update_citation`
**Files:** modify `tools/citations.py`; new test file(s).
- `remove_citation_impl(ctx, *, evidence_id)`: lock; remove marker by ev id (`markers.remove_marker`) + drop ledger row; atomic commit. Error if ev id not found.
- `update_citation_impl(ctx, *, evidence_id, chunk_index=None, stance=None, confidence=None, evidence_quote=None, reaccept_claim=False)`: lock; update the ledger row (re-pin chunk/quote with the same quote-verification as insert; refresh `chunk_content_hash`/offsets); `reaccept_claim=True` re-snapshots `claim_context` from the live sentence around the marker (clears `claim_drift`); marker text unchanged (citekey unchanged). Atomic commit.
Tests: remove round-trip (marker + row gone, one commit); update stance/confidence; update chunk+quote re-verifies (bad quote → error, no commit); reaccept_claim refreshes claim_context.
- Commit `feat(citations): remove/update citation tools`.

### Task 7 — `rename_citekey`
**Files:** modify `tools/citations.py`; new test.
`rename_citekey_impl(ctx, *, old_citekey, new_citekey)`: lock; reject if `new_citekey` already used (in ledger OR `references.citekey` in this workspace); rewrite all `[@old]` markers (`markers.rename_marker_citekey`) + update all matching ledger rows' citekey; atomic commit; update `references.citekey` cache. Error if `old_citekey` not present.
Tests: all markers + rows rewritten in one commit; collision rejection; DB cache updated; unknown old → error.
- Commit `feat(citations): rename_citekey tool`.

### Task 8 — reference tombstoning (migration 0038 + policy + filtering)
**Files:** `db/models.py` + `alembic/versions/0038_reference_tombstone.py`; `tools/references.py` (filter); a delete-policy helper; tests + migration test.
- Migration 0038 (down_revision 0037): add `references.tombstoned_at timestamptz NULL`. Model column + (optional) partial index `WHERE tombstoned_at IS NULL` mirroring `Workspace.soft_deleted_at`.
- Policy helper `delete_or_tombstone_reference(session, repo_dir, reference_id, workspace_id)`: if `citations.json` has any evidence row for this reference_id → set `tombstoned_at=now()` (retain bytes/chunks/metadata); else hard-delete (row + chunks + bytes via existing storage delete). (Audit for an existing reference-delete route; if none exists, ship the helper + unit test and note UI wiring deferred.)
- Filter tombstoned refs from `list_references_impl` + `search_references_impl` (and they must not be insert_citation-able — Task 5 already rejects tombstoned).
- Tests: model+migration round-trip (finally→head); uncited ref hard-deletes; cited-once ref tombstones (chunks/metadata retained); tombstoned ref absent from list/search; insert_citation on tombstoned → error.
- Commit `feat(citations): reference tombstoning (migration 0038 + delete policy + filtering)`.

### Task 9 — register tools + allowed_tools wiring
**Files:** `tools/registry.py` (+ `_collect_tools` schemas), `drafting/claude_adapter.py` (allowed_tools); extend `tests/proposal_assist/test_tools_claude_adapter_wiring.py`.
- Add `insert_citation`/`remove_citation`/`update_citation`/`rename_citekey` `@tool`-wrapped closures (JSON schemas matching the impl kwargs) to `_collect_tools`; add the 4 bare names to `allowed_tools`.
- Tests: the 4 names appear in `options_kwargs["allowed_tools"]` and in the MCP server's tool set; a smoke call through the registered tool returns the MCP envelope.
- Commit `feat(citations): register citation mutation tools + allowlist`.

### Task 10 — full suite + lint gate
- `uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/`; `uv run pytest -q`. Fix any cross-file regressions. Commit fixups.

---

## Keystone risk (call out to implementers)
**Atomic two-file commit + anchor insertion + rollback.** Validate the anchor and quote and build BOTH new file contents in memory BEFORE touching disk; nothing writes until both are ready. Order: validate anchor → quote-verify → assign citekey (do NOT commit DB yet) → build new proposal.md + citations.json strings → `write_and_commit_files` (one commit) → THEN commit the `references.citekey` DB cache. Any failure before the git commit: no DB commit, no partial files (gitio restores). The git repo is the source of truth; the DB citekey is a rebuildable cache. Hold `workspace_lock_session` across the whole op.

## Self-review checklist (run after writing tasks)
- Every tool is lock-guarded and atomic (one commit per mutation). ✓
- Markers carry per-occurrence `ev` ids; removing one of several same-key markers orphans exactly one row. ✓
- Quote verification on insert/update; tombstoned refs rejected. ✓
- Retrieval surfaces the locator (chunk_index) + citekey so the agent can call insert_citation. ✓
- Migration 0038 reversible + round-trip test restores head. ✓
- Slice 3 (validate/list/audit) explicitly NOT in scope here.
