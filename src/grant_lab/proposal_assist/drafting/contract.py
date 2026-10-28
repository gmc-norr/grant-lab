"""Adapter contract for the drafting agent.

Two concrete adapters live in this package:
  - FakeAgentAdapter (fake_adapter.py) — scriptable test double
  - ClaudeAgentSDKAdapter (claude_adapter.py) — wraps claude-agent-sdk

Everything else in the codebase talks to a DraftingAgent — never directly
to the SDK. An SDK upgrade should touch only claude_adapter.py.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class AgentHandle:
    """Opaque token returned by launch(); passed back to send_turn /
    terminate. The `state` dict is adapter-internal (subprocess pid,
    recorder, etc.) — controller code MUST NOT introspect it."""

    workspace_id: uuid.UUID
    state: dict[str, Any] = field(default_factory=dict)


@dataclass
class LaunchContext:
    """Everything an adapter needs at session-creation time.

    Built by WorkspaceController.launch(); passed verbatim to
    DraftingAgent.launch().

    resume_session_id is the SDK session UUID returned by the previous
    successful turn's ResultMessage. When set, the adapter passes it to
    ClaudeAgentOptions(resume=...) so the SDK natively rehydrates prior
    conversation history without us replaying turns. When None, the
    subprocess starts fresh — the model has no memory of past turns.

    B4 will: capture session_id from TurnDoneEvent, persist it per
    workspace, and pass it back here on the next launch.
    """

    workspace_id: uuid.UUID
    resume_session_id: str | None = None


# Stream event variants. send_turn yields these in order during a turn.
# B4 will persist these into chat_turns rows; B6 will forward them via SSE.


@dataclass
class AssistantTextEvent:
    """A chunk of assistant text (the SDK may stream multiple)."""

    text: str


@dataclass
class ToolUseEvent:
    """Agent invoked a tool. The adapter has already routed the call
    to the registered handler; this event is informational so the UI
    can show 'calling tool X...'."""

    tool_name: str
    tool_use_id: str
    input: dict[str, Any]


@dataclass
class ToolResultEvent:
    """Result of a tool invocation, fed back into the model loop."""

    tool_use_id: str
    content: str
    is_error: bool


@dataclass
class TurnDoneEvent:
    """Terminal event for a turn. Carries token + model telemetry +
    SDK session id so chat_turns rows can be stamped and the workspace
    can persist the session id for the next launch's native resume."""

    tokens_in: int | None
    tokens_out: int | None
    model: str | None
    session_id: str | None = None


StreamEvent = AssistantTextEvent | ToolUseEvent | ToolResultEvent | TurnDoneEvent


@dataclass(frozen=True)
class TurnUsage:
    """What a turn had ALREADY billed when it failed, timed out or was cancelled.

    `TurnDoneEvent` is the success channel: it only exists when the turn reached
    its `ResultMessage`. The two most expensive turns there are never reach one —
    an `error_max_turns` / `error_during_execution` result, and a turn cancelled
    at the `TURN_MAX_SECONDS` wall clock — and both had been billed for
    everything they streamed. Review 2026-10-26 (M-PA-1) measured the result:
    `ledger=[('chat_turn','failed',0,0)] ws_cap_used=(0,0)`, so a looping
    workspace stayed invisible to both the per-workspace and the global cap.

    So the failure channel carries usage too, ON THE EXCEPTION (see
    `attach_turn_usage`). Two different numbers live here depending on the path,
    and the difference is deliberate:

      * an error `ResultMessage` HAS a `usage` — that is the authoritative
        total, and it is what gets attached;
      * a cancel never gets one, so the running `AssistantMessage` tally is
        attached instead. Undercounted (AssistantMessage.usage omits
        cache_creation, which dominates), but measured — unlike a total that
        never arrived, which must not be invented.

    `model` rides along because the processor's failure path has no other source
    for it (`TurnDoneEvent.model` is the only one on the success path).
    """

    tokens_in: int | None = None
    tokens_out: int | None = None
    model: str | None = None

    @property
    def billed(self) -> bool:
        """True when a NUMBER was measured. `TurnUsage(None, None)` means the
        request went out and nothing came back to count — which is not the same
        fact as zero, and callers that must tell them apart check this."""
        return bool(self.tokens_in) or bool(self.tokens_out)


# Private, and deliberately not a public attribute name: the readers below are
# handed arbitrary BaseExceptions (a CancelledError from the drain's wall-clock
# bound included), so the key must be one nothing else in any library writes.
_TURN_USAGE_ATTR = "_grant_lab_turn_usage"

# A real chain is 2 links (CancelledError -> TimeoutError). Ten is slack for a
# nested wrap, and a hard stop for a chain someone built by hand.
_MAX_CAUSE_CHAIN_DEPTH = 10


def attach_turn_usage(exc: BaseException, usage: TurnUsage) -> None:
    """Record on `exc` what the turn it is killing had already billed.

    The exception is the only channel available. An async generator cannot yield
    a final event once CancelledError has been thrown into it, and the running
    tally lives in `send_turn`'s frame, which the cancel destroys — so the tally
    rides out on the exception that destroyed it.

    NEVER overwrite: the innermost raiser knows the most (the error
    `ResultMessage` branch attaches the authoritative total; the outer handler
    would otherwise replace it with the partial AssistantMessage tally).
    """
    if getattr(exc, _TURN_USAGE_ATTR, None) is None:
        # suppress: a slotted exception class would refuse the attribute, and
        # losing the tally must never be what kills the turn's finalization.
        with contextlib.suppress(AttributeError):
            setattr(exc, _TURN_USAGE_ATTR, usage)


def turn_usage_from_error(exc: BaseException | None) -> TurnUsage | None:
    """The usage attached to `exc` OR anywhere down its cause chain, else None.

    None is itself load-bearing and means "no outbound request is known to have
    happened": `MissingAnthropicKeyError` is raised before `client.query`, and a
    launch failure never reaches `send_turn` at all, so neither carries an
    attachment and neither should be charged or estimated against a cap.

    THE CHAIN WALK IS NOT OPTIONAL. The four background pass families all stream
    inside `asyncio.wait_for`, and on expiry
    `asyncio.timeouts.Timeout.__aexit__` does `raise TimeoutError from exc_val`
    — so the `CancelledError` that `send_turn` attached the tally to is NOT the
    exception their `except` block receives; it survives only as `__cause__`.
    Reading the raised exception alone returned None there, which left every
    timed-out pass recording 0 against `coherence_daily_token_cap`,
    `evaluator_daily_token_cap`, `uncited_claims_daily_token_cap` and
    `review_dollar_cap_default` — the exact runaway those caps exist to stop,
    and the most expensive turn in the system. (The interactive chat turn is
    unaffected: `process_turn_once` catches the `CancelledError` in its own
    frame, before `drain.py`'s `wait_for` converts it. That is why the one
    tested path passed while all four pass families did not.)

    `__context__` is followed as well as `__cause__` so an implicit re-raise
    (`raise X` inside an `except` with no `from`) keeps the tally too. Walking
    is safe only because the attribute is private to this module and written
    nowhere but `send_turn`, so nothing else on the chain can forge one.

    Cycle- and depth-guarded: `__cause__` is writable, so a caller could hand us
    a ring, and losing the tally must never be preferable to hanging the
    finalizer.
    """
    cur = exc
    seen: set[int] = set()
    while cur is not None and len(seen) < _MAX_CAUSE_CHAIN_DEPTH:
        if id(cur) in seen:
            break
        seen.add(id(cur))
        usage = getattr(cur, _TURN_USAGE_ATTR, None)
        if isinstance(usage, TurnUsage):
            return usage
        # Explicitly "first non-None", not exception truthiness.
        cur = cur.__cause__ if cur.__cause__ is not None else cur.__context__
    return None


def done_or_billed(
    done: TurnDoneEvent | None, exc: BaseException | None
) -> TurnDoneEvent | None:
    """`done`, or a stand-in built from what `exc` says the turn billed.

    The four background pass families (coherence, evaluator, mock-panel review,
    uncited claims) each keep a `done: TurnDoneEvent | None` and then spend it
    half a dozen ways on the failure path — the ledger row, the job's display
    columns, `_fail`. One call to this at the top of each `except` block gives
    every one of those the billed tally, which is why it is a shared helper
    rather than four copies of the same unpacking.

    `session_id` is None on the stand-in: a turn that never produced a
    `ResultMessage` has no SDK session id to persist, and the pass runners store
    exactly what they are given.
    """
    if done is not None:
        return done
    usage = turn_usage_from_error(exc)
    if usage is None or not usage.billed:
        return None
    return TurnDoneEvent(
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        model=usage.model,
        session_id=None,
    )


class SDKTurnError(RuntimeError):
    """Raised by an adapter's send_turn when the backend signals an error
    result (rate-limit, 5xx, etc.) instead of completing the turn. Caller
    (B4's turn processor) catches this to mark the turn as failed without
    tearing down the handle. Lives on the contract so callers can catch
    it without importing claude_adapter (which would force eager SDK
    import).

    `usage` is what the error result had already billed (M-PA-1). It is both an
    attribute and an `attach_turn_usage` attachment, so a caller can read it
    off this class directly or go through `turn_usage_from_error` and treat
    every failed turn — this one, a timeout, a cancel — the same way."""

    def __init__(
        self,
        message: str,
        *,
        status: str | None = None,
        usage: TurnUsage | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.usage = usage
        if usage is not None:
            attach_turn_usage(self, usage)


class DraftingAtCapacity(RuntimeError):
    """Raised by the registry when a NET-NEW subprocess launch is requested but
    the live-subprocess ceiling (DRAFTING_MAX_LIVE_SUBPROCESSES) is reached and
    every live handle is mid-turn (so none can be evicted to make room).

    This is a transient "busy" signal: the API layer maps it to a retriable
    HTTP 429, and the drain tick leaves the queued turn for the next tick.
    Distinct from LockNotAcquired (cross-process contention) — this is the
    in-process self-imposed cap that keeps drafting from starving the engine's
    request pool. Lives on the contract so callers can catch it without
    importing the registry's concrete classes."""


class WorkspaceRetired(RuntimeError):
    """The workspace is retired (soft-deleted), so live per-workspace state may
    not be created for it.

    Two families of raise site, both installed by Slice B-retire:

    * TURNS — ``SubprocessRegistry.claim_launch_or_wait`` /
      ``fulfill_pending_launch`` and ``WorkspaceController.launch``. Turn-path
      callers finalize instead of sending.
    * COLLAB — ``RoomManager.acquire`` / ``operate``, including the re-checks
      after the out-of-lock ``await load()``. The WS route maps this to close
      1008; on HTTP routes there is no local ``except``, and main.py's
      app-level handler turns it into the same 404 as WorkspaceDeleted (which
      it is NOT a subclass of — both are bare RuntimeErrors).

    NOTE: this is a linearization point for CREATION, not a race-free gate on
    everything — a deletion committing after the check can still meet an
    in-flight streaming query, and the reconciliation sweep remains the retry."""

    def __init__(self, workspace_id: uuid.UUID) -> None:
        super().__init__(f"workspace {workspace_id} is retired")
        self.workspace_id = workspace_id


class AgentSdkConfigDirError(RuntimeError):
    """The app-owned CLAUDE_CONFIG_DIR is not a directory this app owns.

    Raised by ``claude_adapter.ensure_agent_sdk_config_dir`` instead of
    chmod'ing or handing the CLI a path the app cannot prove it owns: a symlink
    planted at that name (any of its four shapes — to a directory, to a file,
    dangling, or out of the tree), a plain file there or at one of its parents,
    or a leaf that still does not resolve to itself after creation. Fail closed:
    a launch that cannot prove where the CLI will keep its credentials must not
    start.

    NON-RETRYABLE, and that is why it lives on the contract rather than in
    ``claude_adapter``: it depends only on the filesystem shape at
    ``<workspaces_dir>/.agent-sdk-config``, which no number of drain ticks
    changes, so ``turns/processor.py`` must terminalize the turn instead of
    leaving it ``queued`` for a 5-second retry loop (review 2026-10-26, F4 and
    its re-review). The processor cannot import ``claude_adapter`` to catch it —
    that module is the ONE place the codebase imports ``claude_agent_sdk``, and
    the match build edition depends on the drain path not pulling the SDK in —
    which is the same reason ``SDKTurnError`` lives here. ``claude_adapter``
    re-exports the name, so every existing import of it keeps working.
    """


@runtime_checkable
class DraftingAgent(Protocol):
    """The adapter contract.

    `launch` is awaited once per (workspace, idle-window). It does whatever
    initialization the adapter needs — for Claude this means connecting
    the SDK client (optionally resuming a prior session via
    context.resume_session_id); for Fake it just records the call.

    `send_turn` is an async generator: caller iterates with `async for
    event in adapter.send_turn(handle, msg)`. The final event is always
    TurnDoneEvent.

    `user_message` is either a plain string (cheap text-only path) or a
    list of Anthropic content-block dicts (text and/or image with a
    base64 source). The dicts are ALREADY in Anthropic API shape —
    adapters MUST NOT do attachment-id resolution. That lives in the
    attachments/resolver module called by the processor.

    `terminate` is idempotent: calling on an already-torn-down handle is
    a no-op (logs but doesn't raise)."""

    async def launch(self, context: LaunchContext) -> AgentHandle: ...

    async def send_turn(
        self,
        handle: AgentHandle,
        user_message: str | list[dict[str, Any]],
    ) -> AsyncIterator[StreamEvent]: ...

    async def terminate(self, handle: AgentHandle) -> None: ...

    # OPTIONAL (intentionally NOT a Protocol member, so runtime_checkable
    # isinstance() doesn't require it): adapters MAY provide
    #
    #     async def interrupt(self, handle: AgentHandle) -> None
    #
    # for a best-effort graceful stop of an in-flight turn WITHOUT tearing down
    # the subprocess (so a timed-out turn can stop cleanly and the handle be
    # reused). The registry/controller probe for it via getattr and skip when
    # absent (the Fake doesn't implement it). Callers MUST treat it as advisory
    # and keep terminate() as the guaranteed backstop.

    async def set_turn_provenance(
        self,
        workspace_id: uuid.UUID,
        *,
        triggering_turn_id: uuid.UUID | None,
        triggering_prompt: str | None,
        created_by: int | None,
        origin_session_id: str | None,
        lifecycle_generation: int,
        active_turn_job_id: uuid.UUID,
        allowed_tool_names: set[str] | None = None,
        turn_selection: dict | None = None,
    ) -> None:
        """Stamp server-trusted provenance onto the workspace's retained
        ToolContext before the next send_turn. Called UNCONDITIONALLY every turn
        by process_turn_once (the subprocess is reused across turns, so a missed
        stamp would leak the prior turn's provenance into the propose_* tools).
        A no-op if the workspace has no live handle/ctx (the next launch rebuilds
        the ctx, and the processor re-stamps before send_turn anyway)."""
        ...
