"""SQLAlchemy-backed port implementations. Tool code may NOT import this package.

The split is the whole mechanism: `ports/` is stdlib-only and tool-facing, this
package holds every session, model and statement. F3c installs the import gate
that enforces it; F3b establishes the shape.
"""

from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from call_manager.proposal_assist.port_impl.accounting import SqlAccounting
from call_manager.proposal_assist.port_impl.effects import SqlEffects
from call_manager.proposal_assist.port_impl.queries import SqlQueries
from call_manager.proposal_assist.ports import Ports

__all__ = ["build_ports"]


def build_ports(session_factory: sessionmaker) -> Ports:
    """Construct the capability bundle for one workspace's tool surface.

    Called by the adapter factories in F3d. Nothing calls it in F3b — the slice
    is additive and no production caller is converted.
    """
    return Ports(
        queries=SqlQueries(session_factory),
        effects=SqlEffects(session_factory),
        accounting=SqlAccounting(session_factory),
    )
