"""Test double for DraftingAgent. Records launch contexts + turns +
terminations; lets tests script per-turn event streams via
`script_next_turn(events)`."""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator
from typing import Any

from call_manager.proposal_assist.drafting.contract import (
    AgentHandle,
    LaunchContext,
    StreamEvent,
    TurnDoneEvent,
)


class FakeAgentAdapter:
    """Records launch/send_turn/terminate; lets tests script per-turn
    event streams via `script_next_turn(events)`.

    Inspection attributes for assertions:
      - launches: list[LaunchContext]
      - turns: list[tuple[AgentHandle, str | list[dict]]]
      - terminations: list[AgentHandle]
      - last_user_message: the user_message from the most recent send_turn call
        (str or list[dict]). None if send_turn has not been called yet.
    """

    def __init__(self) -> None:
        self.launches: list[LaunchContext] = []
        self.turns: list[tuple[AgentHandle, str | list[dict[str, Any]]]] = []
        self.terminations: list[AgentHandle] = []
        self.last_user_message: str | list[dict[str, Any]] | None = None
        self._scripted: deque[list[StreamEvent]] = deque()
        self.provenance: list[tuple[Any, Any, str | None, int | None, str | None]] = []
        # SP2a-2: track which workspaces have been launched, so a test can prove
        # the processor stamps provenance AFTER launch (the cold-launch ordering
        # the plan was revised to fix). provenance_saw_launch[i] is True iff
        # set_turn_provenance's workspace had already been launch()ed.
        self._launched_workspaces: set = set()
        self.provenance_saw_launch: list[bool] = []

    def script_next_turn(self, events: list[StreamEvent]) -> None:
        """Queue the events the next send_turn() invocation should yield.
        If you don't script, send_turn yields a single bare TurnDoneEvent."""
        self._scripted.append(events)

    async def launch(self, context: LaunchContext) -> AgentHandle:
        self.launches.append(context)
        self._launched_workspaces.add(context.workspace_id)
        return AgentHandle(
            workspace_id=context.workspace_id,
            state={
                "fake": True,
                "resume_session_id": context.resume_session_id,
            },
        )

    async def send_turn(
        self,
        handle: AgentHandle,
        user_message: str | list[dict[str, Any]],
    ) -> AsyncIterator[StreamEvent]:
        self.last_user_message = user_message
        self.turns.append((handle, user_message))
        if self._scripted:
            scripted = self._scripted.popleft()
        else:
            scripted = [
                TurnDoneEvent(
                    tokens_in=None,
                    tokens_out=None,
                    model=None,
                )
            ]
        for event in scripted:
            yield event

    async def terminate(self, handle: AgentHandle) -> None:
        self.terminations.append(handle)

    async def set_turn_provenance(
        self,
        workspace_id,
        *,
        triggering_turn_id,
        triggering_prompt,
        created_by,
        origin_session_id,
    ) -> None:
        self.provenance_saw_launch.append(workspace_id in self._launched_workspaces)
        self.provenance.append(
            (
                workspace_id,
                triggering_turn_id,
                triggering_prompt,
                created_by,
                origin_session_id,
            )
        )
