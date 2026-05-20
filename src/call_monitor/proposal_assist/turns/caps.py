"""Per-workspace daily-token-cap enforcement.

Stockholm-day boundary: tokens count toward "today" if their chat_turn's
created_at falls within the current Europe/Stockholm day. Cap defaults
live on Settings; per-workspace overrides live on
workspaces.daily_token_cap_{in,out}_override.

This module is enforcement-only. B8 owns the user-facing "you've hit the
cap" UX (banner, admin override widget, "paused until midnight" status).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from call_monitor.config import Settings
from call_monitor.db.models import ChatTurn, Workspace

_STOCKHOLM = ZoneInfo("Europe/Stockholm")
_UTC = ZoneInfo("UTC")


@dataclass
class CapCheckResult:
    allowed: bool
    used_in: int
    used_out: int
    cap_in: int
    cap_out: int
    reason: str | None = None  # 'input_cap_exceeded' | 'output_cap_exceeded' | None


class TokenCapExceeded(RuntimeError):
    def __init__(
        self,
        workspace_id: uuid.UUID,
        *,
        used_in: int,
        used_out: int,
        cap_in: int,
        cap_out: int,
        reason: str,
    ) -> None:
        super().__init__(
            f"workspace {workspace_id} {reason}: "
            f"used_in={used_in} used_out={used_out} cap_in={cap_in} cap_out={cap_out}"
        )
        self.workspace_id = workspace_id
        self.used_in = used_in
        self.used_out = used_out
        self.cap_in = cap_in
        self.cap_out = cap_out
        self.reason = reason


def _stockholm_day_start_utc() -> datetime:
    """Return UTC datetime corresponding to today's 00:00 in Europe/Stockholm."""
    now_se = datetime.now(_STOCKHOLM)
    midnight_se = now_se.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_se.astimezone(tz=_UTC)


def check_daily_token_cap(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    settings: Settings,
) -> CapCheckResult:
    """Sum tokens used today against the effective cap. Read-only — does
    not raise; caller (submit_user_turn) decides how to react."""
    day_start_utc = _stockholm_day_start_utc()

    sums = session.execute(
        select(
            func.coalesce(func.sum(ChatTurn.tokens_in), 0),
            func.coalesce(func.sum(ChatTurn.tokens_out), 0),
        ).where(
            ChatTurn.workspace_id == workspace_id,
            ChatTurn.created_at >= day_start_utc,
        )
    ).one()
    used_in, used_out = int(sums[0]), int(sums[1])

    ws = session.scalar(select(Workspace).where(Workspace.id == workspace_id))
    cap_in = (
        ws.daily_token_cap_in_override
        if ws is not None and ws.daily_token_cap_in_override is not None
        else settings.daily_token_cap_input
    )
    cap_out = (
        ws.daily_token_cap_out_override
        if ws is not None and ws.daily_token_cap_out_override is not None
        else settings.daily_token_cap_output
    )

    reason: str | None = None
    if used_in >= cap_in:
        reason = "input_cap_exceeded"
    elif used_out >= cap_out:
        reason = "output_cap_exceeded"

    return CapCheckResult(
        allowed=reason is None,
        used_in=used_in,
        used_out=used_out,
        cap_in=cap_in,
        cap_out=cap_out,
        reason=reason,
    )
