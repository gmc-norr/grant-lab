"""Tool-facing capability contracts — stdlib only.

Spec: docs/superpowers/specs/2026-08-18-fence-session-ownership-design.md §3.

Tools receive capabilities, never handles. NOTHING in this package may import
sqlalchemy, call_manager.db.models, or any project module that imports either:
an attached ORM instance carries object_session(instance), which is the full
Session again — the leak that defeated the forwarding-proxy design. The
implementations live in call_manager.proposal_assist.port_impl, which tool code
may not import (the gate lands in F3c).
"""

from __future__ import annotations

from dataclasses import dataclass

from call_manager.proposal_assist.ports.accounting import Accounting
from call_manager.proposal_assist.ports.effects import Effects
from call_manager.proposal_assist.ports.queries import Queries

__all__ = ["Ports"]


@dataclass(frozen=True, slots=True)
class Ports:
    """The injected bundle a tool holds instead of a session.

    An INJECTED bundle, never module globals: `effects._session_factory` would be
    reachable from any module that can import `effects`, which after F3d is every
    tool. Frozen, so a tool cannot swap in its own implementation.
    """

    queries: Queries
    effects: Effects
    accounting: Accounting
