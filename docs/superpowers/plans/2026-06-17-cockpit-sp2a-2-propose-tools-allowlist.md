# Cockpit SP2a-2 — Propose Tools + Allowlist Swap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make "the AI cannot mutate the workspace git repo directly" a *structural* guarantee: give the drafting agent `propose_edit` + `propose_citation` tools that write `proposed_change` rows, remove the 6 mutating MCP tools from the SDK allowlist, drop `permission_mode="bypassPermissions"`, and extend `accept_proposed_change` to replay `_insert_citation_locked` for `kind="citation_insert"`.

**Architecture:** SP2a-1 (merged) built the propose→accept substrate: a `proposed_change` table + `propose_prose_edit` / `accept_proposed_change` services + member-gated endpoints. SP2a-2 wires mechanism (B): the agent's tools become *propose-only* (a DB row, never a git commit), provenance (turn id, prompt, actor, session) is stamped *server-side* onto a retained per-launch `ToolContext` (never trusting model args), and accept replays the existing mutation function as the named approver under the commit mutex. Citations verify at both propose-time and accept-time.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, PostgreSQL 16 (pgvector testcontainer in tests), Claude Agent SDK 0.2.82 (`ClaudeSDKClient` / `ClaudeAgentOptions`), in-process `create_sdk_mcp_server` MCP tools, `uv` + `ruff` + `pytest`.

---

## Scope / Out of scope

**In scope (the 7 tasks):**
1. Provenance substrate — `ToolContext` fields + `set_turn_provenance` threaded through contract → claude_adapter → fake_adapter → registry → controller → processor.
2. `read_proposal` captures `agent_view_sections` (section_index → sha) at read time.
3. `propose_citation_insert` service in `proposals/proposing.py`.
4. `propose_edit` tool (impl + `@tool`) — read-time base, anti-clobber.
5. `propose_citation` tool (impl + `@tool`) — propose-time verify + anchor→section mapping.
6. Citation accept path — replay `_insert_citation_locked` as approver; map success/verify-fail/busy/idempotent.
7. Allowlist swap + `permission_mode="default"` + delete the two mutating proposal `@tool`s/impls; lockstep test updates + read-only-allowlist audit.

**Out of scope:**
- **SP2b (spec target).** `accept_proposed_change` keeps raising `NotImplementedError("spec target is SP2b")` for `target_artifact="spec"`. The existing `test_accept_rejects_spec_target` test stays green.
- **`propose_remove` / `propose_update` / `propose_rename`** (spec §5 `…` — deferred). **Consequence to call out (decision 9):** with the mutating citation tools de-allowlisted and no propose-equivalent for remove/update/rename, the agent *loses the ability to remove or relabel citations entirely in SP2a-2.* This is intended per spec but is a behavioral regression for the agent; the human still has the direct citation tools via the web/analysis path.
- **The SPA / Lexical editor / select-to-cite UI** (SP2a build items 3–6, later plans). SP2a-2 is backend-only.
- **Named-approver attribution on the citation *git commit*.** See Task 6 follow-up note — `_insert_citation_locked` / `write_and_commit_files` carry no author param today, so we record the approver on the `proposed_change.resolved_by` row only and leave a `# follow-up:` note. We do NOT deepen `write_and_commit_files`.

**Flagged consequence (decision 5):** `build_bibliography` and `fetch_reference` STAY on the allowlist. `build_bibliography` *does* commit `bibliography.md` to git, but the spec names only the prose+citation mutating surface as "removed," and a bibliography render is not a draft/spec *section* mutation. So a single git-writing tool (`build_bibliography`) remains agent-reachable by design — note it; do not remove it.

---

## File Structure

| Path | SP2a-2 change |
|---|---|
| `src/call_manager/proposal_assist/tools/registry.py` | Add 4 provenance fields + `agent_view_sections` to `ToolContext`; remove 6 mutating `@tool`s + their impl imports + list entries; add `propose_edit` + `propose_citation` `@tool`s + impl imports + list entries. |
| `src/call_manager/proposal_assist/tools/proposal.py` | `read_proposal_impl` populates `ctx.agent_view_sections`; add `propose_edit_impl`; **delete** `edit_proposal_impl` + `replace_proposal_impl` (+ their now-orphaned helpers `_commit_message` / `_write_and_commit`). |
| `src/call_manager/proposal_assist/tools/citations.py` | Add `propose_citation_impl` (propose-time verify, no git, no mutex). `_insert_citation_locked` is reused for accept replay — gains an `owns_txn: bool = True` param (Task 6) so the accept replay can flush (not commit) its citekey-cache write, keeping the accept single-transaction-atomic. The async `*_impl` citation wrappers STAY (broad test-fixture use — see divergence note). |
| `src/call_manager/proposal_assist/proposals/proposing.py` | Add `propose_citation_insert(...)` sibling to `propose_prose_edit`. |
| `src/call_manager/proposal_assist/proposals/accepting.py` | Replace the `citation_insert` `NotImplementedError` with a `kind` branch replaying `_insert_citation_locked`. |
| `src/call_manager/proposal_assist/drafting/contract.py` | Add `set_turn_provenance(...)` to the `DraftingAgent` Protocol. |
| `src/call_manager/proposal_assist/drafting/claude_adapter.py` | Retain the per-workspace `ToolContext`; implement `set_turn_provenance`; remove 6 mutating allowlist names + add 2; change `permission_mode` default to `"default"`; rewrite `_CITATION_INSTRUCTION` + the security-boundary comment. |
| `src/call_manager/proposal_assist/drafting/factory.py` | `tools_factory` stashes the built `ctx` into a sink the adapter retains. |
| `src/call_manager/proposal_assist/drafting/fake_adapter.py` | Add recording `set_turn_provenance`. |
| `src/call_manager/proposal_assist/drafting/registry.py` | Pass-through `set_turn_provenance` to the cached handle's adapter. |
| `src/call_manager/proposal_assist/drafting/controller.py` | Pass-through `set_turn_provenance` to the registry. |
| `src/call_manager/proposal_assist/turns/processor.py` | Ensure launch (idempotent), then stamp provenance unconditionally each turn (after message load, before `controller.send_turn`). Launch-first because the retained `ToolContext` sink is populated only during launch — stamping a cold/unlaunched workspace would silently drop provenance. |
| `src/call_manager/proposal_assist/web/proposals.py` | Map the citation accept errors (busy → 409). |

> NOTE: `editing/save.py`'s workspace-wide turn-freeze deletion (map §6) is **already done in SP2a-1** — it is NOT a task here. The `_turn_in_flight` helper remains for the soft checks in `analysis/insert.py` + `web/sections.py`; leave it.

### Divergence from the harness map (verified against the real code — surface to the controller)

1. **Citation `*_impl` async wrappers are NOT deleted.** The map said "remove the 6 mutating `@tool`s + their `*_impl` imports." For the two *proposal* impls (`edit_proposal_impl`/`replace_proposal_impl`) deletion is safe (only `registry.py` + the two now-deleted test files imported them — decision 8). But `insert_citation_impl` / `remove_citation_impl` / `update_citation_impl` / `rename_citekey_impl` are imported by **many** test files as *seeding fixtures* (`test_citations_insert.py`, `test_citations_remove_update.py`, `test_citations_rename.py`, `test_citations_list.py`, `test_validate_citations.py`, `test_audit_deterministic.py`). (`test_tools_head_precondition.py` also imported `insert_citation_impl` to seed, but Step 7.8 DELETES that file, so it no longer counts toward keeping the impls — the conclusion holds via the other six files.) So Task 7 removes their `@tool` wrappers + imports + list entries **from `registry.py` only**; the impl functions stay in `citations.py`. (`_insert_citation_locked` is also the accept-replay target, so it must stay regardless.)
2. **`accept_proposed_change` replays `_insert_citation_locked` directly on the caller's session** — confirmed sync, takes the caller's `session`, and itself calls `acquire_commit_mutex(session, ctx.workspace_id)` (citations.py:404). It returns a dict and never raises for expected failures. **Single-transaction atomicity is NOT automatic:** `_insert_citation_locked`'s citekey-cache branch (`if ref.citekey != citekey:`, citations.py:~563) does `session.commit()`, which on the replay would prematurely release the SP2a-1 `_load_scoped` FOR UPDATE pc-row lock + the `pg_advisory_xact_lock` mutex while `pc.status` is still "pending" — splitting the git/ledger write from the status flip. Task 6 adds an `owns_txn: bool = True` param: the accept replay passes `owns_txn=False` so that branch FLUSHES (not commits) and the endpoint's single `session.commit()` persists the git/ledger write + status flip + citekey update in ONE transaction. The default `owns_txn=True` leaves the async `insert_citation_impl` path unchanged.
3. **`_insert_citation_locked` / `write_and_commit_files` record NO user/approver** on the git commit (signature `write_and_commit_files(repo_dir, files, message)`; commit message `f"cite: add {citekey} [{ev_id}]"`). Decision 3's "iff" resolves to: do NOT deepen — record `resolved_by` on the row + leave a `# follow-up:` note.

---

## Conventions for every task

- **TDD:** write the failing test → run it (see the exact command + expected FAIL) → minimal impl → run to PASS → commit.
- **Testcontainer flakiness [MEMORY]:** the full `uv run pytest` suite drops the testcontainers Postgres under load. Run new/changed tests **in isolation** (the exact `::test` commands below). If a test fails with `connection refused` / `OperationalError` unrelated to your assertion, **re-run that one test once** before treating it as a real failure.
- **Lint gate before every commit:**
  ```bash
  uv run ruff check --fix src/call_manager/proposal_assist tests/proposal_assist \
    && uv run ruff format src/call_manager/proposal_assist tests/proposal_assist \
    && uv run ruff check .
  ```
- All `pytest` invocations are `uv run pytest …`.

---

## Task 1: Provenance substrate (foundation)

This is the largest net-new piece: it threads server-trusted provenance from `process_turn_once` onto the retained per-launch `ToolContext` so the propose tools (Tasks 4/5) read it from `ctx`, never from spoofable model args.

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/registry.py:42-74` (ToolContext fields)
- Modify: `src/call_manager/proposal_assist/drafting/contract.py:109-139` (Protocol)
- Modify: `src/call_manager/proposal_assist/drafting/claude_adapter.py:140-173,220-222` (retain ctx + impl)
- Modify: `src/call_manager/proposal_assist/drafting/factory.py:68-83` (ctx sink)
- Modify: `src/call_manager/proposal_assist/drafting/fake_adapter.py:31-51` (record)
- Modify: `src/call_manager/proposal_assist/drafting/registry.py:257-265` (pass-through)
- Modify: `src/call_manager/proposal_assist/drafting/controller.py:215-216` (pass-through)
- Modify: `src/call_manager/proposal_assist/turns/processor.py:205-231` (ensure launch, then stamp)
- Modify: `tests/proposal_assist/test_drafting_contract.py:87-100` (`_Dummy` gains `set_turn_provenance` so the runtime_checkable Protocol assertion still holds)
- Test: `tests/proposal_assist/test_drafting_fake_adapter.py`, `tests/proposal_assist/test_drafting_claude_adapter.py` (real-adapter sink cold-launch), `tests/proposal_assist/test_turns_processor.py`, `tests/proposal_assist/test_tools_registry.py`, `tests/proposal_assist/test_drafting_contract.py`

- [ ] **Step 1.1: Write the failing ToolContext-defaults test**

Append to `tests/proposal_assist/test_tools_registry.py`:

```python
def test_tool_context_has_provenance_defaults():
    """The new per-turn provenance fields + agent_view_sections default to
    None / empty so existing bare-ctx tests keep constructing ToolContext
    with only the four required args."""
    ctx = ToolContext(
        workspace_id=uuid.uuid4(),
        repo_dir=Path("/tmp/x"),
        session_factory=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )
    assert ctx.triggering_turn_id is None
    assert ctx.triggering_prompt is None
    assert ctx.created_by is None
    assert ctx.origin_session_id is None
    assert ctx.agent_view_sections == {}
    assert ctx.agent_view_proposal is None
```

- [ ] **Step 1.2: Run it — expect FAIL**

Run: `uv run pytest tests/proposal_assist/test_tools_registry.py::test_tool_context_has_provenance_defaults -v`
Expected: FAIL with `AttributeError: 'ToolContext' object has no attribute 'triggering_turn_id'`.

- [ ] **Step 1.3: Add the fields to ToolContext**

In `src/call_manager/proposal_assist/tools/registry.py`, after the `reference_ingest_limiter` field (line 74), inside the `ToolContext` dataclass body, add:

```python
    # SP2a-2 §6: per-turn provenance, stamped server-side by process_turn_once
    # via DraftingAgent.set_turn_provenance onto the retained ToolContext before
    # each send_turn. NEVER set from model tool args (those are spoofable). The
    # propose_* tools copy these onto the proposed_change row. Stamped
    # UNCONDITIONALLY every turn because the subprocess (and this ctx) is reused
    # across turns — a missed stamp would leak the prior turn's provenance.
    triggering_turn_id: uuid.UUID | None = field(default=None)
    triggering_prompt: str | None = field(default=None)
    created_by: int | None = field(default=None)
    origin_session_id: str | None = field(default=None)

    # SP2a-2 §3 (Task 2): section_index -> section sha as the agent saw them at
    # read time (populated by read_proposal_impl, HEAD-first). propose_edit reads
    # base_section_sha from here; an unread section is rejected (_NOT_READ_ERROR).
    agent_view_sections: dict[int, str] = field(default_factory=dict)

    # SP2a-2 §3 (Task 2): the full proposal text the agent saw at read time
    # (populated by read_proposal_impl alongside agent_view_sections). propose_
    # citation (Task 5) walks split_sections(agent_view_proposal) to map an
    # anchor offset -> section_index from the SAME read-time snapshot it took the
    # section shas from, so the (section_index, base_section_sha) pair stays
    # internally consistent even if a human added/removed a `##` section between
    # the read and the propose. None until the agent has read once.
    agent_view_proposal: str | None = field(default=None)
```

- [ ] **Step 1.4: Run it — expect PASS**

Run: `uv run pytest tests/proposal_assist/test_tools_registry.py::test_tool_context_has_provenance_defaults -v`
Expected: PASS. Also re-run the existing `test_tool_context_is_a_dataclass` to confirm no regression: `uv run pytest tests/proposal_assist/test_tools_registry.py::test_tool_context_is_a_dataclass -v` → PASS.

- [ ] **Step 1.5: Write the failing fake-adapter provenance test**

Append to `tests/proposal_assist/test_drafting_fake_adapter.py`:

```python
async def test_fake_adapter_records_set_turn_provenance():
    """set_turn_provenance is part of the DraftingAgent Protocol; the Fake
    records each call so the processor-stamp test can assert what was passed."""
    import uuid as _uuid

    fake = FakeAgentAdapter()
    wsid = _uuid.uuid4()
    ttid = _uuid.uuid4()
    await fake.set_turn_provenance(
        wsid,
        triggering_turn_id=ttid,
        triggering_prompt="hello agent",
        created_by=7,
        origin_session_id="sess-1",
    )
    assert fake.provenance == [
        (wsid, ttid, "hello agent", 7, "sess-1"),
    ]
```

- [ ] **Step 1.6: Run it — expect FAIL**

Run: `uv run pytest tests/proposal_assist/test_drafting_fake_adapter.py::test_fake_adapter_records_set_turn_provenance -v`
Expected: FAIL with `AttributeError: 'FakeAgentAdapter' object has no attribute 'set_turn_provenance'`.

- [ ] **Step 1.7: Add `set_turn_provenance` to the Protocol**

In `src/call_manager/proposal_assist/drafting/contract.py`, add `import uuid` is already present (line 13). After the `terminate` method in the `DraftingAgent` Protocol (line 139), add:

```python

    async def set_turn_provenance(
        self,
        workspace_id: uuid.UUID,
        *,
        triggering_turn_id: uuid.UUID | None,
        triggering_prompt: str | None,
        created_by: int | None,
        origin_session_id: str | None,
    ) -> None:
        """Stamp server-trusted provenance onto the workspace's retained
        ToolContext before the next send_turn. Called UNCONDITIONALLY every turn
        by process_turn_once (the subprocess is reused across turns, so a missed
        stamp would leak the prior turn's provenance into the propose_* tools).
        A no-op if the workspace has no live handle/ctx (the next launch rebuilds
        the ctx, and the processor re-stamps before send_turn anyway)."""
        ...
```

Adding a method to the `@runtime_checkable` `DraftingAgent` Protocol means every
`isinstance(x, DraftingAgent)` check now also requires `set_turn_provenance`. The
`_Dummy` in `tests/proposal_assist/test_drafting_contract.py` (lines 87-100, only
`launch`/`send_turn`/`terminate`) would fail its `isinstance(_Dummy(), DraftingAgent)`
assertion (line 101). Update `_Dummy` to add the new method (after `terminate`,
line 99):

```python
        async def set_turn_provenance(
            self,
            workspace_id,
            *,
            triggering_turn_id,
            triggering_prompt,
            created_by,
            origin_session_id,
        ) -> None:
            ...
```

(This keeps the runtime_checkable contract test green; the file is run in Step 1.9
and added to the Step 1.17 commit.)

- [ ] **Step 1.8: Implement `set_turn_provenance` on the Fake**

In `src/call_manager/proposal_assist/drafting/fake_adapter.py`, in `__init__` (after line 36, the `self._scripted` line) add:

```python
        self.provenance: list[
            tuple[Any, Any, str | None, int | None, str | None]
        ] = []
```

Then add the method after `terminate` (line 74):

```python
    async def set_turn_provenance(
        self,
        workspace_id,
        *,
        triggering_turn_id,
        triggering_prompt,
        created_by,
        origin_session_id,
    ) -> None:
        self.provenance.append(
            (
                workspace_id,
                triggering_turn_id,
                triggering_prompt,
                created_by,
                origin_session_id,
            )
        )
```

(`Any` is already imported at fake_adapter.py:9.)

- [ ] **Step 1.9: Run it — expect PASS; confirm Protocol still satisfied**

Run: `uv run pytest tests/proposal_assist/test_drafting_fake_adapter.py::test_fake_adapter_records_set_turn_provenance tests/proposal_assist/test_drafting_fake_adapter.py::test_fake_adapter_implements_protocol tests/proposal_assist/test_drafting_contract.py::test_drafting_agent_protocol_methods_exist -v`
Expected: all three PASS (the `isinstance(fake, DraftingAgent)` AND `isinstance(_Dummy(), DraftingAgent)` runtime_checkable checks still hold — both now declare the new `set_turn_provenance` method).

- [ ] **Step 1.10: Implement ctx retention + `set_turn_provenance` on the real adapter**

The adapter must retain the `ToolContext` the factory builds. The factory's `tools_factory(repo_dir, workspace_id)` constructs the ctx internally and returns only the MCP server, so we give the adapter a **ctx sink**: a dict the factory writes into and the adapter reads.

In `src/call_manager/proposal_assist/drafting/claude_adapter.py`, extend `__init__` (lines 140-151) to accept and store the sink + a per-workspace retained-ctx map:

```python
    def __init__(
        self,
        *,
        model: str,
        workspaces_dir: str,
        permission_mode: str = "default",
        tools_factory: Callable[[Path, uuid.UUID], McpSdkServerConfig] | None = None,
        tool_context_sink: dict[uuid.UUID, Any] | None = None,
    ) -> None:
        self._model = model
        self._workspaces_dir = Path(workspaces_dir)
        self._permission_mode = permission_mode  # type: ignore[assignment]
        self._tools_factory = tools_factory
        # SP2a-2: the factory's tools_factory closure writes the per-workspace
        # ToolContext it builds into this sink (keyed by workspace_id) so
        # set_turn_provenance can stamp the live ctx the MCP closures captured.
        # None when no factory is wired (the bare-adapter security tests).
        self._tool_context_sink: dict[uuid.UUID, Any] = (
            tool_context_sink if tool_context_sink is not None else {}
        )
```

> Note: `permission_mode` default changes here from `"bypassPermissions"` to `"default"`. Task 7 owns the allowlist swap + comment rewrite; this Task only needs the new default so the adapter constructs with it. The wiring test that asserts `"bypassPermissions"` is updated in Task 7 — until then it FAILS. Run Task 1's tests in isolation (the commands above target specific `::test`s, so the Task-7-owned wiring test does not block Task 1's green).

Add `set_turn_provenance` after `terminate` (line 428):

```python
    async def set_turn_provenance(
        self,
        workspace_id: uuid.UUID,
        *,
        triggering_turn_id: uuid.UUID | None,
        triggering_prompt: str | None,
        created_by: int | None,
        origin_session_id: str | None,
    ) -> None:
        """Stamp provenance onto the retained per-workspace ToolContext that the
        MCP tool closures captured at launch. No-op if no ctx is retained (e.g.
        an adapter built without a tools_factory, or before the first launch)."""
        ctx = self._tool_context_sink.get(workspace_id)
        if ctx is None:
            return
        ctx.triggering_turn_id = triggering_turn_id
        ctx.triggering_prompt = triggering_prompt
        ctx.created_by = created_by
        ctx.origin_session_id = origin_session_id
```

- [ ] **Step 1.11: Wire the ctx sink through the factory**

In `src/call_manager/proposal_assist/drafting/factory.py`, change the `tools_factory` closure + adapter construction (lines 68-83) so the factory owns a sink dict, writes the ctx into it, and hands it to the adapter:

```python
        tool_context_sink: dict = {}

        def tools_factory(repo_dir, workspace_id):
            ctx = ToolContext(
                workspace_id=workspace_id,
                repo_dir=repo_dir,
                session_factory=session_factory,
                settings=settings,
                citation_service=_citation_service,
                reference_ingest_limiter=reference_ingest_limiter,
            )
            # SP2a-2: retain the ctx so set_turn_provenance can stamp the SAME
            # object the MCP tool closures captured (one ctx per workspace launch).
            tool_context_sink[workspace_id] = ctx
            return build_proposal_assist_mcp_server(ctx)

        return ClaudeAgentSDKAdapter(
            model=settings.proposal_assist_llm_model,
            workspaces_dir=settings.workspaces_dir,
            tools_factory=tools_factory,
            tool_context_sink=tool_context_sink,
        )
```

> CRITICAL: `tools_factory` must stash the SAME `ToolContext` *instance* that
> `build_proposal_assist_mcp_server(ctx)` hands to the MCP tool closures — NOT a
> copy. The whole mechanism is `set_turn_provenance` mutating that one object
> (`ctx.triggering_turn_id = ...`) so the change is visible to the live propose_*
> tools that captured it at launch. The code above does this correctly (one `ctx`
> built, passed to both `tool_context_sink[workspace_id] = ctx` and
> `build_proposal_assist_mcp_server(ctx)`). Do not reconstruct, `dataclasses.replace`,
> or deep-copy the ctx into the sink.

- [ ] **Step 1.12: Add the registry + controller pass-throughs**

In `src/call_manager/proposal_assist/drafting/registry.py`, after `send_turn` (line 265) add:

```python
    async def set_turn_provenance(
        self,
        workspace_id: uuid.UUID,
        *,
        triggering_turn_id: uuid.UUID | None,
        triggering_prompt: str | None,
        created_by: int | None,
        origin_session_id: str | None,
    ) -> None:
        """Pass-through to the adapter so the processor can stamp provenance on
        the cached handle's ToolContext before each turn."""
        await self._adapter.set_turn_provenance(
            workspace_id,
            triggering_turn_id=triggering_turn_id,
            triggering_prompt=triggering_prompt,
            created_by=created_by,
            origin_session_id=origin_session_id,
        )
```

In `src/call_manager/proposal_assist/drafting/controller.py`, after `terminate` (line 216) add:

```python
    async def set_turn_provenance(
        self,
        workspace_id: uuid.UUID,
        *,
        triggering_turn_id: uuid.UUID | None,
        triggering_prompt: str | None,
        created_by: int | None,
        origin_session_id: str | None,
    ) -> None:
        """Stamp server-trusted provenance onto the workspace's retained
        ToolContext before send_turn. Called by process_turn_once every turn."""
        await self._registry.set_turn_provenance(
            workspace_id,
            triggering_turn_id=triggering_turn_id,
            triggering_prompt=triggering_prompt,
            created_by=created_by,
            origin_session_id=origin_session_id,
        )
```

- [ ] **Step 1.12a: Real-adapter (sink-level) cold-launch provenance test**

The Fake records the call but never exercises the *sink* path the real adapter
uses — and that path has a cold-launch hazard the controller flagged: the sink is
populated ONLY when `tools_factory(repo_dir, workspace_id)` runs (inside the
launch path, via `_build_options_kwargs`). If provenance is stamped before the
first launch, the sink is empty and the stamp is silently dropped. This test pins
the real adapter's sink behavior directly (no SDK subprocess / no `connect()`): it
drives the factory via `_build_options_kwargs` (the same seam the existing wiring
tests use), then asserts the retained `ToolContext` carries the stamped values.

Append to `tests/proposal_assist/test_drafting_claude_adapter.py`:

```python
@pytest.mark.asyncio
async def test_set_turn_provenance_stamps_retained_ctx_after_launch():
    """The real adapter retains the per-workspace ToolContext the factory built
    (in the sink) and set_turn_provenance mutates THAT instance, so the live MCP
    tool closures see the stamped provenance. Pre-launch the sink is empty -> a
    no-op (no raise); post-launch the SAME ctx carries the values."""
    import uuid as _uuid
    from pathlib import Path

    from call_manager.proposal_assist.drafting.contract import LaunchContext
    from call_manager.proposal_assist.tools.registry import ToolContext

    wsid = _uuid.uuid4()
    sink: dict = {}
    captured: dict = {}

    def tools_factory(repo_dir: Path, workspace_id):
        ctx = ToolContext(
            workspace_id=workspace_id,
            repo_dir=repo_dir,
            session_factory=None,  # type: ignore[arg-type]
            settings=None,  # type: ignore[arg-type]
        )
        sink[workspace_id] = ctx
        captured["ctx"] = ctx  # the SAME instance the MCP closures would capture
        # A minimal stand-in for the MCP server config; the adapter only stores it.
        return {"type": "sdk", "name": "proposal_assist", "instance": object()}

    adapter = ClaudeAgentSDKAdapter(
        model="claude-haiku-4-5",
        workspaces_dir="/tmp/test_workspaces",
        tools_factory=tools_factory,
        tool_context_sink=sink,
    )
    ttid = _uuid.uuid4()

    # Pre-launch: the sink is empty -> stamping is a no-op, never raises.
    await adapter.set_turn_provenance(
        wsid,
        triggering_turn_id=ttid,
        triggering_prompt="cold",
        created_by=1,
        origin_session_id=None,
    )
    assert wsid not in sink

    # Launch path populates the sink (we use _build_options_kwargs to trigger the
    # factory without a live SDK connect()).
    adapter._build_options_kwargs(LaunchContext(workspace_id=wsid))
    assert sink[wsid] is captured["ctx"]

    # Now the stamp lands on the retained ctx (the cold-launch path the Fake masks).
    await adapter.set_turn_provenance(
        wsid,
        triggering_turn_id=ttid,
        triggering_prompt="hello agent",
        created_by=7,
        origin_session_id="sess-1",
    )
    stamped = captured["ctx"]
    assert stamped.triggering_turn_id == ttid
    assert stamped.triggering_prompt == "hello agent"
    assert stamped.created_by == 7
    assert stamped.origin_session_id == "sess-1"
```

Run: `uv run pytest tests/proposal_assist/test_drafting_claude_adapter.py::test_set_turn_provenance_stamps_retained_ctx_after_launch -v`
Expected: PASS once Steps 1.10–1.11 are implemented (the sink wiring + the adapter
`set_turn_provenance`). This is the cold-launch invariant the processor stamp
(Step 1.15) leans on: launch FIRST, then stamp.

- [ ] **Step 1.13: Write the failing processor-stamp test**

Append to `tests/proposal_assist/test_turns_processor.py`:

```python
@pytest.mark.asyncio
async def test_process_turn_stamps_provenance_on_adapter(
    pg_session_factory,
    _settings_env,
):
    """process_turn_once stamps server-trusted provenance via
    controller.set_turn_provenance after loading the message and before
    send_turn: triggering_turn_id = the user ChatTurn id, created_by =
    submitted_by, triggering_prompt = the raw typed text, origin_session_id =
    None on a fresh (never-resumed) session."""
    wsid, job_id, controller, adapter = _build_controller_and_submit(
        pg_session_factory,
        _settings_env,
        [TurnDoneEvent(tokens_in=1, tokens_out=1, model="fake", session_id="s1")],
    )

    with pg_session_factory() as s:
        user_turn = s.scalar(
            select(ChatTurn).where(
                ChatTurn.workspace_id == wsid, ChatTurn.role == "user"
            )
        )
        user_turn_id = user_turn.id

    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
    )

    assert len(adapter.provenance) == 1
    ws_arg, ttid, prompt, created_by, origin_session_id = adapter.provenance[0]
    assert ws_arg == wsid
    assert ttid == user_turn_id
    assert prompt == "Hi"  # the raw typed text submitted in _build_controller_and_submit
    assert created_by is not None  # the submitter
    assert origin_session_id is None  # fresh session, never resumed
```

- [ ] **Step 1.14: Run it — expect FAIL**

Run: `uv run pytest tests/proposal_assist/test_turns_processor.py::test_process_turn_stamps_provenance_on_adapter -v`
Expected: FAIL — `adapter.provenance` is empty (`assert len(...) == 1`) because the processor doesn't stamp yet.

- [ ] **Step 1.15: Add the triggering-prompt extractor + stamp call to the processor**

In `src/call_manager/proposal_assist/turns/processor.py`, add a module-level helper near `_prepend_summary` (after line 97):

```python
def _extract_triggering_prompt(content: Any) -> str | None:
    """The raw typed user text from a ChatTurn.content payload, mirroring the
    shapes _build_user_content (turns/submit.py) produces: a {"type":"text"}
    dict, or a block-list whose first {"type":"text"} block holds the text.
    Returns None if no text block is present. No truncation (the column is
    nullable Text)."""
    if isinstance(content, dict):
        if content.get("type") == "text":
            text = content.get("text")
            return text if isinstance(text, str) else None
        return None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                return text if isinstance(text, str) else None
    return None
```

First, capture the raw ChatTurn content INSIDE the with-block while `user_turn` is
still attached, right after `user_turn.content` is used to resolve `user_message`
(immediately after line 187, the `resolve_user_content(...)` call). Add:

```python
            # SP2a-2 §6: capture the raw typed payload while the ORM instance is
            # still attached — after the with-block closes, user_turn is detached
            # and lazy-loading user_turn.content would raise DetachedInstanceError.
            raw_turn_content = user_turn.content
```

Then, stamp provenance AFTER the message-load `with session_factory() as s:` block
closes (after line 220) and **before** the `try:`/`controller.send_turn` at lines
230-231. The stamp goes at **function-body indent (4 spaces)** — NOT nested under
`if summary_text:` (that branch only runs on a cold/post-compaction session; the
stamp must run every turn). Launch FIRST (idempotent — see note), THEN stamp, THEN
the existing `send_turn`:

```python
    # SP2a-2 §6: stamp server-trusted provenance onto the workspace's retained
    # ToolContext BEFORE send_turn and UNCONDITIONALLY every turn. The subprocess
    # (and its ToolContext) is reused across turns, so a missed stamp would leak
    # the prior turn's provenance into the propose_* tools. origin_session_id is
    # the resumed SDK session id at claim time (None on a fresh/post-compaction
    # session — the real id only lands at TurnDoneEvent).
    #
    # COLD-LAUNCH ORDER: the retained ToolContext only exists once launch's
    # tools_factory has run. set_turn_provenance is a no-op against an empty sink,
    # so we ensure the launch FIRST (controller.launch is idempotent —
    # claim_launch_or_wait; send_turn calls it internally anyway), THEN stamp,
    # THEN send_turn. Without the explicit launch, a workspace's first (cold) turn
    # would stamp an empty sink and silently drop provenance.
    await controller.launch(workspace_id)
    await controller.set_turn_provenance(
        workspace_id,
        triggering_turn_id=user_turn_id,
        triggering_prompt=_extract_triggering_prompt(raw_turn_content),
        created_by=submitted_by_user_id,
        origin_session_id=ws_session_id,
    )
```

> `user_turn_id`, `submitted_by_user_id`, and `ws_session_id` are all in scope here
> (`user_turn_id`/`submitted_by_user_id` at line 165; `ws_session_id` at line 214).
> The stamp uses the captured `raw_turn_content` (NOT `user_turn.content`) because
> `user_turn` is detached after the with-block — `raw_turn_content` is the
> unresolved ChatTurn payload (the raw typed text, NOT the reference-resolved
> `user_message`). `controller.launch` is `async` and idempotent; calling it
> before `send_turn` (which also calls it) only guarantees the sink is populated
> before the stamp.

- [ ] **Step 1.16: Run it — expect PASS; re-run the existing processor tests**

Run: `uv run pytest tests/proposal_assist/test_turns_processor.py::test_process_turn_stamps_provenance_on_adapter tests/proposal_assist/test_turns_processor.py::test_process_turn_writes_assistant_text_event -v`
Expected: both PASS. (If either reports `OperationalError`/`connection refused`, re-run once — testcontainer flake.)

- [ ] **Step 1.17: Lint gate, then commit**

Run the lint gate (see Conventions). Then:

```bash
git add src/call_manager/proposal_assist/tools/registry.py \
  src/call_manager/proposal_assist/drafting/contract.py \
  src/call_manager/proposal_assist/drafting/claude_adapter.py \
  src/call_manager/proposal_assist/drafting/factory.py \
  src/call_manager/proposal_assist/drafting/fake_adapter.py \
  src/call_manager/proposal_assist/drafting/registry.py \
  src/call_manager/proposal_assist/drafting/controller.py \
  src/call_manager/proposal_assist/turns/processor.py \
  tests/proposal_assist/test_tools_registry.py \
  tests/proposal_assist/test_drafting_fake_adapter.py \
  tests/proposal_assist/test_drafting_contract.py \
  tests/proposal_assist/test_drafting_claude_adapter.py \
  tests/proposal_assist/test_turns_processor.py
git commit -m "feat(proposal-assist): server-trusted turn provenance substrate (SP2a-2 Task 1)"
```

---

## Task 2: `read_proposal` captures `agent_view_sections`

`read_proposal_impl` already sets `ctx.agent_view_head` HEAD-first. Now it also records the per-section shas the agent saw, so `propose_edit` (Task 4) can take its `base_section_sha` from the agent's *read-time* snapshot (anti-clobber), not a live recompute.

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/proposal.py:56-80` (`read_proposal_impl`)
- Test: `tests/proposal_assist/test_tools_proposal_read_sections.py` (new)

- [ ] **Step 2.1: Write the failing test**

Create `tests/proposal_assist/test_tools_proposal_read_sections.py`:

```python
"""read_proposal_impl must populate ctx.agent_view_sections (section_index ->
section sha) at read time, HEAD-first, so propose_edit can take an anti-clobber
read-time base."""

import subprocess
import uuid
from pathlib import Path

import pytest

from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.tools.proposal import (
    PROPOSAL_FILENAME,
    read_proposal_impl,
)
from call_manager.proposal_assist.tools.registry import ToolContext


def _init_repo(repo_dir: Path, content: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


@pytest.mark.asyncio
async def test_read_proposal_populates_agent_view_sections(tmp_path):
    content = "preamble\n\n## Methods\noriginal.\n\n## Results\nfindings.\n"
    repo = Path(tmp_path) / "repo"
    _init_repo(repo, content)
    ctx = ToolContext(
        workspace_id=uuid.uuid4(),
        repo_dir=repo,
        session_factory=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )

    result = await read_proposal_impl(ctx)

    assert result["content"] == content
    expected = {s.index: s.sha for s in split_sections(content)}
    assert ctx.agent_view_sections == expected
    # the full read-time proposal text is retained for propose_citation's
    # read-time anchor->section mapping.
    assert ctx.agent_view_proposal == content
    # sanity: head was captured too (existing behavior)
    assert ctx.agent_view_head is not None
```

- [ ] **Step 2.2: Run it — expect FAIL**

Run: `uv run pytest tests/proposal_assist/test_tools_proposal_read_sections.py::test_read_proposal_populates_agent_view_sections -v`
Expected: FAIL — `ctx.agent_view_sections == {}` (empty default) ≠ the expected per-section map.

- [ ] **Step 2.3: Populate `agent_view_sections` in `read_proposal_impl`**

In `src/call_manager/proposal_assist/tools/proposal.py`, add the import near the top (after line 36 region — `split_sections` is not yet imported here):

```python
from call_manager.proposal_assist.editing.sectionizer import split_sections
```

Then in `read_proposal_impl`, after `content = path.read_text()` and before `return {"content": content}` (currently lines 79-80), insert:

```python
    # SP2a-2 §3: record the per-section shas the agent saw, so a later
    # propose_edit takes its base_section_sha from this READ-time snapshot
    # (anti-clobber) rather than a live recompute. Built from the same `content`
    # we return, so the dict and the returned text agree exactly.
    ctx.agent_view_sections = {s.index: s.sha for s in split_sections(content)}
    # SP2a-2 §3: also retain the full read-time proposal text, so propose_citation
    # (Task 5) maps an anchor offset -> section_index against the SAME snapshot the
    # section shas came from (consistent (section_index, base_section_sha) pair even
    # if a human added/removed a `##` section between read and propose).
    ctx.agent_view_proposal = content
```

- [ ] **Step 2.4: Run it — expect PASS**

Run: `uv run pytest tests/proposal_assist/test_tools_proposal_read_sections.py::test_read_proposal_populates_agent_view_sections -v`
Expected: PASS.

- [ ] **Step 2.5: Lint gate, then commit**

Run the lint gate. Then:

```bash
git add src/call_manager/proposal_assist/tools/proposal.py \
  tests/proposal_assist/test_tools_proposal_read_sections.py
git commit -m "feat(proposal-assist): read_proposal captures agent_view_sections (SP2a-2 Task 2)"
```

---

## Task 3: `propose_citation_insert` service

A sibling to `propose_prose_edit` that writes a `kind="citation_insert"` row with the citation payload + base shas + provenance. Service-level only (no git, no verify — the tool layer in Task 5 verifies and derives the section).

**Files:**
- Modify: `src/call_manager/proposal_assist/proposals/proposing.py:38-70` (add the function)
- Test: `tests/proposal_assist/proposals/test_proposing.py` (add a test)

- [ ] **Step 3.1: Write the failing test**

Append to `tests/proposal_assist/proposals/test_proposing.py`:

```python
def test_propose_citation_insert_writes_row(pg_session_factory, tmp_path):
    from call_manager.proposal_assist.proposals.proposing import (
        propose_citation_insert,
    )

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        content = "preamble\n\n## Methods\noriginal.\n"
        repo = Path(tmp_path) / str(ws.id) / "repo"
        _init_repo(repo, content)
        base_commit, base_section = capture_base(repo, 1)

        pc = propose_citation_insert(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            reference_id="11111111-1111-1111-1111-111111111111",
            anchor="original.",
            chunk_index=0,
            evidence_quote="some verbatim span",
            stance="supports",
            confidence="high",
            origin_kind="interactive_turn",
        )
        s.flush()

        got = s.get(ProposedChange, pc.id)
        assert got.status == "pending"
        assert got.target_artifact == "draft"
        assert got.kind == "citation_insert"
        assert got.section_index == 1
        assert got.base_section_sha == split_sections(content)[1].sha
        assert got.payload == {
            "reference_id": "11111111-1111-1111-1111-111111111111",
            "anchor": "original.",
            "chunk_index": 0,
            "evidence_quote": "some verbatim span",
            "stance": "supports",
            "confidence": "high",
        }
```

- [ ] **Step 3.2: Run it — expect FAIL**

Run: `uv run pytest tests/proposal_assist/proposals/test_proposing.py::test_propose_citation_insert_writes_row -v`
Expected: FAIL with `ImportError: cannot import name 'propose_citation_insert'`.

- [ ] **Step 3.3: Add the service function**

In `src/call_manager/proposal_assist/proposals/proposing.py`, after `propose_prose_edit` (line 70), add:

```python
def propose_citation_insert(
    *,
    session: Session,
    workspace_id: uuid.UUID,
    section_index: int,
    base_commit_sha: str,
    base_section_sha: str,
    reference_id: str,
    anchor: str,
    chunk_index: int,
    evidence_quote: str,
    stance: str = "supports",
    confidence: str | None = None,
    origin_kind: str,
    origin_session_id: str | None = None,
    origin_job_id: uuid.UUID | None = None,
    triggering_turn_id: uuid.UUID | None = None,
    triggering_prompt: str | None = None,
    created_by: int | None = None,
) -> ProposedChange:
    """Write a pending ``kind="citation_insert"`` proposed_change row.

    The payload mirrors insert_citation's args (reference_id, anchor,
    chunk_index, evidence_quote, stance, confidence). ev_id / citekey / offsets
    are minted authoritatively at accept time (by _insert_citation_locked), not
    stored here. section_index / base_section_sha are derived by the tool layer
    from where the anchor falls in the read-time proposal (the citation row still
    participates in section-keyed staleness). The caller owns the transaction
    (this only flush()es).
    """
    pc = ProposedChange(
        workspace_id=workspace_id,
        target_artifact="draft",
        section_index=section_index,
        base_commit_sha=base_commit_sha,
        base_section_sha=base_section_sha,
        kind="citation_insert",
        payload={
            "reference_id": reference_id,
            "anchor": anchor,
            "chunk_index": chunk_index,
            "evidence_quote": evidence_quote,
            "stance": stance,
            "confidence": confidence,
        },
        origin_kind=origin_kind,
        origin_session_id=origin_session_id,
        origin_job_id=origin_job_id,
        triggering_turn_id=triggering_turn_id,
        triggering_prompt=triggering_prompt,
        created_by=created_by,
    )
    session.add(pc)
    session.flush()
    return pc
```

- [ ] **Step 3.4: Run it — expect PASS**

Run: `uv run pytest tests/proposal_assist/proposals/test_proposing.py::test_propose_citation_insert_writes_row -v`
Expected: PASS.

- [ ] **Step 3.5: Lint gate, then commit**

Run the lint gate. Then:

```bash
git add src/call_manager/proposal_assist/proposals/proposing.py \
  tests/proposal_assist/proposals/test_proposing.py
git commit -m "feat(proposal-assist): propose_citation_insert service (SP2a-2 Task 3)"
```

---

## Task 4: `propose_edit` tool (impl + `@tool`)

`propose_edit_impl(ctx, *, section_index, new_text)` takes its base from the agent's **read-time** snapshot (`ctx.agent_view_head` + `ctx.agent_view_sections[section_index]`) — it MUST NOT call `capture_base` (that recomputes live HEAD + re-splits the live doc → clobbers a human edit landed since the read). It calls `propose_prose_edit` with the ctx provenance, opens its own session, commits, and returns `{"proposed_change_id", "status": "pending"}`.

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/proposal.py` (add `propose_edit_impl`)
- Modify: `src/call_manager/proposal_assist/tools/registry.py` (add `@tool` + import + list entry — done fully in Task 7; the closure is added here so the envelope test can exercise it)
- Test: `tests/proposal_assist/tools/test_propose_edit.py` (new)

> NOTE on ordering: Task 7 owns the *allowlist* swap and the final tool-list shape. To keep Task 4 independently testable, add the `propose_edit` `@tool` closure + its impl import + its list entry to `registry.py` **now**; Task 7 then removes the 6 mutating entries and adds `propose_citation`. The intermediate tool count after Task 4 is 24 (23 + propose_edit); after Task 5 it is 25; Task 7 removes 6 → 19. The `test_collect_tools_returns_twenty_named_tools` assertion is updated to its final 19-name form **in Task 7** — it stays red between Task 4 and Task 7, which is expected (run the Task-4 tests in isolation).

- [ ] **Step 4.1: Write the failing impl-level tests (anti-clobber + unread-section reject)**

Create `tests/proposal_assist/tools/test_propose_edit.py`:

```python
"""propose_edit_impl: writes a pending prose_edit proposed_change using the
agent's READ-TIME base (anti-clobber), never a live recompute. Rejects a
section the agent never read."""

import subprocess
import uuid
from pathlib import Path

import pytest

from call_manager.config import Settings
from call_manager.db.models import ProposedChange
from call_manager.proposal_assist.editing.save import save_section
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.tools.proposal import (
    PROPOSAL_FILENAME,
    propose_edit_impl,
    read_proposal_impl,
)
from call_manager.proposal_assist.tools.registry import ToolContext
from tests.proposal_assist.conftest import make_workspace


def _init_repo(repo_dir: Path, content: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


def _build_ctx(pg_session_factory, tmp_path, content):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    repo = Path(tmp_path) / str(wsid) / "repo"
    _init_repo(repo, content)
    ctx = ToolContext(
        workspace_id=wsid,
        repo_dir=repo,
        session_factory=pg_session_factory,
        settings=Settings(),
    )
    return ctx, wsid, repo


@pytest.mark.asyncio
async def test_propose_edit_uses_read_time_base_not_live(
    pg_session_factory, _settings_env, tmp_path
):
    content = "preamble\n\n## Methods\noriginal.\n"
    ctx, wsid, repo = _build_ctx(pg_session_factory, tmp_path, content)

    # Agent reads — snapshots head + section shas at T0.
    await read_proposal_impl(ctx)
    read_time_section_sha = split_sections(content)[1].sha

    # A human edits the SAME section after the read (HEAD + section sha move).
    with pg_session_factory() as s:
        ws = s.get(__import__("call_manager.db.models", fromlist=["Workspace"]).Workspace, wsid)
        base_commit, base_section = (ctx.agent_view_head, read_time_section_sha)
        save_section(
            session=s,
            workspace=ws,
            user_id=ws.owner_id,
            section_index=1,
            new_text="## Methods\nhuman-edited.\n",
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            workspaces_dir=str(tmp_path),
        )
        s.commit()

    # Agent now proposes — must record the READ-TIME base (so accept goes stale),
    # NOT the live (human-edited) base.
    result = await propose_edit_impl(
        ctx, section_index=1, new_text="## Methods\nagent-version.\n"
    )
    assert "error" not in result, result
    assert result["status"] == "pending"

    with pg_session_factory() as s:
        pc = s.get(ProposedChange, uuid.UUID(result["proposed_change_id"]))
        assert pc.kind == "prose_edit"
        assert pc.section_index == 1
        # The recorded base is the READ-TIME sha, not the live human-edited one.
        assert pc.base_section_sha == read_time_section_sha
        assert pc.base_commit_sha == ctx.agent_view_head


@pytest.mark.asyncio
async def test_propose_edit_rejects_unread_section(
    pg_session_factory, _settings_env, tmp_path
):
    content = "preamble\n\n## Methods\noriginal.\n"
    ctx, wsid, repo = _build_ctx(pg_session_factory, tmp_path, content)
    # No read_proposal call: agent_view_head is None and agent_view_sections {}.
    result = await propose_edit_impl(
        ctx, section_index=1, new_text="## Methods\nx.\n"
    )
    assert "error" in result
    assert "read_proposal" in result["error"]


@pytest.mark.asyncio
async def test_propose_edit_rejects_section_read_but_not_in_view(
    pg_session_factory, _settings_env, tmp_path
):
    content = "preamble\n\n## Methods\noriginal.\n"
    ctx, wsid, repo = _build_ctx(pg_session_factory, tmp_path, content)
    await read_proposal_impl(ctx)  # populates indices 0 and 1 only
    result = await propose_edit_impl(
        ctx, section_index=5, new_text="## Ghost\nx.\n"
    )
    assert "error" in result
    assert "read_proposal" in result["error"]
```

- [ ] **Step 4.2: Run it — expect FAIL**

Run: `uv run pytest tests/proposal_assist/tools/test_propose_edit.py -v`
Expected: FAIL with `ImportError: cannot import name 'propose_edit_impl'`.

- [ ] **Step 4.3: Implement `propose_edit_impl`**

In `src/call_manager/proposal_assist/tools/proposal.py`, add the import near the existing imports (the `propose_prose_edit` service):

```python
from call_manager.proposal_assist.proposals.proposing import propose_prose_edit
```

> Import-cycle check: `proposals/proposing.py` imports `PROPOSAL_FILENAME` from `tools.proposal` at module top. Importing `propose_prose_edit` at `tools.proposal` module top would create a cycle. **Import it lazily inside `propose_edit_impl`** to avoid the cycle (mirrors how the registry lazily imports tool impls). So do NOT add the top-level import above; instead do the lazy import shown in the function body below.

Add the function after `read_proposal_impl` (line 80), before `edit_proposal_impl`:

```python
async def propose_edit_impl(
    ctx: ToolContext,
    *,
    section_index: int,
    new_text: str,
) -> dict[str, Any]:
    """Propose a full-text replacement of one section as a pending
    proposed_change (NOT a git commit). The base is the agent's READ-TIME
    snapshot (ctx.agent_view_head + ctx.agent_view_sections[section_index]) — we
    MUST NOT recompute it live (capture_base) here, or a human edit landed since
    the read would silently become the base and accept would clobber it.

    Returns {"proposed_change_id", "status": "pending"} or {"error": ...}.
    """
    # Lazy import avoids the proposing<->proposal module cycle.
    from call_manager.proposal_assist.proposals.proposing import propose_prose_edit

    base_commit_sha = ctx.agent_view_head
    if base_commit_sha is None:
        return {"error": _NOT_READ_ERROR}
    base_section_sha = ctx.agent_view_sections.get(section_index)
    if base_section_sha is None:
        return {"error": _NOT_READ_ERROR}

    with ctx.session_factory() as session:
        pc = propose_prose_edit(
            session=session,
            workspace_id=ctx.workspace_id,
            section_index=section_index,
            new_text=new_text,
            base_commit_sha=base_commit_sha,
            base_section_sha=base_section_sha,
            origin_kind="interactive_turn",
            origin_session_id=ctx.origin_session_id,
            triggering_turn_id=ctx.triggering_turn_id,
            triggering_prompt=ctx.triggering_prompt,
            created_by=ctx.created_by,
        )
        pc_id = str(pc.id)
        session.commit()
    return {"proposed_change_id": pc_id, "status": "pending"}
```

> (Remove the stray top-level `from ...proposing import propose_prose_edit` line if you added it — keep only the lazy import inside the function.)

- [ ] **Step 4.4: Run it — expect PASS**

Run: `uv run pytest tests/proposal_assist/tools/test_propose_edit.py -v`
Expected: all 3 PASS. (Re-run once on any `OperationalError` flake.)

- [ ] **Step 4.5: Write the failing closure/envelope test**

Append to `tests/proposal_assist/test_tools_registry.py`:

```python
@pytest.mark.asyncio
async def test_propose_edit_closure_returns_pending_envelope(tmp_path):
    """The registered propose_edit closure returns the MCP success envelope
    with status 'pending' after a read; an unread section returns an error
    envelope (not a raise)."""
    import json
    import subprocess
    import uuid as _uuid

    from call_manager.config import Settings
    from call_manager.proposal_assist.tools.proposal import (
        PROPOSAL_FILENAME,
        read_proposal_impl,
    )
    from call_manager.proposal_assist.tools.registry import (
        ToolContext,
        _collect_tools,
    )

    repo = Path(tmp_path) / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "a@b"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / PROPOSAL_FILENAME).write_text("preamble\n\n## Methods\norig.\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    # Error path: no read yet -> error envelope, no raise.
    ctx = ToolContext(
        workspace_id=_uuid.uuid4(),
        repo_dir=repo,
        session_factory=None,  # type: ignore[arg-type]
        settings=Settings(),
    )
    tools = _collect_tools(ctx)
    propose = next(t for t in tools if t.name == "propose_edit")
    result = await propose.handler({"section_index": 1, "new_text": "## Methods\nx.\n"})
    assert result["is_error"] is True
    assert "read_proposal" in result["content"][0]["text"]
```

- [ ] **Step 4.6: Run it — expect FAIL**

Run: `uv run pytest "tests/proposal_assist/test_tools_registry.py::test_propose_edit_closure_returns_pending_envelope" -v`
Expected: FAIL — `next(... t.name == "propose_edit")` raises `StopIteration` (no such tool yet).

- [ ] **Step 4.7: Add the `propose_edit` `@tool` closure + import + list entry**

In `src/call_manager/proposal_assist/tools/registry.py`, in `_collect_tools`, extend the `from call_manager.proposal_assist.tools.proposal import (...)` block (currently `edit_proposal_impl, read_proposal_impl, replace_proposal_impl` at lines 158-162) to also import `propose_edit_impl`:

```python
    from call_manager.proposal_assist.tools.proposal import (
        edit_proposal_impl,
        propose_edit_impl,
        read_proposal_impl,
        replace_proposal_impl,
    )
```

Add the closure (place it right after the `read_proposal` `@tool`, before `edit_proposal`):

```python
    @tool(
        "propose_edit",
        "Propose a full-text replacement of one section of proposal.md as a "
        "PENDING change for a human to review and accept — this does NOT edit "
        "or commit anything. `section_index` is the 0-based section you are "
        "rewriting (0 is the preamble); call read_proposal first so the system "
        "knows which version you based your edit on. `new_text` is the WHOLE new "
        "section including its `## heading` line. Returns a proposed_change_id "
        "and status 'pending'.",
        {
            "type": "object",
            "properties": {
                "section_index": {"type": "integer"},
                "new_text": {"type": "string"},
            },
            "required": ["section_index", "new_text"],
        },
    )
    async def propose_edit(args):
        try:
            section_index = int(args["section_index"])
        except (TypeError, ValueError, KeyError):
            return _mcp_error("section_index must be an integer")
        return await _safe_tool(
            "propose_edit",
            ctx,
            propose_edit_impl,
            ctx,
            section_index=section_index,
            new_text=args["new_text"],
        )
```

Add `propose_edit` to the returned list (line 720-752). Insert after `read_proposal`:

```python
        read_proposal,
        # SP2a-2 — propose-only mutation
        propose_edit,
```

- [ ] **Step 4.8: Run it — expect PASS**

Run: `uv run pytest "tests/proposal_assist/test_tools_registry.py::test_propose_edit_closure_returns_pending_envelope" -v`
Expected: PASS.

- [ ] **Step 4.9: Lint gate, then commit**

Run the lint gate. Then:

```bash
git add src/call_manager/proposal_assist/tools/proposal.py \
  src/call_manager/proposal_assist/tools/registry.py \
  tests/proposal_assist/tools/test_propose_edit.py \
  tests/proposal_assist/test_tools_registry.py
git commit -m "feat(proposal-assist): propose_edit tool with read-time base (SP2a-2 Task 4)"
```

---

## Task 5: `propose_citation` tool (impl + `@tool`)

`propose_citation_impl` runs the propose-time verification subset of the insert checks (no mutex, no git write, no `agent_view_head` touch), maps the anchor to its section, and calls `propose_citation_insert`.

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/citations.py` (add `propose_citation_impl`)
- Modify: `src/call_manager/proposal_assist/tools/registry.py` (add `@tool` + import + list entry)
- Test: `tests/proposal_assist/tools/test_propose_citation.py` (new), `tests/proposal_assist/test_tools_registry.py` (envelope tests)

- [ ] **Step 5.1: Write the failing impl-level tests**

Create `tests/proposal_assist/tools/test_propose_citation.py`. Reuse the `_seed_reference` / `_init_repo` / `_build_ctx` shape from `test_citations_insert.py`:

```python
"""propose_citation_impl: verifies the citation at PROPOSE time (reference,
chunk, quote, anchor) and writes a pending citation_insert proposed_change.
No git commit (unlike insert_citation) — propose writes only a DB row."""

import subprocess
import uuid
from pathlib import Path

import pytest

from call_manager.config import Settings
from call_manager.db.models import ProposedChange, Reference, ReferenceChunk
from call_manager.proposal_assist.tools.citations import propose_citation_impl
from call_manager.proposal_assist.tools.proposal import (
    PROPOSAL_FILENAME,
    read_proposal_impl,
)
from call_manager.proposal_assist.tools.registry import ToolContext
from tests.proposal_assist.conftest import make_workspace

EMBED_DIM = 1024
CHUNK_TEXT = (
    "Adjuvant therapy reduces recurrence by 30% in the treated cohort. "
    "Secondary endpoints were not statistically significant."
)
ANCHOR = "recurrence is reduced substantially"
PROPOSAL = (
    "preamble\n\n## Methods\nWe argue that " + ANCHOR + " in our population.\n"
)


def _init_repo(repo_dir: Path, content: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


def _commit_count(repo_dir: Path) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return int(out.stdout.strip())


def _seed_reference(pg_session_factory, workspace_id, *, chunks):
    with pg_session_factory() as s:
        ref = Reference(
            workspace_id=workspace_id,
            kind="paper",
            source_type="uploaded",
            filename="seed.pdf",
            storage_path="seed/seed.pdf",
            mime_type="application/pdf",
            metadata_status="approved",
        )
        s.add(ref)
        s.flush()
        rid = ref.id
        for idx, section_path, text in chunks:
            s.add(
                ReferenceChunk(
                    reference_id=rid,
                    workspace_id=workspace_id,
                    chunk_index=idx,
                    section_path=section_path,
                    text=text,
                    embedding=[0.0] * EMBED_DIM,
                    embedding_model="fake-embedder",
                )
            )
        s.commit()
    return rid


def _build_ctx(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    repo = Path(tmp_path) / str(wsid) / "repo"
    _init_repo(repo, PROPOSAL)
    ctx = ToolContext(
        workspace_id=wsid,
        repo_dir=repo,
        session_factory=pg_session_factory,
        settings=Settings(),
    )
    return ctx, wsid, repo


@pytest.mark.asyncio
async def test_propose_citation_writes_row_and_maps_section(
    pg_session_factory, _settings_env, tmp_path
):
    ctx, wsid, repo = _build_ctx(pg_session_factory, tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    await read_proposal_impl(ctx)  # populate agent_view_head + sections
    before = _commit_count(repo)

    result = await propose_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
        stance="supports",
    )

    assert "error" not in result, result
    assert result["status"] == "pending"
    # No git commit — propose writes only a DB row.
    assert _commit_count(repo) == before

    with pg_session_factory() as s:
        pc = s.get(ProposedChange, uuid.UUID(result["proposed_change_id"]))
        assert pc.kind == "citation_insert"
        # The anchor falls in section 1 (## Methods), so the row is section-keyed there.
        assert pc.section_index == 1
        assert pc.payload["reference_id"] == str(rid)
        assert pc.payload["anchor"] == ANCHOR
        assert pc.payload["chunk_index"] == 0
        assert pc.payload["evidence_quote"] == "reduces recurrence by 30%"
        assert pc.payload["stance"] == "supports"


@pytest.mark.asyncio
async def test_propose_citation_bad_quote_rejected_no_commit(
    pg_session_factory, _settings_env, tmp_path
):
    ctx, wsid, repo = _build_ctx(pg_session_factory, tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    await read_proposal_impl(ctx)
    before = _commit_count(repo)

    result = await propose_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote="this span is not in the chunk at all",
        stance="supports",
    )
    assert "error" in result
    assert _commit_count(repo) == before  # nothing committed
    with pg_session_factory() as s:
        from sqlalchemy import select

        rows = s.scalars(
            select(ProposedChange).where(ProposedChange.workspace_id == wsid)
        ).all()
        assert rows == []  # no pending row written for an unverifiable citation


@pytest.mark.asyncio
async def test_propose_citation_bad_anchor_rejected(
    pg_session_factory, _settings_env, tmp_path
):
    ctx, wsid, repo = _build_ctx(pg_session_factory, tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    await read_proposal_impl(ctx)
    result = await propose_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor="this anchor is nowhere in the proposal",
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
    )
    assert "error" in result
```

- [ ] **Step 5.2: Run it — expect FAIL**

Run: `uv run pytest tests/proposal_assist/tools/test_propose_citation.py -v`
Expected: FAIL with `ImportError: cannot import name 'propose_citation_impl'`.

- [ ] **Step 5.3: Implement `propose_citation_impl`**

In `src/call_manager/proposal_assist/tools/citations.py`, add the helper imports the propose path needs. `split_sections` is not yet imported here — add at module top (alongside the other imports, after line 74):

```python
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.proposals.proposing import propose_citation_insert
```

> Import-cycle check: `proposals/proposing.py` imports only `gitio`, `sectionizer`, and `tools.proposal` (for `PROPOSAL_FILENAME`) — it does NOT import `tools.citations`. So a top-level `propose_citation_insert` import here is acyclic. `split_sections` (sectionizer) has no DB/tool imports — also safe.

Add the function after `insert_citation_impl` (line 386), before `_insert_citation_locked`:

```python
async def propose_citation_impl(
    ctx: ToolContext,
    *,
    reference_id: str,
    anchor: str,
    chunk_index: int,
    evidence_quote: str,
    stance: str = "supports",
    confidence: str | None = None,
) -> dict[str, Any]:
    """Propose a citation insert as a PENDING proposed_change (no git commit).

    Runs the propose-time verification subset of the insert checks (the same
    checks _insert_citation_locked runs, MINUS the commit mutex / write / HEAD
    touch), so an unverifiable citation never becomes a pending proposal. Anchor
    existence/uniqueness is verified against the LIVE on-disk proposal (propose-
    time validity), but the section keying (section_index + base_section_sha) is
    taken from the agent's READ-TIME snapshot (ctx.agent_view_proposal /
    ctx.agent_view_sections) so the pair stays consistent if a human added/removed
    a `##` section between read and propose. Then writes the row via
    propose_citation_insert. The full authoritative re-verify happens again at
    accept time under the commit mutex.

    Returns {"proposed_change_id", "status": "pending"} or {"error": ...}.
    """
    err = _validate_stance_confidence(stance, confidence)
    if err is not None:
        return err

    try:
        ref_uuid = uuid.UUID(str(reference_id))
    except (ValueError, AttributeError, TypeError):
        return {"error": "invalid reference_id (not a uuid)"}

    # Read-time base — required for the section row + anti-clobber staleness.
    base_commit_sha = ctx.agent_view_head
    if base_commit_sha is None:
        return {"error": "you haven't read the proposal yet — call read_proposal first"}
    # Read-time proposal snapshot — the section keying (index + base_section_sha)
    # is derived from this, NOT the live on-disk text, so the pair stays consistent
    # if a human added/removed a `##` section between read and propose.
    if ctx.agent_view_proposal is None:
        return {"error": "you haven't read the proposal yet — call read_proposal first"}

    with ctx.session_factory() as session:
        # 1. Load + workspace-scope the Reference (reject missing/foreign).
        ref = session.get(Reference, ref_uuid)
        if ref is None or ref.workspace_id != ctx.workspace_id:
            return {"error": "reference not found"}
        # 1a. Tombstoned reference cannot be newly cited.
        if ref.tombstoned_at is not None:
            return {"error": "reference is tombstoned and cannot be cited"}

        # 2. Load the ReferenceChunk (reference + index), workspace-scoped.
        chunk = (
            session.query(ReferenceChunk)
            .filter(
                ReferenceChunk.reference_id == ref_uuid,
                ReferenceChunk.chunk_index == chunk_index,
                ReferenceChunk.workspace_id == ctx.workspace_id,
            )
            .one_or_none()
        )
        if chunk is None:
            return {"error": "chunk not found"}

        # 3. Read the live proposal (no ledger needed at propose-time).
        proposal_path = ctx.repo_dir / PROPOSAL_FILENAME
        if not proposal_path.exists():
            return {"error": f"{PROPOSAL_FILENAME} does not exist in this workspace"}
        proposal_text = proposal_path.read_text()

        # 4. Quote-verify against the raw chunk text.
        try:
            find_quote_offsets(chunk.text, evidence_quote)
        except QuoteNotFound:
            return {"error": "evidence quote not found in chunk"}
        except QuoteAmbiguous:
            return {"error": "evidence quote is ambiguous (occurs multiple times) in chunk"}

        # 5. Anchor uniqueness — reuse insert_marker purely to trigger AnchorError
        # on not-found/ambiguous; the mutated text is discarded (we never write it).
        try:
            insert_marker(proposal_text, anchor, "anchorprobe", "ev-000000")
        except AnchorError as exc:
            if "ambiguous" in str(exc):
                return {"error": "anchor is ambiguous"}
            return {"error": "anchor not found"}

        # 6. Map the anchor to the section it falls in (cumulative split offsets),
        # so the citation row is section-keyed for staleness. Both the section
        # split AND the base_section_sha come from the READ-TIME snapshot
        # (ctx.agent_view_proposal / ctx.agent_view_sections), so the
        # (section_index, base_section_sha) pair is internally consistent even if a
        # human added/removed a `##` section on disk between read and propose.
        # NOTE: anchor uniqueness is proven in step 5 via insert_marker, which
        # SKIPS anchor occurrences overlapping an existing `[@citekey]<!-- ev -->`
        # marker. The bare `.find(anchor)` below picks the FIRST raw occurrence,
        # so in a proposal that already contains markers the two could in principle
        # disagree. This never triggers on a fresh marker-free draft (all the
        # plan's tests); the overlap-aware occurrence logic is DEFERRED — acceptable
        # for SP2a-2, do not add the complexity now.
        read_view = ctx.agent_view_proposal
        anchor_offset = read_view.find(anchor)
        sections = split_sections(read_view)
        cursor = 0
        section_index = len(sections) - 1
        for s in sections:
            if anchor_offset < cursor + len(s.text):
                section_index = s.index
                break
            cursor += len(s.text)
        base_section_sha = ctx.agent_view_sections.get(section_index)
        if base_section_sha is None:
            # The agent read the proposal but this section wasn't in its view
            # (it never read that far). Treat as unread — mirror prose's contract.
            return {"error": "you haven't read the proposal yet — call read_proposal first"}

        pc = propose_citation_insert(
            session=session,
            workspace_id=ctx.workspace_id,
            section_index=section_index,
            base_commit_sha=base_commit_sha,
            base_section_sha=base_section_sha,
            reference_id=str(ref_uuid),
            anchor=anchor,
            chunk_index=chunk_index,
            evidence_quote=evidence_quote,
            stance=stance,
            confidence=confidence,
            origin_kind="interactive_turn",
            origin_session_id=ctx.origin_session_id,
            triggering_turn_id=ctx.triggering_turn_id,
            triggering_prompt=ctx.triggering_prompt,
            created_by=ctx.created_by,
        )
        pc_id = str(pc.id)
        session.commit()
    return {"proposed_change_id": pc_id, "status": "pending"}
```

- [ ] **Step 5.4: Run it — expect PASS**

Run: `uv run pytest tests/proposal_assist/tools/test_propose_citation.py -v`
Expected: all 3 PASS. (Re-run once on `OperationalError` flake.)

- [ ] **Step 5.5: Write the failing closure/envelope tests**

Append to `tests/proposal_assist/test_tools_registry.py`:

```python
@pytest.mark.asyncio
async def test_propose_citation_non_integer_chunk_index_returns_envelope():
    """propose_citation with a non-integer chunk_index returns an MCP error
    envelope (is_error=True), not a raise — mirrors insert_citation's guard."""
    from call_manager.proposal_assist.tools.registry import _collect_tools

    tools = _collect_tools(_make_ctx())
    propose = next(t for t in tools if t.name == "propose_citation")
    result = await propose.handler(
        {
            "reference_id": "some-ref",
            "anchor": "somewhere",
            "chunk_index": "not-an-int",
            "evidence_quote": "some quote",
        }
    )
    assert result["is_error"] is True
    assert result["content"][0]["type"] == "text"
    text = result["content"][0]["text"]
    assert "chunk_index" in text or "integer" in text


@pytest.mark.asyncio
async def test_propose_citation_closure_returns_error_envelope_for_bad_ref():
    """A bad (non-uuid) reference_id returns the MCP error envelope, not a raise."""
    from call_manager.proposal_assist.tools.registry import _collect_tools

    tools = _collect_tools(_make_ctx())
    propose = next(t for t in tools if t.name == "propose_citation")
    result = await propose.handler(
        {
            "reference_id": "not-a-uuid",
            "anchor": "somewhere",
            "chunk_index": 0,
            "evidence_quote": "x",
        }
    )
    assert result["is_error"] is True
    assert "reference_id" in result["content"][0]["text"]
```

> NOTE: `_make_ctx()` builds a bare ctx with `repo_dir=Path("/tmp/x")` and `session_factory=None`. The bad-ref test errors at the `uuid.UUID` parse (before any session/repo use), and the non-integer-chunk test errors in the closure guard (before the impl runs) — so neither needs a real repo/session. Both reach `{"error": ...}` and `_wrap` → error envelope.

- [ ] **Step 5.6: Run it — expect FAIL**

Run: `uv run pytest "tests/proposal_assist/test_tools_registry.py::test_propose_citation_non_integer_chunk_index_returns_envelope" "tests/proposal_assist/test_tools_registry.py::test_propose_citation_closure_returns_error_envelope_for_bad_ref" -v`
Expected: FAIL — `next(... t.name == "propose_citation")` raises `StopIteration`.

- [ ] **Step 5.7: Add the `propose_citation` `@tool` closure + import + list entry**

In `src/call_manager/proposal_assist/tools/registry.py`, extend the citations import block (currently `insert_citation_impl, list_citations_impl, remove_citation_impl, rename_citekey_impl, update_citation_impl` at lines 493-499) to also import `propose_citation_impl`:

```python
    from call_manager.proposal_assist.tools.citations import (
        insert_citation_impl,
        list_citations_impl,
        propose_citation_impl,
        remove_citation_impl,
        rename_citekey_impl,
        update_citation_impl,
    )
```

Add the closure (place it right after the `list_citations` `@tool`):

```python
    @tool(
        "propose_citation",
        "Propose attaching an inline citation as a PENDING change for a human to "
        "review and accept — this does NOT insert or commit anything. Pass the "
        "`reference_id`, an `anchor` (a unique substring of the draft the marker "
        "should follow), the `chunk_index`, and a VERBATIM `evidence_quote` "
        "copied word-for-word from that chunk (search_references / "
        "read_reference_section surface the chunk_index). The quote and anchor "
        "are verified now; an unverifiable citation is rejected and never "
        "becomes a pending proposal. The citekey + evidence id are assigned when "
        "a human accepts. Call read_proposal first. Returns a proposed_change_id "
        "and status 'pending'.",
        {
            "type": "object",
            "properties": {
                "reference_id": {"type": "string"},
                "anchor": {"type": "string"},
                "chunk_index": {"type": "integer"},
                "evidence_quote": {"type": "string"},
                "stance": {
                    "type": "string",
                    "enum": ["supports", "contradicts", "background"],
                    "default": "supports",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                },
            },
            "required": ["reference_id", "anchor", "chunk_index", "evidence_quote"],
        },
    )
    async def propose_citation(args):
        try:
            chunk_index = int(args["chunk_index"])
        except (TypeError, ValueError, KeyError):
            return _mcp_error("chunk_index must be an integer")
        return await _safe_tool(
            "propose_citation",
            ctx,
            propose_citation_impl,
            ctx,
            reference_id=args["reference_id"],
            anchor=args["anchor"],
            chunk_index=chunk_index,
            evidence_quote=args["evidence_quote"],
            stance=args.get("stance", "supports"),
            confidence=args.get("confidence"),
        )
```

Add `propose_citation` to the returned list (after `list_citations`):

```python
        # read-only citation discovery
        list_citations,
        # SP2a-2 — propose-only citation
        propose_citation,
```

- [ ] **Step 5.8: Run it — expect PASS**

Run: `uv run pytest "tests/proposal_assist/test_tools_registry.py::test_propose_citation_non_integer_chunk_index_returns_envelope" "tests/proposal_assist/test_tools_registry.py::test_propose_citation_closure_returns_error_envelope_for_bad_ref" -v`
Expected: both PASS.

- [ ] **Step 5.9: Lint gate, then commit**

Run the lint gate. Then:

```bash
git add src/call_manager/proposal_assist/tools/citations.py \
  src/call_manager/proposal_assist/tools/registry.py \
  tests/proposal_assist/tools/test_propose_citation.py \
  tests/proposal_assist/test_tools_registry.py
git commit -m "feat(proposal-assist): propose_citation tool with propose-time verify (SP2a-2 Task 5)"
```

---

## Task 6: Citation accept path

Replace the `citation_insert` `NotImplementedError` (accepting.py:70) with a `kind` branch that builds a workspace-bound `ToolContext` and replays the sync `_insert_citation_locked` on the **caller's** session (it acquires the commit mutex itself — accept-time authoritative re-verify).

**Files:**
- Modify: `src/call_manager/proposal_assist/proposals/accepting.py:54-105`
- Modify: `src/call_manager/proposal_assist/tools/citations.py` (add `owns_txn: bool = True` to `_insert_citation_locked` so the accept replay flushes its citekey-cache write instead of committing — single-transaction atomicity)
- Modify: `src/call_manager/proposal_assist/web/proposals.py:66-108`
- Test: `tests/proposal_assist/proposals/test_accepting.py` (rewrite `test_accept_rejects_non_prose_kind` + add tests, incl. the citekey=None atomicity test), `tests/proposal_assist/test_web_proposed_changes.py` (endpoint e2e)

- [ ] **Step 6.1: Rewrite the failing accept test (positive citation accept)**

In `tests/proposal_assist/proposals/test_accepting.py`, **replace** `test_accept_rejects_non_prose_kind` (lines 249-274) with the positive citation-accept test below, and add a verify-fail test. First add the imports + a citation seeder at the top of the file (after the existing imports):

```python
from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.citations.markers import find_markers
from call_manager.proposal_assist.proposals.proposing import propose_citation_insert
from call_manager.proposal_assist.tools.citations import CITATIONS_FILENAME

EMBED_DIM = 1024


def _seed_reference(s, workspace_id, *, chunk_text, citekey=None):
    ref = Reference(
        workspace_id=workspace_id,
        kind="paper",
        source_type="uploaded",
        filename="seed.pdf",
        storage_path="seed/seed.pdf",
        mime_type="application/pdf",
        citekey=citekey,
        metadata_status="approved",
    )
    s.add(ref)
    s.flush()
    s.add(
        ReferenceChunk(
            reference_id=ref.id,
            workspace_id=workspace_id,
            chunk_index=0,
            section_path="Results",
            text=chunk_text,
            embedding=[0.0] * EMBED_DIM,
            embedding_model="fake-embedder",
        )
    )
    s.flush()
    return ref.id
```

Then add the positive accept + verify-fail tests (replacing the deleted `test_accept_rejects_non_prose_kind`):

```python
CHUNK_TEXT = "Adjuvant therapy reduces recurrence by 30% in the treated cohort."


def test_accept_citation_inserts_marker_and_ledger_row(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver@ki.se")
        s.flush()
        rid = _seed_reference(s, ws.id, chunk_text=CHUNK_TEXT, citekey="smith2021")
        base_commit, base_section = capture_base(repo, 1)
        # "original." is the anchor — it lives in section 1 (## Methods).
        pc = propose_citation_insert(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            reference_id=str(rid),
            anchor="original.",
            chunk_index=0,
            evidence_quote="reduces recurrence by 30%",
            stance="supports",
            confidence="high",
            origin_kind="interactive_turn",
        )
        s.flush()

        accept_proposed_change(
            session=s,
            proposed_change_id=pc.id,
            workspace_id=ws.id,
            approver=approver,
            workspaces_dir=str(tmp_path),
        )
        s.flush()

        got = s.get(ProposedChange, pc.id)
        assert got.status == "accepted"
        assert got.resolved_by == approver.id
        assert got.resulting_commit_sha
        # Marker landed in proposal.md right after the anchor.
        proposal_text = (repo / PROPOSAL_FILENAME).read_text()
        markers = find_markers(proposal_text)
        assert len(markers) == 1
        assert markers[0]["citekey"] == "smith2021"
        # Ledger row landed in citations.json.
        ledger = load_ledger((repo / CITATIONS_FILENAME).read_text())
        assert len(ledger["evidence"]) == 1
        assert ledger["evidence"][0]["evidence_quote"] == "reduces recurrence by 30%"


def test_accept_citation_verify_fail_goes_stale(pg_session_factory, tmp_path):
    # The anchor moved out of the document between propose and accept: the
    # accept-time re-verify fails, so the row flips to stale (parallels prose's
    # StaleSectionError->stale), stamped with the resolver, NO marker written.
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver@ki.se")
        s.flush()
        rid = _seed_reference(s, ws.id, chunk_text=CHUNK_TEXT, citekey="smith2021")
        base_commit, base_section = capture_base(repo, 1)
        pc = propose_citation_insert(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            reference_id=str(rid),
            anchor="this anchor no longer exists",
            chunk_index=0,
            evidence_quote="reduces recurrence by 30%",
            stance="supports",
            origin_kind="interactive_turn",
        )
        s.flush()

        accept_proposed_change(
            session=s,
            proposed_change_id=pc.id,
            workspace_id=ws.id,
            approver=approver,
            workspaces_dir=str(tmp_path),
        )
        s.flush()

        got = s.get(ProposedChange, pc.id)
        assert got.status == "stale"
        assert got.resolved_by == approver.id
        assert got.resolved_at is not None
        # No marker / ledger written.
        assert find_markers((repo / PROPOSAL_FILENAME).read_text()) == []


def test_accept_citation_atomic_when_citekey_cache_branch_runs(
    pg_session_factory, tmp_path
):
    # BLOCKER-1 regression: when the Reference has NO citekey yet, the replay's
    # citekey-cache branch (ref.citekey != minted_citekey) RUNS. With owns_txn=False
    # it must FLUSH (not commit) so the FOR UPDATE pc-row lock + advisory mutex stay
    # held through the status flip — git/ledger write + status flip + citekey update
    # all land in ONE transaction. (Seeding citekey=None is what exercises this
    # branch; the citekey="smith2021" tests above skip it and would mask the bug.)
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver@ki.se")
        s.flush()
        rid = _seed_reference(s, ws.id, chunk_text=CHUNK_TEXT, citekey=None)
        base_commit, base_section = capture_base(repo, 1)
        pc = propose_citation_insert(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            reference_id=str(rid),
            anchor="original.",
            chunk_index=0,
            evidence_quote="reduces recurrence by 30%",
            stance="supports",
            confidence="high",
            origin_kind="interactive_turn",
        )
        s.flush()

        accept_proposed_change(
            session=s,
            proposed_change_id=pc.id,
            workspace_id=ws.id,
            approver=approver,
            workspaces_dir=str(tmp_path),
        )
        # Single commit at the END persists everything atomically (the replay only
        # flushed). Everything must be consistent post-commit.
        s.commit()

        got = s.get(ProposedChange, pc.id)
        assert got.status == "accepted"
        assert got.resolved_by == approver.id
        assert got.resulting_commit_sha
        # Marker landed in proposal.md.
        proposal_text = (repo / PROPOSAL_FILENAME).read_text()
        markers = find_markers(proposal_text)
        assert len(markers) == 1
        minted = markers[0]["citekey"]
        # Ledger row landed in citations.json.
        ledger = load_ledger((repo / CITATIONS_FILENAME).read_text())
        assert len(ledger["evidence"]) == 1
        # The citekey-cache branch ran and updated the Reference in the SAME txn.
        got_ref = s.get(Reference, rid)
        assert got_ref.citekey == minted
```

> `_setup` initializes the repo with `"preamble\n\n## Methods\noriginal.\n"`, so the anchor `"original."` is unique and lives in section 1.

- [ ] **Step 6.2: Run it — expect FAIL**

Run: `uv run pytest "tests/proposal_assist/proposals/test_accepting.py::test_accept_citation_inserts_marker_and_ledger_row" "tests/proposal_assist/proposals/test_accepting.py::test_accept_citation_verify_fail_goes_stale" "tests/proposal_assist/proposals/test_accepting.py::test_accept_citation_atomic_when_citekey_cache_branch_runs" -v`
Expected: FAIL — all three raise `NotImplementedError("citation_insert accept is SP2a-2")`.

- [ ] **Step 6.3: Implement the citation accept branch**

**First, make `_insert_citation_locked` caller-transaction-aware.** Today its
citekey-cache branch (`citations.py:~563`, inside `if ref.citekey != citekey:`)
does `session.commit()`. On the accept path, replaying it on the accept's request
session, that mid-replay commit would PREMATURELY release the SP2a-1 `_load_scoped`
`FOR UPDATE` pc-row lock AND the `pg_advisory_xact_lock` commit mutex while
`pc.status` is still `"pending"` — splitting the git/ledger write from the status
flip across two transactions and breaking serialization. We must keep the FOR
UPDATE lock + advisory mutex held through the status flip so the endpoint's single
`session.commit()` persists everything atomically. Add an `owns_txn` keyword:

In `src/call_manager/proposal_assist/tools/citations.py`, change the
`_insert_citation_locked` signature (line 389) to add the keyword:

```python
def _insert_citation_locked(
    ctx: ToolContext,
    session: Session,
    *,
    ref_uuid: uuid.UUID,
    anchor: str,
    chunk_index: int,
    evidence_quote: str,
    stance: str,
    confidence: str | None,
    owns_txn: bool = True,
) -> dict[str, Any]:
```

Then change the citekey-cache branch (lines 563-576) so it flushes (not commits)
and never rolls back the caller's txn when `owns_txn=False`:

```python
    # 14. DB cache (AFTER git) — best-effort; git ledger is canonical.
    if ref.citekey != citekey:
        try:
            ref.citekey = citekey
            # owns_txn=True (default, the async insert_citation_impl path): this
            # function owns the transaction, so commit the cache write here.
            # owns_txn=False (the accept replay): the CALLER owns the txn and must
            # keep the FOR UPDATE pc-row lock + advisory mutex held through the
            # status flip — flush only; the endpoint's single commit persists it.
            if owns_txn:
                session.commit()
            else:
                session.flush()
        except Exception:  # noqa: BLE001 — cache write must never fail the tool
            # Only roll back when we own the txn; rolling back the caller's accept
            # txn would discard the git/ledger write + status flip. Best-effort: log.
            if owns_txn:
                session.rollback()
            logger.warning(
                "insert_citation: references.citekey cache update failed for "
                "workspace %s ref %s citekey %s — reconciliation will backfill",
                ctx.workspace_id,
                ref_uuid,
                citekey,
                exc_info=True,
            )
```

> The default `owns_txn=True` leaves the existing async `insert_citation_impl`
> path (citations.py) UNCHANGED — it still commits the cache write itself. Only the
> accept replay passes `owns_txn=False`.

Now the accept branch. In `src/call_manager/proposal_assist/proposals/accepting.py`, add the imports near the top (after the existing imports, lines 14-15):

```python
from call_manager.proposal_assist.drafting.locking import BUSY_ERROR_MESSAGE
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.tools.citations import _insert_citation_locked
from call_manager.proposal_assist.tools.registry import ToolContext
```

> NIT E: `_insert_citation_locked` catches `CommitMutexTimeout` internally and
> returns `{"error": _BUSY_ERROR}` (citations.py:404-406), so the busy path is the
> `error == BUSY_ERROR_MESSAGE` branch below — NOT an exception we catch here. Do
> NOT import `CommitMutexTimeout` (it would be an unused F401). This single block
> is the final import form.

Replace the body of `accept_proposed_change` from line 69 onward. The current code is:

```python
    if pc.kind != "prose_edit":
        raise NotImplementedError("citation_insert accept is SP2a-2")

    # ws cannot be None here: ...
    ws = session.get(Workspace, pc.workspace_id)
    try:
        result = save_section(
            ...
        )
    except StaleSectionError:
        ...
        return pc

    ...
    pc.status = "accepted"
    pc.resolved_by = approver.id
    pc.resolved_at = datetime.now(UTC)
    pc.resulting_commit_sha = result.commit_sha
    session.flush()
    return pc
```

Replace the `if pc.kind != "prose_edit":` guard + the prose body with a `kind` branch. The new code (from line 69 onward) becomes:

```python
    if pc.kind == "prose_edit":
        # ws cannot be None here: workspace_id is a NOT NULL FK (ON DELETE CASCADE)
        # and _load_scoped just proved a row with this workspace_id exists.
        ws = session.get(Workspace, pc.workspace_id)
        try:
            result = save_section(
                session=session,
                workspace=ws,
                user_id=approver.id,
                section_index=pc.section_index,
                new_text=pc.payload["new_text"],
                base_commit_sha=pc.base_commit_sha,
                base_section_sha=pc.base_section_sha,
                workspaces_dir=workspaces_dir,
            )
        except StaleSectionError:
            # A human edit landed between the proposer's read and this accept.
            # Stamp the resolver so a stale-on-accept-attempt is dated and
            # distinguishable from a passive refresh_staleness sweep.
            pc.status = "stale"
            pc.resolved_by = approver.id
            pc.resolved_at = datetime.now(UTC)
            session.flush()
            return pc

        # A no-op proposal makes save_section short-circuit (changed=False),
        # returning the current HEAD — still legitimately "accepted".
        pc.status = "accepted"
        pc.resolved_by = approver.id
        pc.resolved_at = datetime.now(UTC)
        pc.resulting_commit_sha = result.commit_sha
        session.flush()
        return pc

    if pc.kind == "citation_insert":
        return _accept_citation_insert(
            session=session,
            pc=pc,
            approver=approver,
            workspaces_dir=workspaces_dir,
        )

    raise NotImplementedError(f"accept of kind {pc.kind!r} is not supported")
```

Then add the helper + a sentinel exception below `accept_proposed_change` (before `reject_proposed_change`):

```python
class CitationAcceptBusy(Exception):
    """The commit mutex was held during a citation accept replay — retryable.
    Distinct from a terminal verification failure (which flips the row stale)."""


def _accept_citation_insert(
    *,
    session: Session,
    pc: ProposedChange,
    approver: User,
    workspaces_dir: Path | str,
) -> ProposedChange:
    """Replay the citation insert as the named approver, under the commit mutex.

    Uses the SYNC, session-owned _insert_citation_locked on the CALLER's session
    (it acquires the commit mutex itself — accept-time authoritative re-verify).
    Passed owns_txn=False so the replay flushes (never commits) its citekey-cache
    write: the caller's FOR UPDATE pc-row lock + advisory commit mutex stay held
    through the status flip, and the endpoint's single session.commit persists the
    git/ledger write AND the status flip in ONE transaction. (Atomicity is NOT
    automatic — owns_txn=False is what makes it single-transaction.)
    Result mapping (decision 4):
      - success / idempotent -> accepted, resulting_commit_sha = result["commit_sha"]
      - verification failure (anchor/quote moved, chunk re-chunked, tombstoned)
        -> stale, resolver stamped (parallels prose StaleSectionError->stale)
      - busy (_BUSY_ERROR) -> CitationAcceptBusy raised; endpoint maps to 409,
        status UNCHANGED (retryable, not terminal).
    """
    repo_dir = workspace_repo_dir(workspaces_dir, pc.workspace_id)
    ws = session.get(Workspace, pc.workspace_id)
    settings = getattr(ws, "settings", None)  # unused on this path; ctx carries it nominally
    ctx = ToolContext(
        workspace_id=pc.workspace_id,
        repo_dir=repo_dir,
        session_factory=None,  # unused: _insert_citation_locked uses the passed session
        settings=settings,  # type: ignore[arg-type]
    )
    payload = pc.payload
    # follow-up: the citation git commit message ("cite: add {citekey} [{ev_id}]")
    # carries NO approver — write_and_commit_files takes no author param. We record
    # the approver on pc.resolved_by only; git-commit-author parity with prose's
    # "user edit: ... (user:{id})" is deferred (would need an author param on
    # _insert_citation_locked / write_and_commit_files).
    result = _insert_citation_locked(
        ctx,
        session,
        ref_uuid=uuid.UUID(str(payload["reference_id"])),
        anchor=payload["anchor"],
        chunk_index=payload["chunk_index"],
        evidence_quote=payload["evidence_quote"],
        stance=payload.get("stance", "supports"),
        confidence=payload.get("confidence"),
        # owns_txn=False: keep the caller's FOR UPDATE pc-row lock + advisory commit
        # mutex held through the status flip so the endpoint's single session.commit
        # persists the git/ledger write AND the status flip in ONE transaction.
        # Without this, the citekey-cache branch's commit() (when ref.citekey is
        # being set for the first time) would split the accept across two txns.
        owns_txn=False,
    )

    if "error" in result:
        if result["error"] == BUSY_ERROR_MESSAGE:
            # Retryable: leave status pending; the endpoint surfaces 409.
            raise CitationAcceptBusy(BUSY_ERROR_MESSAGE)
        # Terminal verification failure -> stale, stamped (mirrors prose).
        pc.status = "stale"
        pc.resolved_by = approver.id
        pc.resolved_at = datetime.now(UTC)
        session.flush()
        return pc

    pc.status = "accepted"
    pc.resolved_by = approver.id
    pc.resolved_at = datetime.now(UTC)
    # NB dict key result["commit_sha"], unlike prose's result.commit_sha attribute.
    pc.resulting_commit_sha = result["commit_sha"]
    session.flush()
    return pc
```

> Busy path: the `result["error"] == BUSY_ERROR_MESSAGE` branch above is the only
> busy handling needed — `_insert_citation_locked` catches `CommitMutexTimeout`
> internally and returns `{"error": _BUSY_ERROR}` (citations.py:404-406), so there
> is no exception to catch here and `CommitMutexTimeout` is NOT imported (see the
> single final import block at the top of this step).

> Import-cycle check: `accepting.py` already imports `editing.save`. Adding `tools.citations` + `tools.registry` imports at the top of `accepting.py` is acyclic (neither tool module imports `proposals.accepting`). `_insert_citation_locked` is a private function but is the documented accept-replay target (map §5) — importing it here is intentional.

Remove the now-unused `settings = getattr(...)` line if ruff flags it (it documents that the ctx's `settings` is nominal on this path; `_insert_citation_locked` reads proposal/ledger off disk and uses only `ctx.repo_dir` + `ctx.workspace_id`). Cleaner: pass `settings=None`:

```python
    ctx = ToolContext(
        workspace_id=pc.workspace_id,
        repo_dir=repo_dir,
        session_factory=None,  # unused on this path
        settings=None,  # type: ignore[arg-type]  — _insert_citation_locked reads off disk
    )
```

(Drop the `ws = session.get(...)` + `settings = getattr(...)` lines entirely — they aren't needed; `_insert_citation_locked` doesn't read `ctx.settings`.)

- [ ] **Step 6.4: Run it — expect PASS; keep the spec-target test green**

Run: `uv run pytest "tests/proposal_assist/proposals/test_accepting.py::test_accept_citation_inserts_marker_and_ledger_row" "tests/proposal_assist/proposals/test_accepting.py::test_accept_citation_verify_fail_goes_stale" "tests/proposal_assist/proposals/test_accepting.py::test_accept_citation_atomic_when_citekey_cache_branch_runs" "tests/proposal_assist/proposals/test_accepting.py::test_accept_rejects_spec_target" -v`
Expected: all 4 PASS (`test_accept_rejects_spec_target` still raises `NotImplementedError("spec target is SP2b")` — the `target_artifact != "draft"` guard at accepting.py:67-68 is untouched and runs before the kind branch).

- [ ] **Step 6.5: Map the busy error at the endpoint**

In `src/call_manager/proposal_assist/web/proposals.py`, import `CitationAcceptBusy` (extend the accepting import, lines 16-21):

```python
from call_manager.proposal_assist.proposals.accepting import (
    CitationAcceptBusy,
    ProposedChangeNotFound,
    ProposedChangeNotPending,
    accept_proposed_change,
    reject_proposed_change,
)
```

In the `accept` route's `try/except` (lines 79-106), add a handler for `CitationAcceptBusy` (place it alongside the `CommitMutexTimeout` 503 handler, but as a 409 retryable):

```python
    except CitationAcceptBusy as exc:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="workspace busy; retry the accept"
        ) from exc
```

Also update the stale comment at lines 76-78 (it says citation accept is unreachable in SP2a):

```python
    # accept_proposed_change replays prose via save_section and citation_insert
    # via _insert_citation_locked. It raises NotImplementedError only for a spec
    # target (SP2b). A citation accept that can't re-verify flips the row to
    # "stale" (returned 200); a busy mutex raises CitationAcceptBusy -> 409.
```

- [ ] **Step 6.6: Write the failing endpoint e2e test**

Append to `tests/proposal_assist/test_web_proposed_changes.py` (the `_init_repo` + helpers are already in that file). Add the imports + seeder at the top (after the existing imports):

```python
from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.citations.markers import find_markers
from call_manager.proposal_assist.proposals.proposing import propose_citation_insert
from call_manager.proposal_assist.tools.citations import CITATIONS_FILENAME

_EMBED_DIM = 1024
_CHUNK_TEXT = "Adjuvant therapy reduces recurrence by 30% in the treated cohort."


def _seed_citation_pending(s, ws, tmp_path):
    repo = Path(tmp_path) / str(ws.id) / "repo"
    _init_repo(repo, "preamble\n\n## Methods\norig.\n")
    ref = Reference(
        workspace_id=ws.id,
        kind="paper",
        source_type="uploaded",
        filename="seed.pdf",
        storage_path="seed/seed.pdf",
        mime_type="application/pdf",
        citekey="smith2021",
        metadata_status="approved",
    )
    s.add(ref)
    s.flush()
    s.add(
        ReferenceChunk(
            reference_id=ref.id,
            workspace_id=ws.id,
            chunk_index=0,
            section_path="Results",
            text=_CHUNK_TEXT,
            embedding=[0.0] * _EMBED_DIM,
            embedding_model="fake-embedder",
        )
    )
    s.flush()
    base_commit, base_section = capture_base(repo, 1)
    pc = propose_citation_insert(
        session=s,
        workspace_id=ws.id,
        section_index=1,
        base_commit_sha=base_commit,
        base_section_sha=base_section,
        reference_id=str(ref.id),
        anchor="orig.",
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
        stance="supports",
        origin_kind="interactive_turn",
    )
    return pc
```

Then the test:

```python
def test_accept_citation_endpoint_applies_marker_and_ledger(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        pc = _seed_citation_pending(s, ws, tmp_path)
        s.commit()
        pc_id, ws_id = str(pc.id), str(ws.id)

    resp = client_authed_owner.post(
        f"/workspaces/{ws_id}/proposed-changes/{pc_id}/accept"
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"

    repo = Path(tmp_path) / ws_id / "repo"
    proposal_text = (repo / PROPOSAL_FILENAME).read_text()
    markers = find_markers(proposal_text)
    assert len(markers) == 1
    assert markers[0]["citekey"] == "smith2021"
    ledger = load_ledger((repo / CITATIONS_FILENAME).read_text())
    assert len(ledger["evidence"]) == 1
```

- [ ] **Step 6.7: Run it — expect PASS**

Run: `uv run pytest "tests/proposal_assist/test_web_proposed_changes.py::test_accept_citation_endpoint_applies_marker_and_ledger" -v`
Expected: PASS. Then re-run the existing endpoint tests to confirm no regression: `uv run pytest tests/proposal_assist/test_web_proposed_changes.py::test_accept_endpoint_applies_change tests/proposal_assist/test_web_proposed_changes.py::test_accept_already_resolved_returns_409 -v` → PASS. (Re-run once on `OperationalError` flake.)

- [ ] **Step 6.8: Lint gate, then commit**

Run the lint gate. Then:

```bash
git add src/call_manager/proposal_assist/proposals/accepting.py \
  src/call_manager/proposal_assist/tools/citations.py \
  src/call_manager/proposal_assist/web/proposals.py \
  tests/proposal_assist/proposals/test_accepting.py \
  tests/proposal_assist/test_web_proposed_changes.py
git commit -m "feat(proposal-assist): citation accept replays _insert_citation_locked as approver (SP2a-2 Task 6)"
```

---

## Task 7: Allowlist swap + permission_mode + delete mutating tools (LAST)

Make the structural cut: remove the 6 mutating tool names from the SDK allowlist, add the 2 propose tools, change `permission_mode` to `"default"`, delete the two mutating *proposal* impls, rewrite the citation instruction + security-boundary comment, and update every lockstep test. Includes the read-only-allowlist audit (decision 1).

**Files:**
- Modify: `src/call_manager/proposal_assist/drafting/claude_adapter.py:113-131,192-258` (instruction + comment + allowlist + permission_mode)
- Modify: `src/call_manager/proposal_assist/tools/registry.py` (remove 6 `@tool`s + imports + list entries)
- Modify: `src/call_manager/proposal_assist/tools/proposal.py` (delete `edit_proposal_impl` + `replace_proposal_impl` + their helpers)
- Delete: `tests/proposal_assist/test_tools_proposal.py`, `tests/test_tools_head_precondition.py` (test only the deleted impls' precondition behavior)
- Modify: `tests/proposal_assist/test_tools_registry.py` (19-name set), `tests/proposal_assist/test_tools_claude_adapter_wiring.py` (19-name set + permission_mode), `tests/proposal_assist/test_drafting_claude_adapter.py` (instruction substrings)

> ORDER WITHIN THIS TASK: do the *test* edits first (they define the target), then the source edits, then run to green. The lockstep tests are currently failing (they assert the pre-SP2a-2 23-name / "bypassPermissions" state, plus the propose tools added in Tasks 4/5 made the count 25). This task lands them all at the final 19-name / "default" state.

- [ ] **Step 7.1: Update `test_collect_tools_returns_twenty_named_tools` to the 19-name set**

In `tests/proposal_assist/test_tools_registry.py`, replace the body of `test_collect_tools_returns_twenty_named_tools` (lines 54-96). Rename it for clarity and set the final 19-name expectation:

```python
def test_collect_tools_returns_nineteen_named_tools():
    """The post-SP2a-2 tool surface: the 6 mutating tools (edit_proposal,
    replace_proposal, insert_citation, remove_citation, update_citation,
    rename_citekey) are removed; propose_edit + propose_citation are added.
    23 - 6 + 2 = 19."""
    from call_manager.proposal_assist.tools.registry import _collect_tools

    tools = _collect_tools(_make_ctx())
    names = {t.name for t in tools}
    assert names == {
        # read-only artifact + context (B5)
        "read_proposal",
        "get_artifact_history",
        "read_artifact_at",
        "diff_artifact",
        "get_call_details",
        "get_profile",
        # reference tools (Slice 2)
        "list_references",
        "search_references",
        "navigate_reference",
        "read_reference",
        "read_reference_section",
        "summarize_data",
        # agent-side fetch (Slice 3)
        "fetch_reference",
        # read-only citation discovery + audit
        "list_citations",
        "validate_citations",
        "list_unsupported_claims",
        "build_bibliography",
        # SP2a-2 — propose-only mutation
        "propose_edit",
        "propose_citation",
    }
    assert len(tools) == 19
```

- [ ] **Step 7.2: Delete the now-obsolete insert_citation closure tests in test_tools_registry.py**

In `tests/proposal_assist/test_tools_registry.py`, **delete** these two tests (they exercise the removed `insert_citation` tool — the `propose_citation` equivalents added in Task 5 cover the same envelope behavior):
- `test_citation_tool_closure_returns_error_envelope_for_bad_ref` (lines 180-199)
- `test_insert_citation_non_integer_chunk_index_returns_envelope` (lines 202-223)

(Keep `test_list_citations_closure_returns_envelope` — `list_citations` stays.)

- [ ] **Step 7.3: Update the wiring test's expected_tools set + permission_mode**

In `tests/proposal_assist/test_tools_claude_adapter_wiring.py`, replace the `expected_tools` set (lines 98-130) with the 19-name set (same as Step 7.1's set), and update the count comment. Then change `test_options_strip_builtin_tools_even_without_tools_factory`'s final assertion (line 162):

```python
    # Default permission mode is now "default" (the bypassPermissions footgun is
    # gone). tools=[]/strict_mcp_config still strip the built-ins unconditionally.
    assert kwargs["permission_mode"] == "default"
```

- [ ] **Step 7.4: Update the citation-instruction substring test**

In `tests/proposal_assist/test_drafting_claude_adapter.py`, update `test_build_options_appends_citation_instruction` (lines 466-489). The instruction now names the propose tools, not the mutating ones:

```python
def test_build_options_appends_citation_instruction():
    """The system-prompt append includes the citation instruction naming the
    propose tools (propose_edit / propose_citation) and the read-only audit
    tools, and tells the agent to copy a verbatim evidence_quote."""
    from call_manager.proposal_assist.drafting import claude_adapter as ca

    adapter = ClaudeAgentSDKAdapter(model="claude-haiku-4-5", workspaces_dir="/tmp/x")
    kwargs = adapter._build_options_kwargs(LaunchContext(workspace_id=uuid.uuid4()))
    sp = kwargs["system_prompt"]
    append = sp["append"]
    assert "propose_citation" in append
    assert "propose_edit" in append
    assert "validate_citations" in append
    assert "build_bibliography" in append
    assert "evidence_quote" in append or "verbatim" in append
    assert "reference_id" in append
    assert "anchor" in append
    # No mutating tool names survive in the instruction.
    assert "insert_citation" not in append
    assert "edit_proposal" not in append
    assert ca._CITATION_INSTRUCTION
    assert ca._CITATION_INSTRUCTION != ca._REFERENCE_SAFETY_INSTRUCTION
    assert ca._REFERENCE_SAFETY_INSTRUCTION in append
    assert ca._CITATION_INSTRUCTION in append
```

- [ ] **Step 7.5: Run the lockstep tests — expect FAIL (they pin the new target)**

Run: `uv run pytest tests/proposal_assist/test_tools_registry.py::test_collect_tools_returns_nineteen_named_tools "tests/proposal_assist/test_tools_claude_adapter_wiring.py::test_options_strip_builtin_tools_even_without_tools_factory" tests/proposal_assist/test_drafting_claude_adapter.py::test_build_options_appends_citation_instruction -v`
Expected: FAIL (source still has 25 tools after Tasks 4/5, still `"bypassPermissions"`, instruction still names `insert_citation`).

- [ ] **Step 7.6: Remove the 6 mutating tools from the registry**

In `src/call_manager/proposal_assist/tools/registry.py`:

1. In the proposal import block (Step 4.7 left it with 4 names), remove `edit_proposal_impl` + `replace_proposal_impl` so it reads:

```python
    from call_manager.proposal_assist.tools.proposal import (
        propose_edit_impl,
        read_proposal_impl,
    )
```

2. Delete the `edit_proposal` `@tool` closure (lines 172-207) and the `replace_proposal` `@tool` closure (lines 209-232).

3. In the citations import block, remove `insert_citation_impl`, `remove_citation_impl`, `rename_citekey_impl`, `update_citation_impl` (keep `list_citations_impl` + `propose_citation_impl`):

```python
    from call_manager.proposal_assist.tools.citations import (
        list_citations_impl,
        propose_citation_impl,
    )
```

4. Delete the `insert_citation` `@tool` closure (lines 576-622), `remove_citation` (624-644), `update_citation` (646-692), `rename_citekey` (694-718).

5. In the returned list (lines 720-752), delete the entries `edit_proposal`, `replace_proposal`, `insert_citation`, `remove_citation`, `update_citation`, `rename_citekey`. The final list (with `propose_edit` after `read_proposal` from Task 4 and `propose_citation` after `list_citations` from Task 5) is:

```python
    return [
        read_proposal,
        # SP2a-2 — propose-only mutation
        propose_edit,
        get_artifact_history,
        read_artifact_at,
        diff_artifact,
        get_call_details,
        get_profile,
        # Slice 2 — references
        list_references,
        search_references,
        navigate_reference,
        read_reference,
        read_reference_section,
        summarize_data,
        # Slice 3 — agent-side fetch
        fetch_reference,
        # read-only citation discovery
        list_citations,
        # SP2a-2 — propose-only citation
        propose_citation,
        # Slice 3 — citation audit
        validate_citations,
        # Slice 3 — uncited-claim discovery
        list_unsupported_claims,
        # Slice 4 — bibliography rendering
        build_bibliography,
    ]
```

- [ ] **Step 7.7: Delete the two mutating proposal impls**

In `src/call_manager/proposal_assist/tools/proposal.py`, delete `edit_proposal_impl` (lines 83-145), `replace_proposal_impl` (lines 148-177), and their now-orphaned helpers `_commit_message` (lines 180-184) and `_write_and_commit` (lines 187-324). Also remove now-unused imports that only those functions used: `os`, `subprocess`, `ArtifactVersion`, `GitCommitError`/`write_and_commit_*` from gitio (keep `current_head` — `read_proposal_impl` uses it), `acquire_commit_mutex` / `CommitMutexTimeout` / `BUSY_ERROR_MESSAGE` from locking, and `_STALE_ERROR` / `_BUSY_ERROR` module constants if unused.

After the deletion, `proposal.py` should retain: the module docstring (trim the edit/replace-specific paragraphs), `PROPOSAL_FILENAME`, `_NOT_READ_ERROR` (used by `propose_edit_impl`), `read_proposal_impl`, and `propose_edit_impl`. Run ruff to surface every unused import/name precisely:

```bash
uv run ruff check src/call_manager/proposal_assist/tools/proposal.py
```

Fix each `F401` (unused import) / `F811` it reports until clean. Keep only: `from __future__ import annotations`, `from typing import Any`, `from call_manager.proposal_assist.citations.gitio import GitCommitError, current_head` (read_proposal_impl uses both), `from call_manager.proposal_assist.editing.sectionizer import split_sections` (Task 2), `from call_manager.proposal_assist.tools.registry import ToolContext`, and `logging`.

- [ ] **Step 7.8: Delete the obsolete impl tests**

```bash
git rm tests/proposal_assist/test_tools_proposal.py tests/test_tools_head_precondition.py
```

(Both test only `edit_proposal_impl` / `replace_proposal_impl` precondition behavior — now deleted. `test_tools_head_precondition.py` also imports `insert_citation_impl` to seed, but its assertions are entirely about the edit/replace HEAD precondition; it has no coverage that survives the impl deletion.)

- [ ] **Step 7.9: Swap the allowlist + permission_mode + rewrite the instruction & comment**

In `src/call_manager/proposal_assist/drafting/claude_adapter.py`:

1. Change the `permission_mode` default in the constructor signature (line 145) — already done in Task 1 Step 1.10 (`permission_mode: str = "default"`). Confirm it reads `"default"`.

2. Rewrite the allowlist (lines 227-258): remove `edit_proposal`, `replace_proposal`, `insert_citation`, `remove_citation`, `update_citation`, `rename_citekey`; add `propose_edit`, `propose_citation`. Final form:

```python
            options_kwargs["allowed_tools"] = [
                "read_proposal",
                # SP2a-2 — propose-only mutation (no direct repo writes)
                "propose_edit",
                "propose_citation",
                "get_artifact_history",
                "read_artifact_at",
                "diff_artifact",
                "get_call_details",
                "get_profile",
                # Slice 2 — references
                "list_references",
                "search_references",
                "navigate_reference",
                "read_reference",
                "read_reference_section",
                "summarize_data",
                # Slice 3 — agent-side fetch
                "fetch_reference",
                # read-only citation discovery + audit
                "list_citations",
                "validate_citations",
                "list_unsupported_claims",
                # bibliography rendering (writes bibliography.md — kept: not a
                # draft/spec section mutation; spec removes only prose+citation)
                "build_bibliography",
            ]
```

3. Rewrite `_CITATION_INSTRUCTION` (lines 113-131) so it names the propose tools and the propose→accept flow:

```python
_CITATION_INSTRUCTION = (
    "You do not edit or commit the proposal directly — you PROPOSE changes that "
    "a human reviews and accepts. To rewrite a section, call read_proposal "
    "first, then propose_edit with the section_index and the whole new section "
    "text (including its ## heading). When a sentence makes a factual or "
    "empirical claim that a provided reference supports, propose a citation: use "
    "search_references or read_reference_section to locate the supporting chunk, "
    "copy a verbatim span from that chunk as the evidence_quote (it must appear "
    "word-for-word in the chunk), then call propose_citation with the "
    "reference_id, the anchor (a unique snippet of the draft the citation should "
    "follow), the chunk_index, and the evidence_quote. The citekey is assigned "
    "when a human accepts. Each propose_* call returns a proposed_change_id with "
    "status 'pending' — it is queued for review, not applied. Use list_citations "
    "to see existing citations, validate_citations to check them, and "
    "list_unsupported_claims to find factual claims that still lack a citation. "
    "When the draft's citations are settled, call build_bibliography to render "
    "the formatted reference list. Read the proposal before proposing edits to "
    "it."
)
```

4. Rewrite the security-boundary comment (lines 192-208) — its "bypassPermissions defaulting" premise is now false:

```python
        # CRITICAL security boundary (Codex B2): allowed_tools alone does NOT
        # disable Claude Code's built-in toolbox. The disable is `tools=[]`
        # PLUS `strict_mcp_config=True`. These MUST be set unconditionally — an
        # adapter constructed without a tools_factory would otherwise expose the
        # FULL built-in bash/file toolbox in the workspace repo. Always strip the
        # built-ins; only wire the MCP server + its allowlist when a factory is
        # present. SP2a-2 also drops permission_mode="bypassPermissions" (now
        # "default") and de-allowlists every direct-mutation tool: the agent can
        # only PROPOSE (write proposed_change rows), never commit to the repo —
        # a structural guarantee, not a behavioral one.
        options_kwargs["tools"] = []
        options_kwargs["strict_mcp_config"] = True
        # Don't load filesystem settings (project/user ~/.claude/settings.json).
        # Same security boundary as tools=[]: those files can carry hooks,
        # permission grants, and MCP server defs that would broaden what the
        # agent can do. The cwd is inside the app tree, so the CLI would
        # otherwise discover the project's .claude/settings.json. Pass [] for
        # SDK isolation mode.
        options_kwargs["setting_sources"] = []
```

- [ ] **Step 7.10: Read-only allowlist audit (decision 1)**

With `permission_mode="default"` (non-bypass), the SDK blocks any tool NOT on `allowed_tools`. Confirm every read-only/kept tool the agent needs is on the list. The authoritative cross-check: the registry's returned tool-list names (Step 7.6) MUST be a subset of `allowed_tools` (Step 7.9). Run this audit assertion as a one-shot test — append to `tests/proposal_assist/test_tools_claude_adapter_wiring.py`:

```python
def test_every_collected_tool_is_allowlisted(_settings_env):
    """permission_mode='default' blocks any non-allowlisted tool. Every tool the
    registry collects must therefore be on the adapter's allowed_tools, or a
    background turn would hang on a blocked call with no UI to approve."""
    import uuid as _uuid

    from call_manager.proposal_assist.drafting.claude_adapter import (
        ClaudeAgentSDKAdapter,
    )
    from call_manager.proposal_assist.drafting.contract import LaunchContext
    from call_manager.proposal_assist.tools.registry import (
        ToolContext,
        _collect_tools,
        build_proposal_assist_mcp_server,
    )

    wsid = _uuid.uuid4()
    ctx = ToolContext(
        workspace_id=wsid,
        repo_dir=__import__("pathlib").Path("/tmp/x"),
        session_factory=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )
    collected = {t.name for t in _collect_tools(ctx)}

    adapter = ClaudeAgentSDKAdapter(
        model="x",
        workspaces_dir="/tmp",
        tools_factory=lambda repo_dir, workspace_id: build_proposal_assist_mcp_server(ctx),
    )
    kwargs = adapter._build_options_kwargs(LaunchContext(workspace_id=wsid))
    allowed = set(kwargs["allowed_tools"])

    missing = collected - allowed
    assert missing == set(), f"collected tools not on the allowlist: {missing}"
    # And the allowlist names exactly the 17 read-only/kept tools + 2 propose
    # tools — no stale mutating names remain.
    assert allowed == collected
    assert len(allowed) == 19
```

- [ ] **Step 7.11: Run the full Task-7 test set — expect PASS**

Run:
```bash
uv run pytest \
  tests/proposal_assist/test_tools_registry.py \
  tests/proposal_assist/test_tools_claude_adapter_wiring.py \
  tests/proposal_assist/test_drafting_claude_adapter.py \
  tests/proposal_assist/tools/test_propose_edit.py \
  tests/proposal_assist/tools/test_propose_citation.py \
  -v
```
Expected: all PASS (19-name set, `permission_mode == "default"`, instruction names propose tools, audit subset holds). Re-run any single `OperationalError` flake once.

- [ ] **Step 7.12: Confirm no dangling imports of the deleted impls**

Run:
```bash
grep -rn "edit_proposal_impl\|replace_proposal_impl" src/ tests/
```
Expected: NO matches in `src/`; NO matches in `tests/` (the two test files were `git rm`'d). If any test still imports them, that test was missed — delete or fix it. Also confirm the citation impls still resolve (they stay):
```bash
uv run python -c "from call_manager.proposal_assist.tools.citations import insert_citation_impl, _insert_citation_locked; print('ok')"
```
Expected: `ok`.

- [ ] **Step 7.13: Lint gate, then commit**

Run the lint gate. Then:

```bash
git add src/call_manager/proposal_assist/drafting/claude_adapter.py \
  src/call_manager/proposal_assist/tools/registry.py \
  src/call_manager/proposal_assist/tools/proposal.py \
  tests/proposal_assist/test_tools_registry.py \
  tests/proposal_assist/test_tools_claude_adapter_wiring.py \
  tests/proposal_assist/test_drafting_claude_adapter.py
git commit -m "feat(proposal-assist): de-allowlist mutating tools + permission_mode=default (SP2a-2 Task 7)"
```

---

## Self-Review

**1. Spec coverage (design spec §4.1 / §5 / §6 + controller decisions):**

| Spec / decision requirement | Task |
|---|---|
| Agent tools = `propose_edit` + `propose_citation` (§4.1) | Tasks 4, 5 |
| Mutating tools removed from allowlist (§4.1) | Task 7 |
| `bypassPermissions` dropped → `"default"` (§4.1, decision 1) | Tasks 1 (default), 7 (swap + audit) |
| Read-only surface untouched + audited on allowlist (decision 1) | Task 7 (Step 7.10 audit) |
| `proposed_change` citation payload = ref/chunk/quote/anchor/stance/confidence (§5) | Task 3 |
| `triggering_prompt` = raw typed text (decision 6) | Task 1 (`_extract_triggering_prompt`) |
| `triggering_turn_id` nullable, set for interactive_turn (§5, decision) | Tasks 1, 3, 4, 5 |
| Provenance server-trusted, not model args (§6) | Task 1 |
| Accept replays `insert_citation_impl`/`_insert_citation_locked` as approver (§5) | Task 6 |
| Citations verified at propose-time AND accept-time (§5) | Tasks 5 (propose), 6 (accept) |
| `base_commit_sha`/`base_section_sha` for citation = anchor's section (decision 2) | Task 5 |
| Accept verify-fail → stale; busy → 409; idempotent → accepted (decision 4) | Task 6 |
| `resolved_by` = approver; git-author parity deferred (decision 3) | Task 6 (+ follow-up note) |
| Keep `build_bibliography` + `fetch_reference` (decision 5) | Task 7 (allowlist) |
| `propose_remove/update/rename` deferred (decision 9) | Out of scope (stated) |
| Delete `edit_proposal_impl`/`replace_proposal_impl` after grep (decision 8) | Task 7 (Steps 7.7, 7.12) |
| `agent_view_sections` populated by `read_proposal` (decision 7) | Task 2 |
| No rollout flag — clean cut (decision 10) | (no flag added anywhere) |
| Turn-freeze deletion (§6) | NOT a task — already done in SP2a-1 (stated up front) |
| Spec target stays `NotImplementedError` (SP2b) | Task 6 (`test_accept_rejects_spec_target` kept) |

No gaps.

**2. Placeholder scan:** No `TBD`/`TODO`/"implement later"/"similar to above". Every code step shows the actual code read from the real files. The one `# follow-up:` note (Task 6, git-commit-author parity) is a deliberate, decision-3-mandated deferral comment in shipped code, not a plan placeholder — it documents a non-goal, not undone work.

**3. Type consistency:**
- `set_turn_provenance(workspace_id, *, triggering_turn_id, triggering_prompt, created_by, origin_session_id)` — identical signature across contract Protocol, fake_adapter, claude_adapter, registry, controller, and the processor call site.
- `ToolContext` provenance fields (`triggering_turn_id: uuid.UUID | None`, `triggering_prompt: str | None`, `created_by: int | None`, `origin_session_id: str | None`, `agent_view_sections: dict[int, str]`) match the kwargs threaded into `propose_prose_edit` / `propose_citation_insert` 1:1.
- `propose_citation_insert(...)` kwargs match `propose_citation_impl`'s call site and `propose_prose_edit`'s provenance-kwarg shape.
- Accept result keys: prose uses `result.commit_sha` (attribute, `SaveResult`); citation uses `result["commit_sha"]` (dict, `_insert_citation_locked`) — both flagged in-code where they diverge.
- Tool names are bare and identical in three places (the `@tool("propose_edit"/...)` decorator, the registry return list, the adapter `allowed_tools`) — the Step 7.10 audit asserts this equality mechanically.

**4. Final-state invariants (verifiable at end of Task 7):**
- `_collect_tools` returns exactly 19 tools; `allowed_tools` == those 19 names; `permission_mode == "default"`.
- `edit_proposal_impl` / `replace_proposal_impl` deleted; the 4 citation `*_impl` async wrappers retained (test-fixture + `_insert_citation_locked` accept-replay dependency).
- `accept_proposed_change` handles `prose_edit` + `citation_insert`; still raises `NotImplementedError` for `target_artifact="spec"`.
