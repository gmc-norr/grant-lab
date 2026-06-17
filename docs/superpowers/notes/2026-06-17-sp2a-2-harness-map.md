# SP2a-2 harness implementation map (workflow-generated, 2026-06-17)

> Source: 6-facet exploration workflow + synthesis. Controller decisions on the 10 open questions are in the SP2a-2 plan. NOTE: §6 (delete turn-freeze) is ALREADY DONE in SP2a-1 — exclude from SP2a-2.

I have everything. The `DraftingAgent` Protocol (`launch`/`send_turn`/`terminate` only — contract.py:131-139), `AgentHandle.state` is adapter-internal, the registry caches `_Entry` per-workspace, and `_insert_citation_locked(ctx, session, *, ref_uuid, anchor, chunk_index, evidence_quote, stance, confidence)` is the sync, session-owned replay target. I now have certainty on every contested point. Assembling the definitive map.

---

# SP2a-2 Implementation Map — propose_* tools + allowlist swap + citation accept path

Single source of truth for the TDD plan. All paths absolute under `/home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2a-2`. **Spec-resolved facts are marked [SPEC]** — these settle several "open questions" the facets flagged; the genuinely-open ones are in §8.

## Spec adjudications (settle facet disagreements up front)

Reading `docs/superpowers/specs/2026-06-17-cockpit-workspace-design.md` §4.1/§5/§6 directly resolves the contested points:

- **[SPEC §4.1, line 121-122]** The agent tools are exactly `propose_edit` + `propose_citation`. "Mutating tools are removed from the allowlist." Read-only surface "untouched."
- **[SPEC §5, line 156]** `kind` enum is `prose_edit | citation_insert | citation_remove | …` — the `…` means **citation_remove/citation_update propose-variants are explicitly out of SP2a-2 scope** (future). So the agent loses direct remove/update/rename *and* gets no propose-equivalent yet. This is intended, not an oversight (resolves tool-registry OQ5 → defer).
- **[SPEC §5, lines 169-170]** Accept replays exactly two functions: prose → `save_section(...)`, citation → `insert_citation_impl(reference_id, chunk_index, evidence_quote, anchor, …)`. Only `prose_edit` + `citation_insert` accept paths ship in SP2a-2.
- **[SPEC §5, line 154]** payload for citation = `reference_id, chunk_index, evidence_quote, anchor, stance, confidence`. Settles the citation-insert payload shape.
- **[SPEC §5, lines 176-178]** Citations verified at BOTH propose-time AND accept-time (authoritative, under mutex).
- **[SPEC §6, lines 183-194]** **Delete the workspace-wide turn-freeze** `_turn_in_flight` block on `save_section` (`editing/save.py:68-83,163-166`) entirely. Safety = commit mutex + base_section_sha staleness. **This is in-scope for SP2a-2** (the prose-edit facet missed it; it is a required edit).
- **[SPEC §5, line 160]** `triggering_turn_id` is "nullable — set only for `interactive_turn`." Provenance comes from the turn, not the model.
- **[SPEC, line 124]** `permission_mode="bypassPermissions"` is dropped (no specified replacement value — see §2/§8).
- **Build_bibliography / fetch_reference**: spec lists the read-only surface that is "untouched" and the mutating surface that is "removed" as prose+citation only. Neither bibliography nor fetch_reference is named as removed; both are corpus/render operations, not draft/spec section mutations. **Default: KEEP both on the allowlist** (resolves OQ E/4 with a default; confirm in §8).

---

## 1. Files to touch

| Path | Current responsibility | SP2a-2 change |
|---|---|---|
| `src/call_manager/proposal_assist/drafting/claude_adapter.py` | Builds `ClaudeAgentOptions`; holds `allowed_tools` (227-258), `permission_mode` default (145), `_CITATION_INSTRUCTION` (113-131). | Remove 6 mutating names from allowlist; add `propose_edit`+`propose_citation`; change `permission_mode` default; rewrite `_CITATION_INSTRUCTION` + the security-boundary comment (192-208). |
| `src/call_manager/proposal_assist/drafting/factory.py` | Builds `ToolContext` once per launch (68-77) and the `ClaudeAgentSDKAdapter` (79-83). | Retain the `ToolContext` so it's reachable for per-turn provenance stamping; optionally pass explicit `permission_mode`. |
| `src/call_manager/proposal_assist/drafting/contract.py` | `DraftingAgent` Protocol = `launch`/`send_turn`/`terminate` only (131-139); `AgentHandle.state` adapter-internal (19-27). | Add `set_turn_provenance(...)` (or equivalent) to the Protocol. |
| `src/call_manager/proposal_assist/drafting/registry.py` | `SubprocessRegistry` caches `_Entry` per workspace (27-34); `send_turn` pass-through (257-265). | Add a pass-through to reach the cached handle/adapter for provenance stamping. |
| `src/call_manager/proposal_assist/drafting/controller.py` | Orchestrates launch + send_turn. | Add `set_turn_provenance(workspace_id, …)` pass-through to registry/adapter. |
| `src/call_manager/proposal_assist/drafting/fake_adapter.py` | Test adapter; records launches/turns; `launch`/`send_turn`/`terminate` (19-73). | Add compatible no-op/recording `set_turn_provenance`. |
| `src/call_manager/proposal_assist/turns/processor.py` | `process_turn_once` (100-312): claims job (165 → `user_turn_id`,`submitted_by_user_id`), loads `user_turn.content` (168), reads `ws_session_id` (214). | Call provenance-stamp after message load (≈220), before `controller.send_turn` (231). |
| `src/call_manager/proposal_assist/tools/registry.py` | `ToolContext` dataclass (42-74); `_collect_tools` (142-752); returned tool list (720-752). | Add 4 per-turn provenance fields + `agent_view_sections` to `ToolContext`; remove 6 mutating `@tool`s + imports + list entries; add `propose_edit`+`propose_citation` `@tool`s + impl imports + list entries. |
| `src/call_manager/proposal_assist/tools/proposal.py` | `read_proposal_impl` (56-80, sets `agent_view_head`); `edit_proposal_impl`/`replace_proposal_impl` (83-177). | `read_proposal_impl` (or new section read) must also capture per-section read-time shas; add `propose_edit_impl`; delete/orphan the two mutating impls (no longer allowlisted — see §8). |
| `src/call_manager/proposal_assist/tools/citations.py` | `insert_citation_impl` (344-386) + sync `_insert_citation_locked` (389-579); verification helpers. | Add `propose_citation_impl` (propose-time verify, no git, no mutex); reuse `_insert_citation_locked` for accept replay. |
| `src/call_manager/proposal_assist/proposals/proposing.py` | `capture_base` (26-35), `propose_prose_edit` (38-70). | Add `propose_citation_insert(...)` service sibling (writes `kind="citation_insert"` row + payload + provenance). |
| `src/call_manager/proposal_assist/proposals/accepting.py` | `accept_proposed_change` (54-105); raises `NotImplementedError` for citation at **line 70**. | Branch on `kind`; add `citation_insert` replay of `_insert_citation_locked` as approver under mutex. |
| `src/call_manager/proposal_assist/editing/save.py` | `save_section` with `_turn_in_flight` workspace-wide block (68-83, 163-166). | **[SPEC §6]** Delete the `_turn_in_flight` block entirely. |
| `src/call_manager/proposal_assist/web/proposals.py` | Accept/reject endpoints (66-108). | Handle citation accept error/stale mapping; ensure ToolContext-free replay (replay uses request session — see §5). |

---

## 2. The allowlist + permission_mode swap

**Current `allowed_tools`** — `claude_adapter.py:227-258` (bare names, set only when `tools_factory` present). **Current `permission_mode`** — default `"bypassPermissions"` at `claude_adapter.py:145`, applied at `claude_adapter.py:187`. Factory does NOT pass `permission_mode` (factory.py:79-83), so the default governs.

**Becomes:**

Remove (6): `edit_proposal` (229), `replace_proposal` (230), `insert_citation` (246), `remove_citation` (247), `update_citation` (248), `rename_citekey` (249).
Add (2): `propose_edit`, `propose_citation` — bare names, MUST equal the `@tool(...)` names in `tools/registry.py`.
Keep (15): `read_proposal`, `get_artifact_history`, `read_artifact_at`, `diff_artifact`, `get_call_details`, `get_profile`, `list_references`, `search_references`, `navigate_reference`, `read_reference`, `read_reference_section`, `summarize_data`, `fetch_reference`, `list_citations`, `validate_citations`, `list_unsupported_claims`, `build_bibliography` (= 17 keeps actually; the two judgement-call tools `fetch_reference`+`build_bibliography` default-KEEP per spec adjudication). **New count = 23 − 6 + 2 = 19.**

`permission_mode`: change default at `claude_adapter.py:145` from `"bypassPermissions"` to `"default"`. (`"default"` pre-approves allowlisted tools in this headless context; `tools=[]`+`strict_mcp_config=True`+`setting_sources=[]` at 200-208 stay load-bearing and unchanged. `"dontAsk"` is a stricter alternative but its enforcement depends on the pinned CLI build — see §8.) Rewrite the security-boundary comment at `claude_adapter.py:192-208` (its "bypassPermissions defaulting" premise is now false).

**Rollout flag:** NOT warranted. Spec §4.1 mandates a clean cut to propose-only; the `'fake'` adapter (`DRAFTING_ADAPTER`, config.py:145-154) already gives a non-API test path. No existing propose-vs-commit flag exists; adding one contradicts the spec's "structural guarantee" framing. (Confirm in §8.)

Lockstep: the same 6 removals + 2 adds must hit the `_collect_tools` return list (`tools/registry.py:720-752`) and the `*_impl` imports (registry.py:158-162 region).

---

## 3. `propose_edit` tool

**Shape [SPEC §5 (A) per-section full-text]:** `propose_edit(section_index: int, new_text: str)` where `new_text` is the WHOLE new section (incl. its `##` heading). NOT `(old_string,new_string)`. New `@tool("propose_edit", …)` in `tools/registry.py` + `propose_edit_impl(ctx, *, section_index, new_text)` (in `tools/proposal.py` or a new `tools/proposing.py`).

**Context / provenance:** from the **`ToolContext` binding** (NOT model args). The tool reads `ctx.workspace_id`, `ctx.session_factory`, and the new per-turn fields `ctx.triggering_turn_id`, `ctx.triggering_prompt`, `ctx.created_by`, `ctx.origin_session_id` (added per §6). **Do not declare `triggering_turn_id` as a tool-schema arg** (the legacy `edit_proposal` arg at registry.py:191 was dead — the model was never told the turn id).

**Read-time base (anti-clobber) [proposing.py docstring 4-11]:**
- `base_commit_sha = ctx.agent_view_head` (the snapshot from the agent's last `read_proposal`). If `None` → return `{"error": _NOT_READ_ERROR}` (reuse proposal.py:51).
- `base_section_sha` = the section sha the agent saw at read time for this `section_index`. **NEW per-section tracking needed**: `read_proposal_impl` (or a new `read_section`/`list_sections` tool) must populate a new `ctx.agent_view_sections: dict[int,str]` (section_index → sha) at read time, HEAD-first like proposal.py:76. `propose_edit` reads `base_section_sha = ctx.agent_view_sections[section_index]`; missing → error.
- **MUST NOT call `capture_base(repo_dir, section_index)` at propose-time** — it recomputes live HEAD + re-splits the live doc (proposing.py:26-35), capturing any human edit landed since the read → silent clobber on accept. `capture_base` is correct ONLY when called at the moment of reading.

**Service:** opens `with ctx.session_factory() as session:`, calls `propose_prose_edit(session=…, workspace_id=ctx.workspace_id, section_index=…, new_text=…, base_commit_sha=…, base_section_sha=…, origin_kind="interactive_turn", origin_session_id=…, triggering_turn_id=…, triggering_prompt=…, created_by=…)` (proposing.py:38-70), then **`session.commit()`** (the service only `flush()`es — proposing.py:69; the caller owns the txn).

**Returns to agent:** `{"proposed_change_id": str(pc.id), "status": "pending"}` (NOT a commit_sha — the agent must understand it proposed, not committed). Errors → `{"error": …}` (→ MCP envelope via `_safe_tool`).

---

## 4. `propose_citation` tool

**Shape [SPEC §5 line 154]:** `propose_citation(reference_id, anchor, chunk_index: int, evidence_quote, stance="supports", confidence=None)` — mirrors `insert_citation`'s args (registry.py:586-604). NO provenance args (from ctx). New `@tool("propose_citation", …)` + `propose_citation_impl(ctx, *, reference_id, anchor, chunk_index, evidence_quote, stance, confidence)`.

**Payload stored on the row:**
```json
{"reference_id": "<uuid>", "anchor": "<unique substring>", "chunk_index": <int>,
 "evidence_quote": "<verbatim>", "stance": "supports", "confidence": <str|null>}
```
Store `stance` resolved to its `"supports"` default; do NOT store ev_id/citekey/offsets (minted authoritatively at accept).

**Propose-time verification [SPEC §5 line 176-178]** — run the propose-time subset of the insert checks so an unverifiable citation never becomes pending (reuse the `_insert_citation_locked` body 409-484, WITHOUT the mutex/write):
1. `_validate_stance_confidence` (citations.py:98-109).
2. Parse `reference_id` → UUID.
3. Load + workspace-scope `Reference`; reject missing/foreign/tombstoned (409-416).
4. Load `ReferenceChunk` by (reference_id, chunk_index, workspace_id); reject missing (419-429).
5. `find_quote_offsets(chunk.text, evidence_quote)` → reject `QuoteNotFound`/`QuoteAmbiguous` (438-443).
6. Anchor uniqueness: call `insert_marker(proposal_text, anchor, <placeholder>, <placeholder>)` inside try/except purely to trigger `AnchorError` on not-found/ambiguous; **discard the mutated text**.
MUST NOT `acquire_commit_mutex`, MUST NOT `write_and_commit_files`, MUST NOT touch `ctx.agent_view_head`.

**Base capture:** citation rows still need `section_index` + `base_section_sha` (NOT NULL, models.py:986-988). Derive `section_index` from which section the `anchor` offset falls in (locate anchor in the read-time proposal text, walk `split_sections` cumulative lengths); `base_commit_sha = ctx.agent_view_head`, `base_section_sha = ctx.agent_view_sections[section_index]`. **This anchor→section mapping is an OPEN design point — see §8.**

**Service:** add `propose_citation_insert(...)` to `proposing.py` (sibling to `propose_prose_edit`), writing `kind="citation_insert"`, the §4 payload, the base shas, and the same provenance kwargs. Tool opens its own session and `commit()`s.

**Returns:** `{"proposed_change_id": str(pc.id), "status": "pending"}` or `{"error": …}`.

---

## 5. Citation accept path

`accepting.py:70` currently: `raise NotImplementedError("citation_insert accept is SP2a-2")`. Replace with a `kind` branch.

**Replay mechanism — use the sync, session-owned `_insert_citation_locked` (citations.py:389), NOT the async `insert_citation_impl`.** Rationale: `insert_citation_impl` opens its OWN session (citations.py:376) and takes `ctx.write_lock` (asyncio) — replaying it would use a *different* session than the accept transaction, breaking single-transaction atomicity (the row status-flip + the git commit + the citekey cache must commit together). `_insert_citation_locked(ctx, session, *, ref_uuid, anchor, chunk_index, evidence_quote, stance, confidence)` is **sync**, takes the **caller's session**, and **itself acquires the commit mutex** (`acquire_commit_mutex(session, ctx.workspace_id)`, citations.py:404) — exactly the accept-time authoritative re-verification the spec wants, in the accept transaction.

**The call (in `accept_proposed_change`, `citation_insert` branch):**
- Build a `ToolContext` bound to this workspace: `workspace_id=pc.workspace_id`, `repo_dir = <workspaces_dir>/<workspace_id>/repo` (mirror save.py path helper), `session_factory` (carried but unused on this path — `_insert_citation_locked` uses the passed session), `settings`, `citation_service`. `agent_view_head=None` is fine (insert reads proposal/ledger off disk; only edit/replace use it as a precondition).
- `result = _insert_citation_locked(ctx, session, ref_uuid=UUID(pc.payload["reference_id"]), anchor=pc.payload["anchor"], chunk_index=pc.payload["chunk_index"], evidence_quote=pc.payload["evidence_quote"], stance=pc.payload.get("stance","supports"), confidence=pc.payload.get("confidence"))`.

**Approver identity:** the citation commit message is built inside `_insert_citation_locked` (`"cite: add {citekey} [{ev_id}]"`, citations.py:535-543) — it does NOT currently take an approver. If named-approver attribution is required on the *git commit* (parity with how prose `save_section` uses `user_id=approver.id`), `_insert_citation_locked`/`write_and_commit_files` needs an optional author/approver param. **OPEN — see §8.** At minimum the `proposed_change` row records `resolved_by=approver.id`.

**Result/error/staleness mapping:** `_insert_citation_locked` returns a dict, never raises for expected failures:
- success → `{"ev_id","citekey","commit_sha"}` (or `+"idempotent":True`). Set `pc.status="accepted"`, `pc.resolved_by=approver.id`, `pc.resolved_at=now`, `pc.resulting_commit_sha = result["commit_sha"]`, `session.flush()`. (Note: dict key `["commit_sha"]`, unlike prose's `result.commit_sha` attribute at accepting.py:103.)
- `{"error": …}` (anchor gone, quote no longer in chunk, reference tombstoned, chunk re-chunked, busy) → the citation analogue of prose's `StaleSectionError`→`"stale"`. **Mapping decision (OPEN §8):** map verification failures to `pc.status="stale"` (stamp resolver, mirror accepting.py:90-93) and `_BUSY_ERROR` to a retryable 409. Idempotent result → still mark accepted with the returned (current-HEAD) `commit_sha`.

**Web endpoint (web/proposals.py:66-108):** accept route is currently sync over the request session — good, it can pass that session straight into `_insert_citation_locked`. Add the new error→HTTP mapping (stale → the existing stale surface; busy → 409). Set `_app.state.settings.workspaces_dir` already wired in tests.

---

## 6. Provenance wiring (the single biggest net-new piece)

**Sources, materialized in `process_turn_once`:**
- `triggering_turn_id` = `user_turn_id` (processor.py:165, from the claim RETURNING).
- `created_by` = `submitted_by_user_id` (processor.py:165).
- `triggering_prompt` = text extracted from `user_turn.content` (processor.py:168) — the raw typed text block, NOT the reference-resolved blob (`triggering_prompt` is nullable Text, models.py:999). Extraction mirrors `_build_user_content` (submit.py:92-113): dict shape → `content["text"]`; block-list → first `{"type":"text"}` block.
- `origin_session_id` = `ws_session_id` (processor.py:214) when non-None (resumed session); `None` on fresh/post-compaction turns (the real id only arrives at `TurnDoneEvent`, claude_adapter.py:394). [SPEC §5 line 160: this field is primarily for background sessions; NULL on interactive is acceptable.]
- `origin_kind="interactive_turn"` — constant, set directly in the propose tools.

**The plumbing gap:** `ToolContext` is built once per LAUNCH (factory.py:68-77) and the MCP server closures capture that single `ctx` (registry.py:100-104); subprocesses are REUSED across turns (registry `_Entry` cache). The per-turn values live only as locals in `process_turn_once`. There is **no current handle from the processor to the live `ToolContext`** — `AgentHandle.state` is adapter-internal (contract.py:19-27), `DraftingAgent` Protocol has only launch/send_turn/terminate (contract.py:131-139).

**Required new mechanism (recommended D1):**
1. Add to `ToolContext` (registry.py:42-74): `triggering_turn_id: uuid.UUID|None=None`, `triggering_prompt: str|None=None`, `created_by: int|None=None`, `origin_session_id: str|None=None`, `agent_view_sections: dict[int,str]=field(default_factory=dict)`. (Defaults keep bare-ctx tests working.)
2. Retain the `ToolContext` at launch: in `factory.tools_factory` (factory.py:68-77), store the built `ctx` so it's reachable — e.g. stash on `AgentHandle.state["tool_context"]` in `ClaudeAgentSDKAdapter.launch` (claude_adapter.py:170-173), or have the factory close over a holder.
3. Add `set_turn_provenance(workspace_id, *, triggering_turn_id, triggering_prompt, created_by, origin_session_id)` to the `DraftingAgent` Protocol (contract.py:110-139), implemented by `ClaudeAgentSDKAdapter` (writes the retained ctx fields), `FakeAgentAdapter` (records/no-op), surfaced through `SubprocessRegistry` (reaches cached `_Entry` handle) and `controller`.
4. `process_turn_once` calls `controller.set_turn_provenance(...)` **after** message load (≈processor.py:220) and **before** `controller.send_turn` (processor.py:231), passing the four values. **Stamp unconditionally every turn** (the subprocess is reused — a missed stamp leaks the previous turn's provenance; `begin_in_flight_exclusive` (registry.py:161) guarantees one turn at a time, so no concurrent-ctx race, but sequential staleness is real — overwrite all four atomically).

This makes provenance **server-trusted** (closes the spoofable-arg hole) and matches the `propose_prose_edit`/`propose_citation_insert` kwargs 1:1.

---

## 7. Test plan

**Mirror these existing patterns:**

- **Service-level propose** — mirror `tests/proposal_assist/proposals/test_proposing.py`: real `pg_session_factory` + `make_workspace`, local `_init_repo`, assert the `ProposedChange` row (`status=="pending"`, `kind`, `payload`, `base_section_sha == split_sections(...)[i].sha`, `len(base_commit_sha)==40`). Add a `propose_citation_insert` test asserting `kind="citation_insert"` + the §4 payload. **NEW:** add a provenance-populated test (no existing test asserts non-default `triggering_turn_id`/`origin_session_id`/`triggering_prompt`/`created_by` — use `make_chat_turn` for a real turn id).
- **Impl-level tool** — mirror `tests/proposal_assist/tools/test_citations_insert.py`: `_seed_reference` (Reference `metadata_status="approved"` + `ReferenceChunk` `embedding=[0.0]*1024`), `_build_ctx`, await `propose_citation_impl(ctx, …)`; assert bad-quote-at-propose returns `{"error":…}` AND **no git commit** (`_commit_count` unchanged — propose writes only a DB row, unlike the old insert).
- **Closure/envelope** — mirror `tests/proposal_assist/test_tools_registry.py`: `propose_citation`'s closure must coerce non-integer `chunk_index` → `_mcp_error` envelope (copy `test_insert_citation_non_integer_chunk_index_returns_envelope`) and return an envelope (not raise) for a bad `reference_id` (copy `test_citation_tool_closure_returns_error_envelope_for_bad_ref`).
- **Accept** — mirror `tests/proposal_assist/proposals/test_accepting.py`: reuse `_seed_reference`. Add a positive `citation_insert` accept (marker + ledger row landed, `status=="accepted"`, `resolved_by`, `resulting_commit_sha`). Add a citation accept-time-verification-fail test (quote/anchor moved between propose and accept → asserts the §5 mapping decision).
- **Endpoint** — mirror `tests/proposal_assist/test_web_proposed_changes.py`: add an end-to-end "accept a citation_insert via POST applies marker to proposal.md + row to citations.json."
- **Adapter wiring** — mirror `tests/proposal_assist/test_tools_claude_adapter_wiring.py` (spy on `ClaudeAgentOptions`) + `test_drafting_claude_adapter.py` (direct `_build_options_kwargs`).

**Existing tests that MUST change (lockstep):**
1. `test_tools_registry.py:54-96` — `test_collect_tools_returns_twenty_named_tools`: the name-set assertion + `assert len(tools) == 23` → **19** with `propose_edit`/`propose_citation` swapped in for the 6 removed. The `insert_citation` closure tests (lines 180-223) move to `propose_citation` or are deleted.
2. `test_tools_claude_adapter_wiring.py:98-131` — `expected_tools` set assertion → new 19-name set.
3. `test_tools_claude_adapter_wiring.py:160-162` — `assert kwargs["permission_mode"] == "bypassPermissions"` → the new default (`"default"`).
4. `test_drafting_claude_adapter.py:466-489` — `test_build_options_appends_citation_instruction`: substring asserts on `"insert_citation"` → the rewritten verbs (`propose_citation`/`propose_edit`).
5. `test_accepting.py:249-274` — `test_accept_rejects_non_prose_kind` (currently asserts `NotImplementedError` for `citation_insert`): **rewrite into the positive citation-accept test**. (`test_accept_rejects_spec_target`, lines 221-247, stays — spec is SP2b.)
6. Any `save_section` turn-freeze test asserting `_turn_in_flight` behavior must be deleted/updated (spec §6 deletes the block).
7. `FakeAgentAdapter` callers (`test_turns_*`, `test_drafting_controller.py`, etc.) — verify they still construct after the Protocol gains `set_turn_provenance` (the fake must implement it).

**Infra note [MEMORY]:** the full suite is flaky (testcontainers PG drops under load); verify new/changed tests in isolation.

---

## 8. Open questions / risks (must be decided)

1. **`permission_mode` replacement value** — `"default"` (recommended, safe, pre-approves allowlisted tools headlessly) vs `"dontAsk"` (stricter "deny if not pre-approved", but enforcement depends on the pinned Claude Code CLI build — SDK pins 0.2.82 but CLI behavior for `"dontAsk"` is unverified). **Decide; default to `"default"`.** Risk: if a kept read-only tool is accidentally off the allowlist, a non-bypass mode blocks it with no UI to approve → the background turn hangs. Audit the 17-keep list against actual tool needs.

2. **Anchor→section_index mapping for `citation_insert`** — `ProposedChange.section_index`/`base_section_sha` are NOT NULL but a citation anchor is document-wide. Recommended: the section the anchor offset falls in (locate anchor in read-time proposal, walk `split_sections` cumulative offsets). Edge cases to specify: ambiguous anchor (already rejected at propose-time), anchor spanning a `##` boundary. This determines how citation rows participate in `refresh_staleness` (staleness.py is section-keyed).

3. **Named-approver attribution on the citation git commit** — prose `save_section` commits as `user_id=approver.id`; `_insert_citation_locked`'s commit message (citations.py:535) carries no approver. Decide whether accept-replayed citations must record the approver on the *git commit* (needs an optional author param on `_insert_citation_locked`/`write_and_commit_files`) or whether `proposed_change.resolved_by` suffices. Spec §5 says "replays the existing mutation function as the named approver" — implies commit attribution is wanted.

4. **Accept-time citation-verification-failure status** — map `_insert_citation_locked`'s `{"error":…}` (anchor/quote moved, chunk re-chunked, tombstoned) to `"stale"` (parallels prose `StaleSectionError`→stale) vs `"rejected"` vs a new surface. Recommended `"stale"` + stamp resolver. `_BUSY_ERROR` → retryable 409 (distinct from terminal stale).

5. **`build_bibliography` / `fetch_reference` on the allowlist** — both write (bibliography.md commit / corpus ingest) but neither is a draft/spec *section* mutation; spec names only prose+citation as removed. **Default KEEP both.** Confirm `build_bibliography`'s git commit isn't considered an in-scope "anything that commits to git" violation (the prose facet flagged this).

6. **`triggering_prompt` content & truncation** — raw typed text (recommended) vs resolved-with-references blob. Field is nullable Text; decide a truncation/size policy.

7. **`agent_view_sections` capture point** — does `read_proposal` (whole-file) populate the per-section sha dict, or is a dedicated `read_section`/`list_sections` read tool added (spec §7 implies section-at-a-time)? Either way, `propose_edit` must reject a section the agent never read (mirror `_NOT_READ_ERROR`). Whether to add a section-read tool to the allowlist is a sub-decision.

8. **Fate of `edit_proposal_impl`/`replace_proposal_impl`** — once de-allowlisted they're dead for the agent. Delete the impls + their `@tool`s + tests, or leave orphaned? Recommend delete (avoid dead-config a reviewer flags), but confirm nothing else imports them (the accept path uses `save.py`'s `save_section`, NOT these impls).

9. **No `propose_remove`/`propose_update`/`propose_rename`** — [SPEC §5 `…`] these are deferred, so the agent loses the ability to remove/relabel citations entirely in SP2a-2. Confirm this regression is acceptable for the release (it is per spec, but call it out to the user).

10. **Rollout flag** — recommended NONE (spec mandates clean cut). Confirm no kill-switch is wanted before hardcoding propose-only.