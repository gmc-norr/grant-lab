# Call-Monitor Whole-Project Review

**Date:** 2026-09-01
**Scope:** Current `master` at `183f8cdd`, including the uncommitted branch-identity work
**Method:** Read-only architecture, backend, frontend, security, concurrency, deployment,
and test review; targeted reproductions plus repository-wide validation
**Code changes:** None. This document and its index entry are the only review-authored changes.

## Executive assessment

No current critical defect was confirmed. The 2026-08-29 review's critical CRDT
varuint denial of service is fixed and its bounded-decoder tests pass. Several other
high-priority findings from that review are also fixed: SSRF refusal now fails closed,
ingestion settles upstream scalar types, citation LLM egress is gated, hosted-model spend
uses a shared ledger, SMTP transport selection is unified, git history runs off the event
loop, collab sync no longer latches on an incremental update, reconnect exhaustion is
reported honestly, and artifact mutations invalidate version queries.

The architecture remains strong around authorization, lifecycle fencing, spend accounting,
and CRDT persistence. The current working tree is nevertheless not release-ready: an
in-progress branch-identity fix has not been propagated through the React editor and leaves
TypeScript red. Beyond that immediate blocker, three systemic risks remain: blocking work
on the single application event loop, an unmonitored scheduler ownership lock, and branch
operations that expose intermediate shared-worktree state to lock-free readers.

## Findings

### High — The in-progress branch guard is not wired through the editor

The backend now requires `base_branch` in `SaveSectionIn`
([workspaces.py](../../../src/call_manager/web/api/workspaces.py#L155)), and the frontend
API mutation types require and send it
([editor.ts](../../../frontend/src/api/editor.ts#L5)). The editor's local `SaveVars` type
and `onSave` payload still omit it
([ArtifactSectionEditor.tsx](../../../frontend/src/editor/ArtifactSectionEditor.tsx#L19),
[ArtifactSectionEditor.tsx](../../../frontend/src/editor/ArtifactSectionEditor.tsx#L197)).

Measured consequence: `pnpm exec tsc --noEmit` reports five errors across
`editor.test.tsx`, `DraftPanel.tsx`, and `SpecPanel.tsx`. A stale built SPA can still load,
but every draft or spec section save it submits without `base_branch` receives HTTP 422.
The server-side guard itself is correctly placed under the commit mutex in
[save.py](../../../src/call_manager/proposal_assist/editing/save.py#L229).

### High — Synchronous drafting work can block the application event loop

`search_references_impl` is async but calls the synchronous embedder directly
([references.py](../../../src/call_manager/proposal_assist/tools/references.py#L89),
[references.py](../../../src/call_manager/proposal_assist/tools/references.py#L124)).
`DraftingController.launch` is also async but performs synchronous session work, shared-lease
acquisition, git reconciliation, and citekey reconciliation before its next await
([controller.py](../../../src/call_manager/proposal_assist/drafting/controller.py#L81),
[controller.py](../../../src/call_manager/proposal_assist/drafting/controller.py#L150)).

These functions run on the same application loop used by HTTP, WebSocket, SSE, and
scheduler-dispatched coroutines. A slow Ollama request, database lock wait, or git operation
therefore stalls unrelated users and workspaces for the duration. The configured Ruff ASYNC
rules pass because they do not recognize arbitrary synchronous library calls.

### High — Scheduler ownership is lost silently if its connection dies

`SchedulerLock` keeps one PostgreSQL connection open to retain a session-level advisory
lock ([scheduler.py](../../../src/call_manager/scheduler.py#L54)). After acquisition, the
connection and lock are never probed again. If it dies, PostgreSQL releases the lock while
the existing in-process scheduler continues running; another instance can then acquire the
same lock and start a second scheduler. This violates the project's single-instance hard
constraint. The DB-only health endpoint
([health.py](../../../src/call_manager/web/routes/health.py#L9)) cannot detect the condition.

### Medium — A branch read can pair the old name with the new bytes

`switch_branch` checks out the target worktree
([branches.py](../../../src/call_manager/proposal_assist/branches.py#L251)) before the route
commits `workspaces.current_branch`
([web/branches.py](../../../src/call_manager/proposal_assist/web/branches.py#L212)). A
concurrent draft or spec read takes no commit mutex and independently reads the branch name
from PostgreSQL and the content from the shared worktree
([workspaces.py](../../../src/call_manager/web/api/workspaces.py#L271)).

The repository contains a positive regression test that observes the old name together with
the target branch's HEAD and bytes. The new `base_branch` save guard prevents those mixed
bytes from later being committed under the wrong branch, once its frontend rollout is
complete, but the read response itself remains inconsistent.

### Medium — Document-replacement reconnects have no backoff or ceiling

Close code 4409 returns before normal reconnect scheduling
([collabProvider.ts](../../../frontend/src/collab/collabProvider.ts#L267)). The callback in
`useCollabDoc` immediately increments `documentGeneration`, destroys the old pair, and
constructs a new Y.Doc and socket
([useCollabDoc.ts](../../../frontend/src/collab/useCollabDoc.ts#L44)).

The pinned test
([useCollabDoc.test.tsx](../../../frontend/src/collab/useCollabDoc.test.tsx#L64)) sent 20
replacement closes and measured 21 sockets, with no terminal state. A persistently stale
generation or repeated switches can therefore create an unbounded immediate reconnect loop.
The bound must live across provider generations; a per-provider attempt counter resets on
every replacement.

### Medium — External call text can forge the ranking prompt fence

The ranking prompt uses a literal `CALL_BODY>>>` closing marker
([prompt.py](../../../src/call_manager/ranking/prompt.py#L155)), while `build_messages`
interpolates upstream title, programme, and body text without escaping that token
([prompt.py](../../../src/call_manager/ranking/prompt.py#L216)). An executed probe with the
marker inside `summary` produced two closing markers and placed attacker-controlled text
after the first. The system message still labels the whole call as untrusted, so this is not
a role-boundary escape, but it contradicts the prompt's own trust boundary and can bias
local or hosted ranking output that reaches analysts and digests.

### Medium — An open SSE stream outlives session and membership revocation

`stream_turn` performs `require_member` once before constructing the response
([turns.py](../../../src/call_manager/web/api/turns.py#L397)). The polling generator then
opens fresh sessions but never rechecks the user's status, session epoch, or membership
([turns.py](../../../src/call_manager/web/api/turns.py#L314)). A logout, account disable,
or workspace-member removal therefore does not stop an already-open stream; it continues
emitting chat turns until the job terminates or the client disconnects.

### Low — Claim-analysis failures expose raw internal exceptions

The analysis pipeline stores `str(exc)` as `last_error`
([pipeline.py](../../../src/call_manager/proposal_assist/analysis/pipeline.py#L650)), and the
annotations API returns that value to workspace members
([annotations.py](../../../src/call_manager/web/api/annotations.py#L191)). Depending on the
failure source, this can disclose SQL, filesystem, model-client, or internal URL details.
Persist a stable public error code/message and keep the raw exception in structured logs.

### Low — Test connection-leak warnings bypass the intended warning gate

Full pytest runs repeatedly emitted `PytestUnraisableExceptionWarning` reports for psycopg
connections deleted while still open. The configured `error::ResourceWarning` filter does
not fail these because pytest wraps destructor-time warnings in another warning class. The
suite can therefore pass while the connection-lifecycle invariant the filter was intended
to enforce is violated.

## Validation record

| Check | Measured result |
|---|---|
| Focused CRDT decoder and branch regressions | 4 passed; bounded decoder confirmed |
| Save/API slice after branch-guard edits | 104 passed |
| Latest complete backend run | 4,380 passed, 1 failed, 1 skipped in 84.15 s |
| Cause of the one backend failure | New test read `response.json()["detail"]`; API uses the standard `error.message` envelope. The assertion was edited afterward; full suite not rerun after that edit. |
| Complete frontend unit run | 125 files, 872 tests passed; run preceded the latest `editor.ts` edit |
| Latest frontend type-check | Failed with 5 `base_branch` propagation errors |
| Production SPA build | Succeeded before the latest `editor.ts` edit; emitted chunk-size warnings |
| Ruff | Passed on the updated Python tree |
| Prompt-fence probe | 2 closing markers produced from one injected marker |
| Patch hygiene | `git diff --check` passed |

The ignored `api_key.txt` is not tracked, but its measured mode is `0660`; if it contains a
live credential, `0600` would be a tighter local boundary. Two untracked `.orig` source
copies are also present. Neither is imported or shipped by the normal Python/TypeScript
build, but both are easy to commit accidentally.

## Priority order

1. Finish the `base_branch` frontend and test propagation, regenerate OpenAPI types, then
   rerun TypeScript, frontend tests/build, and the complete backend suite.
2. Close the mixed branch-name/worktree read window.
3. Bound replacement reconnects across Y.Doc generations.
4. Move synchronous embedding, reconciliation, git, and database work off the app loop.
5. Monitor scheduler-lock ownership and fail stop or degrade readiness when it is lost.
6. Escape or structurally encode ranking call data instead of relying on a forgeable marker.
7. Revalidate identity and membership during SSE polling.
8. Replace member-visible raw analysis errors and make connection leaks fail tests.

## Final assessment

The project has strong foundations and has closed most of the August 29 review's highest
risk findings without architectural churn. The current risk is concentrated rather than
diffuse: branch identity is being added to a mature write path while readers, sockets,
frontend mutation types, generated contracts, and tests all observe different portions of
that transition. Complete that change as one contract update before release. Afterward,
the event-loop and scheduler-lock findings are the highest-impact architectural work.
